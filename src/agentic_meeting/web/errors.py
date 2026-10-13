"""HTTP 接口的错误（docs/interfaces.md §5.9）：响应体是 ``{"error", "code", "params"}``。

处理器里 ``raise ApiError(状态码, 代码, **参数)``；app.py 的异常处理器把它变成响应。
代码和中文在 ``agentic_meeting/messages.py`` 的 ``ERRORS`` 里。
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agentic_meeting.messages import Message, message


class ApiError(StarletteHTTPException):
    """带代码的接口错误。``detail`` 是它的中文，给日志和没走异常处理器的地方用。"""

    def __init__(self, status: int, code: str, **params: str | int) -> None:
        self.message: Message = message(code, **params)
        super().__init__(status, self.message.text)


def invalid(detail: str, status: int = 400) -> ApiError:
    """页面不会发出的请求（缺字段、格式不对）：``request.invalid``，``detail`` 说明哪里不对。"""
    return ApiError(status, "request.invalid", detail=detail)


def error_response(
    status: int, code: str, *, headers: dict[str, str] | None = None, **params: str | int
) -> JSONResponse:
    """不经异常处理器、直接返回的错误响应（中间件、登录接口）。"""
    return JSONResponse(message(code, **params).error(), status_code=status, headers=headers)
