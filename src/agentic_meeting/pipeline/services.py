"""实时模型服务与语音合成服务：把配置翻译成 Pipecat 的服务对象。

写法见 docs/pipecat-notes.md §5（已对照 Pipecat 1.12.0 源码）。

**这里不判断实时模型的接入方式**：llama_server 与 openai_api 的差别已经收敛在
``cfg.realtime_llm`` 的几个成员里（``active``、``request_extra_body()``、``supports_developer_role``），
照着用即可（docs/interfaces.md §1）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

from openai import APIError, AsyncOpenAI, DefaultAsyncHttpxClient
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.processors.aggregators.async_tool_messages import ASYNC_TOOL_INSTRUCTIONS
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.utils.tracing.service_decorators import traced_tts
from pipecat.utils.types import assert_given

from agentic_meeting.config import AppConfig, is_loopback, secret
from agentic_meeting.pipeline.async_tools import localize_async_tool_messages

# 采样参数里能直接放进 Settings 的几项；top_k 不是 OpenAI SDK 的标准参数，
# 由 ``request_extra_body()`` 放进 extra_body，不要再传给 Settings.top_k。
_SAMPLING_FIELDS = ("temperature", "top_p", "presence_penalty", "max_tokens")


def _local_http_client(base_url: str) -> DefaultAsyncHttpxClient | None:
    """本机地址不走系统代理（与 services/supervisor.py 的探测一致）。

    设了 HTTP_PROXY 的机器上，经代理访问 127.0.0.1 会连不上。远端地址返回 None，沿用 SDK 默认
    （照系统设置走代理，与用户在这台机器上访问该地址的方式一致）。
    """
    return DefaultAsyncHttpxClient(trust_env=False) if is_loopback(base_url) else None


class RealtimeLLMService(OpenAILLMService):
    """实时模型服务。两种接入方式共用这一个类，差别全部来自配置。"""

    def __init__(self, *, supports_developer_role: bool, http_client: Any = None, **kwargs):
        # create_client 在基类构造函数里被调用，要用的东西得先放好。
        self._injected_http_client = http_client
        super().__init__(**kwargs)
        # 基类里这是类属性（默认 True）。很多服务不认识 "developer" 角色（llama-server 加载的本地模型
        # 基本都不认识）；设为 False 后 Pipecat 会把它转成 "user"。晚到的异步工具结果正是以
        # developer 消息注入的，所以这个值必须按配置设对。
        self.supports_developer_role = supports_developer_role

    def create_client(self, **kwargs):
        if self._injected_http_client is None:
            return super().create_client(**kwargs)
        return AsyncOpenAI(
            api_key=kwargs.get("api_key"),
            base_url=kwargs.get("base_url"),
            organization=kwargs.get("organization"),
            project=kwargs.get("project"),
            default_headers=kwargs.get("default_headers"),
            http_client=self._injected_http_client,
        )

    def _compose_system_instruction(self):
        """去掉 Pipecat 自动附加的那段异步工具说明（英文）。

        只要有「不随打断取消」的工具（我们的 ``delegate_task``），Pipecat 就会在系统提示词后面追加一段固定的英文，
        要求「先回答用户刚说的话，再把晚到的结果附在同一次回复的末尾，绝不单独成一次回复」。我们的流程正相反：
        任务结果回来时专门触发一次生成，让助理用一两句话简报。怎么对待晚到的结果，由我们自己的提示词
        （``config/prompts/realtime_tasks.md``）说明。
        """
        super()._compose_system_instruction()
        composed = self._settings.system_instruction
        if isinstance(composed, str) and ASYNC_TOOL_INSTRUCTIONS in composed:
            cleaned = composed.replace(ASYNC_TOOL_INSTRUCTIONS, "").rstrip() or None
            self._settings.system_instruction = cleaned
            self._composed_system_instruction = cleaned

    def build_chat_completion_params(self, params_from_context):
        """发请求前把异步工具的协议消息改写成中文行（pipeline/async_tools.py）。正式请求和预热都经过这里。"""
        params = super().build_chat_completion_params(params_from_context)
        messages = params.get("messages")
        if isinstance(messages, list):
            params["messages"] = localize_async_tool_messages(messages)
        return params

    # ---- 上下文预热（docs/architecture.md §6，pipeline/context.py） ----

    def request_params(self, context: LLMContext) -> dict[str, Any]:
        """此刻对 ``context`` 发正式请求会用的全部参数（消息、工具、采样、附加字段）。

        和基类 ``get_chat_completions`` 的前半段是同一段逻辑，所以预热请求的前缀与正式请求逐字一致。
        """
        params_from_context = self.get_llm_adapter().get_llm_invocation_params(
            context,
            system_instruction=assert_given(self._settings.system_instruction),
            convert_developer_to_user=not self.supports_developer_role,
        )
        return self.build_chat_completion_params(params_from_context)

    async def warm_cache(self, context: LLMContext) -> None:
        """预热：与正式请求相同的消息和工具，只生成 1 个 token、不流式。结果不要，只为让服务端把前缀算好。"""
        params = self.request_params(context)
        params["stream"] = False
        params.pop("stream_options", None)
        params.pop("max_completion_tokens", None)
        params["max_tokens"] = 1
        await self._client.chat.completions.create(**params)

    def static_prompt_tokens_text(self, context: LLMContext) -> str:
        """系统提示词 + 工具定义的文本，只用来估算它们占多少 token。"""
        tools = self.request_params(context).get("tools")
        system = assert_given(self._settings.system_instruction) or ""
        return system + (json.dumps(tools, ensure_ascii=False) if isinstance(tools, list) else "")


def build_realtime_llm(
    cfg: AppConfig,
    system_prompt: str,
    *,
    background: bool = False,
    http_client: Any = None,
) -> RealtimeLLMService:
    """按配置创建实时模型服务。

    ``background=True`` 用于后台任务（画面摘要、纪要）：另建一个实例，``extra_body`` 里的槽位号不同，
    llama.cpp 部署方式下两者各占一个槽位、互不冲掉对方的前缀缓存。采样参数为 ``None`` 的不发送。
    后台实例不带配置里的 ``max_tokens``：那是给口头应答定的上限，后台请求每次自己给
    （``run_inference(max_tokens=…)`` 写的是 ``max_completion_tokens``，两个同时发出去含义就不明确了）。
    """
    rt = cfg.realtime_llm
    endpoint = rt.active
    sampling = {
        name: value
        for name in _SAMPLING_FIELDS
        if (value := getattr(endpoint.sampling, name)) is not None
        and not (background and name == "max_tokens")
    }
    return RealtimeLLMService(
        supports_developer_role=rt.supports_developer_role,
        base_url=endpoint.base_url,
        api_key=secret(endpoint.api_key_env) or "none",  # SDK 要求非空；不需要密钥的服务会忽略它
        http_client=http_client or _local_http_client(endpoint.base_url),
        settings=RealtimeLLMService.Settings(
            model=endpoint.model,
            system_instruction=system_prompt,
            extra={"extra_body": rt.request_extra_body(background=background)},
            **sampling,
        ),
    )


def build_agent_llm(cfg: AppConfig, *, http_client: Any = None) -> RealtimeLLMService | None:
    """指向后台 agent 那个远端模型的服务（``screen.caption_provider = "agent_llm"`` 时给画面摘要用）。

    配置里没填地址或模型名时返回 ``None``。不带实时模型的采样参数和附加字段（那些是为实时模型配的）；
    只带 ``agent.extra_body``（一般用来指定思考的强度）。这个模型通常会先思考，思考也占输出额度，
    所以用它直接生成时输出上限要给宽（``agent.generation_max_tokens``，由调用方传）。
    """
    agent = cfg.agent
    if not agent.base_url or not agent.model:
        return None
    return RealtimeLLMService(
        supports_developer_role=False,
        base_url=agent.base_url,
        api_key=secret(agent.api_key_env) or "none",
        http_client=http_client or _local_http_client(agent.base_url),
        settings=RealtimeLLMService.Settings(
            model=agent.model,
            system_instruction="",
            extra={"extra_body": dict(agent.extra_body)},
        ),
    )


def caption_provider(cfg: AppConfig) -> str | None:
    """画面摘要由谁生成：``"realtime_llm"`` / ``"agent_llm"``；不生成返回 ``None``。

    不生成的情况：截图或摘要在配置里关了；选中的那个模型不识图。
    """
    screen = cfg.screen
    if not (screen.enabled and screen.caption):
        return None
    if screen.caption_provider == "agent_llm":
        return "agent_llm" if cfg.agent.supports_vision else None
    return "realtime_llm" if cfg.realtime_llm.active.supports_vision else None


class LocalTTSService(OpenAITTSService):
    """本地语音合成服务（``tts-server``，OpenAI 兼容的 ``/v1/audio/speech``）。

    不能直接用 ``OpenAITTSService``：它的 ``run_tts`` 会检查音色名是否在 OpenAI 官方的固定列表里，
    本地音色名不在其中。这里重写 ``run_tts``：去掉那个检查，``voice`` 直接用配置值，并通过
    ``extra_body`` 传语言。其余照抄父类：``response_format`` 固定为 ``pcm``，边收边产出音频帧，
    首包到达时结束首字节延迟的计时。
    """

    def __init__(self, *, language: str = "", **kwargs):
        # Pipecat 默认在「连续 3 句没有产出音频」后把服务判为不可用，此后整个会话都不再给它活干。
        # 本地 tts-server 重启一下就能恢复，所以关掉这条自动判死（0 = 只逐句报告，不判死）；
        # 400 / 404 这类配置错误仍会由错误类别判为永久。
        kwargs.setdefault("max_consecutive_zero_audio_contexts", 0)
        super().__init__(**kwargs)
        self._language = language
        # 一句话的合成失败就让它失败：SDK 默认的重试（带退避）只会让这一句更晚才报错，
        # 下一句本来就是一次新的尝试。
        self._client = self._client.with_options(max_retries=0)

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        voice = assert_given(self._settings.voice)
        if not voice:
            yield ErrorFrame(error="语音合成的音色（tts.voice）没有设置")
            return
        params: dict[str, Any] = {
            "input": text,
            "model": self._settings.model,
            "voice": voice,
            "response_format": "pcm",
        }
        if self._settings.instructions:
            params["instructions"] = self._settings.instructions
        if self._settings.speed:
            params["speed"] = self._settings.speed
        if self._language:
            params["extra_body"] = {"language": self._language}

        try:
            async with self._client.audio.speech.with_streaming_response.create(**params) as r:
                await self.start_tts_usage_metrics(text)
                async for chunk in r.iter_bytes(self.chunk_size):
                    if chunk:
                        await self.stop_ttfb_metrics()
                        yield TTSAudioRawFrame(chunk, self.sample_rate, 1, context_id=context_id)
        except APIError as e:
            # 连接失败、超时、非 2xx：文字应答照常，只是没有声音（architecture.md §9）。
            # 带上异常让 Pipecat 判断类别——暂时连不上不会把服务标记为不可用，服务恢复后自动有声音；
            # 400 / 404 这类配置错误才会被判为永久。
            yield ErrorFrame(error=f"语音合成请求失败：{e}", exception=e)


def build_tts(cfg: AppConfig, *, http_client: Any = None, **kwargs: Any) -> LocalTTSService:
    """按配置创建语音合成服务。``kwargs`` 原样交给 ``TTSService``（调参、测试用）。"""
    tts = cfg.tts
    return LocalTTSService(
        base_url=tts.base_url,
        api_key=secret(tts.api_key_env) or "local",
        http_client=http_client or _local_http_client(tts.base_url),
        settings=LocalTTSService.Settings(model=tts.model, voice=tts.voice),
        sample_rate=tts.sample_rate,
        language=tts.language,
        **kwargs,
    )
