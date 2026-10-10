"""HTTP 应用：WebRTC 信令、对时、会话与发言接口（``sessions_api.py``）、静态站点。

信令的写法照抄 Pipecat 自带运行器的 ``_setup_webrtc_routes``（docs/pipecat-notes.md §2）；
我们不用它的 ``main()``，因为那会接管整个 FastAPI 应用和命令行。
接口约定见 docs/interfaces.md §5：除信令外全部是 JSON，业务错误返回 ``{"error": "<中文说明>"}``。
健康接口（``health_api.py``）使用 §5.8 的独立状态快照。
截图、任务、报告、导出的接口各在自己的模块里（``frames_api.py`` 等）。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pipecat.transports.smallwebrtc.connection import IceServer
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from starlette.exceptions import HTTPException as StarletteHTTPException

from agentic_meeting.agent.runner import build_runner
from agentic_meeting.agent.tasks import Runner, TaskManager
from agentic_meeting.config import REPO_ROOT, AppConfig
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
from agentic_meeting.store.db import Store
from agentic_meeting.store.embeddings import EmbeddingClient, EmbeddingWorker
from agentic_meeting.web import export, frames_api, health_api, reports_api, sessions_api, tasks_api

DEFAULT_STATIC_DIR = REPO_ROOT / "client" / "dist"

BotRunner = Callable[[Any, Any, AppResources], Awaitable[None]]

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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
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
        )
        app.state.bot = bot
        # 同一局域网内不需要 ICE 服务器，留空即可。
        ice_servers = [IceServer(urls=url) for url in cfg.server.ice_servers]
        app.state.handler = handler or SmallWebRTCRequestHandler(ice_servers=ice_servers or None)
        app.state.health.start(the_store, screen_caption_enabled=captions.enabled)
        try:
            yield
        finally:
            await app.state.health.close()
            # 先告诉页面「服务正在停止」（它据此不自动重连），再断开全部连接
            await sessions.shutdown()
            await app.state.handler.close()
            await sessions.wait_idle()  # 被断开的连接要把连接记录写完，再关库
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

    app = FastAPI(title="组会助理", lifespan=lifespan)
    health_api.register(app, cfg)

    # ---- 错误统一成 {"error": "..."} ----

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) and exc.detail != "Not Found" else None
        if exc.status_code == 404 and message is None:
            message = "没有这个接口或页面"
        return JSONResponse({"error": message or "请求失败"}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": f"请求格式有误：{exc.errors()[:1]}"}, status_code=422)

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
    async def offer(http_request: Request, background_tasks: BackgroundTasks):
        # 不用 FastAPI 的请求体类型：浏览器端 SDK 把连接参数放在驼峰的 requestData 里，
        # 只有 SmallWebRTCRequest.from_dict 认识它（客户端在里面带 session_id 等）。
        try:
            payload = await http_request.json()
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")
            request = SmallWebRTCRequest.from_dict(payload)
        except (ValueError, TypeError) as e:
            raise StarletteHTTPException(400, f"WebRTC 请求格式有误：{e}") from e
        if isinstance(request.request_data, dict) and "session_id" in request.request_data:
            # 继续一场会议：协商之前就校验，找不到直接 404，而不是悄悄新建一场（interfaces.md §5.2）。
            wanted = request.request_data["session_id"]
            if wanted is not None:
                store_ = http_request.app.state.resources.store
                if not isinstance(wanted, str) or not wanted.strip():
                    raise StarletteHTTPException(400, "session_id 必须是非空的字符串")
                if store_ is None or await store_.get_session(wanted.strip()) is None:
                    raise StarletteHTTPException(404, sessions_api.NOT_FOUND_SESSION)

        async def on_connection(connection: Any) -> None:
            state = http_request.app.state
            background_tasks.add_task(state.bot, connection, request.request_data, state.resources)

        return await http_request.app.state.handler.handle_web_request(
            request=request, webrtc_connection_callback=on_connection
        )

    @app.patch("/api/offer")
    async def ice_candidate(request: SmallWebRTCPatchRequest, http_request: Request):
        await http_request.app.state.handler.handle_patch_request(request)
        return {"status": "success"}

    # ---- 静态站点：放在最后，不盖住上面的接口 ----

    root = static_dir if static_dir is not None else DEFAULT_STATIC_DIR
    if (root / "index.html").is_file():
        app.mount("/", StaticFiles(directory=root, html=True), name="client")
    else:

        @app.get("/", include_in_schema=False)
        async def not_built() -> HTMLResponse:
            return HTMLResponse(NOT_BUILT_HTML, status_code=503)

    return app


def make_server(cfg: AppConfig, app: FastAPI | None = None) -> uvicorn.Server:
    """按配置创建 uvicorn 服务器；配置了证书时启用 HTTPS（局域网内的浏览器访问必须 HTTPS）。"""
    config = uvicorn.Config(
        app or create_app(cfg),
        host=cfg.server.host,
        port=cfg.server.port,
        ssl_certfile=str(cfg.resolve(cfg.server.tls_cert)) if cfg.server.tls_cert else None,
        ssl_keyfile=str(cfg.resolve(cfg.server.tls_key)) if cfg.server.tls_key else None,
        log_level="info",
    )
    return uvicorn.Server(config)
