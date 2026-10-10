# Architecture

**English** · [简体中文](zh-CN/architecture.md)

This document describes the components of the system, how data flows between them and why the
design looks the way it does. Field-level formats are in [interfaces.md](interfaces.md); Pipecat
class names and import paths are in [pipecat-notes.md](pipecat-notes.md).

## 1. Overview

```
Browser                                      Server host (GPU)
┌────────────────────────┐              ┌───────────────────────────────────────────────┐
│ Microphone ────────────┼─ WebRTC ────▶│              Application (Python)             │
│ Assistant voice ◀──────┼─ WebRTC ─────┤  ┌────────────── Pipecat pipeline ─────────┐  │
│ Captions/answers/tasks◀┼─ data chan. ─┤  │ input → VAD → ASR → meeting recorder →  │  │
│ Screenshots ───────────┼─ HTTPS ─────▶│  │ wake/turn → realtime LLM → TTS → output │  │
│ Renames / queries ─────┼─ HTTPS ─────▶│  └─────────────────────────────────────────┘  │
└────────────────────────┘              │  Speaker diarization (in-process library)     │
                                        │  Storage (SQLite + screenshot files)          │
                                        │  Task manager ── agent ──▶ remote LLM / MCP   │
                                        │                      └──▶ code sandbox (Docker)│
                                        └──────┬───────────┬───────────┬──────────┬─────┘
                                          HTTP │      HTTP │      HTTP │     HTTP │
                                        ┌──────▼─────┐┌────▼──────┐┌───▼──────┐┌──▼────────┐
                                        │Realtime LLM││    ASR    ││   TTS    ││ Embeddings│
                                        │llama-server││llama-server││tts-server││llama-server│
                                        └────────────┘└───────────┘└──────────┘└───────────┘
```

The diagram shows the realtime LLM served by llama.cpp. It can instead be any OpenAI-compatible
chat completions endpoint (`realtime_llm.mode`), in which case that box lives outside the project
and everything else stays the same. Section 6.1 lists the differences.

Three decisions shape the design:

1. **Inference services are separate processes.** The application talks to them over HTTP only, so
   models can be swapped, moved to another GPU or restarted without touching application code, and
   any of them can be replaced by a service started elsewhere.
2. **Transcription is decoupled from answering.** The transcription path (ASR → speakers → storage →
   captions) always runs, whether or not the assistant has been woken. The answering path starts
   only after the wake word. Processor order guarantees this: the meeting recorder sits upstream of
   the wake-word gate.
3. **Fast and slow paths are split.** The realtime LLM handles only what can be answered within
   about a second. Everything else goes to the background agent, whose results return to the
   conversation asynchronously.

### Design goals

- Everything that touches raw audio runs locally on open-weight models.
- Models are a deployment choice. Paths, endpoints, voices and sampling parameters come from
  configuration; model-specific prompt formats live in `config/asr_profiles/` and `config/prompts/`.
- One command validates the configuration; one command starts and supervises all local services.
- Data that leaves the machine is always visible to the user: the transcript sent to a remote
  realtime endpoint, and the material sent to the agent model for each task.

Latency targets, with all local services running and the realtime LLM served by llama.cpp:

| Metric              | Target                                                              |
| ------------------- | ------------------------------------------------------------------- |
| Caption lag         | Finalized text no more than 1.5 s behind speech                     |
| Time to first text  | About 1 s after the speaker stops (P50 ≤ 1.0 s, P90 ≤ 1.8 s)        |
| Time to first audio | Within 0.5 s of the first text                                      |
| Recall tools        | ≤ 100 ms by speaker, time or keyword; ≤ 400 ms with semantic search |

These targets are not promised for a generic chat completions endpoint, where prefix caching and
network latency are outside the project's control.

## 2. Processes and ports

Ports come from the configuration; the table shows the defaults in the template.

| Process      | Default port | Program                                                                                                                   | Required                                  |
| ------------ | ------------ | ------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| Application  | 7860         | `agentic-meeting serve`                                                                                                   | Yes                                       |
| Realtime LLM | 8080         | Mode 1: `llama-server` (chat and vision, two slots). Mode 2: an external chat completions endpoint; no process is started | Yes                                       |
| ASR          | 8081         | `llama-server` with audio input                                                                                           | Yes                                       |
| TTS          | 8082         | `tts-server`                                                                                                              | No (answers are text-only without it)     |
| Embeddings   | 8083         | `llama-server --embedding`                                                                                                | No (keyword search only without it)       |
| Diarization  | —            | Library loaded into the application process                                                                               | No (captions carry no speaker without it) |
| Code sandbox | —            | One Docker container per task                                                                                             | No                                        |

