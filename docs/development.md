# Development

**English** · [简体中文](zh-CN/development.md)

Conventions and workflows for working on the code base. For the contribution process itself, see
[CONTRIBUTING.md](../CONTRIBUTING.md). AI coding agents will find a condensed version of these rules
in [AGENTS.md](../AGENTS.md).

## Where to start reading

1. [architecture.md](architecture.md) — components and data flow.
2. [interfaces.md](interfaces.md) — configuration schema, database, algorithms, HTTP API, messages.
3. [pipecat-notes.md](pipecat-notes.md) and [agents-sdk-notes.md](agents-sdk-notes.md) — how the
   project uses Pipecat 1.12 and the OpenAI Agents SDK, checked against their sources.
4. [runtimes.md](runtimes.md) and [benchmarks.md](benchmarks.md) — runtimes, models and measured
   behavior.

The English and Chinese documents share file names and section numbers, so a reference such as
"interfaces.md §3.2" in a code comment resolves in either language.

## Commands

```bash
uv sync --extra agent                 # install or update Python dependencies
uv run pytest                         # tests; those needing a GPU or external services are skipped
uv run ruff check src tests scripts   # lint
uv run ruff format src tests scripts  # format
uv run agentic-meeting check          # validate config/config.toml

cd client
npm install
npm test                              # unit tests of the client's pure logic
npm run build                         # type-check and build
```

CI runs the Python suite on Windows and Linux and the client tests on Linux.

## Project rules

1. **No model weights are downloaded.** Neither application code nor scripts fetch weights. The
   `nemo-speech` command-line tool downloads models in many of its modes; the project uses only its
   shared library (runtimes.md §3.2).
2. **No model names in source code.** `src/`, `client/src/` and `scripts/` contain no model names
   or weight file names; tests use made-up names such as `fake-model`. Models, endpoints, voices and
   sampling parameters come from `config/config.toml`; model-specific prompt formats live in
   `config/asr_profiles/` and `config/prompts/`. A test in `tests/test_config.py` enforces this.
3. **Pipecat APIs are verified against the pinned version.** Pipecat is pinned to 1.12.0, and its
   1.x APIs differ substantially from older examples. Check pipecat-notes.md first; otherwise read
   the installed source under `.venv/`, and add what you confirm to the notes.
4. **Pinned versions change deliberately.** Dependencies in `pyproject.toml` and runtime versions in
   `runtimes.lock.toml` are upgraded on their own, following runtimes.md §7. Sources under
   `third_party/` are not modified.
5. **Interfaces are documented first.** Data formats between modules follow interfaces.md and
   `src/agentic_meeting/types.py`. Change the document before the code and explain the reason in the
   commit message.
6. **No branching on the realtime LLM access mode.** The differences between the two modes are
   exposed as properties of `cfg.realtime_llm` — `active`, `managed`, `request_extra_body()`,
   `cache_warm`, `supports_developer_role` (architecture.md §6.1).
7. **Transcription survives failures.** An error in answering, tasks, screenshots or storage must
   not stop recognition or captions (architecture.md §9). Calls to external services have timeouts,
   and failures are logged and degraded rather than raised to the top of the pipeline.
8. **Secrets come from environment variables.** Configuration holds variable names (`*_env` fields)
   only. Keys never appear in code, templates, logs or the database.

## Code style

- Python 3.12 and `asyncio` throughout. Blocking calls — ctypes, large file I/O, image decoding —
  run in threads.
- Logging uses `loguru`, as Pipecat does. `print` is reserved for CLI subcommands and scripts.
- Full type annotations. Public functions and classes have docstrings that explain what and why.
- Comments, docstrings, UI text and prompts are written in Chinese; identifiers are in English.
- Configuration is read from `AppConfig` only. Modules do not define their own default models or
  endpoints.
- A new configuration key is added in `config.py`, `config/config.example.toml`, interfaces.md §1
  and [configuration.md](configuration.md), with a test.
- New dependencies are added with `uv add` and justified in the commit message.

## Testing

Automated tests run without a GPU, model weights or network access.

- **External dependencies are injected.** ASR backends, diarization, model services and the task
  runner are passed in as parameters and replaced with fakes (`tests/fakes.py`).
- HTTP calls are answered by `httpx.MockTransport`.
- Pipecat processors are exercised with Pipecat's own test helper:

  ```python
  from pipecat.tests.utils import SleepFrame, run_test

  down, up = await run_test(
      processor,
      frames_to_send=[frame_a, SleepFrame(sleep=0.1), frame_b],
      expected_down_frames=[TypeA, TypeB],
  )
  ```

- Tests that need real weights, a GPU or external services are marked `@pytest.mark.gpu` and are
  deselected by default:

  ```bash
  AGENTIC_MEETING_TEST_WAV=dialogue.wav uv run pytest -m gpu tests/test_diar_nemo.py
  uv run pytest -m gpu tests/test_agent_runner.py -s
  ```

End-to-end checks use the scripts described in [runtimes.md §6](runtimes.md): `scripts/soak.py`
replays a recording through a running server, and `scripts/eval_realtime_model.py` checks tool
selection and latency of the configured realtime LLM. Run the latter after changing
`config/prompts/realtime_system.md` or switching models.

## Repository layout

See [architecture.md §8](architecture.md#8-repository-layout).
