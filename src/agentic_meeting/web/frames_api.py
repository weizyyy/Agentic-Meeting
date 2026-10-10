"""截图的 HTTP 接口（docs/interfaces.md §5.3）。由 ``web/app.py`` 的 ``create_app`` 调用 ``register``。

上传只在有进行中的会议时接受；入库后向页面推一条 ``frame`` 消息，并把截图交给画面摘要（``resources.captions``；
没有就只进时间线）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException

from agentic_meeting.config import AppConfig
from agentic_meeting.pipeline.bot import AppResources
from agentic_meeting.screen.ingest import IngestError, media_type_of
from agentic_meeting.types import ScreenFrame

NO_LIVE_SESSION = "现在没有进行中的会议，截图没有保存"


def _fail(status: int, message: str) -> StarletteHTTPException:
    return StarletteHTTPException(status, message)


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
            raise _fail(503, "存储尚未就绪")
        return res

    @app.post("/api/frames")
    async def upload_frame(
        request: Request,
        captured_at: Annotated[str, Form()],
        image: Annotated[UploadFile, File()],
    ) -> dict[str, Any]:
        res = resources(request)
        if not cfg.screen.enabled:
            raise _fail(403, "截图功能已在配置里关闭")
        live = res.sessions.live
        if live is None:
            raise _fail(404, NO_LIVE_SESSION)
        async with res.store.session_work(live.session.id):
            try:
                at = float(captured_at)
            except ValueError as e:
                raise _fail(400, "captured_at 必须是数字（Unix 秒）") from e
            # 多读一个字节就能知道超没超，不把超大的文件整个读进内存
            data = await image.read(res.frames.max_bytes + 1)
            try:
                ingested = await res.frames.ingest(live.session, data, at)
            except IngestError as e:
                raise _fail(e.status, e.message) from e
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
            raise _fail(404, "找不到这场会议" if session_id else "现在没有会议")
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
            raise _fail(404, "找不到这张截图")
        # 删除或过期后，浏览器重新验证图片，避免继续展示旧图。
        return FileResponse(
            path,
            media_type=media_type_of(frame.path),
            headers={"Cache-Control": "private, no-cache"},
        )
