# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Bilingual deployment guide with Caddy/nginx HTTPS termination, trusted forwarded headers,
  signaling/download timeouts, coturn and ICE configuration, exposure checks and troubleshooting.

- Optional access password for the web application (`server.password_env`,
  `server.auth_session_days`): a login page, a signed HTTP-only session cookie that lasts 7 days by
  default, logout, CSRF protection for state-changing requests including WebRTC signaling, and a
  limit of 5 failed logins per address in 5 minutes. Every `/api` endpoint is covered; without a
  password nothing changes. `check` and `serve` warn when the server listens beyond the local
  machine without a password. `scripts/soak.py` logs in when a password is configured.
- TURN servers with credentials in `server.ice_servers`: an entry can be a table with `urls`,
  `username` and `credential_env` (the TURN password stays in the environment). The browser now
  receives the same ICE servers from the new `GET /api/ice` endpoint; before, `server.ice_servers`
  applied to the server side only and the browser connected without any. `check` and `serve` warn
  when TURN is configured without an access password.
- A pinned repository-wide Prettier formatter for client code, JSON, YAML and Markdown, with
  shared configuration, `npm run format` / `npm run format:check`, pre-commit and CI checks.

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
