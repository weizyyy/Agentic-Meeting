# Configuration

**English** · [简体中文](zh-CN/configuration.md)

All settings live in `config/config.toml`, created from
[`config/config.example.toml`](../config/config.example.toml). Paths may be absolute or relative to
the repository root. Run `uv run agentic-meeting check` after editing; it reports every problem at
once.

Secrets are never stored in this file. Fields ending in `_env` hold the **name** of an environment
variable; the value goes in `.env` (see [`.env.example`](../.env.example)).

- [`[session]`](#session) · [`[retention]`](#retention) · [`[server]`](#server) · [`[realtime_llm]`](#realtime_llm) ·
  [`[asr]`](#asr) · [`[diarization]`](#diarization) · [`[tts]`](#tts) · [`[embedding]`](#embedding)
- [`[audio]`](#audio) · [`[turn]`](#turn) · [`[realtime]`](#realtime) · [`[screen]`](#screen) ·
  [`[transcript]`](#transcript) · [`[report]`](#report) · [`[agent]`](#agent)
- [Launch settings](#launch-settings)

## `[session]`

| Key              | Default  | Description                                                                                                                                                                                          |
| ---------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `data_dir`       | `"data"` | Where the database, screenshots, task files and logs are stored                                                                                                                                      |
| `assistant_name` | —        | The assistant's name and wake word. An English word (letters and digits, starting with a letter)                                                                                                     |
| `wake_aliases`   | `[]`     | Other spellings that also wake the assistant: English words, or strings of two or more Chinese characters. Useful when ASR keeps writing the name a certain way, e.g. `["Novel", "诺瓦"]` for `Nova` |
| `hotwords`       | `[]`     | Terms passed to ASR as hints: member names, project jargon, abbreviations                                                                                                                            |
| `members`        | `[]`     | Names offered as suggestions when renaming speakers                                                                                                                                                  |

`recording_notice` defaults to `true` and controls the start notice after the connection is ready. Setting it to `false` does not hide the fixed transcription status or disable transcription. This is not a consent dialog. New meetings and manual resumes show it once when actually ready; SDK automatic reconnection does not repeat it.

## `[retention]`

Each category is independent and defaults to `0` (no automatic cleanup). Zero never means immediate deletion.

| Key                     | Default | Range and meaning                                                             |
| ----------------------- | ------- | ----------------------------------------------------------------------------- |
| `transcript_days`       | `0`     | 0–36500; utterances, vectors and full-text index                              |
| `screenshots_days`      | `0`     | 0–36500; screenshot files and rows, including captions                        |
| `reports_days`          | `0`     | 0–36500; terminal reports and running summaries                               |
| `task_artifacts_days`   | `0`     | 0–36500; terminal task directories and artifact lists, not task input/results |
| `cleanup_interval_secs` | `3600`  | 60–86400; seconds between cleanup passes                                      |

Periods must be integers, not booleans, negative/fractional or out-of-range values. A day is 86400 seconds, compared using server UTC timestamps; meetings, reports, digests and tasks have different anchors. Live meetings and protected background work are skipped. Keep exempts all four categories from automatic cleanup; explicit deletion still applies. Interrupted meetings can expire without being ended. See [Data governance](data-governance.md#3-retention-and-keep) for clocks, surviving copies and retries.

## `[server]`

| Key                   | Default     | Description                                                                                                                                                  |
| --------------------- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `host`                | `"0.0.0.0"` | Bind address                                                                                                                                                 |
| `port`                | `7860`      | HTTP(S) port                                                                                                                                                 |
| `tls_cert`, `tls_key` | `""`        | Certificate and key files. Required for access from other devices; see [getting started](getting-started.md#access-from-other-devices)                       |
| `ice_servers`         | `[]`        | STUN/TURN servers used by both the server and the browser. Leave empty on a single LAN; see below                                                            |
| `password_env`        | `""`        | Name of the environment variable that holds the access password. Empty means no login is required. See [Access password](getting-started.md#access-password) |
| `auth_session_days`   | `7`         | How long a login lasts, in days (more than 0, at most 365). The session ends at that time even if the page is in use                                         |

Each entry of `ice_servers` is either a URL string (`"stun:host:3478"`) or a table with `urls` (a
string or a list), `username` and `credential_env`, the name of the environment variable that holds
the TURN password. URLs start with `stun:`, `stuns:`, `turn:` or `turns:`; TURN entries need
`username` and `credential_env`, because browsers reject TURN servers without credentials:

```toml
ice_servers = [
  "stun:turn.example.org:3478",
  { urls = ["turn:turn.example.org:3478", "turns:turn.example.org:5349"], username = "meeting", credential_env = "AGENTIC_MEETING_TURN_PASSWORD" },
]
```

The browser receives the same list, including the TURN password, from `GET /api/ice`, so anyone who
can open the page can read it. Set an access password when you configure TURN; `check` and `serve`
warn otherwise.

The password itself goes into `.env`, never into `config.toml`; it must be at least 8 characters
long. Changing it logs out every device. `agentic-meeting check` and `serve` warn when `host` is
not a loopback address and no password is set.

## `[realtime_llm]`

The model that answers when the assistant is called. `mode` selects one of two access modes; both
subsections may be filled in so that switching is a one-line change.

| `mode`           | Meaning                                                                                                                                                           |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `"llama_server"` | Served by llama.cpp's `llama-server`, which the application can launch. Uses separate slots for live answers and background work, and keeps the prompt cache warm |
| `"openai_api"`   | Any OpenAI-compatible chat completions endpoint. The application does not manage the process and assumes no special server features                               |

Keys common to `[realtime_llm.llama_server]` and `[realtime_llm.openai_api]`:

| Key               | Description                                                                                  |
| ----------------- | -------------------------------------------------------------------------------------------- |
| `base_url`        | Endpoint, ending in `/v1`                                                                    |
| `api_key_env`     | Environment variable holding the API key; empty if none is needed                            |
| `model`           | Value of the `model` request field                                                           |
| `supports_vision` | Whether the model accepts images. Screen summaries and `look_at_screen` depend on it         |
| `extra_body`      | Extra fields merged into every request, e.g. the switch that disables reasoning              |
| `sampling.*`      | `max_tokens`, `temperature`, `top_p`, `top_k`, `presence_penalty`. Unset fields are not sent |

Only for `llama_server`:

| Key                                | Default  | Description                                                                                                         |
| ---------------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------- |
| `thinking`                         | `false`  | Maps to `--reasoning on/off`. Keep it off for low latency                                                           |
| `realtime_slot`, `background_slot` | `0`, `1` | Server slots for live answers and for background work (screen summaries, running summary)                           |
| `launch.*`                         |          | See [Launch settings](#launch-settings). `ctx_size` is shared by all slots; `parallel` must cover both slot numbers |

Only for `openai_api`:

| Key                       | Default | Description                                                                                             |
| ------------------------- | ------- | ------------------------------------------------------------------------------------------------------- |
| `supports_developer_role` | `false` | Whether the endpoint accepts the `developer` role                                                       |
| `cache_warm`              | `false` | Send periodic warm-up requests. Enable only if the server has a prefix cache and the requests are cheap |

> [!IMPORTANT]
> When the endpoint is not on the local machine, the meeting transcript — and screenshots, if vision
> is enabled — are sent to it continuously. `check` and `serve` print a reminder.

## `[asr]`

Streaming speech recognition through `llama-server`'s audio input.

| Key                               | Default                                      | Description                                                                                                                         |
| --------------------------------- | -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `backend`                         | `"llama_server"`                             | Recognition backend                                                                                                                 |
| `profile`                         | `"config/asr_profiles/confucius4_r2t2.toml"` | Prompt-format profile of the ASR model. Add a new profile to use a different model family                                           |
| `base_url`                        | `"http://127.0.0.1:8081"`                    | ASR server address                                                                                                                  |
| `language`                        | `"Chinese"`                                  | Language hint; empty lets the model decide                                                                                          |
| `chunk_ms`                        | `320`                                        | Audio added per step (80–2000). Smaller means lower latency and more GPU load                                                       |
| `window_secs`, `window_drop_secs` | `16.0`, `8.0`                                | Rolling audio window and how much is dropped when it fills                                                                          |
| `unfixed_tokens`                  | `1`                                          | Trailing tokens held back as provisional at each step                                                                               |
| `max_new_tokens`                  | `32`                                         | Generation cap when a segment is finalized                                                                                          |
| `preroll_ms`                      | `1500`                                       | Audio from before speech was detected that is still sent to ASR. A short wake word followed by a pause is lost if this is too small |
| `launch.*`                        |                                              | See [Launch settings](#launch-settings). The ASR server always runs a single slot                                                   |

## `[diarization]`

| Key                | Default         | Description                                                                            |
| ------------------ | --------------- | -------------------------------------------------------------------------------------- |
| `backend`          | `"nemo_ctypes"` | `"none"` disables diarization; every caption is then attributed to one unknown speaker |
| `library_path`     | `""`            | Path to the `nemo_speech_asr_c` library; empty searches `runtimes/nemo_speech`         |
| `model_path`       | —               | Diarization GGUF                                                                       |
| `gpu`              | `0`             | GPU index in llama.cpp's CUDA numbering; `-1` for CPU                                  |
| `preset`           | `""`            | Streaming preset; empty uses the model's low-latency default                           |
| `poll_interval_ms` | `320`           | How often audio is fed to the model                                                    |
| `segmentation.*`   | `0`             | Onset/offset thresholds, padding and minimum durations; `0` keeps the library defaults |

## `[tts]`

Speech synthesis through an OpenAI-compatible `/v1/audio/speech` endpoint that streams PCM.

| Key           | Default                      | Description                                                                          |
| ------------- | ---------------------------- | ------------------------------------------------------------------------------------ |
| `enabled`     | `true`                       | `false` gives text-only answers                                                      |
| `base_url`    | `"http://127.0.0.1:8082/v1"` | TTS server address                                                                   |
| `model`       | `"tts"`                      | Request `model` field; matches the server's `--alias`                                |
| `voice`       | —                            | Voice name supported by the chosen weights                                           |
| `language`    | `"Chinese"`                  | Synthesis language                                                                   |
| `sample_rate` | `24000`                      | Output sample rate in Hz                                                             |
| `launch.*`    |                              | `model_path` (talker), `codec_path`, `default_language`, plus the common launch keys |

## `[embedding]`

| Key              | Default                      | Description                                                                                                          |
| ---------------- | ---------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `enabled`        | `true`                       | `false` limits recall to keyword search                                                                              |
| `base_url`       | `"http://127.0.0.1:8083/v1"` | Embedding server address                                                                                             |
| `model`          | `"embedding"`                | Request `model` field                                                                                                |
| `dimensions`     | `1024`                       | Must equal the model's output dimension                                                                              |
| `query_prefix`   | `""`                         | Instruction prepended to queries, if the model recommends one                                                        |
| `min_similarity` | `0.4`                        | Cosine-similarity floor for semantic matches; `0` disables it                                                        |
| `launch.*`       |                              | See [Launch settings](#launch-settings). Add `--device none` to `extra_args` to keep the server off the GPU entirely |

## `[audio]`

Automatic input gain, applied before voice activity detection.

| Key                | Default | Description                                             |
| ------------------ | ------- | ------------------------------------------------------- |
| `auto_gain`        | `true`  | Raise quiet input to the target level                   |
| `gain_db`          | `0.0`   | Initial gain, or the fixed gain when `auto_gain` is off |
| `max_gain_db`      | `30.0`  | Upper limit of automatic gain                           |
| `target_dbfs`      | `-16.0` | Target RMS of voiced audio; closer to 0 is louder       |
| `noise_floor_dbfs` | `-70.0` | Frames below this level are treated as silence          |
| `level_log_secs`   | `10`    | Interval of the input-level log line; `0` disables it   |

## `[turn]`

| Key                 | Default | Description                                                                                                                         |
| ------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `vad_stop_secs`     | `0.2`   | Silence that ends a speech segment                                                                                                  |
| `vad_min_volume`    | `0.6`   | Volume gate of voice activity detection (0.6 ≈ −50 LUFS); `0` disables the gate                                                     |
| `smart_turn`        | `true`  | Use the end-of-turn model; otherwise a fixed 0.6 s pause                                                                            |
| `wake_timeout_secs` | `30.0`  | Maximum time the assistant stays awake after its name is heard. It is also the window in which speech can interrupt a spoken answer |
| `single_activation` | `true`  | Require the name for every request. The assistant returns to idle as soon as it has answered                                        |

## `[realtime]`

| Key                        | Default          | Description                                                       |
| -------------------------- | ---------------- | ----------------------------------------------------------------- |
| `context_budget_tokens`    | `24000`          | Context size that triggers compaction during idle time            |
| `keep_recent_minutes`      | `10.0`           | Verbatim transcript kept after compaction                         |
| `digest_interval_minutes`  | `5.0`            | Interval of the running summary                                   |
| `digest_provider`          | `"realtime_llm"` | Who writes the running summary: `"realtime_llm"` or `"agent_llm"` |
| `cache_warm_interval_secs` | `30.0`           | Interval of prompt-cache warm-up requests                         |
| `direct_mcp_tools`         | `[]`             | MCP tools the realtime model may call directly                    |

## `[screen]`

| Key                 | Default          | Description                                                                             |
| ------------------- | ---------------- | --------------------------------------------------------------------------------------- |
| `enabled`           | `true`           | Accept screenshots                                                                      |
| `min_interval_secs` | `2.0`            | Minimum time between change-triggered screenshots                                       |
| `heartbeat_secs`    | `60.0`           | Screenshot interval when the picture is static                                          |
| `max_side_px`       | `1920`           | Longest side of an uploaded screenshot                                                  |
| `change_threshold`  | `0.04`           | How much the most-changed block of the thumbnail must differ (0–1) to count as a change |
| `caption`           | `true`           | Generate a text summary for each new picture                                            |
| `caption_provider`  | `"realtime_llm"` | Who writes the summaries: `"realtime_llm"` or `"agent_llm"`                             |

## `[transcript]`

| Key                | Default | Description                                                                                           |
| ------------------ | ------- | ----------------------------------------------------------------------------------------------------- |
| `merge_gap_secs`   | `2.0`   | Merge consecutive segments from one speaker when the pause is shorter than this; `0` disables merging |
| `merge_soft_chars` | `40`    | Start a new caption once a caption has this many characters and ends a sentence                       |
| `merge_max_chars`  | `200`   | Hard limit for a merged caption                                                                       |

## `[report]`

| Key               | Default          | Description                                                                                 |
| ----------------- | ---------------- | ------------------------------------------------------------------------------------------- |
| `provider`        | `"realtime_llm"` | Who writes the post-meeting report: `"realtime_llm"` or `"agent_llm"`                       |
| `max_input_chars` | `8000`           | Transcript characters per request. Longer transcripts are summarized in sections and merged |

## `[agent]`

The background agent: a remote LLM with MCP tools and an optional code sandbox.

| Key                                | Default | Description                                                                                                                                                                                           |
| ---------------------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `enabled`                          | `true`  | `false` removes the task tools from the assistant                                                                                                                                                     |
| `base_url`, `api_key_env`, `model` | —       | OpenAI-compatible endpoint of the agent model                                                                                                                                                         |
| `supports_vision`                  | `true`  | Whether the model accepts images                                                                                                                                                                      |
| `extra_body`                       | `{}`    | Extra fields merged into every request to this model, typically the reasoning effort, e.g. `{ reasoning_effort = "medium" }`                                                                          |
| `attach_frames`                    | `false` | Attach the original screenshots to work given to this model — tasks, and reports or summaries it writes. Off: only text summaries are used, and tasks carry up to three recent screenshots on request |
| `max_attached_frames`              | `40`    | Screenshots per request when `attach_frames` is on; beyond that they are sampled evenly                                                                                                               |
| `generation_max_tokens`            | `16384` | Output limit when this model writes reports, summaries or screen captions. Reasoning tokens count toward it                                                                                           |
| `max_turns`                        | `30`    | Agent loop limit per task                                                                                                                                                                             |
| `task_timeout_secs`                | `900.0` | Time limit per task                                                                                                                                                                                   |
| `max_concurrent_tasks`             | `2`     | Tasks running at the same time                                                                                                                                                                        |

`[[agent.mcp_servers]]` — one block per MCP server (streamable HTTP):

| Key            | Description                                                 |
| -------------- | ----------------------------------------------------------- |
| `name`         | Label used in logs and progress messages                    |
| `url`          | MCP endpoint                                                |
| `headers_env`  | Map of header name → environment variable holding its value |
| `timeout_secs` | Request timeout                                             |

`[agent.sandbox]`:

| Key            | Default    | Description                                                                       |
| -------------- | ---------- | --------------------------------------------------------------------------------- |
| `kind`         | `"docker"` | `"docker"`, `"local"` (trusted macOS/Linux development machines only) or `"none"` |
| `docker_image` | `""`       | Image for the sandbox container; see [`docker/sandbox`](../docker/sandbox)        |
| `network`      | `false`    | Allow network access from the sandbox                                             |
| `timeout_secs` | `120.0`    | Reserved; individual command timeouts are chosen by the model                     |

Screenshots are deduplicated before they are attached: pictures summarized as irrelevant are
skipped and unchanged repeats are sent once. An image costs roughly one token per 1,024 pixels on
the model used in testing (about 2,000 tokens for 1920×1080), so size `max_attached_frames` to the
model's context window.

## Launch settings

Each locally launched service has a `launch` table.

| Key                         | Description                                                                                    |
| --------------------------- | ---------------------------------------------------------------------------------------------- |
| `enabled`                   | `false` means the service is started elsewhere and the application only connects to `base_url` |
| `executable`                | Path to the server binary; empty searches the default locations                                |
| `model_path`, `mmproj_path` | Weights and, where applicable, the multimodal projector                                        |
| `ctx_size`                  | Context size passed to the server                                                              |
| `parallel`                  | Number of slots (realtime LLM only)                                                            |
| `gpu_layers`                | Passed to `-ngl`: a number, `"auto"` or `"all"`; `"0"` runs on CPU                             |
| `env`                       | Extra environment variables, e.g. `{ CUDA_VISIBLE_DEVICES = "1" }`                             |
| `extra_args`                | Arguments appended verbatim, e.g. `["--device", "CUDA0"]`                                      |

The exact command lines are documented in [interfaces.md §9](interfaces.md), and multi-GPU layouts
in [runtimes.md §5](runtimes.md).
