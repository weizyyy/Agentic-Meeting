"""文字输入与同模态应答（docs/architecture.md §5.5、docs/interfaces.md §6.2、§6.3）。

浏览器输入框里打的字是不经过语音的第二个入口：不方便出声时，用它向助理提问或委托任务。
**什么模态输入，就用什么模态回答**——文字请求只用文字回答（跳过语音合成），不会让助理开口打扰别人。

做法（Pipecat 1.12.0 已核实，见 ``pipeline/modality.py``）：实时模型服务把 ``LLMConfigureOutputFrame(skip_tts=…)``
记在自己身上，之后产出的文字帧带着 ``skip_tts``，语音合成服务对带它的帧直接放行、不合成。每个文字请求前面放一个记号：

    TextRequestFrame()                               # 记号：下一个请求只用文字回答
    LLMMessagesAppendFrame([用户消息], run_llm=True)

管线里的 ``ModalityGate`` 在每个新请求进入模型之前按有没有记号设定模态，语音唤醒的请求没有记号、照常朗读。
不能在请求后面紧跟一个「恢复朗读」的帧：模型先调工具、工具结果回来后再生成时，恢复帧已经生效，那段回答就被念出来了。

**不打断**：文字请求不推 ``InterruptionFrame``。助理正在生成、执行工具或朗读时它先排队（先进先出，最多 ``max_queued`` 条），
空闲后依次处理——否则新消息会比还没写进上下文的上一轮回答先进上下文，顺序就乱了。
别人正在说话不算忙：文字回答不出声，不会打扰。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from loguru import logger
from pipecat.frames.frames import Frame, LLMMessagesAppendFrame

from agentic_meeting.pipeline.modality import TextRequestFrame
from agentic_meeting.types import Utterance

MAX_TEXT_CHARS = 2000
MAX_QUEUED = 5
BUSY_TIMEOUT_SECS = (
    60.0  # 发出请求后一直没有收到「助理开始 / 结束一轮」的事件（比如模型出错了），就不再等
)


class TypedRecorder(Protocol):
    """文字入口用到的记录器接口（``MeetingRecorder`` 满足它）。"""

    async def record_typed(self, text: str) -> Utterance | None: ...

    async def context_text(self, utterance: Utterance) -> str: ...


def extract_text(data: Any) -> str | None:
    """从浏览器发来的 ``text_input`` 的 data 里取文字；格式不对返回 ``None``。"""
    if isinstance(data, dict) and isinstance(data.get("text"), str):
        return data["text"]
    return None


class TextInputHandler:
    def __init__(
        self,
        *,
        recorder: TypedRecorder,
        push: Callable[[Frame], Awaitable[None]],
        notice: Callable[[str, str], Awaitable[None]],
        tts_enabled: bool,
        max_queued: int = MAX_QUEUED,
        max_chars: int = MAX_TEXT_CHARS,
        busy_timeout_secs: float = BUSY_TIMEOUT_SECS,
        on_dispatch: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        # 每次把请求交给模型之前调用（让后台模型先让路）
        self._on_dispatch = on_dispatch
        self._recorder = recorder
        self._push = push
        self._notice = notice
        self._tts_enabled = tts_enabled
        self._max_queued = max_queued
        self._max_chars = max_chars
        self._busy_timeout = busy_timeout_secs
        self._queue: deque[Utterance] = deque()
        self._generating = False  # 助理正在生成回答（一轮开始到结束）
        self._tools = False  # 模型发起了工具调用，结果回来后还要再生成一次
        self._speaking = False  # 助理正在朗读
        self._lock = asyncio.Lock()
        self._watchdog: asyncio.Task | None = None

    # ------------------------------------------------------------------ #
    # 助理状态（由 bot.py 接到助理侧聚合器和记录器的事件上）
    # ------------------------------------------------------------------ #

    @property
    def busy(self) -> bool:
        return self._generating or self._tools or self._speaking

    async def set_generating(self, generating: bool) -> None:
        self._generating = generating
        if generating:
            self._tools = False  # 工具结果回来了，这是接着的那次生成
            self._cancel_watchdog()  # 回答确实开始了，不用再等
        await self._drain_if_idle()

    async def tools_started(self) -> None:
        """模型发起了工具调用：这一轮还没完，等结果回来后的再次生成。"""
        self._tools = True
        self._arm_watchdog()  # 工具一直不回来（或结果不触发再次生成）时不能永远算忙

    async def set_speaking(self, speaking: bool) -> None:
        self._speaking = speaking
        await self._drain_if_idle()

    @property
    def queued(self) -> int:
        return len(self._queue)

    async def close(self) -> None:
        """连接结束：停掉看门狗，丢掉还没交出去的排队消息（它们已经落库，只是没有得到回答）。"""
        self._cancel_watchdog()
        self._queue.clear()

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #

    async def handle(self, raw: str) -> None:
        """处理一条浏览器发来的文字。"""
        text = raw.strip()
        if not text:
            await self._warn("消息是空的，没有发送")
            return
        if len(text) > self._max_chars:
            await self._warn(f"消息太长（最多 {self._max_chars} 字），没有发送")
            return
        if self.busy and len(self._queue) >= self._max_queued:
            await self._warn(f"助理正忙，排队已满（{self._max_queued} 条），这条消息没有发送")
            return

        utterance = await self._recorder.record_typed(text)  # 立刻落库并显示在字幕里
        if utterance is None:
            return
        async with self._lock:
            if self.busy or self._queue:
                self._queue.append(utterance)
                position = len(self._queue)
                await self._info(f"助理正忙，这条消息排在第 {position} 位，稍后回答")
                return
            await self._dispatch(utterance)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    async def _drain_if_idle(self) -> None:
        async with self._lock:
            while self._queue and not self.busy:
                await self._dispatch(self._queue.popleft())

    async def _dispatch(self, utterance: Utterance) -> None:
        """把一条文字请求交给实时模型：只出文字、不打断。已经持有 ``_lock``。"""
        try:
            line = await self._recorder.context_text(utterance)
            if self._on_dispatch is not None:
                try:
                    await self._on_dispatch()
                except Exception:
                    logger.exception("文字请求发出前的通知失败")
            if self._tts_enabled:
                await self._push(TextRequestFrame())
            await self._push(
                LLMMessagesAppendFrame(messages=[{"role": "user", "content": line}], run_llm=True)
            )
        except Exception:
            logger.exception("把文字请求交给实时模型失败")
            return
        self._generating = True  # 乐观地算作忙：真正的开始 / 结束事件随后会校正
        self._arm_watchdog()

    def _arm_watchdog(self) -> None:
        self._cancel_watchdog()
        self._watchdog = asyncio.create_task(self._expire_busy())

    def _cancel_watchdog(self) -> None:
        if self._watchdog is not None and self._watchdog is not asyncio.current_task():
            self._watchdog.cancel()
        self._watchdog = None

    async def _expire_busy(self) -> None:
        await asyncio.sleep(self._busy_timeout)
        if self._generating or self._tools:
            logger.warning("等了很久也没有等到助理开始回答（或工具的结果），不再等它")
            self._watchdog = None
            self._tools = False
            await self.set_generating(False)

    async def _warn(self, text: str) -> None:
        await self._safe_notice("warn", text)

    async def _info(self, text: str) -> None:
        await self._safe_notice("info", text)

    async def _safe_notice(self, level: str, text: str) -> None:
        try:
            await self._notice(level, text)
        except Exception:
            logger.exception("发送提示失败")
