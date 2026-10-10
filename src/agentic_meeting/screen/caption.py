"""画面摘要（docs/architecture.md §5.3）。

截图入库后交到这里（``submit``）。一个后台任务每次取**最新**一张待处理的截图，让后台模型看图写一段摘要，
然后：写回数据库 → 向页面推 ``frame_caption`` → 向实时模型的上下文追加一行 ``[画面 时:分:秒] 摘要``。

* **只处理最新一张**：摘要跟不上翻页速度时，积压的旧截图直接记为 ``skipped``——几页之前的幻灯片不值得现在再看。
* **让路**：后台模型被暂停（助理在应答）时不发请求；在途的请求被取消后，这张截图回到待处理（除非又来了更新的）。
* **画面没变的截图**（浏览器的兜底上传，``IngestedFrame.changed`` 为假）不单独生成摘要：沿用上一张的，
  也不往上下文里再追加一行。上一张的摘要没有成（失败、被跳过）时，这一张当作新画面处理，等于隔一会儿重试一次。
* 模型不识图、或配置里关了画面摘要时不启动，截图记为 ``skipped``。
* 模型出错：这张记为 ``failed``，不影响后面的。

提示词是 ``config/prompts/screen_caption.md``。它约定画面与会议无关时只输出 ``IRRELEVANT_CAPTION``；
这种摘要照常入库、显示，但不进实时模型的上下文。
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from loguru import logger
from pipecat.processors.aggregators.llm_context import LLMContext

from agentic_meeting.pipeline.background import Preempted
from agentic_meeting.pipeline.clock import format_hms
from agentic_meeting.screen.ingest import IngestedFrame, media_type_of
from agentic_meeting.types import ScreenFrame

CAPTION_MAX_TOKENS = 200
CAPTION_MAX_CHARS = 300
IRRELEVANT_CAPTION = "无关画面"  # 与 config/prompts/screen_caption.md 里的约定一致
USER_TEXT = "请按要求描述这张屏幕截图。"


class CaptionStore(Protocol):
    async def get_frame(self, frame_id: int) -> ScreenFrame | None: ...

    async def set_frame_caption(
        self, frame_id: int, *, status: str, caption: str | None = None
    ) -> bool: ...


class CaptionModel(Protocol):
    async def run(self, messages: list, *, system: str = "", max_tokens: int) -> str: ...

    async def wait_resumed(self) -> None: ...


def screen_line(t_secs: float, caption: str) -> str:
    """实时模型上下文里的画面行：``[画面 00:14:30] 摘要``（architecture.md §6.1）。"""
    return f"[画面 {format_hms(t_secs)}] {caption}"


def clean_caption(text: str) -> str:
    """模型输出 → 一行摘要：压掉换行和多余空白，过长的截断。"""
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= CAPTION_MAX_CHARS else one_line[:CAPTION_MAX_CHARS] + "…"


class CaptionWorker:
    def __init__(
        self,
        *,
        store: CaptionStore,
        model: CaptionModel | None,
        prompt: str,
        path_of: Callable[[ScreenFrame], Path | None],
        notify: Callable[[str, dict], Awaitable[None]],
        append_context: Callable[[str, float, str], Awaitable[None]],
        max_tokens: int = CAPTION_MAX_TOKENS,
    ) -> None:
        """``model`` 为 ``None`` 表示不生成摘要（配置关了，或模型不识图）。

        ``notify(session_id, data)`` 向页面推一条消息；``append_context(session_id, t, line)`` 向实时模型的上下文
        追加一行（``t`` 是这张截图在会话时间轴上的时刻）。两者都只在那场会议正在进行时才真的发出去，由调用方保证。
        """
        self._store = store
        self._model = model
        self._prompt = prompt
        self._path_of = path_of
        self._notify = notify
        self._append_context = append_context
        self._max_tokens = max_tokens
        self._pending: IngestedFrame | None = None
        self._current: int | None = None  # 正在生成摘要的截图编号
        self._followers: dict[int, list[int]] = {}  # 截图编号 → 画面和它一样、等着沿用它摘要的截图
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self._model is not None

    @property
    def pending_count(self) -> int:
        """待处理新画面的槽位数，不含正在处理的画面和沿用摘要的截图。"""
        return int(self._pending is not None)

    def start(self) -> None:
        if self._model is not None and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="screen-caption")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #

    async def submit(self, ingested: IngestedFrame) -> None:
        frame = ingested.frame
        assert frame.id is not None
        if self._model is None:
            await self._store.set_frame_caption(frame.id, status="skipped")
            return
        if not ingested.changed and ingested.same_as is not None:
            if await self._follow(frame, ingested.same_as):
                return
        # 新画面（或上一张的摘要没成）：它成为待处理的那一张，原来排着的不看了
        previous, self._pending = self._pending, ingested
        if previous is not None:
            await self._settle(previous.frame.id, "skipped")
        self._wake.set()

    async def _follow(self, frame: ScreenFrame, source_id: int) -> bool:
        """画面和 ``source_id`` 那张一样：沿用它的摘要。沿用不了（那张没成）返回 ``False``。"""
        assert frame.id is not None
        waiting = (
            self._pending is not None and self._pending.frame.id == source_id
        ) or self._current == source_id
        if waiting:
            self._followers.setdefault(source_id, []).append(frame.id)
            return True
        source = await self._store.get_frame(source_id)
        if source is None or source.caption_status != "done" or not source.caption:
            return False
        await self._store.set_frame_caption(frame.id, status="done", caption=source.caption)
        await self._safe_notify(frame.session_id, frame.id, source.caption)
        return True

    # ------------------------------------------------------------------ #
    # 后台循环
    # ------------------------------------------------------------------ #

    async def _loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._pending is not None:
                # 先等到后台模型可用，再挑：暂停期间来了更新的画面，就只做更新的那张
                assert self._model is not None
                await self._model.wait_resumed()
                item, self._pending = self._pending, None
                if item is None:
                    break
                try:
                    await self._caption(item)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("画面摘要出错")

    async def _caption(self, item: IngestedFrame) -> None:
        frame = item.frame
        assert frame.id is not None and self._model is not None
        self._current = frame.id
        try:
            try:
                url = await asyncio.to_thread(self._data_url, frame)
                text = await self._model.run(
                    [LLMContext.create_image_url_message(url=url, text=USER_TEXT)],
                    system=self._prompt,
                    max_tokens=self._max_tokens,
                )
            except Preempted:
                if self._pending is None:
                    self._pending = item  # 助理应答完再做这张
                    self._wake.set()
                else:
                    await self._settle(frame.id, "skipped")  # 已经有更新的画面了
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"画面摘要生成失败（截图 {frame.id}）：{type(e).__name__}: {e}")
                await self._settle(frame.id, "failed")
                return
            caption = clean_caption(text)
            if not caption:
                logger.warning(f"画面摘要是空的（截图 {frame.id}）")
                await self._settle(frame.id, "failed")
                return
            await self._store.set_frame_caption(frame.id, status="done", caption=caption)
            await self._safe_notify(frame.session_id, frame.id, caption)
            for follower in self._followers.pop(frame.id, []):
                await self._store.set_frame_caption(follower, status="done", caption=caption)
                await self._safe_notify(frame.session_id, follower, caption)
            if caption != IRRELEVANT_CAPTION:
                try:
                    await self._append_context(
                        frame.session_id, frame.t, screen_line(frame.t, caption)
                    )
                except Exception:
                    logger.exception("把画面摘要追加到实时模型的上下文失败")
        finally:
            self._current = None

    async def _settle(self, frame_id: int | None, status: str) -> None:
        """一张截图不会有摘要了（跳过或失败）：连同等着沿用它的那些一起记下。"""
        if frame_id is None:
            return
        for target in [frame_id, *self._followers.pop(frame_id, [])]:
            try:
                await self._store.set_frame_caption(target, status=status)
            except Exception:
                logger.exception("更新截图的摘要状态失败")

    def _data_url(self, frame: ScreenFrame) -> str:
        path = self._path_of(frame)
        if path is None:
            raise FileNotFoundError(frame.path)
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{media_type_of(frame.path)};base64,{encoded}"

    async def _safe_notify(self, session_id: str, frame_id: int, caption: str) -> None:
        try:
            await self._notify(
                session_id, {"type": "frame_caption", "id": frame_id, "caption": caption}
            )
        except Exception:
            logger.exception("向页面推送画面摘要失败")
