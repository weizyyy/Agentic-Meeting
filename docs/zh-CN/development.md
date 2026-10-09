# 开发指南

[English](../development.md) · **简体中文**

本文说明参与代码开发时的约定与流程。贡献流程本身见 [CONTRIBUTING.zh-CN.md](../../CONTRIBUTING.zh-CN.md)。
供 AI 编程助手使用的精简版规则见 [AGENTS.md](../../AGENTS.md)（英文）。

## 阅读顺序

1. [architecture.md](architecture.md) —— 组成部分与数据流。
2. [interfaces.md](interfaces.md) —— 配置结构、数据库、算法、HTTP 接口、消息。
3. [pipecat-notes.md](pipecat-notes.md) 与 [agents-sdk-notes.md](agents-sdk-notes.md) —— 本项目对
   Pipecat 1.12 和 OpenAI Agents SDK 的用法，均已对照其源码核实。
4. [runtimes.md](runtimes.md) 与 [benchmarks.md](benchmarks.md) —— 运行时、模型与实测表现。

中英文文档的文件名和章节编号保持一致，代码注释中的「interfaces.md §3.2」这类引用在两种语言的文档中都能找到。

## 常用命令

```bash
uv sync --extra agent                 # 安装或更新 Python 依赖
uv run pytest                         # 测试；需要 GPU 或外部服务的用例默认跳过
uv run ruff check src tests scripts   # 静态检查
uv run ruff format src tests scripts  # 格式化
uv run agentic-meeting check          # 校验 config/config.toml

cd client
npm install
npm test                              # 客户端纯逻辑的单元测试
npm run build                         # 类型检查并构建
```

CI 在 Windows 和 Linux 上分别用 Python 3.12、3.13 和 3.14 运行 Python 测试，在 Linux 上运行客户端测试。
在本地用其他版本运行测试、同时不影响 `.venv`：

```bash
UV_PROJECT_ENVIRONMENT=.venv-3.14 uv run --python 3.14 --extra agent pytest
```

## 项目规则

1. **不下载模型权重。** 应用代码和脚本都不获取权重。`nemo-speech` 命令行程序在多种用法下会自动下载模型，
   本项目只使用它的动态库（runtimes.md §3.2）。
2. **源代码中不出现模型名。** `src/`、`client/src/` 和 `scripts/` 中没有模型名或权重文件名；
   测试使用虚构的名字（如 `fake-model`）。模型、接口地址、音色和采样参数来自 `config/config.toml`，
   与模型相关的提示词格式放在 `config/asr_profiles/` 和 `config/prompts/` 中。
   `tests/test_config.py` 中有一条测试保证这一点。
3. **Pipecat 接口以锁定的版本为准。** Pipecat 锁定在 1.12.0，其 1.x 接口与早期示例差别很大。
   先查阅 pipecat-notes.md；没有记载的，阅读 `.venv/` 下已安装的源码，并把确认的结论补充到该文档。
4. **锁定的版本单独升级。** `pyproject.toml` 中的依赖和 `runtimes.lock.toml` 中的运行时版本按
   runtimes.md §7 的步骤单独升级。`third_party/` 下的源码不做修改。
5. **接口先行。** 模块之间的数据格式以 interfaces.md 和 `src/agentic_meeting/types.py` 为准。
   先修改文档，再修改代码，并在提交说明中写明原因。
6. **不按实时模型的接入方式分支。** 两种方式的差别以 `cfg.realtime_llm` 的属性提供：
   `active`、`managed`、`request_extra_body()`、`cache_warm`、`supports_developer_role`
   （architecture.md §6.1）。
7. **转录不因故障中断。** 应答、任务、截图、存储中的错误不得使识别和字幕停止（architecture.md §9）。
   对外部服务的调用设有超时，失败时记录日志并降级，不把异常抛到管线顶层。
8. **密钥来自环境变量。** 配置中只保存变量名（`*_env` 字段）。密钥不出现在代码、模板、日志和数据库中。

## 代码风格

- 最低支持 Python 3.12，同时在 3.13 和 3.14 上测试，因此不要使用 3.12 之后才有的语法和标准库功能。
  全程使用 `asyncio`。阻塞调用（ctypes、大文件读写、图像解码）放入线程执行。
- 日志使用 `loguru`（与 Pipecat 一致）。`print` 仅用于命令行子命令和脚本。
- 类型标注完整。公共函数和类编写文档字符串，说明做什么以及为什么。
- 注释、文档字符串、界面文案和提示词使用中文，标识符使用英文。
- 配置只从 `AppConfig` 读取，模块内不另设默认模型或默认地址。
- 新增配置项时同步修改 `config.py`、`config/config.example.toml`、interfaces.md §1 和
  [configuration.md](configuration.md)，并补充测试。
- 新依赖用 `uv add` 添加，并在提交说明中说明用途。

## 测试

自动化测试不需要 GPU、模型权重或网络。

- **外部依赖通过参数注入。** 识别后端、说话人区分、模型服务和任务运行器都作为参数传入，
  测试中以假实现替代（`tests/fakes.py`）。
- HTTP 调用由 `httpx.MockTransport` 应答。
- Pipecat 处理器使用其自带的测试工具：

  ```python
  from pipecat.tests.utils import SleepFrame, run_test

  down, up = await run_test(
      processor,
      frames_to_send=[frame_a, SleepFrame(sleep=0.1), frame_b],
      expected_down_frames=[TypeA, TypeB],
  )
  ```

- 需要真实权重、GPU 或外部服务的测试标记为 `@pytest.mark.gpu`，默认不运行：

  ```bash
  AGENTIC_MEETING_TEST_WAV=dialogue.wav uv run pytest -m gpu tests/test_diar_nemo.py
  uv run pytest -m gpu tests/test_agent_runner.py -s
  ```

端到端的验证使用 [runtimes.md §6](runtimes.md) 中介绍的脚本：`scripts/soak.py` 把一段录音送入运行中的服务，
`scripts/eval_realtime_model.py` 检查当前实时模型的工具选择与延迟。修改
`config/prompts/realtime_system.md` 或更换模型之后，请运行后者。

## 目录结构

见 [architecture.md §8](architecture.md#8-目录结构)。
