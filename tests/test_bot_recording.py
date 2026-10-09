"""管线组装（pipeline/bot.py）与会议记录器的联动：整场对话被记录下来，模型看到的上下文里有带说话人和时间的行。

假识别后端 + 假的存储 + 模拟的大模型与语音合成，从「叫名字」走到「朗读应答」，再检查落库和上下文。
"""

from __future__ import annotations

import json

import httpx
import numpy as np
import pytest
from fakes import FakeASR
from pipecat.frames.frames import (
    InputAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame
from pipecat.tests.utils import SleepFrame, run_test
from test_bot import PCM, FakeTransport, completion_chunk, sse

from agentic_meeting.pipeline.bot import (
    build_parts,
    pipeline_processors,
    wire_assistant_recording,
)
from agentic_meeting.pipeline.services import build_realtime_llm, build_tts
from agentic_meeting.store.db import default_speaker_name
from agentic_meeting.types import ASRDelta, Utterance


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


async def test_the_whole_exchange_is_recorded_and_the_model_sees_the_meeting_line(cfg):
    llm_bodies: list[dict] = []

    def llm_handler(request: httpx.Request) -> httpx.Response:
        llm_bodies.append(json.loads(request.content))
        body = sse(
            completion_chunk({"role": "assistant", "content": "好的，"}),
            completion_chunk({"content": "马上整理。"}),
            completion_chunk({}, finish="stop"),
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    def tts_handler(request: httpx.Request) -> httpx.Response:
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
    store = MemoryStore()
    parts = build_parts(cfg, asr_backend=asr, llm=llm, tts=tts, store=store, session_id="s1")
    wire_assistant_recording(parts)

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
    down, _ = await run_test(
        Pipeline(pipeline_processors(FakeTransport(), parts)),
        frames_to_send=frames,
        pipeline_params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
    )

    # 落库：用户的话 + 助理的话
    assert [(u.source, u.speaker_idx, u.text) for u in store.utterances] == [
        ("asr", 0, "Nova，帮我总结一下。"),
        ("assistant", -1, "好的，马上整理。"),
    ]
    assert all(u.session_id == "s1" for u in store.utterances)
    kinds = [
        (f.data["source"], f.data["speaker_name"])
        for f in down
        if isinstance(f, RTVIServerMessageFrame) and f.data["type"] == "utterance"
    ]
    assert kinds == [("asr", "未知"), ("assistant", "Nova")]

    # 模型看到的上下文：先是带时间和说话人的行（记录器追加的），再是触发这一轮的原话
    assert len(llm_bodies) == 1
    users = [m["content"] for m in llm_bodies[0]["messages"] if m["role"] == "user"]
    assert users == ["[00:00:00 未知] Nova，帮我总结一下。", "Nova，帮我总结一下。"]
