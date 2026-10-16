"""截图的 HTTP 接口（docs/interfaces.md §5.3）。由 ``web/app.py`` 的 ``create_app`` 调用 ``register``。

上传只在有进行中的会议时接受，``session_id`` 指明是哪一场（同时进行好几场时必须给）；入库后向页面推一条 ``frame`` 消息，并把截图交给画面摘要（``resources.captions``；
没有就只进时间线）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from loguru import logger

from agentic_meeting.config import AppConfig
from agentic_meeting.pipeline.bot import AppResources
from agentic_meeting.screen.ingest import IngestError, media_type_of
from agentic_meeting.types import ScreenFrame
from agentic_meeting.web.errors import ApiError, invalid


def frame_json(frame: ScreenFrame) -> dict[str, Any]:
    return {
        "id": frame.id,
        "t": frame.t,
        "width": frame.width,
        "height": frame.height,
        "caption": frame.caption,
        "caption_status": frame.caption_status,
    }


def register(app: FastAPI, cfg: AppConfig) -> None:
    def resources(request: Request) -> AppResources:
        res: AppResources = request.app.state.resources
        if res.store is None or res.sessions is None or res.frames is None:
            raise ApiError(503, "storage.not_ready")
        return res

    @app.post("/api/frames")
    async def upload_frame(
        request: Request,
        captured_at: Annotated[str, Form()],
        image: Annotated[UploadFile, File()],
        session_id: Annotated[str | None, Form()] = None,
    ) -> dict[str, Any]:
        res = resources(request)
        if not cfg.screen.enabled:
            raise ApiError(403, "frames.disabled")
        if session_id:
            live = res.sessions.live_for(session_id)
        else:
            # 没说是哪一场：只有一场在进行时就是它（旧的页面不带 session_id）
            connections = res.sessions.connections
            if len(connections) > 1:
                raise invalid("同时有好几场会议在进行，session_id 必须给")
            live = connections[0] if connections else None
        if live is None:
            raise ApiError(404, "frames.no_live_session")
        async with res.store.session_work(live.session.id):
            try:
                at = float(captured_at)
            except ValueError as e:
                raise invalid("captured_at 必须是数字（Unix 秒）") from e
            # 多读一个字节就能知道超没超，不把超大的文件整个读进内存
            data = await image.read(res.frames.max_bytes + 1)
            try:
                ingested = await res.frames.ingest(live.session, data, at)
            except IngestError as e:
                raise ApiError(e.status, e.message.code, **e.message.params) from e
            frame = ingested.frame
            await res.sessions.push_to(
                live.session.id,
                {
                    "type": "frame",
                    "id": frame.id,
                    "t": frame.t,
                    "width": frame.width,
                    "height": frame.height,
                },
            )
            if res.captions is not None:
                try:
                    await res.captions.submit(ingested)
                except Exception:  # 摘要出问题不影响截图进时间线
                    logger.exception("把截图交给画面摘要失败")
            return {"id": frame.id, "t": frame.t}

    @app.get("/api/frames")
    async def list_frames(request: Request, session_id: str | None = None) -> dict[str, Any]:
        res = resources(request)
        if session_id:
            session = await res.store.get_session(session_id)
        else:
            session = await res.sessions.current_session()
        if session is None:
            raise ApiError(404, "session.not_found" if session_id else "session.none")
        await res.store.require_available(session.id)
        return {"items": [frame_json(f) for f in await res.store.list_frames(session.id)]}

    @app.get("/api/frames/{frame_id}/image")
    async def frame_image(request: Request, frame_id: int) -> FileResponse:
        res = resources(request)
        frame = await res.store.get_frame(frame_id)
        if frame is not None:
            await res.store.require_available(frame.session_id)
        path = res.frames.path_of(frame) if frame is not None else None
        if frame is None or path is None or not path.is_file():
            raise ApiError(404, "frame.not_found")
        # 删除或过期后，浏览器重新验证图片，避免继续展示旧图。
        return FileResponse(
            path,
            media_type=media_type_of(frame.path),
            headers={"Cache-Control": "private, no-cache"},
        )
