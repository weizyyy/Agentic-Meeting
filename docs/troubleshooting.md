# Troubleshooting

**English** · [简体中文](zh-CN/troubleshooting.md)

- [Startup](#startup)
- [Health and metrics](#health-and-metrics)
- [Captions are fragmentary or missing](#captions-are-fragmentary-or-missing)
- [The assistant does not respond to its name](#the-assistant-does-not-respond-to-its-name)
- [Answers are slow](#answers-are-slow)
- [Speakers are mislabeled](#speakers-are-mislabeled)
- [Screenshots and summaries](#screenshots-and-summaries)
- [Reports and background tasks](#reports-and-background-tasks)
- [Behavior when a service is down](#behavior-when-a-service-is-down)

## Startup

**`check` reports missing items.** Each line names the configuration key to fix. Model paths are
resolved relative to the repository root.

**A service does not become ready.** The error message includes the last lines of
`data/logs/<service>.log`. Common causes are a wrong model path, insufficient GPU memory, or a port
already in use by a previous run.

**Another device cannot use the microphone or share its screen.** Browsers require HTTPS for both.
See [Access from other devices](getting-started.md#access-from-other-devices).

**Inference processes are left running after a crash.** On Windows and Linux the operating system
ends them when the application process disappears. macOS has no equivalent mechanism; stop
`llama-server` and `tts-server` manually.

## Health and metrics

Request the [backend application port](getting-started.md#check-health-and-metrics), not the Vite
port. `/metrics` is JSON, not Prometheus exposition text. Field and response definitions are in
[interfaces.md §5.8](interfaces.md#58-health-checks-and-basic-metrics).

**Health is 200 but readiness is 503.** `/healthz` only proves that the HTTP process can answer;
it does not check the DB or models. Inspect readiness `lifecycle`, `storage` and `services.asr`.
`starting`/`stopping`, an unavailable Store or unhealthy ASR prevents readiness. `SELECT 1`
proves the existing DB connection can read, not disk capacity or future write durability.
An enabled optional realtime, TTS, embedding or agent endpoint failing or unknown instead gives
HTTP 200 with `degraded`. Disabled services are normal.

**Metrics are partial or slow on a cold cache.** Local gauges are read on each request; service
and successful task-count snapshots are refreshed on demand and cached for less than 5 seconds.
Concurrent requests share collection work. Storage readiness and task counting each have a
0.5-second wait budget; concurrent HTTP service probes have a 2-second round budget; the entire
readiness/metrics collector has a 2.5-second budget, including shared-work and DB waits. These are
collection budgets, not a network response SLA: scheduling and HTTP transmission can add time.
Missing observations return `null`/`unknown` and metrics `partial`, without discarding other data.
Expired successful values are not served as fresh. A service round containing unknown results may
also be reused for 5 seconds, with age `null`; task counts recover only after a new successful
query. An observed storage failure immediately invalidates cached counts. Downstream recovery
appears on subsequent refreshes; there is no permanent polling loop.

Numeric fields in the five metric groups use finite nonnegative counts or seconds. `null` means no sample or an
unavailable observation, never zero; normal idle zeros and no-caption nulls do not imply `partial`.

| Group | Interpretation |
|---|---|
| Live connections | `live_connections` is 0 or 1 for the current registered active media connection, not browser visits or historical rows; assembly/takeover gaps can show 0 |
| Caption lag | `caption_lag_seconds` is sampled after the first successful nonempty server push for each ASR delta. `caption_sample_age_seconds` is the sample's monotonic age in seconds |
| Queues | `transcript_retry` counts unsaved utterances; `screen_caption.depth` is 0 or 1 pending new-screen slot, excluding work in progress and summary-reuse followers (disabled is 0); `agent_tasks` is the DB `queued` count |
| Retained tasks | `task_counts` covers all retained DB tasks in the five named states, even with the agent disabled. These are gauges, not totals since startup; deletion can lower them. Empty DB means five zeros; query failure makes the whole group null |
| HTTP services | Fixed names `asr`, `realtime`, `tts`, `embedding`, `agent`; state and reason semantics match readiness. `unavailable` is a complete observation and alone does not make metrics partial |

Caption lag estimates backlog on the shared session audio timeline. It excludes network delivery,
browser rendering and final word stabilization, and cannot verify the 1.5-second finalized-caption
target. During silence, lag stays at the last sample while age grows: check age before interpreting
an old low value. A new connection starts with no sample; disconnect/takeover clears the old one.
Resume uses the shared timeline without adding the resume base twice. Failed or invalid samples
leave the previous sample and its increasing age intact.

`task_counts_age_seconds` and `service_snapshot_age_seconds` describe separate snapshots, so their
ages may differ. Counts age is null when unavailable; service age is null when any enabled service
is unknown. The grouped task query scans retained tasks, so its cost grows with history; exceeding
the budget yields null. A SQLite query already queued may finish after its awaiting coroutine is
cancelled; collection does not interrupt other business queries.

**A service is reachable but inference fails.** Generic OpenAI-compatible `/models` probes treat
any HTTP response, including 401/403/404/503, as `reachable`: they do not prove authentication,
model access, inference success or model quality. Dedicated `/health` needs HTTP 200 for `ok`.
Only configured HTTP inference endpoints are probed, including externally managed ones;
in-process diarization, MCP, Docker, browser ICE and bandwidth are outside coverage.

**Startup or shutdown checks do not connect.** The server may not listen before lifespan startup
completes or after shutdown begins. If a request reaches an app in `starting`/`stopping`, readiness
is 503 and metrics partial, with unavailable observations null/unknown; disabled screen depth is
still 0. This does not guarantee network access during those phases.

## Captions are fragmentary or missing

The usual cause is a microphone that is too quiet. Voice activity detection has a volume gate; when
only the loudest syllables pass it, each sentence is cut into fragments and ASR receives only a few
words.

The server applies automatic gain by default (`[audio]`), and the meter at the top of the page
shows the input level before gain. Fixing the level at the source works best:

1. Raise the input volume of the microphone in the operating system's sound settings.
2. If the driver offers a microphone boost, set it to about +20 dB. More than that amplifies noise.
3. Move closer, or use an external or headset microphone. Built-in laptop microphones are usually
   the quietest.
4. Confirm that the browser is using the microphone you adjusted.

To measure how quiet a microphone is and how much gain recovers, record ten seconds or so in your
usual meeting position and run:

```bash
uv run python scripts/mic_check.py --wav recording.wav
```

The script needs no model weights. It replays the recording at several gain levels through voice
activity detection. A low *speech ratio* together with a high count of *speech starts* on the
`0 dB` row means the input is being chopped up. Background and reference numbers are in
[benchmarks.md](benchmarks.md#low-microphone-level).

If raising the level is not possible, lower `turn.vad_min_volume` or raise `audio.target_dbfs`.

## The assistant does not respond to its name

Look at how the name appears in the captions.

- **Written differently** (for example `novel` or `诺瓦` for `Nova`): add that spelling to
  `session.wake_aliases`.
- **Missing or cut in half**: the name was spoken before voice activity detection triggered.
  Increase `asr.preroll_ms` (default 1500).
- **Written correctly, still no answer**: the realtime LLM is failing. The page shows
  "助理暂不可用" with the reason, and `uv run agentic-meeting services status` shows whether the
  endpoint is reachable.

A name that is a single distinct English word, not used in normal conversation, is recognized most
reliably.

## Answers are slow

- **Reasoning is enabled on the realtime model.** Turn it off (`thinking = false` for llama.cpp, or
  the provider's switch in `extra_body`). Time to first token multiplies otherwise.
- **The end-of-turn model keeps waiting.** When it judges a sentence unfinished, the pipeline waits
  a few seconds before answering. Set `turn.smart_turn = false` to use a fixed 0.6 s pause instead.
- **The endpoint reprocesses the whole context on every request.** Lower
  `realtime.context_budget_tokens`, or enable `cache_warm` if the server has a prefix cache.

The server log prints a latency breakdown after every spoken answer (`应答延迟拆分`).

## Speakers are mislabeled

- Rename a speaker or merge two speakers from the chips above the captions, or reassign individual
  captions; see the [user guide](user-guide.md#captions-and-speakers).
- After a server restart, diarization starts over and returning speakers get new labels. Merge them
  into the earlier ones.
- The diarization model used in testing separates at most eight speakers.
- The first short utterance of a new speaker is the most likely to be attributed to someone else.

## Screenshots and summaries

- **A slide change is not captured until the next minute.** The change did not exceed
  `screen.change_threshold`. Lower it, or lower `screen.heartbeat_secs`.
- **Too many screenshots** (a video or an animation is on screen): raise `screen.change_threshold`
  or `screen.min_interval_secs`.
- **No summaries.** The model that writes them must accept images (`supports_vision`). While the
  assistant is answering, summaries are paused and resume afterwards; when slides change faster than
  summaries can be written, only the newest picture is summarized.

## Reports and background tasks

- **The report is empty.** A reasoning model can spend its whole output budget on reasoning. Raise
  `agent.generation_max_tokens` or lower the reasoning effort in `extra_body`.
- **A task says it cannot run code.** Docker is not running or `agent.sandbox.docker_image` is not
  set. The task still completes with search and image reading only.
- **A task fails with an authentication error.** The environment variable named by `api_key_env` or
  `headers_env` is not set in the shell that started the server; check `.env`.

## Behavior when a service is down

Transcription has the highest priority: no failure elsewhere stops it.

| Failure | What you see | What still works |
|---|---|---|
| Realtime LLM unreachable | Notice "助理暂不可用" with the reason; no answers | Transcription, screenshots, export |
| Speech synthesis unreachable | Notice "语音不可用"; answers appear as text only | Everything else; answers are still stored |
| Embedding service unreachable | Nothing visible | Recall falls back to keyword search; embeddings are backfilled later |
| Diarization fails to load | Notice at connection time; captions carry no speaker | Transcription and answers |
| ASR server unreachable | Notice "识别服务暂时不可用，正在重试"; captions pause | Retries with backoff and resumes on its own |
| Screen summary fails | That screenshot has no summary | The screenshot stays on the timeline and can still be viewed by the assistant |
| Remote agent model or MCP unreachable | The task is marked failed with the reason | Everything else |
| Docker missing | Tasks cannot run code and say so | Search and image reading |
| Browser disconnects | The meeting becomes *interrupted* | The page reconnects automatically; the timeline continues |
