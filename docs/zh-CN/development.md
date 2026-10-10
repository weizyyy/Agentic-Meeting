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
uv run pytest                         # 端到端测试；需要 GPU 或真实模型的用例默认跳过
uv run ruff check src tests scripts   # 静态检查
uv run ruff format src tests scripts  # 格式化
uv run agentic-meeting check          # 校验 config/config.toml

npm ci                               # 安装锁定版本的仓库格式化工具
npm run format                       # 格式化客户端代码、JSON、YAML 和 Markdown
npm run format:check                 # 只检查格式，不写入文件

cd client
npm ci
npm run build                         # 类型检查并构建
```

CI 在 Windows 和 Linux 上分别用 Python 3.12、3.13 和 3.14 运行 Python 测试，在 Linux 上对客户端做类型检查并构建，
并构建容器镜像、在里面把各个程序启动一次。
并不是每次改动都跑全部任务：_Classify changes_ 任务读取改动的文件，只启动可能受影响的任务。
Prettier 检查总是运行。

| 改动的文件                                                                        | Python（6 个任务） | Web client | Browser end-to-end | Container images |
| --------------------------------------------------------------------------------- | ------------------ | ---------- | ------------------ | ---------------- |
| `src/`、`config/`（含提示词）、`.python-version`                                  | 跑                 |            | 跑                 |                  |
| `pyproject.toml`、`uv.lock`                                                       | 跑                 |            | 跑                 | 跑               |
| `tests/browser/`                                                                  | 跑                 |            | 跑                 |                  |
| `docker/`、`compose.yaml`                                                         | 跑                 |            |                    | 跑               |
| 子模块、`runtimes.lock.toml`、`scripts/runtimes.py`、`scripts/container.py`       | 跑                 |            |                    | 跑               |
| `tests/` 和 `scripts/` 下其他文件                                                 | 跑                 |            |                    |                  |
| `client/e2e/`、`client/playwright.config.ts`                                      |                    |            | 跑                 |                  |
| `client/` 下其他文件                                                              |                    | 跑         | 跑                 |                  |
| 其他 Markdown 文件、`docs/`、`LICENSE`、issue 模板、格式化工具设置                |                    |            |                    |                  |
| `.github/dependabot.yml`、`.pre-commit-config.yaml`、`.gitignore`、`.env.example` |                    |            |                    |                  |
| 其他文件，包括工作流文件                                                          | 跑                 | 跑         | 跑                 | 跑               |

只有分类结果表明不需要时，_All checks_ 才把跳过的任务算作通过。新增的顶层文件或目录在加入
`.github/workflows/ci.yml` 的分类之前会跑全部任务。

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
   `tests/test_repository_rules.py` 中有一条测试保证这一点。
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
9. **测试是端到端的。** 新测试写成 `tests/e2e/` 或 `client/e2e/` 中的场景（见[测试](#测试)）。
   不添加针对单个函数或类的单元测试，包括开发时“先写测试再写代码”留下的测试；唯一的例外是运行中的系统发现不了的仓库规则检查，
   并在 PR 说明里写明理由。

## 代码风格

- 根目录 `.prettierrc.json` 统一 TypeScript/TSX、CSS、HTML、JSON、YAML 和 Markdown 的格式：
  两空格缩进、双引号、分号、尾逗号、100 列目标宽度和 LF 换行。
  Markdown 正文保留现有折行，代码块中的示例不重新格式化。Python 继续使用 Ruff。
- 根目录 `.prettierignore` 排除上游源码、运行时/模型/数据目录、密钥、生成文件和
  `config/prompts/` 中的运行时提示词模板（README 参与格式化）。
  命令行、使用项目本地 Prettier 的编辑器、pre-commit 和 CI 共用该配置。
  安装钩子 `uvx pre-commit install` 之前，先在根目录执行 `npm ci`。
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

测试是端到端的，不需要 GPU、模型权重或网络。

- **`tests/e2e/`** 用 `agentic-meeting --config <临时配置> serve` 起真实的应用进程，和用户启动的方式完全相同，
  数据目录放在临时目录里。推理服务换成测试进程里的几个小 HTTP 服务（`tests/e2e/inference.py`），
  说的是同样的协议：识别用 llama-server 的音频输入和 `/tokenize`，实时模型和后台 agent 用 OpenAI 兼容的
  对话补全，另有 `/v1/audio/speech` 和 `/v1/embeddings`。每个服务都可以单独停掉，用来验证降级。
  测试只通过 HTTP 和 WebRTC 与应用交互：`tests/e2e/meeting_client.py` 用 aiortc 扮演会议页面，
  麦克风轨道送的是一段真实的语音录音（来自子模块 `third_party/Confucius4-R2T2`），数据通道上说 RTVI 协议。
- **`tests/test_repository_rules.py`** 守住运行起来不会立刻暴露的几条约定：源码里没有模型名，
  识别档案与上游对话模板一致，`scripts/eval_realtime_model.py` 的评测样例正好覆盖实时模型的全部工具。
- **`tests/test_real_services.py`** 用 `config/config.toml` 里的真实模型和服务做检查，标记为
  `@pytest.mark.gpu`，默认不运行：

  ```bash
  AGENTIC_MEETING_TEST_WAV=dialogue.wav uv run pytest -m gpu tests/test_real_services.py
  ```

新测试请写成用户认得出的场景：他们在会议页面或命令行上做了什么，之后通过接口、数据通道或文件看到了什么。
不要给单个函数或类写测试：那只会把代码现在的形状固定下来，并不能证明系统能用。`npm run build` 对网页客户端做类型检查，
它收发的每一种消息在服务端一侧由 `tests/e2e/` 覆盖，下面的浏览器测试则在真实浏览器里操作构建好的页面。

### 浏览器端到端测试

浏览器测试使用客户端 lockfile 中的 Playwright 1.64.0、Python 3.12–3.14 和 Node.js 24（CI 版本）。
在仓库根目录安装锁定依赖、构建真实客户端并安装三个浏览器引擎：

```bash
uv sync --frozen --extra agent
npm ci
npm ci --prefix client
npm run build --prefix client
cd client
npx playwright install --with-deps chromium firefox webkit
cd ..
```

浏览器安装会下载浏览器二进制文件，在 Linux 上安装系统包可能需要管理员权限；不会下载模型权重。
修改客户端代码后先重新构建，再运行测试，因为测试服务器提供的是 `client/dist/`。

```bash
# 三个引擎：Chromium、Firefox 和 WebKit
npm run test:e2e --prefix client
# 单个引擎
npm run test:e2e --prefix client -- --project=chromium
# 单个引擎中的一个用例
npm run test:e2e --prefix client -- --project=chromium --grep '真实页面、WebRTC、RTVI 与合成媒体探针'
```

无界面的 Linux 还需要音频输出后端，否则 Firefox 原生 `AudioContext.resume()` 可能一直等待，
尚未开始 WebRTC 协商。CI 启动 PulseAudio 的 CPU null sink，丢弃输出，不需要物理声卡。
在无界面的 Ubuntu 上，先准备这个后端再运行浏览器测试：

```bash
sudo apt-get install -y pulseaudio pulseaudio-utils
pulseaudio --start --exit-idle-time=-1
pactl load-module module-null-sink sink_name=e2e
pactl set-default-sink e2e
```

当前锁定的 Linux WebKit 会挂起文档外的静音 MediaStream video，包括测试中的原生屏幕帧消费端。
CI 使用 WebKit 的测试环境变量允许播放。本地 Linux 准备好 PulseAudio 后运行：

```bash
WEBKIT_GST_ALLOW_PLAYBACK_OF_INVISIBLE_VIDEOS=1 npm run test:e2e --prefix client
```

锁定的浏览器包含 [WebKit 319380 上游修复](https://github.com/WebKit/WebKit/commit/f2797a15c336841f348b94902c3dd556a0cd5540)
后移除此临时措施。原生媒体轨道、视频解码、截图上传及断言均保持不变。

这是测试环境准备，不替换 AudioContext 或 SDK。CI runner 在 job 结束后销毁。
本地运行结束后，用 `pactl load-module` 打印的 ID 卸载模块（`pactl unload-module <id>`）；
只有 PulseAudio 专为这次测试启动时才停止它。

三个引擎运行相同的核心用例，单 worker，不重试。每个用例启动独立的 `tests.browser.server` Python 子进程，
监听动态分配的回环端口。夹具等待服务器启动完成后输出的结构化 `E2E_READY` 消息。
服务器拥有一个系统临时目录（`agentic-meeting-e2e-*`），内含独立 SQLite 数据库、虚构会议种子、截图与任务产物。
收尾时停止子进程、关闭 Store 并删除该目录，不读取或写入 `config/config.toml`、`.env` 或 `data/`。
强制终止进程可能留下临时文件；确认测试进程已退出后，只删除系统临时目录中属于该用例的
`agentic-meeting-e2e-*` 目录。

测试覆盖生产 `create_app`、Store、HTTP 业务接口，以及真实 SmallWebRTC 传输、Pipecat 管线和 RTVI 数据通道。
推理由受控测试 bot 替代，不启动 ASR、LLM、TTS、向量嵌入或屏幕描述模型。
Chromium 与 WebKit 使用 CPU WebAudio 麦克风轨道，Firefox 使用浏览器原生的假媒体设备。
三个引擎都使用 `canvas.captureStream()` 屏幕轨道替代真实桌面采集。
浏览器对测试服务器之外的请求会被阻断并使测试失败。
这些检查不验证真实麦克风或屏幕授权、物理设备、模型效果、外部服务、GPU 行为或互联网 ICE 连通性。
CI 在 Ubuntu CPU 上运行三个引擎；其他系统的平台相关浏览器启动或 ICE 故障需要定位，不能通过跳过引擎绕过。

失败时的 trace、截图与每个用例的 `server.log` 保存在 `client/test-results/`，HTML 报告位于
`client/playwright-report/`。调试命令：

```bash
cd client
npx playwright show-report playwright-report
# 将示例替换为失败用例的实际 trace 路径
npx playwright show-trace 'test-results/<failed-test>/trace.zip'
```

这两个产物目录已被 Git 忽略，调试完成后可以删除。CI 只在失败或取消时上传这两个目录，保留三天。
E2E job 接入必需的 `All checks`，其失败、取消或跳过都会传播为检查失败。

### 使用真实模型的检查

端到端的验证使用 [runtimes.md §6](runtimes.md) 中介绍的脚本：`scripts/soak.py` 把一段录音送入运行中的服务，
`scripts/eval_realtime_model.py` 检查当前实时模型的工具选择与延迟。修改
`config/prompts/realtime_system.md` 或更换模型之后，请运行后者。

## 目录结构

见 [architecture.md §8](architecture.md#8-目录结构)。
