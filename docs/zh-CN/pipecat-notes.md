# Pipecat 集成说明

[English](../pipecat-notes.md) · **简体中文**

本文说明 Agentic-Meeting 对 Pipecat **1.12.0** 的用法。Pipecat 在 1.x 期间接口变化很大
（`PipelineTask` 改为 `PipelineWorker`、`PipelineRunner` 改为 `WorkerRunner`、轮次策略重写、
设置项改为 `Settings` 对象），针对早期版本的示例往往不再适用。

文中的导入路径均可在本项目的虚拟环境中执行，签名取自已安装的源码；路径相对于 `site-packages/pipecat/`。
本文没有涉及的内容，请阅读已安装的源码，并把确认的结论补充进来。

## 1. 总体形状

```python
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.workers.runner import WorkerRunner

pipeline = Pipeline([processor_a, processor_b, ...])
worker = PipelineWorker(
    pipeline,
    params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
    app_resources=resources,          # 任意对象；工具函数里用 params.app_resources 取回
    idle_timeout_secs=None,           # 会议中长时间无人与助理对话是常态
)
runner = WorkerRunner(handle_sigint=False)   # 嵌入 FastAPI 时不接管信号
await runner.add_workers(worker)
await runner.run()
```

- `PipelineTask`、`PipelineRunner` 仍可导入，但已标记为弃用，将在 2.0 中删除。
- `PipelineWorker` 默认会自动接好 RTVI（`enable_rtvi` 默认开启），不需要手工往管线里加 RTVI 处理器。
  需要拿到 `RTVIProcessor` 注册事件时，自己创建一个并通过 `rtvi_processor=` 传入。

## 2. SmallWebRTC 传输与信令

```python
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest, SmallWebRTCRequest, SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

transport = SmallWebRTCTransport(
    webrtc_connection=connection,                     # 由信令回调给出
    params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
)
# 管线首尾分别是 transport.input() 和 transport.output()
```

信令路由与 Pipecat 自带的运行器一致（`runner/run.py` 的 `_setup_webrtc_routes`）：

```python
handler = SmallWebRTCRequestHandler(ice_servers=ice_servers or None)

@app.post("/api/offer")
async def offer(request: SmallWebRTCRequest, background_tasks: BackgroundTasks):
    async def on_connection(connection: SmallWebRTCConnection):
        background_tasks.add_task(run_bot, connection, request.request_data)
    return await handler.handle_web_request(request=request, webrtc_connection_callback=on_connection)

@app.patch("/api/offer")
async def ice_candidate(request: SmallWebRTCPatchRequest):
    await handler.handle_patch_request(request)
    return {"status": "success"}

# 应用关闭时：await handler.close()
```

- `request.request_data` 是浏览器在连接参数 `requestData` 中传入的对象。浏览器端 SDK 在请求体里用的是
  驼峰的 `requestData`，而 `SmallWebRTCRequest` 的字段是 `request_data`——像上面骨架那样让 FastAPI 直接按
  `SmallWebRTCRequest` 解析请求体，驼峰那个键会被忽略（`request_data` 恒为 `None`）。要拿到它，读原始 JSON 后用
  `SmallWebRTCRequest.from_dict(payload)`（它同时接受两种写法）。`web/app.py` 就是这么做的。
- `IceServer` 就是 aiortc 的 `RTCIceServer(urls, username, credential, credentialType)`；
  `SmallWebRTCRequestHandler(ice_servers=None)` 只提供 host 候选，同一局域网内够用。处理器里的 ICE 服务器
  只作用于服务端这一端的连接，浏览器那一端要另外给（§12）。
- 程序化启动：`uvicorn.Server(uvicorn.Config(app, host=..., port=..., ssl_certfile=..., ssl_keyfile=...)).serve()`
  在已有的事件循环里运行；它自己接管 SIGINT，收到 Ctrl+C 后优雅关闭并返回（或再抛 `KeyboardInterrupt`），
  所以 `serve --with-services` 把「停推理服务」放在 `finally` 里。`fastapi`、`uvicorn` 由 `pipecat-ai[runner]`
  带入，没有单独列在 `pyproject.toml` 里。
- 传输的事件：`on_client_connected(transport, connection)`、`on_client_disconnected(transport, connection)`、
  `on_app_message(transport, message, sender)`，用 `@transport.event_handler("...")` 注册。
- 本项目不使用自带运行器的 `main()`，它会接管整个 FastAPI 应用和命令行。

## 3. 语音活动检测与轮次

### 3.1 语音活动检测放在管线里

```python
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.processors.audio.vad_processor import VADProcessor

vad = VADProcessor(vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.2)))
```

它向上下游**广播** `VADUserStartedSpeakingFrame` / `VADUserStoppedSpeakingFrame` / `UserSpeakingFrame`。
放在识别服务之前，识别服务才能收到这两个事件。Silero 模型随 Pipecat 核心安装（ONNX，跑在 CPU）。