Inference services started by the application listen on `127.0.0.1` only. The application port is
the only one exposed.

## 3. Session timeline

Every record is placed on one axis, the **session timeline**: seconds since the first audio frame of
the meeting.

- The timeline advances by counting samples of received audio at a fixed 16 kHz:
  `t = samples_received / 16000`. It does not use the wall clock, so processing delays and clock
  differences between modules do not affect it.
- The ASR service and the meeting recorder each count the audio frames that pass through them. They
  see the same frames, so they agree. For this to hold, the ASR service is never muted (no
  `STTMuteFrame`) and no audio frames are dropped between the two.
- Screenshots: the browser synchronizes its clock with the server when it connects
  (interfaces.md §5.1) and stamps each screenshot with the capture time in server time, which the
  server converts to the session timeline.
- The Unix time of the timeline origin is stored as `sessions.started_at` and added when a
  wall-clock time is needed.

### 3.1 Session lifecycle

A meeting is a **session**. A session is not a connection: connections drop and devices change, and
the session carries on.

The state is derived from `ended_at` in the database and whether a live connection is attached:

| State       | Condition                                                                                                    | Available in the page                                   |
| ----------- | ------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------- |
| Live        | A connection is attached to the session                                                                      | Live captions, end                                      |
| Interrupted | `ended_at` is empty and no connection is attached (page closed or refreshed, network lost, server restarted) | Resume, review, export, rename, delete                  |
| Ended       | `ended_at` is set                                                                                            | Resume (reopen), review, export, report, rename, delete |

Operations:

| Operation                      | Trigger                                                 | Behavior                                                                                                                                                                         |
| ------------------------------ | ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Start                          | "Start meeting"; the connection carries no `session_id` | Creates a session whose timeline starts now. An existing live connection is taken over first and its meeting becomes interrupted. Other unfinished meetings are left as they are |
| Disconnect                     | Page closed or refreshed, network lost                  | Only the connection is closed. `ended_at` is not written; the session becomes interrupted                                                                                        |
| End                            | "End meeting"                                           | Disconnects, writes `ended_at` and produces the final running summary                                                                                                            |
| Resume                         | "Resume"; the connection carries a `session_id`         | See below                                                                                                                                                                        |
| Rename, delete, merge speakers | Meeting list                                            | Deletion asks for confirmation and is refused for a live meeting; it also removes screenshot files and task directories                                                          |

**There is one live connection at a time.** ASR runs a single slot, diarization is one stream and the
realtime LLM has one context. When a new connection arrives, the server cancels the old pipeline,
waits for it to release its resources and then starts the new one. A page that is still connected
receives a notice and returns to the disconnected state.

**Resuming** (`session_id` travels in `requestData` of `POST /api/offer` and is validated before
negotiation; an unknown id yields 404):

1. **Timeline.** Session time is wall time minus `started_at`. It is sampled once when the
   connection is established to obtain `base_secs`, and advances by sample count from there:
   `t = base_secs + samples_received / 16000`. `base_secs` is never earlier than the point the
   timeline has already reached, so time cannot run backwards on a quick reconnect. The gap during
   the interruption stays on the timeline, and screenshots use the same origin. Each connection's
   span is recorded in `session_connections`.
2. **Diarization.** Within one server process the same diarization stream is reused, so speaker
   numbers are stable. Stream time is the cumulative duration of audio actually fed, and each
   connection records a pair (stream time, session time) for conversion; no silence is fed for the
   gap. After a server restart a new stream is opened and numbering restarts at 1, so the new numbers
   are offset by the highest existing speaker number. A returning speaker may therefore appear as a
   new one, which can be merged in the UI, but two different people are never collapsed into one.
   The stream is owned by the session manager (`SessionDiarizer` in `diar/stream.py`) and is released
   when another meeting starts, when this one is ended or deleted, or when the application exits.
3. **Realtime LLM context.** A fresh `LLMContext` is rebuilt from the database: the latest running
   summary plus the transcript and screen lines of the last `keep_recent_minutes`. This is the same
   code as compaction (§6).
