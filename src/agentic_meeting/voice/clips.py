"""会议进行中切出单人语音片段、算声纹、落库（docs/interfaces.md §4.4）。

记录器把收到的音频、每条识别发言落库或并入上一条的时间范围、助理说话的时段交给 :class:`VoiceClipper`。
一条发言不再增长（来了别的发言，或停了一阵）、并且说话人区分和延迟校正都稳定下来之后：

1. 取这条发言各段语音的时间范围，去掉助理说话的时段（回声）；
2. 有说话人区分时只留恰好一个槽位在说话的部分，并且只留这条发言的说话人那个槽位；
3. 每段两端各裁掉一点，按顺序拼成不超过 ``MAX_CLIP_SECS`` 的窗口，丢掉不足 ``MIN_CLIP_SECS`` 的；
4. 每个窗口请声纹服务算一个向量，连同发言编号落库。

片段挂在发言上：改说话人、合并说话人都跟着发言走，删发言（保留期、删会议）时一起删掉。

**不影响转录**：这里的任何失败（声纹服务不通、超时、落库失败）都只记日志、丢掉这一段，转录照常。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from loguru import logger

from agentic_meeting.types import SpeakerSegment, Utterance
from agentic_meeting.voice.client import SpeakerClient, SpeakerServiceError

SAMPLE_RATE = 16000
_BYTES_PER_SEC = SAMPLE_RATE * 2

BUFFER_SECS = 60.0  # 环形缓冲保留的音频
MIN_CLIP_SECS = 1.0  # 一个窗口至少要有这么多语音
MAX_CLIP_SECS = 6.0  # 一个窗口最多这么多语音
MIN_PIECE_SECS = 0.3  # 裁边之后更短的语音段不要
EDGE_TRIM_SECS = 0.1  # 每段两端裁掉的时长：边上常带着别人的尾音或噪声
SETTLE_SECS = 6.0  # 发言结束这么久之后再切：说话人区分的结论和延迟校正（5 秒）都已稳定
TICK_SECS = 1.0
MAX_PENDING = 32  # 等着处理的发言超过这么多就丢最旧的（声纹是尽力而为）
EMBED_TIMEOUT_SECS = 5.0


@dataclass(frozen=True, slots=True)
class VoiceClip:
    session_id: str
    utterance_id: int
    t_start: float
    t_end: float
    secs: float  # 窗口里语音的总时长（不含被裁掉的空档）
    diar_idx: int  # 说话人区分的槽位；没有说话人区分时为 0
    vector: list[float]
    model_sha256: str


class VoiceClipStore(Protocol):
    async def add_voice_clip(self, clip: VoiceClip, *, now: float | None = None) -> bool: ...


class AudioRing:
    """最近 ``max_secs`` 秒的音频，按会话时间轴取用。"""

    def __init__(self, max_secs: float = BUFFER_SECS) -> None:
        self._max_bytes = int(max_secs * _BYTES_PER_SEC)
        self._chunks: deque[tuple[float, bytes]] = deque()  # (这块的结束时刻, PCM)
        self._bytes = 0

    def append(self, pcm16: bytes, end_secs: float) -> None:
        if not pcm16:
            return
        self._chunks.append((end_secs, pcm16))
        self._bytes += len(pcm16)
        while self._bytes - len(self._chunks[0][1]) >= self._max_bytes:
            _, dropped = self._chunks.popleft()
            self._bytes -= len(dropped)

    def slice(self, t_start: float, t_end: float) -> bytes | None:
        """``[t_start, t_end)`` 的音频；有一部分已经不在缓冲里时返回 None。"""
        if not self._chunks or t_end <= t_start:
            return None
        first_end, first = self._chunks[0]
        if t_start < first_end - len(first) / _BYTES_PER_SEC - 1e-6:
            return None
        if t_end > self._chunks[-1][0] + 1e-6:
            return None
        out = bytearray()
        for end, pcm in self._chunks:
            start = end - len(pcm) / _BYTES_PER_SEC
            if end <= t_start or start >= t_end:
                continue
            a = max(0, int(round((t_start - start) * SAMPLE_RATE)) * 2)
            b = min(len(pcm), int(round((t_end - start) * SAMPLE_RATE)) * 2)
            out += pcm[a:b]
        return bytes(out)


@dataclass
class _Group:
    """一条发言（并进来的片段都算它的）。"""

    utterance: Utterance
    spans: list[tuple[float, float]] = field(default_factory=list)
    closed: bool = False

    @property
    def end(self) -> float:
        return max(e for _, e in self.spans)


def subtract(
    spans: Sequence[tuple[float, float]], holes: Sequence[tuple[float, float]]
) -> list[tuple[float, float]]:
    """从每个区间里挖掉 ``holes``。"""
    result: list[tuple[float, float]] = []
    for start, end in spans:
        pieces = [(start, end)]
        for h_start, h_end in holes:
            cut: list[tuple[float, float]] = []
            for a, b in pieces:
                if h_end <= a or h_start >= b:
                    cut.append((a, b))
                    continue
                if h_start > a:
                    cut.append((a, h_start))
                if h_end < b:
                    cut.append((h_end, b))
            pieces = cut
        result += pieces
    return result


def single_speaker_pieces(
    spans: Sequence[tuple[float, float]], segments: Sequence[SpeakerSegment]
) -> list[tuple[float, float, int]]:
    """区间里恰好一个槽位在说话的部分，带上那个槽位。"""
    result: list[tuple[float, float, int]] = []
    for start, end in spans:
        inside = [s for s in segments if s.end > start and s.start < end]
        cuts = sorted(
            {start, end, *(t for s in inside for t in (s.start, s.end) if start < t < end)}
        )
        for a, b in zip(cuts, cuts[1:], strict=False):
            mid = (a + b) / 2
            active = {s.speaker for s in inside if s.start <= mid < s.end}
            if len(active) == 1:
                slot = active.pop()
                if result and result[-1][2] == slot and abs(result[-1][1] - a) < 1e-9:
                    result[-1] = (result[-1][0], b, slot)  # 相邻的同一槽位接起来
                else:
                    result.append((a, b, slot))
    return result


def choose_slot(pieces: Sequence[tuple[float, float, int]], speaker_idx: int) -> int | None:
    """发言的说话人就是某个槽位时取它；否则（未知、手动指定的说话人）取说得最多的槽位。"""
    totals: dict[int, float] = {}
    for a, b, slot in pieces:
        totals[slot] = totals.get(slot, 0.0) + (b - a)
    if not totals:
        return None
    if speaker_idx in totals:
        return speaker_idx
    return max(sorted(totals), key=lambda slot: totals[slot])


def windows(pieces: Sequence[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    """裁边、去掉太短的段，按顺序拼成不超过 ``MAX_CLIP_SECS`` 的窗口；不足 ``MIN_CLIP_SECS`` 的丢掉。"""
    trimmed = [
        (a + EDGE_TRIM_SECS, b - EDGE_TRIM_SECS)
        for a, b in pieces
        if b - a - 2 * EDGE_TRIM_SECS >= MIN_PIECE_SECS
    ]
    result: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    filled = 0.0
    for a, b in trimmed:
        while b > a:
            take = min(b - a, MAX_CLIP_SECS - filled)
            current.append((a, a + take))
            filled += take
            a += take
            if filled >= MAX_CLIP_SECS - 1e-9:
                result.append(current)
                current, filled = [], 0.0
    if current and filled >= MIN_CLIP_SECS:
        result.append(current)
    return result


class VoiceClipper:
    """一场会议的一次连接里的声纹片段采集。所有方法都在事件循环里调用；计算在声纹服务里。"""

    def __init__(
        self,
        *,
        client: SpeakerClient,
        store: VoiceClipStore,
        session_id: str,
        segments: Callable[[float], Awaitable[list[SpeakerSegment] | None]] | None = None,
        idle_close_secs: float = 3.0,
        on_stored: Callable[[], None] | None = None,
        on_service: Callable[[bool], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._on_stored = on_stored  # 每落库一个片段调用一次
        self._on_service = on_service  # 声纹服务断了（False）或恢复了（True）时调用一次
        self._store = store
        self._session_id = session_id
        self._segments = segments
        self._idle_close = idle_close_secs
        self._ring = AudioRing()
        self._groups: dict[int, _Group] = {}  # 按发言编号
        self._assistant: list[tuple[float, float | None]] = []
        self._now = 0.0
        self._failing = False

    # ---- 记录器调用 ----

    def push_audio(self, pcm16: bytes, end_secs: float) -> None:
        self._ring.append(pcm16, end_secs)
        self._now = end_secs

    def on_speech(self, utterance: Utterance, t_start: float, t_end: float) -> None:
        """一段识别语音落库（新发言）或并进了 ``utterance``。"""
        if utterance.id is None or utterance.source != "asr" or t_end <= t_start:
            return
        for other_id, other in self._groups.items():
            if other_id != utterance.id:
                other.closed = True  # 来了别的发言，之前的不会再并进东西了
        group = self._groups.setdefault(utterance.id, _Group(utterance))
        group.spans.append((t_start, t_end))
        if len(self._groups) > MAX_PENDING:
            oldest = min(self._groups, key=lambda key: self._groups[key].end)
            del self._groups[oldest]
            logger.debug("声纹片段积压，丢掉最旧的一条发言")

    def on_assistant_speaking(self, start: float, end: float | None) -> None:
        if end is None:
            self._assistant.append((start, None))
        elif self._assistant and self._assistant[-1] == (start, None):
            self._assistant[-1] = (start, end)
        else:
            self._assistant.append((start, end))

    # ---- 后台 ----

    async def run(self) -> None:
        """每秒处理一次已经稳定的发言，直到被取消。"""
        while True:
            await asyncio.sleep(TICK_SECS)
            await self._process(ready_before=self._now - SETTLE_SECS)

    async def drain(self) -> None:
        """连接结束：剩下的发言都处理掉（音频已经送完，不再等稳定）。"""
        for group in self._groups.values():
            group.closed = True
        await self._process(ready_before=float("inf"))

    async def _process(self, *, ready_before: float) -> None:
        for key, group in list(self._groups.items()):
            if not group.closed and self._now - group.end >= self._idle_close:
                group.closed = True
            if group.closed and group.end <= ready_before:
                del self._groups[key]
                try:
                    await self._clip(group)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("切声纹片段失败，跳过这条发言（转录不受影响）")
        horizon = self._now - BUFFER_SECS
        self._assistant = [p for p in self._assistant if p[1] is None or p[1] > horizon]

    async def _clip(self, group: _Group) -> None:
        holes = [(a, b if b is not None else float("inf")) for a, b in self._assistant]
        spans = subtract(group.spans, holes)
        if not spans:
            return
        slot = 0
        segments = None
        if self._segments is not None:
            # None 表示没有说话人区分：整条发言都算这个人的
            segments = await self._segments(min(a for a, _ in spans) - 1.0)
        if segments is not None:
            pieces3 = single_speaker_pieces(spans, segments)
            chosen = choose_slot(pieces3, group.utterance.speaker_idx)
            if chosen is None:
                return
            slot = chosen
            spans = [(a, b) for a, b, s in pieces3 if s == chosen]
        for window in windows(spans):
            audio = [self._ring.slice(a, b) for a, b in window]
            if any(part is None for part in audio):
                continue  # 已经不在缓冲里了
            pcm = b"".join(part for part in audio if part is not None)
            vector = await self._embed(pcm)
            if vector is None:
                continue
            vector, model = vector
            clip = VoiceClip(
                session_id=self._session_id,
                utterance_id=group.utterance.id or 0,
                t_start=window[0][0],
                t_end=window[-1][1],
                secs=round(len(pcm) / _BYTES_PER_SEC, 3),
                diar_idx=slot,
                vector=vector,
                model_sha256=model,
            )
            try:
                stored = await self._store.add_voice_clip(clip, now=time.time())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"声纹片段没能落库，已丢弃：{type(e).__name__}")
                continue
            if stored and self._on_stored is not None:
                self._on_stored()

    async def _embed(self, pcm: bytes) -> tuple[list[float], str] | None:
        try:
            async with asyncio.timeout(EMBED_TIMEOUT_SECS):
                info = await self._client.info()
                vector = await self._client.embed(pcm)
        except (SpeakerServiceError, TimeoutError) as e:
            if not self._failing:
                self._failing = True
                logger.warning(f"声纹服务不可用，暂停采集声纹片段（转录不受影响）：{e}")
                await self._report(False)
            return None
        if self._failing:
            self._failing = False
            logger.info("声纹服务恢复，继续采集声纹片段")
            await self._report(True)
        return vector, info.model_sha256

    async def _report(self, available: bool) -> None:
        if self._on_service is None:
            return
        try:
            await self._on_service(available)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("通知页面声纹服务的状态失败")
