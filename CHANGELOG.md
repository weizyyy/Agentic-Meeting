# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Support for Python 3.13 and 3.14. The locked versions of existing dependencies are unchanged;
  `audioop-lts` is added for these interpreters. CI tests all three versions on Windows and Linux.

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

[Unreleased]: https://github.com/weizyyy/Agentic-Meeting/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/weizyyy/Agentic-Meeting/releases/tag/v0.1.0
