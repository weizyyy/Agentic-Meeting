"""会后报告的 HTTP 接口（docs/interfaces.md §5.6）。

``POST /api/sessions/{id}/report`` 触发生成（异步，立刻返回 202）；``GET`` 取最近一份；``report.md`` 下载文本。
页面在生成期间每 2 秒轮询一次 ``GET``，不依赖音频连接。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from agentic_meeting.pipeline.report import ReportBusy, ReportUnavailable
from agentic_meeting.types import Report
from agentic_meeting.web.export import download_headers, export_title

NOT_FOUND_SESSION = "找不到这场会议"
NO_REPORT = "这场会议还没有报告"


def report_json(report: Report) -> dict[str, Any]:
    return {
        "id": report.id,
        "status": report.status,
        "created_at": report.created_at,
        "provider": report.provider,
        "text_md": report.text_md,
        "error": report.error,
    }


def register(app: FastAPI) -> None:
    def parts(request: Request) -> tuple[Any, Any, Any]:
        resources = request.app.state.resources
        if resources.store is None or resources.sessions is None:
            raise StarletteHTTPException(503, "存储尚未就绪")
        return resources.store, resources.sessions, resources.reports

    @app.post("/api/sessions/{session_id}/report")
    async def start_report(request: Request, session_id: str) -> JSONResponse:
        store, manager, reports = parts(request)
        if await store.get_session(session_id) is None:
            raise StarletteHTTPException(404, NOT_FOUND_SESSION)
        await store.require_available(session_id)
        if manager.is_live(session_id):
            raise StarletteHTTPException(409, "会议正在进行，结束或断开之后才能生成报告")
        if reports is None or not reports.enabled:
            raise StarletteHTTPException(503, "没有可用的模型，生成不了报告")
        try:
            report_id = await reports.start(session_id)
        except ReportBusy as e:
            raise StarletteHTTPException(409, "这场会议已经有一份报告正在生成") from e
        except ReportUnavailable as e:
            raise StarletteHTTPException(503, str(e)) from e
        return JSONResponse({"report_id": report_id, "status": "running"}, status_code=202)

    @app.get("/api/sessions/{session_id}/report")
    async def latest_report(request: Request, session_id: str) -> dict[str, Any]:
        store, _, _ = parts(request)
        if await store.get_session(session_id) is None:
            raise StarletteHTTPException(404, NOT_FOUND_SESSION)
        await store.require_available(session_id)
        report = await store.latest_report(session_id)
        if report is None:
            raise StarletteHTTPException(404, NO_REPORT)
        return report_json(report)

    @app.get("/api/sessions/{session_id}/report.md")
    async def download_report(request: Request, session_id: str) -> Response:
        store, _, _ = parts(request)
        summary = await store.get_summary(session_id)
        if summary is None:
            raise StarletteHTTPException(404, NOT_FOUND_SESSION)
        await store.require_available(session_id)
        report = await store.latest_report(session_id, done_only=True)
        if report is None:
            raise StarletteHTTPException(404, "这场会议还没有生成好的报告")
        return Response(
            report.text_md,
            media_type="text/markdown; charset=utf-8",
            headers=download_headers(
                f"{export_title(summary)} 会后报告", session_id, stem="report"
            ),
        )
