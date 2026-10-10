# 容器

[English](../containers.md) · **简体中文**

在一台装了 Docker 的 Linux 机器上（有显卡更好），可以用 Docker Compose 运行整套系统。每个推理服务在自己的容器里，
所以可以只起需要的几个、把某一个挪到别的机器上，或者换成自己的镜像。模型权重留在宿主机上：任何镜像里都没有权重，
启动时也不会下载。

- [1. 各容器的分工](#1-各容器的分工)
- [2. 环境要求](#2-环境要求)
- [3. 第一次启动](#3-第一次启动)
- [4. 选择和组合服务](#4-选择和组合服务)
- [5. 镜像](#5-镜像)
- [6. 代码沙箱](#6-代码沙箱)
- [7. macOS 与 Windows](#7-macos-与-windows)
- [8. 日常操作与常见问题](#8-日常操作与常见问题)

## 1. 各容器的分工

| `compose.yaml` 里的服务 | 镜像                                              | 宿主机上的端口                  | 用显卡（CUDA 叠加文件） | 默认启动            |
| ----------------------- | ------------------------------------------------- | ------------------------------- | ----------------------- | ------------------- |
| `asr`                   | llama.cpp 官方的 `llama-server` 镜像              | `127.0.0.1:8081`                | 是                      | 是                  |
| `realtime`              | llama.cpp 官方的 `llama-server` 镜像              | `127.0.0.1:8080`                | 是                      | profile `realtime`  |
| `tts`                   | `agentic-meeting-tts`（qwentts.cpp `tts-server`） | `127.0.0.1:8082`                | 是                      | profile `tts`       |
| `embedding`             | llama.cpp 官方的 `llama-server` 镜像（CPU）       | `127.0.0.1:8083`                | 否                      | profile `embedding` |
| `app`                   | `agentic-meeting`（应用、网页客户端）             | `server.port`（7860），所有网卡 | 是（说话人区分）        | 是                  |
| `asr-template`          | `agentic-meeting`，写出识别服务的对话模板         | —                               | 否                      | 是，写完即退出      |

- 推理服务在各自容器里监听 8080 端口，映射到宿主机回环地址上 `config/config.toml` 默认使用的端口。和本机安装一样，
  其他机器连不到它们（[architecture.md §2](architecture.md)）。
- 它们的命令行就是 `services up` 生成的那些（[interfaces.md §9](interfaces.md)）。识别服务要用 `asr.profile`
  里的对话模板：一次性的 `asr-template` 容器在 `asr` 启动前，按你的 `config.toml` 把它写进一个卷。
- `app` 使用宿主机网络。WebRTC 的媒体对每个连接走一个随机 UDP 端口（[deployment.md §1](deployment.md#1-哪些东西要能连通)），
  桥接网络无法把这些端口映射出去。所以应用容器只支持 Linux（§7）。
- 说话人区分是加载进应用进程的动态库，所以 `app` 容器也要用显卡。
- `asr` 健康后 `app` 才启动；已启动的可选服务也会等它们就绪。

## 2. 环境要求

| 要求                                                                                                        | 说明                                                                |
| ----------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| Linux，x86-64                                                                                               | 镜像只构建 `linux/amd64`                                            |
| Docker Engine 25 及以上，Compose 2.24 及以上                                                                | `docker compose version`                                            |
| NVIDIA 显卡：[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/) | 驱动要支持 CUDA 12.8（570 或更新的版本）；宿主机不需要 CUDA Toolkit |
| AMD、Intel 显卡：宿主机上的 Vulkan 驱动                                                                     | 实验性（§5）                                                        |
| 模型文件                                                                                                    | 与本机安装相同：[runtimes.md §4](runtimes.md#4-模型文件)            |

需要克隆一份仓库：里面有 `compose.yaml` 和它挂载的文件。宿主机上不需要安装 Python、Node.js、uv 和各推理运行时。

## 3. 第一次启动

```bash
git clone https://github.com/weizyyy/Agentic-Meeting.git
cd Agentic-Meeting
cp config/config.example.toml config/config.toml
cp .env.example .env
mkdir -p data
```

1. **模型文件**：与本机安装一样放在 `models/` 下。
2. **`config/config.toml`**：按[入门指南](getting-started.md#配置)填写。模型路径相对仓库根目录（`models/…`）；
   `models/` 以外的绝对路径在容器里看不到。各 `base_url` 的默认值已经指向这些容器。只有 CPU 的机器上把
   `diarization.gpu` 设为 `-1`。
3. **`.env`**：除了密钥，还放 Compose 读取的变量（见下表）。取消 `.env.example` 里容器那一节的注释，
   按 `config.toml` 里的写法填上模型文件。
4. **`data/`** 要对容器里的用户可写，默认是 uid 1000。你的用户 uid 不同时，在 `.env` 里设置
   `AGENTIC_MEETING_UID` 和 `AGENTIC_MEETING_GID`（`id -u`、`id -g`）。

然后启动：

```bash
docker compose up -d                                                # 只用 CPU
docker compose -f compose.yaml -f docker/compose.cuda.yaml up -d    # NVIDIA 显卡
```

不想每次都写文件列表，可以写进 `.env`：`COMPOSE_FILE=compose.yaml:docker/compose.cuda.yaml`。打开
<http://localhost:7860>；其他设备访问需要 HTTPS，见[入门指南](getting-started.md#从其他设备访问)或
[部署指南](deployment.md)。

Compose 从 `.env` 读取的变量：

| 变量                                                                                  | 默认值                      | 含义                                                          |
| ------------------------------------------------------------------------------------- | --------------------------- | ------------------------------------------------------------- |
| `COMPOSE_PROFILES`                                                                    | —                           | 要启动的可选服务：`realtime`、`tts`、`embedding`，逗号分隔    |
| `COMPOSE_FILE`                                                                        | `compose.yaml`              | 要叠加的文件，用 `:` 分隔                                     |
| `ASR_MODEL`、`ASR_MMPROJ`                                                             | —                           | 识别模型及其音频投影，例如 `models/asr/….gguf`                |
| `ASR_CTX_SIZE`、`ASR_GPU_LAYERS`                                                      | `8192`、`all`               | 即 `asr.launch.ctx_size` 和 `gpu_layers`                      |
| `REALTIME_MODEL`、`REALTIME_MMPROJ`                                                   | —                           | 实时模型及其视觉投影；不能识图的模型把 `REALTIME_MMPROJ` 留空 |
| `REALTIME_CTX_SIZE`、`REALTIME_PARALLEL`、`REALTIME_GPU_LAYERS`、`REALTIME_REASONING` | `32768`、`2`、`all`、`off`  | `realtime_llm.llama_server` 里对应的设置                      |
| `TTS_MODEL`、`TTS_CODEC`、`TTS_LANGUAGE`                                              | —、—、`Chinese`             | talker 和 codec 的 GGUF，`tts.launch.default_language`        |
| `EMBEDDING_MODEL`、`EMBEDDING_POOLING`                                                | —、`last`                   | 嵌入模型 GGUF 及其池化方式                                    |
| `AGENTIC_MEETING_UID`、`AGENTIC_MEETING_GID`                                          | `1000`                      | 应用容器里的用户                                              |
| `AGENTIC_MEETING_TAG`、`AGENTIC_MEETING_TTS_TAG`                                      | `cpu`，或叠加文件对应的版本 | 镜像标签，例如 `0.3.0-cuda` 可固定在某个发布版本              |
| `LLAMA_SERVER_TAG`、`LLAMA_SERVER_CUDA_TAG`、`LLAMA_SERVER_VULKAN_TAG`                | 锁定的构建号                | llama.cpp 镜像的标签                                          |

用 `-a`、`--alias` 传的请求名 `realtime`、`tts`、`embedding` 与 `realtime_llm.llama_server.model`、`tts.model`、
`embedding.model` 的默认值一致；改了这几项，`compose.yaml` 里的命令也要一起改。没有对应变量的选项（例如
`launch.extra_args`）写在 `compose.override.yaml` 里该服务的 `command` 中。

`check` 在应用容器里运行：

```bash
docker compose run --rm --no-deps app check
```

## 4. 选择和组合服务

- **只起部分服务**：直接写服务名，`docker compose up -d asr tts app`。没有启动的可选服务照常让应用降级
  （[architecture.md §9](architecture.md)），应用仍会启动。
- **远端的实时模型**：`realtime_llm.mode = "openai_api"` 时，`COMPOSE_PROFILES` 里不要写 `realtime`。
- **CPU 和显卡混用**：CUDA 叠加文件里嵌入模型仍用 CPU。要让别的服务也改用 CPU，把它的标签改回去，例如
  `AGENTIC_MEETING_TTS_TAG=cpu`。
- **多块显卡**：在 `compose.override.yaml` 里把某个服务的 `count: all` 换成 `device_ids: ["1"]`（编号同
  `nvidia-smi`），或者在它的 `command` 里加 `--device`（[runtimes.md §5](runtimes.md#5-多显卡分配)）。说话人区分用
  `diarization.gpu` 指定。
- **服务放在别的机器上，或用自己的镜像**：把 `config.toml` 里对应的 `base_url` 指过去，这里不启动它。自己运行的服务要用
  同样的命令行；识别服务还要带 `--chat-template-file`，模板用 `asr-template` 写出的那份（或者 `services up` 写出的
  `data/run/asr_chat_template.jinja`）。
- **推理服务在容器里，应用在本机**：只启动推理服务（`docker compose up -d asr tts embedding`），在本机运行
  `uv run agentic-meeting serve`，不加 `--with-services`。macOS 和 Windows 上就走这条路（§7）。

## 5. 镜像

| 镜像                                      | 标签                                                          | 内容                                                            |
| ----------------------------------------- | ------------------------------------------------------------- | --------------------------------------------------------------- |
| `ghcr.io/weizyyy/agentic-meeting`         | `cpu`、`cuda`、`vulkan`；`<版本>-<变体>`                      | 应用、构建好的网页客户端、说话人区分用的 NeMo-Speech.cpp 动态库 |
| `ghcr.io/weizyyy/agentic-meeting-tts`     | `cpu`、`cuda`、`vulkan`；`<版本>-<变体>`                      | 从钉住的 qwentts.cpp 源码构建的 `tts-server` 和 `qwen-tts`      |
| `ghcr.io/weizyyy/agentic-meeting-sandbox` | `latest`；`<版本>`                                            | 代码沙箱：带 numpy、pandas、matplotlib 的 Python                |
| `ghcr.io/ggml-org/llama.cpp`（上游）      | `server-b11429`、`server-cuda-b11429`、`server-vulkan-b11429` | `runtimes.lock.toml` 钉住的构建号的 `llama-server`              |

发版流程每个版本都把本项目的三个镜像发布到 GHCR；不带版本号的标签跟随最新的发布版本。应用镜像和语音合成镜像与本机安装
一样，用 `runtimes.lock.toml` 和子模块里的运行时版本。CUDA 镜像基于 CUDA 12.8；语音合成镜像为 Pascal 到 Blackwell 的
各代显卡编译。

CI 在每个可能影响镜像的改动上构建全部镜像（CUDA 版语音合成除外，它在发版时构建），并在 CPU 和 Vulkan 镜像里把各个程序启动一次。
这些镜像都还没有用真实模型跑过；Vulkan 变体是实验性的。

自己构建（不是发布版本的提交需要这样做）：

```bash
git submodule update --init --depth 1 third_party/qwentts.cpp
git -C third_party/qwentts.cpp submodule update --init --depth 1
docker compose -f compose.yaml -f docker/compose.cuda.yaml build
```

或者一次构建一个：

```bash
docker build -f docker/app/Dockerfile --build-arg VARIANT=cuda -t ghcr.io/weizyyy/agentic-meeting:cuda .
docker build -f docker/tts/Dockerfile --build-arg VARIANT=cuda -t ghcr.io/weizyyy/agentic-meeting-tts:cuda .
```

CUDA 版的语音合成编译时间较长；`--build-arg CUDA_ARCHITECTURES=89` 只为一代显卡编译。构建上下文里只有列出的文件
（`docker/*/Dockerfile.dockerignore`）：`.env`、`config/config.toml`、证书、`models/` 和 `data/` 永远不会进去。

## 6. 代码沙箱

后台 agent 为每个任务开一个单独的容器来执行代码。应用本身在容器里时，它得能用上某个容器运行时。Compose 的做法是通过套接字
使用宿主机的 Docker：沙箱容器和应用容器并列运行，工作区经 Docker API 复制进出，不需要两边路径一致。改在应用容器里运行
Docker 则需要特权容器，交出去的权限一样多。

能访问 Docker 套接字就等于拥有宿主机的 root 权限，所以要显式启用：

```bash
# .env
AGENTIC_MEETING_DOCKER_GID=<docker 组的 gid：getent group docker | cut -d: -f3>
COMPOSE_FILE=compose.yaml:docker/compose.cuda.yaml:docker/compose.sandbox.yaml
```

```toml
[agent.sandbox]
kind = "docker"
docker_image = "ghcr.io/weizyyy/agentic-meeting-sandbox:latest"
```

先用 `docker pull ghcr.io/weizyyy/agentic-meeting-sandbox:latest` 拉一次沙箱镜像。不加这个叠加文件时，agent 仍可以
检索和读图，并会说明不能运行代码（[常见问题](troubleshooting.md)）。

## 7. macOS 与 Windows

应用容器要靠宿主机网络跑 WebRTC，而 Docker Desktop 提供的宿主机网络无法让局域网里的浏览器连进来。在这两个系统上，
请在本机运行应用（[入门指南](getting-started.md)），推理服务可以放进容器：

- **Windows** 上的 Docker Desktop 用 WSL 2 后端时可以把 NVIDIA 显卡交给容器。用 CUDA 叠加文件启动推理服务
  （`docker compose -f compose.yaml -f docker/compose.cuda.yaml up -d asr tts embedding`，需要时加上 `realtime`），
  再用 `uv run agentic-meeting serve` 运行应用。这种组合还没有验证过。
- **macOS** 不能把显卡交给容器。请在本机安装：预编译运行时支持 Metal（`python scripts/runtimes.py fetch …`、
  `build qwentts --backend metal`）。CPU 镜像也能在上面运行，但慢得多。

## 8. 日常操作与常见问题

```bash
docker compose ps                    # 各容器的状态和健康情况
docker compose logs -f asr           # 某个服务的日志
docker compose pull && docker compose up -d   # 更新到最新的镜像
docker compose down                  # 全部停止；data/ 和 models/ 留在宿主机上
```

`/healthz`、`/readyz` 和 `/metrics` 与不用容器时一样（[入门指南](getting-started.md#检查健康状态与指标)）。

**`bind source path does not exist`。** 缺少 `config/config.toml`、`.env` 或 `data/`，按 §3 建好。

**应用日志里 `/app/data` 下报 `Permission denied`。** `data/` 对容器里的用户不可写，见 §3 的 `AGENTIC_MEETING_UID`。

**`asr` 一直不健康或反复重启。** `docker compose logs asr` 会写明原因，通常是 `ASR_MODEL` 或 `ASR_MMPROJ`
没填或指到了 `models/` 以外。加载可能要几分钟，健康检查最多等十分钟。

**`could not select device driver "nvidia"`。** 没有安装 NVIDIA Container Toolkit，或装完后没有重启 Docker。

**Compose 提示某个变量没有设置，变量名是密码的一部分。** Compose 也读 `.env`，并会展开值里的 `$`。把这类值放进单引号；
应用自己读这个文件，不受影响。

**页面能连上，但其他设备上没有声音和字幕。** 与不用容器时一样：UDP 要能到达宿主机，否则配置 TURN
（[deployment.md §4](deployment.md#4-stun-与-turn)）。
