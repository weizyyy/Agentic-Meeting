# Interface reference

**English** · [简体中文](zh-CN/interfaces.md)

Data formats exchanged between modules, between the server and the browser, and between the
application and the inference services. The implementation follows this document; when an
interface has to change, the document changes first.

- [1. Configuration](#1-configuration)
- [2. Database](#2-database)
- [3. Streaming ASR](#3-streaming-asr)
- [4. Speaker diarization](#4-speaker-diarization)
- [5. HTTP API](#5-http-api)
- [6. Data-channel messages](#6-data-channel-messages)
- [7. Tools of the realtime LLM](#7-tools-of-the-realtime-llm)
- [8. Background tasks](#8-background-tasks)
- [9. Command lines of the inference services](#9-command-lines-of-the-inference-services)

## 1. Configuration

The source of truth is the pydantic model in
[`src/agentic_meeting/config.py`](../src/agentic_meeting/config.py); the template is
[`config/config.example.toml`](../config/config.example.toml). Every key is described in
[configuration.md](configuration.md).

| Section        | Contents                                                                                                                          |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `session`      | Assistant name (the wake word), aliases, ASR hotwords, member list, data directory                                                |
| `server`       | Bind address and port, TLS certificate, ICE servers, access password and login duration                                           |
| `realtime_llm` | Access `mode` and one complete set of settings per mode: `[realtime_llm.llama_server]` and `[realtime_llm.openai_api]`            |
| `asr`          | Backend, prompt-format profile, step and window sizes, provisional tokens, pre-roll, launch settings                              |
| `diarization`  | Backend, library path, model path, GPU index, segmentation thresholds                                                             |
| `tts`          | Endpoint, model field, voice, language, launch settings                                                                           |
| `embedding`    | Endpoint, model field, dimensions, query prefix, similarity floor, launch settings                                                |
| `audio`        | Input gain: switch, initial gain, limit, target level, noise floor, log interval                                                  |
| `turn`         | Silence threshold, VAD volume gate, end-of-turn model, wake window, single activation                                             |
| `realtime`     | Context budget, verbatim window after compaction, summary interval and provider, warm-up interval, direct MCP tools               |
| `screen`       | Screenshot intervals and threshold, summaries and their provider                                                                  |
| `transcript`   | Merging of adjacent segments into one caption                                                                                     |
| `report`       | Report provider and input size per request                                                                                        |
| `agent`        | Remote model endpoint, extra request fields, output budget, screenshot attachment, MCP servers, sandbox, concurrency and timeouts |

Rules:

- Unknown keys are rejected (`extra="forbid"`). A new key is added to `config.py`, the template,
  [configuration.md](configuration.md) and a test.
- Secrets are referenced by **environment variable name** (keys ending in `_env`) and read with
  `config.secret()`.
- Relative paths are resolved against the repository root with `AppConfig.resolve()`.
- Application code does not branch on the realtime LLM access mode. `RealtimeLLMConfig` exposes:

  | Member                                 | Meaning                                                                                              |
  | -------------------------------------- | ---------------------------------------------------------------------------------------------------- |
  | `active`                               | The selected set of settings (`base_url`, `api_key_env`, `model`, `supports_vision`, `sampling`, …)  |
  | `managed`                              | Whether the process supervisor launches the realtime LLM (`llama_server` mode with `launch.enabled`) |
  | `request_extra_body(background=False)` | Fields to place in `extra_body` of each request, already merged for the mode                         |
  | `cache_warm`                           | Whether to send warm-up requests                                                                     |
  | `supports_developer_role`              | Whether the server accepts the `developer` role                                                      |

  Only the process supervisor reads `mode` and `llama_server.launch` directly.

- `config.check_warnings(cfg)` returns items that do not prevent startup but that the operator should
  know about — currently, data leaving the machine. `check` and `serve` print them, and the browser
  receives one `notice` per item after connecting.
- Model-specific strings stay out of the code. Chat template, prefix format and output markers of
  the ASR model live in `config/asr_profiles/*.toml` (loaded by `load_asr_profile()`); prompts live
  in `config/prompts/*.md`. A test scans `src/` for model names.

## 2. Database

The schema is defined in [`src/agentic_meeting/store/schema.sql`](../src/agentic_meeting/store/schema.sql).

| Item                     | Location                                                |
| ------------------------ | ------------------------------------------------------- |
| Database                 | `<data_dir>/meetings.db`                                |
| Screenshots              | `<data_dir>/sessions/<session id>/frames/<number>.webp` |
| Task working directories | `<data_dir>/sessions/<session id>/tasks/<label>/`       |
| Logs                     | `<data_dir>/logs/`                                      |

### 2.1 Connection setup

Each connection runs:

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
```

and loads the sqlite-vec extension:

```python
import sqlite_vec
conn.enable_load_extension(True)
sqlite_vec.load(conn)
conn.enable_load_extension(False)
```

With aiosqlite these calls must run on the connection's own thread:
`await db.enable_load_extension(True)` followed by
`await db.load_extension(sqlite_vec.loadable_path())`.

The vector table is created with the configured dimension; its row id equals `utterances.id`:

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS utterances_vec USING vec0(embedding float[<dimensions>]);
```

If an existing vector table has a different dimension, startup fails with an explanation. The table
is never dropped automatically.

### 2.2 Vector writes and queries

```python
import struct
blob = struct.pack(f"{len(vec)}f", *vec)                      # little-endian float32
conn.execute("INSERT INTO utterances_vec(rowid, embedding) VALUES (?, ?)", (utt_id, blob))
rows = conn.execute(
    "SELECT rowid, distance FROM utterances_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
    (query_blob, k),
).fetchall()
```

Embeddings are backfilled asynchronously. A caption is stored with `embedded = 0`; a background
worker embeds pending captions in batches, writes the vectors and sets `embedded = 1`. An
unavailable embedding service does not affect storage.

### 2.3 Full-text search

`utterances_fts` uses the trigram tokenizer.

- Queries of three or more characters: `SELECT rowid FROM utterances_fts WHERE utterances_fts MATCH ?`,
  with the term wrapped in double quotes so that punctuation is not parsed as FTS syntax.
- Queries of one or two characters cannot match trigrams and fall back to `text LIKE '%term%'`,
  narrowed by session and time.

### 2.4 Recall semantics

`recall` (§7) proceeds in this order:

1. Filter by structured conditions: session, speaker (display name or number), time range.
2. With a query, run keyword search (§2.3) and vector search (§2.2) within the filtered set and merge
   the results; without one, return captions in time order.
3. Return at most `limit` items, each with `t_start`, speaker display name and text.

Details (`store/db.py`, `store/embeddings.py`):

- Vector search is restricted with `rowid IN (subquery)`, so results never cross session boundaries.
  Only backfilled captions (`embedded = 1`) are searched.
- The vector side contributes at most `VECTOR_TOP_K = 8` results above the similarity floor
  `embedding.min_similarity`. Vectors are normalized to unit length on the client, and the floor is
  converted to a Euclidean distance limit `sqrt(2 − 2s)`. Keyword results (newest first) and vector
  results (nearest first) are taken alternately up to `limit`, deduplicated and returned in time
  order, so neither side crowds out the other.
- The query embedding has a 0.3 s deadline. On timeout, or when the service is down, only keyword
  results are used. `embedding.query_prefix` is prepended to queries, not to stored captions.
- Backfill runs every 3 s in batches of 16 and continues without pause while a backlog exists.
  Errors back off up to 60 s and are logged once per outage. A dimension mismatch is a configuration
  error: it is logged and backfill stops.
- `vec0` does not support `INSERT OR REPLACE`; a vector is rewritten by delete and insert.

### 2.5 Sessions, connections and reports

The session lifecycle is described in [architecture.md §3.1](architecture.md).

- `sessions.last_active_at` — the last time a connection attached or detached, or a caption was
  written. The meeting list is ordered by it, newest first.
- `session_connections` — one row per connection: `connected_at`, `disconnected_at` (Unix seconds)
  and `t_from`, `t_to` (session timeline). It is the source for "interrupted for N minutes" and for
  the actual duration of a meeting, the sum of `t_to − t_from`.
- The session **state is not stored**. A non-empty `ended_at` means _ended_; otherwise a live
  connection in memory means _live_, and its absence _interrupted_.
- `digests` — running summaries (`pipeline/digest.py`). `text` is **cumulative**: every summary
  covers the meeting from the start, and compaction uses only the latest. `t_from` and `t_to` record
  the span newly covered, so consecutive rows are contiguous. `last_utterance_id` records how far the
  summary has read; "new captions" are determined by id rather than by time, because the
  assistant's own words are stored when its turn ends and may start before the previous summary's
  end. A summary is produced every `realtime.digest_interval_minutes` if there are new captions, at
  most 300 captions per round, and once more in the background after a disconnect or the end of the
  meeting (120 s limit).
- `reports` — `id, session_id, created_at, status ('running' | 'done' | 'failed'), provider, text_md,
error`. A session may have several; the page shows the latest and exports use the latest `done`
  one. Reports left `running` by a previous process are marked `failed` at startup.
- Reserved values of `speakers.idx`: `0` unknown, `-1` the assistant, `-2` typed text. Diarization
  numbers start at 1; speakers created manually in the UI start at 1000.
- `utterances.source`: `'asr'`, `'assistant'` or `'text'`.
- `tasks.modality`: `'voice'` or `'text'` — the modality of the turn that delegated the task, which
  decides whether its completion is spoken.

Columns added after the first schema are created by `Store._migrate` when an older database is
opened.

## 3. Streaming ASR

### 3.1 Backend interface

The `StreamingASR` protocol is defined in
[`src/agentic_meeting/asr/base.py`](../src/agentic_meeting/asr/base.py); the output type `ASRDelta`
is in [`types.py`](../src/agentic_meeting/types.py).

### 3.2 Algorithm of the llama-server backend

Implemented in `src/agentic_meeting/asr/llama_server.py`. The algorithm follows the upstream
streaming implementation of the ASR model:

- `third_party/Confucius4-R2T2/r2t2_llama/llama_native_backend.py` — `LlamaServerClient.generate`
  (request layout).
- `third_party/Confucius4-R2T2/r2t2/r2t2_asr.py` — `streaming_transcribe_no_reset` (rolling window,
  token-level rollback).
- `third_party/Confucius4-R2T2/ws_server.py` — per-step token budget, repetition detection.

Every rule below exists because a simpler variant produced worse output; see
[benchmarks.md](benchmarks.md#streaming-asr).

**State** (one per audio stream):

| Variable      | Meaning                                                                                               |
| ------------- | ----------------------------------------------------------------------------------------------------- |
| `pending`     | Audio received but not yet enough for a step (float32, 16 kHz)                                        |
| `window`      | The current audio window                                                                              |
| `steps`       | List of `(samples added in the step, text finalized in the step)`, aligned with the audio in `window` |
| `prefix_text` | Concatenation of the finalized text in `steps` — the finalized text for the current window            |
| `unstable`    | Provisional tail left by the previous step, for display only; regenerated in the next step            |
| `busy`        | Whether a request is in flight; only one is allowed at a time                                         |

**One step** (triggered when `pending` holds at least `chunk_ms` of audio and no request is in
flight):

1. **Take audio.** Move the accumulated audio from `pending` to `window`. When recognition lags,
   several chunks are merged, up to three times the step size.
2. **Slide the window.** If `window` exceeds `window_secs`, pop entries from the head of `steps`
   until the popped samples reach `window_drop_secs`, and cut **exactly that many** samples from
   `window`, so that audio and text stay aligned.
3. **Compute the token budget** for this step:
   `base = max(1, new_samples // 1280)` — one token per 80 ms of audio — doubled if the last
   finalized character is CJK (or, at the start of a segment, if the language is Chinese), and
   capped by `max(4, 2 * base)` and `asr.max_new_tokens`. A fixed large budget must not be used: the
   model is trained to emit only stable content and runs ahead when given room.
4. **Request** (§3.3). The assistant prefix is the profile's `assistant_prefix` with the language
   filled in, followed by `prefix_text`. `max_tokens` is the budget from step 3.
5. **Clean the continuation** `cont`: remove U+FFFD, cut at any of the profile's `cut_markers`, and
   for Chinese remove spaces between adjacent CJK characters while keeping spaces between English
   words.
6. **Hotword-echo guard** (only at the start of a segment, when `prefix_text` is empty). If `cont`
   begins with two or more consecutive hotwords, in order and starting from the first, the model is
   reciting the hotword list from the system message. In a regular step the whole step is void:
   nothing is finalized or displayed and `(samples, "")` is appended to `steps`. In a finalizing step
   only the echoed part is removed.
7. **Roll back by tokens to find the finalization point.** Let `full = prefix_text + cont`. Call
   `POST {asr.base_url}/tokenize` with `{"content": full, "add_special": false, "with_pieces": true}`,
   drop the last `unfixed_tokens` tokens and join the rest.
   - A token's `piece` is a string, or an array of byte values when a character is split across
     tokens. Pieces are converted to bytes before joining; if decoding fails, the cut is inside a
     character and one more token is dropped.
   - If the cut falls inside an English word or a number, more tokens are dropped. **An English word
     is never split across two deltas**; wake-word matching relies on this.
   - The finalization point never precedes `len(prefix_text)`. If the rejoined text differs from
     `full`, nothing is finalized in this step.

   Rollback must be by token, not by character. A character-level cut can split a token, and the
   model then continues from half a word, dropping characters and inserting punctuation.

8. **Emit.** `stable_new = full[len(prefix_text):cut]`, `unstable = full[cut:]`; append
   `(samples, stable_new)` to `steps` and emit
   `ASRDelta(stable_text=stable_new, unstable_text=unstable, audio_end_secs=<latest value from the caller>)`.

**`flush()`** waits for the request in flight, moves all remaining audio into the window and runs
one more step with the budget raised to `asr.max_new_tokens` and no rollback. It emits a delta with
`segment_end=True` and clears `window`, `steps` and `unstable`. A `segment_end` delta is emitted even
when there was no audio.

**`start()`** sends a warm-up request (one second of silence, empty prefix) and waits for it. It
usually takes 0.2 s but can take more than ten seconds on the first uses of a machine.

**Failures and safeguards:**

- A failed request — connection error, timeout, non-2xx or malformed response, for either the chat
  or the tokenize call — rolls the step back completely: the audio returns to the head of `pending`
  and `window` and `steps` are restored. Retries back off at 0.1, 0.2, 0.4 … up to 2 s and include
  audio that arrived meanwhile. After five consecutive failures `deltas()` raises `ASRBackendError`
  once the queued deltas are drained, and the backend stops processing; `push_audio` and `flush`
  are then ignored silently. Failure is reported through `deltas()` only. Calling `start()` again
  resets the instance.
- Repetition guard: if `cont` repeats a short fragment (1–6 characters) six or more times in a row,
  it is discarded as a hallucination and `window`, `steps` and `unstable` are cleared.
- Falling behind real time: a warning is logged when `pending` exceeds 5 s; beyond `window_secs`
  the oldest audio is dropped.
- `ASRDelta.audio_end_secs` is the caller's latest value **when the request was issued**. With a
  backlog it is ahead of what the step actually consumed.
- A step without new text does not call the tokenize endpoint.

Tunable settings: `asr.chunk_ms` (smaller steps give more responsive captions and more GPU load),
`asr.unfixed_tokens` (more provisional tokens give steadier but slower captions) and
`asr.window_secs` / `asr.window_drop_secs`.

### 3.3 Request format

`POST {asr.base_url}/v1/chat/completions` — `llama-server` must be started with the audio projector:

```json
{
  "messages": [
    {"role": "system", "content": "<hotword string, may be empty>"},
    {"role": "user", "content": [
      {"type": "input_audio", "input_audio": {"data": "<base64 WAV>", "format": "wav"}}
    ]},
    {"role": "assistant", "content": "<assistant prefix>"}
  ],
  "temperature": 0.0,
  "max_tokens": 8,
  "stream": false
}
```

- Audio: `window` written as 16 kHz mono 16-bit WAV.
- `max_tokens`: the step budget (typically 4–8); `asr.max_new_tokens` only when finalizing.
- Hotword string: `session.hotwords` plus the assistant's name, joined according to the profile.
  Wake-word aliases are not included.
- `choices[0].message.content` **includes the prefix**. It is stripped; the remainder is the
  continuation.
- **The chat template is set when the ASR server starts, not per request.** The pinned
  `llama-server` ignores a `chat_template` field in the request body. The process supervisor writes
  the profile's `chat_template` to a file and passes it with `--chat-template-file` (§9).
- When the last message is from the assistant, `llama-server` continues it (`--prefill-assistant` is
  on by default). Prefix continuation depends on this.
- An empty `system` content is accepted.

Tokenize endpoint: `POST {asr.base_url}/tokenize` with
`{"content": "<text>", "add_special": false, "with_pieces": true}` returns
`{"tokens": [{"id": 1234, "piece": "现在"}, ...]}`, where `piece` is a string or an array of byte
values. It is a local call that takes milliseconds.

### 3.4 The ASR service in the pipeline

`StreamingASRService` extends Pipecat's `STTService`.

| Input          | Action                                                                                                                                                                                                                                                             |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Audio frame    | Count samples. While nobody is speaking, keep the frame in the pre-roll buffer (the last `preroll_ms`, cleared when speech stops, so it only holds audio since the previous segment). While speaking, call the backend's `push_audio`. Pass the frame on unchanged |
| Speech started | Send the pre-roll buffer to the backend as one block, then clear it                                                                                                                                                                                                |
| Speech stopped | Call the backend's `flush()` and wait for it                                                                                                                                                                                                                       |
| Backend delta  | Convert to transcription frames as described below                                                                                                                                                                                                                 |

The pre-roll has to be long enough to cover a short wake word followed by a pause, because VAD often
triggers only on the request that follows.

Transcription frames (reasons in parentheses refer to Pipecat 1.12.0 behavior):

1. Every delta produces an **interim transcription frame** with the segment's finalized text plus
   the provisional tail (used for captions and turn detection).
2. A non-blank `stable_text` produces a **transcription frame** carrying `stable_text` unchanged
   (the wake-word strategy only looks at final frames; emitting one per step keeps wake-up fast).
3. The transcription frame that closes a segment has `finalized=True` (the turn-stop strategy ends
   the turn immediately on a finalized transcription instead of waiting for a timeout).
4. **A transcription frame is never empty or blank** (the turn-stop strategy remembers the last
   transcription text and does not end the turn when it is empty). Blank `stable_text` is prepended
   to the next non-blank delta. If finalization brings no new text, no transcription frame is pushed.
5. After finalization no interim frames are pushed until speech starts again (an interim frame
   resets the "transcription finalized" state).
6. Each transcription frame sets `includes_inter_frame_spaces = True` (otherwise the user aggregator
   inserts a space between frames, which breaks Chinese text).
7. Both frame types carry the original `ASRDelta` in `result`, use `time_now_iso8601()` as
   `timestamp` and leave `user_id` empty; the speaker is determined by the meeting recorder.
8. The service is constructed with `ttfs_p99_latency=0.5` as the fallback wait when no finalized
   transcription arrives.

**Timeline.** Samples are counted before any early-return path of the Pipecat base class (muted by
`STTMuteFrame`, reconnecting, marked unusable), so the count never stops and matches the meeting
recorder's (architecture.md §3).

**Startup and failure of the backend:**

- The backend starts in a background task and does not block `StartFrame`. Audio arriving before it
  is ready is counted and passed on but not sent to the backend.
- When `deltas()` raises `ASRBackendError`, or startup fails, the service stops sending audio,
  pushes a `notice` to the browser, discards the partial segment and calls `start()` on the same
  backend instance with backoff of 1, 2, 4 … up to 30 s. On success it pushes a second notice.
  Sample counting and audio pass-through are unaffected. Each outage produces one failure notice and
  one recovery notice.
- An error while handling a single delta is logged and that delta is skipped.
- Audio received during an outage is not recognized later.

## 4. Speaker diarization

### 4.1 Backend interface

The `Diarizer` protocol is defined in
[`src/agentic_meeting/diar/base.py`](../src/agentic_meeting/diar/base.py).

### 4.2 Library binding

[`diar/nemo_ctypes.py`](../src/agentic_meeting/diar/nemo_ctypes.py) contains the synchronous
`CtypesBinding` and the asynchronous `NemoDiarizer`, which implements `Diarizer` and runs every
library call on one single-threaded executor. `diar.build_diarizer(cfg, on_notice=…)` returns a
`NullDiarizer` when `backend = "none"`, and also when the library or model fails to load, after
logging the error and notifying the user through `on_notice`.

`segments(since_secs)` returns segments that **end after** `since_secs`, ordered by start time.
`finish()` lets the model label the last frames when a stream ends; `labeled_secs()` reports how far
labeling has progressed.

- C header: `third_party/NeMo-Speech.cpp/include/nemo_speech/diar.h`; usage example:
  `third_party/NeMo-Speech.cpp/examples/diarize_file.cpp`.
- Library: `nemo_speech_asr_c` (`bin/nemo_speech_asr_c.dll` on Windows; expected under `lib/` on
  Linux and macOS).
- On Windows, call `os.add_dll_directory(<bin directory>)` before loading, or dependent libraries
  are not found.

Structures (field order and types must match the header):

```python
import ctypes as C

class DiarModelConfig(C.Structure):
    _fields_ = [
        ("size", C.c_size_t),               # must equal sizeof(this struct)
        ("model_path", C.c_char_p),         # UTF-8 path of the weights
        ("gpu", C.c_int32),                 # GPU index; -1 = CPU
        ("preset", C.c_char_p),             # None = the model's low-latency default
        ("chunk_frames", C.c_int32),        # the next five: <= 0 keeps the preset
        ("right_context_frames", C.c_int32),
        ("left_context_frames", C.c_int32), # -1 keeps the preset; 0 is a valid value
        ("fifo_frames", C.c_int32),
        ("spkcache_frames", C.c_int32),
        ("update_period_frames", C.c_int32),
    ]

class DiarSegmentationConfig(C.Structure):
    _fields_ = [
        ("size", C.c_size_t),
        ("onset", C.c_float), ("offset", C.c_float),
        ("pad_onset_sec", C.c_double), ("pad_offset_sec", C.c_double),
        ("min_gap_sec", C.c_double), ("min_duration_sec", C.c_double),
    ]                                       # fields <= 0 use the library defaults

class DiarSegment(C.Structure):
    _fields_ = [("start_time", C.c_double), ("end_time", C.c_double), ("speaker", C.c_int32)]
```

Functions returning `int` return a status code, `0` for success. On failure
`nemo_speech_asr_last_error()` returns a `const char*` that is **thread-local** and must be read on
the failing thread immediately.

| Function                             | Arguments                                                                                   | Returns  |
| ------------------------------------ | ------------------------------------------------------------------------------------------- | -------- |
| `nemo_speech_diar_create`            | `(DiarModelConfig*, void**)`                                                                | status   |
| `nemo_speech_diar_destroy`           | `(void* model)`                                                                             | —        |
| `nemo_speech_diar_num_speakers`      | `(void* model)`                                                                             | `int32`  |
| `nemo_speech_diar_seconds_per_frame` | `(void* model)`                                                                             | `double` |
| `nemo_speech_diar_stream_open`       | `(void* model, void** stream)`                                                              | status   |
| `nemo_speech_diar_stream_push_f32`   | `(void* stream, float*, size_t n, int32 sample_rate)`                                       | status   |
| `nemo_speech_diar_stream_finish`     | `(void* stream)`                                                                            | status   |
| `nemo_speech_diar_stream_close`      | `(void* stream)`                                                                            | —        |
| `nemo_speech_diar_segments`          | `(void* stream, DiarSegmentationConfig*, DiarSegment* out, size_t capacity, size_t* count)` | status   |

- Set `argtypes` and `restype` for every function; otherwise 64-bit pointers are truncated.
- `nemo_speech_diar_segments` is called twice: first with `out=None, capacity=0` to obtain the count,
  then with an array of that size. Speaker numbers start at 1.
- A stream must not be called concurrently, and `push` is a blocking GPU computation — hence the
  single-threaded executor.
- In long meetings the library compresses frame-level probabilities older than about 20 minutes into
  final segments, which `segments` still returns; memory does not grow without bound.
- `gpu` uses llama.cpp's CUDA numbering (`gpu = 0` is CUDA0 of `llama-server --list-devices`), which
  may differ from the order shown by `nvidia-smi`.

### 4.3 Text attribution and caption splitting (`diar/fusion.py`)

Input: `ASRDelta`s in arrival order, and speaker segments that can be queried at any time. Output:
caption updates and finalized `Utterance`s.

1. **Time span of a delta**: `[previous delta's audio_end_secs, this delta's audio_end_secs]`,
   shifted back by a constant ASR delay estimate of 0.2 s. The first delta of a segment starts at the
   speech-started time.
2. **Attribution**: the speaker with the longest cumulative speech inside the span. With no segment
   in the span, the delta is `SPEAKER_UNKNOWN` and inherits the previous delta's speaker within the
   segment.
3. **Splitting**: a caption ends when its ASR segment ends, or when two adjacent non-blank deltas
   belong to different speakers and the new speaker has continued for at least 0.8 s, which prevents
   fragments caused by jitter.
4. **Storage**: a caption is stored as soon as it ends, with the speaker known at that moment.
   Diarization labels trail the audio by 0.6–1.1 s, so the last second of a caption may not be
   labeled yet.
5. **Late correction**: for 5 s after storage the attribution is recomputed once per second. A
   change updates the database and sends `utterance_update`.
6. **The assistant's own voice**: deltas that overlap an assistant speaking period (from "bot started
   speaking" until 0.3 s after "bot stopped speaking") by more than half are dropped. The assistant's
   words are stored from the assistant aggregator's events with `source="assistant"`.
7. **Merging adjacent segments.** VAD splits on pauses, and the pause threshold
   (`turn.vad_stop_secs`) is short to keep answers fast, so a sentence is often cut where the
   speaker takes a breath. On storage, a newly finalized segment is merged into the previous caption
   (`should_merge`) when:
   - the previous caption also came from ASR, with nothing from the assistant or typed input in
     between; the speaker is the same and the gap does not exceed `transcript.merge_gap_secs`; and if
     either speaker is still unknown, the gap does not exceed 0.8 s;
   - the previous caption has not reached `merge_soft_chars` at a sentence end (`。！？!?…`) and has
     not reached `merge_max_chars`;
   - the previous caption has not been covered by a running summary yet.

   Merging appends the text to the stored row (no space between CJK characters, one space between
   Latin words or digits), extends the end time and clears the vector for re-embedding. The page
   receives an `utterance` message with the **original** caption's `id`, the new segment's
   `segment_id` and the merged text. The LLM context is not rewritten: each segment was appended as
   its own line, and a rebuild from the database later yields the merged line.

Details of `TranscriptAssembler`:

- Unknown never triggers a split. A caption that starts before diarization has a result is stored as
  unknown and adopts the first known speaker.
- Splitting uses a pending area: after a speaker change, the new speaker's deltas are held there
  while the caption still shows on the original row. Once the new speaker has continued for 0.8 s the
  caption is split at the delta where they started; if the original speaker returns first, or a
  third speaker appears, the pending text is merged back. Blank deltas follow the most recent
  destination.
- Ties go to the lower speaker number. A zero-length span uses the segment covering that instant.
- `segment_id` increases whenever a caption row closes. A row that closes without finalized text
  after showing provisional text sends an empty `CaptionUpdate` so that the page can clear it.
- A speech-started event that arrives before the previous segment has closed (system frames jump
  the queue; pipecat-notes.md §10) is remembered and applied afterwards.
- Dropped echo deltas do not extend a caption's end time, but a dropped delta that ends a segment
  still closes the caption.

## 5. HTTP API

All endpoints except signaling exchange JSON. Errors are returned as `{"error": "<message>"}` with
an appropriate status code; messages are in Chinese. During development the client runs on port
5173 and proxies `/api` to the application (`client/vite.config.ts`). When an access password is
configured, every endpoint except those of §5.7 requires a login (§5.7).

### 5.1 Sessions and clock synchronization

| Method and path                                      | Request            | Response                                                                                                                |
| ---------------------------------------------------- | ------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| `GET /api/time`                                      | —                  | `{"server_time": <Unix seconds, float>}`                                                                                |
| `GET /api/sessions?limit=20&before=<last_active_at>` | —                  | `{"items": [session summary]}`, newest `last_active_at` first; `before` pages backwards. `limit` is 1–500               |
| `GET /api/sessions/{id}`                             | —                  | Session summary plus `screen` (the `[screen]` configuration), `members` and `connections`. 404 if unknown               |
| `GET /api/session`                                   | —                  | The _current session_: the one with the live connection, otherwise the most recent unfinished one. 404 if there is none |
| `PATCH /api/sessions/{id}`                           | `{"title": "..."}` | Updated summary. The title is trimmed and limited to 200 characters                                                     |
| `POST /api/sessions/{id}/end`                        | —                  | `{"id", "ended_at"}`. A live session is disconnected first; the final running summary is produced in the background     |
| `POST /api/session/end`                              | —                  | Same, for the current session                                                                                           |
| `DELETE /api/sessions/{id}`                          | —                  | `{"id"}`. Screenshot files and task directories are removed. 409 for a live session                                     |

**Session summary:** `{"id", "title", "started_at", "ended_at", "last_active_at",
"state": "live" | "interrupted" | "ended", "duration_secs", "utterance_count", "speakers": [...],
"preview": [{"speaker", "text"}]}`. `duration_secs` is the sum of the connection spans, excluding
gaps; `preview` holds the last two captions. `connections` is
`[{connected_at, disconnected_at, t_from, t_to}]`.

Ending a live session sends `session_closed(reason="ended")` to the page, cancels the pipeline, waits
for it to finish and then writes `ended_at`.

Clock synchronization: the browser records its local time before and after the request, `t0` and
`t1`; the offset "server time − local time" is approximately `server_time − (t0 + t1) / 2`. Three
measurements are taken on connection and the one with the shortest round trip is used.

### 5.2 WebRTC signaling

| Method and path    | Description                                                                                                             |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| `POST /api/offer`  | Request and response are defined by Pipecat's `SmallWebRTCRequestHandler` and passed through                            |
| `PATCH /api/offer` | Adds ICE candidates: `{"pc_id", "candidates": [{"candidate", "sdp_mid", "sdp_mline_index"}]}` → `{"status": "success"}` |
| `GET /api/ice`     | `{"ice_servers": [{"urls": [...], "username"?, "credential"?}]}`: the ICE servers the browser uses for its connection   |

The client may include `{"session_id": "<session to resume>"}` in `requestData`:

- absent or `null` — a new session is created;
- present and known — that session is resumed (an ended one is reopened); see architecture.md §3.1;
- present and unknown — 404 before negotiation; not a string, or empty — 400;
- an existing live connection is taken over by the new one.

`POST /api/offer` reads the raw JSON and passes it to `SmallWebRTCRequest.from_dict`, because the
client SDK sends connection parameters in camel-case `requestData`. After negotiation
`run_bot(connection, request_data, resources)` runs as a background task until the connection
closes. On the server, `SessionManager.attach` takes over the old connection, reopens an ended
session, computes `base_secs = max(now − started_at, furthest point reached on the timeline)` and
records the connection. The LLM context is rebuilt from the database (`build_context_messages`); if
that fails, the context starts empty.

**ICE servers.** `server.ice_servers` is used on both ends of the connection: the server passes it
to `SmallWebRTCRequestHandler`, and the browser fetches `GET /api/ice` before every connection it
starts and gives the result to the WebRTC SDK (`SmallWebRTCTransport.iceServers`). Each entry has
the shape of the browser's `RTCIceServer`; `username` and `credential` are present only for entries
that configure them. The credential is read from the variable named by `credential_env` when the
application starts. Anyone allowed to call `/api` can read it, so with a TURN server the access
password (§5.7) should be set and the TURN server should relay only to this machine. When the
request fails, the browser connects without ICE servers, which still works on a single LAN. An
empty list (the default) means host candidates only.

`GET /` returns a 503 page with build instructions when `client/dist` does not exist.

### 5.3 Screenshots

`POST /api/frames`, `multipart/form-data`:

| Field         | Type            | Description                                                             |
| ------------- | --------------- | ----------------------------------------------------------------------- |
| `captured_at` | float as string | Capture time in Unix seconds, **already converted to server time**      |
| `image`       | file            | `image/webp` or `image/jpeg`, longest side at most `screen.max_side_px` |

Response: `{"id": <screenshot id>, "t": <session timeline seconds>}`.

| Status | Condition                                                                                                       |
| ------ | --------------------------------------------------------------------------------------------------------------- |
| 400    | Not an image, not WebP/JPEG, too large in pixels, `captured_at` not a number or more than 60 s from server time |
| 403    | `screen.enabled = false`                                                                                        |
| 404    | No live meeting                                                                                                 |
| 413    | Larger than 4 MB                                                                                                |
| 422    | Missing field                                                                                                   |
| 500    | The file could not be written (the database row is rolled back)                                                 |

`GET /api/frames/{id}/image` returns the image with `Cache-Control: private, no-cache`.
`GET /api/frames?session_id=` returns
`{"items": [{"id", "t", "width", "height", "caption", "caption_status"}]}` in time order; without
`session_id` the current session is used.

Behavior (`screen/ingest.py`, `screen/caption.py`):

- The file type is taken from the decoded image, not from the browser's claim. Files are named
  `<6-digit id>.webp` or `.jpg`. `t = max(0, captured_at − sessions.started_at)`.
- **The server decides again whether the picture changed.** The image is reduced to a 64×36
  grayscale thumbnail and compared with the last _changed_ picture of the meeting: the thumbnail is
  divided into 8×6-pixel blocks and the mean absolute difference of the most-changed block, divided
  by 255, is compared with `screen.change_threshold`. The browser uses the same measure to decide
  whether to upload. A whole-image average is not used because two slides that differ only in text
  differ by about 1 % on average. An unchanged picture still enters the timeline and is announced
  with `frame`, but it inherits the previous summary and adds no line to the LLM context. Because
  the comparison is with the last changed picture, slow cumulative drift eventually crosses the
  threshold. The reference picture lives in memory; after a restart the first picture counts as
  changed.
- **Summaries.** Only the newest pending screenshot is summarized; older pending ones are marked
  `skipped`. Summaries pause while the assistant is answering, and a cancelled one is retried
  afterwards unless a newer picture has arrived. A model error or an empty summary marks the
  screenshot `failed`. On success the summary is stored, `frame_caption` is pushed and
  `[画面 HH:MM:SS] <summary>` is appended to the LLM context without triggering the model. A summary
  that says the picture is irrelevant is stored and shown but not added to the context. If the
  meeting has changed meanwhile, the summary is stored only.
- `screen.caption_provider = "agent_llm"` uses the agent model, which requires
  `agent.supports_vision` and a configured endpoint.

### 5.4 Captions and speakers

`session_id` may be omitted in all endpoints below and then refers to the current session (§5.1).

| Method and path                                     | Description                                                                                                                                                                                                                                                     |
| --------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/utterances?session_id=&after_id=&limit=`  | Captions with id greater than `after_id`, ascending. Used to catch up after a reconnect                                                                                                                                                                         |
| `GET /api/utterances?session_id=&tail=50`           | The **last 50** captions, ascending                                                                                                                                                                                                                             |
| `GET /api/utterances?session_id=&before_id=&limit=` | Pages towards earlier captions, ascending                                                                                                                                                                                                                       |
| `GET /api/speakers?session_id=`                     | `{"items": [{"idx", "display_name"}]}`                                                                                                                                                                                                                          |
| `PUT /api/speakers/{idx}`                           | Body `{"session_id"?, "display_name": "..."}` → `{"idx", "display_name"}`; broadcasts `speaker`                                                                                                                                                                 |
| `POST /api/utterances/speaker`                      | Body `{"session_id"?, "ids": [...], "speaker_idx": 2}` or `{…, "new_speaker": "..."}`. Reassigns the captions to an existing or a new speaker → `{"speaker": {idx, display_name}, "ids": [actually changed]}`; broadcasts `utterance_update` for a live session |
| `POST /api/speakers/{idx}/merge`                    | Body `{"session_id"?, "into": 2}`. Moves all captions of `idx` to `into` and deletes `idx` → `{"from", "into", "display_name", "moved"}`; broadcasts `speakers_merged` for a live session                                                                       |

Caption item: `{"id", "speaker_idx", "speaker_name", "t_start", "t_end", "text", "source"}`.
`limit` and `tail` are 1–500; `after_id`, `before_id` and `tail` are mutually exclusive (400).
Speaker names are trimmed and limited to 50 characters. An unknown speaker yields 404.

**Reassigning captions.** `ids` is a non-empty list of at most 500 integers. Exactly one of
`speaker_idx` and `new_speaker` is required. `speaker_idx` cannot be negative, `0` means unknown,
and any other value must be an existing speaker of the session. Only captions with `source = "asr"`
are changed; others are ignored without error. New speakers are numbered from 1000
(`MANUAL_SPEAKER_BASE`), separate from diarization numbers. A caption stored less than 5 s ago may
still be changed back by late correction (§4.3).

**Merging speakers.** Both `idx` and `into` must be positive and different (400 otherwise); an
unknown speaker yields 404. Tasks requested by `idx` are reassigned as well. The page updates its
loaded captions from the single `speakers_merged` message.

A speaker record is created on first appearance with the default name "说话人 N". `-1` carries the
assistant's name and `-2` the name "文字输入".

**Wall-clock times** are computed in the browser (`client/src/timeline.ts`): for a timeline value
`t` inside a connection span, `connected_at + (t − t_from)`; otherwise `started_at + t`.

### 5.5 Tasks

| Method and path                        | Description                                                                       |
| -------------------------------------- | --------------------------------------------------------------------------------- |
| `GET /api/tasks?session_id=`           | `{"items": [...]}` without detailed results                                       |
| `GET /api/tasks/{id}`                  | One task with all fields and its progress events                                  |
| `POST /api/tasks/{id}/cancel`          | Cancels the task and returns the list item. A finished task is returned unchanged |
| `GET /api/tasks/{id}/artifacts/{name}` | Downloads an artifact file                                                        |

- `{id}` is the full task id, `<session id>.t3`.
- List item: `{"id", "label", "goal", "status", "brief", "error", "modality", "created_at",
"started_at", "finished_at"}`.
- The detail view adds `detail_md`, `sources`, `artifacts` (file names), `requested_by` (display
  name), `requested_t`, `events` (`[{at, kind, summary}]`) and `outbound` — **what the task sent out**:
  `{"goal", "t_from", "t_to", "frames": [{id, t}], "model_host"}`.
- Artifacts are served only if the task reported them and they were retrieved into the task
  directory. PNG, JPEG, WebP and GIF are returned as images; everything else as
  `application/octet-stream` with `Content-Disposition: attachment` and
  `X-Content-Type-Options: nosniff`, because artifacts are produced by model-written code.

### 5.6 Export and reports

| Method and path                     | Description                                                                                      |
| ----------------------------------- | ------------------------------------------------------------------------------------------------ |
| `GET /api/export/{session_id}.md`   | Markdown transcript                                                                              |
| `GET /api/export/{session_id}.json` | Structured JSON                                                                                  |
| `GET /api/export/{session_id}.zip`  | Archive with `transcript.md`, `session.json`, `report.md` (if any), `frames/` and `tasks/`       |
| `POST /api/sessions/{id}/report`    | Starts report generation → 202 `{"report_id", "status": "running"}`                              |
| `GET /api/sessions/{id}/report`     | The latest report: `{"id", "status", "created_at", "provider", "text_md", "error"}`; 404 if none |
| `GET /api/sessions/{id}/report.md`  | The latest finished report as a download                                                         |

**Markdown export** (`web/export.py`):

- `text/markdown; charset=utf-8` with `Content-Disposition: attachment`. The file name is the meeting
  title, with an ASCII fallback `meeting-<first 8 characters of the id>.md`.
- Structure: title → start and end time, state, duration, speakers → summary (the latest cumulative
  running summary) → transcript → background tasks, if any.
- Captions and screen summaries are interleaved by time. Adjacent captions from the same speaker and
  source within 120 s are joined into a paragraph that starts with the speaker and time; typed
  input is marked.
- Screen entries are block quotes with the summary and an image link `frames/<file>`, a relative
  path that matches the archive layout. Consecutive identical summaries appear once, irrelevant
  pictures are omitted and pictures without a summary are shown without text.
- Markdown characters in the transcript are escaped. A live meeting can be exported up to the
  present.

**JSON and ZIP:**

- Top-level JSON fields: `format_version` (1), `session`, `connections`, `speakers`, `utterances`
  (items as in §5.4), `frames` (`[{id, t, file, width, height, caption, caption_status}]`),
  `digests` (`[{t_from, t_to, text, created_at}]`), `tasks` (all fields plus `events`; `artifacts`
  are relative paths `tasks/<label>/<file>`) and `report` (the latest finished one, or `null`).
  Keys named `t*` are session-timeline seconds; keys ending in `_at` are Unix seconds.
- ZIP members, in order: `transcript.md` (identical to the Markdown export), `session.json`,
  `report.md`, screenshots under `frames/`, artifacts under `tasks/<label>/`. Text members are
  compressed and files are stored. The archive is streamed and never held in memory.
- **Exports cannot be used to read other files.** Artifact names that are absolute, contain `..` or
  a drive letter are rejected. Every member must resolve to an existing file inside
  `<data_dir>/sessions/<session id>/`, or it is skipped.

**Reports** (`pipeline/report.py`, `web/reports_api.py`):

- `POST` returns 409 for a live meeting or when a report is already being generated, 503 when no
  model is available and 404 for an unknown session. A new report can be requested after a failure
  or completion.
- Generation: bring the running summary up to date; load captions, screenshots, tasks and
  summaries; if the transcript fits in `report.max_input_chars`, send it whole, otherwise split it
  into sections — at the size limit, or at a summary boundary once a section is half full — extract
  key points per section with `report_section.md`, and merge them with `report.md`.
- The heading with title, start time, duration, speakers and generation time is written by code. The
  body is model output restricted to `##` headings and `-` lists, because the page shows it as
  pre-formatted text.
- Requests go through the background-model gate and are re-sent if the assistant interrupts them.
  A report has a 15-minute limit.
- With `report.provider = "agent_llm"` the agent model is used, and `check_warnings` reports that
  the transcript will be sent to that endpoint.
- The page polls `GET /api/sessions/{id}/report` every 2 s while a report is running.

### 5.7 Access password

Opt-in. With `server.password_env` empty (the default) nothing below applies: `/api` is open and
`GET /api/auth` reports `enabled: false`. With it set, the password is read from that environment
variable at startup, and the middleware in `web/auth.py` guards every path under `/api/` except
`GET /api/auth` and `POST /api/auth/login`. The static site (`/`, `/assets/…`) stays public; it
contains no meeting data.

| Method and path         | Request               | Response                                                                                                                                                                                     |
| ----------------------- | --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/auth`         | —                     | `{"enabled", "authenticated", "csrf_token"}`; `csrf_token` is `null` when not logged in or when the password is disabled                                                                     |
| `POST /api/auth/login`  | `{"password": "..."}` | `{"enabled": true, "authenticated": true, "csrf_token"}` and the session cookie. 401 wrong password; 429 too many attempts, with `Retry-After` in seconds; 404 when the password is disabled |
| `POST /api/auth/logout` | —                     | `{"enabled": true, "authenticated": false, "csrf_token": null}`; the cookie is cleared                                                                                                       |

**Guarded requests.** Without a valid session cookie the response is 401 `{"error": "请先登录"}`.
`POST`, `PUT`, `PATCH` and `DELETE` additionally need the header `X-CSRF-Token` with the token of
the current session, otherwise 403. This includes `POST`/`PATCH /api/offer`: the client passes the
header to the WebRTC SDK, which sends it with the offer and the ICE candidates. `GET` and `HEAD`
need only the cookie, so `<img>` sources and download links keep working.

**Session cookie.** Name `am_session`, attributes `HttpOnly; SameSite=Strict; Path=/`, `Max-Age`
of `server.auth_session_days`, and `Secure` when the request arrived over HTTPS. The value is
`<expiry, Unix seconds>.<random nonce>.<signature>`, where the signature is HMAC-SHA256 over the
first two fields. The session expires at the stated time; there is no sliding renewal.

**Keys.** On first start a random 32-byte secret is written to `<data_dir>/auth_secret` (readable
by the owner only). The password is stretched once at startup with scrypt (`n=2^14, r=8, p=1`),
salted with the secret; the signing key is HMAC-SHA256 of that result. Changing the password
therefore logs out every device, and so does deleting the file; a leaked cookie together with the
secret still makes each password guess cost a full scrypt evaluation. The CSRF token is HMAC-SHA256 of
the session nonce under the same key; it is not stored anywhere.

**Login attempts.** Each attempt is stretched with the same scrypt parameters (in a worker thread,
at most 4 at a time) and compared in constant time. At most 5 failed attempts per client
address are allowed in any 5-minute window; further attempts receive 429 until the oldest failure
leaves the window. A successful login clears the count for that address. Behind a reverse proxy the
client address comes from `X-Forwarded-For` only when uvicorn trusts the proxy
(`--forwarded-allow-ips`, by default `127.0.0.1`).

**Startup checks.** `check_ready` reports a password variable that is named but unset or shorter
than 8 characters. `server_warnings(cfg)` returns a warning when `server.host` is not a loopback
address and no password is configured, and another when a TURN server is configured without a
password (its credential is then readable through `GET /api/ice`); `check` and `serve` print them.
They are not sent to the browser.

A WebRTC connection that is already established is not interrupted when its cookie expires or the
user logs out elsewhere; the next HTTP request is refused.

## 6. Data-channel messages

### 6.1 Server → browser

The server pushes `RTVIServerMessageFrame(data=<object>)`; the browser receives it in
`onServerMessage`. Every message has a `type`:

| `type`             | Other fields                                                              | Meaning                                                                                                                                                                                                                   |
| ------------------ | ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `caption`          | `segment_id, speaker_idx, speaker_name, t_start, stable, unstable`        | Live caption of the segment being spoken. A later message with the same `segment_id` replaces the earlier one                                                                                                             |
| `utterance`        | `id, segment_id, speaker_idx, speaker_name, t_start, t_end, text, source` | A caption has been finalized and stored; it replaces the live caption with that `segment_id`. If `id` is already on the page, a new segment was merged into that caption (§4.3): `text` is the merged text                |
| `utterance_update` | `id, speaker_idx, speaker_name`                                           | Speaker correction                                                                                                                                                                                                        |
| `speaker`          | `idx, display_name`                                                       | Speaker renamed                                                                                                                                                                                                           |
| `speakers_merged`  | `from, into, display_name`                                                | Speaker `from` was merged into `into`                                                                                                                                                                                     |
| `frame`            | `id, t, width, height`                                                    | New screenshot                                                                                                                                                                                                            |
| `frame_caption`    | `id, caption`                                                             | Screenshot summary available                                                                                                                                                                                              |
| `assistant_state`  | `state`: `idle` / `listening` / `thinking` / `speaking`                   | Assistant state                                                                                                                                                                                                           |
| `task`             | `id, label, goal, status, brief, error, modality, created_at`             | Task created or changed; `id` is the full id and `label` the short one (`t3`)                                                                                                                                             |
| `task_event`       | `task_id, at, kind, summary`                                              | Task progress                                                                                                                                                                                                             |
| `notice`           | `level`: `info` / `warn` / `error`, `text`                                | Something the user should know, such as a degraded service. Errors of the realtime LLM and TTS are translated into notices by `pipeline/errors.py`; of Pipecat's own RTVI `error` messages the page shows only fatal ones |
| `session`          | `id, title, started_at, resumed, base_secs, state`                        | Sent once after connecting: which session the connection belongs to, whether it is a resume, and the timeline origin of this connection                                                                                   |
| `session_closed`   | `reason`: `taken_over` / `ended` / `server_stopping`                      | Why the server is about to close the connection (best effort)                                                                                                                                                             |

`segment_id` is an increasing integer generated by the server; one speech span may produce several
when the speaker changes.

All three kinds of writes to the LLM context go through the meeting recorder under one lock:
storing a caption and appending its line, appending a screen line (`append_context_line`), and the
full rebuild during compaction (`rebuild_context`).

How the recorder (`pipeline/recorder.py`) produces these messages:

- Each `ASRDelta` is processed once — the interim and final frames share the object — by
  `TranscriptAssembler` (§4.3), which returns caption updates and finalized captions. In `caption`,
  `stable` is the finalized text of **that row** so far and `unstable` its tail; the speaker is `0`
  until known. Unchanged captions are not re-sent.
- For each finalized caption: store → push `utterance` → append `[HH:MM:SS speaker] text` to the LLM
  context (`LLMMessagesAppendFrame`, `run_llm=False`) → only then release the transcription frame
  that triggers the user turn. If storage fails, `utterance.id` is `null` and the caption is queued
  for retry. Assistant and typed captions have no live row; their `segment_id` is `null`.
- Speaker attribution is rechecked every second for 5 s after storage.
- Deltas overlapping the assistant's speech are not shown, stored or added to the context.

The server sends only `listening` as `assistant_state` (when the wake word is heard); the browser
derives `speaking` and `idle` from the SDK callbacks `onBotStartedSpeaking` and
`onBotStoppedSpeaking`. The assistant's text arrives through the SDK callbacks `onBotLlmStarted`,
`onBotLlmText` and `onBotLlmStopped`, not through custom messages.

### 6.2 Browser → server

Sent with `client.sendClientMessage(type, data)` and handled in the `on_client_message` event of
`RTVIProcessor`.

| `type`         | `data`                    | Meaning                                                                                                                                                                                                |
| -------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `text_input`   | `{"text": "..."}`         | A typed question or task. It is equivalent to a request from an awake user: no wake word is needed and the answer is text only (§6.3). The trimmed text must be non-empty and at most 2,000 characters |
| `screen_state` | `{"sharing": true/false}` | Screen sharing started or stopped; used for logging only                                                                                                                                               |

### 6.3 Typed input and output modality

Handling of `text_input` (`pipeline/text_input.py`; design in architecture.md §5.5):

1. Validate the text. Store a caption (`source="text"`, `speaker_idx=-2`,
   `addressed_to_assistant=1`), push `utterance` and append `[HH:MM:SS 文字输入] text` to the context.
2. To trigger the model, push `TextRequestFrame()` (a marker) followed by
   `LLMMessagesAppendFrame([...], run_llm=True)`. `ModalityGate`, placed directly before the realtime
   LLM, pushes `LLMConfigureOutputFrame(skip_tts=<marker present>)` before each new request enters the
   model. A regeneration after a tool result does not pass the gate and keeps the modality of its
   request. No marker is placed when TTS is disabled, and no `InterruptionFrame` is pushed.
3. While the assistant is generating, executing a tool or speaking, typed requests queue — first in,
   first out, at most five; further ones are dropped with a warning — and are triggered one by one
   when it becomes idle. Queued requests are already stored and visible. If no "assistant started"
   event follows a request within 60 s, the queue moves on.
4. Invalid input (blank, too long or malformed) produces a `warn` notice and is not stored.
5. Tasks record the modality of the delegating turn in `tasks.modality`; tasks delegated by typing
   are reported in text only (§7).

## 7. Tools of the realtime LLM

Tools are Pipecat _direct functions_: the signature and docstring are the tool description
(pipecat-notes.md §6). Names and parameters are in English, descriptions in Chinese. Results are
small JSON-serializable dictionaries.

| Tool             | Parameters                                                                            | Result                                                                                                                  | Kind         |
| ---------------- | ------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- | ------------ |
| `recall`         | `query?`, `speaker?` (display name), `minutes_ago_from?`, `minutes_ago_to?`, `limit?` | `{"items": [{"time": "00:14:02", "speaker": "...", "text": "..."}]}`                                                    | synchronous  |
| `get_digest`     | `scope`: `"all"` or `"recent"`                                                        | `{"digest": "...", "covers_until": "00:42:10", "since_then": [...]}`                                                    | synchronous  |
| `look_at_screen` | `frame_ids?` — comma-separated screenshot ids, at most three                          | Without ids: adds the latest screenshot to the context and lists earlier ones. With ids: adds those earlier screenshots | synchronous  |
| `delegate_task`  | `goal`, `minutes_of_context?` (default 5), `include_screen?`                          | First `{"task_id": "t3", "status": "accepted"}`; at the end `{"task_id", "status", "brief"}`                            | asynchronous |
| `task_status`    | `task_id?` (default: the most recent)                                                 | `{"task_id", "status", "goal", "recent_steps": [...]}`                                                                  | synchronous  |
| `cancel_task`    | `task_id`                                                                             | `{"task_id", "status"}`                                                                                                 | synchronous  |

Time parameters are "minutes ago" rather than absolute times: realtime models are poor at time
arithmetic, and "just now" or "ten minutes ago" map directly. Times in results are `HH:MM:SS` on the
session timeline.

Lookup tools (`pipeline/tools.py`):

- Tools reach storage, the embedding client and the configuration through `params.app_resources`.
  The current meeting and elapsed time come from the live connection.
- All parameters are optional. `minutes_ago_from` is the earlier bound and `minutes_ago_to` the
  later one; `0` means unbounded and swapped values are corrected. Strings, nulls and negative
  numbers count as absent. `limit` defaults to 10 with a maximum of 30; texts are cut at 300
  characters.
- `recall.speaker` is matched exactly by display name, then by unique substring. Without a match the
  result is `{"items": [], "note": "...", "speakers": [...]}`.
- `get_digest`: `all` returns the latest cumulative summary plus up to 40 captions after it;
  `recent` returns up to 60 captions of the last summary interval without the summary.
- `look_at_screen` without ids returns `{"time", "seconds_ago", "caption", "note", "earlier"}` and
  adds the latest screenshot to the context as a **user message** placed after the record of the
  tool call (it waits up to 0.5 s for that record). `earlier` lists up to 20 earlier screenshots as
  `{"id", "time", "caption"}` — those with a summary, not irrelevant and not repeating their
  neighbor — so that the model can decide whether to look back. With `frame_ids` it returns
  `{"frames": [{"id", "time", "caption"}], "note"}` and adds up to three screenshots of the current
  meeting as user messages. Without vision support only the latest summary is returned. Images stay
  in the context until the next compaction.
- **Requests to the assistant are not meeting content.** Typed input and ASR captions of the last
  60 s that contain the assistant's name are excluded from tool results, so that a question does not
  find itself. The assistant's own words are included.
- Empty results are explained in `note` rather than raised. With no live meeting the result is a
  note; internal errors return `{"error": "..."}` and never propagate into the pipeline.
- The tool order is fixed — `recall`, `get_digest`, `look_at_screen`, `delegate_task`,
  `task_status`, `cancel_task` — and definitions are identical in every request, which keeps the
  prefix cache stable.
- The task tools exist only when `agent.enabled = true`; their part of the prompt is
  `config/prompts/realtime_tasks.md`.

Task tools:

- `delegate_task` is declared with `@tool_options(cancel_on_interruption=False,
timeout_secs=<agent.task_timeout_secs + 140>)`. It reports acceptance immediately with
  `result_callback(..., properties=FunctionCallResultProperties(is_final=False))` and the final
  result with a second `result_callback`.
- `goal` is required and limited to 2,000 characters. The transcript window is the last
  `minutes_of_context` minutes, at most 60. With `include_screen`, the three most recent screenshots
  in that window are attached, or the latest one if the screen has not changed. With
  `agent.attach_frames`, every relevant screenshot so far is attached regardless of
  `include_screen`. The requester is the speaker of the latest finalized caption, or "typed input".
- Final results: `{"task_id", "status": "succeeded", "brief"}` or
  `{"task_id", "status": "failed" | "cancelled", "reason"}`.
- **When and how results are announced.** The modality of the delegating request is recorded. For a
  spoken request, completion waits for a pause in the conversation (`wait_quiet`, at most 20 s); for
  a typed one it is delivered at once and silently. Before the result is handed back, an
  `LLMConfigureOutputFrame` is queued on the LLM service, because the generation triggered by the
  result does not pass the modality gate. The task is then marked `announced`.
- `task_status` and `cancel_task` take the short label (`t2`, case-insensitive); an empty value means
  the most recent task.
- **What the model sees** (`pipeline/async_tools.py`). Pipecat stores asynchronous tool traffic in
  the context as JSON with English explanations and escaped Chinese. Before a request is sent these
  messages are rewritten as Chinese lines: `[任务 t1 已受理] …`, `[任务 t1 完成] <conclusion>`,
  `[任务 t1 失败] <reason>`, `[任务 t1 已取消] …`. The stored context is unchanged. After compaction,
  recently finished tasks are written in the same format.
- Pipecat appends an English instruction about late results to the system prompt when asynchronous
  tools are present. It contradicts this flow and is removed in `RealtimeLLMService`;
  `realtime_tasks.md` describes how late results are handled.

When `realtime.direct_mcp_tools` is non-empty, those MCP tools are attached to the realtime LLM
directly (pipecat-notes.md §7).

## 8. Background tasks

### 8.1 Task manager (`agent/tasks.py`)

```python
class TaskManager:
    async def submit(self, *, session_id: str, goal: str, requested_by: int, requested_t: float,
                     transcript_window: tuple[float, float], frame_ids: list[int],
                     modality: str) -> TaskRecord: ...
    async def wait(self, task_id: str) -> TaskResult: ...      # raises TaskFailed on failure or cancellation
    async def cancel(self, task_id: str) -> TaskRecord | None: ...
    async def status(self, session_id: str, label: str | None = None) -> dict | None: ...
    async def add_event(self, task_id: str, kind: str, summary: str, payload: dict | None = None) -> None: ...
```

- One manager serves the whole application; tasks keep running after a disconnect or a change of
  meeting.
- Concurrency is limited by `agent.max_concurrent_tasks` and run time by `agent.task_timeout_secs`.
- **Ids.** The primary key is `<session id>.t<n>`, unique across the database. The part after the
  dot is the label (`TaskRecord.label`) used in speech, tool parameters and the UI; the HTTP API uses
  the full id.
- `t_from`, `t_to` and `frame_ids_json` record the transcript window and screenshots given to the
  agent — what the task sent out.
- `wait` raises `TaskFailed(status, reason)`; an unknown task raises `LookupError`. Runner failures
  meant for the user are raised as `RunnerError("<reason>")`; other exceptions are reported as an
  internal error with the exception type, with details in the log.
- Queued tasks can be cancelled. `status` without a label returns the most recent task of the
  session with its last three progress events.
- After a restart, tasks left `queued` or `running` are marked `failed`.
- Working directory: `<data_dir>/sessions/<session>/tasks/<label>/`.

### 8.2 Runner input and output (`agent/runner.py`)

The first user message for the agent consists of:

1. the task goal;
2. the relevant transcript — captions within the window, formatted like the context lines in
   architecture.md §6, at most 400 lines;
3. the screenshots, attached as `image_url` data URLs and copied into `input/` of the task
   directory and of the sandbox workspace as `screen_<HHMMSS>_<id>.webp`;
4. limitations of this run, when the sandbox or an MCP server is unavailable.

Without `agent.supports_vision` the images are not attached and only mentioned in the text.

The agent's final answer must be this JSON object:

```json
{
  "brief": "A spoken-style conclusion of at most 60 characters",
  "detail_md": "Detailed result for the task panel, plain text",
  "sources": ["https://..."],
  "artifacts": ["plot.png"]
}
```

- Parsing is tolerant: a surrounding code fence, a sentence before or after, and literal newlines
  inside strings are accepted. If parsing fails or `brief` is missing, the raw answer becomes
  `detail_md` with a generic `brief`.
- `artifacts` accepts relative paths inside the workspace only. Files are fetched from the sandbox
  into the task directory; a file that cannot be fetched is not an artifact. The limit is 20 MB per
  file. Without a sandbox `artifacts` is always empty.
- Failures reported to the user as `RunnerError`: tasks disabled, agent not configured, dependency
  missing, remote model unreachable, invalid key, error status from the remote model, too many steps
  (`agent.max_turns`) and agent runtime errors.
- An unavailable sandbox or MCP server does not fail the task. A `note` event is recorded and the
  agent is told in its input.
- `agent.extra_body` is passed with every model request through `ModelSettings`.
- SDK usage is described in [agents-sdk-notes.md](agents-sdk-notes.md).

### 8.3 Progress events

The runner translates the agent framework's stream events into one-sentence summaries in Chinese
that can be read aloud.

| Event           | `kind`        | Example `summary`                |
| --------------- | ------------- | -------------------------------- |
| Task started    | `status`      | 开始处理                         |
| MCP tool called | `tool_call`   | 正在检索「对比学习 温度系数」    |
| Tool returned   | `tool_result` | 工具返回了结果（约 320 字）      |
| Code executed   | `tool_call`   | 正在运行一段代码                 |
| Code finished   | `tool_result` | 代码运行完成                     |
| Task finished   | `status`      | 已完成 / 失败：<reason> / 已取消 |

Wording (`describe_tool_call`, `describe_tool_output`): arguments named `query`, `q` or `keywords`
give "正在检索「…」"; `url` gives "正在打开「…」"; anything else "正在调用工具 xxx". Tool output is
only characterized — returned a result of about N characters, returned an error, returned nothing —
and never read out. `note` events report an unavailable sandbox or search service. Quoted queries
are limited to 60 characters.

## 9. Command lines of the inference services

The process supervisor (`services/supervisor.py`) builds command lines from the configuration.
Options follow `third_party/llama.cpp/tools/server/README.md` and
`third_party/qwentts.cpp/tools/tts-server.cpp` at the pinned versions.

**Realtime LLM** — only in `llama_server` mode, from `[realtime_llm.llama_server]`:

```
llama-server -m <model_path> [--mmproj <mmproj_path>] -a <model>
             --host 127.0.0.1 --port <from base_url>
             -c <ctx_size> -np <parallel> -ngl <gpu_layers>
             --jinja --reasoning <on|off>
             <extra_args...>
```

In `openai_api` mode nothing is launched. An unmanaged entry is created for a reachability check,
`GET <base_url>/models` with the key. **Any HTTP response counts as reachable**, because providers
differ in which routes they implement; only a connection failure or timeout counts as unreachable.

**ASR**

```
llama-server -m <model_path> --mmproj <mmproj_path>
             --host 127.0.0.1 --port <from base_url>
             -c <ctx_size> -np 1 -ngl <gpu_layers>
             --jinja --chat-template-file <data_dir>/run/asr_chat_template.jinja
             --cache-ram 0
             <extra_args...>
```

The template file is written before launch from the `chat_template` of the ASR profile. With
`asr.launch.enabled = false` the externally started server must be given the same
`--chat-template-file`; `services status` prints a reminder.

`--cache-ram 0` disables `llama-server`'s host-memory prompt cache (8 GB by default). ASR requests
share no prefix, and with the cache enabled the process grows by 8 GB during the first minutes
([benchmarks.md](benchmarks.md#resources-over-50-minutes)). The same applies to the embedding
server.

**Embeddings**

```
llama-server -m <model_path> -a <embedding.model> --embedding
             --host 127.0.0.1 --port <from base_url> -ngl <gpu_layers> --cache-ram 0 <extra_args...>
```

`gpu_layers = "0"` alone still allocates a few hundred MB on every GPU; add `--device none` to
`extra_args` to avoid that, as the template does.

**TTS**

```
tts-server --model <model_path> --codec <codec_path> --alias <tts.model>
           --host 127.0.0.1 --port <from base_url> --lang <default_language> <extra_args...>
```

General rules:

- Health check: `GET /health` returning 200, for both programs.
- Service names are fixed: `realtime`, `asr`, `tts`, `embedding`.
- Standard output and error of each child are appended to `data/logs/<name>.log`. A failed start
  reports the last 30 lines written by the current run.
- `launch.env` is merged into the child's environment. A service with `launch.enabled = false` is
  not started and is checked once, without waiting.
- Managed services bind to `127.0.0.1`, so their `base_url` must be a loopback address with an
  explicit port; otherwise `build_specs` fails and suggests `launch.enabled = false`.
- Before launching, each service is probed once. One that is already healthy — a leftover from a
  crash, or an instance started separately — is reused and not stopped on exit.
- Health checks of loopback addresses bypass the system proxy; remote addresses follow the system
  settings, as the application's later requests do.
- On exit a termination signal is sent, followed by a forced kill after 5 s. On Windows children are
  started with `CREATE_NEW_PROCESS_GROUP`, so that `Ctrl+C` in the terminal does not reach them
  directly and shutdown is always orderly.
- If the parent is killed or crashes, the operating system ends the children: on Windows they are
  placed in a job object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`; on Linux they register
  `prctl(PR_SET_PDEATHSIG)`. macOS has no equivalent, and leftover processes must be stopped
  manually.

**Extra request fields for the realtime LLM** always come from
`cfg.realtime_llm.request_extra_body(background=...)`:

| Mode           | Contents                                                                                                                                                                                                         |
| -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `llama_server` | The configured `extra_body`, `top_k` if set, and `{"id_slot": <slot>, "cache_prompt": true}`. Live answers and warm-ups use `realtime_slot`; background work passes `background=True` and uses `background_slot` |
| `openai_api`   | The configured `extra_body` and `top_k` if set. No llama.cpp-specific fields                                                                                                                                     |

**Speech synthesis endpoint** (qwentts.cpp `tts-server`):

- `POST /v1/audio/speech` with `{"model", "input", "voice", "language", "response_format": "pcm"}`
  in UTF-8. The response is `audio/pcm`, streamed in chunks: 24 kHz, 16-bit little-endian, mono.
- `language` is optional and defaults to the `--lang` value given at launch.
- An unknown voice yields 502 `{"error": {"message": "synthesis failed", ...}}`, not a 4xx.
  `GET /v1/audio/voices` returns `{"voices": [{"name": ..., "kind": "speaker"}, ...]}`. After the TTS
  service becomes healthy, `check_tts_voice` verifies the configured voice and stops startup with the
  list of available voices if it is missing. If the endpoint does not exist or answers in another
  format — as other OpenAI-compatible TTS services may — only a warning is logged.
