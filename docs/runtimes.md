# Runtimes and models

**English** · [简体中文](zh-CN/runtimes.md)

A *runtime* is an inference program such as `llama-server`; *model files* are weights. Neither is
stored in the repository. Runtimes are fetched or built by a script. Model files are downloaded
manually — nothing in this project downloads weights.

## 1. Overview

| Runtime | Used for | How it is obtained | Pinned version |
|---|---|---|---|
| [llama.cpp](https://github.com/ggml-org/llama.cpp) | ASR, embeddings, and the realtime LLM in `llama_server` mode | Prebuilt package | v0.6.0 (build b11429) |
| [NeMo-Speech.cpp](https://github.com/NVIDIA/NeMo-Speech.cpp) | Speaker diarization, loaded as a library | Prebuilt package with C SDK | v0.2.0 |
| [qwentts.cpp](https://github.com/ServeurpersoCom/qwentts.cpp) | Speech synthesis (`tts-server`) | Built from source | commit 51512f1 |
| [Confucius4-R2T2](https://github.com/netease-youdao/Confucius4-R2T2) reference code | Reference for the streaming ASR algorithm | Source only; not built | commit 26d55a5 |

Versions are pinned in [`runtimes.lock.toml`](../runtimes.lock.toml). The sources are Git
submodules under `third_party/` (shallow, read-only).

## 2. The runtime script

```bash
python scripts/runtimes.py status                  # source and binary status of every runtime
python scripts/runtimes.py variants llama_cpp      # available prebuilt packages; the default is marked
python scripts/runtimes.py fetch llama_cpp         # download and unpack the package for this platform
python scripts/runtimes.py fetch nemo_speech
python scripts/runtimes.py build qwentts --backend cuda
python scripts/runtimes.py source qwentts          # initialize the submodule only
```

- The script uses the standard library only and runs before the virtual environment exists.
- Packages are unpacked to `runtimes/<name>/`; downloads are cached in `runtimes/_downloads/`.
- NeMo-Speech.cpp packages are verified against the published SHA-256.
- After a fresh clone the submodules are empty: run `git submodule update --init --depth 1` or the
  `source` subcommand.

## 3. Runtime notes

### 3.1 llama.cpp

- Prebuilt packages are split by CUDA major version (`cuda12`, `cuda13`). The script selects the one
  matching the installed CUDA Toolkit. With only a GPU driver installed, add `--with-extras` to fetch
  the CUDA runtime libraries as well (about 400 MB).
- macOS packages include Metal support.
- Verify the installation:

  ```bash
  runtimes/llama_cpp/llama-server --version
  runtimes/llama_cpp/llama-server --list-devices     # GPUs and their ids (CUDA0, CUDA1, …)
  ```

- The authoritative reference for command-line options is
  `third_party/llama.cpp/tools/server/README.md`, which matches the binary. The options this project
  passes are listed in [interfaces.md §9](interfaces.md).

### 3.2 NeMo-Speech.cpp

- The package contains the `nemo-speech` command-line program and a C SDK. The project uses only the
  SDK's shared library, `nemo_speech_asr_c` (on Windows, `bin/nemo_speech_asr_c.dll`), through
  ctypes; see [interfaces.md §4.2](interfaces.md).
- Header: `third_party/NeMo-Speech.cpp/include/nemo_speech/diar.h`.

> [!WARNING]
> The `nemo-speech` command-line program downloads weights automatically whenever a model is given
> by catalog name rather than by file — for example `nemo-speech diarize x.wav` without
> `--diar-model <file>`, `nemo-speech pull …`, or `nemo-speech serve --diar-model sortformer`.
> Agentic-Meeting never invokes it.

### 3.3 qwentts.cpp

Upstream publishes no prebuilt packages, so the runtime is built from source.

| Platform | Requirements |
|---|---|
| Windows | Visual Studio or Build Tools with the "Desktop development with C++" workload, CMake, Ninja; the full CUDA Toolkit for the GPU build |
| Linux | gcc/g++, CMake; the CUDA Toolkit or the Vulkan SDK for the GPU build |
| macOS | Xcode command-line tools, CMake |

```bash
python scripts/runtimes.py build qwentts --backend cuda      # or vulkan / metal / cpu
python scripts/runtimes.py build qwentts --backend cuda --clean --cmake-arg=-DCMAKE_CUDA_ARCHITECTURES=native
```

The script:

1. Initializes qwentts.cpp and its `ggml` submodule.
2. On Windows, imports the MSVC environment by locating `vcvars64.bat` through `vswhere`, so the
   build works from an ordinary terminal.
3. On Windows, before a CUDA build, compiles a minimal `.cu` file to check that `nvcc` works with the
   active MSVC toolset, and stops with an explanation if it does not.
4. Runs `cmake -S . -B build <backend flags>` and `cmake --build build --config Release`. The
   binaries (`tts-server`, `qwen-tts`, …) are placed under `third_party/qwentts.cpp/build/`.

Options: `--clean` removes an existing `build/` directory and is required when switching backends;
`--cmake-arg=<arg>` may be repeated; `--vcvars-ver <version>` selects a side-by-side MSVC toolset on
Windows. A CUDA build compiles kernels for several GPU generations by default;
`--cmake-arg=-DCMAKE_CUDA_ARCHITECTURES=native` shortens the build when the binary is used on the
same machine only.

**The CUDA Toolkit and the MSVC toolset must be compatible.** A toolkit accepts the MSVC versions
that existed when it was released. Slightly newer ones work with `-allow-unsupported-compiler`,
which the script adds automatically; much newer ones crash the `nvcc` front end. CUDA 13.0.48 with
MSVC 14.51 (the toolset shipped with Visual Studio 18) is such a combination: every `.cu` file makes
`cudafe++` exit with an access violation. The script detects this before running CMake and suggests:

1. Install the previous MSVC toolset (the Visual Studio 2022 generation, 14.4x) from the Visual
   Studio Installer and select it with `--vcvars-ver`. MSVC 14.44 with CUDA 13.0 is known to work:

   ```bash
   python scripts/runtimes.py build qwentts --backend cuda --clean --vcvars-ver 14.44 --cmake-arg=-DCMAKE_CUDA_ARCHITECTURES=native
   ```

2. Install a newer CUDA Toolkit that supports the current Visual Studio.
3. Build the CPU version: `python scripts/runtimes.py build qwentts --backend cpu --clean`.

Measured with a 0.6B custom-voice model at Q8 (environment in [benchmarks.md](benchmarks.md)):

| | GPU build | CPU build |
|---|---|---|
| Real-time factor (time ÷ audio duration) | 0.19 | about 1.1 |
| Time to first byte, streaming | 0.08 s (0.24 s for the first request after start) | about 0.2 s (about 1 s for the first) |
| GPU memory | about 2.4 GB | 0 |
| Output | 24 kHz, 16-bit, mono | same |

`tts-server` uses the lowest-numbered GPU by default. The CPU build is adequate for development;
long sentences are synthesized slightly slower than real time.

`tts-server` options: `--model`, `--codec`, `--alias`, `--host`, `--port`, `--lang`, `--max-batch`,
`--codec-chunk-dur`, `--no-fa`. Routes: `GET /health`, `GET /v1/models`, `GET /v1/audio/voices`,
`POST /v1/audio/speech`. Request and response details are at the end of
[interfaces.md §9](interfaces.md).

Speech synthesis is optional: set `tts.enabled = false` to run without it.

### 3.4 Confucius4-R2T2 reference code

This submodule is read as reference material only. It is not installed or imported; it depends on
PyTorch packages and ships a prebuilt extension for Linux, CUDA and Python 3.12 only.

| File | Relevant content |
|---|---|
| `r2t2_llama/llama_native_backend.py` | `LlamaServerClient.generate`: how requests with audio and a prefix are sent to `llama-server`; `LlamaServerStreaming`: the streaming state machine |
| `r2t2/r2t2_asr.py` | `streaming_transcribe_no_reset`: rolling window, synchronized dropping of text and audio, rollback rule |
| `ws_server.py` | `detect_and_fix_repetitions`, `detect_hallucination` |
| `README.zh.md` | Model capabilities, hotword usage, official evaluation results |

## 4. Model files

Place the following files anywhere under `models/` and reference them in `config/config.toml`. The
last column lists what the project has been tested with; other models of the same kind are
configured the same way.

| Configuration key | File | Tested with |
|---|---|---|
| `realtime_llm.llama_server.launch.model_path` | Chat model GGUF (`llama_server` mode only) | Any model with tool calling |
| `realtime_llm.llama_server.launch.mmproj_path` | Its vision projector GGUF | Leave empty and set `supports_vision = false` for text-only models |
| `asr.launch.model_path` | ASR model GGUF | Confucius4-R2T2, Q8_0 and f16 |
| `asr.launch.mmproj_path` | The ASR model's audio projector GGUF | The `mmproj` file from the same release |
| `diarization.model_path` | Diarization GGUF in NeMo-Speech.cpp format | Nemotron-3-Diarization, q8_0 |
| `tts.launch.model_path` | Talker GGUF in qwentts.cpp format | Qwen3-TTS custom-voice, 0.6B and 1.7B |
| `tts.launch.codec_path` | Tokenizer/codec GGUF | From the same release as the talker |
| `embedding.launch.model_path` | Embedding GGUF | Qwen3-Embedding 0.6B, Q8_0; set `embedding.dimensions` to the model's output size |

Notes:

- GGUF files are converted for a specific runtime and are not interchangeable: ASR uses the standard
  llama.cpp format, speech synthesis the qwentts.cpp format and diarization the NeMo-Speech.cpp
  format.
- `tts.voice` must name a voice supported by the chosen weights.
- When changing the realtime LLM, check how reasoning is disabled (`thinking = false` maps to
  `--reasoning off`; some models also need a template parameter in `extra_body`), the recommended
  sampling parameters, and whether the model accepts images.
- In `openai_api` mode no local weights are needed for the realtime LLM. Fill in the endpoint, model
  name and key variable under `[realtime_llm.openai_api]`.

Run `uv run agentic-meeting check` afterwards; it lists every file that is still missing.

## 5. Multiple GPUs

`llama-server --list-devices` prints llama.cpp's own device ids. A service is assigned to a GPU in
one of two ways:

- Add `["--device", "CUDA1"]` to `launch.extra_args` (`llama-server` only).
- Set `CUDA_VISIBLE_DEVICES` in `launch.env` (works for every child process). Its numbering follows
  `nvidia-smi` and is not necessarily the same as llama.cpp's; confirm with the load log.

Diarization runs inside the application process and is assigned with `diarization.gpu`, which uses
llama.cpp's CUDA numbering (`gpu = 0` is CUDA0). `nvidia-smi` orders devices by a different rule and
may list them in the opposite order.

Example for a 22 GB + 6 GB pair:

| llama.cpp id | Memory | Suggested services |
|---|---|---|
| CUDA0 | 22 GB | Realtime LLM, speech synthesis |
| CUDA1 | 6 GB | ASR, diarization |

```toml
[realtime_llm.llama_server.launch]
extra_args = ["--device", "CUDA0"]

[asr.launch]
extra_args = ["--device", "CUDA1"]

[diarization]
gpu = 1

[embedding.launch]
gpu_layers = "0"
extra_args = ["--pooling", "last", "--device", "none"]   # --device none keeps the server off the GPU entirely
```

## 6. Verification

With the weights in place and `config/config.toml` filled in:

```bash
uv run agentic-meeting check             # what is still missing: weight files, secrets, image name
uv run agentic-meeting services up       # start the local services and wait until they are healthy
uv run agentic-meeting services status   # from another terminal: reachability of each service
```

`services up` also verifies that the configured TTS voice exists. Logs are written to
`data/logs/<service>.log`, and a failed start prints the tail of the relevant log. The rules are
described in [interfaces.md §9](interfaces.md).

The whole pipeline can be exercised with a recording instead of a microphone:

```bash
uv run agentic-meeting serve --with-services                        # terminal 1
uv run python scripts/soak.py --audio meeting.mp3 --minutes 10      # terminal 2
uv run python scripts/eval_realtime_model.py                        # tool selection and latency of the realtime LLM
uv run python scripts/mic_check.py --wav recording.wav              # offline microphone diagnosis; no weights needed
```

`soak.py` creates a meeting that can be reviewed in the web page afterwards, calls the assistant
with synthesized speech and typed questions at intervals, and records time to first text and audio,
caption lag, memory and GPU memory. A sample run is documented in [benchmarks.md](benchmarks.md).

Tests that need real hardware are marked `gpu` and skipped by default:

```bash
AGENTIC_MEETING_TEST_WAV=dialogue.wav uv run pytest -m gpu tests/test_diar_nemo.py   # diarization with a real model
uv run pytest -m gpu tests/test_agent_runner.py -s                                   # remote model, MCP and the Docker sandbox
```

When serving the realtime LLM with llama.cpp, it can be checked by hand first:

```bash
runtimes/llama_cpp/llama-server -m <chat-model.gguf> --mmproj <projector.gguf> -a realtime \
    --port 8080 -c 32768 -np 2 --jinja --reasoning off
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/v1/chat/completions -H "Content-Type: application/json" \
    -d '{"model":"realtime","messages":[{"role":"user","content":"Introduce yourself in one sentence."}],"max_tokens":64}'
```

**Verified environments.** The full system has been run on Windows 11 with Visual Studio 18 (MSVC
14.44 toolset), CUDA Toolkit 13.0 and Docker 29. On Linux the automated test suite passes; the full
system has not been run there. macOS is untested.

## 7. Upgrading a runtime

1. Change the version and asset names in `runtimes.lock.toml`.
2. Move the submodule: `git -C third_party/<name> fetch --depth 1 origin <tag>`, check it out, then
   `git add third_party/<name>` in the repository root.
3. Run `python scripts/runtimes.py fetch <name> --force`.
4. Repeat the verification in §6 and run `uv run pytest`.
5. If an interface changed, update [interfaces.md §9](interfaces.md) and this document.

Pipecat is upgraded the same way: change the exact version in `pyproject.toml`, run `uv sync`, and
review [pipecat-notes.md](pipecat-notes.md) item by item.
