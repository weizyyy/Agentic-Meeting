"""滚动纪要（docs/architecture.md §6）。

每隔 ``realtime.digest_interval_minutes`` 看一眼正在进行的会议：自上一份纪要以来有新发言，就把
「上一份纪要 + 新增的转录」交给后台模型，生成更新后的**完整**纪要，写进 ``digests`` 表。

* 纪要的文本是**累积**的：每一份都覆盖从会议开头到当时。上下文压缩时只需要拿最新的一份。
  ``t_from`` / ``t_to`` 只记这一次新纳入的那一段，相邻两份首尾相接。
* 「新发言」按发言编号算（``last_utterance_id``），不按时间：助理的话是一轮结束时才落库的，
  它的开始时间可能早于上一份纪要的终点，按时间算会漏掉。
* 和画面摘要共用同一个后台模型入口：助理应答时暂停，被取消的这一次稍后重做。
* 一次最多纳入 ``MAX_NEW_UTTERANCES`` 条：纪要停了很久（后台模型出过故障）之后不会一口气塞进一个超长的请求，
  而是分几轮追上。
* 连接断开或会议结束时再做一次（``finalize``），让纪要覆盖到最后；有超时，不会卡住别的事。

没有后台模型时什么都不做，``run_once`` 返回 ``None``。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from loguru import logger

from agentic_meeting.pipeline.background import Preempted
from agentic_meeting.pipeline.clock import context_line
from agentic_meeting.screen.caption import IRRELEVANT_CAPTION, screen_line
from agentic_meeting.store.db import Store
from agentic_meeting.types import Digest, NamedUtterance, ScreenFrame

DIGEST_MAX_TOKENS = 1500
MAX_NEW_UTTERANCES = 300
RETRY_AFTER_PREEMPT_SECS = 15.0
FINALIZE_TIMEOUT_SECS = 120.0
NO_PREVIOUS_DIGEST = "（无）"


class DigestModel(Protocol):
    async def run(self, messages: list, *, system: str = "", max_tokens: int) -> str: ...


def transcript_text(utterances: list[NamedUtterance], frames: list[ScreenFrame]) -> str:
    """把发言和画面摘要按时间排成一段文字，格式与实时模型上下文里的行一致。

    连续相同的画面摘要只留第一条；「无关画面」和没有摘要的截图不出现。
    """
    lines: list[tuple[float, int, str]] = []
    for item in utterances:
        u = item.utterance
        lines.append((u.t_start, 0, context_line(u.t_start, item.speaker_name, u.text)))
    last_caption: str | None = None
    for frame in sorted(frames, key=lambda f: (f.t, f.id or 0)):
        caption = (frame.caption or "").strip()
        if not caption or caption == IRRELEVANT_CAPTION or caption == last_caption:
            continue
        last_caption = caption
        lines.append((frame.t, 1, screen_line(frame.t, caption)))
    lines.sort(key=lambda x: (x[0], x[1]))
    return "\n".join(text for _, _, text in lines)


class DigestWorker:
    def __init__(
        self,
        *,
        store: Store,
        model: DigestModel | None,
        render: Callable[[str, str], str],
        interval_secs: float,
        current_session: Callable[[], str | None],
        attacher: Any = None,
        max_tokens: int = DIGEST_MAX_TOKENS,
        max_new_utterances: int = MAX_NEW_UTTERANCES,
        retry_secs: float = RETRY_AFTER_PREEMPT_SECS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """``render(此前的纪要, 新增的转录)`` 给出完整的提示词（``config/prompts/digest.md``）；
        ``current_session()`` 返回正在进行的会议编号，没有返回 ``None``。
        ``attacher``（``screen/attach.py`` 的 ``FrameAttacher``）给了就把这一轮时间段里的截图原图附在请求里。
        """
        self._store = store
        self._model = model
        self._render = render
        self._interval = interval_secs
        self._current_session = current_session
        self._attacher = attacher
        self._max_tokens = max_tokens
        self._max_new = max_new_utterances
        self._retry = retry_secs
        self._sleep = sleep
        self._lock = asyncio.Lock()  # 同一时刻只做一份：定时的、收尾的、压缩前催的都走这里
        self._task: asyncio.Task | None = None
        self._finalizing: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return self._model is not None

    def start(self) -> None:
        if self._model is not None and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="rolling-digest")

    async def stop(self) -> None:
        tasks = [t for t in (self._task, *self._finalizing) if t is not None]
        self._task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------ #
    # 生成一份
    # ------------------------------------------------------------------ #

    async def run_once(self, session_id: str) -> Digest | None:
        """有新发言就生成一份新纪要并返回；没有新发言、没有后台模型、模型给了空结果时返回 ``None``。

        被实时应答抢占时抛 ``Preempted``，模型服务出错时抛它的异常——由调用方决定怎么办。
        """
        if self._model is None:
            return None
        async with self._lock:
            previous = await self._store.latest_digest(session_id)
            after_id = previous.last_utterance_id if previous else 0
            new = await self._store.list_utterances(
                session_id, after_id=after_id, limit=self._max_new
            )
            if not new:
                return None
            t_from = previous.t_to if previous else 0.0
            t_to = max(t_from, max(n.utterance.t_end for n in new))
            frames = await self._store.list_frames(session_id, t_from=t_from, t_to=t_to)
            prompt = self._render(
                previous.text if previous else NO_PREVIOUS_DIGEST, transcript_text(new, frames)
            )
            message: dict[str, Any] = {"role": "user", "content": prompt}
            if self._attacher is not None and frames:
                message = await self._attacher.user_message(prompt, frames)
            text = await self._model.run([message], max_tokens=self._max_tokens)
            if not text:
                logger.warning("后台模型给出的纪要是空的，这一轮不算，下次再试")
                return None
            last_id = max(n.utterance.id or 0 for n in new)
            return await self._store.add_digest(
                session_id, t_from=t_from, t_to=t_to, text=text, last_utterance_id=last_id
            )

    async def catch_up(self, session_id: str) -> Digest | None:
        """一直做到没有新发言为止（积压很多时要分几轮），返回最后生成的那一份。"""
        latest: Digest | None = None
        while (digest := await self.run_once(session_id)) is not None:
            latest = digest
        return latest

    def finalize(self, session_id: str, *, timeout_secs: float = FINALIZE_TIMEOUT_SECS) -> None:
        """连接断开或会议结束：在后台把纪要补到最后。不等它，失败只记日志。"""
        if self._model is None:
            return
        task = asyncio.create_task(self._finalize(session_id, timeout_secs), name="final-digest")
        self._finalizing.add(task)
        task.add_done_callback(self._finalizing.discard)

    async def _finalize(self, session_id: str, timeout_secs: float) -> None:
        try:
            await asyncio.wait_for(self.catch_up(session_id), timeout_secs)
        except TimeoutError:
            logger.warning("会议收尾的纪要没能在限定时间内生成")
        except Preempted:
            logger.info("会议收尾的纪要被实时应答打断了，这次不做")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"会议收尾的纪要生成失败：{type(e).__name__}: {e}")

    # ------------------------------------------------------------------ #
    # 定时
    # ------------------------------------------------------------------ #

    async def _loop(self) -> None:
        delay = self._interval
        while True:
            await self._sleep(delay)
            delay = self._interval
            session_id = self._current_session()
            if session_id is None:
                continue
            try:
                await self.catch_up(session_id)
            except Preempted:
                delay = min(self._interval, self._retry)  # 助理在应答：过一会儿再做
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"滚动纪要生成失败，下个周期再试：{type(e).__name__}: {e}")
