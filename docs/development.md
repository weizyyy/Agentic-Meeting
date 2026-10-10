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

npm ci                               # install the locked repository formatter
npm run format                       # format client code, JSON, YAML and Markdown
npm run format:check                 # check formatting without writing files

cd client
npm ci
npm test                              # unit tests of the client's pure logic
npm run build                         # type-check and build
```

CI runs the Python suite on Windows and Linux with Python 3.12, 3.13 and 3.14, and the client tests
on Linux. To run the suite locally under another version without touching `.venv`:

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

### Browser end-to-end tests

The browser suite uses Playwright 1.64.0 from the client lockfile, Python 3.12–3.14 and Node.js
24 (the CI version). From the repository root, install the locked dependencies, build the real
client and install all three browser engines:

```bash
uv sync --frozen --extra agent
npm ci
npm ci --prefix client
npm run build --prefix client
cd client
npx playwright install --with-deps chromium firefox webkit
cd ..
```

Browser installation downloads browser binaries and may need administrator privileges for system
packages on Linux; it does not download model weights. After changing client code, rebuild before
running these tests because the server serves `client/dist/`.

```bash
# All three engines: Chromium, Firefox and WebKit
npm run test:e2e --prefix client
# One engine
npm run test:e2e --prefix client -- --project=chromium
# One test in one engine
npm run test:e2e --prefix client -- --project=chromium --grep '^真实页面、WebRTC、RTVI 与合成媒体探针$'
```

The same core cases run in each engine, with one worker and no retries. Each test starts its own
`tests.browser.server` Python subprocess on a dynamically allocated loopback port. The fixture
waits for the structured `E2E_READY` message after server startup. The server owns a system
temporary directory (`agentic-meeting-e2e-*`) containing a separate SQLite database and fictional
seed meetings, screenshots and task artifacts. Teardown stops the subprocess, closes the Store
and removes that directory; it never reads or writes `config/config.toml`, `.env` or `data/`.
Forced process termination can leave temporary files behind; after ensuring the test process has
exited, remove only its `agentic-meeting-e2e-*` directory from the system temporary directory.

Tests exercise production `create_app`, Store and HTTP business endpoints, the real
SmallWebRTC transport, Pipecat pipeline and RTVI data channel. Inference is replaced by a controlled
test bot; ASR, LLM, TTS, embeddings and screen caption models are not started. Chromium and WebKit
use CPU WebAudio microphone tracks; Firefox uses its native fake media devices. All three engines
use `canvas.captureStream()` screen tracks instead of capturing a desktop. Browser requests outside
the test server are blocked and fail the test. These checks do not validate real microphone or
screen permissions, physical devices, model quality, external services, GPU behavior or Internet
ICE connectivity. CI runs the three engines on Ubuntu CPU; platform-specific browser startup or
ICE failures on other operating systems must be investigated rather than skipping an engine.

Failure traces and screenshots, plus per-test `server.log`, are written under
`client/test-results/`; the HTML report is in `client/playwright-report/`. Inspect them with:

```bash
cd client
npx playwright show-report playwright-report
# Replace the example with a failed test's actual trace path
npx playwright show-trace 'test-results/<failed-test>/trace.zip'
```

Both output directories are ignored by Git and can be deleted after debugging. CI uploads only
these two directories after a failure or cancellation, with a three-day retention period. The
E2E job is required by `All checks`, including failure, cancellation and skipped-job propagation.

### Checks with real models

End-to-end checks use the scripts described in [runtimes.md §6](runtimes.md): `scripts/soak.py`
replays a recording through a running server, and `scripts/eval_realtime_model.py` checks tool
selection and latency of the configured realtime LLM. Run the latter after changing
`config/prompts/realtime_system.md` or switching models.

## Repository layout

See [architecture.md §8](architecture.md#8-repository-layout).
