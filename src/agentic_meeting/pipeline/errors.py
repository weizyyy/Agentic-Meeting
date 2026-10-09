"""把管线里的错误翻译成给页面的中文提示（docs/architecture.md §9）。

实时模型或语音合成出错时，Pipecat 会向上游推一个 ``ErrorFrame``；页面默认只能拿到一句英文的原始错误。
这里按出错的是哪个服务，换成用户看得懂的一句话：

* 实时模型：「助理暂不可用：<原因>」；
* 语音合成：「语音不可用，助理这次只出文字：<原因>」。

同一类提示在一段时间内只发一次——服务连不上时每句话、每次请求都会报错，没必要刷屏。
其他处理器的错误只进日志（它们各自有自己的提示，或者对用户不可见）。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

# 同一类提示两次之间至少隔这么久。
MIN_INTERVAL_SECS = 30.0
REASON_MAX_CHARS = 120

Notice = Callable[[str, str], Awaitable[None]]


def describe_reason(frame: Any) -> str:
    """用一句中文说明出错的原因；认不出来就用错误原文（截短）。"""
    exception = getattr(frame, "exception", None)
    status = getattr(exception, "status_code", None)
    name = type(exception).__name__ if exception is not None else ""
    if status in (401, 403):
        return "密钥被拒绝，请检查配置里的密钥变量"
    if status == 404:
        return "接口地址或模型名不对（404）"
    if status == 429:
        return "被服务端限流了，稍后再试"
    if isinstance(status, int) and status >= 500:
        return f"服务端出错（{status}）"
    if "Timeout" in name:
        return "请求超时"
    if "Connection" in name or "Connect" in name:
        return "连不上服务"
    text = str(getattr(frame, "error", "") or "未知错误").strip()
    return text if len(text) <= REASON_MAX_CHARS else text[:REASON_MAX_CHARS] + "…"


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
            kind, text = "llm", f"助理暂不可用：{describe_reason(frame)}"
        elif processor is not None and processor is self._tts:
            kind, text = "tts", f"语音不可用，助理这次只出文字：{describe_reason(frame)}"
        else:
            return
        now = self._clock()
        last = self._last.get(kind)
        if last is not None and now - last < self._min_interval:
            return
        self._last[kind] = now
        try:
            await self._notice("warn", text)
        except Exception:
            logger.exception("发送服务不可用的提示失败")
