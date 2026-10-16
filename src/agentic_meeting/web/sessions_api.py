"""会话、发言、说话人的 HTTP 接口（docs/interfaces.md §5.1、§5.4）。

由 ``web/app.py`` 的 ``create_app`` 调用 ``register``。接口都只走 HTTP，不需要音频连接：
页面一打开（包括刷新、换设备、没连接时）就能看到会议列表和最近的对话。
旁观一场会议的设备另开一条服务端推送的事件流（``/api/sessions/{id}/events``，``pipeline/watchers.py``）。
错误统一是 ``{"error", "code", "params"}``（interfaces.md §5.9，app.py 里的异常处理器负责转换）。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import StreamingResponse

from agentic_meeting.config import AppConfig
from agentic_meeting.pipeline.bot import AppResources
from agentic_meeting.pipeline.session import SessionManager
from agentic_meeting.pipeline.watchers import device_label
from agentic_meeting.store.db import SessionBusy, Store
from agentic_meeting.types import NamedUtterance, Session, SessionSummary
from agentic_meeting.web.errors import ApiError, invalid
from agentic_meeting.web.frames_api import frame_json
from agentic_meeting.web.tasks_api import event_json, task_json

MAX_TITLE_CHARS = 200
MAX_SPEAKER_NAME_CHARS = 50
MAX_PAGE = 500
MAX_REASSIGN = 500  # 一次最多改这么多条发言的说话人
HEARTBEAT_SECS = 5  # 旁观事件流空闲时多久发一行心跳
SEND_TIMEOUT_SECS = 15  # 旁观事件流一次发送最多等这么久，对端不读就断开


def summary_json(summary: SessionSummary, *, live: bool) -> dict[str, Any]:
    s = summary.session
    state = "live" if live else ("ended" if s.ended_at is not None else "interrupted")
    return {
        "id": s.id,
        "title": s.title,
        "keep": s.keep,
        "deletion_pending": s.deletion_pending,
        "started_at": s.started_at,
        "ended_at": s.ended_at,
        "last_active_at": s.last_active_at,
        "state": state,
        "duration_secs": summary.duration_secs,
        "utterance_count": summary.utterance_count,
        "speakers": summary.speakers,
        "preview": [{"speaker": who, "text": text} for who, text in summary.preview],
    }


def utterance_json(item: NamedUtterance) -> dict[str, Any]:
    u = item.utterance
    return {
        "id": u.id,
        "speaker_idx": u.speaker_idx,
        "speaker_name": item.speaker_name,
        "t_start": u.t_start,
        "t_end": u.t_end,
        "text": u.text,
        "source": u.source,
    }


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as e:
        raise invalid("请求体必须是 JSON") from e
    if not isinstance(body, dict):
        raise invalid("请求体必须是 JSON 对象")
    return body


def register(app: FastAPI, cfg: AppConfig) -> None:
    def resources(request: Request) -> AppResources:
        return request.app.state.resources

    def parts(request: Request) -> tuple[Store, SessionManager]:
        res = resources(request)
        if res.store is None or res.sessions is None:
            raise ApiError(503, "storage.not_ready")
        return res.store, res.sessions

    async def resolve(
        request: Request, session_id: str | None, *, metadata: bool = False
    ) -> Session:
        """元数据可读待删除行；内容和变更只允许当前可用会议。"""
        store, manager = parts(request)
        session = (
            await store.get_session(session_id) if session_id else await manager.current_session()
        )
        if session is None:
            raise ApiError(404, "session.not_found" if session_id else "session.none")
        if not metadata:
            await store.require_available(session.id)
        return session

    async def summary_of(request: Request, session: Session) -> dict[str, Any]:
        store, manager = parts(request)
        summary = await store.get_summary(session.id, now=time.time())
        assert summary is not None
        return summary_json(summary, live=manager.is_live(session.id))

    # ---- 会议列表与详情 ----

    @app.get("/api/sessions")
    async def list_sessions(
        request: Request,
        limit: int = Query(20, ge=1, le=MAX_PAGE),
        before: float | None = None,
    ) -> dict[str, Any]:
        store, manager = parts(request)
        rows = await store.list_sessions(limit, before, now=time.time())
        return {"items": [summary_json(r, live=manager.is_live(r.session.id)) for r in rows]}

    @app.get("/api/session")
    async def current_session(request: Request) -> dict[str, Any]:
        return await session_detail(request, await resolve(request, None, metadata=True))

    @app.get("/api/sessions/{session_id}")
    async def get_session(request: Request, session_id: str) -> dict[str, Any]:
        return await session_detail(request, await resolve(request, session_id, metadata=True))

    async def session_detail(request: Request, session: Session) -> dict[str, Any]:
        store, _ = parts(request)
        body = await summary_of(request, session)
        body["screen"] = cfg.screen.model_dump()
        body["members"] = list(cfg.session.members)  # 说话人改名的候选
        body["connections"] = [
            {
                "connected_at": c.connected_at,
                "disconnected_at": c.disconnected_at,
                "t_from": c.t_from,
                "t_to": c.t_to,
            }
            for c in await store.list_connections(session.id)
        ]
        return body

    # ---- 重命名、结束、删除 ----

    @app.patch("/api/sessions/{session_id}")
    async def rename_session(request: Request, session_id: str) -> dict[str, Any]:
        _, manager = parts(request)
        body = await _json_body(request)
        if not body or set(body) - {"title", "keep"}:
            raise invalid("只接受 title、keep，至少给一个")
        title = body.get("title")
        if "title" in body:
            if not isinstance(title, str):
                raise invalid("title 必须是字符串")
            title = title.strip()
            if len(title) > MAX_TITLE_CHARS:
                raise ApiError(400, "session.title_too_long", max=MAX_TITLE_CHARS)
        keep = body.get("keep")
        if "keep" in body and not isinstance(keep, bool):
            raise invalid("keep 必须是布尔值")
        session = await resolve(request, session_id)
        updated = await manager.update_session(session.id, title=title, keep=keep)
        assert updated is not None
        return await summary_of(request, updated)

    @app.post("/api/sessions/{session_id}/text")
    async def submit_text(request: Request, session_id: str) -> dict[str, Any]:
        """旁观这场会议的设备打字提问；处理结果的提示放在响应里（interfaces.md §5.1）。"""
        _, manager = parts(request)
        body = await _json_body(request)
        text = body.get("text")
        if not isinstance(text, str):
            raise invalid("text 必须是字符串")
        session = await resolve(request, session_id)
        notices = await manager.submit_text(session.id, text)
        if notices is None:
            raise ApiError(409, "session.not_live")
        level, msg = notices[-1] if notices else ("info", None)
        return {
            "accepted": level != "warn",
            "notice": msg.notice(level) if msg is not None else None,
        }

    @app.get("/api/sessions/{session_id}/events")
    async def session_events(
        request: Request, session_id: str, after: str | None = None
    ) -> StreamingResponse:
        # 与普通会议读取使用同一解析/授权入口；不触碰媒体连接。
        store, manager = parts(request)
        session = await resolve(request, session_id)
        cursor = request.headers.get("last-event-id", after)

        def encode(kind: str, payload: dict, *, event_id: bool = True) -> str:
            identifier = f"id: {payload['epoch']}:{payload['seq']}\n" if event_id else ""
            return f"{identifier}event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        async def stream() -> AsyncIterator[str]:
            subscriber, valid, replay = manager.watchers.subscribe(
                session.id, device_label(request.headers.get("user-agent", "")), cursor
            )
            source = subscriber.source
            seq = source.seq
            transient = source.snapshot_transient()
            try:
                yield "retry: 1000\n\n"
                if not valid:
                    if cursor:
                        yield encode("reset", {"reason": "cursor_unavailable"}, event_id=False)
                    # 读取期间保留生产工作租约，避免删除在快照中途移除半套记录。
                    async with store.session_work(session.id):
                        snapshot = {
                            "session": await session_detail(request, session),
                            "utterances": [
                                utterance_json(u) for u in await store.all_utterances(session.id)
                            ],
                            "speakers": [
                                {"idx": s.idx, "display_name": s.display_name}
                                for s in await store.list_speakers(session.id)
                            ],
                            "tasks": [
                                {
                                    **task_json(t),
                                    "events": [
                                        event_json(e) for e in await store.list_task_events(t.id)
                                    ],
                                }
                                for t in await store.list_tasks(session.id)
                            ],
                            "frames": [frame_json(f) for f in await store.list_frames(session.id)],
                            "transient": transient,
                        }
                    yield encode("snapshot", source.envelope(snapshot, seq=seq))
                for event in replay:
                    if subscriber.overflow:
                        break
                    if event is None:
                        return
                    yield encode("meeting", event)
                    if event["data"]["type"] == "session_deleted" or (
                        event["data"]["type"] == "session_state"
                        and event["data"]["state"] == "ended"
                    ):
                        return
                while True:
                    if subscriber.overflow:
                        yield encode("reset", {"reason": "slow_consumer"}, event_id=False)
                        return
                    try:
                        event = await asyncio.wait_for(subscriber.queue.get(), HEARTBEAT_SECS)
                    except TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    if event is None:
                        return
                    yield encode("meeting", event)
                    if event["data"]["type"] == "session_deleted" or (
                        event["data"]["type"] == "session_state"
                        and event["data"]["state"] == "ended"
                    ):
                        return
            finally:
                manager.watchers.unsubscribe(subscriber)

        body = stream()

        class EventResponse(StreamingResponse):
            async def stream_response(self, send):
                async def bounded_send(message):
                    # 只限制单次发送：对端不读时在期限内释放请求，健康的流一直开着，不定期重建。
                    async with asyncio.timeout(SEND_TIMEOUT_SECS):
                        await send(message)

                try:
                    await super().stream_response(bounded_send)
                finally:
                    # send 可能阻塞在生成器外；取消请求时仍须立即关闭生成器的订阅。
                    await body.aclose()

        return EventResponse(
            body,
            media_type="text/event-stream",
            headers={"Cache-Control": "private, no-store", "X-Accel-Buffering": "no"},
        )

    async def end(request: Request, session: Session) -> dict[str, Any]:
        _, manager = parts(request)
        ended = await manager.end(session.id)
        if ended is None:
            raise ApiError(404, "session.not_found")
        return {"id": ended.id, "ended_at": ended.ended_at}

    @app.post("/api/sessions/{session_id}/end")
    async def end_session(request: Request, session_id: str) -> dict[str, Any]:
        return await end(request, await resolve(request, session_id))

    @app.post("/api/session/end")
    async def end_current_session(request: Request) -> dict[str, Any]:
        return await end(request, await resolve(request, None))

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(request: Request, session_id: str) -> dict[str, Any]:
        session = await resolve(request, session_id, metadata=True)
        cleaner = resources(request).retention
        if cleaner is None:
            raise ApiError(503, "cleanup.not_ready")
        try:
            await cleaner.delete_session(session.id)
        except LookupError as exc:
            raise ApiError(404, "session.not_found") from exc
        except SessionBusy:
            raise
        except Exception as exc:
            raise ApiError(500, "session.delete_incomplete") from exc
        return {"id": session.id}

    # ---- 发言 ----

    @app.get("/api/utterances")
    async def list_utterances(
        request: Request,
        session_id: str | None = None,
        after_id: int | None = None,
        before_id: int | None = None,
        tail: int | None = Query(None, ge=1, le=MAX_PAGE),
        limit: int = Query(200, ge=1, le=MAX_PAGE),
    ) -> dict[str, Any]:
        store, _ = parts(request)
        session = await resolve(request, session_id)
        try:
            rows = await store.list_utterances(
                session.id, after_id=after_id, before_id=before_id, tail=tail, limit=limit
            )
        except ValueError as e:
            raise invalid("after_id、before_id、tail 只能给一个") from e
        return {"items": [utterance_json(r) for r in rows]}

    @app.post("/api/utterances/speaker")
    async def reassign_utterances(request: Request) -> dict[str, Any]:
        """把选中的发言改成另一个说话人（已有的，或者新建一个）。"""
        store, manager = parts(request)
        body = await _json_body(request)
        ids = body.get("ids")
        if (
            not isinstance(ids, list)
            or not ids
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)
        ):
            raise invalid("ids 必须是非空的发言编号列表")
        if len(ids) > MAX_REASSIGN:
            raise ApiError(400, "utterances.too_many", max=MAX_REASSIGN)
        idx, new_name = body.get("speaker_idx"), body.get("new_speaker")
        if (idx is None) == (new_name is None):
            raise invalid("speaker_idx 和 new_speaker 要给一个，且只给一个")
        given = body.get("session_id")
        if given is not None and not isinstance(given, str):
            raise invalid("session_id 必须是字符串")
        session = await resolve(request, given)
        if new_name is not None:
            if not isinstance(new_name, str) or not new_name.strip():
                raise invalid("new_speaker 必须是非空的名字")
            if len(new_name.strip()) > MAX_SPEAKER_NAME_CHARS:
                raise ApiError(400, "speaker.name_too_long", max=MAX_SPEAKER_NAME_CHARS)
            speaker = await store.add_speaker(session.id, new_name)
            idx = speaker.idx
        else:
            if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
                raise invalid("speaker_idx 必须是说话人编号（助理和文字输入不能当发言人）")
            if idx != 0 and not await store.has_speaker(session.id, idx):
                raise ApiError(404, "speaker.not_found")
        changed = await store.set_utterances_speaker(session.id, ids, idx)
        name = await store.speaker_name(session.id, idx)
        if manager.is_live(session.id):
            live = manager.live
            if live is not None and live.recorder is not None:
                live.recorder.set_speaker_name(idx, name)
        for utterance_id in changed:
            await manager.push_to(
                session.id,
                {
                    "type": "utterance_update",
                    "id": utterance_id,
                    "speaker_idx": idx,
                    "speaker_name": name,
                },
            )
        return {"speaker": {"idx": idx, "display_name": name}, "ids": changed}

    # ---- 说话人 ----

    @app.get("/api/speakers")
    async def list_speakers(request: Request, session_id: str | None = None) -> dict[str, Any]:
        store, _ = parts(request)
        session = await resolve(request, session_id)
        return {
            "items": [
                {"idx": s.idx, "display_name": s.display_name}
                for s in await store.list_speakers(session.id)
            ]
        }

    @app.put("/api/speakers/{idx}")
    async def rename_speaker(request: Request, idx: int) -> dict[str, Any]:
        store, manager = parts(request)
        body = await _json_body(request)
        name = body.get("display_name")
        if not isinstance(name, str) or not name.strip():
            raise invalid("display_name 必须是非空的字符串")
        name = name.strip()
        if len(name) > MAX_SPEAKER_NAME_CHARS:
            raise ApiError(400, "speaker.name_too_long", max=MAX_SPEAKER_NAME_CHARS)
        given = body.get("session_id")
        if given is not None and not isinstance(given, str):
            raise invalid("session_id 必须是字符串")
        session = await resolve(request, given)
        if not await store.rename_speaker(session.id, idx, name):
            raise ApiError(404, "speaker.not_found")
        live = manager.live
        if live is not None and live.session.id == session.id:
            if live.recorder is not None:
                live.recorder.set_speaker_name(idx, name)
        await manager.push_to(session.id, {"type": "speaker", "idx": idx, "display_name": name})
        return {"idx": idx, "display_name": name}

    @app.post("/api/speakers/{idx}/merge")
    async def merge_speaker(request: Request, idx: int) -> dict[str, Any]:
        _, manager = parts(request)
        body = await _json_body(request)
        into = body.get("into")
        if not isinstance(into, int) or isinstance(into, bool):
            raise invalid("into 必须是说话人编号（整数）")
        if idx <= 0 or into <= 0:
            raise ApiError(400, "speaker.not_mergeable")
        if idx == into:
            raise ApiError(400, "speaker.merge_self")
        given = body.get("session_id")
        if given is not None and not isinstance(given, str):
            raise invalid("session_id 必须是字符串")
        session = await resolve(request, given)
        merged = await manager.merge_speakers(session.id, idx, into)
        if merged is None:
            raise ApiError(404, "speaker.not_found")
        return merged
