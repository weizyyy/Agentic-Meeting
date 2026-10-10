# Agentic-Meeting 的应用镜像：应用、网页客户端和说话人区分的动态库（说话人区分在应用进程里运行）。
# 推理服务各有各的镜像，由 compose.yaml 组合，见 docs/containers.md。
#
# 镜像里没有任何模型权重，构建和启动时也不下载：权重由用户挂载到 /app/models。
#
# 构建上下文是仓库根目录：
#
#   docker build -f docker/app/Dockerfile --build-arg VARIANT=cuda -t agentic-meeting:cuda .
#
# VARIANT 是 cpu、cuda 或 vulkan。NeMo-Speech.cpp 用 runtimes.lock.toml 里钉住的预编译包
# （scripts/runtimes.py fetch），与本机安装取的是同一份；CUDA 版的包自带 CUDA 运行库，只需要宿主机的显卡驱动。

ARG VARIANT=cpu
ARG UBUNTU_IMAGE=ubuntu:24.04
ARG NODE_IMAGE=node:24-bookworm-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32

FROM ${UV_IMAGE} AS uv

# --------------------------------------------------------------------------- #
# 网页客户端
# --------------------------------------------------------------------------- #
FROM ${NODE_IMAGE} AS client
WORKDIR /src/client
COPY client/package.json client/package-lock.json ./
RUN npm ci
COPY client/ ./
RUN npm run build

# --------------------------------------------------------------------------- #
# 运行环境
# --------------------------------------------------------------------------- #
FROM ${UBUNTU_IMAGE} AS base-cpu

FROM ${UBUNTU_IMAGE} AS base-vulkan
RUN apt-get update \
    && apt-get install -y --no-install-recommends libvulkan1 mesa-vulkan-drivers \
    && rm -rf /var/lib/apt/lists/*

FROM ${UBUNTU_IMAGE} AS base-cuda
# NVIDIA Container Toolkit 按这两个变量把显卡和驱动挂进容器
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

FROM base-${VARIANT} AS app
ARG VARIANT

LABEL org.opencontainers.image.title="Agentic-Meeting" \
      org.opencontainers.image.description="Self-hosted voice assistant for research group meetings: application (${VARIANT})" \
      org.opencontainers.image.source="https://github.com/weizyyy/Agentic-Meeting" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3.12 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_PYTHON=/usr/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1

# 依赖单独一层：只改源码时不必重装
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=bind,from=uv,source=/uv,target=/usr/local/bin/uv \
    uv sync --locked --no-dev --extra agent --no-install-project

# 说话人区分的动态库，解压到 runtimes/nemo_speech/（与本机运行 scripts/runtimes.py fetch 的结果相同）
COPY runtimes.lock.toml ./
COPY scripts/runtimes.py scripts/
RUN set -eu; \
    case "$VARIANT" in \
      cpu | cuda | vulkan) ;; \
      *) echo "未知的 VARIANT：$VARIANT（可选 cpu、cuda、vulkan）" >&2; exit 1 ;; \
    esac; \
    python3 scripts/runtimes.py fetch nemo_speech --variant "linux-x64-$VARIANT"; \
    rm -rf runtimes/_downloads

COPY --from=client /src/client/dist/ client/dist/

COPY config/config.example.toml config/
COPY config/asr_profiles/ config/asr_profiles/
COPY config/prompts/ config/prompts/
COPY scripts/ scripts/
COPY src/ src/
RUN --mount=type=bind,from=uv,source=/uv,target=/usr/local/bin/uv \
    uv sync --locked --no-dev --extra agent

# 以基础镜像自带的 ubuntu 用户（uid 1000）运行。会议数据写在 /app/data，平时挂成宿主机的 data/。
RUN mkdir data && chown 1000:1000 data
USER 1000:1000
EXPOSE 7860
ENTRYPOINT ["agentic-meeting"]
CMD ["serve"]
