# 入门指南

[English](../getting-started.md) · **简体中文**

本文介绍从克隆仓库到开始第一场会议的全部步骤。

- [准备工作](#准备工作)
- [安装](#安装)
- [准备模型文件](#准备模型文件)
- [配置](#配置)
- [运行](#运行)
- [从其他设备访问](#从其他设备访问)
- [启用代码沙箱](#启用代码沙箱)
- [不用麦克风进行验证](#不用麦克风进行验证)

## 准备工作

| 项目 | 说明 |
|---|---|
| 操作系统 | 带 NVIDIA 显卡的 Windows 11 或 Linux，或 Apple 芯片的 macOS |
| 显存 | 本地语音模型需要单卡 12 GB 或以上，各档配置见 [runtimes.md §4](runtimes.md#4-模型文件) |
| [uv](https://docs.astral.sh/uv/) | 安装 Python 和锁定版本的依赖。支持 Python 3.12、3.13 和 3.14；默认使用 3.12，可用 `uv sync --python 3.14` 指定其他版本 |
| Node.js 22.18+ | 构建网页客户端 |
| Git | 推理运行时以子模块形式管理 |
| C++ 工具链、CMake | 仅在构建语音合成运行时时需要，见 [runtimes.md §3.3](runtimes.md#33-qwenttscpp) |
| Docker | 可选，供后台 agent 运行代码 |

## 安装

```bash
git clone https://github.com/weizyyy/Agentic-Meeting.git
cd Agentic-Meeting
git submodule update --init --depth 1

uv sync --extra agent
cd client && npm install && npm run build && cd ..
```

获取推理运行时。脚本会按当前平台选择对应的预编译包：

```bash
python scripts/runtimes.py fetch llama_cpp
python scripts/runtimes.py fetch nemo_speech
python scripts/runtimes.py build qwentts --backend cuda --cmake-arg=-DCMAKE_CUDA_ARCHITECTURES=native
python scripts/runtimes.py status
```

语音合成没有预编译包，需要从源码构建。`CMAKE_CUDA_ARCHITECTURES=native` 表示只为本机的显卡编译 CUDA 内核，
构建时间明显缩短；需要让生成的程序也能在其他代际的显卡上运行时，去掉这个参数。
其他后端为 `vulkan`、`metal` 和 `cpu`。构建失败时可以先用 `--backend cpu`，
或在配置中设置 `tts.enabled = false`，之后再处理。详见 [runtimes.md](runtimes.md)。

## 准备模型文件

模型权重需要自行下载并放到 `models/` 目录下，本仓库中的任何脚本都不会下载权重。

| 用途 | 需要的文件 |
|---|---|
| 流式识别 | 模型 GGUF 及其音频投影（`mmproj`）GGUF |
| 说话人区分 | NeMo-Speech.cpp 格式的说话人区分 GGUF |
| 语音合成 | qwentts.cpp 格式的 talker GGUF 与 codec GGUF |
| 嵌入 | 嵌入模型 GGUF |
| 实时模型 | 对话模型 GGUF（识图时还需 `mmproj`）—— 仅在用 llama.cpp 部署时需要 |

测试所用的模型以及各运行时要求的格式见 [runtimes.md §4](runtimes.md#4-模型文件)。

## 配置

```bash
cp config/config.example.toml config/config.toml
cp .env.example .env
```

编辑 `config/config.toml`。首次运行需要填写：

1. **`[session] assistant_name`** —— 助理的名字，同时也是唤醒词。必须是英文单词（如 `Nova`），
   并且不是会议中常说的词。
2. **模型路径** —— `[asr.launch]`、`[diarization]`、`[tts.launch]`、`[embedding.launch]` 中的
   `model_path`（以及 `mmproj_path` / `codec_path`），还有 `tts.voice`。
3. **实时模型** —— 在 `[realtime_llm]` 中选择一种接入方式：

   ```toml
   [realtime_llm]
   mode = "llama_server"      # 由 llama.cpp 在本地部署，填写 [realtime_llm.llama_server.launch]
   # mode = "openai_api"      # 任意 OpenAI 兼容接口，填写 [realtime_llm.openai_api]
   ```

   实时模型应关闭思考（reasoning）。先思考再回答的模型，首字延迟会成倍增加。
4. **后台 agent**（可选）—— 在 `[agent]` 中填写接口地址、模型名和 MCP 服务；不需要时设置 `enabled = false`。

密钥不写入 `config.toml`。配置文件中只保存环境变量的**名字**（`api_key_env`、`headers_env`），
变量的值放在 `.env` 中。

填写完成后进行校验：

```bash
uv run agentic-meeting check
```

`check` 会列出缺失的文件、未设置的密钥以及其他会妨碍启动的问题。实时模型指向其他机器时，
还会提示会议转录将发送到该地址。

完整的配置项说明见 [configuration.md](configuration.md)。

## 运行

```bash
uv run agentic-meeting serve --with-services
```

该命令先启动本地推理服务并等待其就绪，再启动应用；按 `Ctrl+C` 全部停止。
浏览器打开 <http://localhost:7860>，点击「开始新会议」并允许使用麦克风。

推理服务也可以单独管理：

```bash
uv run agentic-meeting services up        # 启动并守护本地推理服务
uv run agentic-meeting services status    # 仅做健康检查
uv run agentic-meeting serve              # 仅启动应用
```

日志写入 `data/logs/<服务名>.log`，会议数据保存在 `data/` 目录下。

接下来请阅读[使用指南](user-guide.md)。

## 从其他设备访问

浏览器只允许 HTTPS 页面或 `localhost` 页面使用麦克风和屏幕共享，因此局域网内的其他设备需要通过证书访问。
[mkcert](https://github.com/FiloSottile/mkcert) 是最简便的办法：

```bash
mkcert -install
mkcert -cert-file config/meeting.pem -key-file config/meeting-key.pem <服务器的局域网 IP> localhost 127.0.0.1
```

```toml
[server]
host = "0.0.0.0"
port = 443
tls_cert = "config/meeting.pem"
tls_key = "config/meeting-key.pem"
```

其他设备即可访问 `https://<服务器的局域网 IP>`。如需消除证书警告，把 `mkcert -CAROOT` 所示目录中的
`rootCA.pem` 安装为各设备的受信任根证书。`*.pem` 文件已被 Git 忽略。同一局域网内无需配置 ICE 服务器。

### 访问口令

不设置访问口令时，任何能访问该端口的人都可以查看和删除会议记录。如果网络里还有不该看到会议内容的人，
请设置口令：

```toml
[server]
password_env = "AGENTIC_MEETING_PASSWORD"
```

```bash
# 仓库根目录的 .env（已被 Git 忽略）
AGENTIC_MEETING_PASSWORD=<较长的口令，至少 8 个字符>
```

之后每个浏览器第一次打开页面时需要输入口令，登录有效期为 `auth_session_days`（默认 7 天）。
顶部栏的「退出登录」可在当前浏览器上退出。修改口令或删除 `data/auth_secret` 会让所有设备退出登录。
5 分钟内输错 5 次后，该设备需要等待一段时间才能再试。

请与 HTTPS 一起使用：纯 HTTP 下口令以明文在网络上传输。口令是整个应用共用的一个密码，目前还没有个人账号。

## 启用代码沙箱

没有 Docker 时，后台 agent 仍可以检索和读图。若要让它运行代码（计算、画图），先构建沙箱镜像，再在配置中引用：

```bash
docker build -t agentic-meeting-sandbox:py312 docker/sandbox
```

```toml
[agent.sandbox]
kind = "docker"
docker_image = "agentic-meeting-sandbox:py312"
```

## 不用麦克风进行验证

`scripts/soak.py` 是一个无界面的客户端：它像浏览器一样建立连接，按实际速度播放一段录音，
定时呼叫助理，并记录延迟与资源占用。

```bash
uv run python scripts/soak.py --audio meeting.mp3 --minutes 30
```

它创建的会议之后可以在网页中查看，用于检查转录、生成报告和试用导出。
一次完整的运行结果见 [benchmarks.md](benchmarks.md)。
