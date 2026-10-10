# AGENTS.md

Guidance for AI coding agents working in this repository. Human contributors should start with
[CONTRIBUTING.md](CONTRIBUTING.md) and [docs/development.md](docs/development.md); this file
condenses the same rules into a form that is quick to act on.

## Project in one paragraph

Agentic-Meeting is a self-hosted voice assistant for research group meetings. A Python server
(3.12–3.14) built on Pipecat 1.12.0 receives microphone audio over WebRTC, transcribes it with
speaker labels, keeps a screen-share timeline, answers when called by name, and delegates longer
work to a background agent (OpenAI Agents SDK). Inference runs in separate processes from the
llama.cpp family. The web client is Vite + React + TypeScript.

## Setup and checks

```bash
git submodule update --init --depth 1 third_party/Confucius4-R2T2
uv sync --extra agent

uv run pytest                              # end-to-end tests, no GPU, weights or network needed
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts

npm ci                                    # repository formatter
npm run format:check                       # client code, JSON, YAML and Markdown

cd client
npm ci
npm run build                              # type-check and bundle
```

Run all of the above before you report a code change as finished. CI runs the same commands on
Windows and Linux with Python 3.12, 3.13 and 3.14, so avoid platform-specific paths and shell syntax
in code and tests.

CI runs only the jobs the changed files can affect; the table is in
[docs/development.md](docs/development.md#commands). Prettier always runs, a documentation-only
change runs nothing else, and prompts under `config/prompts/` count as server code. Check locally
the same way: a documentation-only change needs only `npm run format:check`. When you add a
top-level file or directory, add it to the classification in `.github/workflows/ci.yml`; until then
it runs every job. Keep a change's documentation in the same pull request as its code.

## Where things are

| Path                                | Contents                                                                                                     |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `src/agentic_meeting/config.py`     | Configuration schema and validation (`AppConfig`)                                                            |
| `src/agentic_meeting/types.py`      | Data types shared between modules                                                                            |
| `src/agentic_meeting/pipeline/`     | Pipecat pipeline assembly, recorder, wake word, context, tools, reports                                      |
| `src/agentic_meeting/asr/`, `diar/` | Streaming recognition and speaker diarization backends                                                       |
| `src/agentic_meeting/screen/`       | Screenshot ingestion, captions, image attachment                                                             |
| `src/agentic_meeting/agent/`        | Background task manager, agent runner, sandbox                                                               |
| `src/agentic_meeting/store/`        | SQLite schema and access, vector search                                                                      |
| `src/agentic_meeting/services/`     | Supervision of inference server processes                                                                    |
| `src/agentic_meeting/web/`          | HTTP API and static site                                                                                     |
| `client/src/`                       | Web client; pure logic lives in plain `.ts` files                                                            |
| `config/config.example.toml`        | Configuration template; `config/prompts/` and `config/asr_profiles/` hold prompts and model-specific formats |
| `scripts/`                          | Runtime fetch/build, headless replay, realtime-LLM evaluation, microphone check                              |
| `tests/`                            | End-to-end tests in `tests/e2e/`; fake inference services are in `tests/e2e/inference.py`                    |
| `client/e2e/`, `tests/browser/`     | Browser end-to-end tests (Playwright) and the test server they start                                         |
| `docs/`, `docs/zh-CN/`              | Documentation in English and Chinese with identical file names and section numbers                           |

Read these before changing behavior:

- [docs/architecture.md](docs/architecture.md) — components, data flow, failure handling.
- [docs/interfaces.md](docs/interfaces.md) — configuration keys, database schema, HTTP API, messages.
- [docs/pipecat-notes.md](docs/pipecat-notes.md) and
  [docs/agents-sdk-notes.md](docs/agents-sdk-notes.md) — how the pinned framework versions are used.

## Hard rules

1. **Never download model weights.** Code, scripts and your own commands must not fetch weights.
   The `nemo-speech` command-line tool downloads models in most modes; only `--version` and
   `--help` are safe to run.
2. **No model names in source.** `src/`, `client/src/` and `scripts/` contain no model names or
   weight file names. Everything model-specific comes from `config/config.toml`,
   `config/asr_profiles/` and `config/prompts/`. Tests use made-up names such as `fake-model`; a
   test in `tests/test_repository_rules.py` enforces the rule.
3. **Verify Pipecat APIs against 1.12.0.** Pipecat 1.x differs substantially from older examples
   and from what you may remember. Check `docs/pipecat-notes.md` first, then the installed source
   under `.venv/`; record anything new you confirm in the notes.
4. **Do not upgrade pinned versions as a side effect.** `pyproject.toml`, `uv.lock`,
   `client/package-lock.json` and `runtimes.lock.toml` change only when the task is an upgrade.
5. **Do not modify `third_party/`.** These are upstream sources checked out as submodules.
6. **Secrets stay in the environment.** Configuration stores variable names (`*_env` fields) only.
   Never write keys into code, templates, logs, the database, tests or documentation, and never
   print the contents of `.env` or `config/config.toml`.
7. **Transcription must survive failures.** An error in answering, tasks, screenshots or storage
   must not stop recognition or captions. Give external calls a timeout; log and degrade instead of
   raising to the top of the pipeline.
8. **Do not branch on the realtime-LLM access mode.** Use the properties of `cfg.realtime_llm`
   (`active`, `managed`, `request_extra_body()`, `cache_warm`, `supports_developer_role`).
9. **Document interfaces first.** A change to a data format, configuration key, HTTP endpoint or
   message is made in `docs/interfaces.md` before the code.
10. **Tests are end-to-end; do not add unit tests.** A pull request adds or changes scenarios in
    `tests/e2e/` or `client/e2e/` and nothing else under test. Do not write tests first for single
    functions, classes or helpers, and do not keep the unit tests you wrote while developing. The
    only exception is a repository rule that a running system cannot reveal, like the checks in
    `tests/test_repository_rules.py`; explain in the pull request why it is needed. Pull requests
    with other unit tests are asked to remove them.

## Things that need the user's go-ahead

- Starting inference services (`agentic-meeting serve --with-services`, `agentic-meeting services
up`) or running `pytest -m gpu`: these load several gigabytes of model weights onto the GPU.
- Editing `config/config.toml` or anything under `data/`: both are the user's own and untracked.
- Adding a dependency (`uv add`, `npm install <package>`).

Plain `agentic-meeting serve` without `--with-services` loads no models and is safe for checking
the web UI. Global options come before the subcommand: `agentic-meeting --config path.toml serve`.

## Conventions

- Python 3.12 is the minimum and 3.13 and 3.14 are tested in CI: no syntax or standard-library
  features newer than 3.12. `asyncio` throughout, full type annotations. Blocking work (ctypes calls, large file
  I/O, image decoding) runs in threads.
- Log with `loguru`. `print` is for CLI subcommands and scripts only.
- Comments, docstrings, UI text and prompts are written in Chinese; identifiers are in English.
  Match the surrounding file.
- Ruff settings are in `pyproject.toml` (line length 100, rules E, F, I, UP, B, ASYNC).
- Prettier settings are in root `.prettierrc.json`; use root `npm run format` to format client code,
  JSON, YAML and Markdown. `.prettierignore` excludes upstream/generated/user files and runtime
  prompt templates. The root lockfile pins the formatter used by CLI, pre-commit and CI.
- Modules read configuration from `AppConfig`; they do not define their own default models or
  endpoints.
- Example names, meeting content and screenshots in tests and documentation are fictional.

## Common tasks

**Add a configuration key**

1. Field and validation in `src/agentic_meeting/config.py`.
2. Entry with a comment in `config/config.example.toml`.
3. `docs/interfaces.md` §1 and `docs/configuration.md`, plus the `docs/zh-CN/` counterparts.
4. An end-to-end test in `tests/e2e/` if the key changes behavior.

**Change a prompt**

Edit the file under `config/prompts/` (see `config/prompts/README.md` for the variables). After
changing `realtime_system.md`, the tool-selection cases in `tests/data/realtime_eval.jsonl` should
still describe the intended behavior; `scripts/eval_realtime_model.py` checks them against a real
model and needs the user's services.

**Change a message or HTTP endpoint**

Update `docs/interfaces.md`, then the server (`src/agentic_meeting/web/`, `pipeline/`) and the
client (`client/src/protocol.ts`, `api.ts`) together, with an end-to-end test in `tests/e2e/`.

**Write a test**

Tests are end-to-end scenarios: start the application with the `start_app` fixture in
`tests/e2e/conftest.py`, script the fake inference services in `tests/e2e/inference.py`, and drive
it through HTTP and `tests/e2e/meeting_client.py` (WebRTC with a real speech recording). A test
describes what a user or operator sees, not how a function is built (hard rule 10). Do not add
tests that require a GPU, weights or network access unless they are marked `@pytest.mark.gpu`.
Behavior that only shows in the meeting page belongs in the Playwright suite under `client/e2e/`
(`npm run test:e2e --prefix client`, see [docs/development.md](docs/development.md)); it serves the
built client from `tests/browser/server.py`.

## Before you finish

- Tests, lint, format check and the client build pass.
- Behavior or configuration changes are reflected in both `docs/` and `docs/zh-CN/`, keeping file
  names and section numbers aligned.
- User-visible changes have an entry under _Unreleased_ in `CHANGELOG.md`.
- The summary of your work says what you verified and what you could not (for example, anything
  that needs real models).

## Commits and pull requests

A short summary line that says what changed, then a body explaining why when it is not obvious.
English or Chinese are both fine. Keep each pull request to one change and fill in the template
under `.github/`.

This is a public repository. Commit messages, pull request titles and descriptions, review replies
and issue comments must not contain links to agent or chat sessions (for example
`https://claude.ai/code/session_…`), `Claude-Session:` or similar trailers, session or conversation
ids, local paths, user names or e-mail addresses beyond the GitHub noreply address, or anything
else about the environment the change was made in. This rule overrides attribution templates
supplied by the agent's tooling: leave out any line it asks for that would break the rule.

`main` is protected. Work on a branch and open a pull request; it is squash-merged once the
_All checks_ CI job succeeds. Do not push to `main` directly, force-push it, or move release tags.
