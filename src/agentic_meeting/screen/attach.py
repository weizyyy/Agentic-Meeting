"""把截图原图附给模型：挑哪些、怎么排成消息（docs/architecture.md §5.3）。

平时各个环节（纪要、报告、上下文）只用截图的文字摘要。``agent.attach_frames`` 打开时，交给后台 agent 那个模型的
请求（会后报告、滚动纪要、后台任务）还会带上截图原图；实时模型的 ``look_at_screen`` 回看之前的截图时也用这里的挑选规则。

挑选规则（:func:`select_frames`）：

* 「无关画面」不要（摘要等于 ``IRRELEVANT_CAPTION`` 的）；
* 画面没变的不重复带：和上一张留下的摘要完全相同的跳过（浏览器每分钟的兜底截图）；没有摘要的照留——图本身还在；
* 超过上限时均匀抽取，第一张和最后一张一定留。
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from loguru import logger

from agentic_meeting.pipeline.clock import format_hms
from agentic_meeting.screen.caption import IRRELEVANT_CAPTION
from agentic_meeting.screen.ingest import media_type_of
from agentic_meeting.store.work import drain_io
from agentic_meeting.types import ScreenFrame

ATTACH_INTRO = "下面是会议中的屏幕截图，按时间先后排列，每张前面标了它出现的时间："


def is_relevant(frame: ScreenFrame) -> bool:
    return (frame.caption or "").strip() != IRRELEVANT_CAPTION


def select_frames(frames: Sequence[ScreenFrame], limit: int) -> list[ScreenFrame]:
    """按上面的规则挑出要带的截图，按时间升序。``limit <= 0`` 时一张都不带。"""
    if limit <= 0:
        return []
    kept: list[ScreenFrame] = []
    last_caption: str | None = None
    for frame in sorted(frames, key=lambda f: (f.t, f.id or 0)):
        if not is_relevant(frame):
            continue
        caption = (frame.caption or "").strip()
        if caption and caption == last_caption:
            continue
        kept.append(frame)
        last_caption = caption or None
    if len(kept) <= limit:
        return kept
    if limit == 1:
        return [kept[-1]]
    step = (len(kept) - 1) / (limit - 1)
    return [kept[round(i * step)] for i in range(limit)]


def frame_label(frame: ScreenFrame) -> str:
    return f"[画面 {format_hms(frame.t)}]"


class FrameAttacher:
    """读截图文件，排成 OpenAI 格式的消息内容（一段说明 + 每张图前面一行时间）。"""

    def __init__(self, path_of: Callable[[ScreenFrame], Path | None], limit: int) -> None:
        self._path_of = path_of
        self.limit = limit

    def _read(self, frame: ScreenFrame) -> str | None:
        path = self._path_of(frame)
        if path is None:
            return None
        try:
            data = path.read_bytes()
        except OSError:
            logger.warning(f"截图文件读不到，不附这一张：{frame.path}")
            return None
        return f"data:{media_type_of(frame.path)};base64,{base64.b64encode(data).decode('ascii')}"

    async def parts(self, frames: Sequence[ScreenFrame]) -> list[dict[str, Any]]:
        """挑选后的截图排成内容片段；一张都没有时返回空列表。读文件放线程里做。"""
        chosen = select_frames(frames, self.limit)
        urls = await drain_io(asyncio.to_thread(lambda: [self._read(frame) for frame in chosen]))
        parts: list[dict[str, Any]] = []
        for frame, url in zip(chosen, urls, strict=True):
            if url is None:
                continue
            parts.append({"type": "text", "text": frame_label(frame)})
            parts.append({"type": "image_url", "image_url": {"url": url}})
        if parts:
            parts.insert(0, {"type": "text", "text": ATTACH_INTRO})
        return parts

    async def user_message(self, prompt: str, frames: Sequence[ScreenFrame]) -> dict[str, Any]:
        """一条用户消息：提示词在前，截图在后；没有可附的截图时就是普通的文字消息。"""
        parts = await self.parts(frames)
        if not parts:
            return {"role": "user", "content": prompt}
        return {"role": "user", "content": [{"type": "text", "text": prompt}, *parts]}
