"""HTTP 应用：WebRTC 信令、对时、会话与发言接口（``sessions_api.py``）、静态站点。

信令的写法照抄 Pipecat 自带运行器的 ``_setup_webrtc_routes``（docs/pipecat-notes.md §2）；
我们不用它的 ``main()``，因为那会接管整个 FastAPI 应用和命令行。
接口约定见 docs/interfaces.md §5：除信令外全部是 JSON，业务错误返回 ``{"error", "code", "params"}``（§5.9）。
健康接口（``health_api.py``）使用 §5.8 的独立状态快照。
截图、任务、报告、导出的接口各在自己的模块里（``frames_api.py`` 等），访问口令在 ``auth.py``。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pipecat.transports.smallwebrtc.connection import IceServer
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from starlette.exceptions import HTTPException as StarletteHTTPException
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from agentic_meeting.agent.runner import build_runner
from agentic_meeting.agent.tasks import RESTART_REASON, Runner, TaskManager
from agentic_meeting.config import REPO_ROOT, AppConfig, secret
from agentic_meeting.messages import message
from agentic_meeting.pipeline.background import BackgroundModel, InferenceLLM
from agentic_meeting.pipeline.bot import AppResources, run_bot
from agentic_meeting.pipeline.digest import DigestWorker
from agentic_meeting.pipeline.prompts import load_prompt
from agentic_meeting.pipeline.report import ReportWorker
from agentic_meeting.pipeline.services import (
    build_agent_llm,
    build_realtime_llm,
    caption_provider,
)
from agentic_meeting.pipeline.session import SessionManager
from agentic_meeting.screen.attach import FrameAttacher
from agentic_meeting.screen.caption import CaptionWorker
from agentic_meeting.screen.ingest import FrameIngestor
from agentic_meeting.store.db import CleanupPlan, SessionBusy, Store
from agentic_meeting.store.embeddings import EmbeddingClient, EmbeddingWorker
from agentic_meeting.store.retention import RetentionWorker
from agentic_meeting.web import access as meeting_access
from agentic_meeting.web import (
    auth,
    export,
    frames_api,
    health_api,
    reports_api,
    sessions_api,
    sso,
    tasks_api,
)
from agentic_meeting.web.errors import ApiError, error_response, invalid

DEFAULT_STATIC_DIR = REPO_ROOT / "client" / "dist"

BotRunner = Callable[[Any, Any, AppResources], Awaitable[None]]

# 应用关闭时，断开连接之后再等管线自己结束的时间；超过就取消。
BOT_EXIT_TIMEOUT_SECS = 5.0
# uvicorn 自己记着的连接都关完之后，再等 asyncio 服务器确认连接全部关闭的时间（见 MeetingServer）。
SERVER_CLOSE_GRACE_SECS = 2.0

NOT_BUILT_HTML = """<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>请先构建客户端</title>
<body style="font-family:system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem;line-height:1.7">
<h1>请先构建客户端</h1>
<p>服务端已经在运行，但浏览器端页面还没有构建。在仓库根目录运行：</p>
<pre style="background:#f4f4f5;padding:1rem;border-radius:6px">cd client &amp;&amp; npm install &amp;&amp; npm run build</pre>
<p>构建完成后刷新本页即可，不需要重启服务端。</p>
</body></html>
"""


def ice_servers_for_browser(cfg: AppConfig) -> list[dict[str, Any]]:
    """``server.ice_servers`` 写成浏览器 ``RTCIceServer`` 的形状；凭据此时从环境变量读出（interfaces.md §5.2）。"""
    servers: list[dict[str, Any]] = []
    for ice in cfg.server.ice_servers:
        entry: dict[str, Any] = {"urls": list(ice.urls)}
        if ice.username:
            entry["username"] = ice.username
            entry["credential"] = secret(ice.credential_env) or ""
        servers.append(entry)
    return servers


def _bot_done(bots: set[asyncio.Task[None]]) -> Callable[[asyncio.Task[None]], None]:
    def done(task: asyncio.Task[None]) -> None:
        bots.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.opt(exception=task.exception()).error("会议管线异常结束")

    return done


async def _finish_bots(bots: set[asyncio.Task[None]]) -> None:
    """应用关闭时：连接已断开，等管线自己结束（有超时），还没结束的取消。"""
    pending = set(bots)
    if not pending:
        return
    _done, late = await asyncio.wait(pending, timeout=BOT_EXIT_TIMEOUT_SECS)
    if late:
        logger.warning("应用关闭时有 {} 条会议管线没有及时结束，取消", len(late))
        for task in late:
            task.cancel()
        await asyncio.gather(*late, return_exceptions=True)


def create_app(
    cfg: AppConfig,
    *,
    handler: SmallWebRTCRequestHandler | None = None,
    bot: BotRunner = run_bot,
    static_dir: Path | None = None,
    store: Store | None = None,
    embedder: EmbeddingClient | None = None,
    background_llm: InferenceLLM | None = None,
    agent_llm: InferenceLLM | None = None,
    task_runner: Runner | None = None,
) -> FastAPI:
    """创建应用。

    ``handler``、``bot``、``static_dir``、``store`` 可注入（测试用）。不注入 ``handler`` 时在 lifespan 里按配置创建，
    应用关闭时断开全部连接；不注入 ``store`` 时在 lifespan 里打开 ``<data_dir>/meetings.db``，关闭时关掉。
    嵌入：注入了 ``embedder`` 就用它；没注入且 ``store`` 也没注入（正式运行）时按配置创建；
    注入了 ``store`` 而没给 ``embedder``（测试）时不做嵌入，免得测试去连本机的嵌入服务。
    后台模型（画面摘要、滚动纪要）同理：注入了 ``background_llm`` / ``agent_llm`` 就用它们；都没注入且 ``store``
    也没注入时按配置创建；只注入了 ``store`` 时没有后台模型，截图的摘要状态记为 ``skipped``。
    后台任务：``agent.enabled`` 为真时有任务管理器；运行器注入了就用注入的（测试），否则是正式的 agent 运行器。
    """

    browser_ice_servers = ice_servers_for_browser(cfg)
    access = meeting_access.MeetingAccess(cfg) if cfg.server.meeting_access else None

    @asynccontextmanager
    async def run_lifespan(app: FastAPI) -> AsyncIterator[None]:
        opened: Store | None = None
        if store is None:
            opened = await Store.open(
                cfg.resolve(cfg.session.data_dir) / "meetings.db",
                cfg.embedding.dimensions,
                assistant_name=cfg.session.assistant_name,
            )
        the_store = store or opened
        assert the_store is not None
        # 上次崩溃或被强杀时没来得及关闭的连接记录，补关。
        await the_store.close_dangling_connections()
        sessions = SessionManager(the_store)
        if access is not None:
            await access.start(the_store)
        frames = FrameIngestor(
            the_store,
            cfg.resolve(cfg.session.data_dir),
            max_side_px=cfg.screen.max_side_px,
            change_threshold=cfg.screen.change_threshold,
        )
        the_embedder = embedder
        if the_embedder is None and store is None and cfg.embedding.enabled:
            the_embedder = EmbeddingClient.from_config(cfg.embedding)
        backfill = EmbeddingWorker(the_store, the_embedder) if the_embedder is not None else None
        if backfill is not None:
            backfill.start()
        # 后台模型：实时模型的后台实例（llama.cpp 部署方式下即后台槽位）；画面摘要也可以改由后台 agent 的模型生成。
        production = store is None
        bg_llm = background_llm or (
            build_realtime_llm(cfg, "", background=True) if production else None
        )
        background = BackgroundModel(bg_llm) if bg_llm is not None else None
        models = [background] if background is not None else []
        caption_model: BackgroundModel | None = None
        remote_model: BackgroundModel | None = (
            None  # 后台 agent 的那个远端模型（按需创建，只建一个）
        )

        def remote_background() -> BackgroundModel | None:
            nonlocal remote_model
            if remote_model is None:
                remote = agent_llm or (build_agent_llm(cfg) if production else None)
                if remote is not None:
                    remote_model = BackgroundModel(remote)
                    models.append(remote_model)
            return remote_model

        provider = caption_provider(cfg)

        def agent_budget(by_agent_model: bool, *names: str) -> dict[str, int]:
            # 后台 agent 的模型会先思考，思考也占输出额度：由它直接生成时把输出上限放宽，否则正文是空的
            return dict.fromkeys(names, cfg.agent.generation_max_tokens) if by_agent_model else {}

        if provider == "realtime_llm":
            caption_model = background
        elif provider == "agent_llm":
            caption_model = remote_background()

        async def append_screen_line(session_id: str, _t: float, line: str) -> None:
            await sessions.append_context(session_id, line)

        async def notify_page(session_id: str, data: dict) -> None:
            if data["type"] == "frame_caption_status":
                sessions.publish(session_id, data)
            else:
                await sessions.push_to(session_id, data)

        captions = CaptionWorker(
            store=the_store,
            model=caption_model,
            prompt=load_prompt("screen_caption"),
            path_of=frames.path_of,
            notify=notify_page,
            append_context=append_screen_line,
            **agent_budget(provider == "agent_llm", "max_tokens"),
        )
        captions.start()

        def live_session_id() -> str | None:
            live = sessions.live
            return live.session.id if live is not None else None

        # 交给后台 agent 那个模型的请求要不要带截图原图（agent.attach_frames）
        attacher = (
            FrameAttacher(frames.path_of, cfg.agent.max_attached_frames)
            if cfg.agent.attach_frames and cfg.agent.supports_vision
            else None
        )
        digest_by_agent = cfg.realtime.digest_provider == "agent_llm"
        digests = DigestWorker(
            store=the_store,
            model=remote_background() if digest_by_agent else background,
            attacher=attacher if digest_by_agent else None,
            **agent_budget(digest_by_agent, "max_tokens"),
            render=lambda previous, new: load_prompt(
                "digest", previous_digest=previous, new_transcript=new
            ),
            interval_secs=cfg.realtime.digest_interval_minutes * 60.0,
            current_session=live_session_id,
        )
        digests.start()
        sessions.on_finished.append(digests.finalize)  # 断开或结束时把纪要补到最后
        # 会后报告。上次运行没生成完的先标为失败。
        reports = ReportWorker(
            store=the_store,
            model=remote_background() if cfg.report.provider == "agent_llm" else background,
            provider=cfg.report.provider,
            render_report=lambda **values: load_prompt("report", **values),
            render_section=lambda **values: load_prompt("report_section", **values),
            max_input_chars=cfg.report.max_input_chars,
            digests=digests,
            attacher=attacher if cfg.report.provider == "agent_llm" else None,
            **agent_budget(cfg.report.provider == "agent_llm", "max_tokens", "section_max_tokens"),
        )
        await reports.recover()
        # 后台任务。上次运行遗留的没做完的任务先标为失败。
        tasks: TaskManager | None = None
        if cfg.agent.enabled:
            tasks = TaskManager(
                store=the_store,
                runner=task_runner or build_runner(cfg, the_store, load_prompt("agent_system")),
                notify=sessions.push_to,
                max_concurrent=cfg.agent.max_concurrent_tasks,
                timeout_secs=cfg.agent.task_timeout_secs,
            )
            await tasks.recover()
        else:
            await the_store.fail_unfinished_tasks(RESTART_REASON)

        def cleaned(plan: CleanupPlan) -> None:
            if plan.manual or plan.screenshots:
                frames.forget(plan.session_id)
                captions.forget(plan.session_id)

        retention = RetentionWorker(
            the_store,
            sessions,
            cfg.retention,
            cfg.resolve(cfg.session.data_dir),
            on_cleaned=cleaned,
        )
        app.state.resources = AppResources(
            cfg,
            tasks=tasks,
            reports=reports,
            store=the_store,
            sessions=sessions,
            frames=frames,
            embedder=the_embedder,
            captions=captions,
            background=background,
            background_models=models,
            digests=digests,
            retention=retention,
            access=access,
        )
        if access is not None:
            from agentic_meeting.pipeline.questions import Questions

            app.state.resources.questions = Questions(app.state.resources)
        retention.start()
        app.state.bot = bot
        # 会议管线由应用自己持有，不挂在 /api/offer 的请求上：uvicorn 关闭时会先等请求任务全部结束，
        # 再进入下面的收尾；管线挂在请求上就要等会议结束，而断开会议的正是收尾，Ctrl+C 一次停不下来。
        bots: set[asyncio.Task[None]] = set()
        app.state.bots = bots
        # 同一局域网内不需要 ICE 服务器，留空即可。浏览器那一端用的是同一份（GET /api/ice）。
        ice_servers = [IceServer(**entry) for entry in browser_ice_servers]
        app.state.handler = handler or SmallWebRTCRequestHandler(ice_servers=ice_servers or None)
        app.state.health.start(the_store, screen_caption_enabled=captions.enabled)
        try:
            yield
        finally:
            if access is not None:
                await access.stop()
            await app.state.health.close()
            await retention.stop()
            # 先告诉页面「服务正在停止」（它据此不自动重连），再断开全部连接
            await sessions.shutdown()
            await app.state.handler.close()
            await sessions.wait_idle()  # 被断开的连接要把连接记录写完，再关库
            await sessions.drain()
            await _finish_bots(bots)
            if tasks is not None:
                await tasks.close()
            await reports.stop()
            await captions.stop()
            await digests.stop()
            if backfill is not None:
                await backfill.stop()
            if the_embedder is not None and embedder is None:
                await the_embedder.close()
            if opened is not None:
                await opened.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from agentic_meeting.store.instance import instance_lock

        with instance_lock(cfg.resolve(cfg.session.data_dir)) if store is None else nullcontext():
            async with run_lifespan(app):
                yield

    app = FastAPI(title="组会助理", lifespan=lifespan)
    health_api.register(app, cfg)

    # ---- 错误统一成 {"error", "code", "params"}（interfaces.md §5.9） ----

    @app.exception_handler(SessionBusy)
    async def session_busy(_request: Request, _exc: SessionBusy) -> JSONResponse:
        return error_response(409, "session.busy")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        headers = getattr(exc, "headers", None)
        if isinstance(exc, ApiError):
            body = exc.message.error()
        elif exc.status_code == 404 and exc.detail == "Not Found":  # 路由没匹配上
            body = message("route.not_found").error()
        else:
            # 本应用的处理器以外抛出的（405、Pipecat 信令的错误）：带上状态码，原文进日志
            logger.info(f"接口出错（{exc.status_code}）：{exc.detail}")
            body = message("request.failed", status=exc.status_code).error()
        return JSONResponse(body, status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(invalid(str(exc.errors()[:1]), 422).message.error(), status_code=422)

    # ---- 访问口令与单点登录（interfaces.md §5.7）：启用时拦住除登录以外的全部 /api ----

    if access is None:
        auth.register(app, auth.AuthGuard.from_config(cfg), sso.SingleSignOn.from_config(cfg))

        @app.api_route("/api/shares/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "DELETE"])
        @app.api_route(
            "/api/sessions/{session_id}/shares", methods=["GET", "HEAD", "POST", "PUT", "DELETE"]
        )
        @app.api_route(
            "/api/sessions/{session_id}/shares/{path:path}",
            methods=["GET", "HEAD", "POST", "PUT", "DELETE"],
        )
        async def sharing_unavailable(request: Request) -> None:
            # 静态站点的POST兜底会返回405；禁用的分享接口固定为404。
            raise ApiError(404, "session.not_found")
    else:
        meeting_access.register(app, access)

    # ---- 会话、发言、说话人（interfaces.md §5.1、§5.4） ----

    sessions_api.register(app, cfg)

    # ---- 截图（interfaces.md §5.3） ----

    frames_api.register(app, cfg)

    # ---- 后台任务（interfaces.md §5.5） ----

    tasks_api.register(app, cfg)

    # ---- 导出与会后报告（interfaces.md §5.6） ----

    export.register(app)
    reports_api.register(app)

    # ---- 对时（interfaces.md §5.1） ----

    @app.get("/api/time")
    async def server_time() -> dict[str, float]:
        return {"server_time": time.time()}

    # ---- WebRTC 信令（interfaces.md §5.2） ----

    @app.post("/api/offer")
    async def offer(http_request: Request):
        # 不用 FastAPI 的请求体类型：浏览器端 SDK 把连接参数放在驼峰的 requestData 里，
        # 只有 SmallWebRTCRequest.from_dict 认识它（客户端在里面带 session_id 等）。
        try:
            payload = await http_request.json()
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")
            request = SmallWebRTCRequest.from_dict(payload)
        except (ValueError, TypeError) as e:
            raise invalid(f"WebRTC 请求格式有误：{e}") from e
        if isinstance(request.request_data, dict) and "session_id" in request.request_data:
            # 继续一场会议：协商之前就校验，找不到直接 404，而不是悄悄新建一场（interfaces.md §5.2）。
            wanted = request.request_data["session_id"]
            if wanted is not None:
                store_ = http_request.app.state.resources.store
                if not isinstance(wanted, str) or not wanted.strip():
                    raise invalid("session_id 必须是非空的字符串")
                if access is not None:
                    await access.require(http_request.state.identity, wanted.strip(), "host")
                if store_ is None or await store_.get_session(wanted.strip()) is None:
                    raise ApiError(404, "session.not_found")
                await store_.require_available(wanted.strip())

        async def on_connection(connection: Any) -> None:
            state = http_request.app.state
            resources = state.resources
            lease = None
            if access is not None:
                identity = http_request.state.identity
                wanted = (
                    request.request_data.get("session_id")
                    if isinstance(request.request_data, dict)
                    else None
                )
                async with access.lock:
                    if wanted:
                        try:
                            await access.require(identity, wanted.strip(), "host")
                        except ApiError:
                            await connection.disconnect()
                            raise
                    elif await access.authenticate(identity.cookie) is None:
                        await connection.disconnect()
                        raise ApiError(401, "auth.required")
                    live = await (
                        resources.sessions.attach(wanted.strip())
                        if wanted
                        else resources.sessions.begin(owner=identity.principal)
                    )
                live.identity = identity
                await access.audit(
                    identity, live.session.id, "recording.resume" if wanted else "recording.start"
                )
                resources = replace(resources, live=live, identity=identity)
                access.peers[connection.pc_id] = (identity, live)

                async def close() -> None:
                    live.stop_requested = True
                    for pending in list(live.tool_tasks):
                        pending.cancel()
                    await connection.disconnect()
                    if live.worker is not None:
                        await live.worker.cancel()

                lease = access.track(identity, live.session.id, "host", close)

            async def run() -> None:
                try:
                    await state.bot(connection, request.request_data, resources)
                finally:
                    if access is not None:
                        access.leases.discard(lease)
                        access.peers.pop(connection.pc_id, None)

            task = asyncio.create_task(
                run(),
                name=f"bot-{getattr(connection, 'pc_id', '')}",
            )
            state.bots.add(task)
            task.add_done_callback(_bot_done(state.bots))

        if access is None:
            return await http_request.app.state.handler.handle_web_request(
                request=request, webrtc_connection_callback=on_connection
            )
        async with access.offer_lock:
            identity = http_request.state.identity
            wanted = (
                request.request_data.get("session_id")
                if isinstance(request.request_data, dict)
                else None
            )
            if request.pc_id:
                peer = await access.peer(identity, request.pc_id)
                if wanted is not None and wanted.strip() != peer.session.id:
                    raise ApiError(404, "session.not_found")
            elif wanted:
                await access.require(identity, wanted.strip(), "host")
            elif not access.can_host(identity):
                raise ApiError(403, "access.denied")
            live = http_request.app.state.resources.sessions.live
            if live is not None:
                membership = await access.store.membership(live.session.id, identity.principal)
                if membership is None or membership["role"] != "host":
                    raise ApiError(409, "access.busy")
            return await http_request.app.state.handler.handle_web_request(
                request=request, webrtc_connection_callback=on_connection
            )

    @app.patch("/api/offer")
    async def ice_candidate(request: SmallWebRTCPatchRequest, http_request: Request):
        if access is not None:
            await access.peer(http_request.state.identity, request.pc_id)
        await http_request.app.state.handler.handle_patch_request(request)
        return {"status": "success"}

    @app.get("/api/ice")
    async def ice_config() -> dict[str, list[dict[str, Any]]]:
        return {"ice_servers": browser_ice_servers}

    # ---- 静态站点：放在最后，不盖住上面的接口 ----

    root = static_dir if static_dir is not None else DEFAULT_STATIC_DIR
    if (root / "index.html").is_file():
        app.mount("/", StaticFiles(directory=root, html=True), name="client")
    else:

        @app.get("/", include_in_schema=False)
        async def not_built() -> HTMLResponse:
            return HTMLResponse(NOT_BUILT_HTML, status_code=503)

    return app


class _BoundedServer:
    """包住 asyncio 的服务器对象，等它确认连接全部关闭时有时限。

    uvicorn 关闭时先等它自己记着的连接都关掉，最后还要 ``wait_closed()``，等 asyncio 服务器把接受过的每条连接
    都登记为关闭。这一步不打日志、没有时限，再按 Ctrl+C 也不管用。有连接没登记上（比如 Windows 的事件循环关闭
    一条已被对方重置的连接时 ``socket.shutdown()`` 抛错，后面的登记被跳过），就会一直卡在「Shutting down」。
    走到这一步时 uvicorn 那边的连接已经关完，剩下的等待只是记账，等一小会儿就够了。
    """

    def __init__(self, server: Any) -> None:
        self._server = server

    def close(self) -> None:
        self._server.close()

    async def wait_closed(self) -> None:
        try:
            await asyncio.wait_for(self._server.wait_closed(), SERVER_CLOSE_GRACE_SECS)
        except TimeoutError:
            logger.warning(
                "HTTP 服务器在 {:g} 秒内没有确认全部连接已关闭，不再等", SERVER_CLOSE_GRACE_SECS
            )


class MeetingServer(uvicorn.Server):
    """在 uvicorn 的关闭流程上补三处：

    * 先结束旁观会议的事件流。uvicorn 先等正在处理的请求全部结束，才进入应用的收尾（lifespan）；
      事件流是不会自己结束的请求，在收尾里才结束它们就晚了，Ctrl+C 一次停不下来。
    * 等 asyncio 服务器确认连接关闭有时限（``_BoundedServer``）。
    * 关闭过程中再收到停止信号（再按一次 Ctrl+C 或 Ctrl+Break）就立即退出：先调 ``on_force_quit``
      （结束一并拉起的推理服务），再结束进程，不再等任何收尾。uvicorn 自己只在第二次 Ctrl+C 时跳过一部分等待，
      第二次 Ctrl+Break 什么都不做。
    """

    def __init__(
        self,
        config: uvicorn.Config,
        app: FastAPI,
        on_force_quit: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(config)
        self._app = app
        self._on_force_quit = on_force_quit

    def handle_exit(self, sig: int, frame: Any) -> None:
        if self.should_exit:
            logger.warning("关闭过程中再次收到停止信号，立即退出")
            if self._on_force_quit is not None:
                try:
                    self._on_force_quit()
                except Exception:
                    logger.exception("结束推理服务时出错")
            os._exit(1)
        super().handle_exit(sig, frame)

    async def shutdown(self, sockets: Any = None) -> None:
        resources = getattr(self._app.state, "resources", None)
        sessions = getattr(resources, "sessions", None)
        if sessions is not None:
            sessions.watchers.close_all()
        self.servers = [_BoundedServer(server) for server in self.servers]
        await super().shutdown(sockets)


def make_server(
    cfg: AppConfig,
    app: FastAPI | None = None,
    *,
    on_force_quit: Callable[[], None] | None = None,
) -> uvicorn.Server:
    """按配置创建 uvicorn 服务器；配置了证书时启用 HTTPS（局域网内的浏览器访问必须 HTTPS）。

    ``on_force_quit``：关闭过程中再按一次 Ctrl+C、进程立即退出之前调用（见 ``MeetingServer``）。
    """
    app = app or create_app(cfg)
    config = uvicorn.Config(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        ssl_certfile=str(cfg.resolve(cfg.server.tls_cert)) if cfg.server.tls_cert else None,
        ssl_keyfile=str(cfg.resolve(cfg.server.tls_key)) if cfg.server.tls_key else None,
        log_level="info",
        timeout_graceful_shutdown=5,
        # 转发头由下面自己套的 ProxyHeadersMiddleware 处理（信任的地址照旧取 FORWARDED_ALLOW_IPS）：
        # 在它外面先记下 TCP 直连对端，反向代理身份头只认受信任代理直接发来的（interfaces.md §5.7）
        proxy_headers=False,
    )
    config.app = sso.record_peer(
        ProxyHeadersMiddleware(app, trusted_hosts=config.forwarded_allow_ips)
    )
    return MeetingServer(config, app, on_force_quit)
