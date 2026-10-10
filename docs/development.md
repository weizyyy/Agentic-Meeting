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
uv run pytest                         # end-to-end tests; those needing a GPU or real models are skipped
uv run ruff check src tests scripts   # lint
uv run ruff format src tests scripts  # format
uv run agentic-meeting check          # validate config/config.toml

npm ci                               # install the locked repository formatter
npm run format                       # format client code, JSON, YAML and Markdown
npm run format:check                 # check formatting without writing files

cd client
npm ci
npm run build                         # type-check and build
```

CI runs the Python suite on Windows and Linux with Python 3.12, 3.13 and 3.14, and type-checks and
builds the client on Linux. To run the suite locally under another version without touching `.venv`:

```bash
UV_PROJECT_ENVIRONMENT=.venv-3.14 uv run --python 3.14 --extra agent pytest
```

## Project rules

1. **No model weights are downloaded.** Neither application code nor scripts fetch weights. The
   `nemo-speech` command-line tool downloads models in many of its modes; the project uses only its
   shared library (runtimes.md §3.2).
2. **No model names in source code.** `src/`, `client/src/` and `scripts/` contain no model names
   or weight file names; tests use made-up names such as `fake-model`. Models, endpoints, voices and
   sampling parameters come from `config/config.toml`; model-specific prompt formats live in
   `config/asr_profiles/` and `config/prompts/`. A test in `tests/test_repository_rules.py` enforces
   this.
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

- Root `.prettierrc.json` defines the shared style for TypeScript/TSX, CSS, HTML, JSON, YAML and
  Markdown: two-space indentation, double quotes, semicolons, trailing commas, a 100-column target
  and LF line endings. Markdown prose keeps its existing wrapping, and fenced examples are not
  reformatted. Python continues to use Ruff.
- Root `.prettierignore` excludes upstream sources, runtime/model/data directories, secrets,
  generated files and runtime prompt templates under `config/prompts/` (the README is included).
  CLI commands, editors using the project's local Prettier, pre-commit and CI share this config.
  Run `npm ci` at the root before installing hooks with `uvx pre-commit install`.
- Python 3.12 is the minimum version; 3.13 and 3.14 are tested as well, so avoid syntax and
  standard-library features newer than 3.12. `asyncio` is used throughout. Blocking calls — ctypes, large file I/O, image decoding —
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

The test suite is end-to-end. It runs without a GPU, model weights or network access.

- **`tests/e2e/`** starts the real application with `agentic-meeting --config <temporary file> serve`,
  exactly as a user does, with its data directory in a temporary folder. The inference services are
  replaced by small HTTP servers in the test process (`tests/e2e/inference.py`) that speak the same
  protocols: llama-server audio input and `/tokenize` for ASR, OpenAI-compatible chat completions
  for the realtime and agent models, `/v1/audio/speech` and `/v1/embeddings`. Each can be switched
  off to test degradation. Tests talk to the application only through HTTP and WebRTC:
  `tests/e2e/meeting_client.py` plays the meeting page with aiortc, sends a real speech recording
  over the microphone track (from the `third_party/Confucius4-R2T2` submodule) and speaks RTVI on
  the data channel.
- **`tests/test_repository_rules.py`** guards rules that a running system would not reveal at once:
  no model names in source, the ASR profile matching the upstream chat template, and the
  evaluation cases of `scripts/eval_realtime_model.py` covering exactly the realtime tools.
- **`tests/test_real_services.py`** checks real models and services from `config/config.toml`.
  These tests are marked `@pytest.mark.gpu` and are deselected by default:

  ```bash
  AGENTIC_MEETING_TEST_WAV=dialogue.wav uv run pytest -m gpu tests/test_real_services.py
  ```

Write a new test as a scenario a user would recognize: what they do in the meeting page or on the
command line, and what they then see through the API, the data channel or the files. Avoid tests of
single functions or classes; they bind the code's current shape without proving that the system
works. The web client has no tests of its own yet: `npm run build` type-checks it, and the server
side of every message it exchanges is covered by `tests/e2e/`.

Other end-to-end checks use the scripts described in [runtimes.md §6](runtimes.md):
`scripts/soak.py` replays a recording through a running server, and `scripts/eval_realtime_model.py`
checks tool selection and latency of the configured realtime LLM. Run the latter after changing
`config/prompts/realtime_system.md` or switching models.

## Repository layout

See [architecture.md §8](architecture.md#8-repository-layout).
