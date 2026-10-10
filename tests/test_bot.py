"""管线组装（pipeline/bot.py）。

* 「能构造出来」：不连任何服务，断言处理器顺序符合 architecture.md §4；
* 唤醒、打断的行为：用组装出来的真实用户侧聚合器配上真实的唤醒策略来驱动；
* 端到端演练：假识别后端 + 模拟的大模型与语音合成，从「叫名字」走到「朗读应答」。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import numpy as np
import pytest
from fakes import FakeASR
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    InputAudioRawFrame,
    InterruptionFrame,
    LLMContextAssistantTurnFrame,
    LLMContextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame
from pipecat.tests.utils import SleepFrame, run_test
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)

from agentic_meeting.asr.stt_service import StreamingASRService
from agentic_meeting.pipeline.activity import AssistantActivity
from agentic_meeting.pipeline.bot import (
    AppResources,
    build_input_filter,
    build_parts,
    pipeline_processors,
    requested_session_id,
    resumed_context,
    session_message,
    transport_params,
    wire_wake_events,
    wire_wake_sleep,
)
from agentic_meeting.pipeline.modality import ModalityGate
from agentic_meeting.pipeline.recorder import MeetingRecorder
from agentic_meeting.pipeline.services import (
    LocalTTSService,
    RealtimeLLMService,
    build_realtime_llm,
    build_tts,
)
from agentic_meeting.pipeline.wake import WakeWordUserTurnStartStrategy
from agentic_meeting.types import ASRDelta

PCM = bytes(range(256)) * 20


class Passthrough(FrameProcessor):
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class FakeTransport:
    """只提供 ``input()`` / ``output()``，够用来排管线。"""

    def __init__(self):
        self._input, self._output = Passthrough(name="in"), Passthrough(name="out")

    def input(self):
        return self._input

    def output(self):
        return self._output


@pytest.fixture
def cfg(make_cfg):
    cfg = make_cfg()
    cfg.session.assistant_name = "Nova"
    cfg.session.wake_aliases = []
    cfg.turn.smart_turn = False  # 端到端演练里不依赖轮次结束模型对静音的判断
    cfg.turn.wake_timeout_secs = 5.0
    cfg.turn.single_activation = True
    return cfg


# --------------------------------------------------------------------------- #
# 能构造出来、顺序对
# --------------------------------------------------------------------------- #


def test_processors_are_in_the_documented_order(cfg):
    transport = FakeTransport()
    parts = build_parts(cfg)
    processors = pipeline_processors(transport, parts)

    assert [type(p) for p in processors[1:7]] == [
        type(parts.vad),
        StreamingASRService,
        MeetingRecorder,
        type(parts.user_aggregator),
        ModalityGate,  # 每个新请求进入模型之前设定应答模态（pipeline/modality.py）
        RealtimeLLMService,
    ]
    assert processors[0] is transport.input()
    assert isinstance(processors[7], LocalTTSService)
    assert processors[8] is transport.output()
    assert processors[9] is parts.assistant_aggregator
    assert len(processors) == 10
    Pipeline(processors)  # 能真的串起来


def test_tts_is_left_out_of_the_pipeline_when_disabled(cfg):
    cfg.tts.enabled = False
    parts = build_parts(cfg)
    assert parts.tts is None
    processors = pipeline_processors(FakeTransport(), parts)
    assert not any(isinstance(p, LocalTTSService) for p in processors)
    assert len(processors) == 9
    Pipeline(processors)


def test_vad_uses_the_configured_stop_silence(cfg):
    cfg.turn.vad_stop_secs = 0.35
    assert build_parts(cfg).vad_analyzer.params.stop_secs == 0.35


def test_vad_uses_the_configured_volume_gate(cfg):
    assert build_parts(cfg).vad_analyzer.params.min_volume == 0.6
    cfg.turn.vad_min_volume = 0.35
    assert build_parts(cfg).vad_analyzer.params.min_volume == 0.35


def test_input_gain_filter_follows_the_audio_config(cfg):
    cfg.audio.auto_gain, cfg.audio.gain_db = True, 3.0
    cfg.audio.max_gain_db, cfg.audio.target_dbfs = 24.0, -20.0
    cfg.audio.noise_floor_dbfs, cfg.audio.level_log_secs = -66.0, 7.0
    flt = build_input_filter(cfg)
    assert flt is not None
    assert (flt.auto_gain, flt.max_gain_db, flt.target_dbfs) == (True, 24.0, -20.0)
    assert (flt.noise_floor_dbfs, flt.level_log_secs, flt.gain_db) == (-66.0, 7.0, 3.0)


def test_no_input_gain_filter_when_it_would_do_nothing(cfg):
    cfg.audio.auto_gain, cfg.audio.gain_db = False, 0.0
    assert build_input_filter(cfg) is None
    cfg.audio.gain_db = 6.0  # 关了自动增益但配了固定增益：仍然要挂
    assert build_input_filter(cfg) is not None


def test_transport_hands_the_gain_filter_to_pipecat(cfg):
    flt = build_input_filter(cfg)
    params = transport_params(cfg, flt)
    assert params.audio_in_filter is flt
    assert params.audio_in_enabled and params.audio_out_enabled
    assert transport_params(cfg, None).audio_in_filter is None


def test_asr_backend_hears_the_hotwords_and_the_assistants_name(cfg):
    cfg.session.hotwords = ["课题术语", "Nova"]
    cfg.session.wake_aliases = ["Novah"]  # 别名是听错时的写法，不当热词
    backend = build_parts(cfg).asr.backend
    assert backend.hotwords == ["课题术语", "Nova"]


def test_asr_preroll_comes_from_the_configuration(cfg):
    cfg.asr.preroll_ms = 450
    assert build_parts(cfg).asr._preroll_bytes == 450 * 32


def test_smart_turn_selects_the_turn_analyzer_stop_strategy(cfg):
    cfg.turn.smart_turn = True
    parts = build_parts(cfg)
    (strategy,) = parts.user_aggregator._params.user_turn_strategies.stop
    assert isinstance(strategy, TurnAnalyzerUserTurnStopStrategy)
    assert isinstance(strategy._turn_analyzer, LocalSmartTurnAnalyzerV3)


def test_without_smart_turn_a_speech_timeout_decides_the_end_of_turn(cfg):
    cfg.turn.smart_turn = False
    (strategy,) = build_parts(cfg).user_aggregator._params.user_turn_strategies.stop
    assert isinstance(strategy, SpeechTimeoutUserTurnStopStrategy)


@pytest.mark.parametrize("single_activation", [True, False])
def test_the_wake_strategy_is_first_and_follows_the_configuration(cfg, single_activation):
    cfg.session.wake_aliases = ["Novah"]
    cfg.turn.single_activation = single_activation
    cfg.turn.wake_timeout_secs = 7.0
    parts = build_parts(cfg)
    first = parts.user_aggregator._params.user_turn_strategies.start[0]

    assert first is parts.wake
    assert isinstance(first, WakeWordUserTurnStartStrategy)
    assert first._phrases == ["Nova", "Novah"]
    assert first._single_activation is single_activation
    assert first._timeout == 7.0


def test_system_prompt_contains_the_assistant_name(cfg):
    parts = build_parts(cfg)
    assert "Nova" in parts.llm._settings.system_instruction


def test_app_resources_carry_the_config(cfg):
    assert AppResources(cfg).cfg is cfg


# --------------------------------------------------------------------------- #
# 唤醒与打断（真实的聚合器 + 真实的唤醒策略）
# --------------------------------------------------------------------------- #


def transcript(text: str) -> TranscriptionFrame:
    frame = TranscriptionFrame(text=text, user_id="", timestamp="t", finalized=True)
    frame.includes_inter_frame_spaces = True
    return frame


async def drive_aggregator(parts, frames):
    return await run_test(
        parts.user_aggregator,
        frames_to_send=frames,
        pipeline_params=PipelineParams(audio_in_sample_rate=16000),
    )


async def test_speech_without_the_assistants_name_never_reaches_the_model(cfg):
    parts = build_parts(cfg)
    frames = [
        VADUserStartedSpeakingFrame(),
        transcript("今天讨论一下数据集的问题"),
        SleepFrame(sleep=0.05),
        VADUserStoppedSpeakingFrame(),
        SleepFrame(sleep=1.0),
    ]
    down, up = await drive_aggregator(parts, frames)
    assert not any(isinstance(f, (UserStartedSpeakingFrame, LLMContextFrame)) for f in down)


async def test_the_name_starts_a_turn_and_the_model_gets_the_context(cfg):
    parts = build_parts(cfg)
    frames = [
        VADUserStartedSpeakingFrame(),
        transcript("Nova，帮我总结一下"),
        SleepFrame(sleep=0.05),
        VADUserStoppedSpeakingFrame(),
        SleepFrame(sleep=1.2),
    ]
    down, _ = await drive_aggregator(parts, frames)
    contexts = [f for f in down if isinstance(f, LLMContextFrame)]
    assert len(contexts) == 1
    assert "Nova，帮我总结一下" in json.dumps(contexts[0].context.messages, ensure_ascii=False)


async def test_wake_event_tells_the_browser_the_assistant_is_listening(cfg):
    parts = build_parts(cfg)
    sent: list[dict] = []

    async def send(data: dict) -> None:
        sent.append(data)

    wire_wake_events(parts.wake, send)
    frames = [transcript("今天先看数据"), SleepFrame(sleep=0.05), transcript("Nova 在吗")]
    await drive_aggregator(parts, [*frames, SleepFrame(sleep=0.3)])
    assert sent == [{"type": "assistant_state", "state": "listening"}]


def first_turn_then_user_speaks_over_the_assistant() -> list:
    """叫名字、说完、助理开口回答、有人在助理说话时开口。第一轮必须真的结束，第二次开口才算新的一轮。"""
    return [
        VADUserStartedSpeakingFrame(),
        transcript("Nova，说一下"),
        SleepFrame(sleep=0.05),
        VADUserStoppedSpeakingFrame(),
        SleepFrame(sleep=1.2),  # 停顿 0.6 秒判定说完，模型拿到上下文
        BotStartedSpeakingFrame(),
        SleepFrame(sleep=0.05),
        VADUserStartedSpeakingFrame(),
        SleepFrame(sleep=0.2),
    ]


async def test_speaking_while_the_assistant_talks_interrupts_it_when_awake(cfg):
    parts = build_parts(cfg)  # 唤醒窗口 5 秒，第二次开口还在窗口内
    down, _ = await drive_aggregator(parts, first_turn_then_user_speaks_over_the_assistant())
    # 一次来自叫名字，一次来自助理说话时有人开口
    assert sum(isinstance(f, InterruptionFrame) for f in down) == 2


async def test_speaking_after_the_wake_window_has_closed_does_not_interrupt(cfg):
    # 记录一个已知的边界：Pipecat 的唤醒策略在 single_activation 模式下只保持 wake_timeout_secs，
    # 助理说话也不会续期。窗口过后回到 IDLE，策略会拦住后面所有策略，开口说话打不断朗读，
    # 只有再叫一次名字才行。
    cfg.turn.wake_timeout_secs = 0.3
    parts = build_parts(cfg)
    down, _ = await drive_aggregator(parts, first_turn_then_user_speaks_over_the_assistant())
    assert sum(isinstance(f, InterruptionFrame) for f in down) == 1  # 只有叫名字那一次


async def answer_then_someone_else_speaks(cfg, monkeypatch, *, answered: bool):
    """叫名字问一句 →（助理答完 / 没答完）→ 另一个人接着说会上的事。返回交给模型的上下文帧。"""
    parts = build_parts(cfg)
    activity = AssistantActivity(idle_delay_secs=0.0)
    wire_wake_sleep(activity, parts.wake, parts.user_aggregator)

    submitted = asyncio.Event()
    push = parts.user_aggregator.push_frame

    async def observed_push(frame, direction):
        await push(frame, direction)
        if isinstance(frame, LLMContextFrame):
            submitted.set()

    monkeypatch.setattr(parts.user_aggregator, "push_frame", observed_push)

    async def assistant_answers() -> None:
        await asyncio.wait_for(submitted.wait(), 2.0)
        await activity.set_generating(True)
        if answered:
            await activity.set_generating(False)

    frames = [
        VADUserStartedSpeakingFrame(),
        transcript("Nova，说一下"),
        SleepFrame(sleep=0.05),
        VADUserStoppedSpeakingFrame(),
        SleepFrame(sleep=1.8),
        VADUserStartedSpeakingFrame(),
        transcript("我们接着讨论下一个议题"),
        SleepFrame(sleep=0.05),
        VADUserStoppedSpeakingFrame(),
        SleepFrame(sleep=1.2),
    ]
    answering = asyncio.create_task(assistant_answers())
    try:
        down, _ = await drive_aggregator(parts, frames)
        await answering
        return [f for f in down if isinstance(f, LLMContextFrame)]
    finally:
        answering.cancel()
        await asyncio.gather(answering, return_exceptions=True)
        await activity.close()


async def test_after_the_answer_other_peoples_speech_no_longer_reaches_the_model(cfg, monkeypatch):
    # 唤醒窗口 5 秒还没过，但助理已经答完：会上其他人接着说的话不该再触发它
    assert len(await answer_then_someone_else_speaks(cfg, monkeypatch, answered=True)) == 1


async def test_while_the_answer_is_still_coming_speech_still_reaches_the_model(cfg, monkeypatch):
    # 助理还没答完时开口是打断 / 追问，仍然算数（和原来一样）
    assert len(await answer_then_someone_else_speaks(cfg, monkeypatch, answered=False)) == 2


# --------------------------------------------------------------------------- #
# 端到端演练
# --------------------------------------------------------------------------- #


def sse(*chunks: dict) -> str:
    return (
        "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks)
        + "data: [DONE]\n\n"
    )


def completion_chunk(delta: dict, finish: str | None = None) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "fake",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


async def test_end_to_end_from_the_assistants_name_to_a_spoken_reply(cfg):
    llm_bodies: list[dict] = []
    tts_bodies: list[dict] = []

    def llm_handler(request: httpx.Request) -> httpx.Response:
        llm_bodies.append(json.loads(request.content))
        body = sse(
            completion_chunk({"role": "assistant", "content": "好的，"}),
            completion_chunk({"content": "马上整理。"}),
            completion_chunk({}, finish="stop"),
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    def tts_handler(request: httpx.Request) -> httpx.Response:
        tts_bodies.append(json.loads(request.content))
        return httpx.Response(200, content=PCM, headers={"content-type": "audio/pcm"})

    asr = FakeASR()
    asr.on_push = {1: [ASRDelta("Nova，", "帮我", 0.1)], 2: [ASRDelta("帮我总结一下", "", 0.2)]}
    asr.on_flush = [[ASRDelta("。", "", 0.2, segment_end=True)]]

    llm = build_realtime_llm(
        cfg,
        "你是助理",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(llm_handler)),
    )
    tts = build_tts(
        cfg,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(tts_handler)),
        stop_frame_timeout_s=0.3,
    )
    parts = build_parts(cfg, asr_backend=asr, llm=llm, tts=tts)
    sent: list[dict] = []

    async def send(data: dict) -> None:
        sent.append(data)

    wire_wake_events(parts.wake, send)

    chunk = np.zeros(16 * 100, dtype="<i2").tobytes()

    def audio() -> InputAudioRawFrame:
        return InputAudioRawFrame(audio=chunk, sample_rate=16000, num_channels=1)

    frames = [
        SleepFrame(sleep=0.1),  # 让识别后端先在后台起来
        VADUserStartedSpeakingFrame(),
        audio(),
        SleepFrame(sleep=0.05),
        audio(),
        SleepFrame(sleep=0.05),
        VADUserStoppedSpeakingFrame(),
        SleepFrame(sleep=2.5),
    ]
    pipeline = Pipeline(pipeline_processors(FakeTransport(), parts))
    down, _ = await run_test(
        pipeline,
        frames_to_send=frames,
        pipeline_params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
    )

    # 字幕：浏览器收到了 caption 消息，说话人未知
    captions = [
        f.data
        for f in down
        if isinstance(f, RTVIServerMessageFrame) and f.data["type"] == "caption"
    ]
    assert captions and captions[-1]["stable"] == "Nova，帮我总结一下。"
    assert captions[-1]["speaker_name"] == "未知"
    # 被叫到名字
    assert sent == [{"type": "assistant_state", "state": "listening"}]
    # 大模型收到的是系统提示词和用户这一轮的话——各条转录之间没有多余的空格
    assert len(llm_bodies) == 1
    messages = llm_bodies[0]["messages"]
    assert messages[0] == {"role": "system", "content": "你是助理"}
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "Nova，帮我总结一下。"
    # 文字应答、朗读
    spoken = [f.text for f in down if isinstance(f, LLMContextAssistantTurnFrame)]
    assert "".join(spoken) == "好的，马上整理。"
    assert tts_bodies and "整理" in tts_bodies[0]["input"]
    assert b"".join(f.audio for f in down if isinstance(f, TTSAudioRawFrame)).startswith(PCM[:64])
    assert asr.count("flush") == 1


async def test_the_pipeline_keeps_transcribing_when_the_model_is_unreachable(cfg):
    # 转录链路优先存活：实时模型挂了，字幕照常，只是没有应答。
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("连接被拒绝")

    asr = FakeASR()
    asr.on_push = {1: [ASRDelta("Nova，你好", "", 0.1)], 2: [ASRDelta("今天的议题", "", 0.2)]}
    llm = build_realtime_llm(
        cfg, "你是助理", http_client=httpx.AsyncClient(transport=httpx.MockTransport(broken))
    )
    parts = build_parts(cfg, asr_backend=asr, llm=llm)
    chunk = np.zeros(16 * 100, dtype="<i2").tobytes()

    def audio() -> InputAudioRawFrame:
        return InputAudioRawFrame(audio=chunk, sample_rate=16000, num_channels=1)

    frames = [
        SleepFrame(sleep=0.1),
        VADUserStartedSpeakingFrame(),
        audio(),
        SleepFrame(sleep=0.05),
        audio(),
        SleepFrame(sleep=0.05),
        VADUserStoppedSpeakingFrame(),
        SleepFrame(sleep=1.5),
    ]
    down, _ = await run_test(
        Pipeline(pipeline_processors(FakeTransport(), parts)),
        frames_to_send=frames,
        pipeline_params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
    )
    captions = [
        f.data
        for f in down
        if isinstance(f, RTVIServerMessageFrame) and f.data["type"] == "caption"
    ]
    assert captions and captions[-1]["stable"].endswith("今天的议题")
    assert sum(isinstance(f, InputAudioRawFrame) for f in down) == 2  # 音频照常下传


# --------------------------------------------------------------------------- #
# 继续一场会议
# --------------------------------------------------------------------------- #


def test_requested_session_id_comes_from_the_connection_parameters():
    assert requested_session_id({"session_id": " abc "}) == "abc"
    for data in (None, {}, {"session_id": None}, {"session_id": ""}, {"session_id": 3}, "abc"):
        assert requested_session_id(data) is None


def test_session_message_says_whether_this_is_a_continuation():
    from agentic_meeting.types import Session

    session = Session(id="s1", started_at=1000.0, title="周三组会")
    assert session_message(session) == {
        "type": "session",
        "id": "s1",
        "title": "周三组会",
        "keep": False,
        "started_at": 1000.0,
        "resumed": False,
        "base_secs": 0.0,
        "state": "live",
    }
    again = session_message(session, True, 720.5)
    assert (again["resumed"], again["base_secs"]) == (True, 720.5)


def test_a_resumed_connection_shifts_the_clock_and_starts_with_the_rebuilt_context(cfg):
    from agentic_meeting.pipeline.clock import SessionClock

    restored = [
        {"role": "user", "content": "[会议纪要]\n一、讨论了学习率。"},
        {"role": "user", "content": "[00:11:50 王老师] 那就先这样"},
    ]
    parts = build_parts(
        cfg, asr_backend=FakeASR(), clock=SessionClock(720.0), messages=list(restored)
    )
    assert parts.context.get_messages() == restored
    assert parts.recorder.elapsed_secs == 720.0  # 还没收到音频：时间轴停在本次连接的起点
    assert parts.asr._base_secs == 720.0
    fresh = build_parts(cfg, asr_backend=FakeASR())
    assert fresh.context.get_messages() == [] and fresh.recorder.elapsed_secs == 0.0
    # 字幕行的编号每次连接各占一段，页面上留着的旧行不会和新连接的撞号
    assert fresh.recorder._assembler._next_id == 1
    assert fresh.recorder._merge_gap == cfg.transcript.merge_gap_secs == 2.0  # 并片段的设置来自配置
    seeded = build_parts(cfg, asr_backend=FakeASR(), first_segment_id=7_000_001)
    assert seeded.recorder._assembler._next_id == 7_000_001
    assert fresh.asr._base_secs == 0.0


async def test_resumed_context_is_rebuilt_from_the_database(cfg, tmp_path):
    from agentic_meeting.pipeline.session import LiveConnection
    from agentic_meeting.store.db import Store
    from agentic_meeting.types import Utterance

    cfg.realtime.keep_recent_minutes = 5
    store = await Store.open(tmp_path / "m.db", 4, assistant_name="Nova")
    try:
        session = await store.create_session("会", now=1000.0)
        await store.add_utterance(Utterance(session.id, 1, 10.0, 12.0, "很早以前的话"))
        last = await store.add_utterance(Utterance(session.id, 1, 300.0, 302.0, "纪要里有的话"))
        await store.add_digest(
            session.id, t_from=0.0, t_to=302.0, text="一、讨论了学习率。", last_utterance_id=last
        )
        await store.add_utterance(Utterance(session.id, 2, 700.0, 702.0, "断线前的最后一句"))
        await store.add_utterance(
            Utterance(session.id, -1, 703.0, 705.0, "好的", source="assistant")
        )

        fresh = LiveConnection(session, 1, 1000.0)
        assert await resumed_context(cfg, store, fresh) is None  # 新建的会议不用重建
        assert await resumed_context(cfg, None, fresh) is None

        live = LiveConnection(session, 2, 2000.0, base_secs=1000.0, resumed=True)
        messages = await resumed_context(cfg, store, live)
        assert messages is not None
        assert messages[0]["content"].endswith("一、讨论了学习率。")
        assert [m["content"] for m in messages[1:]] == [
            "[00:11:40 说话人 2] 断线前的最后一句",
            "好的",
        ]
        assert messages[-1]["role"] == "assistant"

        async def broken(*args, **kwargs):
            raise RuntimeError("库坏了")

        store.latest_digest = broken  # 读不出来不拦着连接
        assert await resumed_context(cfg, store, live) is None
    finally:
        await store.close()
