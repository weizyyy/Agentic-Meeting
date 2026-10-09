"""实时模型服务与语音合成服务（pipeline/services.py）。

不连任何真实服务：HTTP 一律用 ``httpx.MockTransport``（或本机回环上的一个小假服务）。
实时模型的两种接入方式各测一遍——工厂函数里没有 ``if mode``，差别全部来自配置对象。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from pipecat.adapters.services.open_ai_adapter import openai_is_given
from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame, TTSSpeakFrame
from pipecat.pipeline.worker import PipelineParams
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import SleepFrame, run_test
from pipecat.utils.text.base_text_aggregator import AggregationType
from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator

from agentic_meeting.pipeline.services import (
    LocalTTSService,
    RealtimeLLMService,
    build_realtime_llm,
    build_tts,
)

PROMPT = "你是组会助理，回答要简短。"
PCM = bytes(range(256)) * 20  # 5120 字节，足够分成多块


def completion(text: str = "好的") -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "fake",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}
        ],
    }


class Recorder:
    """记录收到的请求体的 MockTransport 处理函数。"""

    def __init__(self, response: httpx.Response | None = None):
        self.bodies: list[dict] = []
        self.paths: list[str] = []
        self.response = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        self.bodies.append(json.loads(request.content))
        if self.response is not None:
            return self.response
        return httpx.Response(200, json=completion())

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


@pytest.fixture
def llama_cfg(make_cfg):
    cfg = make_cfg()
    cfg.realtime_llm.mode = "llama_server"
    ep = cfg.realtime_llm.llama_server
    ep.base_url = "http://127.0.0.1:8080/v1"
    ep.model = "fake-local-llm"
    ep.extra_body = {"chat_template_kwargs": {"enable_thinking": False}}
    ep.sampling.temperature = 0.7
    ep.sampling.top_p = None
    ep.sampling.top_k = 20
    ep.sampling.presence_penalty = None
    ep.sampling.max_tokens = 256
    ep.realtime_slot, ep.background_slot = 0, 1
    return cfg


@pytest.fixture
def api_cfg(make_cfg):
    cfg = make_cfg()
    cfg.realtime_llm.mode = "openai_api"
    ep = cfg.realtime_llm.openai_api
    ep.base_url = "https://llm.example.com/v1"
    ep.model = "fake-remote-llm"
    ep.api_key_env = "FAKE_REALTIME_KEY"
    ep.extra_body = {"reasoning_effort": "minimal"}
    ep.sampling.temperature = None
    ep.sampling.top_p = 0.9
    ep.sampling.top_k = None
    ep.sampling.presence_penalty = 0.1
    ep.sampling.max_tokens = None
    ep.supports_developer_role = False
    return cfg


# --------------------------------------------------------------------------- #
# 实时模型：Settings
# --------------------------------------------------------------------------- #


def test_unset_sampling_parameters_are_not_sent(llama_cfg):
    service = build_realtime_llm(llama_cfg, PROMPT)
    s = service._settings
    assert s.temperature == 0.7 and s.max_tokens == 256
    assert not openai_is_given(s.top_p)  # 配置里是 None：不传，由服务端默认
    assert not openai_is_given(s.presence_penalty)
    assert s.top_k is None  # top_k 走 extra_body，不走 Settings
    assert s.model == "fake-local-llm"
    assert s.system_instruction == PROMPT


def test_llama_server_extra_body_carries_slot_and_cache_prompt(llama_cfg):
    realtime = build_realtime_llm(llama_cfg, PROMPT)
    background = build_realtime_llm(llama_cfg, PROMPT, background=True)

    assert realtime._settings.extra == {
        "extra_body": {
            "chat_template_kwargs": {"enable_thinking": False},  # 用户配置的原样带上
            "top_k": 20,
            "id_slot": 0,
            "cache_prompt": True,
        }
    }
    assert background._settings.extra["extra_body"]["id_slot"] == 1  # 后台任务用另一个槽位
    assert background._settings.extra["extra_body"]["cache_prompt"] is True


def test_openai_api_extra_body_is_only_what_the_user_configured(api_cfg):
    service = build_realtime_llm(api_cfg, PROMPT)
    assert service._settings.extra == {"extra_body": {"reasoning_effort": "minimal"}}
    # 后台实例的请求字段相同（分开只是为了能单独取消后台请求）
    background = build_realtime_llm(api_cfg, PROMPT, background=True)
    assert background._settings.extra == service._settings.extra


def test_openai_api_takes_address_model_and_sampling_from_its_own_section(api_cfg, monkeypatch):
    monkeypatch.setenv("FAKE_REALTIME_KEY", "sk-fake-for-test")
    service = build_realtime_llm(api_cfg, PROMPT)

    assert service._settings.model == "fake-remote-llm"
    assert str(service._client.base_url) == "https://llm.example.com/v1/"
    assert service._client.api_key == "sk-fake-for-test"
    assert service._settings.top_p == 0.9
    assert service._settings.presence_penalty == 0.1
    assert not openai_is_given(service._settings.temperature)
    assert not openai_is_given(service._settings.max_tokens)


def test_api_key_falls_back_to_a_placeholder_when_none_is_configured(llama_cfg, monkeypatch):
    monkeypatch.delenv("FAKE_REALTIME_KEY", raising=False)
    # llama-server 不需要密钥：SDK 要求非空，给个占位
    assert build_realtime_llm(llama_cfg, PROMPT)._client.api_key == "none"


@pytest.mark.parametrize("supported", [True, False])
def test_developer_role_support_follows_the_configuration(api_cfg, supported):
    api_cfg.realtime_llm.openai_api.supports_developer_role = supported
    assert build_realtime_llm(api_cfg, PROMPT).supports_developer_role is supported


def test_llama_server_never_supports_the_developer_role(llama_cfg):
    assert build_realtime_llm(llama_cfg, PROMPT).supports_developer_role is False


def test_the_class_does_not_leak_the_flag_between_instances(api_cfg):
    api_cfg.realtime_llm.openai_api.supports_developer_role = True
    first = build_realtime_llm(api_cfg, PROMPT)
    api_cfg.realtime_llm.openai_api.supports_developer_role = False
    second = build_realtime_llm(api_cfg, PROMPT)
    assert (first.supports_developer_role, second.supports_developer_role) == (True, False)
    assert RealtimeLLMService.supports_developer_role is True  # 基类默认值没被改


# --------------------------------------------------------------------------- #
# 实时模型：真正发出去的请求
# --------------------------------------------------------------------------- #


async def test_request_body_of_the_llama_server_mode(llama_cfg):
    recorder = Recorder()
    service = build_realtime_llm(llama_cfg, PROMPT, http_client=recorder.client())
    reply = await service.run_inference(LLMContext(messages=[{"role": "user", "content": "你好"}]))

    assert reply == "好的"
    body = recorder.bodies[0]
    assert recorder.paths == ["/v1/chat/completions"]
    assert body["model"] == "fake-local-llm"
    assert body["messages"][0] == {"role": "system", "content": PROMPT}
    assert (body["temperature"], body["max_tokens"]) == (0.7, 256)
    # extra_body 里的字段被 SDK 合并到了请求体的顶层
    assert (body["id_slot"], body["cache_prompt"], body["top_k"]) == (0, True, 20)
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "top_p" not in body and "presence_penalty" not in body  # 没配置的不出现


async def test_request_body_of_the_openai_api_mode(api_cfg):
    recorder = Recorder()
    service = build_realtime_llm(api_cfg, PROMPT, http_client=recorder.client())
    await service.run_inference(LLMContext(messages=[{"role": "user", "content": "你好"}]))

    body = recorder.bodies[0]
    assert body["model"] == "fake-remote-llm"
    assert body["reasoning_effort"] == "minimal"
    assert (body["top_p"], body["presence_penalty"]) == (0.9, 0.1)
    for absent in ("id_slot", "cache_prompt", "top_k", "temperature"):
        assert absent not in body


@pytest.mark.parametrize(("supported", "role"), [(True, "developer"), (False, "user")])
async def test_developer_messages_are_converted_only_when_unsupported(api_cfg, supported, role):
    api_cfg.realtime_llm.openai_api.supports_developer_role = supported
    recorder = Recorder()
    service = build_realtime_llm(api_cfg, PROMPT, http_client=recorder.client())
    context = LLMContext(
        messages=[
            {"role": "user", "content": "查一下"},
            {"role": "developer", "content": "后台任务 t1 已完成"},
        ]
    )
    await service.run_inference(context)
    assert recorder.bodies[0]["messages"][-1]["role"] == role


# --------------------------------------------------------------------------- #
# 本机地址不走系统代理（与 services/supervisor.py 一致）
# --------------------------------------------------------------------------- #


class _LoopbackServer:
    """回环上的假服务：聊天接口回一条补全，语音接口回一段 PCM。"""

    def __init__(self):
        self.paths: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.paths.append(self.path)
                if self.path.endswith("/audio/speech"):
                    data, kind = PCM, "audio/pcm"
                else:
                    data, kind = json.dumps(completion("来自回环")).encode(), "application/json"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def loopback(monkeypatch):
    # 系统代理指向一个没人监听的端口：如果请求被送去代理，必然失败。
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    server = _LoopbackServer()
    yield server
    server.close()


async def test_llm_to_a_loopback_address_ignores_the_system_proxy(llama_cfg, loopback):
    llama_cfg.realtime_llm.llama_server.base_url = loopback.url
    service = build_realtime_llm(llama_cfg, PROMPT)
    reply = await service.run_inference(LLMContext(messages=[{"role": "user", "content": "嗨"}]))
    assert reply == "来自回环"


async def test_tts_to_a_loopback_address_ignores_the_system_proxy(make_cfg, loopback):
    cfg = make_cfg()
    cfg.tts.base_url = loopback.url
    down, up = await run_tts(build_tts(cfg, **FAST_STOP), "你好。")
    assert [f for f in up if isinstance(f, ErrorFrame)] == []
    assert b"".join(f.audio for f in down if isinstance(f, TTSAudioRawFrame)) == PCM


# --------------------------------------------------------------------------- #
# 语音合成
# --------------------------------------------------------------------------- #


# Pipecat 在一句话最后一块音频之后等这么久才收尾（默认 3 秒）；测试里缩短，免得每条用例白等。
FAST_STOP = {"stop_frame_timeout_s": 0.2}


async def run_tts(service: LocalTTSService, text: str):
    return await run_test(
        service,
        frames_to_send=[TTSSpeakFrame(text=text), SleepFrame(sleep=0.3)],
        pipeline_params=PipelineParams(audio_out_sample_rate=24000),
    )


def pcm_response() -> httpx.Response:
    return httpx.Response(200, content=PCM, headers={"content-type": "audio/pcm"})


def test_build_tts_translates_the_configuration(make_cfg, monkeypatch):
    monkeypatch.setenv("FAKE_TTS_KEY", "tts-secret")
    cfg = make_cfg()
    cfg.tts.api_key_env = "FAKE_TTS_KEY"
    cfg.tts.base_url = "http://tts.example.com/v1"
    service = build_tts(cfg, **FAST_STOP)

    assert isinstance(service, LocalTTSService)
    assert service._settings.model == cfg.tts.model
    assert service._settings.voice == "fake-voice"
    assert service._init_sample_rate == cfg.tts.sample_rate
    assert str(service._client.base_url) == "http://tts.example.com/v1/"
    assert service._client.api_key == "tts-secret"


def test_tts_api_key_falls_back_to_a_placeholder(make_cfg):
    assert build_tts(make_cfg(), **FAST_STOP)._client.api_key == "local"


async def test_run_tts_request_uses_any_voice_name_pcm_and_the_language(make_cfg):
    recorder = Recorder(pcm_response())
    cfg = make_cfg()
    cfg.tts.voice = "某个不在 OpenAI 列表里的音色"
    service = build_tts(cfg, http_client=recorder.client(), **FAST_STOP)
    down, up = await run_tts(service, "你好，我是助理。")

    assert [f for f in up if isinstance(f, ErrorFrame)] == []  # 不会因为音色不在官方列表里被拒绝
    assert recorder.paths == ["/v1/audio/speech"]
    body = recorder.bodies[0]
    assert body["input"] == "你好，我是助理。"
    assert body["model"] == cfg.tts.model
    assert body["voice"] == "某个不在 OpenAI 列表里的音色"
    assert body["response_format"] == "pcm"
    assert body["language"] == cfg.tts.language  # extra_body 被合并到顶层
    assert "speed" not in body and "instructions" not in body


async def test_run_tts_turns_pcm_chunks_into_audio_frames(make_cfg):
    service = build_tts(make_cfg(), http_client=Recorder(pcm_response()).client(), **FAST_STOP)
    down, _ = await run_tts(service, "你好。")

    audio = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    assert audio and all(f.sample_rate == 24000 and f.num_channels == 1 for f in audio)
    assert b"".join(f.audio for f in audio) == PCM


@pytest.mark.parametrize("status", [500, 404])
async def test_run_tts_failure_becomes_an_error_frame_and_the_pipeline_survives(make_cfg, status):
    service = build_tts(
        make_cfg(), http_client=Recorder(httpx.Response(status)).client(), **FAST_STOP
    )
    down, up = await run_tts(service, "你好。")

    # Pipecat 还会在这句话收尾时追加一条「没有产出音频」的通用错误，所以不数条数
    assert any("语音合成请求失败" in f.error for f in up if isinstance(f, ErrorFrame))
    assert [f for f in down if isinstance(f, TTSAudioRawFrame)] == []


async def test_a_temporary_tts_outage_does_not_disable_the_service(make_cfg):
    # tts-server 暂时连不上（比如正在重启）：这几句没有声音，服务恢复后下一句要有。
    # Pipecat 默认连续 3 句没有音频就把服务判死、整个会话不再给它活干；这里连失败 4 句来确认没有这回事。
    calls = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 4:
            raise httpx.ConnectError("连接被拒绝")
        return pcm_response()

    client = httpx.AsyncClient(transport=httpx.MockTransport(flaky))
    service = build_tts(make_cfg(), http_client=client, **FAST_STOP)
    frames = []
    for i in range(5):
        frames += [TTSSpeakFrame(text=f"第{i}句。"), SleepFrame(sleep=0.2)]
    down, up = await run_test(
        service, frames_to_send=frames, pipeline_params=PipelineParams(audio_out_sample_rate=24000)
    )

    assert calls == 5  # 失败不重试（max_retries=0），每句各一次请求
    assert sum(isinstance(f, ErrorFrame) for f in up) >= 4
    assert service.is_usable
    assert b"".join(f.audio for f in down if isinstance(f, TTSAudioRawFrame)) == PCM


async def test_a_tts_configuration_error_does_disable_the_service(make_cfg):
    # 音色名不对之类的配置错误（服务端回 404）重试也没用：这个才该被判为不可用。
    service = build_tts(make_cfg(), http_client=Recorder(httpx.Response(404)).client(), **FAST_STOP)
    await run_tts(service, "你好。")
    assert not service.is_usable


# --------------------------------------------------------------------------- #
# 中文断句：Pipecat 默认聚合器能按中文标点断句（pipecat-notes.md §5）
# --------------------------------------------------------------------------- #


async def sentences_of(text: str, chunk: int) -> list[str]:
    aggregator = SimpleTextAggregator(aggregation_type=AggregationType.SENTENCE)
    out: list[str] = []
    for i in range(0, len(text), chunk):
        async for aggregation in aggregator.aggregate(text[i : i + chunk]):
            out.append(aggregation.text)
    rest = await aggregator.flush()
    if rest:
        out.append(rest.text)
    return out


@pytest.mark.parametrize("chunk", [1, 3, 7])  # 模型是一个 token 一个 token 吐字的，切法不应影响结果
async def test_default_sentence_aggregation_splits_chinese_at_full_width_punctuation(chunk):
    text = "好的，我来整理一下。刚才提到三点：第一，数据量不够；第二，指标要重新定义！你们觉得呢？"
    assert await sentences_of(text, chunk) == [
        "好的，我来整理一下。",
        "刚才提到三点：第一，数据量不够；",  # 全角分号也算句末
        "第二，指标要重新定义！",
        "你们觉得呢？",
    ]


@pytest.mark.parametrize("chunk", [1, 4])
async def test_default_sentence_aggregation_leaves_english_terms_and_numbers_alone(chunk):
    text = "已经把 Dr. Wang 的意见记下来了。下一步是 v2.0 的评估，准确率 3.5 分。"
    assert await sentences_of(text, chunk) == [
        "已经把 Dr. Wang 的意见记下来了。",
        "下一步是 v2.0 的评估，准确率 3.5 分。",
    ]
