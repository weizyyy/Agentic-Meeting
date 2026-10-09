"""助理此刻忙不忙（docs/architecture.md §6：互斥）。

「忙」= 助理正在为一次应答工作，从被叫到名字（或收到文字请求）开始，到这一轮生成完、朗读完为止：

* **等着作答**：检测到唤醒词，或刚把一条文字请求交给模型——用户还没说完 / 模型还没开始输出；
* **生成中**：助理侧聚合器的一轮开始到结束；
* **执行工具**：模型发起了工具调用，结果回来后还要再生成一次（两次生成之间这一段也算忙）；
* **朗读中**：``BotStartedSpeakingFrame`` 到 ``BotStoppedSpeakingFrame``。

忙的时候后台模型暂停（画面摘要、滚动纪要让路），也不做上下文压缩和预热；闲下来再恢复。
从忙到闲有一小段延迟（``idle_delay_secs``）：生成结束和开始朗读之间可能有个短暂的空隙，不要在那里来回切。

「等着作答」可能等不来回答（叫了名字却没下文、模型出错），所以有两条退路：唤醒窗口过期的事件，和一个看门狗超时。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from loguru import logger

AWAITING_TIMEOUT_SECS = 60.0
IDLE_DELAY_SECS = 0.5

Listener = Callable[[bool], Awaitable[None] | None]


class AssistantActivity:
    def __init__(
        self,
        *,
        awaiting_timeout_secs: float = AWAITING_TIMEOUT_SECS,
        idle_delay_secs: float = IDLE_DELAY_SECS,
    ) -> None:
        self._awaiting_timeout = awaiting_timeout_secs
        self._idle_delay = idle_delay_secs
        self._awaiting = False
        self._generating = False
        self._speaking = False
        self._reported = False  # 上一次告诉监听者的状态
        self._listeners: list[Listener] = []
        self._watchdog: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._closed = False

    # ---- 查询与订阅 ----

    @property
    def busy(self) -> bool:
        """监听者眼里的状态：从忙到闲要过了延迟才算数。"""
        return self._reported

    @property
    def responding(self) -> bool:
        """助理此刻正在生成或朗读（不含「被叫到名字、还在等用户说完」）。预热只看这个。"""
        return self._generating or self._speaking

    def subscribe(self, listener: Listener) -> None:
        """状态变化时调用 ``listener(busy)``。监听者出错只记日志。"""
        self._listeners.append(listener)

    # ---- 事件（由 bot.py 接到各处） ----

    async def wake_detected(self) -> None:
        await self._set_awaiting(True)

    async def wake_expired(self) -> None:
        await self._set_awaiting(False)

    async def request_sent(self) -> None:
        """一条文字请求交给了模型。"""
        await self._set_awaiting(True)

    async def set_generating(self, generating: bool) -> None:
        self._generating = generating
        if generating:
            self._awaiting = False  # 等的回答来了（或工具结果回来后的再次生成开始了）
            self._cancel(self._watchdog)
            self._watchdog = None
        await self._update()

    async def tools_started(self) -> None:
        """模型发起了工具调用：结果回来后还要再生成一次，这期间仍然算忙（和「等着作答」用同一个看门狗）。"""
        await self._set_awaiting(True)

    async def set_speaking(self, speaking: bool) -> None:
        self._speaking = speaking
        await self._update()

    async def close(self) -> None:
        """连接结束：停掉定时任务，并告诉监听者「闲了」（后台模型不能一直停在暂停上）。"""
        self._closed = True
        self._cancel(self._watchdog)
        self._cancel(self._idle_task)
        self._watchdog = self._idle_task = None
        self._awaiting = self._generating = self._speaking = False
        if self._reported:
            await self._report(False)

    # ---- 内部 ----

    async def _set_awaiting(self, awaiting: bool) -> None:
        if self._closed:
            return
        self._awaiting = awaiting
        self._cancel(self._watchdog)
        self._watchdog = (
            asyncio.create_task(self._expire_awaiting(), name="assistant-awaiting-watchdog")
            if awaiting
            else None
        )
        await self._update()

    async def _expire_awaiting(self) -> None:
        await asyncio.sleep(self._awaiting_timeout)
        self._watchdog = None
        if self._awaiting:
            logger.debug("等了很久也没有等到助理开始回答，不再算作忙")
            self._awaiting = False
            await self._update()

    async def _update(self) -> None:
        if self._closed:
            return
        now_busy = self._awaiting or self._generating or self._speaking
        if now_busy:
            self._cancel(self._idle_task)
            self._idle_task = None
            if not self._reported:
                await self._report(True)
        elif self._reported and self._idle_task is None:
            if self._idle_delay <= 0:
                await self._report(False)
            else:
                self._idle_task = asyncio.create_task(self._become_idle(), name="assistant-idle")

    async def _become_idle(self) -> None:
        await asyncio.sleep(self._idle_delay)
        self._idle_task = None
        if not (self._awaiting or self._generating or self._speaking):
            await self._report(False)

    async def _report(self, busy: bool) -> None:
        self._reported = busy
        for listener in list(self._listeners):
            try:
                result = listener(busy)
                if result is not None:
                    await result
            except Exception:
                logger.exception("通知助理忙闲状态的监听者失败")

    @staticmethod
    def _cancel(task: asyncio.Task | None) -> None:
        if task is not None and task is not asyncio.current_task():
            task.cancel()
