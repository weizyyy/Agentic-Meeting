# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- The web client is rebuilt on Tailwind CSS 4, shadcn components and [Pipecat UI](https://ui.pipecat.ai),
  starting with the top bar: connection status, a theme menu (follow system, light or dark), and
  Pipecat's microphone control, which mutes and unmutes and picks the microphone and speaker. The
  rest of the page takes the same colors and themes and moves to the new components in later
  releases.
- The meeting title, speakers and captions move to the new components. The title row has
  **保留** (Keep) and an **导出** (Export) menu; each caption puts the speaker and times on one line
  above the text, assistant answers are tinted, and **跟随最新** (Follow latest) appears while you
  scroll back through earlier captions.

### Changed

- Firefox 128 is now the oldest supported Firefox (was 114), because the stylesheet uses features
  it added. Chrome, Edge and Safari minimums are unchanged.
- The microphone level meter and its quiet-input chip in the top bar are replaced by the bars on
  the microphone button. A microphone that stays too quiet is still reported by the server's notice.

### Removed

- The Playwright browser end-to-end tests, their test server and the _Browser end-to-end_ CI job.
  In 14 failed runs they caught one product defect; the other 13 were problems in the tests
  themselves, and the job was the slowest in CI. Server behavior stays covered by `tests/e2e/`.

## [0.3.0] - 2026-10-12

This release completes the v0.3 milestone: the web client keeps working across microphone
permission changes, reconnections and screen-sharing interruptions in Chrome, Edge, Firefox and
Safari, and `agentic-meeting check` estimates the GPU memory of the configured local services.

Upgrading from 0.2.1: nothing to change. Configuration, database and containers work as before.

### Added

