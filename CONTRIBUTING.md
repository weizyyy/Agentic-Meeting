# Contributing to Agentic-Meeting

**English** · [简体中文](CONTRIBUTING.zh-CN.md)

Thank you for your interest in the project. Bug reports, documentation fixes and code contributions
are all welcome. Issues and pull requests may be written in English or Chinese.

## Reporting bugs and requesting features

- Search the [existing issues](https://github.com/weizyyy/Agentic-Meeting/issues) first, and check
  the [roadmap](ROADMAP.md) for work that is already planned.
- Questions about setup or usage, and early ideas, belong in [Discussions](https://github.com/weizyyy/Agentic-Meeting/discussions).
- Use the issue templates. For bugs, include the version or commit, the operating system and GPU,
  the realtime LLM access mode, and the relevant log lines.
- Remove API keys, internal addresses and meeting content before posting logs.
- Report security problems privately; see [SECURITY.md](SECURITY.md).

## Development setup

```bash
git clone https://github.com/weizyyy/Agentic-Meeting.git
cd Agentic-Meeting
git submodule update --init --depth 1

uv sync --extra agent
npm ci
cd client && npm ci && cd ..
```

The automated tests are end-to-end and need no GPU, model weights or network access:

```bash
uv run pytest
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
npm run format:check
cd client && npm run build
```

To run the lint and format checks automatically before each commit, install the
[pre-commit](https://pre-commit.com/) hooks once with `uvx pre-commit install`.
Run `npm ci` at the repository root first so the hook uses the locked Prettier version.
Use root `npm run format` to format TypeScript/TSX, CSS, HTML, JSON, YAML and Markdown;
Python is formatted separately with `uv run ruff format src tests scripts`.

[docs/development.md](docs/development.md) describes the project layout, the coding conventions and
how to test changes that involve real models.

## Pull requests

1. Open an issue first for anything larger than a small fix, so the approach can be agreed on.
2. Create a branch from `main` and keep each pull request focused on one change.
3. Add or update tests. External services are injected and replaced with fakes in tests.
4. Update the documentation in both `docs/` and `docs/zh-CN/` when behavior or configuration
   changes, and add an entry under _Unreleased_ in `CHANGELOG.md`.
5. Make sure CI passes. `main` is protected: changes land through pull requests, which are
   squash-merged once the _All checks_ job succeeds.

A few project rules are enforced by tests or review:

- Source code contains no model names or weight file names. Everything model-specific comes from
  `config/config.toml`, `config/asr_profiles/` and `config/prompts/`.
- Code and scripts never download model weights.
- Dependencies in `pyproject.toml` and runtime versions in `runtimes.lock.toml` are pinned and are
  upgraded deliberately, not as a side effect of another change.
- Files under `third_party/` are upstream sources and are not modified.
- Secrets are referenced by environment variable name only.

## Commit messages

Write a short summary line that says what changed, followed by a body explaining why when it is not
obvious. Either English or Chinese is fine.

The repository is public. Keep commit messages and pull request text free of links to AI tool
sessions, session ids, local paths and personal contact details.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
