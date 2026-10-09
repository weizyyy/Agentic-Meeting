"""文字归属与发言切分（docs/interfaces.md §4.3）。

``TranscriptAssembler`` 是**纯逻辑**：不做 IO、不依赖 Pipecat。会议记录器把识别增量、说话人分段、
助理说话时段喂给它，它返回要做的事——更新字幕、一条发言定稿。

规则（对应 §4.3 的编号）：

1. **时间区间**：每个增量的区间是 ``[上一个增量的终点, 本增量的终点]``，整体向前平移一个识别延迟估计
   （定稿文字落后于音频，默认 0.2 秒）；段内第一个增量的起点取「开始说话」的时刻。
   区间只会向前，永远不倒退。
2. **归属**：区间内统计每个说话人累计的发声时长，取最长者（平手取编号小的）；区间里没有任何分段，就沿用本段上一个
   增量的说话人。**未知从不触发切分**：当前发言的说话人还是未知时，第一个有结论的说话人直接补上，不切开。
3. **切分**：段落收尾时结束当前发言；段内换了说话人、且新说话人**连续**说满 ``min_switch_secs``（0.8 秒）时，在他开口的
   那个增量处切开。没说满的那几个增量先挂在待定区里，说满了归新发言，没说满（又回到原来的人或换了第三个人）就并回原发言，
   防止说话人区分抖动造成碎片。
4. **助理说话时段**：与助理说话（从开始到结束后 ``bot_tail_secs``）重叠超过一半的增量直接丢弃，不进字幕、不进发言。
5. **事后更正**：``recheck`` 按整条发言的时间范围重新归属，变了就返回新编号。

6. **相邻片段并成一条**（``should_merge``，由会议记录器在落库时用）：语音检测按停顿切段，停顿阈值很短
   （要保证应答快），一句话中间换口气就会被切成几段。同一个人、间隔不超过 ``merge_gap_secs`` 的相邻片段并进上一条发言，
   直到那一条够长并且停在句末，或者到了字数上限。

字幕的 ``segment_id`` 标识「一行字幕」：每关闭一行（换人切开或段落收尾）加一，所以一次「开始说话 → 停止说话」内
可能有多个。收尾时若这一行没有任何定稿文字，发一条文字全空的 ``CaptionUpdate`` 让界面清掉灰色的临时字幕。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from agentic_meeting.types import SPEAKER_UNKNOWN, ASRDelta, SpeakerSegment

# 起始值，待用真人语音校准（docs/interfaces.md §4.3）。
RECOGNITION_LATENCY_SECS = 0.2
MIN_SWITCH_SECS = 0.8
BOT_TAIL_SECS = 0.3
BOT_OVERLAP_DROP = 0.5  # 重叠超过区间长度的这个比例就丢弃

_TIE = 1e-9

# 并片段（规则 6）的默认值；实际用的值来自配置的 [transcript] 段。
MERGE_GAP_SECS = 2.0
MERGE_SOFT_CHARS = 40
MERGE_MAX_CHARS = 200
# 有一方的说话人还没有结论时，只在停顿这么短（明显是一句话中间换气）时才并：
# 别人很快插了一句短话，说话人区分往往还没来得及给结论，不能因此并进前一个人的话里。
MERGE_UNKNOWN_GAP_SECS = 0.8
SENTENCE_END = "。！？!?…"


def join_fragments(first: str, second: str) -> str:
    """把同一个人相邻的两段接起来：中文之间不加空格，两边都是英文或数字时加一个。"""
    first, second = first.rstrip(), second.lstrip()
    if not first or not second:
        return first + second
    if first[-1].isascii() and first[-1].isalnum() and second[0].isascii() and second[0].isalnum():
        return f"{first} {second}"
    return first + second


def should_merge(
    *,
    prev_speaker: int,
    prev_text: str,
    prev_end: float,
    speaker: int,
    t_start: float,
    gap_secs: float = MERGE_GAP_SECS,
    soft_chars: int = MERGE_SOFT_CHARS,
    max_chars: int = MERGE_MAX_CHARS,
) -> bool:
    """新定稿的一段要不要并进上一条发言。

    * ``gap_secs`` 为 0：不并。
    * 两段是同一个人、间隔不超过 ``gap_secs``；有一方说话人未知时间隔要更短（``MERGE_UNKNOWN_GAP_SECS``）。
    * 上一条已经有 ``soft_chars`` 个字并且停在句末：它是一句完整的话了，新的一段另起一条。
    * 上一条已经到 ``max_chars``：不再往里并（一个人一直说下去也不会变成无限长的一条）。
    """
    if gap_secs <= 0:
        return False
    gap = t_start - prev_end
    unknown = prev_speaker == SPEAKER_UNKNOWN or speaker == SPEAKER_UNKNOWN
    if not unknown and prev_speaker != speaker:
        return False
    if gap > (min(gap_secs, MERGE_UNKNOWN_GAP_SECS) if unknown else gap_secs) + 1e-6:
        return False
    text = prev_text.rstrip()
    if len(text) >= max_chars:
        return False
    return not (len(text) >= soft_chars and text[-1:] in SENTENCE_END)


@dataclass(frozen=True, slots=True)
class CaptionUpdate:
    """一行字幕的当前内容；同一 ``segment_id`` 的后一条覆盖前一条。文字全空表示清掉这一行。"""

    segment_id: int
    speaker_idx: int
    t_start: float
    stable: str
    unstable: str


@dataclass(frozen=True, slots=True)
class UtteranceFinal:
    """一条发言定稿，可以落库了。"""

    segment_id: int
    speaker_idx: int
    t_start: float
    t_end: float
    text: str


Event = CaptionUpdate | UtteranceFinal


def dominant_speaker(segments: Sequence[SpeakerSegment], start: float, end: float) -> int | None:
    """``[start, end]`` 内累计发声最长的说话人；没有任何重叠返回 ``None``。平手取编号小的。

    ``end <= start``（零长度的区间）时，用覆盖那个时刻的分段。
    """
    totals: dict[int, float] = {}
    for s in segments:
        if end > start:
            overlap = min(end, s.end_secs) - max(start, s.start_secs)
            if overlap > 0:
                totals[s.speaker] = totals.get(s.speaker, 0.0) + overlap
        elif s.start_secs <= start < s.end_secs:
            totals[s.speaker] = totals.get(s.speaker, 0.0) + 1.0
    if not totals:
        return None
    best = max(totals.values())
    return min(speaker for speaker, total in totals.items() if total >= best - _TIE)


@dataclass(slots=True)
class _Line:
    segment_id: int
    speaker: int
    t_start: float
    t_end: float
    text: str = ""
    shown: bool = False  # 是否已经发过字幕（决定收尾时要不要发「清除」）


@dataclass(slots=True)
class _Pending:
    speaker: int
    parts: list[tuple[str, float, float]] = field(
        default_factory=list
    )  # (文字, 区间起点, 区间终点)
    secs: float = 0.0

    @property
    def text(self) -> str:
        return "".join(p[0] for p in self.parts)


class TranscriptAssembler:
    def __init__(
        self,
        *,
        recognition_latency_secs: float = RECOGNITION_LATENCY_SECS,
        min_switch_secs: float = MIN_SWITCH_SECS,
        bot_tail_secs: float = BOT_TAIL_SECS,
        first_segment_id: int = 1,
    ) -> None:
        self._latency = recognition_latency_secs
        self._min_switch = min_switch_secs
        self._bot_tail = bot_tail_secs
        self._next_id = first_segment_id
        self._speech_start: float | None = None
        self._queued_start: float | None = None  # 上一段还没收尾时就来了的「开始说话」
        self._prev_end: float | None = None
        self._current: _Line | None = None
        self._pending: _Pending | None = None
        self._last_speaker: int | None = None
        self._last_caption: CaptionUpdate | None = None
        self._bot_windows: list[list[float | None]] = []  # [开始, 结束或 None]

    # ------------------------------------------------------------------ #
    # 输入
    # ------------------------------------------------------------------ #

    def on_speech_started(self, t: float) -> list[Event]:
        """VAD 确认开始说话（已减去确认延迟）。"""
        if self._current is None:
            self._speech_start = t
        else:
            # 系统帧会插队：下一次「开始说话」可能先于上一段的收尾增量到达，记下来等上一段关闭后再用。
            self._queued_start = t
        return []

    def on_bot_speaking(self, start_t: float, end_t: float | None) -> list[Event]:
        """助理开始说话（``end_t`` 为 ``None``）或说完（带上同一个 ``start_t`` 补上结尾）。"""
        if end_t is not None and self._bot_windows and self._bot_windows[-1][0] == start_t:
            self._bot_windows[-1][1] = end_t
        else:
            self._bot_windows.append([start_t, end_t])
        return []

    def on_delta(self, delta: ASRDelta, segments: Sequence[SpeakerSegment]) -> list[Event]:
        events: list[Event] = []
        start, end = self._interval(delta)
        self._prev_end = end

        if self._overlaps_bot(start, end):
            if delta.segment_end:
                self._close(events, None)  # 回声不算发言的一部分，不延长它的结束时间
            return events

        who = dominant_speaker(segments, start, end)
        line = self._current
        if line is None:
            line = self._current = _Line(
                self._next_id, who if who is not None else SPEAKER_UNKNOWN, start, end
            )

        text = delta.stable_text
        if text.strip():
            self._place(events, line, text, who, start, end)
        elif text:  # 只有空白：跟着最近的去处，不参与归属
            if self._pending is not None:
                self._pending.parts.append((text, start, end))
            else:
                line.text += text
        line = self._current  # 切开之后 current 已经是新的一行
        assert line is not None

        if delta.segment_end:
            self._caption(events, line, "")  # 先把定稿后的字幕推一次，再收尾
            self._close(events, end)
        else:
            self._caption(events, line, delta.unstable_text)
        return events

    def recheck(self, utterance: UtteranceFinal, segments: Sequence[SpeakerSegment]) -> int | None:
        """按整条发言的时间范围重新归属；说话人变了返回新编号，没变或没有任何分段返回 ``None``。"""
        who = dominant_speaker(segments, utterance.t_start, utterance.t_end)
        if who is None or who == utterance.speaker_idx:
            return None
        return who

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _interval(self, delta: ASRDelta) -> tuple[float, float]:
        end = delta.audio_end_secs - self._latency
        if self._current is None and self._speech_start is not None:
            start = self._speech_start
        elif self._prev_end is not None:
            start = self._prev_end
        else:
            start = max(0.0, end)
        return start, max(start, end)

    def _overlaps_bot(self, start: float, end: float) -> bool:
        length = end - start
        for window_start, window_end in self._bot_windows:
            stop = float("inf") if window_end is None else window_end + self._bot_tail
            if length > 0:
                overlap = min(end, stop) - max(start, window_start)
                if overlap > BOT_OVERLAP_DROP * length:
                    return True
            elif window_start <= start < stop:
                return True
        return False

    def _place(
        self,
        events: list[Event],
        line: _Line,
        text: str,
        who: int | None,
        start: float,
        end: float,
    ) -> None:
        """把一个有字的增量放进当前发言或待定区，必要时切开。"""
        speaker = who if who is not None else self._last_speaker
        if speaker is None:
            speaker = SPEAKER_UNKNOWN
        self._last_speaker = speaker

        if line.speaker == SPEAKER_UNKNOWN and speaker != SPEAKER_UNKNOWN:
            line.speaker = speaker  # 第一个有结论的说话人补上，不切开
        if speaker == SPEAKER_UNKNOWN or speaker == line.speaker:
            self._merge_pending(line)  # 待定区里的是抖动，并回原发言
            line.text += text
            line.t_end = max(line.t_end, end)
            return

        # 另一个人在说话：先挂在待定区
        if self._pending is not None and self._pending.speaker != speaker:
            self._merge_pending(line)
        if self._pending is None:
            self._pending = _Pending(speaker)
        self._pending.parts.append((text, start, end))
        self._pending.secs += end - start
        if self._pending.secs >= self._min_switch:
            self._split(events, line)

    def _merge_pending(self, line: _Line) -> None:
        if self._pending is None:
            return
        line.text += self._pending.text
        line.t_end = max(line.t_end, self._pending.parts[-1][2])
        self._pending = None

    def _split(self, events: list[Event], line: _Line) -> None:
        pending = self._pending
        assert pending is not None
        self._pending = None
        self._finish_line(events, line)
        self._current = _Line(
            self._next_id,
            pending.speaker,
            pending.parts[0][1],
            pending.parts[-1][2],
            text=pending.text,
        )

    def _finish_line(self, events: list[Event], line: _Line) -> None:
        """关闭一行：有字就发定稿，没字但显示过就发清除；然后 id 加一。"""
        text = line.text.strip()
        if text:
            events.append(
                UtteranceFinal(line.segment_id, line.speaker, line.t_start, line.t_end, text)
            )
        elif line.shown:
            events.append(CaptionUpdate(line.segment_id, line.speaker, line.t_start, "", ""))
        self._last_caption = None
        self._next_id = line.segment_id + 1

    def _close(self, events: list[Event], end: float | None) -> None:
        """段落收尾：待定区并回当前发言，关闭它，等下一次开始说话。"""
        line = self._current
        if line is not None:
            self._merge_pending(line)
            if end is not None:
                line.t_end = max(line.t_end, end)
            self._finish_line(events, line)
        self._current = None
        self._pending = None
        self._last_speaker = None
        self._speech_start, self._queued_start = self._queued_start, None

    def _caption(self, events: list[Event], line: _Line, unstable: str) -> None:
        stable = line.text + (self._pending.text if self._pending else "")
        if not (stable or unstable):
            return
        update = CaptionUpdate(line.segment_id, line.speaker, line.t_start, stable, unstable)
        if update != self._last_caption:
            line.shown = True
            events.append(update)
            self._last_caption = update
