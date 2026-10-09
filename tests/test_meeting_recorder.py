"""会议记录器（pipeline/recorder.py）的完整行为：说话人归属、发言落库、上下文追加、
助理说话时段、事后更正、容错与收尾。

最基本的行为（转录帧 → 字幕消息、放行所有帧）在 test_recorder.py，不带存储和说话人区分时仍然成立。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import numpy as np
import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    DataFrame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    LLMMessagesAppendFrame,
    SystemFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame
from pipecat.tests.utils import SleepFrame, run_test

from agentic_meeting.diar.base import NullDiarizer
from agentic_meeting.pipeline.clock import SessionClock
from agentic_meeting.pipeline.recorder import MeetingRecorder
from agentic_meeting.store.db import default_speaker_name
from agentic_meeting.types import (
    SPEAKER_ASSISTANT,
    SPEAKER_TYPED,
    SPEAKER_UNKNOWN,
    ASRDelta,
    SpeakerSegment,
    Utterance,
)

# 音频、VAD 事件是系统帧，转录是数据帧：Pipecat 让系统帧插队，所以步骤之间要留出间隔。
GAP = SleepFrame(sleep=0.03)


def audio(ms: int = 100) -> InputAudioRawFrame:
    data = np.zeros(16 * ms, dtype="<i2").tobytes()
    return InputAudioRawFrame(audio=data, sample_rate=16000, num_channels=1)


def interim(text: str, delta: ASRDelta) -> InterimTranscriptionFrame:
    frame = InterimTranscriptionFrame(text=text, user_id="", timestamp="t", result=delta)
    frame.includes_inter_frame_spaces = True
    return frame


def final(text: str, delta: ASRDelta) -> TranscriptionFrame:
    frame = TranscriptionFrame(
        text=text, user_id="", timestamp="t", result=delta, finalized=delta.segment_end
    )
    frame.includes_inter_frame_spaces = True
    return frame


def seg(start: float, end: float, speaker: int) -> SpeakerSegment:
    return SpeakerSegment(start, end, speaker)


# --------------------------------------------------------------------------- #
# 替身与小工具
# --------------------------------------------------------------------------- #


class FakeStore:
    """假的存储：记录写入；可以让前几次写入失败。"""

    def __init__(self, *, fail_adds: int = 0):
        self.utterances: list[Utterance] = []
        self.updates: list[tuple[int, int]] = []
        self.fail_adds = fail_adds
        self.names: dict[int, str] = {}

    async def add_utterance(self, utterance: Utterance) -> int:
        if self.fail_adds > 0:
            self.fail_adds -= 1
            raise RuntimeError("磁盘满了")
        utterance.id = len(self.utterances) + 1
        self.utterances.append(utterance)
        return utterance.id

    async def speaker_name(self, session_id: str, idx: int) -> str:
        return self.names.get(idx, default_speaker_name(idx, "Nova"))

    async def update_utterance_speaker(self, utterance_id: int, speaker_idx: int) -> bool:
        self.updates.append((utterance_id, speaker_idx))
        for u in self.utterances:
            if u.id == utterance_id:
                u.speaker_idx = speaker_idx
                return True
        return False


class FakeDiarizer:
    """假的说话人区分：记录送入的音频，按脚本返回分段（每次 segments() 取下一项，最后一项重复）。"""

    max_speakers = 4

    def __init__(self, script=None, *, push_error=None, segments_error=None):
        self.script = script or [[]]
        self.pushed: list[bytes] = []
        self.segment_calls: list[float] = []
        self.push_error = push_error
        self.segments_error = segments_error

    async def start(self) -> None: ...

    async def push_audio(self, pcm16: bytes) -> None:
        if self.push_error:
            raise self.push_error
        self.pushed.append(pcm16)

    async def segments(self, since_secs: float = 0.0) -> list[SpeakerSegment]:
        self.segment_calls.append(since_secs)
        if self.segments_error:
            raise self.segments_error
        step = self.script[min(len(self.segment_calls) - 1, len(self.script) - 1)]
        return [s for s in step if s.end_secs > since_secs]

    async def close(self) -> None: ...


@dataclass
class BotSignal(DataFrame):
    """让 Injector 向上游推一个「助理开始 / 停止说话」帧。"""

    started: bool = True


class Injector(FrameProcessor):
    """放在记录器后面：收到 BotSignal 就向上游推助理说话的起止帧（真实运行时它们来自传输输出）。"""

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotSignal):
            cls = BotStartedSpeakingFrame if frame.started else BotStoppedSpeakingFrame
            await self.push_frame(cls(), FrameDirection.UPSTREAM)
            return
        await self.push_frame(frame, direction)


@dataclass
class CallSignal(DataFrame):
    """让 Hook 在管线里（记录器已经启动之后）执行一段代码。"""

    fn: Callable[[], Awaitable[object]]


class Hook(FrameProcessor):
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, CallSignal):
            await frame.fn()
            return
        await self.push_frame(frame, direction)


def messages(frames, kind: str) -> list[dict]:
    return [
        f.data
        for f in frames
        if isinstance(f, RTVIServerMessageFrame) and f.data.get("type") == kind
    ]


def appended(frames) -> list[LLMMessagesAppendFrame]:
    return [f for f in frames if isinstance(f, LLMMessagesAppendFrame)]


def data_order(frames) -> list[str]:
    """数据帧的类型顺序（系统帧与数据帧各排各的队，只比较数据帧）。"""
    return [
        type(f).__name__ for f in frames if not isinstance(f, (SystemFrame, RTVIServerMessageFrame))
    ]


async def run(recorder, frames, *, tail: float = 0.05, wrap=None):
    processor = wrap(recorder) if wrap else recorder
    down, _ = await run_test(processor, frames_to_send=[*frames, SleepFrame(sleep=tail)])
    return down


def one_utterance_frames(text="你好", end=0.8, lead=3) -> list:
    """lead 块 0.1 秒的音频 → VAD 开始说话 → 一个收尾的增量（临时转录帧 + 转录帧）。"""
    d = ASRDelta(text, "", end, segment_end=True)
    return [
        *[audio(100)] * lead,
        VADUserStartedSpeakingFrame(start_secs=0.0),
        GAP,
        interim(text, d),
        final(text, d),
    ]


# --------------------------------------------------------------------------- #
# 落库、消息、上下文
# --------------------------------------------------------------------------- #


async def test_a_finalized_segment_is_stored_announced_and_appended_to_the_context():
    store = FakeStore()
    rec = MeetingRecorder(store=store, session_id="s1", assistant_name="Nova")
    down = await run(rec, one_utterance_frames())

    (saved,) = store.utterances
    assert (saved.session_id, saved.speaker_idx, saved.text, saved.source) == (
        "s1",
        SPEAKER_UNKNOWN,
        "你好",
        "asr",
    )
    assert saved.t_start == pytest.approx(0.3) and saved.t_end == pytest.approx(0.6)
    (message,) = messages(down, "utterance")
    assert message == {
        "type": "utterance",
        "id": 1,
        "segment_id": 1,
        "speaker_idx": 0,
        "speaker_name": "未知",
        "t_start": pytest.approx(0.3),
        "t_end": pytest.approx(0.6),
        "text": "你好",
        "source": "asr",
    }
    (frame,) = appended(down)
    assert frame.messages == [{"role": "user", "content": "[00:00:00 未知] 你好"}]
    assert frame.run_llm is False


async def test_the_context_line_is_appended_before_the_finalized_transcription_frame():
    rec = MeetingRecorder(store=FakeStore(), session_id="s1")
    down = await run(rec, one_utterance_frames())
    assert data_order(down) == [
        "InterimTranscriptionFrame",
        "LLMMessagesAppendFrame",
        "TranscriptionFrame",
    ]


async def test_a_finalized_transcription_without_a_preceding_interim_is_handled_before_it_passes():
    store = FakeStore()
    rec = MeetingRecorder(store=store, session_id="s1")
    d = ASRDelta("直接来的", "", 0.5, segment_end=True)
    frames = [VADUserStartedSpeakingFrame(start_secs=0.0), GAP, final("直接来的", d)]
    down = await run(rec, frames)
    assert [u.text for u in store.utterances] == ["直接来的"]
    assert data_order(down) == ["LLMMessagesAppendFrame", "TranscriptionFrame"]


async def test_a_recorder_without_store_or_diarizer_still_works():
    down = await run(MeetingRecorder(), one_utterance_frames())
    assert len(messages(down, "caption")) == 1
    assert messages(down, "utterance")[0]["id"] is None  # 没有存储：消息照发，只是没有编号


async def test_speaker_names_come_from_the_store():
    store = FakeStore()
    store.names[2] = "王老师"
    diar = FakeDiarizer([[seg(0, 50, 2)]])
    rec = MeetingRecorder(store=store, session_id="s1", diarizer=diar)
    down = await run(rec, one_utterance_frames())
    assert messages(down, "utterance")[0]["speaker_name"] == "王老师"
    assert appended(down)[0].messages[0]["content"] == "[00:00:00 王老师] 你好"
    assert messages(down, "caption")[0]["speaker_name"] == "王老师"


async def test_a_rename_takes_effect_immediately():
    rec = MeetingRecorder(
        store=FakeStore(), session_id="s1", diarizer=FakeDiarizer([[seg(0, 50, 2)]])
    )
    rec.set_speaker_name(2, "李同学")  # 界面改名后立即生效，不必等缓存过期
    down = await run(rec, one_utterance_frames())
    assert messages(down, "utterance")[0]["speaker_name"] == "李同学"


# --------------------------------------------------------------------------- #
# 说话人区分
# --------------------------------------------------------------------------- #


async def test_utterances_are_attributed_with_the_diarizers_recent_segments():
    diar = FakeDiarizer([[seg(0, 100, 3)]])
    store = FakeStore()
    rec = MeetingRecorder(store=store, session_id="s1", diarizer=diar)
    await run(rec, one_utterance_frames())
    assert [u.speaker_idx for u in store.utterances] == [3]
    assert diar.segment_calls and all(since >= 0.0 for since in diar.segment_calls)


async def test_all_audio_reaches_the_diarizer_in_order_in_batches():
    diar = FakeDiarizer()
    rec = MeetingRecorder(diarizer=diar, diar_batch_ms=160)
    blocks = [
        InputAudioRawFrame(audio=bytes([i + 1]) * 3200, sample_rate=16000, num_channels=1)
        for i in range(23)
    ]
    await run(rec, blocks)  # 每块 0.1 秒；结束帧到来时还剩没凑满一批的音频
    assert b"".join(diar.pushed) == b"".join(f.audio for f in blocks)
    assert len(diar.pushed) < len(blocks)  # 攒批了，不是一帧一发


@pytest.mark.parametrize("diarizer", [None, NullDiarizer()])
async def test_no_audio_worker_without_a_real_diarizer(diarizer):
    rec = MeetingRecorder(diarizer=diarizer)
    down = await run(rec, [audio(100)] * 3)
    assert rec._audio_task is None and rec._audio_queue is None
    assert len([f for f in down if isinstance(f, InputAudioRawFrame)]) == 3


async def test_a_failing_diarizer_degrades_to_unknown_and_tells_the_user_once():
    diar = FakeDiarizer([[seg(0, 100, 3)]], push_error=RuntimeError("显存不足"))
    store = FakeStore()
    rec = MeetingRecorder(store=store, session_id="s1", diarizer=diar, diar_batch_ms=100)
    d = ASRDelta("还在转录", "", 1.2, segment_end=True)
    frames = [audio(100)] * 8 + [GAP, VADUserStartedSpeakingFrame(start_secs=0.0), GAP]
    frames += [interim("还在转录", d), final("还在转录", d)]
    down = await run(rec, frames, tail=0.2)
    warns = [m for m in messages(down, "notice") if m["level"] == "warn"]
    assert len(warns) == 1 and "显存不足" in warns[0]["text"] and "未知" in warns[0]["text"]
    assert [u.text for u in store.utterances] == ["还在转录"]  # 转录没停
    assert store.utterances[0].speaker_idx == SPEAKER_UNKNOWN  # 说话人区分坏了之后不再采信它
    assert len([f for f in down if isinstance(f, InputAudioRawFrame)]) == 8  # 音频照常放行


async def test_a_diarizer_that_cannot_list_segments_leaves_the_speaker_unknown():
    store = FakeStore()
    diar = FakeDiarizer(segments_error=RuntimeError("读不出来"))
    rec = MeetingRecorder(store=store, session_id="s1", diarizer=diar)
    down = await run(rec, one_utterance_frames())
    assert [(u.text, u.speaker_idx) for u in store.utterances] == [("你好", SPEAKER_UNKNOWN)]
    assert len(appended(down)) == 1


# --------------------------------------------------------------------------- #
# 助理说话时段
# --------------------------------------------------------------------------- #


async def test_echo_during_the_assistants_speech_is_neither_stored_nor_added_to_the_context():
    store = FakeStore()
    rec = MeetingRecorder(store=store, session_id="s1")
    echo = ASRDelta("助理的回声", "", 2.0, segment_end=True)
    frames = [audio(100)] * 10 + [GAP, BotSignal(True), GAP]  # 助理从 1.0 秒开始说话
    frames += [VADUserStartedSpeakingFrame(start_secs=0.0), GAP]
    frames += [interim("助理的回声", echo), final("助理的回声", echo)]
    down = await run(rec, frames, wrap=lambda r: Pipeline([r, Injector()]))
    assert store.utterances == []
    assert appended(down) == [] and messages(down, "utterance") == []


async def test_listeners_hear_when_the_assistant_starts_and_stops_speaking():
    rec = MeetingRecorder()
    heard: list[bool] = []

    async def listener(speaking: bool) -> None:
        heard.append(speaking)

    rec.on_bot_speaking_changed = listener
    frames = [
        BotSignal(True),
        GAP,
        BotSignal(False),
        GAP,
        BotSignal(False),
    ]  # 多余的「停止」不重复通知
    await run(rec, frames, wrap=lambda r: Pipeline([r, Injector()]))
    assert heard == [True, False]


async def test_a_failing_speaking_listener_does_not_disturb_the_recorder():
    rec = MeetingRecorder(store=FakeStore(), session_id="s1")

    async def broken(speaking: bool) -> None:
        raise RuntimeError("监听者出错")

    rec.on_bot_speaking_changed = broken
    frames = [BotSignal(True), GAP, BotSignal(False), GAP, *one_utterance_frames(lead=3)]
    down = await run(rec, frames, wrap=lambda r: Pipeline([r, Injector()]))
    assert len(appended(down)) == 1  # 记录器照常工作


async def test_speech_after_the_assistant_finished_is_recorded_again():
    store = FakeStore()
    rec = MeetingRecorder(store=store, session_id="s1")
    echo = ASRDelta("回声", "", 2.0, segment_end=True)
    later = ASRDelta("之后的真人发言", "", 6.0, segment_end=True)
    frames = [audio(100)] * 10 + [GAP, BotSignal(True), GAP]
    frames += [VADUserStartedSpeakingFrame(start_secs=0.0), GAP]
    frames += [interim("回声", echo), final("回声", echo), GAP]
    frames += [audio(100)] * 20 + [GAP, BotSignal(False), GAP]  # 3.0 秒时助理说完
    frames += [audio(100)] * 20 + [GAP, VADUserStartedSpeakingFrame(start_secs=0.0), GAP]
    frames += [interim("之后的真人发言", later), final("之后的真人发言", later)]
    await run(rec, frames, wrap=lambda r: Pipeline([r, Injector()]))
    assert [u.text for u in store.utterances] == ["之后的真人发言"]


# --------------------------------------------------------------------------- #
# 容错
# --------------------------------------------------------------------------- #


async def test_a_failing_store_never_stops_the_transcript_and_the_utterance_is_retried_in_order():
    store = FakeStore(fail_adds=1)
    rec = MeetingRecorder(store=store, session_id="s1")
    first = ASRDelta("第一句", "", 0.8, segment_end=True)
    second = ASRDelta("第二句", "", 1.8, segment_end=True)
    frames = [audio(100)] * 2 + [VADUserStartedSpeakingFrame(start_secs=0.0), GAP]
    frames += [interim("第一句", first), final("第一句", first), GAP]
    frames += [audio(100)] * 8 + [VADUserStartedSpeakingFrame(start_secs=0.0), GAP]
    frames += [interim("第二句", second), final("第二句", second)]
    down = await run(rec, frames)

    assert [u.text for u in store.utterances] == ["第一句", "第二句"]  # 第一句补写在前，顺序不乱
    assert [u.id for u in store.utterances] == [1, 2]
    assert [m["id"] for m in messages(down, "utterance")] == [
        None,
        2,
    ]  # 第一次没写成，消息里没有编号
    assert len(appended(down)) == 2  # 上下文照常追加
    assert len([f for f in down if isinstance(f, TranscriptionFrame)]) == 2  # 转录帧一个没少


async def test_unsaved_utterances_are_flushed_when_the_pipeline_ends():
    store = FakeStore(fail_adds=1)
    rec = MeetingRecorder(store=store, session_id="s1")
    await run(rec, one_utterance_frames())  # 唯一一次写入失败，之后没有别的发言触发补写
    assert [u.text for u in store.utterances] == ["你好"]  # 结束帧到来时补写


async def test_still_failing_at_the_end_does_not_raise():
    store = FakeStore(fail_adds=99)
    rec = MeetingRecorder(store=store, session_id="s1")
    down = await run(rec, one_utterance_frames())
    assert store.utterances == [] and len(appended(down)) == 1


# --------------------------------------------------------------------------- #
# 事后更正
# --------------------------------------------------------------------------- #


async def test_speaker_is_rechecked_and_corrected_when_the_diarizer_catches_up():
    store = FakeStore()
    # 定稿时那次查询还没有结论，之后的核对里说话人区分跟上了
    diar = FakeDiarizer([[], [seg(0, 100, 3)]])
    rec = MeetingRecorder(
        store=store, session_id="s1", diarizer=diar, recheck_interval_secs=0.03, recheck_attempts=4
    )
    down = await run(rec, one_utterance_frames(), tail=0.4)
    assert store.updates == [(1, 3)]  # 只更正一次，之后的核对结果不变
    (update,) = messages(down, "utterance_update")
    assert update == {
        "type": "utterance_update",
        "id": 1,
        "speaker_idx": 3,
        "speaker_name": "说话人 3",
    }
    assert store.utterances[0].speaker_idx == 3
    assert len(diar.segment_calls) == 1 + 4  # 定稿时一次 + 核对 4 次


async def test_recheck_stops_after_the_configured_attempts():
    diar = FakeDiarizer([[]])
    rec = MeetingRecorder(
        store=FakeStore(),
        session_id="s1",
        diarizer=diar,
        recheck_interval_secs=0.01,
        recheck_attempts=3,
    )
    await run(rec, one_utterance_frames(), tail=0.3)
    assert len(diar.segment_calls) == 1 + 3


async def test_pending_rechecks_are_cancelled_when_the_pipeline_ends():
    diar = FakeDiarizer([[]])
    rec = MeetingRecorder(
        store=FakeStore(),
        session_id="s1",
        diarizer=diar,
        recheck_interval_secs=5.0,
        recheck_attempts=5,
    )
    await run(rec, one_utterance_frames(), tail=0.05)
    assert len(diar.segment_calls) == 1  # 没有等 5 秒，也没有留下悬着的任务
    assert not rec._tasks


# --------------------------------------------------------------------------- #
# 助理的话、键入的文字
# --------------------------------------------------------------------------- #


async def test_the_assistants_turn_is_stored_as_an_assistant_utterance():
    store = FakeStore()
    rec = MeetingRecorder(store=store, session_id="s1", assistant_name="Nova")

    async def started():
        rec.assistant_turn_started()

    frames = [
        CallSignal(started),
        GAP,  # 系统帧（音频）会插队，要先让这个信号处理完
        *[audio(100)] * 10,
        GAP,
        CallSignal(lambda: rec.record_assistant("  好的，我来看看。 ")),
        CallSignal(lambda: rec.record_assistant("   ")),  # 空的忽略
    ]
    down = await run(rec, frames, wrap=lambda r: Pipeline([Hook(), r]))
    (saved,) = store.utterances
    assert (saved.speaker_idx, saved.source, saved.text) == (
        SPEAKER_ASSISTANT,
        "assistant",
        "好的，我来看看。",
    )
    assert (saved.t_start, saved.t_end) == (0.0, pytest.approx(1.0))
    (message,) = messages(down, "utterance")
    assert (message["speaker_name"], message["segment_id"], message["source"]) == (
        "Nova",
        None,
        "assistant",
    )
    assert appended(down) == []  # 助理的话已经由助理侧聚合器写进上下文，不重复追加


async def test_typed_text_is_stored_as_a_text_utterance_addressed_to_the_assistant():
    store = FakeStore()
    rec = MeetingRecorder(store=store, session_id="s1")
    holder: dict = {}

    async def type_something():
        holder["u"] = await rec.record_typed("  帮我查一下论文  ")
        holder["line"] = await rec.context_text(holder["u"])
        holder["empty"] = await rec.record_typed("   ")

    frames = [*[audio(100)] * 12, GAP, CallSignal(type_something)]
    down = await run(rec, frames, wrap=lambda r: Pipeline([Hook(), r]))
    (saved,) = store.utterances
    assert (saved.speaker_idx, saved.source, saved.text, saved.addressed_to_assistant) == (
        SPEAKER_TYPED,
        "text",
        "帮我查一下论文",
        True,
    )
    assert holder["line"] == "[00:00:01 文字输入] 帮我查一下论文"
    assert holder["empty"] is None
    (message,) = messages(down, "utterance")
    assert (message["speaker_name"], message["source"]) == ("文字输入", "text")
    assert appended(down) == []  # 要不要追加、要不要触发模型，由调用方决定


# --------------------------------------------------------------------------- #
# 会话时钟
# --------------------------------------------------------------------------- #


async def test_elapsed_secs_follows_the_audio_on_the_session_timeline():
    rec = MeetingRecorder(clock=SessionClock(base_secs=10.0))
    assert rec.elapsed_secs == 10.0
    await run(rec, [audio(100)] * 7)
    assert rec.elapsed_secs == pytest.approx(10.7)


async def test_the_session_clock_base_offsets_the_speech_start():
    rec = MeetingRecorder(clock=SessionClock(base_secs=100.0))
    frames = [audio(100)] * 5 + [VADUserStartedSpeakingFrame(start_secs=0.2), GAP]
    frames += [interim("你", ASRDelta("", "你", 100.6))]
    down = await run(rec, frames)
    assert messages(down, "caption")[0]["t_start"] == pytest.approx(100.3)


# --------------------------------------------------------------------------- #
# 与真实的聚合器一起
# --------------------------------------------------------------------------- #


async def test_the_appended_line_lands_in_the_shared_context_exactly_once():
    context = LLMContext()
    pair = LLMContextAggregatorPair(context)
    rec = MeetingRecorder(store=FakeStore(), session_id="s1")
    pipeline = Pipeline([rec, pair.user(), pair.assistant()])
    await run_test(pipeline, frames_to_send=[*one_utterance_frames(), SleepFrame(sleep=0.1)])
    contents = [m["content"] for m in context.get_messages() if m["role"] == "user"]
    assert contents.count("[00:00:00 未知] 你好") == 1  # 用户侧消费了追加帧，助理侧不会再加一遍


# --------------------------------------------------------------------------- #
# 相邻片段并成一条（diar/fusion.py 规则 6）
# --------------------------------------------------------------------------- #


class MergingStore(FakeStore):
    """多一个 ``extend_utterance``：把文字并进已有的发言。"""

    def __init__(self, *, refuse: bool = False, **kw):
        super().__init__(**kw)
        self.extended: list[tuple[int, str, float]] = []
        self.refuse = refuse

    async def extend_utterance(self, utterance_id: int, *, text: str, t_end: float) -> bool:
        if self.refuse:
            return False
        self.extended.append((utterance_id, text, t_end))
        return True


def spoken(text: str, start_audio: float, end: float) -> list:
    """一段话：先把音频补到 ``start_audio`` 秒，开始说话，然后一个在 ``end`` 秒收尾的增量。"""
    d = ASRDelta(text, "", end, segment_end=True)
    return [
        *[audio(100)] * round(start_audio * 10),
        VADUserStartedSpeakingFrame(start_secs=0.0),
        GAP,
        interim(text, d),
        final(text, d),
        GAP,
    ]


async def test_fragments_split_by_short_pauses_become_one_record():
    store = MergingStore()
    rec = MeetingRecorder(store=store, session_id="s1", merge_gap_secs=2.0)
    frames = [
        *spoken("我们先看一下", 0.5, 1.4),  # 0.5 → 1.2 秒
        *spoken("消融实验的", 1.2, 2.9),  # 累计音频 1.7 秒处开始：停了 0.5 秒
        *spoken(
            "结果", 0.6, 3.6
        ),  # 累计 2.3 秒处开始（比上一段的终点还早一点：语音检测的边界就是这么不整齐）
    ]
    down = await run(rec, frames)

    assert len(store.utterances) == 1  # 只落了一条，后面两段是并进去的
    assert [(i, t) for i, t, _ in store.extended] == [
        (1, "我们先看一下消融实验的"),
        (1, "我们先看一下消融实验的结果"),
    ]
    sent = messages(down, "utterance")
    assert [(m["id"], m["segment_id"], m["text"]) for m in sent] == [
        (1, 1, "我们先看一下"),
        (1, 2, "我们先看一下消融实验的"),  # 同一条发言，带着新片段那行字幕的编号
        (1, 3, "我们先看一下消融实验的结果"),
    ]
    assert sent[-1]["t_start"] == pytest.approx(0.5) and sent[-1]["t_end"] == pytest.approx(3.4)
    # 实时模型的上下文里每个片段各一行，内容不缺、不重复
    assert [f.messages[0]["content"] for f in appended(down)] == [
        "[00:00:00 未知] 我们先看一下",
        "[00:00:01 未知] 消融实验的",
        "[00:00:02 未知] 结果",
    ]


async def test_a_long_pause_or_another_voice_in_between_starts_a_new_record():
    store = MergingStore()
    rec = MeetingRecorder(store=store, session_id="s1", merge_gap_secs=2.0)
    frames = [
        *spoken("第一句", 0.5, 1.4),
        *spoken("隔了很久才说的第二句", 4.0, 6.0),  # 停了三秒多
    ]
    await run(rec, frames)
    assert [u.text for u in store.utterances] == ["第一句", "隔了很久才说的第二句"]
    assert store.extended == []

    # 中间助理说了话（或有人打了字）：不并
    store = MergingStore()
    rec = MeetingRecorder(store=store, session_id="s1", merge_gap_secs=2.0)

    async def assistant_speaks():
        await rec.record_assistant("我在")

    frames = [
        *spoken("Nova", 0.5, 1.4),
        CallSignal(assistant_speaks),
        *spoken("帮我查一下", 0.3, 2.2),
    ]
    await run(rec, frames, wrap=lambda r: Pipeline([r, Hook()]))
    assert [u.text for u in store.utterances] == ["Nova", "我在", "帮我查一下"]
    assert store.extended == []


async def test_merging_is_off_by_default_and_falls_back_when_the_store_refuses():
    store = MergingStore()
    await run(
        MeetingRecorder(store=store, session_id="s1"),
        [*spoken("第一段", 0.5, 1.4), *spoken("第二段", 0.3, 2.2)],
    )
    assert [u.text for u in store.utterances] == ["第一段", "第二段"] and store.extended == []

    # 存储不让并（那条发言已经进了纪要）：照常另存一条，什么都不丢
    store = MergingStore(refuse=True)
    down = await run(
        MeetingRecorder(store=store, session_id="s1", merge_gap_secs=2.0),
        [*spoken("第一段", 0.5, 1.4), *spoken("第二段", 0.3, 2.2)],
    )
    assert [u.text for u in store.utterances] == ["第一段", "第二段"]
    assert [m["id"] for m in messages(down, "utterance")] == [1, 2]

    # 存储没有这个能力（旧的假存储）：同样照常
    plain = FakeStore()
    await run(
        MeetingRecorder(store=plain, session_id="s1", merge_gap_secs=2.0),
        [*spoken("第一段", 0.5, 1.4), *spoken("第二段", 0.3, 2.2)],
    )
    assert [u.text for u in plain.utterances] == ["第一段", "第二段"]


async def test_a_merged_record_takes_the_speaker_once_one_fragment_knows_it():
    store = MergingStore()
    diar = FakeDiarizer([[], [seg(1.0, 3.0, 2)]])  # 第一段定稿时还没有结论，第二段时有了
    rec = MeetingRecorder(
        store=store, session_id="s1", diarizer=diar, merge_gap_secs=2.0, recheck_attempts=0
    )
    down = await run(rec, [*spoken("嗯", 0.5, 1.0), *spoken("我觉得可以", 0.8, 2.6)])
    assert store.extended[0][:2] == (1, "嗯我觉得可以")
    assert store.updates == [(1, 2)]  # 整条发言补上说话人
    last = messages(down, "utterance")[-1]
    assert (last["id"], last["speaker_idx"], last["speaker_name"]) == (1, 2, "说话人 2")


async def test_real_store_merges_fragments_into_one_row(tmp_path):
    from agentic_meeting.store.db import Store

    store = await Store.open(tmp_path / "m.db", 4, assistant_name="Nova")
    try:
        session = await store.create_session("会")
        rec = MeetingRecorder(store=store, session_id=session.id, merge_gap_secs=2.0)
        await run(rec, [*spoken("我们先看一下", 0.5, 1.4), *spoken("基线", 1.0, 2.6)])
        (row,) = await store.all_utterances(session.id)
        assert row.utterance.text == "我们先看一下基线"
        assert row.utterance.t_start == pytest.approx(0.5) and row.utterance.t_end == pytest.approx(
            2.4
        )
    finally:
        await store.close()