- `agentic-meeting check` prints a GPU memory estimate: the size of the weight files of every
  service it launches on the GPU, and their total. Context caches and runtime buffers come on top,
  so the figure is a lower bound ([#57]).

### Changed

- The browser end-to-end tests cover a connection that closes unexpectedly, a manual resume after
  automatic retries give up, a meeting taken over by a second page, microphone permission granted
  after a refusal, the assistant's voice blocked by autoplay, and screen captures that arrive after
  the meeting ended ([#55], by [@gad-en1nd]).
- The user guide and README state which desktop browsers have been checked by hand on macOS
  ([#55], [#56]).
- Lowering GPU memory requirements ([#16]) is now a long-term goal outside any release; the roadmap
  lists it under Not scheduled ([#59]).

### Fixed

- Automatic peer replacement keeps the microphone track used by the new peer instead of stopping
  it while closing the old connection. Ending the meeting still releases sender tracks ([#55], by
  [@gad-en1nd]).
- Starting or resuming after microphone permission is restored retries device initialization
  without requiring a page reload ([#55], by [@gad-en1nd]).
- Screen sharing releases streams returned after the meeting ends or disconnects, and repeated
  clicks while the screen picker is open no longer request another capture ([#55], by
  [@gad-en1nd]).

[#16]: https://github.com/weizyyy/Agentic-Meeting/issues/16
[#55]: https://github.com/weizyyy/Agentic-Meeting/pull/55
[#56]: https://github.com/weizyyy/Agentic-Meeting/pull/56
[#57]: https://github.com/weizyyy/Agentic-Meeting/pull/57
[#59]: https://github.com/weizyyy/Agentic-Meeting/pull/59

## [0.2.1] - 2026-10-11

This release adds container images for the application and every inference service, and makes the
web page explain why a browser cannot hold a meeting or play the assistant's voice.

Upgrading from 0.2.0: nothing to change. Configuration, database and the native installation work
as before; the containers are an additional way to run the same system.

### Added

- Container images and a Docker Compose setup for Linux ([#49]): an application image and a speech
  synthesis image in CPU, CUDA and Vulkan variants, with `llama-server` from llama.cpp's own images
  at the pinned build. Each inference service runs in its own container and can be started, moved or
  replaced separately; model weights are mounted from the host and never built into an image.
  Override files add NVIDIA GPUs, Vulkan devices and, opt-in, the code sandbox through the host's
  Docker. The release workflow publishes the images and the sandbox image to GHCR, starting with
  this release (`ghcr.io/weizyyy/agentic-meeting`, `-tts` and `-sandbox`, tagged `0.2.1-cpu`,
  `0.2.1-cuda`, `0.2.1-vulkan` and so on). The images have not yet been run with real models, and
  the Vulkan variant is experimental. See `docs/containers.md`.
- The web page states the supported browsers (Chrome and Edge 111, Firefox 114, Safari 16.4) and
  explains why a browser cannot hold a meeting: too old to run the page, not opened over HTTPS, or
  missing WebRTC, microphone capture or Web Audio. Starting and resuming are blocked in such a
  browser, and past meetings stay readable ([#47]).
- When the browser's autoplay policy blocks the assistant's voice, the page says so and offers a
  button that turns it on, instead of staying silent ([#47]).

### Changed

- The browser end-to-end tests cover cancelling the screen-sharing picker, a screen that fails to
  open and stopping sharing from the browser's own control in Chromium, Firefox and WebKit ([#53]),
  and wait for the server to accept the connection before pushing captions, which removes an
  intermittent failure on slow runners ([#51]).

### Fixed

- Names from `session.members` now show as buttons next to the rename input and the new-speaker
  input, so they can be picked with one click. They used to be offered only as browser
  autocomplete, which hid them while the input still held the current name, and they never loaded
  at all in the first meeting when the page was opened before any meeting existed ([#48]).
- With several background tasks, the task panel no longer squeezes the assistant's answer down to a
  single line: the answer keeps at least four lines, the task panel shrinks first (to about one
  task in a 900-pixel-high window) and scrolls, and a window too short for everything scrolls the
  right column instead of overlapping panels ([#50]).

[#47]: https://github.com/weizyyy/Agentic-Meeting/pull/47
[#48]: https://github.com/weizyyy/Agentic-Meeting/pull/48
[#49]: https://github.com/weizyyy/Agentic-Meeting/pull/49
[#50]: https://github.com/weizyyy/Agentic-Meeting/pull/50
[#51]: https://github.com/weizyyy/Agentic-Meeting/pull/51
[#53]: https://github.com/weizyyy/Agentic-Meeting/pull/53

## [0.2.0] - 2026-10-10

This release makes it safe to let other people reach an instance: an access password, TURN and
deployment guidance, health endpoints and data retention.

Upgrading from 0.1.x: existing configuration files keep working, since every new key is optional and
the new features are off by default, except the transcription notice when a meeting starts
(`session.recording_notice`). The database gains a few columns on the first start; no manual
migration is needed.

Welcome to [@gad-en1nd], who made their first contributions in this release: the health, readiness
and metrics endpoints ([#33]), data retention and complete meeting deletion ([#38]), condition-based
waits in the tests ([#39]) and the browser end-to-end test suite ([#40], merged as [#43]). Thank
you!

### Added

- Optional access password for the web application (`server.password_env`,
  `server.auth_session_days`): a login page, a signed HTTP-only session cookie that lasts 7 days by
  default, logout, CSRF protection for state-changing requests including WebRTC signaling, and a
  limit of 5 failed logins per address in 5 minutes. Login requests larger than 16 KiB are refused
  without being read into memory. Every `/api` endpoint is covered; without a password nothing
  changes. `check` and `serve` warn when the server listens beyond the local machine without a
  password. `scripts/soak.py` logs in when a password is configured. ([#30], [#37])
- TURN servers with credentials in `server.ice_servers`: an entry can be a table with `urls`,
  `username` and `credential_env` (the TURN password stays in the environment). The browser now
  receives the same ICE servers from the new `GET /api/ice` endpoint; before, `server.ice_servers`
  applied to the server side only and the browser connected without any. `check` and `serve` warn
  when TURN is configured without an access password. ([#32])
- A deployment guide (`docs/deployment.md`): Caddy and nginx examples with the headers, upload size
  and streaming the application needs, when and how to use TURN with a coturn example that relays
  only to the meeting server, a checklist before exposing an instance, and troubleshooting. ([#36])
- Anonymous JSON `GET /healthz`, `/readyz` and `/metrics` endpoints for process liveness,
  transcription readiness and basic connection, caption-lag, queue, retained-task and HTTP-service
  gauges. Optional service failures allow degraded readiness; incomplete metrics preserve available
  observations. Responses exclude meeting content and credentials, while numeric gauges expose load.
  English and Chinese guidance explains how to request and interpret them. ([#33], by [@gad-en1nd])
- Optional retention periods (`[retention]`) for transcripts, screenshots, reports and digests, and
  finished task files, each set separately and disabled by default. A meeting marked Keep is exempt
  from automatic cleanup. Deleting a meeting removes its records and files completely and retries
  parts that failed, with a visible pending state. The page shows a fixed transcription indicator
  and a configurable notice when a meeting starts. A data governance guide in English and Chinese
  explains where data goes, which copies survive and what deletion cannot reach. ([#38], by
  [@gad-en1nd])

### Changed

- The automated tests are end-to-end. `uv run pytest` starts the real application with fake
  inference services and drives it over HTTP and WebRTC with a real speech recording; a Playwright
  suite runs the built web client in Chromium, Firefox and WebKit against the real server. Both run
  in CI on every pull request without a GPU, model weights or network access. The earlier unit tests
  were removed. ([#41]; browser suite [#43] and condition-based waits [#39] by [@gad-en1nd])
- A pinned repository-wide Prettier formatter for client code, JSON, YAML and Markdown, with shared
  configuration, `npm run format` / `npm run format:check`, pre-commit and CI checks. ([#31])
- CI runs only the jobs that the changed files can affect: Python tests for server code, tests and
  scripts, the client build for the web client, browser end-to-end tests for either side of the
  meeting page. A documentation-only change runs only the Prettier check. ([#45])
- `SECURITY.md` lists the supported versions and covers the anonymous health endpoints, TURN
  credentials, the deployment checklist and the limits of deletion.

### Fixed

- On a slow or busy machine, a meeting connection could stay stuck without the page ever receiving
  its session, and the first words after connecting could be held back until the assistant
  answered only the start of a question. Loading the voice activity and turn-detection models
  blocked the server for a few seconds while the connection was being set up, and their first
  inference delayed the start of the audio. Both now happen in a worker thread before the meeting
  starts. ([#46])
- `/readyz` no longer briefly reports `not_ready` with a storage `timeout` while service probes
  refresh. Each probe used to reload the CA certificates on the event loop, which on slower machines
  stalled it past the 0.5-second storage budget; the certificates are now loaded once per process.
  ([#44])
- A single `Ctrl+C` during a meeting now stops `agentic-meeting serve` (and then the inference
  services started with `--with-services`). Previously the server waited for the meeting to end,
  which only happened on a second `Ctrl+C`. ([#42])
- The web client no longer depends on `c.daily.co`. The Pipecat SDK's default media manager
  downloaded a script from that host and sent error reports to `sentry.io` when a meeting started,
  so browsers without access to it could not connect at all. The page now uses the SDK's
  `WavMediaManager`, which makes no requests of its own, and switches the connection to the new
  microphone track itself when the system default microphone changes. ([#35])

[#30]: https://github.com/weizyyy/Agentic-Meeting/pull/30
[#31]: https://github.com/weizyyy/Agentic-Meeting/pull/31
[#32]: https://github.com/weizyyy/Agentic-Meeting/pull/32
[#33]: https://github.com/weizyyy/Agentic-Meeting/pull/33
[#35]: https://github.com/weizyyy/Agentic-Meeting/pull/35
[#36]: https://github.com/weizyyy/Agentic-Meeting/pull/36
[#37]: https://github.com/weizyyy/Agentic-Meeting/pull/37
[#38]: https://github.com/weizyyy/Agentic-Meeting/pull/38
[#39]: https://github.com/weizyyy/Agentic-Meeting/pull/39
[#40]: https://github.com/weizyyy/Agentic-Meeting/pull/40
[#41]: https://github.com/weizyyy/Agentic-Meeting/pull/41
[#42]: https://github.com/weizyyy/Agentic-Meeting/pull/42
[#43]: https://github.com/weizyyy/Agentic-Meeting/pull/43
[#44]: https://github.com/weizyyy/Agentic-Meeting/pull/44
[#45]: https://github.com/weizyyy/Agentic-Meeting/pull/45
[#46]: https://github.com/weizyyy/Agentic-Meeting/pull/46
[@gad-en1nd]: https://github.com/gad-en1nd

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

[Unreleased]: https://github.com/weizyyy/Agentic-Meeting/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/weizyyy/Agentic-Meeting/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/weizyyy/Agentic-Meeting/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/weizyyy/Agentic-Meeting/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/weizyyy/Agentic-Meeting/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/weizyyy/Agentic-Meeting/releases/tag/v0.1.0
