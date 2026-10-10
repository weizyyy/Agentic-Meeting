# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Anonymous JSON `GET /healthz`, `/readyz` and `/metrics` endpoints for process liveness,
  transcription readiness and basic connection, caption-lag, queue, retained-task and HTTP-service
  gauges. Optional service failures allow degraded readiness; incomplete metrics preserve available
  observations. Responses exclude meeting content and credentials, while numeric gauges expose load.
- English and Chinese guidance for requesting and interpreting health checks and metrics.
- Optional access password for the web application (`server.password_env`,
  `server.auth_session_days`): a login page, a signed HTTP-only session cookie that lasts 7 days by
  default, logout, CSRF protection for state-changing requests including WebRTC signaling, and a
  limit of 5 failed logins per address in 5 minutes. Login requests larger than 16 KiB are refused
  without being read into memory. Every `/api` endpoint is covered; without a
  password nothing changes. `check` and `serve` warn when the server listens beyond the local
  machine without a password. `scripts/soak.py` logs in when a password is configured.
- TURN servers with credentials in `server.ice_servers`: an entry can be a table with `urls`,
  `username` and `credential_env` (the TURN password stays in the environment). The browser now
  receives the same ICE servers from the new `GET /api/ice` endpoint; before, `server.ice_servers`
  applied to the server side only and the browser connected without any. `check` and `serve` warn
  when TURN is configured without an access password.
- A pinned repository-wide Prettier formatter for client code, JSON, YAML and Markdown, with
  shared configuration, `npm run format` / `npm run format:check`, pre-commit and CI checks.

- Optional independent retention periods for transcripts, screenshots, reports/digests and
  terminal task files, disabled by default; persistent per-meeting Keep exempts automatic cleanup.
  Complete manual deletion retries partial failures with a visible pending state. The client
  shows a fixed transcription indicator and a configurable start notice; bilingual data governance
  documentation explains data destinations, surviving copies and deletion limits.
- A deployment guide (`docs/deployment.md`): Caddy and nginx examples with the headers, upload size
  and streaming the application needs, when and how to use TURN with a coturn example that relays
  only to the meeting server, a checklist before exposing an instance, and troubleshooting.

### Fixed

- On a slow or busy machine, a meeting connection could stay stuck without the page ever receiving
  its session, and the first words after connecting could be held back until the assistant
  answered only the start of a question. Loading the voice activity and turn-detection models
  blocked the server for a few seconds while the connection was being set up, and their first
  inference delayed the start of the audio. Both now happen in a worker thread before the meeting
  starts.
- `/readyz` no longer briefly reports `not_ready` with a storage `timeout` while service probes
  refresh. Each probe used to reload the CA certificates on the event loop, which on slower machines
  stalled it past the 0.5-second storage budget; the certificates are now loaded once per process.
- A single `Ctrl+C` during a meeting now stops `agentic-meeting serve` (and then the inference
  services started with `--with-services`). Previously the server waited for the meeting to end,
  which only happened on a second `Ctrl+C`.
- The web client no longer depends on `c.daily.co`. The Pipecat SDK's default media manager
  downloaded a script from that host and sent error reports to `sentry.io` when a meeting started,
  so browsers without access to it could not connect at all. The page now uses the SDK's
  `WavMediaManager`, which makes no requests of its own, and switches the connection to the new
  microphone track itself when the system default microphone changes.

## [0.1.1] - 2026-10-09

### Added

- Support for Python 3.13 and 3.14. The locked versions of existing dependencies are unchanged;
  `audioop-lts` is added for these interpreters. CI tests all three versions on Windows and Linux.
- A roadmap (`ROADMAP.md`) with milestones and issues on GitHub.
- Optional pre-commit hooks that mirror the CI lint and format checks.

### Changed

- The artifact download endpoint also verifies that the task directory lies inside the data
  directory, in addition to the existing check that the file lies inside the task directory.
- Documentation: a single 12 GB graphics card is documented as the minimum for the local speech
  models (ASR at Q8 and the 0.6B speech synthesis model use slightly more than 10 GB together with
  the other local services). The earlier figure of about 6.5 GB was the sum of services measured
  one at a time.
- The README states what the project is for, that it is under heavy development, and that it has
  no sign-in yet.
- The code of conduct is now the full text of Contributor Covenant 3.0, in English and Chinese.

## [0.1.0] - 2026-10-09

First public release.

### Added

- **Transcription** — streaming ASR through `llama-server`, speaker diarization through
  NeMo-Speech.cpp, automatic input gain, merging of adjacent segments into readable captions, and
  storage in SQLite with keyword (FTS5) and semantic (sqlite-vec) search.
- **Assistant** — wake-word activation with configurable aliases, spoken and text answers,
  barge-in, typed questions answered in text only, and tools for recall, running summaries and
  reading the shared screen, including earlier screenshots.
- **Realtime LLM access modes** — a llama.cpp deployment with dedicated slots and cache warm-up, or
  any OpenAI-compatible chat completions endpoint.
- **Screen timeline** — change-triggered screenshots, per-picture summaries, and optional
  attachment of original screenshots to work done by the agent model.
- **Background agent** — task delegation with progress reporting and cancellation, built on the
  OpenAI Agents SDK with MCP servers and a Docker sandbox.
- **Sessions** — resume after disconnects, device changes and server restarts; read-only viewing
  from other devices; speaker renaming, merging and per-caption reassignment.
- **After the meeting** — structured reports and export to Markdown, JSON and ZIP.
- **Operations** — `agentic-meeting check`, `services up/status` and `serve`; supervised inference
  services that are terminated with the parent process; notices in the UI when a service is degraded.
- **Tooling** — `scripts/soak.py` (headless end-to-end replay), `scripts/eval_realtime_model.py`
  (tool-selection and latency checks for candidate models) and `scripts/mic_check.py`.

### Known limitations

- End-to-end verification has been done on Windows 11 only. On Linux the unit-test suite passes;
  macOS is untested.
- The longest continuous run tested is 54 minutes.
- The web UI, prompts and CLI messages are available in Chinese only.

[Unreleased]: https://github.com/weizyyy/Agentic-Meeting/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/weizyyy/Agentic-Meeting/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/weizyyy/Agentic-Meeting/releases/tag/v0.1.0
