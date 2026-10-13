"""把管线里的错误翻译成给页面的提示（docs/architecture.md §9、docs/interfaces.md §6.1.1）。

实时模型或语音合成出错时，Pipecat 会向上游推一个 ``ErrorFrame``；页面默认只能拿到一句英文的原始错误。
这里按出错的是哪个服务，换成用户看得懂的提示，原因归成几类（``reason``）：

* 实时模型：``llm.unavailable``，「助理暂不可用：<原因>」；
* 语音合成：``tts.unavailable``，「语音不可用，助理这次只出文字：<原因>」。

同一类提示在一段时间内只发一次——服务连不上时每句话、每次请求都会报错，没必要刷屏。
其他处理器的错误只进日志（它们各自有自己的提示，或者对用户不可见）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from loguru import logger

from agentic_meeting.messages import Message, Notice, message

# 同一类提示两次之间至少隔这么久。
MIN_INTERVAL_SECS = 30.0
REASON_MAX_CHARS = 120


def describe_reason(code: str, frame: Any) -> Message:
    """把出错的原因归类；认不出来就用错误原文（截短）。参数见 interfaces.md §6.1.1。"""
    exception = getattr(frame, "exception", None)
    status = getattr(exception, "status_code", None)
    name = type(exception).__name__ if exception is not None else ""
    if status in (401, 403):
        reason = "auth"
    elif status == 404:
        reason = "not_found"
    elif status == 429:
        reason = "rate_limited"
    elif isinstance(status, int) and status >= 500:
        return message(code, reason="server_error", status=status, detail="")
    elif "Timeout" in name:
        reason = "timeout"
    elif "Connection" in name or "Connect" in name:
        reason = "connection"
    else:
        text = str(getattr(frame, "error", "") or "未知错误").strip()
        if len(text) > REASON_MAX_CHARS:
            text = text[:REASON_MAX_CHARS] + "…"
        return message(code, reason="other", status=0, detail=text)
    return message(code, reason=reason, status=0, detail="")


class ErrorNotifier:
    """按出错的服务给页面发提示。``llm`` / ``tts`` 是管线里的那两个处理器（``tts`` 可以没有）。"""

    def __init__(
        self,
        notice: Notice,
        *,
        llm: Any,
        tts: Any = None,
        min_interval_secs: float = MIN_INTERVAL_SECS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._notice = notice
        self._llm = llm
        self._tts = tts
        self._min_interval = min_interval_secs
        self._clock = clock
        self._last: dict[str, float] = {}

    async def on_error(self, frame: Any) -> None:
        processor = getattr(frame, "processor", None)
        if processor is not None and processor is self._llm:
            kind = "llm"
        elif processor is not None and processor is self._tts:
            kind = "tts"
        else:
            return
        now = self._clock()
        last = self._last.get(kind)
        if last is not None and now - last < self._min_interval:
            return
        self._last[kind] = now
        try:
            await self._notice("warn", describe_reason(f"{kind}.unavailable", frame))
        except Exception:
            logger.exception("发送服务不可用的提示失败")
