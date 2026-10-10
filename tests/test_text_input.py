"""文字输入与同模态应答（pipeline/text_input.py）。

前半用假的记录器和假的「推帧」函数测处理器本身（校验、排队、不打断、忙闲判断）；
后半用真实的管线（假识别后端、模拟的大模型与语音合成）验证：文字请求只出文字、语音合成一次都没被调用，
之后被叫到名字的那一轮又恢复朗读。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import numpy as np
import pytest
from fakes import FakeASR
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    LLMConfigureOutputFrame,
    LLMMessagesAppendFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame
from pipecat.tests.utils import SleepFrame, run_test
from test_bot import PCM, FakeTransport, completion_chunk, sse
from test_meeting_recorder import CallSignal, Hook
from waiting import wait_until

from agentic_meeting.pipeline.bot import (
    build_parts,
    pipeline_processors,
    wire_assistant_recording,
    wire_text_input,
    wire_wake_events,
)
from agentic_meeting.pipeline.modality import TextRequestFrame
from agentic_meeting.pipeline.services import build_realtime_llm, build_tts
from agentic_meeting.pipeline.text_input import (
    MAX_QUEUED,
    TextInputHandler,
    extract_text,
)
from agentic_meeting.store.db import default_speaker_name
from agentic_meeting.types import SPEAKER_TYPED, ASRDelta, Utterance

# --------------------------------------------------------------------------- #
# 处理器本身
# --------------------------------------------------------------------------- #


class FakeRecorder:
    def __init__(self):
        self.typed: list[str] = []

    async def record_typed(self, text: str) -> Utterance | None:
        self.typed.append(text)
        return Utterance(
            "s1", SPEAKER_TYPED, 5.0, 5.0, text, source="text", addressed_to_assistant=True
        )

    async def context_text(self, utterance: Utterance) -> str:
        return f"[00:00:05 文字输入] {utterance.text}"


class Rig:
    def __init__(self, *, tts_enabled=True, **kwargs):
        self.recorder = FakeRecorder()
        self.pushed: list[Frame] = []
        self.notices: list[tuple[str, str]] = []
        self.push_error: Exception | None = None

        async def push(frame: Frame) -> None:
            if self.push_error:
                raise self.push_error
            self.pushed.append(frame)

        async def notice(level: str, text: str) -> None:
            self.notices.append((level, text))

        self.handler = TextInputHandler(
            recorder=self.recorder, push=push, notice=notice, tts_enabled=tts_enabled, **kwargs
        )

    def lines(self) -> list[str]:
        """推给模型的用户消息，按顺序。"""
        return [
            f.messages[0]["content"] for f in self.pushed if isinstance(f, LLMMessagesAppendFrame)
        ]

    def shape(self) -> list[str]:
        out = []
        for f in self.pushed:
            if isinstance(f, LLMConfigureOutputFrame):
                out.append(f"skip_tts={f.skip_tts}")
            elif isinstance(f, TextRequestFrame):
                out.append("text_request")
            elif isinstance(f, LLMMessagesAppendFrame):
                out.append(f"append(run_llm={f.run_llm})")
        return out


async def test_an_idle_assistant_gets_a_text_only_request_without_being_interrupted():
    rig = Rig()
    await rig.handler.handle("  帮我查一下论文  ")
    assert rig.recorder.typed == ["帮我查一下论文"]
    # 请求前面一个记号（管线里的模态闸门据此让这次回答只出文字）；后面不跟「恢复朗读」——
    # 模型先调工具再回答时，那样会把工具之后的回答念出来。没有 InterruptionFrame
    assert rig.shape() == ["text_request", "append(run_llm=True)"]
    assert rig.lines() == ["[00:00:05 文字输入] 帮我查一下论文"]
    assert rig.pushed[1].messages == [{"role": "user", "content": rig.lines()[0]}]
    assert not any(type(f).__name__ == "InterruptionFrame" for f in rig.pushed)
    assert rig.handler.busy and rig.notices == []


async def test_without_a_speech_service_there_is_nothing_to_switch_off():
    rig = Rig(tts_enabled=False)
    await rig.handler.handle("你好")
    assert rig.shape() == ["append(run_llm=True)"]


@pytest.mark.parametrize("raw", ["", "   ", "\n\t "])
async def test_blank_messages_are_refused_with_a_warning(raw):
    rig = Rig()
    await rig.handler.handle(raw)
    assert rig.recorder.typed == [] and rig.pushed == []
    assert [level for level, _ in rig.notices] == ["warn"]


async def test_too_long_messages_are_refused_and_exactly_the_limit_is_fine():
    rig = Rig(max_chars=10)
    await rig.handler.handle("字" * 11)
    assert rig.pushed == [] and rig.notices[0][0] == "warn" and "10" in rig.notices[0][1]
    await rig.handler.handle("字" * 10)
    assert len(rig.lines()) == 1


async def test_requests_wait_while_the_assistant_is_generating_and_go_in_order_afterwards():
    rig = Rig()
    await rig.handler.set_generating(True)  # 助理正在回答别的
    await rig.handler.handle("第一条")
    await rig.handler.handle("第二条")
    assert rig.pushed == [] and rig.handler.queued == 2
    assert rig.recorder.typed == ["第一条", "第二条"]  # 已经落库、显示在字幕里，只是还没交给模型
    assert [n[0] for n in rig.notices] == ["info", "info"]
    assert "第 1 位" in rig.notices[0][1] and "第 2 位" in rig.notices[1][1]

    await rig.handler.set_generating(False)
    assert rig.lines() == ["[00:00:05 文字输入] 第一条"]  # 一次只交一条
    assert rig.handler.queued == 1
    await rig.handler.set_generating(True)  # 第一条的回答开始
    await rig.handler.set_generating(False)  # 回答完
    assert rig.lines() == ["[00:00:05 文字输入] 第一条", "[00:00:05 文字输入] 第二条"]
    assert rig.handler.queued == 0


async def test_requests_also_wait_for_the_assistant_to_stop_speaking():
    rig = Rig()
    await rig.handler.set_speaking(True)
    await rig.handler.handle("等朗读结束")
    assert rig.pushed == []
    await rig.handler.set_generating(False)  # 回答生成完了，但声音还在播
    assert rig.pushed == []
    await rig.handler.set_speaking(False)
    assert rig.lines() == ["[00:00:05 文字输入] 等朗读结束"]


async def test_back_to_back_requests_do_not_overtake_each_other():
    rig = Rig()
    await rig.handler.handle("甲")
    await rig.handler.handle("乙")  # 甲刚交出去，助理还没来得及报告「开始回答」：乙也要排队
    assert rig.lines() == ["[00:00:05 文字输入] 甲"]
    assert rig.handler.queued == 1
    await rig.handler.set_generating(True)
    await rig.handler.set_generating(False)
    assert rig.lines()[-1] == "[00:00:05 文字输入] 乙"


async def test_the_queue_has_a_limit_and_overflow_is_dropped_with_a_warning():
    rig = Rig()
    await rig.handler.set_generating(True)
    for i in range(MAX_QUEUED):
        await rig.handler.handle(f"第{i}条")
    await rig.handler.handle("多出来的")
    assert rig.handler.queued == MAX_QUEUED
    assert "多出来的" not in rig.recorder.typed  # 没排上的不落库，免得字幕里有一条没人回答的
    assert rig.notices[-1][0] == "warn" and "排队已满" in rig.notices[-1][1]


async def test_a_silent_model_does_not_block_the_queue_forever():
    rig = Rig(busy_timeout_secs=0.05)
    await rig.handler.handle("甲")
    await rig.handler.handle("乙")  # 排队
    assert rig.lines() == ["[00:00:05 文字输入] 甲"]
    try:
        await wait_until(lambda: len(rig.lines()) == 2, description="无响应看门狗处理下一条")
        assert rig.lines() == ["[00:00:05 文字输入] 甲", "[00:00:05 文字输入] 乙"]
    finally:
        await rig.handler.close()


async def test_the_watchdog_stands_down_once_the_answer_starts():
    rig = Rig(busy_timeout_secs=0.05)
    await rig.handler.handle("甲")
    await rig.handler.set_generating(True)  # 回答开始了
    await rig.handler.handle("乙")
    await asyncio.sleep(0.2)
    assert rig.lines() == ["[00:00:05 文字输入] 甲"]  # 回答还在进行，不能因为超时就把乙插进去
    await rig.handler.close()


async def test_a_failing_push_is_logged_and_does_not_wedge_the_handler():
    rig = Rig()
    rig.push_error = RuntimeError("管线已经关了")
    await rig.handler.handle("甲")  # 不抛出
    assert not rig.handler.busy
    rig.push_error = None
    await rig.handler.handle("乙")
    assert rig.lines() == ["[00:00:05 文字输入] 乙"]


async def test_a_failing_notice_does_not_break_handling():
    rig = Rig()

    async def broken(level: str, text: str) -> None:
        raise RuntimeError("发不出去")

    rig.handler._notice = broken
    await rig.handler.handle("   ")  # 只是提示失败，不抛出


class Emitter:
    """假的事件源：event_handler 装饰器记下处理函数，fire 触发它。"""

    def __init__(self):
        self.handlers: dict[str, object] = {}

    def event_handler(self, name):
        def decorator(fn):
            self.handlers[name] = fn
            return fn

        return decorator

    async def fire(self, name, *args):
        await self.handlers[name](self, *args)


class FakeMessage:
    def __init__(self, type, data):
        self.type, self.data = type, data


async def test_wiring_connects_client_messages_assistant_turns_and_speaking():
    rig = Rig()
    rtvi, aggregator = Emitter(), Emitter()
    recorder = FakeRecorder()
    wire_text_input(rtvi, aggregator, recorder, rig.handler, rig.handler._notice)

    # 助理开始一轮回答：之后的文字请求排队，一轮结束后才交出去
    await aggregator.fire("on_assistant_turn_started")
    await rtvi.fire("on_client_message", FakeMessage("text_input", {"text": "排队的"}))
    assert rig.pushed == []
    await aggregator.fire("on_assistant_turn_stopped", object())
    assert rig.lines() == ["[00:00:05 文字输入] 排队的"]

    # 助理还在朗读：同样要等
    await aggregator.fire("on_assistant_turn_started")
    await aggregator.fire("on_assistant_turn_stopped", object())
    await recorder_speaking(recorder, rig, True)
    await rtvi.fire("on_client_message", FakeMessage("text_input", {"text": "等朗读"}))
    assert len(rig.lines()) == 1
    await recorder_speaking(recorder, rig, False)
    assert rig.lines()[-1] == "[00:00:05 文字输入] 等朗读"


async def recorder_speaking(recorder, rig, speaking: bool) -> None:
    await recorder.on_bot_speaking_changed(speaking)


async def test_wiring_ignores_other_client_messages_and_reports_bad_text_input():
    rig = Rig()
    rtvi = Emitter()
    wire_text_input(rtvi, Emitter(), FakeRecorder(), rig.handler, rig.handler._notice)
    await rtvi.fire("on_client_message", FakeMessage("screen_state", {"sharing": True}))
    await rtvi.fire("on_client_message", FakeMessage("text_input", {"text": 5}))
    await rtvi.fire("on_client_message", FakeMessage("text_input", "不是对象"))
    assert rig.pushed == [] and rig.recorder.typed == []
    assert [n[0] for n in rig.notices] == [
        "warn",
        "warn",
    ]  # 格式不对的两条各提示一次，别的消息类型不理会


def test_extract_text():
    assert extract_text({"text": "你好"}) == "你好"
    assert extract_text({"text": 5}) is None
    assert extract_text({}) is None
    assert extract_text("你好") is None
    assert extract_text(None) is None


# --------------------------------------------------------------------------- #
# 真实的管线：文字请求只出文字，下一轮语音唤醒照常朗读
# --------------------------------------------------------------------------- #


class MemoryStore:
    def __init__(self):
        self.utterances: list[Utterance] = []

    async def add_utterance(self, utterance: Utterance) -> int:
        utterance.id = len(self.utterances) + 1
        self.utterances.append(utterance)
        return utterance.id

    async def speaker_name(self, session_id: str, idx: int) -> str:
        return default_speaker_name(idx, "Nova")

    async def update_utterance_speaker(self, utterance_id: int, speaker_idx: int) -> bool:
        return True


@pytest.fixture
def cfg(make_cfg):
    cfg = make_cfg()
    cfg.session.assistant_name = "Nova"
    cfg.session.wake_aliases = []
    cfg.turn.smart_turn = False
    cfg.turn.wake_timeout_secs = 5.0
    cfg.turn.single_activation = True
    return cfg


async def test_a_typed_request_is_answered_in_text_only_and_the_next_spoken_turn_is_spoken_again(
    cfg,
):
    llm_bodies: list[dict] = []
    tts_inputs: list[str] = []

    def llm_handler(request: httpx.Request) -> httpx.Response:
        llm_bodies.append(json.loads(request.content))
        answer = "文字回答。" if len(llm_bodies) == 1 else "语音回答。"
        body = sse(
            completion_chunk({"role": "assistant", "content": answer}),
            completion_chunk({}, finish="stop"),
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    def tts_handler(request: httpx.Request) -> httpx.Response:
        tts_inputs.append(json.loads(request.content)["input"])
        return httpx.Response(200, content=PCM, headers={"content-type": "audio/pcm"})

    asr = FakeASR()
    asr.on_push = {1: [ASRDelta("Nova，", "帮我", 0.1)], 2: [ASRDelta("帮我总结一下", "", 0.2)]}
    asr.on_flush = [[ASRDelta("。", "", 0.2, segment_end=True)]]
    llm = build_realtime_llm(
        cfg, "你是助理", http_client=httpx.AsyncClient(transport=httpx.MockTransport(llm_handler))
    )
    tts = build_tts(
        cfg,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(tts_handler)),
        stop_frame_timeout_s=0.3,
    )
    store = MemoryStore()
    parts = build_parts(cfg, asr_backend=asr, llm=llm, tts=tts, store=store, session_id="s1")
    wire_assistant_recording(parts)
    wire_wake_events(parts.wake, lambda data: asyncio.sleep(0))

    hook = Hook()
    notices: list[tuple[str, str]] = []

    async def notice(level: str, text: str) -> None:
        notices.append((level, text))

    handler = TextInputHandler(
        recorder=parts.recorder, push=hook.push_frame, notice=notice, tts_enabled=True
    )

    @parts.assistant_aggregator.event_handler("on_assistant_turn_started")
    async def _started(_aggregator):
        await handler.set_generating(True)

    @parts.assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def _stopped(_aggregator, message):
        await handler.set_generating(False)

    chunk = np.zeros(16 * 100, dtype="<i2").tobytes()

    def audio() -> InputAudioRawFrame:
        return InputAudioRawFrame(audio=chunk, sample_rate=16000, num_channels=1)

    frames = [
        SleepFrame(sleep=0.1),  # 让识别后端先在后台起来
        CallSignal(lambda: handler.handle("帮我整理一下要点")),
        SleepFrame(sleep=1.2),  # 文字回答生成完
        VADUserStartedSpeakingFrame(),
        audio(),
        SleepFrame(sleep=0.05),
        audio(),
        SleepFrame(sleep=0.05),
        VADUserStoppedSpeakingFrame(),
        SleepFrame(sleep=2.5),
    ]
    down, _ = await run_test(
        Pipeline([hook, *pipeline_processors(FakeTransport(), parts)]),
        frames_to_send=frames,
        pipeline_params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
    )

    # 第一轮（文字）：模型收到带时间和「文字输入」的那一行；语音合成没有被调用
    assert len(llm_bodies) == 2
    first_users = [m["content"] for m in llm_bodies[0]["messages"] if m["role"] == "user"]
    assert first_users == ["[00:00:00 文字输入] 帮我整理一下要点"]
    # 第二轮（语音唤醒）：恢复朗读，而且只朗读第二轮的回答
    assert tts_inputs and all("文字回答" not in text for text in tts_inputs)
    assert any("语音回答" in text for text in tts_inputs)

    # 落库：键入的文字、它的文字回答、语音那一轮、语音回答
    assert [(u.source, u.speaker_idx, u.text) for u in store.utterances] == [
        ("text", -2, "帮我整理一下要点"),
        ("assistant", -1, "文字回答。"),
        ("asr", 0, "Nova，帮我总结一下。"),
        ("assistant", -1, "语音回答。"),
    ]
    assert store.utterances[0].addressed_to_assistant is True
    names = [
        f.data["speaker_name"]
        for f in down
        if isinstance(f, RTVIServerMessageFrame) and f.data["type"] == "utterance"
    ]
    assert names == ["文字输入", "Nova", "未知", "Nova"]
    assert notices == []
    await handler.close()