4. **ASR.** Each connection starts with an empty window; `ASRDelta.audio_end_secs` is offset by
   `base_secs`.
5. **Speaker names, captions, screenshots and tasks** are in the database and remain untouched.
6. The server sends a `session` message once the connection is up (interfaces.md §6.1); the page
   then loads recent captions over HTTP.

The page is useful without a connection: the meeting list, recent captions, exports, reports,
renaming and deletion all use plain HTTP (interfaces.md §5.1, §5.4). Another device can follow a
live meeting by polling the captions endpoint.

## 4. Pipeline

Processors in order, upstream to downstream:

| #   | Processor                                      | Origin                                     | Responsibility                                                                                                                                                  |
| --- | ---------------------------------------------- | ------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Transport input with the **input gain** filter | Pipecat; the filter is ours                | Reads 16 kHz mono audio from WebRTC and applies `audio_in_filter` to lift quiet input before pushing frames downstream                                          |
| 2   | Voice activity detection                       | Pipecat `VADProcessor`                     | Emits speech-started and speech-stopped frames                                                                                                                  |
| 3   | `StreamingASRService`                          | Ours                                       | Sends audio to the ASR backend while someone is speaking and turns incremental results into transcription frames                                                |
| 4   | `MeetingRecorder`                              | Ours                                       | Feeds diarization, attributes text to speakers, splits it into captions, stores them, pushes caption messages and appends finalized captions to the LLM context |
| 5   | User context aggregator                        | Pipecat                                    | Wake-word and turn detection; after wake-up, adds the user's words to the context and triggers the model                                                        |
| 5.5 | `ModalityGate`                                 | Ours                                       | Sets the output modality of each new request before it reaches the model: text only for typed requests, speech for spoken ones (§5.5)                           |
| 6   | Realtime LLM service                           | Pipecat's OpenAI-compatible service        | Generates text and calls tools                                                                                                                                  |
| 7   | TTS service                                    | Pipecat, pointed at the local `tts-server` | Text to speech                                                                                                                                                  |
| 8   | Transport output                               | Pipecat                                    | Sends audio back to the browser                                                                                                                                 |
| 9   | Assistant context aggregator                   | Pipecat                                    | Writes what the assistant said back to the context                                                                                                              |

Why this order:

