"""一场会议的说话人区分流：比单次连接活得长（docs/architecture.md §3.1「继续的做法」第 2 条）。

同一进程内继续一场会议时，说话人区分的流不重开——模型记得之前的每个人，编号不变。
但流里的时间是「实际喂入的音频累计时长」，和会话时间轴不是同一把尺子：断线的空档不补喂静音，
所以每次连接记一对（流内起点，会话起点），读分段时在出口换算一次。

换算是纯函数（``session_to_stream`` / ``map_segments``），``SessionDiarizer`` 只负责记账：
喂了多少音频、每次连接从哪里接上、编号偏移（服务重启后新开的流）、合并说话人之后的别名。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from agentic_meeting.asr.base import ASR_SAMPLE_RATE
from agentic_meeting.diar.base import Diarizer
from agentic_meeting.types import SpeakerSegment

_BYTES_PER_SEC = ASR_SAMPLE_RATE * 2


@dataclass(frozen=True, slots=True)
class Anchor:
    """一次连接的接续点：流内时间 ``stream_start`` 对应会话时间 ``session_start``。"""

    stream_start: float
    session_start: float


def session_to_stream(anchors: Sequence[Anchor], session_secs: float) -> float:
    """会话时间 → 流内时间。落在断线空档里的时刻，对应空档之后那次连接的起点。"""
    if not anchors:
        return max(0.0, session_secs)
    result = anchors[0].stream_start  # 早于第一次连接的时刻
    for index, anchor in enumerate(anchors):
        if session_secs < anchor.session_start:
            break
        # 这次连接里走了多远；不超过下一次连接在流里的起点（超过的部分是断线的空档）
        end = anchors[index + 1].stream_start if index + 1 < len(anchors) else float("inf")
        result = min(anchor.stream_start + (session_secs - anchor.session_start), end)
    return result


def resolve_alias(speaker: int, alias: Mapping[int, int] | None) -> int:
    """合并说话人之后的别名（可能连着合并了几次：3 → 2 → 1）。"""
    if not alias:
        return speaker
    seen = {speaker}
    while speaker in alias:
        speaker = alias[speaker]
        if speaker in seen:
            break
        seen.add(speaker)
    return speaker


def map_segments(
    anchors: Sequence[Anchor],
    segments: Sequence[SpeakerSegment],
    *,
    speaker_offset: int = 0,
    alias: Mapping[int, int] | None = None,
) -> list[SpeakerSegment]:
    """把流内时间的分段换算成会话时间。跨过接续点的分段在接续点切开（两边隔着断线的空档）。

    说话人编号先加偏移、再套别名。结果按开始时间排序。
    """
    out: list[SpeakerSegment] = []
    for segment in segments:
        speaker = resolve_alias(segment.speaker + speaker_offset, alias)
        if not anchors:
            out.append(SpeakerSegment(segment.start_secs, segment.end_secs, speaker))
            continue
        for index, anchor in enumerate(anchors):
            end = anchors[index + 1].stream_start if index + 1 < len(anchors) else float("inf")
            # 第一个接续点之前不该有音频；万一有，也算在第一次连接里
            start = anchor.stream_start if index else float("-inf")
            piece_start = max(segment.start_secs, start)
            piece_end = min(segment.end_secs, end)
            if piece_end <= piece_start:
                continue
            shift = anchor.session_start - anchor.stream_start
            out.append(SpeakerSegment(piece_start + shift, piece_end + shift, speaker))
    out.sort(key=lambda s: (s.start_secs, s.end_secs))
    return out


class SessionDiarizer:
    """包在真正的说话人区分外面，实现同一个 ``Diarizer`` 协议；``close()`` 不关底层的流。

    由会话管理器持有（``SessionManager.diarizer_for``）；换到别的会议、结束会议、应用退出时才 ``shutdown()``。
    """

    def __init__(self, inner: Diarizer, *, session_id: str, speaker_offset: int = 0) -> None:
        self._inner = inner
        self.session_id = session_id
        self.speaker_offset = speaker_offset
        self.failed = False  # 底层出过错：下次连接要重开一条流
        self._fed_bytes = 0
        self._anchors: list[Anchor] = []
        self._alias: dict[int, int] = {}

    @property
    def max_speakers(self) -> int:
        return self._inner.max_speakers

    @property
    def anchors(self) -> list[Anchor]:
        return list(self._anchors)

    @property
    def fed_secs(self) -> float:
        return self._fed_bytes / _BYTES_PER_SEC

    def begin_connection(self, base_secs: float) -> None:
        """新的一次连接从会话时间 ``base_secs`` 开始；之后喂进来的音频接在流的末尾。"""
        anchor = Anchor(self.fed_secs, base_secs)
        if self._anchors and self._anchors[-1].stream_start == anchor.stream_start:
            self._anchors[-1] = anchor  # 上一次连接一点音频都没喂进来
        else:
            self._anchors.append(anchor)

    def merge(self, src: int, dst: int) -> None:
        """合并说话人之后：流里再输出 ``src``，一律当成 ``dst``。"""
        if src != dst:
            self._alias[src] = dst

    async def start(self) -> None:
        return None  # 底层的流在交给我们之前已经启动

    async def push_audio(self, pcm16: bytes) -> None:
        # 先记账：底层调用在工作线程里，连接被取消时它仍会跑完，这段音频确实进了流
        self._fed_bytes += len(pcm16)
        try:
            await self._inner.push_audio(pcm16)
        except Exception:
            self.failed = True
            raise

    async def segments(self, since_secs: float = 0.0) -> list[SpeakerSegment]:
        found = await self._inner.segments(session_to_stream(self._anchors, since_secs))
        mapped = map_segments(
            self._anchors, found, speaker_offset=self.speaker_offset, alias=self._alias
        )
        return [s for s in mapped if s.end_secs > since_secs]

    async def close(self) -> None:
        return None  # 连接结束不关流；真正的释放在 shutdown()

    async def shutdown(self) -> None:
        await self._inner.close()
