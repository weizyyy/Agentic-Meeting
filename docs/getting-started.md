# Getting started

**English** · [简体中文](zh-CN/getting-started.md)

This guide takes you from a fresh clone to a running meeting.

- [Prerequisites](#prerequisites)
- [Install](#install)
- [Prepare model files](#prepare-model-files)
- [Configure](#configure)
- [Run](#run)
- [Check health and metrics](#check-health-and-metrics)
- [Access from other devices](#access-from-other-devices)
- [Enable the code sandbox](#enable-the-code-sandbox)
- [Verify without a microphone](#verify-without-a-microphone)

## Prerequisites

| Requirement                      | Notes                                                                                                                                               |
| -------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| Operating system                 | Windows 11 or Linux with an NVIDIA GPU, or macOS on Apple silicon                                                                                   |
| GPU memory                       | 12 GB or more on one card for the local speech models; see the hardware tiers in [runtimes.md §4](runtimes.md#4-model-files)                        |
| [uv](https://docs.astral.sh/uv/) | Installs Python and the locked dependencies. Python 3.12, 3.13 and 3.14 are supported; uv uses 3.12 unless told otherwise (`uv sync --python 3.14`) |
| Node.js 22.18+                   | Builds the web client                                                                                                                               |
| Git                              | The inference runtimes are tracked as submodules                                                                                                    |
| C++ toolchain, CMake             | Only for building the speech synthesis runtime; see [runtimes.md §3.3](runtimes.md#33-qwenttscpp)                                                   |
| Docker                           | Optional. Lets the background agent run code                                                                                                        |

## Install

```bash
git clone https://github.com/weizyyy/Agentic-Meeting.git
cd Agentic-Meeting
git submodule update --init --depth 1

uv sync --extra agent
cd client && npm install && npm run build && cd ..
```

Fetch the inference runtimes. The script picks the right prebuilt package for your platform:

```bash
python scripts/runtimes.py fetch llama_cpp
python scripts/runtimes.py fetch nemo_speech
python scripts/runtimes.py build qwentts --backend cuda --cmake-arg=-DCMAKE_CUDA_ARCHITECTURES=native
python scripts/runtimes.py status
```

Speech synthesis has no prebuilt package and is compiled from source. `CMAKE_CUDA_ARCHITECTURES=native`
compiles the CUDA kernels for the GPU in this machine only, which shortens the build; leave the
argument out for a binary that also runs on other GPU generations. The other backends are
`vulkan`, `metal` and `cpu`. If the build fails, start
with `--backend cpu` or set `tts.enabled = false` and come back to it later. Details are in
[runtimes.md](runtimes.md).

## Prepare model files

Download the weights yourself and place them under `models/`. Nothing in this repository downloads
weights.

| Purpose             | Needed files                                                                    |
| ------------------- | ------------------------------------------------------------------------------- |
| Streaming ASR       | Model GGUF and its audio projector (`mmproj`) GGUF                              |
| Speaker diarization | Diarization GGUF in NeMo-Speech.cpp format                                      |
| Speech synthesis    | Talker GGUF and codec GGUF in qwentts.cpp format                                |
| Embeddings          | Embedding GGUF                                                                  |
| Realtime LLM        | Chat model GGUF (and `mmproj` for vision) — only when serving it with llama.cpp |

[runtimes.md §4](runtimes.md#4-model-files) lists the models used in testing and the format each
runtime expects.

## Configure

```bash
cp config/config.example.toml config/config.toml
cp .env.example .env
```

Edit `config/config.toml`. For a first run you need:

1. **`[session] assistant_name`** — the assistant's name, which is also the wake word. It must be an
   English word such as `Nova`; pick one that does not come up in normal conversation.
2. **Model paths** — `model_path` (and `mmproj_path` / `codec_path`) under `[asr.launch]`,
   `[diarization]`, `[tts.launch]` and `[embedding.launch]`, plus `tts.voice`.
3. **The realtime LLM** — choose one access mode in `[realtime_llm]`:

   ```toml
   [realtime_llm]
   mode = "llama_server"      # served locally by llama.cpp; fill [realtime_llm.llama_server.launch]
   # mode = "openai_api"      # any OpenAI-compatible endpoint; fill [realtime_llm.openai_api]
   ```

   Disable reasoning ("thinking") for this model. A model that reasons before answering multiplies
   the time to first token.

4. **The background agent** (optional) — endpoint, model name and MCP servers under `[agent]`, or
   `enabled = false` to turn it off.

Secrets never go into `config.toml`. The file stores the _names_ of environment variables
(`api_key_env`, `headers_env`); put the values in `.env`.

Then validate:

```bash
uv run agentic-meeting check
```

`check` lists missing files, unset secrets and anything else that would prevent startup. When the
realtime LLM points to another machine it also reminds you that the transcript will be sent there.

The full reference is [configuration.md](configuration.md).

## Run

```bash
uv run agentic-meeting serve --with-services
```

This starts the local inference services, waits until they are healthy, then starts the
application. `Ctrl+C` stops everything. Open <http://localhost:7860>, click **开始新会议** and allow
microphone access.

To manage the inference services separately:

```bash
uv run agentic-meeting services up        # start and supervise the local services
uv run agentic-meeting services status    # health check only
uv run agentic-meeting serve              # application only
```

Logs are written to `data/logs/<service>.log`; meeting data lives in `data/`.

Global CLI options go before the subcommand, for example:

```bash
uv run agentic-meeting --config config/config.example.toml check
```

Continue with the [user guide](user-guide.md).

## Check health and metrics

Once the backend is listening, request its application port directly (7860 by default):

```bash
curl -i http://localhost:7860/healthz
curl -i http://localhost:7860/readyz
curl -i http://localhost:7860/metrics
```

These three exact GET routes require no login and return `Content-Type: application/json`.
They live at the backend root, without `/api`. The Vite development server on port 5173 currently
proxies only `/api`; request port 7860 for these checks, or your configured backend port.

| Endpoint   | Use and response                                                                                                                                                                     |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `/healthz` | Process liveness: HTTP 200, exactly `{"status":"ok"}`. Performs no external I/O; model failure does not fail liveness                                                                |
| `/readyz`  | Readiness: running lifecycle, open Store `SELECT 1` succeeds, and required ASR `/health` is healthy. HTTP 200 with `ok` or `degraded`; 503 with `not_ready`                          |
| `/metrics` | JSON gauges and service observations, always HTTP 200. `ok` means observations are complete; `partial` preserves available fields and uses `null`/`unknown` for missing observations |

Only enabled optional services failing or remaining unknown yield readiness `degraded`; ASR,
storage or lifecycle failure yields `not_ready`. Use readiness to gate transcription traffic,
and liveness to check whether the HTTP process answers. A complete observation of a failed service
can still yield metrics `ok`. Full response examples and the closed schema are in
[interfaces.md §5.8](interfaces.md#58-health-checks-and-basic-metrics); interpretation and recovery
are in [troubleshooting.md](troubleshooting.md#health-and-metrics).

The responses exclude meeting content, identifiers, URLs and credentials. Anonymous numeric gauges
still reveal load and activity. With the access password enabled, the middleware guards `/api`
and its descendants; these three root GET routes remain anonymous. Authentication changes must
retain their anonymous access without exempting business API routes. This feature does not
establish that the instance is safe to expose publicly.

## Access from other devices

Browsers allow microphone and screen capture only on HTTPS pages or on `localhost`, so other
machines on the network need a certificate. [mkcert](https://github.com/FiloSottile/mkcert) is the
simplest way to get one:

```bash
mkcert -install
mkcert -cert-file config/meeting.pem -key-file config/meeting-key.pem <server LAN IP> localhost 127.0.0.1
```

```toml
[server]
host = "0.0.0.0"
port = 443
tls_cert = "config/meeting.pem"
tls_key = "config/meeting-key.pem"
```

Other devices can now open `https://<server LAN IP>`. To avoid the certificate warning, install
`rootCA.pem` from the directory printed by `mkcert -CAROOT` as a trusted root on each device.
`*.pem` files are ignored by Git. No ICE servers are needed on a single LAN.

### Access password

Anyone who can reach the port can read and delete meeting records unless an access password is set.
On a network shared with people who should not see the meetings, set one:

```toml
[server]
password_env = "AGENTIC_MEETING_PASSWORD"
```

```bash
# .env in the repository root (ignored by Git)
AGENTIC_MEETING_PASSWORD=<a long passphrase, at least 8 characters>
```

The page then asks for the password once per browser; the login lasts `auth_session_days` (7 days by
default). **退出登录** (Log out) in the top bar ends it on that browser. Changing the password, or
deleting `data/auth_secret`, logs out every device. After 5 wrong attempts within 5 minutes a device
has to wait before trying again.

Use the password together with HTTPS: over plain HTTP it crosses the network unencrypted. The
password protects the whole application with one shared secret; per-person accounts are not
available yet.

## Enable the code sandbox

The background agent can search and read images without Docker. To let it run code — calculations
and plots — build the sandbox image and reference it in the configuration:

```bash
docker build -t agentic-meeting-sandbox:py312 docker/sandbox
```

```toml
[agent.sandbox]
kind = "docker"
docker_image = "agentic-meeting-sandbox:py312"
```

## Verify without a microphone

`scripts/soak.py` is a headless client. It connects like a browser, plays a recording at real-time
speed, calls the assistant at intervals and records latency and resource usage:

```bash
uv run python scripts/soak.py --audio meeting.mp3 --minutes 30
```

The meeting it creates appears in the web page afterwards, so you can review the transcript,
generate a report and try the exports. See [benchmarks.md](benchmarks.md) for a sample run.
