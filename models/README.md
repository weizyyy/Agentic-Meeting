# models/

Place model weights here, in any subdirectory, and reference them in `config/config.toml`.

- Everything in this directory except this file is ignored by Git.
- Nothing in the project downloads weights.
- The required files and formats are listed in [docs/runtimes.md §4](../docs/runtimes.md#4-model-files).
- Run `uv run agentic-meeting check` to confirm that every path resolves.

---

模型权重放在此目录下（可使用任意子目录），并在 `config/config.toml` 中填写路径。

- 除本文件外，此目录下的内容均被 Git 忽略。
- 本项目不会下载任何权重。
- 需要的文件及其格式见 [docs/zh-CN/runtimes.md §4](../docs/zh-CN/runtimes.md#4-模型文件)。
- 运行 `uv run agentic-meeting check` 可确认各路径是否有效。
