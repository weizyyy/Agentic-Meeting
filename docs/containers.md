# Containers

**English** · [简体中文](zh-CN/containers.md)

The whole system can run with Docker Compose on a Linux machine with Docker, and a GPU if you have
one. Each inference service runs in its own container, so you can start only the ones you need, move
one to another machine, or replace one with your own image. Model weights stay on the host: no
image contains weights, and nothing downloads them at start.

- [1. What runs where](#1-what-runs-where)
- [2. Requirements](#2-requirements)
- [3. First start](#3-first-start)
- [4. Choosing and combining services](#4-choosing-and-combining-services)
- [5. Images](#5-images)
- [6. Code sandbox](#6-code-sandbox)
- [7. macOS and Windows](#7-macos-and-windows)
- [8. Operation and troubleshooting](#8-operation-and-troubleshooting)

## 1. What runs where

| Service in `compose.yaml` | Image                                            | Port on the host          | GPU (CUDA override) | Started by default         |
| ------------------------- | ------------------------------------------------ | ------------------------- | ------------------- | -------------------------- |
| `asr`                     | llama.cpp's own `llama-server` image             | `127.0.0.1:8081`          | Yes                 | Yes                        |
| `realtime`                | llama.cpp's own `llama-server` image             | `127.0.0.1:8080`          | Yes                 | Profile `realtime`         |
| `tts`                     | `agentic-meeting-tts` (qwentts.cpp `tts-server`) | `127.0.0.1:8082`          | Yes                 | Profile `tts`              |
| `embedding`               | llama.cpp's own `llama-server` image (CPU)       | `127.0.0.1:8083`          | No                  | Profile `embedding`        |
| `app`                     | `agentic-meeting` (application, web client)      | `server.port` (7860), all | Yes (diarization)   | Yes                        |
| `asr-template`            | `agentic-meeting`; writes the ASR chat template  | —                         | No                  | Yes, exits when it is done |

- The inference services listen on port 8080 inside their containers and are published on the
  host's loopback addresses at the ports that `config/config.toml` uses by default. Other machines
  cannot reach them, as with a native installation ([architecture.md §2](architecture.md)).
- Their command lines are the ones `services up` builds ([interfaces.md §9](interfaces.md)). The
  ASR server needs the chat template of `asr.profile`: the one-shot `asr-template` container writes
  it from your `config.toml` into a volume before `asr` starts.
- `app` uses the host network. WebRTC sends media over a random UDP port per connection
  ([deployment.md §1](deployment.md#1-what-has-to-be-reachable)), which cannot be published from a
  bridge network. This is why the application container runs on Linux only (§7).
- Speaker diarization is a library loaded into the application process, so the `app` container
  needs the GPU too.
- `app` starts once `asr` is healthy, and waits for the optional services that are started.

## 2. Requirements

| Requirement                                                                                                         | Notes                                                                           |
| ------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| Linux, x86-64                                                                                                       | Images are built for `linux/amd64`                                              |
| Docker Engine 25 or newer with Compose 2.24 or newer                                                                | `docker compose version`                                                        |
| For NVIDIA GPUs: the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/) | A driver that supports CUDA 12.8 (570 or newer); the host needs no CUDA Toolkit |
| For AMD and Intel GPUs: a Vulkan driver on the host                                                                 | Experimental (§5)                                                               |
| The model files                                                                                                     | As for a native installation: [runtimes.md §4](runtimes.md#4-model-files)       |

A clone of the repository holds `compose.yaml` and the files it mounts. Python, Node.js, uv and the
runtimes do not need to be installed on the host.

## 3. First start

```bash
git clone https://github.com/weizyyy/Agentic-Meeting.git
cd Agentic-Meeting
cp config/config.example.toml config/config.toml
cp .env.example .env
mkdir -p data
```

1. **Model files.** Place them under `models/`, as for a native installation.
2. **`config/config.toml`.** Fill it in as described in [getting started](getting-started.md#configure).
   Model paths are relative to the repository root (`models/…`); absolute paths outside `models/`
   are not visible in the containers. The `base_url` defaults already point to the containers. On a
   CPU-only machine set `diarization.gpu = -1`.
3. **`.env`.** Besides the secrets, it holds the variables Compose reads (below). Uncomment the
   container section of `.env.example` and fill in the model files in the same form as in
   `config.toml`.
4. **`data/`** must be writable by the user the containers run as, uid 1000 by default. If your
   user has another uid, set `AGENTIC_MEETING_UID` and `AGENTIC_MEETING_GID` in `.env` (`id -u`,
   `id -g`).

Then start everything:

```bash
docker compose up -d                                                # CPU only
docker compose -f compose.yaml -f docker/compose.cuda.yaml up -d    # NVIDIA GPU
```

To avoid typing the file list, put it in `.env`:
`COMPOSE_FILE=compose.yaml:docker/compose.cuda.yaml`. Open <http://localhost:7860>; for other
devices, set up HTTPS as in [getting started](getting-started.md#access-from-other-devices) or the
[deployment guide](deployment.md).

Variables in `.env` that Compose reads:

| Variable                                                                              | Default                                    | Meaning                                                                             |
| ------------------------------------------------------------------------------------- | ------------------------------------------ | ----------------------------------------------------------------------------------- |
| `COMPOSE_PROFILES`                                                                    | —                                          | Optional services to start: `realtime`, `tts`, `embedding`, comma-separated         |
| `COMPOSE_FILE`                                                                        | `compose.yaml`                             | Files to combine, separated by `:`                                                  |
| `ASR_MODEL`, `ASR_MMPROJ`                                                             | —                                          | ASR model and its audio projector, e.g. `models/asr/….gguf`                         |
| `ASR_CTX_SIZE`, `ASR_GPU_LAYERS`                                                      | `8192`, `all`                              | `asr.launch.ctx_size` and `gpu_layers`                                              |
| `REALTIME_MODEL`, `REALTIME_MMPROJ`                                                   | —                                          | Realtime LLM and its vision projector; leave `REALTIME_MMPROJ` empty without vision |
| `REALTIME_CTX_SIZE`, `REALTIME_PARALLEL`, `REALTIME_GPU_LAYERS`, `REALTIME_REASONING` | `32768`, `2`, `all`, `off`                 | The corresponding `realtime_llm.llama_server` settings                              |
| `TTS_MODEL`, `TTS_CODEC`, `TTS_LANGUAGE`                                              | —, —, `Chinese`                            | Talker and codec GGUF, `tts.launch.default_language`                                |
| `EMBEDDING_MODEL`, `EMBEDDING_POOLING`                                                | —, `last`                                  | Embedding GGUF and its pooling                                                      |
| `AGENTIC_MEETING_UID`, `AGENTIC_MEETING_GID`                                          | `1000`                                     | User of the application container                                                   |
| `AGENTIC_MEETING_TAG`, `AGENTIC_MEETING_TTS_TAG`                                      | `cpu`, or the variant of the override file | Image tags, for example `0.3.0-cuda` to stay on a release                           |
| `LLAMA_SERVER_TAG`, `LLAMA_SERVER_CUDA_TAG`, `LLAMA_SERVER_VULKAN_TAG`                | the locked build                           | Tags of the llama.cpp images                                                        |

The request names `realtime`, `tts` and `embedding` passed with `-a` and `--alias` match the
defaults of `realtime_llm.llama_server.model`, `tts.model` and `embedding.model`. If you change
those keys, change the command in `compose.yaml` too. Options that are not variables — for example
`launch.extra_args` — go into the service's `command` in a `compose.override.yaml`.

`check` runs inside the application container:

```bash
docker compose run --rm --no-deps app check
```

## 4. Choosing and combining services

- **Only some services.** Name them: `docker compose up -d asr tts app`. Optional services that are
  not started make the application degrade as usual
  ([architecture.md §9](architecture.md)); it still starts.
- **A remote realtime LLM.** With `realtime_llm.mode = "openai_api"` leave `realtime` out of
  `COMPOSE_PROFILES`.
- **Mixing CPU and GPU.** In the CUDA override, embeddings stay on the CPU. To move another service
  to the CPU, set its tag back, for example `AGENTIC_MEETING_TTS_TAG=cpu`.
- **Several GPUs.** Replace `count: all` with `device_ids: ["1"]` for a service in a
  `compose.override.yaml` (numbering as in `nvidia-smi`), or add `--device` to its `command`
  ([runtimes.md §5](runtimes.md#5-multiple-gpus)). Diarization is assigned with `diarization.gpu`.
- **A service on another machine or your own image.** Point its `base_url` in `config.toml` there and
  do not start it here. A service you run yourself needs the same command line, including, for ASR,
  `--chat-template-file` with the template `asr-template` writes (or `services up` writes to
  `data/run/asr_chat_template.jinja`).
- **Inference in containers, application on the host.** Start only the inference services
  (`docker compose up -d asr tts embedding`) and run `uv run agentic-meeting serve` on the host, without
  `--with-services`. This is the route for macOS and Windows (§7).

## 5. Images

| Image                                     | Tags                                                          | Contents                                                               |
| ----------------------------------------- | ------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `ghcr.io/weizyyy/agentic-meeting`         | `cpu`, `cuda`, `vulkan`; `<version>-<variant>`                | Application, built web client, NeMo-Speech.cpp library for diarization |
| `ghcr.io/weizyyy/agentic-meeting-tts`     | `cpu`, `cuda`, `vulkan`; `<version>-<variant>`                | `tts-server` and `qwen-tts`, built from the pinned qwentts.cpp source  |
| `ghcr.io/weizyyy/agentic-meeting-sandbox` | `latest`; `<version>`                                         | Python with numpy, pandas and matplotlib for the code sandbox          |
| `ghcr.io/ggml-org/llama.cpp` (upstream)   | `server-b11429`, `server-cuda-b11429`, `server-vulkan-b11429` | `llama-server` at the build pinned in `runtimes.lock.toml`             |

The release workflow publishes the three images of this project to GHCR for every version; the tags
without a version follow the latest release. The application and speech synthesis images use the
runtime versions in `runtimes.lock.toml` and the submodules, like a native installation. The CUDA
images are based on CUDA 12.8; the speech synthesis image is compiled for GPU generations from Pascal
to Blackwell.

CI builds every image on each change that can affect it, except the CUDA build of speech synthesis,
which is built on release, and starts the programs in the CPU and Vulkan images. None of the images
has yet been run with real models; the Vulkan variant is experimental.

To build them yourself (needed for a commit that is not a release):

```bash
git submodule update --init --depth 1 third_party/qwentts.cpp
git -C third_party/qwentts.cpp submodule update --init --depth 1
docker compose -f compose.yaml -f docker/compose.cuda.yaml build
```

or one image at a time:

```bash
docker build -f docker/app/Dockerfile --build-arg VARIANT=cuda -t ghcr.io/weizyyy/agentic-meeting:cuda .
docker build -f docker/tts/Dockerfile --build-arg VARIANT=cuda -t ghcr.io/weizyyy/agentic-meeting-tts:cuda .
```

A CUDA build of speech synthesis takes a while; `--build-arg CUDA_ARCHITECTURES=89` builds for a
single GPU generation only. Only the listed files enter the build context
(`docker/*/Dockerfile.dockerignore`): `.env`, `config/config.toml`, certificates, `models/` and
`data/` never do.

## 6. Code sandbox

The background agent runs code in a separate container per task. When the application itself runs
in a container, it has to reach a container runtime. Compose uses the host's Docker for this, through
its socket: the sandbox containers run next to the application container, and the workspace is copied
in and out through the Docker API, so no paths need to match. Running Docker inside the application
container instead would need a privileged container, which gives away as much.

Access to the Docker socket is equivalent to root on the host. It is therefore opt-in:

```bash
# .env
AGENTIC_MEETING_DOCKER_GID=<gid of the docker group: getent group docker | cut -d: -f3>
COMPOSE_FILE=compose.yaml:docker/compose.cuda.yaml:docker/compose.sandbox.yaml
```

```toml
[agent.sandbox]
kind = "docker"
docker_image = "ghcr.io/weizyyy/agentic-meeting-sandbox:latest"
```

Pull the sandbox image once with `docker pull ghcr.io/weizyyy/agentic-meeting-sandbox:latest`. Without
the override file the agent still searches and reads images, and says that it cannot run code
([troubleshooting](troubleshooting.md)).

## 7. macOS and Windows

The application container needs host networking for WebRTC, which Docker Desktop does not provide in
a way that browsers on the network can reach. On these systems run the application natively
([getting started](getting-started.md)) and, if you like, the inference services in containers:

- **Windows** with Docker Desktop and the WSL 2 backend can give containers an NVIDIA GPU. Start the
  inference services with the CUDA override (`docker compose -f compose.yaml -f
docker/compose.cuda.yaml up -d asr tts embedding`, plus `realtime` if needed) and run the
  application with `uv run agentic-meeting serve`. This combination has not been verified yet.
- **macOS** has no GPU passthrough to containers. Install natively: the prebuilt runtimes support
  Metal (`python scripts/runtimes.py fetch …`, `build qwentts --backend metal`). The CPU images also
  run there, much more slowly.

## 8. Operation and troubleshooting

```bash
docker compose ps                    # state and health of every container
docker compose logs -f asr           # the log of one service
docker compose pull && docker compose up -d   # update to the latest images
docker compose down                  # stop everything; data/ and models/ stay on the host
```

`/healthz`, `/readyz` and `/metrics` work as without containers
([getting started](getting-started.md#check-health-and-metrics)).

**`bind source path does not exist`.** `config/config.toml`, `.env` or `data/` is missing; create
them as in §3.

**The application logs `Permission denied` under `/app/data`.** `data/` is not writable by the
container user; see `AGENTIC_MEETING_UID` in §3.

**`asr` stays unhealthy or restarts.** `docker compose logs asr` shows the reason; usually
`ASR_MODEL` or `ASR_MMPROJ` is empty or points outside `models/`. Loading may take several minutes;
the health check allows ten.

**`could not select device driver "nvidia"`.** The NVIDIA Container Toolkit is not installed or
Docker was not restarted after installing it.

**Compose warns that a variable is not set, naming part of a password.** Compose also reads `.env`
and expands `$` in its values. Put such values in single quotes; the application reads the file
itself and is not affected.

**The page connects but there is no audio or captions from another device.** As without containers:
UDP must reach the host, or use TURN ([deployment.md §4](deployment.md#4-stun-and-turn)).
