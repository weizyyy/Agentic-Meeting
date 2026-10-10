# Pipecat integration notes

**English** · [简体中文](zh-CN/pipecat-notes.md)

How Agentic-Meeting uses Pipecat **1.12.0**. Pipecat's APIs changed substantially during 1.x —
`PipelineTask` became `PipelineWorker`, `PipelineRunner` became `WorkerRunner`, turn strategies were
rewritten and settings moved into `Settings` objects — so examples written for earlier versions
often do not apply.

Every import path in this document works in the project's virtual environment, and signatures are
taken from the installed source. Paths are relative to `site-packages/pipecat/`. When something is
not covered here, read the installed source and add what you confirm.

## 1. Overall shape

```python
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.workers.runner import WorkerRunner

pipeline = Pipeline([processor_a, processor_b, ...])
worker = PipelineWorker(
    pipeline,
    params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
    app_resources=resources,          # any object; tools read it back as params.app_resources
    idle_timeout_secs=None,           # long stretches without talking to the assistant are normal
)
runner = WorkerRunner(handle_sigint=False)   # embedded in FastAPI: leave signals alone
await runner.add_workers(worker)
await runner.run()
```

- `PipelineTask` and `PipelineRunner` still import but are deprecated and will be removed in 2.0.
- `PipelineWorker` wires up RTVI by default (`enable_rtvi`). To register events on the
  `RTVIProcessor`, create one and pass it as `rtvi_processor=`.

## 2. SmallWebRTC transport and signaling

```python
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    SmallWebRTCPatchRequest, SmallWebRTCRequest, SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

transport = SmallWebRTCTransport(
    webrtc_connection=connection,                     # provided by the signaling callback
    params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
)
# The pipeline starts with transport.input() and ends with transport.output().
```

The signaling routes follow Pipecat's own runner (`_setup_webrtc_routes` in `runner/run.py`):

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

# On shutdown: await handler.close()
```

- `request.request_data` is the object the browser passes as `requestData`. The client SDK sends
  the camel-case key, while the `SmallWebRTCRequest` field is `request_data`: letting FastAPI parse
  the body as in the skeleton above silently drops it. Read the raw JSON and use
  `SmallWebRTCRequest.from_dict(payload)`, which accepts both spellings, as `web/app.py` does.
- `IceServer` is aiortc's `RTCIceServer(urls, username, credential, credentialType)`.
  `SmallWebRTCRequestHandler(ice_servers=None)` offers host candidates only, which is enough on a
  single LAN. The handler's ICE servers apply to the server's peer connection only; the browser
  needs its own (§12).
- Starting programmatically: `uvicorn.Server(uvicorn.Config(app, ...)).serve()` runs inside an
  existing event loop and handles SIGINT itself, so `serve --with-services` stops the inference
  services in a `finally` block. `fastapi` and `uvicorn` come with `pipecat-ai[runner]`.
- Transport events, registered with `@transport.event_handler("...")`:
  `on_client_connected(transport, connection)`, `on_client_disconnected(transport, connection)`,
  `on_app_message(transport, message, sender)`.
- The runner's `main()` is not used; it would take over the FastAPI application and the command
  line.

## 3. Voice activity detection and turns

### 3.1 VAD in the pipeline

```python
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.processors.audio.vad_processor import VADProcessor

