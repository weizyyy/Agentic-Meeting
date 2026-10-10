# 运行时与模型

[English](../runtimes.md) · **简体中文**

「运行时」指推理程序（如 `llama-server`），「模型文件」指权重，两者都不纳入仓库。
运行时由脚本获取或构建；模型文件需要手动下载，本项目不会下载任何权重。

## 1. 一览

| 运行时                                                                        | 用途                                                 | 获取方式             | 版本锁                        |
| ----------------------------------------------------------------------------- | ---------------------------------------------------- | -------------------- | ----------------------------- |
| [llama.cpp](https://github.com/ggml-org/llama.cpp)                            | 语音识别、嵌入，以及 `llama_server` 方式下的实时模型 | 预编译包             | v0.6.0（二进制构建号 b11429） |
| [NeMo-Speech.cpp](https://github.com/NVIDIA/NeMo-Speech.cpp)                  | 说话人区分（以动态库形式加载）                       | 预编译包（含 C SDK） | v0.2.0                        |
| [qwentts.cpp](https://github.com/ServeurpersoCom/qwentts.cpp)                 | 语音合成 `tts-server`                                | 从源码构建           | commit 51512f1                |
| [Confucius4-R2T2](https://github.com/netease-youdao/Confucius4-R2T2) 参考代码 | 流式识别算法的参考                                   | 仅源码，无需构建     | commit 26d55a5                |

版本锁定在 [`runtimes.lock.toml`](../../runtimes.lock.toml) 中；源码以 Git 子模块的形式放在
`third_party/` 下（浅克隆，只读）。

容器镜像用的是同样的版本：应用镜像用本脚本获取 NeMo-Speech.cpp 的预编译包，语音合成镜像从 qwentts.cpp
子模块构建，`llama-server` 用 llama.cpp 官方发布的同一构建号的镜像（[容器 §5](containers.md#5-镜像)）。

## 2. 获取脚本

```bash
python scripts/runtimes.py status                  # 看每个运行时的源码与二进制状态
python scripts/runtimes.py variants llama_cpp      # 列出可下载的预编译包，标出当前平台的默认项
python scripts/runtimes.py fetch llama_cpp         # 按当前平台自动选择并下载、解压
python scripts/runtimes.py fetch nemo_speech
python scripts/runtimes.py build qwentts --backend cuda
python scripts/runtimes.py source qwentts          # 只初始化子模块源码
```

- 脚本只用标准库，建虚拟环境之前就能跑。
- 预编译包解压到 `runtimes/<名字>/`，下载缓存在 `runtimes/_downloads/`。
- NeMo-Speech.cpp 的包会校验官方发布的 SHA-256。
- 刚克隆仓库后子模块是空的：`git submodule update --init --depth 1` 或用上面的 `source` 子命令。

## 3. 各运行时说明

### 3.1 llama.cpp

- 预编译包按 CUDA 大版本区分（`cuda12` / `cuda13`）。脚本会选与已安装的 CUDA Toolkit 大版本一致的包；
  没装 Toolkit（只有显卡驱动）时，加 `--with-extras` 连同 CUDA 运行库一起取（多 400 MB 左右）。
- macOS 的预编译包自带 Metal 支持。
- 验证：

  ```bash
  runtimes/llama_cpp/llama-server --version
  runtimes/llama_cpp/llama-server --list-devices     # 列出可用显卡及其编号（CUDA0、CUDA1…）
  ```

- 命令行参数的权威说明：`third_party/llama.cpp/tools/server/README.md`（与二进制同版本）。
  本项目用到的参数见 interfaces.md §9。

### 3.2 NeMo-Speech.cpp

- 预编译包中包含命令行程序 `nemo-speech` 和 C SDK。本项目只使用 SDK 的动态库
  `nemo_speech_asr_c`（Windows：`bin/nemo_speech_asr_c.dll`），通过 ctypes 调用，见 interfaces.md §4.2。
- 头文件：`third_party/NeMo-Speech.cpp/include/nemo_speech/diar.h`。

> [!WARNING]
> 以目录名而非文件指定模型时，`nemo-speech` 命令行程序会自动下载权重，例如不带
> `--diar-model <文件>` 的 `nemo-speech diarize x.wav`、`nemo-speech pull …`，或
> `nemo-speech serve --diar-model sortformer`。Agentic-Meeting 不会调用该程序。

### 3.3 qwentts.cpp

上游不发布预编译包，因此需要从源码构建。

| 平台    | 需要                                                                                                     |
| ------- | -------------------------------------------------------------------------------------------------------- |
| Windows | Visual Studio 或 Build Tools（勾选「使用 C++ 的桌面开发」）、CMake、Ninja；GPU 版另需完整的 CUDA Toolkit |
| Linux   | gcc/g++、CMake；GPU 版另需 CUDA Toolkit（或 Vulkan SDK）                                                 |
| macOS   | Xcode 命令行工具、CMake                                                                                  |

```bash
python scripts/runtimes.py build qwentts --backend cuda      # 或 vulkan / metal / cpu
python scripts/runtimes.py build qwentts --backend cuda --clean --cmake-arg=-DCMAKE_CUDA_ARCHITECTURES=native
```

脚本做的事：

1. 初始化 qwentts.cpp 及其 `ggml` 子模块。
2. Windows 上自动导入 MSVC 环境：用 `vswhere` 找到 `vcvars64.bat` 并读回环境变量，
   因此在普通终端中即可构建。
3. Windows 上构建 CUDA 版之前，先用一个最小的 `.cu` 文件探测 nvcc 能否与当前 MSVC 配合，
   不行就立刻给出原因和解决办法，不进入 cmake。
4. `cmake -S . -B build <后端开关>` → `cmake --build build --config Release`。
   产物在 `third_party/qwentts.cpp/build/` 下（`tts-server`、`qwen-tts` 等）。

可选参数：`--clean`（先删掉已有的 `build/`；换后端时必须加）、`--cmake-arg=<参数>`（可重复）、
`--vcvars-ver <版本>`（Windows：改用并排安装的旧版 MSVC 工具集）。
CUDA 版默认为多代显卡各编译一份内核，较慢；只在这台机器上用时加
`--cmake-arg=-DCMAKE_CUDA_ARCHITECTURES=native` 可以明显缩短构建时间。

**CUDA Toolkit 与 MSVC 工具集的版本必须互相兼容。** CUDA Toolkit 只认它发布时已有的 MSVC 版本：
稍新一点的加 `-allow-unsupported-compiler` 就能用（脚本会自动处理并提示）；差得多的会让 nvcc 的前端
直接崩溃。CUDA 13.0.48 与 MSVC 14.51（Visual Studio 18 自带的工具集）即属此类：
任何 `.cu` 文件都会使 `cudafe++` 因访问冲突退出。脚本会在运行 CMake 之前检测到这种情况，并给出以下建议：

1. 在 Visual Studio Installer 的「单个组件」里加装旧一代的 MSVC 工具集（VS 2022 那一代，14.4x），
   然后用 `--vcvars-ver` 指定。MSVC 14.44 与 CUDA 13.0 的组合可以正常构建：

   ```bash
   python scripts/runtimes.py build qwentts --backend cuda --clean --vcvars-ver 14.44 --cmake-arg=-DCMAKE_CUDA_ARCHITECTURES=native
   ```

2. 安装支持当前 Visual Studio 的更新版 CUDA Toolkit。
3. 先用 CPU 版：`python scripts/runtimes.py build qwentts --backend cpu --clean`。

使用 0.6B 内置音色模型（Q8 量化）的实测结果（测试环境见 [benchmarks.md](benchmarks.md)）：

| 项目                      | GPU 版                                  | CPU 版                     |
| ------------------------- | --------------------------------------- | -------------------------- |
| 实时率（耗时 ÷ 音频时长） | 0.19                                    | 约 1.1（略慢于实时）       |
| 流式首字节                | 0.08 秒（服务启动后第一次请求 0.24 秒） | 约 0.2 秒（第一次约 1 秒） |
| 显存                      | 约 2.4 GB                               | 0                          |
| 输出格式                  | 24 kHz、16 位、单声道                   | 同左                       |
| 中英混合文本              | 正常                                    | 正常                       |

`tts-server` 默认用编号最小的显卡（CUDA0）。CPU 版可以用来开发和功能验证，长句朗读会比实时慢一点。

`tts-server` 的参数：`--model`、`--codec`、`--alias`、`--host`、
`--port`、`--lang`、`--max-batch`、`--codec-chunk-dur`、`--no-fa`。
路由：`GET /health`、`GET /v1/models`、`GET /v1/audio/voices`、
`POST /v1/audio/speech`。请求与响应的细节见 interfaces.md §9 末尾。

语音合成是可选的：设置 `tts.enabled = false` 即可在没有它的情况下运行。

### 3.4 Confucius4-R2T2 参考代码

该子模块仅作为参考资料阅读，不安装、不导入（它依赖 PyTorch 系的包，且自带的预编译扩展只支持
Linux + CUDA + Python 3.12）。要看的文件：

| 文件                                 | 看什么                                                                                                       |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------ |
| `r2t2_llama/llama_native_backend.py` | `LlamaServerClient.generate`：怎么向 `llama-server` 发带音频和前缀的请求；`LlamaServerStreaming`：流式状态机 |
| `r2t2/r2t2_asr.py`                   | `streaming_transcribe_no_reset`：滚动窗口、文字与音频同步丢弃、回退规则                                      |
| `ws_server.py`                       | `detect_and_fix_repetitions`、`detect_hallucination`：复读与幻觉的检测                                       |
| `README.zh.md`                       | 模型能力、热词用法、官方评测数据                                                                             |

## 4. 模型文件

把下列文件放在 `models/` 下的任意位置，并在 `config/config.toml` 中填写路径。
最后一栏是本项目测试时使用的模型；同类的其他模型以相同方式配置。

| 配置项                                         | 文件                                       | 测试所用                                                                |
| ---------------------------------------------- | ------------------------------------------ | ----------------------------------------------------------------------- |
| `realtime_llm.llama_server.launch.model_path`  | 对话模型 GGUF（仅 `llama_server` 方式）    | 任意支持工具调用的模型                                                  |
| `realtime_llm.llama_server.launch.mmproj_path` | 该模型配套的识图投影 GGUF                  | 模型不识图时留空并设 `supports_vision = false`                          |
| `asr.launch.model_path`                        | 识别模型 GGUF                              | Confucius4-R2T2，Q8_0 与 f16                                            |
| `asr.launch.mmproj_path`                       | 识别模型配套的音频投影 GGUF                | 同一发布页里以 `mmproj` 开头的文件                                      |
| `diarization.model_path`                       | 说话人区分 GGUF（NeMo-Speech.cpp 格式）    | Nemotron-3-Diarization，q8_0                                            |
| `tts.launch.model_path`                        | 语音合成的 talker GGUF（qwentts.cpp 格式） | Qwen3-TTS 内置音色版，0.6B 与 1.7B                                      |
| `tts.launch.codec_path`                        | 语音合成的 tokenizer/codec GGUF            | 与 talker 同一发布页                                                    |
| `embedding.launch.model_path`                  | 嵌入模型 GGUF                              | Qwen3-Embedding 0.6B，Q8_0；`embedding.dimensions` 须设为模型的输出维度 |

**硬件档位。** 使用测试所用的模型时，全部本地服务（识别、说话人区分、语音合成、嵌入）合计占用的显存：

| 档位               | 识别 | 语音合成 | 显存         |
| ------------------ | ---- | -------- | ------------ |
| 12 GB 显卡（最低） | Q8_0 | 0.6B     | 略高于 10 GB |
| 16 GB 及以上的显卡 | f16  | 1.7B     | 约 15 GB     |

实时模型若由本机的 `llama-server` 提供，需要在上述数字之外另算显存；采用 `realtime_llm.mode = "openai_api"`
时则不占用。各服务也可以分散到多块显卡上（见第 5 节）。

注意：

- GGUF 文件是针对特定运行时转换的，不能互换：识别使用标准的 llama.cpp 格式，
  语音合成使用 qwentts.cpp 的格式，说话人区分使用 NeMo-Speech.cpp 的格式。
- `tts.voice` 填所选语音合成权重支持的音色名。
- 更换实时模型时需要确认三点：如何关闭思考（`thinking = false` 对应 `--reasoning off`，
  个别模型还需要在 `extra_body` 里加模板参数）、推荐的采样参数、是否识图。
- 实时模型选用 chat completions 接口方式（`realtime_llm.mode = "openai_api"`）时不需要任何本地权重，
  只填 `[realtime_llm.openai_api]` 里的地址、模型名和密钥变量名；关思考的做法按该服务的文档写进 `extra_body`。

填完后运行 `uv run agentic-meeting check`，它会列出所有还没准备好的文件。

## 5. 多显卡分配

`llama-server --list-devices` 给出的编号是 llama.cpp 自己的编号。分配方式二选一：

- 在 `launch.extra_args` 里加 `["--device", "CUDA1"]`（只对 `llama-server` 有效）；
- 在 `launch.env` 里设 `CUDA_VISIBLE_DEVICES`（对所有子进程有效；注意它的编号与 `nvidia-smi` 一致，
  不一定与 llama.cpp 的编号一致，以实际加载日志为准）。

说话人区分在应用进程内运行，用 `diarization.gpu` 指定显卡。`diarization.gpu` 采用 llama.cpp 的 CUDA 编号（`gpu = 0` 即 CUDA0）。`nvidia-smi` 按另一套规则排序，
列出的顺序可能正好相反。

以一台 22 GB + 6 GB 双显卡的机器为例：

| llama.cpp 编号 | 显存  | 建议放置的服务       |
| -------------- | ----- | -------------------- |
| CUDA0          | 22 GB | 实时模型、语音合成   |
| CUDA1          | 6 GB  | 语音识别、说话人区分 |

```toml
[realtime_llm.llama_server.launch]
extra_args = ["--device", "CUDA0"]

[asr.launch]
extra_args = ["--device", "CUDA1"]

[diarization]
gpu = 1

[embedding.launch]
gpu_layers = "0"
extra_args = ["--pooling", "last", "--device", "none"]   # --device none 使该服务完全不占用显卡
```

## 6. 验证

权重放好、`config/config.toml` 填好之后：

```bash
uv run agentic-meeting check             # 配置里还缺什么（权重文件、密钥、镜像名）
uv run agentic-meeting services up       # 按配置拉起本地推理服务，全部就绪后挂着；Ctrl+C 全部停止
uv run agentic-meeting services status   # 另开一个终端：各服务是否可达
```

`services up` 会顺带核对语音合成的音色名是否存在。日志在 `data/logs/<服务名>.log`，启动失败时错误信息里带日志的最后几行。
规则见 interfaces.md §9。

整条链路的验证不需要麦克风，拿一段会议录音就行：

```bash
uv run agentic-meeting serve --with-services                        # 一个终端：应用 + 推理服务
uv run python scripts/soak.py --audio 录音.mp3 --minutes 10          # 另一个终端：把录音按真实速度送进去
uv run python scripts/eval_realtime_model.py                         # 实时模型：首字延迟、工具选得对不对
uv run python scripts/mic_check.py --wav 我的录音.wav                 # 麦克风偏轻时的离线诊断（不需要权重）
```

`soak.py` 会新建一场会议（之后能在页面的会议列表里看到完整的转录），定时用合成的语音叫助理、打字提问，
并记录首字与首音延迟、字幕延迟、内存与显存。一次完整的运行结果见 [benchmarks.md](benchmarks.md)。

需要真实硬件的自动化测试标了 `gpu`，默认跳过：

```bash
AGENTIC_MEETING_TEST_WAV=多人对话.wav uv run pytest -m gpu tests/test_real_services.py -s   # 说话人区分、远端模型、MCP、沙箱
```

用 llama.cpp 部署实时模型时，也可以先手工确认模型能起来：

```bash
runtimes/llama_cpp/llama-server -m <对话模型.gguf> --mmproj <投影.gguf> -a realtime \
    --port 8080 -c 32768 -np 2 --jinja --reasoning off
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/v1/chat/completions -H "Content-Type: application/json" \
    -d '{"model":"realtime","messages":[{"role":"user","content":"用一句话介绍你自己"}],"max_tokens":64}'
```

**已验证的环境。** 完整系统已在 Windows 11、Visual Studio 18（MSVC 14.44 工具集）、CUDA Toolkit 13.0、
Docker 29 上运行。Linux 上自动化测试全部通过，完整系统尚未在该平台上运行；macOS 未经测试。

## 7. 升级运行时

1. 改 `runtimes.lock.toml` 里的版本与资产名。
2. 更新对应子模块指针：`git -C third_party/<名字> fetch --depth 1 origin <新标签>`，检出后在仓库根目录
   `git add third_party/<名字>`。
3. `python scripts/runtimes.py fetch <名字> --force`。
4. 升级 llama.cpp 时，同时改 `compose.yaml` 和 `docker/compose.*.yaml` 里镜像标签中的构建号；
   `tests/test_repository_rules.py` 里有一条测试核对它们与 `runtimes.lock.toml` 一致。其他镜像重新构建时自动用上新版本。
5. 重跑 §6 的验证和 `uv run pytest`。
6. 接口有变化时，更新 interfaces.md §9 与本文件。

Pipecat 的升级同理：改 `pyproject.toml` 里的精确版本号 → `uv sync` → 逐条核对
[pipecat-notes.md](pipecat-notes.md)。