**音量门限**：`VADParams` 除了 `confidence=0.7`、`start_secs=0.2`、`stop_secs=0.2`，还有
`min_volume=0.6`。判「说话中」= `置信度 ≥ confidence` **且** `平滑音量 ≥ min_volume`
（`vad_analyzer.py` 的 `_run_analyzer`）。平滑音量 = 最近 0.4 秒的 BS.1770 响度，从 −110…−10 LUFS
线性映射到 0…1（`audio/utils.py` 的 `calculate_audio_volume`），再以系数 0.2 指数平滑；所以
**0.6 ≈ −50 LUFS，每低 6 dB 约少 0.06**。麦克风电平偏低时，只有最响的音节过得了门限，
语音被切成碎片（见 [benchmarks.md](benchmarks.md#麦克风电平偏低)）。
`build_parts()` 把 `stop_secs` 和 `min_volume` 都接到配置上（`turn.vad_stop_secs`、`turn.vad_min_volume`，默认 0.6）。
`min_volume=0` 即关闭这道门限，只剩模型置信度。

### 3.1.1 输入音频滤波器

`TransportParams(audio_in_filter=...)` 接收一个 `BaseAudioFilter`
（`pipecat.audio.filters.base_audio_filter`），四个抽象方法：

```python
class MyFilter(BaseAudioFilter):
    async def start(self, sample_rate: int): ...      # 传输输入初始化时调一次
    async def stop(self): ...                         # 清理时调一次
    async def process_frame(self, frame: FilterControlFrame): ...  # 运行中改设置用，可以什么都不做
    async def filter(self, audio: bytes) -> bytes: ...  # 每个输入音频帧调一次：s16le 单声道字节进，字节出
```

调用位置在 `BaseInputTransport._audio_task_handler`（`transports/base_input.py`）：**先滤波，再把帧推向下游**，
所以 `VADProcessor`、识别服务、会议记录器（说话人区分）看到的都是处理后的同一份音频。
两条约束：`filter()` 返回空字节会让这一帧被**丢弃**（滤波器缓冲时用的），而会话时间轴按采样数计数
（architecture.md §3），因此增益滤波器必须**逐帧等长输出**；它跑在事件循环里，只做轻量的 numpy 运算。
自带的现成滤波器（`rnnoise_filter`、`koala_filter`、`krisp_viva_filter`、`aic_filter`）都是降噪，没有增益。

### 3.2 轮次策略

```python
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair, LLMUserAggregatorParams,
)
from pipecat.turns.user_start import TranscriptionUserTurnStartStrategy, VADUserTurnStartStrategy
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

context = LLMContext(tools=[...])
pair = LLMContextAggregatorPair(
    context,
    user_params=LLMUserAggregatorParams(
        user_turn_strategies=UserTurnStrategies(
            start=[wake, VADUserTurnStartStrategy(), TranscriptionUserTurnStartStrategy()],  # wake 见 §3.3
            stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())],
        ),
    ),
)
user_aggregator, assistant_aggregator = pair.user(), pair.assistant()
```

- 轮次结束模型 `LocalSmartTurnAnalyzerV3` 自带 ONNX 权重（包内 `smart-turn-v3.2-cpu.onnx`），
  不需要安装 `local-smart-turn` extra（它会引入 torch）。
- 不用轮次结束模型时，换成 `SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)`。
- 唤醒策略必须放在 `start` 列表的**第一个**：它在未唤醒时返回 `STOP`，后面的策略就不会执行。

### 3.3 唤醒词

助理的名字约定为**英文单词**。Pipecat 自带的 `WakePhraseUserTurnStartStrategy`
（`turns/user_start/wake_phrase_user_turn_start_strategy.py`）支持英文唤醒词，其行为如下：

- 只检查最终的 `TranscriptionFrame`，不看临时转录。
- 把每条转录先去掉标点，再用一个空格接到累积文本后面（上限 250 字符），在累积文本里找唤醒词。
- 未唤醒时对每条转录调用 `trigger_reset_aggregation()`，清掉聚合器里的文本——所以未唤醒时的发言
  不会进入上下文（这就是会议记录器必须在它上游的原因）。
- 唤醒时创建后台任务触发 `on_wake_phrase_detected(strategy, phrase)` 事件；
  `single_activation=True` 表示每一轮都要重新叫名字。

它的局限在于词边界：匹配所用的正则是 `\b` + 唤醒词 + `\b`，而 Python 的 `\b` 把汉字也视为单词字符：

| 转录文本                                                 | 自带策略   |
| -------------------------------------------------------- | ---------- |
| `请 Jarvis 帮我查一下`（名字两侧有空格）                 | 命中       |
| `请Jarvis帮我查一下`（名字紧贴汉字）                     | **不命中** |
| `Jarvis，帮我查一下`（它先删掉逗号，名字就贴上了「帮」） | **不命中** |

识别模型输出的中英混排文字约有一半是后两种形式。因此本项目使用一个很小的子类
`WakeWordUserTurnStartStrategy`（`src/agentic_meeting/pipeline/wake.py`）：
它继承自带策略，只把编译好的匹配模式替换为「唤醒词前后不是英文字母或数字」；由汉字组成的别名按连续字符匹配。
实测结果见 [benchmarks.md](benchmarks.md#唤醒词匹配)。

为此，识别服务保证以下两点（interfaces.md §3.2、§3.4）：

- 一个英文单词不会被拆进两条转录帧（策略在各条转录之间加空格，拆开就匹配不上了）。
- 不推送只有空白的转录帧。

**唤醒窗口与打断**（已用真实的聚合器和唤醒策略在 `tests/test_bot.py` 中验证）：

- 没叫名字的发言：不触发 `UserStartedSpeakingFrame`、不打断、不给模型（上下文里也没有这句话）。
- 叫了名字：依次出现 `UserStartedSpeakingFrame`、`InterruptionFrame`；说完（停顿 + 转录收尾）后
  `LLMContextFrame` 交给模型。
- 助理朗读时有人开口：**只有还在唤醒窗口内**才会打断。`single_activation=True` 时窗口是
  `wake_timeout_secs`（默认 30 秒），从叫名字那一刻起算，**助理说话不会续期**；窗口过后策略回到 IDLE，
  阻止其后的所有策略，此时开口说话无法打断朗读。
- **窗口内任何人的发言都会被当作对助理说的。** `single_activation=True` 时，自带策略在应答之后并不回到 IDLE，
  而是保持唤醒直到超时。子类增加了 `sleep()`，由 `pipeline/bot.py` 的 `wire_wake_sleep` 在助理由忙转闲时调用；
  用户轮次进行中（打断，或叫完名字停顿之后才说出的请求）时不调用。
- 第二次开口要成为「新的一轮」，上一轮必须已经结束（`UserStoppedSpeakingFrame`）；
  叫名字的那一轮还没结束时，同一轮里的再次开口不会产生新的打断。

## 4. 自定义识别服务

基类 `services/stt_service.py` 的 `STTService`：

- 构造参数（节选）：`audio_passthrough=True`（把音频帧继续传给下游，**必须保持开启**，下游的会议记录器要用）、
  `sample_rate`、`ttfs_p99_latency`。
- 基类的 `process_frame` 会：对每个 `InputAudioRawFrame` 调 `process_audio_frame` →
  `run_stt(frame.audio)`；对 `VADUserStartedSpeakingFrame` / `VADUserStoppedSpeakingFrame`
  调内部处理后原样下传。
- 子类必须实现 `async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]`。
  对流式后端，在这里把音频交给后端后 `yield None` 即可；识别结果由后台任务拿到后用
  `await self.push_frame(frame)` 推送。
- 要感知语音活动事件：重写 `process_frame`，**先调 `await super().process_frame(frame, direction)`**
  （它负责转发），再按帧类型做自己的事。
- 生命周期钩子：`async def start(self, frame)`、`stop(self, frame)`、`cancel(self, frame)`，
  重写时都要先调 `super()`。后台任务用 `self.create_task(coro)` 创建、`await self.cancel_task(task)` 取消。
- 转录帧：

```python
from pipecat.frames.frames import InterimTranscriptionFrame, TranscriptionFrame
from pipecat.utils.time import time_now_iso8601

TranscriptionFrame(text="...", user_id="", timestamp=time_now_iso8601(), result=delta, finalized=False)
InterimTranscriptionFrame(text="...", user_id="", timestamp=time_now_iso8601(), result=delta)
```

关于空文本和 `finalized` 的规则见 interfaces.md §3.4，那些规则来自
`turns/user_stop/turn_analyzer_user_turn_stop_strategy.py` 的实现。

`includes_inter_frame_spaces`：文本帧上的这个属性默认为
`False`，用户侧聚合器据此在相邻两条转录之间各加一个空格。一句中文会被拆成多条转录帧发出，
因此每条转录帧在构造之后都设置 `frame.includes_inter_frame_spaces = True`（它不是构造参数，只能事后赋值），
聚合器就会原样拼接。

可参考的现成实现：`services/whisper/stt.py`（分段式，结构简单）、`services/funasr/`（本地流式）。

实现 `StreamingASRService` 时确认的几点（`services/stt_service.py`、`services/ai_service.py`）：

- `InputAudioRawFrame`、`VADUser*SpeakingFrame` 都是**系统帧**，按到达顺序处理；基类的 `process_frame`
  会把它们（以及音频，`audio_passthrough=True` 时）传给下游，子类重写时先 `await super().process_frame(...)`，
  再做自己的处理，无需再次转发。
- 基类 `process_audio_frame` 在**被静音、重连中、`is_usable` 为 False** 时提前返回，不会调 `run_stt`。
  要求「永不中断」的计数（会话时间轴）必须放在重写的 `process_audio_frame` 里、调 `super()` 之前。
- `AIService.start()` 会调 `self._settings.validate_complete()`：`NOT_GIVEN` 的字段会报错误日志。
  自定义服务构造时传 `settings=STTSettings(model=None, language=None)`（不支持的字段用 `None`）。
- `start()` / `stop()` / `cancel()` 里抛的异常会被 `AIService._start` 等吞掉只记日志；`start()` 里不要做耗时的事
  （它挡住 `StartFrame` 往下传），慢的初始化放进 `self.create_task(...)` 的后台任务。
- 可自行恢复的故障不通过 `push_error` 上报：错误类别判为永久时会把服务标记为不可用
  （`set_usable(False)`），此后基类不再给它送音频。可恢复的故障用日志 + 给浏览器的 `notice` 消息。
- 从后台任务里 `await self.push_frame(...)` 推转录帧是允许的；系统帧与数据帧在下游各有优先级，
  转录帧（数据帧）不保证与音频帧（系统帧）保持相对顺序。

## 5. 模型服务与语音合成

```python
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.tts import OpenAITTSService

class RealtimeLLMService(OpenAILLMService):
    """两种接入方式共用的实时模型服务，差别全部来自配置。"""

    def __init__(self, *, supports_developer_role: bool, **kwargs):
        super().__init__(**kwargs)
        # 基类里这是类属性（默认 True）。很多服务不认识 "developer" 角色（llama-server 加载的本地模型
        # 基本都不认识）；设为 False 后 Pipecat 会把它转成 "user"。晚到的异步工具结果正是以
        # developer 消息注入的，所以这个值必须按配置设对。
        self.supports_developer_role = supports_developer_role

rt = cfg.realtime_llm
endpoint = rt.active
llm = RealtimeLLMService(
    supports_developer_role=rt.supports_developer_role,
    base_url=endpoint.base_url,
    api_key=secret(endpoint.api_key_env) or "none",      # SDK 要求非空；不需要密钥的服务会忽略它
    settings=RealtimeLLMService.Settings(
        model=endpoint.model,
        system_instruction=system_prompt,
        temperature=..., top_p=..., max_tokens=...,       # 值为 None 的不传
        extra={"extra_body": rt.request_extra_body(background=False)},
    ),
)

class LocalTTSService(OpenAITTSService):
    """见下方「语音合成必须写子类」。重写 run_tts，其余沿用父类。"""

tts = LocalTTSService(
    base_url=cfg.tts.base_url,
    api_key=secret(cfg.tts.api_key_env) or "local",
    settings=LocalTTSService.Settings(model=cfg.tts.model, voice=cfg.tts.voice),
    sample_rate=cfg.tts.sample_rate,
)
```

**语音合成需要子类。** `services/openai/tts.py` 的 `run_tts` 会检查
音色名是否在 OpenAI 官方的固定列表里（`VALID_VOICES`：alloy、echo……），不在就直接返回错误帧。
本地语音合成的音色名不在其中，因此不能直接使用 `OpenAITTSService`。`LocalTTSService` 继承它并重写
`run_tts`：沿用父类的实现，去掉 `VALID_VOICES` 的检查，`voice` 直接使用配置值，
并通过 `extra_body={"language": cfg.tts.language}` 传语言。父类里这几点可以原样保留：
`response_format` 固定为 `"pcm"`、用 `with_streaming_response` 边收边产出 `TTSAudioRawFrame`、
首包到达时调 `stop_ttfb_metrics()`。

- `Settings.extra` 会原样并入 `chat.completions.create(**params)` 的参数
  （`services/openai/base_llm.py` 的 `build_chat_completion_params`）。OpenAI SDK 不认识的字段
  必须包在 `extra_body` 里，否则 SDK 会报「未知参数」。
- 构造函数里的 `model=`、`params=`（`InputParams`）是弃用写法，用 `settings=`。
- 带外推理（不经过管线，用于预热、画面摘要、纪要）：
  `await llm.run_inference(context, max_tokens=..., system_instruction=...)`，返回字符串。
  它用的是服务自身的设置（含 `extra`）。后台任务（画面摘要、纪要）另建一个 `RealtimeLLMService` 实例，
  `extra_body` 使用 `rt.request_extra_body(background=True)`，不与实时应答共用实例：llama.cpp 部署方式下
  这会让两者各占一个槽位、互不冲掉对方的前缀缓存；通用接口方式下两个实例的请求字段相同，
  分开只是为了能单独取消后台请求。
- `extra_body` 可以是空字典。`top_k` 已由 `request_extra_body` 放入其中，不再传给 `Settings.top_k`
  （OpenAI 兼容服务的基类不会发送它）。
- `LocalTTSService` 的实现要点（参照 `services/openai/tts.py` 和 `tts_service.py`）：
  - `run_tts(self, text, context_id)` 是 1.12.0 的签名；`self.chunk_size` 来自 `start()` 时确定的采样率。
  - 构造时要关掉 `max_consecutive_zero_audio_contexts`（传 0）：默认连续 3 句没有音频就把服务判为不可用，
    此后整个会话都不再给它活干。本地 `tts-server` 重启一下就能恢复，不应因此永久没声音。
    400 / 404 这类配置错误仍然由错误类别（`INVALID_REQUEST`）判为永久，这是想要的。
  - 失败以 `ErrorFrame(error=..., exception=e)` 上报，而不是抛出；带上异常，Pipecat 才能按类别判断
    （连不上、5xx 不算永久）。Pipecat 还会在这句话收尾时再追加一条「没有产出音频」的通用错误。
  - `stop_frame_timeout_s` 默认 3 秒：最后一块音频之后等这么久才推 `TTSStoppedFrame`。保持默认即可，
    且不应短于两句话之间的生成间隔。
  - OpenAI SDK 默认失败重试 2 次（带退避），对一句话的合成只会让报错更晚，所以用
    `client.with_options(max_retries=0)` 关掉。实时模型的聊天请求保持 SDK 默认。
  - 本机地址不走系统代理：传 `DefaultAsyncHttpxClient(trust_env=False)`（实时模型的 `create_client` 同理）。
- `run_inference(context, max_tokens=...)` 覆盖输出上限时，写的是 **`max_completion_tokens`**（它在参数里
  永远存在，值为 SDK 的未设置标记）而不是 `max_tokens`。个别第三方接口可能不认这个字段；
  不认时改成在构造 `Settings` 时设 `max_tokens`。
- `run_inference` 的行为（`tests/test_caption_wiring.py` 中有请求体的回归测试）：
  - 传了 `max_tokens=N` 时请求体里是 `max_completion_tokens: N`；`Settings.max_tokens` 没设时请求体里**没有** `max_tokens` 字段。
    两个都设会同时发出去，所以后台实例（`build_realtime_llm(..., background=True)`）不带配置里的 `max_tokens`。
    仓库里的 llama.cpp 源码把 `max_completion_tokens` 登记为 `max_tokens` 的别名（`tools/server/server-schema.cpp`）；
    其他服务是否接受该字段需要自行确认。
  - `system_instruction` 传 `None`（或服务的 `system_instruction` 是空串）时不会多出一条空的 system 消息。
  - 它是普通的协程：取消包着它的任务，HTTP 请求随之中止——后台请求的「抢占」就是这么做的。
  - `LLMContext(messages)` 直接接收消息列表，带图片的消息用下一条的辅助函数生成。
- 往上下文里放图片：`LLMContext.create_image_url_message(url="data:image/webp;base64,...", text="...")`。
- 语音合成默认按句子聚合文本再合成（`TTSService` 的 `text_aggregation_mode`，默认 `SENTENCE`）。
  中文断句无需自定义聚合器（`tests/test_local_services.py` 中有回归测试）：默认的
  `SimpleTextAggregator` 在 `。！？；` 处断句，逗号和冒号不断；中英混排里的 `Dr.`、`v2.0`、`3.5` 不会被切开；
  一个字符一个字符喂和一次喂几个字符，结果相同。一句话要等到下一个字符到达（或回复结束）才会放行——
  所以首句越短首音越快，首句过长是靠提示词（短句）而不是代码来解决。
- `tts-server` 流式返回的是 24 kHz、16 位、单声道 PCM，与 `tts.sample_rate` 的默认值一致；
  接口细节见 interfaces.md §9 末尾。

## 6. 工具（函数调用）

```python
from pipecat.adapters.schemas.direct_function import tool_options
from pipecat.frames.frames import FunctionCallResultProperties
from pipecat.services.llm_service import FunctionCallParams

async def recall(params: FunctionCallParams, query: str = "", speaker: str = "", limit: int = 10):
    """查找会议中说过的话。

    Args:
        query: 关键词或一句话描述要找的内容。
        speaker: 说话人的名字，留空表示不限。
        limit: 最多返回多少条。
    """
    store = params.app_resources.store
    await params.result_callback({"items": [...]})

@tool_options(cancel_on_interruption=False, timeout_secs=960)
async def delegate_task(params: FunctionCallParams, goal: str, minutes_of_context: int = 5):
    """把需要检索、读图或计算的事交给后台去做。..."""
    task_id = await params.app_resources.tasks.submit(...)
    await params.result_callback(
        {"task_id": task_id, "status": "accepted"},
        properties=FunctionCallResultProperties(is_final=False),
    )
    result = await params.app_resources.tasks.wait(task_id)
    await params.result_callback({"task_id": task_id, "status": "succeeded", "brief": result.brief})

context = LLMContext(tools=[recall, delegate_task, ...])   # 放进 tools 即自动注册
```

- 「直接函数」：第一个参数必须是 `params: FunctionCallParams`；其余参数的类型标注和 Google 风格的
  文档字符串会被自动转成工具定义。写法见 `adapters/schemas/direct_function.py`。
- `cancel_on_interruption=False` 的含义：对话不等这个工具返回；结果晚到时 Pipecat 把它作为新消息注入
  上下文并触发一次生成。这就是「委托后先口头确认、做完再简报」的机制。
- `is_final=False` 的中间结果不会结束这次调用，也不会重置超时。
- **工具调用在上下文中的形式**（`llm_response_universal.py`）：助理侧聚合器收到
  `FunctionCallInProgressFrame` 时**一次加两条**消息——`assistant`（带 `tool_calls`）和 `tool`（内容先是
  `"IN_PROGRESS"`），结果到了再原地改写那条 `tool` 消息。所以这期间从别处追加进来的转录行、画面行不会插到两者中间。
  结果写入后聚合器向上游推一次上下文帧，模型带着结果继续生成。
- 工具函数可能比上面那两条消息先跑起来（帧要经过语音合成和传输才到助理侧聚合器）。要在工具里直接往上下文加消息
  （`params.context.add_message(...)`，`look_at_screen` 加图片就是这样）又想排在调用记录之后，就先等
  `tool_call_id` 出现在上下文里。
- 用 `functools.wraps` 包一层的直接函数照样能提取工具定义（`inspect.signature` 会顺着 `__wrapped__` 找到原签名）；
  `Literal[...]` 之类的类型没有验证过，参数只用 `str` / `int` / `float`。
- 测试里 `run_test` 自己建 `PipelineWorker`，没法传 `app_resources`；管线起来之后设
  `llm.pipeline_worker._app_resources` 即可（`tests/test_tools.py`）。
- **`skip_tts` 与工具。**`LLMConfigureOutputFrame` 是记在模型服务身上的持久状态，不是「只管下一次生成」。
  一次请求带工具调用时会生成两次（调用、拿到结果后的回答），中间夹着的任何配置帧都会影响第二次。
  所以模态在请求进入模型**之前**设定，不在请求之后「恢复」。工具结果触发的再次生成是助理侧聚合器向**上游**推的
  `LLMContextFrame`，放在模型上游的处理器看不到它。
- 工具调用期间没有「结束」事件：模型服务有 `on_function_calls_started`（参数是调用列表），之后以下一次
  `on_assistant_turn_started` 为准。只发起工具调用、没有文字的那次生成也会触发 `on_assistant_turn_started` / `stopped`
  （`stopped` 的 `message.content` 为空）。
- **异步工具**（`cancel_on_interruption=False`）：
  - `@tool_options(...)` 只是把选项挂在函数上（`_pipecat_cancel_on_interruption`、`_pipecat_timeout_secs`），
    需要放在最外层。
  - 上下文里的消息由 `processors/aggregators/async_tool_messages.py` 生成：开始时一条 `tool` 占位；
    每次 `is_final=False` 的回报一条 `developer`；最终结果一条 `developer`（如果期间上下文里没有新的用户 / developer 消息，
    则是原地改写占位）。内容是 JSON：`{"type": "async_tool", "status", "tool_call_id", "description": 英文说明, "result": 再编码的字符串}`，
    外层用默认的 `json.dumps`，**中文被转义成 `\uXXXX`**。`async_tool_messages.parse_message(msg)` 可以把它解回来
    （只认 `tool` / `developer` 角色；`developer` 被转成 `user` 之后要自己把角色换回去再解析）。
  - 中间回报和最终回报都会触发一次生成（除非那时用户正在说话或助理正在朗读，会推迟到说完）。
  - 只要注册了异步工具，`LLMService._compose_system_instruction` 就在系统提示词后面追加 `ASYNC_TOOL_INSTRUCTIONS`（英文）。
    没有开关；子类重写该方法将其去掉。
  - 最终结果触发的生成是助理侧聚合器向**上游**推的上下文帧。要在它之前改模型服务的状态（比如 `skip_tts`），
    直接 `await params.llm.queue_frame(帧)`，再调 `result_callback`——帧会排在那次生成前面。
  - `FunctionCallResultProperties(on_context_updated=协程函数)`：结果写进上下文之后回调（在单独的任务里跑）。
- `FunctionCallParams` 的字段：`function_name, tool_call_id, arguments, llm, pipeline_worker, context,
result_callback, app_resources, worker_runner`。

## 7. MCP 客户端

```python
from mcp.client.session_group import StreamableHttpParameters
from pipecat.services.mcp_service import MCPClient

mcp = MCPClient(
    server_params=StreamableHttpParameters(url=server.url, headers={...}, timeout=server.timeout_secs),
    tools_filter=cfg.realtime.direct_mcp_tools,     # 只暴露白名单里的工具
)
tools_schema = await mcp.tools()      # 返回带处理函数的工具集合，放进 LLMContext 即可
```

`register_tools(llm)` 是已弃用的写法。该客户端仅用于「实时模型直连少量 MCP 工具」的可选功能；后台 agent 用它自己框架的 MCP 客户端。

## 8. 上下文操作

| 需求                   | 做法                                                             |
| ---------------------- | ---------------------------------------------------------------- |
| 追加消息但不触发生成   | 向管线推 `LLMMessagesAppendFrame(messages=[...], run_llm=False)` |
| 追加消息并触发生成     | 同上，`run_llm=True`                                             |
| 整体替换上下文（压缩） | `LLMMessagesUpdateFrame(messages=[...], run_llm=False)`          |
| 让助理直接说一句话     | `TTSSpeakFrame(text="...")`                                      |
| 读取当前消息           | `context.get_messages()`                                         |

**整体替换上下文**：`LLMMessagesUpdateFrame` 由用户侧聚合器处理（`set_messages`），不往下游传；
两个聚合器共用同一个 `LLMContext`，所以助理侧看到的也是新的。系统提示词在 `Settings.system_instruction` 里、
工具在 `LLMContext.tools` 里，都不在消息列表中，替换不影响它们。

**正式请求的参数怎么来的（`services/openai/base_llm.py` 的 `get_chat_completions`）**：
`adapter.get_llm_invocation_params(context, system_instruction=…, convert_developer_to_user=…)` 给出消息、工具、
`tool_choice`，再由 `build_chat_completion_params` 并上采样参数和 `Settings.extra`。预热请求走同一条路
（`RealtimeLLMService.request_params`），所以两者的消息与工具逐字一致——`tests/test_context.py` 里有一条测试
把两次请求的请求体拿来比。

消息是 OpenAI 格式的字典：`{"role": "user" | "assistant" | "system", "content": "..."}`。
追加类的帧由用户侧聚合器处理，所以推送位置必须在它的**上游**（会议记录器正好在上游）。
从管线外部推帧：`await worker.queue_frame(frame)`。

聚合器事件（用 `@aggregator.event_handler("...")` 注册）：

- 用户侧：`on_user_turn_started`、`on_user_turn_stopped`（带用户这一轮的完整文本）。
- 助理侧：`on_assistant_turn_started`、`on_assistant_turn_stopped`（带助理这一轮说出的文本，
  用它把助理发言落库）。

## 9. 向浏览器发消息

```python
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame

await self.push_frame(RTVIServerMessageFrame(data={"type": "caption", ...}))
```

- 这是系统帧，在处理器里用 `push_frame` 推即可；管线外用 `worker.queue_frame`。
- 浏览器端在 `PipecatClient` 的 `onServerMessage` 回调里收到 `data`。
- 浏览器发来的自定义消息：`RTVIProcessor` 的 `on_client_message` 事件
  （`processors/frameworks/rtvi/processor.py`）。

## 10. 自定义处理器

```python
from pipecat.frames.frames import Frame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

class MeetingRecorder(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)      # 必须先调
        ...                                                # 自己的处理
        await self.push_frame(frame, direction)            # 必须放行，否则下游收不到
```

助理说话的起止：`BotStartedSpeakingFrame` / `BotStoppedSpeakingFrame`（方向是向上游），
处理器里按类型判断即可收到。

**系统帧与数据帧分别排队，相互之间的顺序没有保证。**`InputAudioRawFrame`、`VADUser*SpeakingFrame`、
`RTVIServerMessageFrame` 是系统帧，会插队；转录、文字是数据帧，按顺序排队。因此处理器不能假设
「`VADUserStartedSpeakingFrame` 一定在它之前的转录帧之后到达」。测试里要验证这类先后关系时，在步骤之间
放 `SleepFrame`；断言通过的帧时，系统帧和数据帧分开比较各自的顺序。

**其他注意事项**：

- 处理器的属性和方法不要与 `FrameProcessor` / `BaseObject` 的内部名称重名：`_name`（处理器名字，字符串）、`_id`、
  `_metrics`、`_task_manager`、`_event_handlers`、`_setup`、`_next` / `_prev` 等。例如定义名为 `_name` 的方法，
  会导致 `'str' object is not callable`。
- 助理说话的起止帧 `BotStartedSpeakingFrame` / `BotStoppedSpeakingFrame` 由传输输出**同时**向下游和上游各推一份
  （`transports/base_output.py`）；位于传输输出上游的处理器（比如会议记录器）收到的是**上游**那份——
  `process_frame` 中需要处理 `direction == FrameDirection.UPSTREAM`。
- `LLMMessagesAppendFrame` 被用户侧聚合器消费（`add_messages` 进共享的 `LLMContext`），**不会**再被转发到助理侧聚合器，
  所以追加一次只在上下文里出现一次（`tests/test_meeting_recorder.py` 用真实聚合器验证过）。
- 助理这一轮说的话：`on_assistant_turn_started(aggregator)` 和 `on_assistant_turn_stopped(aggregator, message)`，
  `message.content` 是这一轮的完整文本（被打断且还没有任何 token 时可能为空）、`message.interrupted` 是否被打断。

可观测性：`PipelineParams(enable_metrics=True)` 加两个观察者就够了——
`MetricsLogObserver()`（每个服务的首字节延迟等）和 `UserBotLatencyObserver()`；后者的
`on_latency_breakdown` 事件给出一个 `LatencyBreakdown`，`turn_contribution_lines()` 是「用户说完 → 助理开口」
按环节拆开的耗时（停顿判定、转录、大模型、语音合成），直接写日志即可（`pipeline/bot.py` 里已接）。

`PipelineWorker.cancel()` 立即取消管线（客户端断开时用），`stop_when_done()` 排队一个 `EndFrame` 等已排队的
帧处理完。`run_test` 的第一个参数可以是一整条 `Pipeline`，端到端演练就是这样做的。

## 11. 多 worker（未使用）

`pipecat.workers` 提供一条总线，可以把后台 agent 做成独立 worker、甚至拆到另一台机器
（`workers/proxy/websocket/`）。接口见 `workers/base_worker.py`：`request_job`、`job`、
`send_job_update`、`send_job_response`、`cancel_job_group`。

本项目没有使用它：任务管理器运行的是进程内的 `asyncio` 任务，更简单，也足以满足需求。

## 12. 浏览器端

包与版本锁在 `client/package.json`；类型定义在 `client/node_modules/@pipecat-ai/*/dist/index.d.ts`，
以其为准。

```ts
import { PipecatClient } from "@pipecat-ai/client-js";
import { PipecatClientAudio, PipecatClientProvider } from "@pipecat-ai/client-react";
import { SmallWebRTCTransport, WavMediaManager } from "@pipecat-ai/small-webrtc-transport";

const client = new PipecatClient({
  transport: new SmallWebRTCTransport({ mediaManager: new WavMediaManager() }),
  enableMic: true,
  enableCam: false,
  callbacks: {
    onTransportStateChanged: (state) => {},   // "ready" 才是服务端管线就绪
    onServerMessage: (data) => { /* 自定义消息，见 interfaces.md §6.1 */ },
    onBotLlmStarted: () => {}, onBotLlmText: ({ text }) => {}, onBotLlmStopped: () => {},
    onBotStartedSpeaking: () => {}, onBotStoppedSpeaking: () => {},
    onTrackStarted: (track, participant) => {},  // participant?.local 为 true 的是本机麦克风
    onDeviceError: (error) => {}, onError: (message) => {},
  },
});
await client.connect({ webrtcRequestParams: { endpoint: "/api/offer", requestData: { /* 可选 */ } } });
client.sendClientMessage("text_input", { text: "..." });
await client.disconnect();
```

- 连接参数用 `webrtcRequestParams: { endpoint, requestData? }`（`webrtcUrl` / `connectionUrl` 已弃用）；
  `connect()` 在服务端的管线就绪（状态 `ready`）后才 resolve，失败时 reject。`requestData` 会以驼峰的
  `requestData` 键出现在 `/api/offer` 的请求体里（见 §2）。
- **助理的语音要自己挂到 `<audio>` 上**：传输层不会自动播放。用 `@pipecat-ai/client-react` 的
  `<PipecatClientProvider client={client}>` 包住界面，里面放一个 `<PipecatClientAudio />` 即可。
  客户端对象建一次，开始 / 结束只是对它 `connect()` / `disconnect()`。
- **页面用 `WavMediaManager`，不用默认的媒体管理器。** 不传 `mediaManager` 时传输层会建一个 `DailyMediaManager`
  （`@daily-co/daily-js`），它在开始会议时从 `c.daily.co` 下载 call-machine 脚本，还会往 `sentry.io` 报错；浏览器访问不到
  `c.daily.co` 时根本不会创建 `RTCPeerConnection`。页面传入 `new SmallWebRTCTransport({ mediaManager: new WavMediaManager() })`
  （两者都由 `@pipecat-ai/small-webrtc-transport` 导出），它自己不发任何网络请求。`WavMediaManager` 用
  `getUserMedia({ audio: true })` 取麦克风（选了设备时再带 `deviceId`），并通过 `onTrackStarted` 报告这条轨道。它还会运行一个
  `AudioWorklet` 录音器和一个流式播放器，那是给 WebSocket 传输层用的；在 SmallWebRTC 下没人读录到的数据，助理的声音照旧作为远端
  WebRTC 轨道到达。
- **换麦克风时传输层不会把新轨道发出去。** 系统默认麦克风变了、或者当前的被拔掉时，`WavMediaManager` 会停掉旧轨道、换一条新的，
  但只有 `DailyMediaManager` 接了「把轨道换进 PeerConnection」这一步。页面在 `onTrackStarted` 里自己做（`localAudio.ts`），
  用的是传输层的 `getAudioTransceiver()`：1.10.8 的内部方法，类型声明里没有，调用前先检查它在不在。不做这一步，
  连接里留着的是已经停掉的轨道，服务端再也收不到声音。
- 媒体管理器不暴露麦克风约束。回声消除的做法：`onTrackStarted` 里对本机音频轨检查 `track.getSettings().echoCancellation`，
  不是 `true` 就 `track.applyConstraints({ echoCancellation: true })`（主流浏览器默认就是开的）。降噪与自动增益保持浏览器默认。
  约束只能事后通过 `applyConstraints` 设置；浏览器的自动增益在具体设备上是否生效尚未验证。页面会把 `getSettings()` 的结果输出到控制台。
- **SDK 会自行重连。** 服务端重启后，无需任何操作就会出现一个新的连接（`POST /api/offer`，新的 `pc_id`）。这条重连**不会**重新触发 RTVI 的 `client-ready`，所以服务端在 `on_client_ready` 里发的
  `session` 消息页面收不到；页面要靠 HTTP 兜底——连接就绪时和连接期间每 5 秒核对一次「当前会话」（`useMeetingClient.ts`）。
  同一个 `pc_id` 的重连（ICE 重启）服务端日志里是 `Reusing existing connection`，不会再次调用 bot。
- **SDK 重连时使用的连接参数可以事后修改。**
  传输层把 `connect({ webrtcRequestParams })` 给的那个对象原样存着（`this._webrtcRequest`），每次发 offer 时现读
  `this._webrtcRequest.requestData`。所以页面始终传同一个对象，知道自己在哪场会议里之后把 `session_id` 写进它的
  `requestData`——SDK 自己重连（ICE 断开 5 秒后新建 PeerConnection，最多 3 次）发的 offer 就带着 `session_id`，
  服务端会继续同一场会议，而不是新建一场。
  SDK 放弃之后传输层状态变成断开，页面自己的重连（退避 1、2、4、8、8 秒）才接手。
- **ICE 服务器设在传输层上。** `new SmallWebRTCTransport()` 不带任何 ICE 服务器，服务端 `SmallWebRTCRequestHandler`
  里的设置也传不过来。传输层有一个 `iceServers` 的 setter（`RTCIceServer[]`），每次新建 `RTCPeerConnection` 时读取，
  SDK 自己重连时也一样。页面在每次 `connect()` 之前用 `GET /api/ice` 的结果设置它（`useMeetingClient.ts`）。
- `tsconfig` 里 `verbatimModuleSyntax` + `allowImportingTsExtensions`：源码里的相对导入写 `.ts` 扩展名，
  纯逻辑文件（协议解析、字幕合并、状态归约）才能被 Node 自带的测试运行器直接跑（`npm test`，不需要测试框架）；
  `*.test.ts` 不参与 `tsc` 与打包。
- 屏幕截图不使用 SDK 的 `enableScreenShare`（它会建立一条 WebRTC 视频轨）。
  页面直接调用 `navigator.mediaDevices.getDisplayMedia()`，把画面画到 `<canvas>` 上取静态图上传。
