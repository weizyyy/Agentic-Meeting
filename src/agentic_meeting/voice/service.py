"""声纹服务：一个独立进程，把一段人声变成一个声纹向量（docs/interfaces.md §4.4、§9）。

单独成进程有两个原因：原生代码崩溃或一次计算偏慢都不会波及会议的事件循环和转录（硬规则 7）；
测试里也可以用假服务替换它。它只监听本机回环地址——声纹是生物特征数据，不对外提供。

接口：

* ``GET /health``：模型加载好之后返回 200。
* ``GET /v1/info``：``{"dimensions", "model_sha256", "sample_rate"}``。``model_sha256`` 是模型文件的指纹，
  换了模型的向量不能与旧向量比较。
* ``POST /v1/embed``：请求体是 16 kHz 单声道 16 位小端 PCM（``application/octet-stream``），
  返回 ``{"embedding": [...]}``，向量已归一化为单位长度。

运行：``python -m agentic_meeting.voice.service --model <模型文件> --port 8084``。
需要可选依赖 ``uv sync --extra speaker``。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import threading
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

from agentic_meeting.config import is_loopback_host

SAMPLE_RATE = 16000
MIN_REQUEST_SECS = 0.3
MAX_REQUEST_SECS = 30.0


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class Extractor:
    """包一层声纹提取器：同一时刻只算一段，向量归一化。"""

    def __init__(self, model: Path, threads: int) -> None:
        try:
            import sherpa_onnx
        except ImportError as e:
            raise SystemExit(
                "声纹服务需要可选依赖 sherpa-onnx：先运行 uv sync --extra speaker"
            ) from e
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(model), num_threads=threads, provider="cpu"
        )
        if not config.validate():
            raise SystemExit(f"声纹模型无法加载：{model}")
        try:
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        except RuntimeError as e:
            raise SystemExit(f"声纹模型无法加载：{model}（{e}）") from e
        self.dimensions = int(self._extractor.dim)
        self._lock = threading.Lock()

    def embed(self, pcm16: bytes) -> list[float]:
        samples = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        with self._lock:
            stream = self._extractor.create_stream()
            stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=samples)
            stream.input_finished()
            if not self._extractor.is_ready(stream):
                raise ValueError("这段音频太短，算不出声纹")
            vector = np.asarray(self._extractor.compute(stream), dtype=np.float64)
        norm = float(np.linalg.norm(vector))
        if not np.isfinite(norm) or norm == 0.0:
            raise ValueError("声纹模型返回了无效的向量")
        return (vector / norm).tolist()


def build_app(extractor: Any, model_sha256: str) -> FastAPI:
    """``extractor`` 只需要有 ``dimensions`` 和 ``embed(pcm16) -> list[float]``。"""
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/info")
    async def info() -> dict[str, Any]:
        return {
            "dimensions": extractor.dimensions,
            "model_sha256": model_sha256,
            "sample_rate": SAMPLE_RATE,
        }

    @app.post("/v1/embed")
    async def embed(request: Request) -> Any:
        pcm = await request.body()
        secs = len(pcm) / 2 / SAMPLE_RATE
        if len(pcm) % 2 or not MIN_REQUEST_SECS <= secs <= MAX_REQUEST_SECS:
            return JSONResponse(
                {
                    "error": f"需要 {MIN_REQUEST_SECS}–{MAX_REQUEST_SECS:.0f} 秒的 16 kHz 单声道"
                    " 16 位 PCM"
                },
                status_code=400,
            )
        try:
            vector = await asyncio.to_thread(extractor.embed, pcm)
        except ValueError as e:
            # 原因只记日志；响应里只给固定的说明
            logger.info(f"这段音频没算出声纹：{e}")
            return JSONResponse({"error": "这段音频算不出声纹"}, status_code=422)
        return {"embedding": vector}

    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m agentic_meeting.voice.service", description="声纹服务（只监听本机）"
    )
    parser.add_argument("--model", required=True, help="声纹模型文件（ONNX）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，只允许本机回环地址")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--threads", type=int, default=1, help="计算线程数")
    args = parser.parse_args(argv)
    if not is_loopback_host(args.host):
        parser.error("声纹服务只能监听本机回环地址（例如 127.0.0.1）")
    model = Path(args.model)
    if not model.is_file():
        parser.error(f"模型文件不存在：{model}")

    import uvicorn

    extractor = Extractor(model, max(1, args.threads))
    sha256 = file_sha256(model)
    logger.info(f"声纹模型已加载：{extractor.dimensions} 维，指纹 {sha256[:12]}")
    uvicorn.run(build_app(extractor, sha256), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main(sys.argv[1:])
