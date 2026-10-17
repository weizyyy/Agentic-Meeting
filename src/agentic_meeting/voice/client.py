"""应用一侧的声纹服务客户端（服务的接口见 ``voice/service.py`` 和 docs/interfaces.md §4.4）。"""

from __future__ import annotations

import math
from dataclasses import dataclass

import httpx

from agentic_meeting.config import SpeakerMemoryConfig

REQUEST_TIMEOUT_SECS = 5.0


class SpeakerServiceError(RuntimeError):
    """声纹服务这次没给出可用的结果（连不上、超时、报错、格式不对）。可以稍后重试。"""


@dataclass(frozen=True, slots=True)
class SpeakerModelInfo:
    dimensions: int
    model_sha256: str


class SpeakerClient:
    def __init__(
        self,
        base_url: str,
        *,
        http: httpx.AsyncClient | None = None,
        timeout_secs: float = REQUEST_TIMEOUT_SECS,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._owns_http = http is None
        # 只会是本机地址（配置校验保证），不走系统代理
        self._http = http or httpx.AsyncClient(timeout=timeout_secs, trust_env=False)
        self._info: SpeakerModelInfo | None = None

    @classmethod
    def from_config(cls, cfg: SpeakerMemoryConfig) -> SpeakerClient | None:
        """功能关闭时返回 None。"""
        return None if cfg.backend == "none" else cls(cfg.base_url)

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def info(self) -> SpeakerModelInfo:
        """模型的维度和指纹。成功一次之后缓存；服务重启换了模型要重启应用才会看到。"""
        if self._info is not None:
            return self._info
        try:
            response = await self._http.get(f"{self._base}/v1/info")
            response.raise_for_status()
            body = response.json()
            info = SpeakerModelInfo(int(body["dimensions"]), str(body["model_sha256"]))
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            raise SpeakerServiceError(f"读取声纹模型信息失败：{type(e).__name__}") from e
        if info.dimensions <= 0 or not info.model_sha256:
            raise SpeakerServiceError("声纹服务返回的模型信息无效")
        self._info = info
        return info

    async def embed(self, pcm16: bytes) -> list[float]:
        """16 kHz 单声道 16 位 PCM → 单位长度的声纹向量。"""
        info = await self.info()
        try:
            response = await self._http.post(
                f"{self._base}/v1/embed",
                content=pcm16,
                headers={"Content-Type": "application/octet-stream"},
            )
            response.raise_for_status()
            vector = [float(x) for x in response.json()["embedding"]]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            raise SpeakerServiceError(f"计算声纹失败：{type(e).__name__}") from e
        if len(vector) != info.dimensions or not all(math.isfinite(x) for x in vector):
            raise SpeakerServiceError("声纹服务返回的向量无效")
        return vector
