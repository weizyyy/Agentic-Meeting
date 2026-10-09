"""会议记录器（pipeline/recorder.py）最基本的行为：把转录帧翻译成字幕消息，并放行所有帧。"""

from __future__ import annotations

import numpy as np
import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    SystemFrame,
    TextFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame
from pipecat.tests.utils import SleepFrame, run_test

from agentic_meeting.pipeline.recorder import MeetingRecorder
from agentic_meeting.types import SPEAKER_UNKNOWN, ASRDelta


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


def captions(frames) -> list[dict]:
    return [
        f.data
        for f in frames
        if isinstance(f, RTVIServerMessageFrame) and f.data.get("type") == "caption"
    ]


async def drive(frames: list):
    down, _ = await run_test(MeetingRecorder(), frames_to_send=[*frames, SleepFrame(sleep=0.05)])
    return down


async def test_interim_frames_become_captions_with_an_unknown_speaker():
    d1 = ASRDelta("你好", "吗", 0.5)
    d2 = ASRDelta("吗？", "", 0.8)
    down = await drive([audio(), interim("你好吗", d1), final("你好", d1), interim("你好吗？", d2)])

    first, second = captions(down)
    assert first["segment_id"] == second["segment_id"]  # 同一段里后一条覆盖前一条
    assert (first["stable"], first["unstable"]) == ("你好", "吗")
    assert (second["stable"], second["unstable"]) == ("你好吗？", "")
    for caption in (first, second):
        assert caption["speaker_idx"] == SPEAKER_UNKNOWN
        assert caption["speaker_name"] == "未知"


async def test_the_transcription_of_a_delta_that_was_already_shown_adds_no_duplicate():
    delta = ASRDelta("你好", "吗", 0.5)
    down = await drive([interim("你好吗", delta), final("你好", delta)])
    assert len(captions(down)) == 1


async def test_a_transcription_without_a_preceding_interim_still_produces_a_caption():
    down = await drive([final("你好", ASRDelta("你好", "", 0.5))])
    (caption,) = captions(down)
    assert (caption["stable"], caption["unstable"]) == ("你好", "")


async def test_segment_end_closes_the_segment_and_the_next_one_gets_a_new_id():
    end = ASRDelta("吗？", "", 0.8, segment_end=True)
    nxt = ASRDelta("", "再", 1.5)
    down = await drive(
        [
            interim("你好吗？", end),
            final("吗？", end),  # 同一个增量的转录帧，已经显示过了
            interim("再", nxt),
        ]
    )
    first, second = captions(down)
    assert second["segment_id"] == first["segment_id"] + 1
    assert (second["stable"], second["unstable"]) == ("", "再")  # 新的一段从头累计


async def test_segment_end_without_any_text_still_closes_the_segment():
    nothing = ASRDelta("", "", 0.8, segment_end=True)
    first_text = ASRDelta("你好", "", 0.5)
    next_text = ASRDelta("好", "", 1.0)
    down = await drive(
        [interim("你好", first_text), interim("你好", nothing), interim("好", next_text)]
    )
    # 收尾的增量没有新文字，字幕内容与上一条相同，不重复发；但那一段确实结束了
    ids = [c["segment_id"] for c in captions(down)]
    assert ids == [ids[0], ids[0] + 1]


# 音频、VAD 事件是系统帧，转录是数据帧：Pipecat 让系统帧插队，所以步骤之间要留出间隔，
# 否则后面的音频会先于前面的转录被处理（真实运行时转录紧跟在音频之后到达，不存在这个问题）。
GAP = SleepFrame(sleep=0.03)


async def test_t_start_is_the_speech_start_on_the_session_timeline():
    # 0.5 秒音频之后 VAD 报「开始说话」，它是在说话 0.2 秒后才确认的：这一段从 0.3 秒开始
    frames = [audio(100)] * 5 + [VADUserStartedSpeakingFrame(start_secs=0.2), GAP]
    frames += [interim("你", ASRDelta("", "你", 0.6))]
    down = await drive(frames)
    assert captions(down)[0]["t_start"] == pytest.approx(0.3)


async def test_a_later_segment_starts_at_its_own_vad_start():
    first = ASRDelta("你好", "", 0.4, segment_end=True)
    second = ASRDelta("", "再", 1.5)
    frames = [audio(100)] * 2 + [VADUserStartedSpeakingFrame(start_secs=0.0), GAP]
    frames += [interim("你好", first), GAP, VADUserStoppedSpeakingFrame()] + [audio(100)] * 8
    frames += [VADUserStartedSpeakingFrame(start_secs=0.2), GAP, interim("再", second)]
    down = await drive(frames)
    t_starts = [c["t_start"] for c in captions(down)]
    assert t_starts == [pytest.approx(0.2), pytest.approx(0.8)]  # 10 块 × 0.1 秒 − 0.2 秒确认时间


async def test_every_other_frame_passes_through_unchanged_and_in_order():
    delta = ASRDelta("你", "", 0.1)
    frames = [audio(), VADUserStartedSpeakingFrame(), interim("你", delta), final("你", delta)]
    frames += [TextFrame(text="别的帧"), VADUserStoppedSpeakingFrame(), audio()]
    down = await drive(frames)

    # 系统帧与数据帧在 Pipecat 里各排各的队，相互顺序不保证；各自内部的顺序必须不变。
    passed = [f for f in down if not isinstance(f, RTVIServerMessageFrame)]
    system = [type(f).__name__ for f in passed if isinstance(f, SystemFrame)]
    data = [type(f).__name__ for f in passed if not isinstance(f, SystemFrame)]
    assert system == [
        "InputAudioRawFrame",
        "VADUserStartedSpeakingFrame",
        "VADUserStoppedSpeakingFrame",
        "InputAudioRawFrame",
    ]
    assert data == ["InterimTranscriptionFrame", "TranscriptionFrame", "TextFrame"]
