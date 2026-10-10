# Troubleshooting

**English** · [简体中文](zh-CN/troubleshooting.md)

- [Startup](#startup)
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

**The page keeps asking for the password, or says there were too many attempts.** The password is
read from the variable named by `server.password_env` when the application starts; restart it after
changing `.env`. After 5 wrong attempts from one address within 5 minutes, logins from that address
are refused until the oldest attempt is 5 minutes old. Over plain HTTP from another device the login
works but the password is not encrypted; use HTTPS.

**Inference processes are left running after a crash.** On Windows and Linux the operating system
ends them when the application process disappears. macOS has no equivalent mechanism; stop
`llama-server` and `tts-server` manually.

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
activity detection. A low _speech ratio_ together with a high count of _speech starts_ on the
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

| Failure                               | What you see                                          | What still works                                                              |
| ------------------------------------- | ----------------------------------------------------- | ----------------------------------------------------------------------------- |
| Realtime LLM unreachable              | Notice "助理暂不可用" with the reason; no answers     | Transcription, screenshots, export                                            |
| Speech synthesis unreachable          | Notice "语音不可用"; answers appear as text only      | Everything else; answers are still stored                                     |
| Embedding service unreachable         | Nothing visible                                       | Recall falls back to keyword search; embeddings are backfilled later          |
| Diarization fails to load             | Notice at connection time; captions carry no speaker  | Transcription and answers                                                     |
| ASR server unreachable                | Notice "识别服务暂时不可用，正在重试"; captions pause | Retries with backoff and resumes on its own                                   |
| Screen summary fails                  | That screenshot has no summary                        | The screenshot stays on the timeline and can still be viewed by the assistant |
| Remote agent model or MCP unreachable | The task is marked failed with the reason             | Everything else                                                               |
| Docker missing                        | Tasks cannot run code and say so                      | Search and image reading                                                      |
| Browser disconnects                   | The meeting becomes _interrupted_                     | The page reconnects automatically; the timeline continues                     |

## Keep, cleanup and pending deletion

**Data remains after setting a period.** Zero disables that category. The clocks in [Data governance §3](data-governance.md#3-retention-and-keep) may start later than the meeting; equality with the cutoff is retained. Live, kept or protected background work is skipped and revisited later. Expiring transcripts does not delete report, screenshot-caption or task input copies.

**Keep did not save.** Read the error: network failures, expired login, CSRF rejection or deletion conflicts do not show success. Retry after reconnecting or logging in. Keep cannot undo pending deletion.

**Deletion fails or remains pending.** A 409 can mean busy meeting/background work or another deletion attempt; wait. A 500 means deletion is incomplete and some files may already be gone. The page refreshes status and retains Retry delete; cleanup passes and restart also retry, even with all periods zero. Repair server directory permissions, disk or refused links/path ownership before retrying; do not move the database or construct deletion paths manually. Successful deletion removes the record; DELETE of an already removed id returns 404. There is no undo.

**The start notice is absent.** `session.recording_notice` controls only the dismissible notice, which automatic SDK reconnect does not repeat. The top bar's transcription indication remains independent of assistant state and successful detail loading.
