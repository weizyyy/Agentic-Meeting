# 贡献指南

[English](CONTRIBUTING.md) · **简体中文**

感谢关注本项目。欢迎提交问题报告、文档修订和代码贡献。议题和合并请求可以使用中文或英文。

## 报告问题与提出需求

- 提交之前请先搜索[已有的议题](https://github.com/weizyyy/Agentic-Meeting/issues)。
- 请使用议题模板。报告问题时请提供版本或提交号、操作系统与显卡、实时模型的接入方式，以及相关的日志。
- 粘贴日志前请去掉 API 密钥、内部地址和会议内容。
- 安全问题请私下报告，见 [SECURITY.md](SECURITY.md)。

## 开发环境

```bash
git clone https://github.com/weizyyy/Agentic-Meeting.git
cd Agentic-Meeting
git submodule update --init --depth 1

uv sync --extra agent
cd client && npm install && cd ..
```

自动化测试不需要 GPU、模型权重或网络：

```bash
uv run pytest
uv run ruff check src tests scripts
uv run ruff format src tests scripts
cd client && npm test && npm run build
```

目录结构、代码约定，以及涉及真实模型的改动如何测试，见 [docs/zh-CN/development.md](docs/zh-CN/development.md)。

## 合并请求

1. 小修小补以外的改动，请先开一个议题讨论方案。
2. 从 `main` 创建分支，每个合并请求只做一件事。
3. 补充或更新测试。外部服务均通过参数注入，测试中以假实现替代。
4. 行为或配置发生变化时，同时更新 `docs/` 与 `docs/zh-CN/` 下的文档，并在 `CHANGELOG.md` 的
   *Unreleased* 一节中添加条目。
5. 确认 CI 通过。

以下规则由测试或评审把关：

- 源代码中不出现模型名或权重文件名。与模型相关的内容全部来自 `config/config.toml`、
  `config/asr_profiles/` 和 `config/prompts/`。
- 代码和脚本不下载模型权重。
- `pyproject.toml` 中的依赖和 `runtimes.lock.toml` 中的运行时版本是锁定的，升级需单独进行，
  不随其他改动顺带完成。
- `third_party/` 下是上游源码，不做修改。
- 密钥只通过环境变量名引用。

## 提交说明

第一行用一句话概括改动内容；原因不明显时，在正文中说明。中文或英文均可。

## 许可

提交贡献即表示同意以 [MIT 许可](LICENSE)发布所贡献的内容。
