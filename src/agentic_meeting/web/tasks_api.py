"""后台任务的 HTTP 接口（docs/interfaces.md §5.5）。由 ``web/app.py`` 的 ``create_app`` 调用 ``register``。

任务面板用它们：列表、单个任务的全部内容（结果、来源、产物、进度、这次任务外发了什么）、取消、下载产物。
路径里的任务编号是完整编号（``<会话 id>.t3``）；页面上显示的是短编号。
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agentic_meeting.agent.sandbox import safe_artifact_name
from agentic_meeting.agent.tasks import task_dir
from agentic_meeting.config import AppConfig
from agentic_meeting.pipeline.bot import AppResources
from agentic_meeting.types import TaskEvent, TaskRecord

NOT_FOUND_TASK = "找不到这个任务"


def _fail(status: int, message: str) -> StarletteHTTPException:
    return StarletteHTTPException(status, message)


def task_json(task: TaskRecord) -> dict[str, Any]:
    """列表里的一项：不含详细结果。"""
    return {
        "id": task.id,
        "label": task.label,
        "goal": task.goal,
        "status": task.status,
        "brief": task.brief,
        "error": task.error,
        "modality": task.modality,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
    }


def event_json(event: TaskEvent) -> dict[str, Any]:
    return {"at": event.at, "kind": event.kind, "summary": event.summary}


def register(app: FastAPI, cfg: AppConfig) -> None:
    def resources(request: Request) -> AppResources:
        res: AppResources = request.app.state.resources
        if res.store is None or res.sessions is None:
            raise _fail(503, "存储尚未就绪")
        return res

    async def find(request: Request, task_id: str) -> TaskRecord:
        task = await resources(request).store.get_task(task_id)
        if task is None:
            raise _fail(404, NOT_FOUND_TASK)
        await resources(request).store.require_available(task.session_id)
        return task

    @app.get("/api/tasks")
    async def list_tasks(request: Request, session_id: str | None = None) -> dict[str, Any]:
        res = resources(request)
        if session_id:
            session = await res.store.get_session(session_id)
        else:
            session = await res.sessions.current_session()
        if session is None:
            raise _fail(404, "找不到这场会议" if session_id else "现在没有会议")
        await res.store.require_available(session.id)
        return {"items": [task_json(t) for t in await res.store.list_tasks(session.id)]}

    @app.get("/api/tasks/{task_id}")
    async def get_task(request: Request, task_id: str) -> dict[str, Any]:
        res = resources(request)
        task = await find(request, task_id)
        body = task_json(task)
        body["detail_md"] = task.detail_md or ""
        body["sources"] = task.sources
        body["artifacts"] = task.artifacts
        body["requested_by"] = await res.store.speaker_name(task.session_id, task.requested_by)
        body["requested_t"] = task.requested_t
        body["events"] = [event_json(e) for e in await res.store.list_task_events(task.id)]
        # 这次任务外发给远端模型的内容：目标、哪一段转录、哪几张截图
        frames = []
        for frame_id in task.frame_ids:
            frame = await res.store.get_frame(frame_id)
            if frame is not None and frame.session_id == task.session_id:
                frames.append({"id": frame.id, "t": frame.t})
        body["outbound"] = {
            "goal": task.goal,
            "t_from": task.t_from,
            "t_to": task.t_to,
            "frames": frames,
            "model_host": _host(cfg.agent.base_url),
        }
        return body

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(request: Request, task_id: str) -> dict[str, Any]:
        res = resources(request)
        task = await find(request, task_id)
        if task.finished:
            return task_json(task)  # 已经结束的：原样返回，不算错
        if res.tasks is None:
            raise _fail(503, "后台任务功能没有开启")
        cancelled = await res.tasks.cancel(task.id)
        return task_json(cancelled or task)

    @app.get("/api/tasks/{task_id}/artifacts/{name:path}")
    async def artifact(request: Request, task_id: str, name: str) -> FileResponse:
        task = await find(request, task_id)
        safe = safe_artifact_name(name)
        # 只给任务自己报告过、并且确实取回来了的产物
        if safe is None or safe not in task.artifacts:
            raise _fail(404, "找不到这个产物文件")
        data_dir = cfg.resolve(cfg.session.data_dir)
        path = await asyncio.to_thread(_artifact_file, data_dir, task, safe)
        if path is None:
            raise _fail(404, "找不到这个产物文件")
        filename = os.path.basename(path)
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        # 产物是远端模型写的代码生成的：不让浏览器把它当页面或脚本执行
        inline = media_type in ("image/png", "image/jpeg", "image/webp", "image/gif")
        return FileResponse(
            path,
            media_type=media_type if inline else "application/octet-stream",
            filename=None if inline else filename,
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-cache"},
        )


def _artifact_file(data_dir: Path, task: TaskRecord, name: str) -> str | None:
    """任务产物在磁盘上的真实路径；不在任务目录之内、或者不是文件，返回 None。"""
    # 任务目录必须在数据目录的 sessions/ 之下，产物必须在任务目录之下
    root = _inside(str(data_dir / "sessions"), str(task_dir(data_dir, task)))
    path = _inside(root, name) if root else None
    return path if path and os.path.isfile(path) else None


def _inside(base: str, target: str) -> str | None:
    """``target``（相对 ``base`` 的路径，或绝对路径）落在 ``base`` 之内时返回它的真实路径，否则返回 None。

    先按字面规整（去掉 ``..``）核对一次，再解析符号链接核对一次：产物是模型写的代码生成的，
    不能让一个指到目录外面的链接把别的文件带出去。
    """
    base = os.path.normpath(base)
    path = os.path.normpath(os.path.join(base, target))
    if not path.startswith(base + os.sep):
        return None
    real_base = os.path.realpath(base)
    real = os.path.realpath(path)
    if not real.startswith(real_base + os.sep):
        return None
    return real


def _host(base_url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(base_url).hostname or ""
