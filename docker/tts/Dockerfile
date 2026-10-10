# 语音合成服务的镜像：qwentts.cpp 的 tts-server（上游没有预编译包，从源码构建），见 docs/containers.md。
# 镜像里没有任何模型权重，构建和启动时也不下载：权重由用户挂载。
#
# 构建上下文是仓库根目录，先取得 qwentts.cpp 和它的 ggml 子模块：
#
#   git submodule update --init --depth 1 third_party/qwentts.cpp
#   git -C third_party/qwentts.cpp submodule update --init --depth 1
#   docker build -f docker/tts/Dockerfile --build-arg VARIANT=cuda -t agentic-meeting-tts:cuda .
#
# VARIANT 是 cpu、cuda 或 vulkan。后端开关与 runtimes.lock.toml 的 [qwentts.build] 一致；
# 其余构建参数照上游 third_party/qwentts.cpp/Dockerfile。

ARG VARIANT=cpu
ARG UBUNTU_IMAGE=ubuntu:24.04
ARG CUDA_DEVEL_IMAGE=nvidia/cuda:12.8.1-devel-ubuntu24.04
ARG CUDA_RUNTIME_IMAGE=nvidia/cuda:12.8.1-runtime-ubuntu24.04

# --------------------------------------------------------------------------- #
# 工具链：每种 VARIANT 一套，后端开关写成 CMake 初始缓存
# --------------------------------------------------------------------------- #
FROM ${UBUNTU_IMAGE} AS toolchain-cpu
RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ cmake ninja-build \
    && rm -rf /var/lib/apt/lists/*
RUN : > /opt/backend.cmake

FROM toolchain-cpu AS toolchain-vulkan
RUN apt-get update \
    && apt-get install -y --no-install-recommends libvulkan-dev glslc spirv-headers \
    && rm -rf /var/lib/apt/lists/*
RUN echo 'set(GGML_VULKAN ON CACHE BOOL "")' > /opt/backend.cmake

FROM ${CUDA_DEVEL_IMAGE} AS toolchain-cuda
RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ cmake ninja-build \
    && rm -rf /var/lib/apt/lists/*
# 构建时没有显卡，链接用 CUDA 开发镜像自带的 libcuda 桩库
RUN printf '%s\n' \
    'set(GGML_CUDA ON CACHE BOOL "")' \
    'set(CMAKE_EXE_LINKER_FLAGS "-L/usr/local/cuda/lib64/stubs -lcuda" CACHE STRING "")' \
    'set(CMAKE_SHARED_LINKER_FLAGS "-L/usr/local/cuda/lib64/stubs -lcuda" CACHE STRING "")' \
    > /opt/backend.cmake

FROM toolchain-${VARIANT} AS build
# 留空 = qwentts.cpp 自己的默认架构列表（Pascal 到 Blackwell）
ARG CUDA_ARCHITECTURES=""
WORKDIR /src
COPY third_party/qwentts.cpp/ ./
RUN test -f CMakeLists.txt -a -f ggml/CMakeLists.txt || { \
      echo "third_party/qwentts.cpp 或其 ggml 子模块是空的，先按本文件开头的命令取得源码。" >&2; exit 1; }
# GGML_NATIVE=OFF：不按构建机的 CPU 优化；CPU 部分按指令集各编一份，运行时挑本机能用的最好的一份。
RUN cmake -S . -B build -G Ninja -C /opt/backend.cmake \
        -DCMAKE_BUILD_TYPE=Release \
        -DGGML_NATIVE=OFF -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON \
        ${CUDA_ARCHITECTURES:+-DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCHITECTURES}} \
    && cmake --build build --target tts-server qwen-tts \
    && mkdir /out \
    && cp -P build/tts-server build/qwen-tts build/*.so* /out/

# --------------------------------------------------------------------------- #
# 运行环境
# --------------------------------------------------------------------------- #
FROM ${UBUNTU_IMAGE} AS base-cpu

FROM ${UBUNTU_IMAGE} AS base-vulkan
RUN apt-get update \
    && apt-get install -y --no-install-recommends libvulkan1 mesa-vulkan-drivers \
    && rm -rf /var/lib/apt/lists/*

FROM ${CUDA_RUNTIME_IMAGE} AS base-cuda
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

FROM base-${VARIANT}
ARG VARIANT

LABEL org.opencontainers.image.title="Agentic-Meeting speech synthesis" \
      org.opencontainers.image.description="qwentts.cpp tts-server for Agentic-Meeting (${VARIANT})" \
      org.opencontainers.image.source="https://github.com/weizyyy/Agentic-Meeting" \
      org.opencontainers.image.licenses="MIT"

# curl 给健康检查用
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=build /out/ ./
# 程序离开了构建目录，同目录下的 ggml 动态库要显式加进搜索路径
ENV LD_LIBRARY_PATH=/app

USER 1000:1000
EXPOSE 8080
HEALTHCHECK CMD ["curl", "-fsS", "-o", "/dev/null", "http://127.0.0.1:8080/health"]
ENTRYPOINT ["/app/tts-server"]
