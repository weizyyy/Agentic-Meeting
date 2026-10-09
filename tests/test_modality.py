"""应答模态（pipeline/modality.py）：按每一次请求设定只出文字还是照常朗读，工具之后的回答沿用这次请求的模态。

后半用真实的管线（模拟的大模型和语音合成）验证整体联调时发现的问题：文字请求让模型调了工具，
工具结果回来后的那段回答不能被朗读出来。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import numpy as np
from fakes import FakeASR
from pipecat.frames.frames import (
    InputAudioRawFrame,
    LLMConfigureOutputFrame,
    LLMContextFrame,
    LLMMessagesAppendFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.tests.utils import SleepFrame, run_test
from test_bot import PCM, FakeTransport, completion_chunk, sse
from test_meeting_recorder import CallSignal, Hook

from agentic_meeting.pipeline.activity import AssistantActivity
from agentic_meeting.pipeline.bot import (
    AppResources,
    build_parts,
    pipeline_processors,
    wire_activity,
    wire_assistant_recording,
    wire_text_input,
    wire_tool_activity,
    wire_wake_events,
)
from agentic_meeting.pipeline.modality import ModalityGate, TextRequestFrame
from agentic_meeting.pipeline.services import build_realtime_llm, build_tts
from agentic_meeting.pipeline.text_input import TextInputHandler
from agentic_meeting.store.db import Store
from agentic_meeting.types import SPEAKER_TYPED, ASRDelta, Utterance

# --------------------------------------------------------------------------- #
# 闸门本身
# --------------------------------------------------------------------------- #


def context_frame() -> LLMContextFrame:
    return LLMContextFrame(context=LLMContext())


def modes(frames) -> list:
    """按顺序：配置帧记成 skip_tts 的值，上下文帧记成 "request"。"""
    out = []
    for f in frames:
        if isinstance(f, LLMConfigureOutputFrame):
            out.append(f.skip_tts)
        elif isinstance(f, LLMContextFrame):
            out.append("request")
    return out


async def test_voice_request_is_spoken_and_marked_request_is_text_only():
    down, _ = await run_test(
        ModalityGate(),
        frames_to_send=[
            context_frame(),  # 语音请求：前面没有记号
            TextRequestFrame(),
            LLMMessagesAppendFrame(messages=[], run_llm=False),  # 记号和请求之间夹着别的帧也不要紧
            context_frame(),  # 文字请求
            context_frame(),  # 记号只管一次：下一个请求又是语音
        ],
    )
    assert modes(down) == [False, "request", True, "request", False, "request"]
    assert not any(isinstance(f, TextRequestFrame) for f in down)  # 记号不往下传
    assert sum(isinstance(f, LLMMessagesAppendFrame) for f in down) == 1  # 别的帧原样放行


async def test_upstream_context_frames_pass_untouched():
    """工具结果触发的再次生成走向上游：闸门不动它，模型沿用这次请求的模态。"""
    down, up = await run_test(
        ModalityGate(),
        frames_to_send=[context_frame(), TextRequestFrame()],
        frames_to_send_direction=FrameDirection.UPSTREAM,
    )
    assert [type(f) for f in up] == [LLMContextFrame, TextRequestFrame]
    assert not any(isinstance(f, LLMConfigureOutputFrame) for f in [*down, *up])


# --------------------------------------------------------------------------- #
# 文字入口：工具执行期间也算忙
# --------------------------------------------------------------------------- #


class TypedRecorder:
    async def record_typed(self, text):
        return Utterance("s", SPEAKER_TYPED, 1.0, 1.0, text, source="text")

    async def context_text(self, utterance):
        return f"[00:00:01 文字输入] {utterance.text}"


def handler_rig(**kw):
    pushed, notices = [], []

    async def push(frame):
        pushed.append(frame)

    async def notice(level, text):
        notices.append((level, text))

    handler = TextInputHandler(
        recorder=TypedRecorder(), push=push, notice=notice, tts_enabled=True, **kw
    )

    def requests():
        return [f.messages[0]["content"] for f in pushed if isinstance(f, LLMMessagesAppendFrame)]

    return handler, requests, notices


async def test_text_requests_wait_through_the_tool_round():
    handler, requests, notices = handler_rig()
    await handler.handle("第一条")
    await handler.set_generating(True)  # 模型开始生成：发起工具调用
    await handler.tools_started()
    await handler.set_generating(False)  # 这次生成结束了，但工具结果还没回来
    assert handler.busy
    await handler.handle("第二条")
    assert len(requests()) == 1 and [n[0] for n in notices] == ["info"]  # 排队
    await handler.set_generating(True)  # 工具结果回来后的再次生成
    await handler.set_generating(False)
    assert [r.split("] ")[1] for r in requests()] == ["第一条", "第二条"]
    await handler.close()


async def test_tool_round_that_never_resumes_does_not_block_forever():
    handler, requests, _ = handler_rig(busy_timeout_secs=0.05)
    await handler.handle("第一条")
    await handler.set_generating(True)
    await handler.tools_started()
    await handler.set_generating(False)
    await handler.handle("第二条")
    assert len(requests()) == 1
    await asyncio.sleep(0.15)  # 工具一直没有带来下一次生成
    assert len(requests()) == 2
    await handler.close()


async def test_activity_stays_busy_through_the_tool_round():
    activity = AssistantActivity(idle_delay_secs=0)
    seen: list[bool] = []
    activity.subscribe(seen.append)
    await activity.request_sent()
    await activity.set_generating(True)
    await activity.tools_started()
    await activity.set_generating(False)  # 两次生成之间
    assert seen == [True] and activity.busy and not activity.responding
    await activity.set_generating(True)
    await activity.set_generating(False)
    assert seen == [True, False]


async def test_activity_tool_round_has_a_watchdog():
    activity = AssistantActivity(idle_delay_secs=0, awaiting_timeout_secs=0.03)
    await activity.set_generating(True)
    await activity.tools_started()
    await activity.set_generating(False)
    assert activity.busy
    await asyncio.sleep(0.1)
    assert not activity.busy


async def test_wire_tool_activity_notifies_both():
    class Emitter:
        def event_handler(self, name):
            def decorator(fn):
                self.name, self.fn = name, fn
                return fn

            return decorator

    calls: list[str] = []
    activity = SimpleNamespace(tools_started=lambda: _record(calls, "activity"))
    text_input = SimpleNamespace(tools_started=lambda: _record(calls, "text"))
    llm = Emitter()
    wire_tool_activity(llm, activity, text_input)
    assert llm.name == "on_function_calls_started"
    await llm.fn(llm, ["call"])
    assert calls == ["activity", "text"]
    wire_tool_activity(llm, activity)  # 没有文字入口也能接
    await llm.fn(llm, ["call"])
    assert calls == ["activity", "text", "activity"]


async def _record(calls, name):
    calls.append(name)


# --------------------------------------------------------------------------- #
# 真实的管线：文字请求 + 工具 → 全程不朗读；随后的语音请求 + 工具 → 朗读
# --------------------------------------------------------------------------- #


def tool_call_chunk(name: str) -> dict:
    call = {
        "index": 0,
        "id": "call_abc",
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }
    return completion_chunk({"role": "assistant", "tool_calls": [call]})


async def test_tool_round_keeps_the_modality_of_its_request(make_cfg, tmp_path):
    cfg = make_cfg()
    cfg.session.assistant_name = "Nova"
    cfg.session.wake_aliases = []
    cfg.turn.smart_turn = False
    cfg.turn.wake_timeout_secs = 5.0
    cfg.turn.single_activation = True
    llm_bodies: list[dict] = []
    tts_inputs: list[str] = []

    def llm_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        llm_bodies.append(body)
        after_tool = body["messages"][-1]["role"] == "tool"
        if after_tool:
            answer = "文字那一轮的回答。" if len(llm_bodies) == 2 else "语音那一轮的回答。"
            text = sse(
                completion_chunk({"role": "assistant", "content": answer}),
                completion_chunk({}, finish="stop"),
            )
        else:
            text = sse(tool_call_chunk("get_digest"), completion_chunk({}, finish="tool_calls"))
        return httpx.Response(200, content=text, headers={"content-type": "text/event-stream"})

    def tts_handler(request: httpx.Request) -> httpx.Response:
        tts_inputs.append(json.loads(request.content)["input"])
        return httpx.Response(200, content=PCM, headers={"content-type": "audio/pcm"})

    store = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    try:
        session = await store.create_session(now=1000.0)
        asr = FakeASR()
        asr.on_push = {1: [ASRDelta("Nova，", "整理", 0.1)], 2: [ASRDelta("整理一下", "", 0.2)]}
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
        parts = build_parts(
            cfg, asr_backend=asr, llm=llm, tts=tts, store=store, session_id=session.id
        )
        wire_assistant_recording(parts)
        wire_wake_events(parts.wake, lambda data: asyncio.sleep(0))
        resources = AppResources(
            cfg,
            store=store,
            sessions=SimpleNamespace(
                live=SimpleNamespace(session=session, recorder=parts.recorder)
            ),
        )
        hook = Hook()

        async def notice(level, text):
            pass

        activity = AssistantActivity(idle_delay_secs=0)
        busy_log: list[bool] = []
        activity.subscribe(busy_log.append)
        handler = TextInputHandler(
            recorder=parts.recorder,
            push=hook.push_frame,
            notice=notice,
            tts_enabled=True,
            on_dispatch=activity.request_sent,
        )
        wire_text_input(
            SimpleNamespace(event_handler=lambda name: lambda fn: fn),
            parts.assistant_aggregator,
            parts.recorder,
            handler,
            notice,
        )
        wire_activity(activity, parts.wake, parts.assistant_aggregator, parts.recorder)
        wire_tool_activity(parts.llm, activity, handler)

        async def attach_resources():
            parts.llm.pipeline_worker._app_resources = resources

        chunk = np.zeros(16 * 100, dtype="<i2").tobytes()

        def audio() -> InputAudioRawFrame:
            return InputAudioRawFrame(audio=chunk, sample_rate=16000, num_channels=1)

        frames = [
            SleepFrame(sleep=0.1),
            CallSignal(attach_resources),
            CallSignal(lambda: handler.handle("帮我整理一下要点")),
            SleepFrame(sleep=1.5),  # 文字请求：工具调用 + 回答
            VADUserStartedSpeakingFrame(),
            audio(),
            SleepFrame(sleep=0.05),
            audio(),
            SleepFrame(sleep=0.05),
            VADUserStoppedSpeakingFrame(),
            SleepFrame(sleep=3.0),  # 语音请求：工具调用 + 回答 + 朗读
        ]
        await run_test(
            Pipeline([hook, *pipeline_processors(FakeTransport(), parts)]),
            frames_to_send=frames,
            pipeline_params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
        )
        await handler.close()
        await activity.close()

        assert len(llm_bodies) == 4  # 两个请求，各两次生成
        # 文字那一轮：工具之后的回答也不朗读（曾经在这里被念出来）
        assert all("文字那一轮" not in text for text in tts_inputs)
        # 语音那一轮：工具之后的回答照常朗读
        assert any("语音那一轮" in text for text in tts_inputs)
        said = [
            (n.utterance.source, n.utterance.text)
            for n in await store.list_utterances(session.id, tail=10)
        ]
        assert ("assistant", "文字那一轮的回答。") in said
        assert ("assistant", "语音那一轮的回答。") in said
        # 文字那一轮从发出到答完，中间（工具执行期间）没有被判成「闲」
        assert busy_log[0] is True
        first_idle = busy_log.index(False)
        assert len(llm_bodies) >= 2 and first_idle >= 1
    finally:
        await store.close()