vad = VADProcessor(vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.2)))
```

`VADProcessor` **broadcasts** `VADUserStartedSpeakingFrame`, `VADUserStoppedSpeakingFrame` and
`UserSpeakingFrame` in both directions. It is placed before the ASR service so that the service
receives the events. The Silero model ships with Pipecat (ONNX, CPU).

**Volume gate.** Besides `confidence=0.7`, `start_secs=0.2` and `stop_secs=0.2`, `VADParams` has
`min_volume=0.6`. A frame is speech when `confidence ≥ threshold` **and** `smoothed volume ≥
min_volume` (`_run_analyzer` in `vad_analyzer.py`). The volume is the BS.1770 loudness of the last
0.4 s, mapped linearly from −110…−10 LUFS to 0…1 (`calculate_audio_volume` in `audio/utils.py`) and
smoothed exponentially with a factor of 0.2. Hence **0.6 is about −50 LUFS, and each 6 dB lowers
the value by about 0.06**. With a quiet microphone only the loudest syllables pass and speech is cut
into fragments ([benchmarks.md](benchmarks.md#low-microphone-level)). `build_parts()` maps
`stop_secs` and `min_volume` to `turn.vad_stop_secs` and `turn.vad_min_volume`; `min_volume=0`
disables the gate.

### 3.1.1 Input audio filter

`TransportParams(audio_in_filter=...)` takes a `BaseAudioFilter`
(`pipecat.audio.filters.base_audio_filter`) with four abstract methods:

```python
class MyFilter(BaseAudioFilter):
    async def start(self, sample_rate: int): ...      # once, when the input transport initializes
    async def stop(self): ...                         # once, on cleanup
    async def process_frame(self, frame: FilterControlFrame): ...  # runtime settings; may be a no-op
    async def filter(self, audio: bytes) -> bytes: ...  # per input frame: s16le mono in, bytes out
```

It is called from `BaseInputTransport._audio_task_handler` (`transports/base_input.py`): **the
filter runs first, then the frame is pushed downstream**, so VAD, ASR and the meeting recorder all
see the same processed audio. Two constraints follow. Returning empty bytes from `filter()` drops
the frame, and the session timeline counts samples (architecture.md §3), so the gain filter must be
**length-preserving**. It runs on the event loop and must stay lightweight. The filters bundled with
Pipecat (`rnnoise_filter`, `koala_filter`, `krisp_viva_filter`, `aic_filter`) are noise suppressors;
none applies gain.

### 3.2 Turn strategies

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
            start=[wake, VADUserTurnStartStrategy(), TranscriptionUserTurnStartStrategy()],  # wake: §3.3
            stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())],
        ),
    ),
)
user_aggregator, assistant_aggregator = pair.user(), pair.assistant()
```

- `LocalSmartTurnAnalyzerV3` ships with its ONNX weights (`smart-turn-v3.2-cpu.onnx`). The
  `local-smart-turn` extra is not needed and would pull in torch.
- Without the turn model, use `SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)`.
- The wake-word strategy must be **first** in `start`: while idle it returns `STOP`, which prevents
  the following strategies from running.

### 3.3 Wake word

The assistant's name is an English word. Pipecat's `WakePhraseUserTurnStartStrategy`
(`turns/user_start/wake_phrase_user_turn_start_strategy.py`) supports English wake phrases:

- It checks final `TranscriptionFrame`s only, not interim ones.
- It strips punctuation from each transcription, appends it to the accumulated text with a space
  (capped at 250 characters) and searches the accumulated text.
- While idle it calls `trigger_reset_aggregation()` for every transcription, clearing the
  aggregator's text. Speech outside the wake window therefore never reaches the context — which is
  why the meeting recorder has to sit upstream.
- On a match it fires `on_wake_phrase_detected(strategy, phrase)` from a background task.

Its limitation is the word boundary. The pattern is `\b` + phrase + `\b`, and Python's `\b` treats
CJK characters as word characters:

| Transcription                                                                       | Built-in strategy |
| ----------------------------------------------------------------------------------- | ----------------- |
| `请 Jarvis 帮我查一下` (spaces around the name)                                     | match             |
| `请Jarvis帮我查一下` (name adjacent to CJK characters)                              | **no match**      |
| `Jarvis，帮我查一下` (the comma is stripped first, leaving the name adjacent to 帮) | **no match**      |

