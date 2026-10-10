## Summary

<!-- What does this change do, and why? Link the related issue if there is one. -->

## How was it tested?

<!-- Commands you ran, hardware or models involved, anything a reviewer should repeat. -->

## Checklist

- [ ] `uv run pytest` and `uv run ruff check src tests scripts` pass
- [ ] `uv run ruff format --check src tests scripts` and root `npm run format:check` pass
- [ ] `npm run build` passes in `client/` (if the web client changed)
- [ ] Documentation is updated in both `docs/` and `docs/zh-CN/` (if behavior or configuration changed)
- [ ] `CHANGELOG.md` has an entry under _Unreleased_ (for user-visible changes)