- Input gain is part of step 1 (Pipecat's `audio_in_filter`) rather than a separate processor, so
  that steps 2–4 and diarization all see the same processed audio. The filter is length-preserving
  and does not disturb the sample count. It must precede VAD, whose volume gate (about −50 LUFS by
  default; pipecat-notes.md §3.1) would otherwise cut quiet speech into fragments
  ([benchmarks.md](benchmarks.md#low-microphone-level)).
- VAD precedes ASR because the ASR service uses the speech-started and speech-stopped events to
  decide when to send audio and when to finalize.
- The recorder precedes the user aggregator because Pipecat's wake-word strategy clears
  transcription text while idle so that it never reaches the context. The recorder has to store the
  frame first and then pass it on unchanged.
- The recorder, not the aggregator, appends finalized captions to the context. That way the model
  has "heard" the whole meeting whether or not it was woken.

## 5. Data flow

### 5.1 Transcription

```
audio ─┬─▶ ASR service ──(while speaking)──▶ ASR backend ──▶ increments (stable text + unstable tail)
       │                                             │
       │                          transcription frames (final) and interim frames (captions)
       │                                             ▼
       └─▶ meeting recorder ──▶ diarization      recorder: attribute text to speakers by time
                                   │                 │
                                   └── segments ────▶│
                                                     ├─▶ caption messages ──▶ browser
                                                     ├─▶ stored captions (closed on pause or speaker change)
                                                     └─▶ one transcript line appended to the LLM context
```

- At every step the ASR backend gives the model the current audio window plus the already
  finalized text as a prefix to continue, and holds back the last few tokens as provisional. This is
  the upstream streaming algorithm of the ASR model, implemented on top of a stock `llama-server`
  with audio input (interfaces.md §3).
- A pause finalizes the tail and clears the window. One speech-started to speech-stopped span is one
  ASR segment.
- A **caption** ends when the ASR segment ends or the speaker changes within it.
- Speaker decisions lag behind the text. A caption is stored with the best decision available; if
  diarization revises it within the next few seconds, the database is updated and the page is told
  to relabel the caption. The line already appended to the LLM context is left as it is.

### 5.2 Answering

```
transcription ──▶ wake-word strategy: does the finalized text contain the assistant's name?
                     │ no:  nothing happens
                     │ yes: a user turn starts; the context cache is warmed
                     ▼
               end-of-turn detection (pause + turn model + final transcription)
                     ▼
               realtime LLM (no reasoning, streaming) ──┬─▶ text ──▶ browser
                                                        ├─▶ TTS ──▶ browser
                                                        └─▶ tool calls
                                                              ├─ synchronous tools: result returns at once
                                                              └─ delegation: returns "accepted"; the result arrives later
```

**Wake window.** After its name is heard the assistant is awake: it answers when the turn ends and
can be interrupted by speech while it talks. With `turn.single_activation` (the default) it returns
to idle as soon as it has answered. Pipecat's strategy on its own stays awake for the full timeout,
during which anything said in the room would be treated as addressed to the assistant. The assistant
does not go idle while someone is speaking — that is either an interruption or the request following
the name after a pause — and waits for that turn to be answered. `turn.wake_timeout_secs` is the
upper bound from name to answer (`pipeline/wake.py`, `wire_wake_sleep` in `pipeline/bot.py`).

Tools available to the realtime LLM fall into three groups (interfaces.md §7):

- **Lookups** — recall captions, fetch the running summary, check task progress. Milliseconds,
  synchronous.
- **Screen** — add a screenshot to the context as an image. Costs a few hundred milliseconds of
  image encoding.
- **Delegation** — create a background task. The tool is declared as not cancelled by interruptions;
  it reports acceptance at once and the final result when the task ends. Pipecat inserts the late
  result into the context and triggers a new generation, which becomes the spoken briefing.

### 5.3 Screenshots

```
browser: screen ─▶ downscale and compare with the last upload ─▶ changed beyond the threshold, or heartbeat due
                                                  ▼
                                    encode as WebP ─▶ HTTPS upload with capture time
                                                  ▼
server: validate ─▶ write file ─▶ store ─▶ timeline message ─▶ browser
                                    └─▶ summary job (background slot, when idle) ─▶ stored
                                                               └─▶ one "screen" line appended to the context
```

Screenshots do not travel over a WebRTC video track: video encoding blurs small text on slides and
wastes bandwidth. Still images are sharp, precisely timed and produced only when the picture
changes.

**Who sees the original image.** By default, three places: summary generation, the `look_at_screen`
tool, and tasks delegated with a request to include the screen (the three most recent
screenshots). Everything else — the context, running summaries, reports — uses the text summaries.

- `look_at_screen` without arguments attaches the latest screenshot and lists earlier ones (id,
  time, summary). The model decides from the summaries whether it needs to look back, and if so
  calls the tool again with the ids, at most three per call.
- With `agent.attach_frames`, work given to the agent model carries the originals: tasks get every
  screenshot up to the time of delegation, the report gets those of the whole meeting (or of each
  section when the transcript is split; the final merge request gets none), and each running summary
  gets those of its own time span. Reports and summaries carry images only when that model writes
  them (`report.provider`, `realtime.digest_provider`).
- Selection (`select_frames` in `screen/attach.py`): pictures summarized as irrelevant are skipped,
  unchanged repeats are sent once, pictures without a summary are kept, and beyond
  `agent.max_attached_frames` the set is sampled evenly, always keeping the first and the last.

### 5.4 Background tasks

```
delegation tool ─▶ task manager: create and queue the task
                        ▼
                 agent runner: assemble input (goal + relevant transcript + screenshots)
                        ▼
                 agent loop: remote LLM ⇄ MCP tools ⇄ code in the sandbox
                        │  each step ─▶ progress event (stored and pushed to the browser)
                        ▼
                 result: spoken brief + details + sources + artifact files
                        ▼
                 reported to the delegation tool ─▶ the realtime LLM phrases a short briefing ─▶ spoken
```

- Tasks have short labels (`t1`, `t2`, …) that are easy to refer to aloud.
- "How is it going?" is answered by the realtime LLM through the status tool, which reads the latest
  progress events.
- If someone is speaking when a task finishes, the briefing waits for a pause, up to a limit.

### 5.5 Typed input and output modality

Typed input is a second entry point that bypasses speech: a participant types a question or
delegates a task in the page. **The answer uses the modality of the request**, so a typed request
never makes the assistant speak over the meeting.

```
text box ──text_input──▶ text entry ─┬─▶ recorder: store (source="text", speaker −2), push utterance, append context line
                                     └─▶ trigger the realtime LLM (no wake word) ──▶ streamed text; TTS skipped
```

| Trigger                  | Answer                                                                       | Mechanism (Pipecat 1.12.0)                                                                                                                                                                                                                            |
| ------------------------ | ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Wake word                | Streamed text and speech                                                     | The modality gate pushes `LLMConfigureOutputFrame(skip_tts=False)` before the request reaches the model                                                                                                                                               |
| Typed input              | Text only                                                                    | The text entry places a `TextRequestFrame` marker ahead of the request. The gate sees it and pushes `LLMConfigureOutputFrame(skip_tts=True)`; the LLM service marks its text frames accordingly and the TTS service passes them through unsynthesized |
| Task completion briefing | The modality of the turn that delegated the task (stored in the task record) | Same configuration frames around the briefing                                                                                                                                                                                                         |

Rules:

- Modality is set **per request**, at the entrance to the model (`pipeline/modality.py`). Wrapping a
  typed request in a pair of configuration frames does not work: when the model calls a tool and
  generates again, the frame that restores speech has already taken effect between the two
  generations. A regeneration triggered by a tool result travels upstream as a context frame, does
  not pass the gate and keeps the modality of its request. With TTS disabled no marker is placed.
- Known edge case: a request of the other modality arriving while a tool call is still executing
  changes the modality. Tool calls are short and typed requests queue while the assistant is busy, so
  this is rare in practice.
- "Busy" includes tool execution. During that time typed requests queue, background models stay
  paused and the context is not compacted.
- Typed requests never interrupt speech or generation in progress. They queue first-in first-out, up
  to five. Other people talking does not count as busy.
- Typed input does not depend on the wake window and does not change the wake state of the voice
  path.

## 6. Context management

Answering within a second depends on the realtime LLM having to process only what is **new**.
`llama-server` caches the prefix of the previous request; a request that begins with the same content
computes only the remainder. Three rules follow:

1. **Append only.** The context is the system prompt, the tool definitions and messages appended in
   time order. Any edit to earlier content invalidates the cache, so speaker renames and label
   corrections are never applied to lines already in the context.
2. **Compact when idle.** Once the context exceeds `realtime.context_budget_tokens`, a moment when
   nobody is addressing the assistant is used to replace the older transcript with the running
   summary, keeping the last few minutes verbatim, followed immediately by a warm-up.
3. **Warm up.** When the wake word is recognized — the speaker needs a few more seconds to finish —
   and on a timer, a request with exactly the same messages and tool definitions is sent to the same
   slot, generating a single token.

Slot 0 serves live answers and warm-ups; slot 1 serves screen summaries and running summaries. When
the wake word is detected, the request in flight on slot 1 is cancelled to free the GPU for the
answer.

Yielding (`pipeline/activity.py`, `pipeline/background.py`):

- **Busy** lasts from the wake word (or a typed request) until generation and speech have finished.
  If the name is followed by nothing, the wake timeout ends it, with a 60-second watchdog as a
  backstop. Busy-to-idle has a 0.5 s delay to avoid flapping between generation and speech.
- **Background model access goes through one gate** (`BackgroundModel`): serialized, paused while the
  assistant is busy, with the in-flight request cancelled on pause. The caller decides whether to
  retry; a screen summary is retried unless a newer picture has arrived.
- Compaction and warm-up use the same busy signal for mutual exclusion.
- Everything resumes when the connection closes.

Implementation of the three rules (`pipeline/context.py`):

- **Estimate.** One token per CJK character, one per four other characters, four per message, 1,500
  per image, with the system prompt and tool definitions counted once. Checked every five seconds;
  the tokenizer endpoint is not called.
- **Compaction** triggers when the estimate exceeds the budget, the assistant is idle and at least
  60 seconds have passed since the last compaction. The running summary is brought up to date first,
  then the context is rebuilt from the database: a summary block, followed by the captions the
  summary does not cover and those of the last `keep_recent_minutes`, with screen lines. The verbatim
  part starts at the earlier of "where the summary ends" and "the start of the recent window", leaving
  no gap. If the recent window alone exceeds the budget, it is halved repeatedly down to one minute.
  The assistant's own words are rebuilt as assistant messages; tool call records and viewed images
  are not kept.
- **Rebuilding goes through the meeting recorder** (`rebuild_context`), which shares a lock with
  "store caption and append context line". Otherwise a caption stored after the rebuild read the
  database would be appended and then wiped by the replacement frame. Screen lines are appended
  through the recorder as well (`append_context_line`).
- **Warm-up** is skipped entirely when `cfg.realtime_llm.cache_warm` is false. Otherwise it fires on
  the wake word, after compaction and on a timer (skipped when the context has not changed), never
  while the assistant is generating or speaking, with at most one request in flight. Failures are
  only logged. `RealtimeLLMService.warm_cache` builds the request with the same code as a real
  request (`request_params`), changing only `stream=False` and `max_tokens=1`.

### 6.1 Differences between the two access modes

The pipeline, tools, context format and the append-only and compaction rules are identical in both
modes. The differences are confined to the properties below; application code reads these properties
and never branches on the mode itself.

| Aspect                                 | Mode 1 `llama_server`                                           | Mode 2 `openai_api`                        | Property                                              |
| -------------------------------------- | --------------------------------------------------------------- | ------------------------------------------ | ----------------------------------------------------- |
| Process                                | Can be launched by the application (`launch.enabled`)           | Not managed; reachability check only       | `cfg.realtime_llm.managed`                            |
| Endpoint, model, key, sampling, vision | Fields of its own subsection                                    | Same                                       | `cfg.realtime_llm.active`                             |
| Extra request fields                   | `extra_body` plus slot id and `cache_prompt`                    | `extra_body` only                          | `cfg.realtime_llm.request_extra_body(background=...)` |
| Isolation of live and background work  | One slot each; neither evicts the other's cache                 | No slots; both hit the same endpoint       | Same                                                  |
| Cache warm-up                          | Always                                                          | Off by default; opt-in                     | `cfg.realtime_llm.cache_warm`                         |
| Disabling reasoning                    | `--reasoning off` at launch, plus template parameters if needed | Provider-specific, written in `extra_body` | —                                                     |
| `developer` role                       | Converted to `user`                                             | Treated as unsupported unless configured   | `cfg.realtime_llm.supports_developer_role`            |
| Data destination                       | Local                                                           | Possibly remote; the user is warned        | `config.check_warnings()`                             |

Cancelling background requests on wake-up applies to both modes: in mode 1 to free the GPU, in mode 2
to avoid competing for the endpoint's concurrency.

Formats of the lines in the context (the system prompt explains these conventions to the model):

```
[00:14:02 王老师] 这个 baseline 的学习率是不是设大了
[00:14:09 说话人3] 我回去再跑一组对比
[画面 00:14:30] 幻灯片：消融实验结果表；去掉数据增强后验证集准确率下降 2.1 个点
[任务 t2 完成] 已核实该论文在 Google Scholar 的引用数为 1,243
```

## 7. Budgets

### 7.1 Latency

Expected contributions in mode 1:

| Stage                                 | Expected  | Depends on                                                                 |
| ------------------------------------- | --------- | -------------------------------------------------------------------------- |
| Deciding the speaker has finished     | 0.2–0.4 s | VAD silence threshold; the turn model takes around ten milliseconds on CPU |
| Final transcription arrives           | 0.1–0.3 s | The last ASR step                                                          |
| First token                           | 0.1–0.2 s | Reasoning disabled and prefix cached                                       |
| First sentence and first audio packet | 0.3–0.5 s | Generation speed and TTS first-packet latency                              |

Measured values, including a full-meeting run through a chat completions endpoint, are in
[benchmarks.md](benchmarks.md).

### 7.2 GPU memory

| Component              | Measured        | Notes                                                                                                                                     |
| ---------------------- | --------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| ASR (Q8, context 8192) | about 3.8 GB    | More than the file size suggests, mostly working memory of the audio encoder                                                              |
| Diarization            | about 0.2 GB    |                                                                                                                                           |
| TTS (0.6B, Q8)         | about 2.4 GB    |                                                                                                                                           |
| Embeddings             | 0               | With `--device none`                                                                                                                      |
| Realtime LLM           | Model-dependent | Estimate: weights + vision projector + context cache (about 32 KB per token; 1 GB for a 32K context). None when a remote endpoint is used |

With two GPUs, put the realtime LLM and TTS on the larger one and ASR and diarization on the smaller
one, so that continuous recognition does not compete with the realtime LLM. On a 22 GB + 6 GB pair
the small card uses about 4 GB and the large one keeps about 18 GB for the LLM. On a single 24 GB
card the three local models total about 6.4 GB. Configuration examples are in
[runtimes.md §5](runtimes.md).

## 8. Repository layout

```
Agentic-Meeting/
├── README.md                  Overview and quick start
├── AGENTS.md                  Guidance for AI coding agents
├── pyproject.toml / uv.lock   Python dependencies (locked)
├── runtimes.lock.toml         Versions of the inference runtimes
├── config/
│   ├── config.example.toml    Configuration template
│   ├── asr_profiles/          Prompt-format profiles of ASR models
│   └── prompts/               Prompts, plain text
├── docs/                      Documentation (English); docs/zh-CN for Chinese
├── scripts/
│   ├── runtimes.py            Fetch or build inference runtimes
│   ├── soak.py                Headless end-to-end replay and soak test
│   ├── eval_realtime_model.py Tool-selection and latency checks for a realtime LLM
│   └── mic_check.py           Microphone level diagnosis
├── src/agentic_meeting/
│   ├── config.py              Configuration loading and validation
│   ├── cli.py                 Command-line entry point
│   ├── types.py               Shared data types
│   ├── audio/                 Input gain and offline diagnosis
│   ├── services/              Process supervision of inference services
│   ├── asr/                   Streaming ASR: backend interface, llama-server backend, Pipecat service
│   ├── diar/                  Diarization: backend interface, library binding, text attribution
│   ├── store/                 SQLite schema and access, vector search
│   ├── pipeline/              Pipeline assembly, recorder, wake word, context management, tools
│   ├── screen/                Screenshot ingestion, summaries, image attachment
│   ├── agent/                 Task manager, agent runner, sandbox
│   └── web/                   HTTP API and static site
├── client/                    Web client (Vite, React, TypeScript)
├── tests/                     End-to-end tests (no GPU required)
├── third_party/               Runtime sources (Git submodules, read-only)
├── runtimes/                  Downloaded runtime binaries (not tracked)
├── models/                    Model weights (not tracked)
└── data/                      Meeting data and logs (not tracked)
```

## 9. Failure handling

**Transcription has the highest priority.** A failure in answering, tasks, screenshots or storage
must never stop recognition and captions.

| Failure                                                 | Effect                         | Handling                                                                                                              |
| ------------------------------------------------------- | ------------------------------ | --------------------------------------------------------------------------------------------------------------------- |
| TTS unavailable                                         | No speech                      | Text answers continue; the page shows a notice with the reason (`pipeline/errors.py`, one notice per kind every 30 s) |
| Embedding service unavailable                           | No semantic search             | Recall falls back to keywords; backfill retries with backoff and catches up later                                     |
| Screen summary fails (model error, no vision, disabled) | No summary for that screenshot | The screenshot stays on the timeline, marked `failed` or `skipped`; `look_at_screen` can still show the image         |
| Screenshot cannot be written or is rejected             | Missing on the timeline        | The page reports it once and retries on the next tick                                                                 |
| Diarization fails to load                               | No speakers                    | Captions are attributed to "unknown"; the page shows a notice                                                         |
| ASR request fails                                       | Captions stop                  | Exponential backoff; a notice after repeated failures; audio timing continues                                         |
| Input gain filter fails                                 | Invisible to the user          | The frame is passed through unchanged and the error is logged                                                         |
| Microphone too quiet (gain at its limit)                | Fragmentary captions           | Input level is logged; the page suggests raising the input volume                                                     |
| Realtime LLM unavailable                                | No answers                     | Transcription continues; the page shows a notice with the reason (unreachable, key rejected, rate limited, timeout)   |
| Remote agent model or MCP unavailable                   | Task fails                     | The task is marked failed with the reason and the assistant says so                                                   |
| Docker missing                                          | No code execution              | The agent still searches and reads images, and reports that it could not run code                                     |
| Browser disconnects                                     | Capture stops                  | The client reconnects; the session is kept and the timeline continues                                                 |