About half of the mixed Chinese–English output of the ASR model takes the last two forms. The
project therefore uses a small subclass, `WakeWordUserTurnStartStrategy`
(`src/agentic_meeting/pipeline/wake.py`), which only replaces the compiled patterns with "not
preceded or followed by an ASCII letter or digit". Aliases made of Chinese characters are matched as
plain runs of characters. Measured results are in [benchmarks.md](benchmarks.md#wake-word-matching).

The ASR service guarantees two things for this to work (interfaces.md §3.2, §3.4):

- an English word is never split across two transcription frames, because the strategy inserts a
  space between frames;
- blank transcription frames are never pushed.

**Wake window and interruption** (verified with the real aggregator and strategy in
`tests/test_bot.py`):

- Speech without the name does not produce `UserStartedSpeakingFrame`, does not interrupt and does
  not reach the model.
- Speech with the name produces `UserStartedSpeakingFrame` and `InterruptionFrame`; when the turn
  ends, an `LLMContextFrame` goes to the model.
- Speaking while the assistant talks interrupts it **only inside the wake window**. With
  `single_activation=True` the window is `wake_timeout_secs` from the moment the name is heard and
  is not extended by the assistant's speech. Afterwards the strategy is idle and blocks the others.
- **Inside the window, anything anyone says counts as addressed to the assistant.** With
  `single_activation=True` the built-in strategy does not return to idle after an answer; it stays
  awake until the timeout. The subclass adds `sleep()`, which `wire_wake_sleep` in
  `pipeline/bot.py` calls when the assistant goes from busy to idle. It does not sleep while a user
  turn is in progress — an interruption, or a request following the name after a pause.
- A second utterance becomes a new turn only after the previous turn has ended
  (`UserStoppedSpeakingFrame`).

## 4. Custom STT service

Base class: `STTService` in `services/stt_service.py`.

- Constructor arguments include `audio_passthrough=True` (audio frames continue downstream; the
  meeting recorder needs them), `sample_rate` and `ttfs_p99_latency`.
- The base `process_frame` calls `process_audio_frame` → `run_stt(frame.audio)` for each
  `InputAudioRawFrame` and forwards VAD frames after internal handling.
- Subclasses implement `async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]`.
  A streaming backend hands the audio over and yields `None`; results are pushed later from a
  background task with `await self.push_frame(frame)`.
- To observe VAD events, override `process_frame` and call
  `await super().process_frame(frame, direction)` **first**; it does the forwarding.
- Lifecycle hooks `start`, `stop` and `cancel` call `super()` first. Background tasks are created
  with `self.create_task(coro)` and cancelled with `await self.cancel_task(task)`.
- Transcription frames:

  ```python
  from pipecat.frames.frames import InterimTranscriptionFrame, TranscriptionFrame
  from pipecat.utils.time import time_now_iso8601

  TranscriptionFrame(text="...", user_id="", timestamp=time_now_iso8601(), result=delta, finalized=False)
  InterimTranscriptionFrame(text="...", user_id="", timestamp=time_now_iso8601(), result=delta)
  ```

  The rules about empty text and `finalized` in interfaces.md §3.4 derive from
  `turns/user_stop/turn_analyzer_user_turn_stop_strategy.py`.

- `includes_inter_frame_spaces` defaults to `False` on text frames, and the user aggregator then
  inserts a space between consecutive transcriptions. Because one Chinese sentence is emitted as
  many frames, each frame sets `frame.includes_inter_frame_spaces = True` after construction (it is
  not a constructor argument).

Points confirmed while writing `StreamingASRService` (`services/stt_service.py`,
`services/ai_service.py`):

- `InputAudioRawFrame` and the VAD frames are **system frames**, processed in arrival order. The
  base `process_frame` forwards them; a subclass must not forward them again.
- The base `process_audio_frame` returns early when the service is muted, reconnecting or not
  usable, without calling `run_stt`. Counting that must never stop — the session timeline — belongs
  in the overridden `process_audio_frame`, before `super()`.
- `AIService.start()` calls `self._settings.validate_complete()`, which logs an error for fields
  left `NOT_GIVEN`. A custom service passes `settings=STTSettings(model=None, language=None)`.
- Exceptions raised in `start()`, `stop()` and `cancel()` are swallowed and logged. `start()` must
  not do slow work, since it holds back `StartFrame`; slow initialization goes into a background
  task.
- `push_error` is not used for failures that recover on their own. A permanent error category marks
  the service unusable (`set_usable(False)`) and the base class stops sending it audio. Recoverable
  failures are logged and reported to the browser with a `notice`.
- Pushing transcription frames from a background task is allowed. Data frames and system frames have
  separate priorities downstream, so transcription frames do not keep their position relative to
  audio frames.

Existing implementations worth reading: `services/whisper/stt.py` (segmented) and `services/funasr/`
(local streaming).

## 5. LLM and TTS services

```python
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.openai.tts import OpenAITTSService

class RealtimeLLMService(OpenAILLMService):
    """One class for both access modes; the differences come from configuration."""

    def __init__(self, *, supports_developer_role: bool, **kwargs):
        super().__init__(**kwargs)
        # A class attribute (True) in the base class. Many servers do not know the "developer"
        # role; when False, Pipecat converts it to "user". Late asynchronous tool results are
        # injected as developer messages, so this must match the server.
        self.supports_developer_role = supports_developer_role

rt = cfg.realtime_llm
endpoint = rt.active
llm = RealtimeLLMService(
    supports_developer_role=rt.supports_developer_role,
    base_url=endpoint.base_url,
    api_key=secret(endpoint.api_key_env) or "none",      # the SDK requires a non-empty value
    settings=RealtimeLLMService.Settings(
        model=endpoint.model,
        system_instruction=system_prompt,
        temperature=..., top_p=..., max_tokens=...,       # omit values that are None
        extra={"extra_body": rt.request_extra_body(background=False)},
    ),
)

tts = LocalTTSService(                                    # subclass of OpenAITTSService, see below
    base_url=cfg.tts.base_url,
    api_key=secret(cfg.tts.api_key_env) or "local",
    settings=LocalTTSService.Settings(model=cfg.tts.model, voice=cfg.tts.voice),
    sample_rate=cfg.tts.sample_rate,
)
```

**A TTS subclass is required.** `run_tts` in `services/openai/tts.py` rejects voices that are not in
OpenAI's fixed list (`VALID_VOICES`). `LocalTTSService` overrides `run_tts` with the parent's
implementation minus that check, uses the configured voice and passes the language through
`extra_body`. The rest is kept: `response_format="pcm"`, streaming `TTSAudioRawFrame`s with
`with_streaming_response`, and `stop_ttfb_metrics()` on the first packet.

LLM service:

- `Settings.extra` is merged into the arguments of `chat.completions.create`
  (`build_chat_completion_params` in `services/openai/base_llm.py`). Fields unknown to the OpenAI
  SDK must be wrapped in `extra_body`.
- The constructor arguments `model=` and `params=` are deprecated; use `settings=`.
- Out-of-band inference, used for warm-up, screen summaries and running summaries:
  `await llm.run_inference(context, max_tokens=..., system_instruction=...)` returns a string and
  uses the service's own settings. Background work uses a separate `RealtimeLLMService` instance
  with `rt.request_extra_body(background=True)`: in llama.cpp mode the two instances occupy
  different slots; in the generic mode the separation makes background requests cancellable on their
  own.
- `top_k` is placed in `extra_body` by `request_extra_body`; it is not passed as `Settings.top_k`,
  which the OpenAI-compatible base class does not send.
- `run_inference(context, max_tokens=N)` sends `max_completion_tokens: N`, not `max_tokens`. If
  `Settings.max_tokens` is also set, both are sent, so the background instance omits the configured
  `max_tokens`. llama.cpp treats `max_completion_tokens` as an alias of `max_tokens`; other servers
  may not accept it. A request-body regression test is in `tests/test_caption_wiring.py`.
- `system_instruction=None`, or an empty string on the service, adds no empty system message.
- `run_inference` is a plain coroutine: cancelling the task that awaits it aborts the HTTP request.
  This is how background requests are preempted.
- `LLMContext(messages)` takes a list of messages; image messages are built with
  `LLMContext.create_image_url_message(url="data:image/webp;base64,...", text="...")`.

TTS service:

- `run_tts(self, text, context_id)` is the 1.12.0 signature; `self.chunk_size` derives from the
  sample rate fixed in `start()`.
- `max_consecutive_zero_audio_contexts` is set to 0. By default three consecutive sentences without
  audio mark the service unusable for the rest of the session, which is wrong for a local server
  that recovers after a restart. Configuration errors (400, 404) are still classified as permanent.
- Failures are reported as `ErrorFrame(error=..., exception=e)`, not raised. With the exception
  attached Pipecat can classify the error; connection failures and 5xx are not permanent. Pipecat
  adds a generic "no audio produced" error when the sentence completes.
- `stop_frame_timeout_s` defaults to 3 s between the last audio chunk and `TTSStoppedFrame`. It
  should not be shorter than the gap between two generated sentences.
- The OpenAI SDK retries twice by default, which only delays the error for a single sentence;
  `client.with_options(max_retries=0)` disables it for TTS.
- Loopback addresses bypass the system proxy: `DefaultAsyncHttpxClient(trust_env=False)`, for the
  LLM client as well.
- Text is aggregated into sentences before synthesis (`text_aggregation_mode`, default `SENTENCE`).
  The default `SimpleTextAggregator` splits Chinese correctly at `。！？；` and not at commas or
  colons; `Dr.`, `v2.0` and `3.5` in mixed text are not split; feeding one character at a time or
  several gives the same result (regression test in `tests/test_local_services.py`). A sentence is
  released when the next character arrives or the response ends, so a short first sentence gives
  faster first audio — a matter for the prompt.
- `tts-server` streams 24 kHz, 16-bit mono PCM; see the end of interfaces.md §9.

## 6. Tools

```python
from pipecat.adapters.schemas.direct_function import tool_options
from pipecat.frames.frames import FunctionCallResultProperties
from pipecat.services.llm_service import FunctionCallParams

async def recall(params: FunctionCallParams, query: str = "", speaker: str = "", limit: int = 10):
    """Find things said in the meeting.

    Args:
        query: Keywords or a sentence describing what to find.
        speaker: Speaker name; empty for anyone.
        limit: Maximum number of results.
    """
    store = params.app_resources.store
    await params.result_callback({"items": [...]})

@tool_options(cancel_on_interruption=False, timeout_secs=960)
async def delegate_task(params: FunctionCallParams, goal: str, minutes_of_context: int = 5):
    """Hand research, image reading or computation to the background agent."""
    task = await params.app_resources.tasks.submit(...)
    await params.result_callback(
        {"task_id": task.label, "status": "accepted"},
        properties=FunctionCallResultProperties(is_final=False),
    )
    result = await params.app_resources.tasks.wait(task.id)
    await params.result_callback({"task_id": task.label, "status": "succeeded", "brief": result.brief})

context = LLMContext(tools=[recall, delegate_task, ...])   # listing them registers them
```

- **Direct functions.** The first parameter is `params: FunctionCallParams`; type annotations and a
  Google-style docstring become the tool definition (`adapters/schemas/direct_function.py`). A
  function wrapped with `functools.wraps` still works. Parameters are limited to `str`, `int` and
  `float`; `Literal[...]` has not been verified.
- `cancel_on_interruption=False` means the conversation does not wait for the tool. A late result is
  injected into the context as a new message and triggers a generation. This is the mechanism behind
  "acknowledge now, report later".
- An intermediate result with `is_final=False` neither ends the call nor resets its timeout.
- **How tool calls appear in the context** (`llm_response_universal.py`). On
  `FunctionCallInProgressFrame` the assistant aggregator adds **two** messages at once — `assistant`
  with `tool_calls`, and `tool` with the content `"IN_PROGRESS"` — and rewrites the `tool` message
  in place when the result arrives. Lines appended meanwhile cannot land between the two. After
  writing the result the aggregator pushes a context frame upstream and the model continues.
- A tool function can start before those two messages exist, because the frame travels through TTS
  and the transport first. A tool that adds its own message with `params.context.add_message(...)`
  and wants it after the call record waits until `tool_call_id` appears in the context, as
  `look_at_screen` does.
- In tests, `run_test` creates its own `PipelineWorker` and takes no `app_resources`; set
  `llm.pipeline_worker._app_resources` once the pipeline is running (`tests/test_tools.py`).
- **`skip_tts` and tools.** `LLMConfigureOutputFrame` sets persistent state on the LLM service, not
  a one-shot flag. A request with a tool call generates twice — the call, then the answer — and a
  configuration frame arriving in between affects the second generation. Modality is therefore set
  **before** a request enters the model and never "restored" afterwards. The regeneration after a
  tool result is an `LLMContextFrame` pushed **upstream** by the assistant aggregator and is not
  seen by processors placed before the model.
- Tool calls have no "finished" event. The LLM service fires `on_function_calls_started`; the next
  `on_assistant_turn_started` marks the continuation. A generation that only calls tools still fires
  `on_assistant_turn_started` and `on_assistant_turn_stopped`, with empty `message.content`.
- **Asynchronous tools** (`cancel_on_interruption=False`):
  - `@tool_options(...)` attaches attributes to the function and goes outermost.
  - Context messages are produced by `processors/aggregators/async_tool_messages.py`: a `tool`
    placeholder at the start, one `developer` message per intermediate result and one for the final
    result (or an in-place rewrite of the placeholder if no user or developer message arrived
    meanwhile). The content is JSON —
    `{"type": "async_tool", "status", "tool_call_id", "description": <English text>, "result": <re-encoded string>}`
    — produced by the default `json.dumps`, so **Chinese is escaped as `\uXXXX`**.
    `async_tool_messages.parse_message(msg)` decodes it for `tool` and `developer` roles.
  - Intermediate and final results both trigger a generation, deferred while the user is speaking or
    the assistant is talking.
  - Whenever an asynchronous tool is registered, `LLMService._compose_system_instruction` appends
    `ASYNC_TOOL_INSTRUCTIONS` (English) to the system prompt. There is no switch; the subclass
    overrides the method to remove it.
  - To change LLM service state before the generation triggered by the final result — `skip_tts`,
    for instance — call `await params.llm.queue_frame(frame)` and then `result_callback`; the frame
    is queued ahead of that generation.
  - `FunctionCallResultProperties(on_context_updated=coroutine_function)` is called, in a separate
    task, after the result has been written to the context.
- `FunctionCallParams` fields: `function_name, tool_call_id, arguments, llm, pipeline_worker,
context, result_callback, app_resources, worker_runner`.

## 7. MCP client

```python
from mcp.client.session_group import StreamableHttpParameters
from pipecat.services.mcp_service import MCPClient

mcp = MCPClient(
    server_params=StreamableHttpParameters(url=server.url, headers={...}, timeout=server.timeout_secs),
    tools_filter=cfg.realtime.direct_mcp_tools,     # expose only the listed tools
)
tools_schema = await mcp.tools()      # tools with handlers; pass them to LLMContext
```

`register_tools(llm)` is deprecated. This client serves only the optional feature of letting the
realtime LLM call a few MCP tools directly; the background agent uses the MCP client of its own
framework.

## 8. Context operations

| Goal                                   | How                                                          |
| -------------------------------------- | ------------------------------------------------------------ |
| Append messages without generating     | Push `LLMMessagesAppendFrame(messages=[...], run_llm=False)` |
| Append messages and generate           | The same with `run_llm=True`                                 |
| Replace the whole context (compaction) | `LLMMessagesUpdateFrame(messages=[...], run_llm=False)`      |
| Make the assistant say something       | `TTSSpeakFrame(text="...")`                                  |
| Read the current messages              | `context.get_messages()`                                     |

- `LLMMessagesUpdateFrame` is consumed by the user aggregator (`set_messages`) and not forwarded.
  Both aggregators share one `LLMContext`, so the assistant side sees the new content. The system
  prompt lives in `Settings.system_instruction` and tools in `LLMContext.tools`; neither is part of
  the message list.
- Request parameters (`get_chat_completions` in `services/openai/base_llm.py`):
  `adapter.get_llm_invocation_params(context, system_instruction=…, convert_developer_to_user=…)`
  yields messages, tools and `tool_choice`, and `build_chat_completion_params` adds sampling
  parameters and `Settings.extra`. Warm-up requests take the same path
  (`RealtimeLLMService.request_params`), so their messages and tools are identical to a real
  request; `tests/test_context.py` compares the two request bodies.
- Messages are OpenAI-format dictionaries. Append frames are handled by the user aggregator and must
  be pushed **upstream** of it, where the meeting recorder is. From outside the pipeline use
  `await worker.queue_frame(frame)`.
- Aggregator events (`@aggregator.event_handler("...")`): `on_user_turn_started` and
  `on_user_turn_stopped` on the user side; `on_assistant_turn_started` and
  `on_assistant_turn_stopped` on the assistant side. The latter carries the text of the turn and is
  used to store the assistant's words.

## 9. Messages to the browser

```python
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame

await self.push_frame(RTVIServerMessageFrame(data={"type": "caption", ...}))
```

- It is a system frame: use `push_frame` inside a processor and `worker.queue_frame` outside the
  pipeline.
- The browser receives `data` in the `onServerMessage` callback of `PipecatClient`.
- Custom messages from the browser arrive in the `on_client_message` event of `RTVIProcessor`
  (`processors/frameworks/rtvi/processor.py`).

## 10. Custom processors

```python
from pipecat.frames.frames import Frame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

class MeetingRecorder(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)      # required, first
        ...
        await self.push_frame(frame, direction)            # required, or downstream gets nothing
```

- **System frames and data frames are queued separately; their relative order is not guaranteed.**
  `InputAudioRawFrame`, the VAD frames and `RTVIServerMessageFrame` are system frames and jump the
  queue; transcriptions and text are data frames. A processor must not assume that
  `VADUserStartedSpeakingFrame` arrives after the transcription that preceded it. Tests put a
  `SleepFrame` between steps and compare system and data frames separately.
- **Attribute names.** Do not reuse names that `FrameProcessor` or `BaseObject` use internally:
  `_name`, `_id`, `_metrics`, `_task_manager`, `_event_handlers`, `_setup`, `_next`, `_prev`.
  Defining a method called `_name`, for example, fails with `'str' object is not callable`.
- `BotStartedSpeakingFrame` and `BotStoppedSpeakingFrame` are pushed by the output transport in
  **both** directions (`transports/base_output.py`). A processor upstream of the output transport,
  such as the meeting recorder, receives the **upstream** copy, so `process_frame` must handle
  `direction == FrameDirection.UPSTREAM`.
- `LLMMessagesAppendFrame` is consumed by the user aggregator and not forwarded to the assistant
  aggregator; an append appears in the context exactly once.
- `on_assistant_turn_stopped(aggregator, message)`: `message.content` is the full text of the turn
  (possibly empty if interrupted before any token) and `message.interrupted` tells whether it was
  interrupted.
- **Observability.** `PipelineParams(enable_metrics=True)` with `MetricsLogObserver()` and
  `UserBotLatencyObserver()` is sufficient. The latter's `on_latency_breakdown` event provides a
  `LatencyBreakdown` whose `turn_contribution_lines()` splits "user stopped → assistant started" by
  stage; `pipeline/bot.py` logs it.
- `PipelineWorker.cancel()` cancels the pipeline immediately (used when the client disconnects);
  `stop_when_done()` queues an `EndFrame`. `run_test` accepts a whole `Pipeline`, which is how the
  end-to-end tests run.

## 11. Multiple workers (not used)

`pipecat.workers` provides a bus that could run the background agent as a separate worker, even on
another machine (`workers/proxy/websocket/`; see `workers/base_worker.py` for `request_job`, `job`,
`send_job_update`, `send_job_response`, `cancel_job_group`). The project does not use it: the task
manager runs in-process `asyncio` tasks, which is simpler and sufficient.

## 12. Browser client

Packages and versions are pinned in `client/package.json`; the type definitions under
`client/node_modules/@pipecat-ai/*/dist/index.d.ts` are authoritative.

```ts
import { PipecatClient } from "@pipecat-ai/client-js";
import { PipecatClientAudio, PipecatClientProvider } from "@pipecat-ai/client-react";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";

const client = new PipecatClient({
  transport: new SmallWebRTCTransport(),
  enableMic: true,
  enableCam: false,
  callbacks: {
    onTransportStateChanged: (state) => {},   // "ready" means the server pipeline is up
    onServerMessage: (data) => { /* custom messages, interfaces.md §6.1 */ },
    onBotLlmStarted: () => {}, onBotLlmText: ({ text }) => {}, onBotLlmStopped: () => {},
    onBotStartedSpeaking: () => {}, onBotStoppedSpeaking: () => {},
    onTrackStarted: (track, participant) => {},  // participant?.local marks the local microphone
    onDeviceError: (error) => {}, onError: (message) => {},
  },
});
await client.connect({ webrtcRequestParams: { endpoint: "/api/offer", requestData: { /* optional */ } } });
client.sendClientMessage("text_input", { text: "..." });
await client.disconnect();
```

- Connection parameters are `webrtcRequestParams: { endpoint, requestData? }`; `webrtcUrl` and
  `connectionUrl` are deprecated. `connect()` resolves once the server pipeline is ready and rejects
  on failure. `requestData` appears under the camel-case key in the body of `/api/offer` (§2).
- **The assistant's audio must be attached to an `<audio>` element.** The transport does not play
  it. Wrap the UI in `<PipecatClientProvider client={client}>` and include `<PipecatClientAudio />`.
  The client object is created once; starting and ending a meeting call `connect()` and
  `disconnect()` on it.
- The default media manager, `DailyMediaManager` (`@daily-co/daily-js`), does not expose microphone
  constraints and requests the microphone without any. Echo cancellation is checked in
  `onTrackStarted` with `track.getSettings().echoCancellation` and enabled with
  `track.applyConstraints({ echoCancellation: true })` when needed. Noise suppression and automatic
  gain are left at the browser defaults; whether the browser's automatic gain takes effect on a
  given device has not been verified. The page logs `getSettings()` to the console.
- **The SDK reconnects on its own.** After a server restart a new connection appears without user
  action (`POST /api/offer`, a new `pc_id`). This reconnection does **not** fire RTVI `client-ready`
  again, so the `session` message sent from `on_client_ready` is not delivered. The page falls back
  to HTTP, checking the current session when the connection becomes ready and every five seconds
  (`useMeetingClient.ts`). A reconnection with the same `pc_id` (ICE restart) is logged by the server
  as `Reusing existing connection` and does not start a new bot.
- **Connection parameters used by SDK reconnects can be changed afterwards.** The transport keeps
  the object passed in `connect({ webrtcRequestParams })` and reads `requestData` from it for every
  offer. The page always passes the same object and writes `session_id` into it once the session is
  known, so an SDK-initiated reconnect (a new PeerConnection five seconds after ICE disconnects, up
  to three times) resumes the same meeting. When the SDK gives up, the page's own reconnection with
  backoff of 1, 2, 4, 8, 8 s takes over.
- **ICE servers are set on the transport.** `new SmallWebRTCTransport()` uses no ICE servers, and
  nothing from the server's `SmallWebRTCRequestHandler` reaches it. The transport has an
  `iceServers` setter (`RTCIceServer[]`) that is read each time it creates an `RTCPeerConnection`,
  SDK reconnects included. The page sets it from `GET /api/ice` before each `connect()`
  (`useMeetingClient.ts`).
- `tsconfig` uses `verbatimModuleSyntax` and `allowImportingTsExtensions`: relative imports carry
  the `.ts` extension so that pure-logic modules can be run directly by Node's test runner
  (`npm test`). `*.test.ts` files are excluded from `tsc` and the bundle.
- Screenshots do not use the SDK's `enableScreenShare`, which would create a WebRTC video track. The
  page calls `navigator.mediaDevices.getDisplayMedia()`, draws the picture onto a `<canvas>` and
  uploads still images.
