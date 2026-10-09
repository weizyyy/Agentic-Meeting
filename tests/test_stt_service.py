"""识别服务（asr/stt_service.py）：把 StreamingASR 后端接进 Pipecat 管线。

用一个假的后端（记录收到的调用，增量由测试在指定时刻放进它的队列）和 Pipecat 自带的
``run_test`` 驱动。断言转录帧时只看转录类帧的先后顺序，不依赖它们与音频帧之间的相对位置。
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
from fakes import FakeASR
from pipecat.frames.frames import (
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    STTMuteFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.worker import PipelineParams
from pipecat.processors.frame_processor import FrameDirection
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame
from pipecat.tests.utils import SleepFrame, run_test

from agentic_meeting.asr.base import ASRBackendError
from agentic_meeting.asr.stt_service import StreamingASRService
from agentic_meeting.types import ASRDelta


def audio(value: int, ms: int = 100) -> InputAudioRawFrame:
    data = np.full(16 * ms, value, dtype="<i2").tobytes()
    return InputAudioRawFrame(audio=data, sample_rate=16000, num_channels=1)


def chunk(value: int, ms: int = 100) -> bytes:
    return audio(value, ms).audio


STARTED = VADUserStartedSpeakingFrame
STOPPED = VADUserStoppedSpeakingFrame
PAUSE = SleepFrame(sleep=0.05)


def d(stable: str, unstable: str = "", end: float = 0.0, segment_end: bool = False) -> ASRDelta:
    return ASRDelta(stable, unstable, end, segment_end)


async def drive(service: StreamingASRService, frames: list, *, tail: float = 0.1):
    """送入帧，等后台任务处理完，返回 (下游帧, 上游帧)。"""
    return await run_test(
        service,
        frames_to_send=[*frames, SleepFrame(sleep=tail)],
        pipeline_params=PipelineParams(audio_in_sample_rate=16000),
    )


def transcripts(frames) -> list[tuple[str, str, bool]]:
    """转录类帧的 (类型, 文本, finalized)，按先后顺序。"""
    out = []
    for f in frames:
        if isinstance(f, TranscriptionFrame):
            out.append(("final", f.text, f.finalized))
        elif isinstance(f, InterimTranscriptionFrame):
            out.append(("interim", f.text, False))
    return out


def notices(frames) -> list[dict]:
    return [
        f.data
        for f in frames
        if isinstance(f, RTVIServerMessageFrame) and f.data.get("type") == "notice"
    ]


def make_service(fake: FakeASR, *, preroll_ms: int = 300, **kwargs) -> StreamingASRService:
    return StreamingASRService(backend=fake, preroll_ms=preroll_ms, **kwargs)


# --------------------------------------------------------------------------- #
# 音频如何送给后端
# --------------------------------------------------------------------------- #


async def test_audio_is_not_sent_to_the_backend_without_a_speech_start():
    fake = FakeASR()
    await drive(make_service(fake), [audio(1), audio(2), audio(3)])
    assert fake.pushes == []
    assert fake.count("flush") == 0


async def test_speech_start_sends_the_preroll_first_then_live_audio():
    fake = FakeASR()
    frames = [audio(1), audio(2), audio(3), audio(4), STARTED(), audio(5), audio(6)]
    await drive(make_service(fake, preroll_ms=300), frames)

    # 预留缓冲只保留最近 300 毫秒（3 块），作为一整块先送；audio_end_secs = 累计采样数 / 16000
    assert fake.pushes == [
        (chunk(2) + chunk(3) + chunk(4), pytest.approx(0.4)),
        (chunk(5), pytest.approx(0.5)),
        (chunk(6), pytest.approx(0.6)),
    ]


async def test_sample_count_runs_even_when_no_audio_is_sent():
    fake = FakeASR()
    frames = [audio(1), audio(2), audio(3), audio(4), audio(5), STARTED()]
    await drive(make_service(fake), frames)
    assert fake.pushes[0][1] == pytest.approx(0.5)


async def test_sample_count_does_not_stop_when_the_service_is_muted():
    # 架构要求识别服务与会议记录器的时间轴一致，所以被静音时也要继续计数。
    fake = FakeASR()
    frames = [STTMuteFrame(mute=True), audio(1), audio(2), audio(3), STTMuteFrame(mute=False)]
    frames += [audio(4), STARTED()]
    await drive(make_service(fake), frames)
    assert fake.pushes == [(chunk(4), pytest.approx(0.4))]


async def test_no_preroll_when_it_is_zero():
    fake = FakeASR()
    await drive(make_service(fake, preroll_ms=0), [audio(1), STARTED(), audio(2)])
    assert fake.pushes == [(chunk(2), pytest.approx(0.2))]


async def test_preroll_only_holds_audio_since_the_last_speech():
    fake = FakeASR()
    frames = [audio(1), STARTED(), audio(2), STOPPED(), audio(3), STARTED(), audio(4)]
    await drive(make_service(fake, preroll_ms=300), frames)
    assert fake.pushes == [
        (chunk(1), pytest.approx(0.1)),  # 第一次开始说话：预留缓冲里是第 1 块
        (chunk(2), pytest.approx(0.2)),
        (chunk(3), pytest.approx(0.3)),  # 第二次：只有停止之后到的第 3 块，第 2 块不会再送一遍
        (chunk(4), pytest.approx(0.4)),
    ]


async def test_speech_stop_flushes_the_backend_and_stops_sending_audio():
    fake = FakeASR()
    frames = [STARTED(), audio(1), STOPPED(), audio(2), audio(3)]
    await drive(make_service(fake), frames)
    assert [c[0] for c in fake.calls if c[0] != "close"] == ["start", "push", "flush"]


async def test_speech_stop_without_a_start_does_not_flush():
    fake = FakeASR()
    await drive(make_service(fake), [audio(1), STOPPED()])
    assert fake.count("flush") == 0


async def test_audio_frames_are_passed_downstream_unchanged():
    fake = FakeASR()
    frames = [audio(1), audio(2), STARTED(), audio(3), STOPPED(), audio(4)]
    down, _ = await drive(make_service(fake), frames)
    passed = [f for f in down if isinstance(f, InputAudioRawFrame)]
    assert [f.audio for f in passed] == [chunk(i) for i in (1, 2, 3, 4)]
    # 开始 / 停止说话事件也要放行，下游的轮次判定要用
    assert sum(isinstance(f, STARTED) for f in down) == 1
    assert sum(isinstance(f, STOPPED) for f in down) == 1


# --------------------------------------------------------------------------- #
# 增量 → 转录帧
# --------------------------------------------------------------------------- #


async def test_each_delta_becomes_an_interim_frame_and_stable_text_a_transcription():
    fake = FakeASR()
    fake.on_push = {
        1: [d("你好", "吗", 0.1)],
        2: [d("", "吗？", 0.2)],  # 只有尾巴的变化：只有临时转录帧
        3: [d("吗？", "", 0.3)],
    }
    fake.on_flush = [[d("", "", 0.3, segment_end=True)]]
    frames = [STARTED(), audio(1), PAUSE, audio(2), PAUSE, audio(3), PAUSE, STOPPED()]
    down, _ = await drive(make_service(fake), frames)

    assert transcripts(down) == [
        ("interim", "你好吗", False),
        ("final", "你好", False),
        ("interim", "你好吗？", False),
        ("interim", "你好吗？", False),
        ("final", "吗？", False),
        ("interim", "你好吗？", False),  # 收尾的空增量：没有新文字，不推转录帧
    ]


async def test_segment_end_with_text_is_a_finalized_transcription():
    fake = FakeASR()
    fake.on_push = {1: [d("你好", "吗", 0.1)]}
    fake.on_flush = [[d("吗？", "", 0.2, segment_end=True)]]
    down, _ = await drive(make_service(fake), [STARTED(), audio(1), PAUSE, STOPPED()])

    assert transcripts(down) == [
        ("interim", "你好吗", False),
        ("final", "你好", False),
        ("interim", "你好吗？", False),
        ("final", "吗？", True),  # 先临时、后转录；收尾的转录帧 finalized=True
    ]


async def test_segment_end_without_text_pushes_no_transcription_frame():
    fake = FakeASR()
    fake.on_flush = [[d("", "", 0.0, segment_end=True)]]
    down, _ = await drive(make_service(fake), [STARTED(), audio(1), STOPPED()])
    assert [t for t in transcripts(down) if t[0] == "final"] == []


async def test_whitespace_only_stable_text_moves_to_the_next_transcription():
    fake = FakeASR()
    fake.on_push = {1: [d(" ", "", 0.1)], 2: [d("你好", "", 0.2)], 3: [d("吗", "", 0.3)]}
    frames = [STARTED(), audio(1), PAUSE, audio(2), PAUSE, audio(3), PAUSE]
    down, _ = await drive(make_service(fake), frames)

    finals = [t for t in transcripts(down) if t[0] == "final"]
    assert finals == [("final", " 你好", False), ("final", "吗", False)]
    assert not any(not t[1].strip() for t in transcripts(down))  # 绝不推空白文本


async def test_pending_whitespace_is_dropped_at_the_end_of_a_segment():
    fake = FakeASR()
    fake.on_push = {1: [d(" ", "", 0.1)], 2: [d("你好", "", 0.2)]}
    fake.on_flush = [[d(" ", "", 0.1, segment_end=True)]]
    frames = [STARTED(), audio(1), PAUSE, STOPPED(), PAUSE, STARTED(), audio(2), PAUSE]
    down, _ = await drive(make_service(fake), frames)
    # 第一段结束时挂着的空格不能漏到第二段开头
    assert [t for t in transcripts(down) if t[0] == "final"] == [("final", "你好", False)]


async def test_transcription_frames_carry_the_contract_fields():
    fake = FakeASR()
    delta = d("你好", "吗", 0.1)
    fake.on_push = {1: [delta]}
    down, _ = await drive(make_service(fake), [STARTED(), audio(1), PAUSE])

    frames = [f for f in down if isinstance(f, (TranscriptionFrame, InterimTranscriptionFrame))]
    assert len(frames) == 2
    for frame in frames:
        assert frame.includes_inter_frame_spaces is True  # 否则聚合器会在每条之间加空格
        assert frame.result is delta
        assert frame.user_id == ""
        assert frame.timestamp


async def test_every_transcript_text_is_non_blank_across_a_mixed_stream():
    fake = FakeASR()
    fake.on_push = {
        1: [d("", "", 0.1)],
        2: [d("  ", "", 0.2)],
        3: [d("", "吗", 0.3)],
        4: [d("你", "", 0.4)],
    }
    fake.on_flush = [[d(" ", "", 0.4, segment_end=True)]]
    frames = [STARTED()]
    for i in range(4):
        frames += [audio(i + 1), PAUSE]
    frames.append(STOPPED())
    down, _ = await drive(make_service(fake), frames)
    assert all(t[1].strip() for t in transcripts(down))


async def test_service_declares_a_fallback_wait_for_a_missing_finalized_transcript():
    service = make_service(FakeASR())
    assert service.service_metadata_frame().ttfs_p99_latency == 0.5


# 这一条直接调用处理增量的方法：「收尾之后、下一次开始说话之前不推临时转录帧」在真实管线里
# 无法稳定复现（后端自己不会在收尾后冒出增量），但规则本身需要固定下来。
async def test_no_interim_frames_outside_speech_except_for_the_segment_end():
    service = make_service(FakeASR())
    pushed = []

    async def capture(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)

    service.push_frame = capture

    await service._handle_delta(d("你好", "吗", 0.1))  # 没有在说话
    assert transcripts(pushed) == [("final", "你好", False)]  # 临时帧被抑制，转录帧照推

    pushed.clear()
    await service._handle_delta(d("吗？", "", 0.2, segment_end=True))
    assert transcripts(pushed) == [("interim", "你好吗？", False), ("final", "吗？", True)]


# --------------------------------------------------------------------------- #
# 生命周期、失败与恢复
# --------------------------------------------------------------------------- #


async def test_backend_is_started_in_the_background_and_closed_on_end():
    fake = FakeASR()
    await drive(make_service(fake), [PAUSE])
    assert fake.count("start") == 1
    assert fake.count("close") == 1


async def test_a_slow_backend_start_does_not_hold_up_the_pipeline():
    # 第一次预热可能要十几秒，不能让整条管线的 StartFrame 等着。
    gate = asyncio.Event()

    class SlowStart(FakeASR):
        async def start(self) -> None:
            await gate.wait()
            await super().start()

    fake = SlowStart()
    down, _ = await drive(make_service(fake), [audio(1), audio(2)], tail=0.05)
    assert [f.audio for f in down if isinstance(f, InputAudioRawFrame)] == [chunk(1), chunk(2)]


async def test_audio_is_not_sent_while_the_backend_is_not_ready():
    gate = asyncio.Event()

    class SlowStart(FakeASR):
        async def start(self) -> None:
            await gate.wait()
            await super().start()

    fake = SlowStart()
    await drive(make_service(fake), [STARTED(), audio(1), PAUSE, STOPPED()])
    assert fake.pushes == []
    assert fake.count("flush") == 0


async def test_backend_failure_is_announced_and_the_backend_restarted():
    fake = FakeASR()
    fake.on_push = {1: [ASRBackendError("连续失败")]}
    service = make_service(fake, retry_base_secs=0.01)
    frames = [STARTED(), audio(1), SleepFrame(sleep=0.3), audio(2), PAUSE]
    down, _ = await drive(service, frames)

    assert fake.starts == 2  # 失败后同一个后端被重新启动
    assert [(n["level"], n["text"]) for n in notices(down)] == [
        ("warn", "识别服务暂时不可用，正在重试"),
        ("info", "识别服务已恢复"),
    ]
    assert [c[1] for c in fake.calls if c[0] == "push"] == [chunk(1), chunk(2)]  # 恢复后继续送


async def test_failed_restarts_back_off_and_keep_trying():
    fake = FakeASR(start_failures=0)
    fake.on_push = {1: [ASRBackendError("连续失败")]}
    service = make_service(fake, retry_base_secs=0.01)
    original_start = fake.start

    async def flaky_start() -> None:
        await original_start()
        if fake.starts in (2, 3):  # 第一次启动成功，重启的前两次失败
            raise ASRBackendError("还没好")

    fake.start = flaky_start
    frames = [STARTED(), audio(1), SleepFrame(sleep=0.5), audio(2), PAUSE]
    down, _ = await drive(service, frames)

    assert fake.starts == 4
    assert [n["level"] for n in notices(down)] == ["warn", "info"]  # 只在失败和恢复时各说一次
    assert [c[1] for c in fake.calls if c[0] == "push"] == [chunk(1), chunk(2)]


async def test_audio_keeps_flowing_downstream_while_the_backend_is_down():
    fake = FakeASR(start_failures=1000)  # 永远起不来
    service = make_service(fake, retry_base_secs=0.01)
    down, _ = await drive(service, [audio(1), audio(2), audio(3)], tail=0.1)
    assert [f.audio for f in down if isinstance(f, InputAudioRawFrame)] == [
        chunk(1),
        chunk(2),
        chunk(3),
    ]


async def test_an_error_while_handling_one_delta_does_not_stop_the_transcript():
    fake = FakeASR()
    fake.on_push = {1: [d("坏", "", 0.1)], 2: [d("好", "", 0.2)]}
    service = make_service(fake)
    original = service._handle_delta
    calls = 0

    async def flaky(delta: ASRDelta) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("处理第一条时出错")
        await original(delta)

    service._handle_delta = flaky
    down, _ = await drive(service, [STARTED(), audio(1), PAUSE, audio(2), PAUSE])
    assert [t for t in transcripts(down) if t[0] == "final"] == [("final", "好", False)]


# --------------------------------------------------------------------------- #
# 继续一场会议：时间加上本次连接的起点
# --------------------------------------------------------------------------- #


async def test_delta_times_are_shifted_by_the_connection_base():
    fake = FakeASR()
    fake.on_push = {1: [d("你好", "世", end=0.1)]}
    fake.on_flush = [[d("世界", end=0.2, segment_end=True)]]
    service = make_service(fake, base_secs=720.0)
    down, _ = await drive(service, [STARTED(), audio(1), PAUSE, audio(2), STOPPED()])
    results = [
        f.result for f in down if isinstance(f, TranscriptionFrame | InterimTranscriptionFrame)
    ]
    assert [round(r.audio_end_secs, 3) for r in results] == [720.1, 720.1, 720.2, 720.2]
    # 同一个增量的临时转录帧和转录帧仍然共用一个对象（会议记录器靠它去重）
    assert results[0] is results[1] and results[2] is results[3]
    assert (results[0].stable_text, results[0].unstable_text) == ("你好", "世")
    # 后端只知道本次连接的音频：它收到的时间不带起点
    assert [round(end, 3) for _, end in fake.pushes] == [0.1, 0.2]


async def test_without_a_base_the_delta_is_passed_through_untouched():
    fake = FakeASR()
    delta = d("你好", end=0.1, segment_end=True)
    fake.on_push = {1: [delta]}
    down, _ = await drive(make_service(fake), [STARTED(), audio(1), PAUSE, STOPPED()])
    results = [f.result for f in down if isinstance(f, TranscriptionFrame)]
    assert results and results[0] is delta
