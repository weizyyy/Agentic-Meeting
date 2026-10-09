"""嵌入回填与语义召回（docs/interfaces.md §2.2、§2.4）。

* ``EmbeddingClient``：调嵌入服务（OpenAI 格式的 ``POST {base_url}/embeddings``）。
* ``EmbeddingWorker``：后台每隔几秒取一批还没有向量的发言，算出向量写回数据库。发言落库时不等它——
  嵌入服务不可用只是暂时不能按语义查，转录、落库、关键词召回都不受影响。失败后退避重试，不往上报错。
* ``recall``：召回的入口。关键词检索总是做；查询词的嵌入限时 ``QUERY_EMBED_TIMEOUT_SECS``（实测 CPU 上约 120 毫秒），
  超时或服务不可用就只用关键词结果——被叫到名字后的应答不能卡在这里。
  向量一路有相关度门槛（``embedding.min_similarity``，余弦相似度）：不够像的不算命中。

返回的向量维度与配置不符时报出明确的错误：那是配置问题（``embedding.dimensions`` 与模型不一致），重试没有用，
回填就此停下，等用户改配置。
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx
from loguru import logger

from agentic_meeting.config import EmbeddingConfig
from agentic_meeting.store.db import Store
from agentic_meeting.types import NamedUtterance

QUERY_EMBED_TIMEOUT_SECS = 0.3
BACKFILL_INTERVAL_SECS = 3.0
BACKFILL_BATCH = 16
BACKFILL_MAX_BACKOFF_SECS = 60.0
REQUEST_TIMEOUT_SECS = 30.0


class EmbeddingError(RuntimeError):
    """嵌入服务这次没给出可用的结果（连不上、报错、格式不对）。可以稍后重试。"""


class EmbeddingDimensionError(EmbeddingError):
    """返回的向量维度与配置不符：配置问题，重试没有用。"""


class EmbeddingClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        dimensions: int,
        query_prefix: str = "",
        min_similarity: float = 0.0,
        http: httpx.AsyncClient | None = None,
        timeout_secs: float = REQUEST_TIMEOUT_SECS,
    ) -> None:
        self._url = base_url.rstrip("/") + "/embeddings"
        self._model = model
        self._dimensions = dimensions
        self._query_prefix = query_prefix
        self._min_similarity = min_similarity
        self._owns_http = http is None
        # 本机地址不走系统代理（与其他本地服务一致）
        self._http = http or httpx.AsyncClient(timeout=timeout_secs, trust_env=False)

    @classmethod
    def from_config(cls, cfg: EmbeddingConfig, **kwargs: Any) -> EmbeddingClient:
        return cls(
            base_url=cfg.base_url,
            model=cfg.model,
            dimensions=cfg.dimensions,
            query_prefix=cfg.query_prefix,
            min_similarity=cfg.min_similarity,
            **kwargs,
        )

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def max_distance(self) -> float | None:
        """相关度门槛换算成向量表里的距离上限；没设门槛返回 ``None``。

        向量都归一化成了单位长度，欧氏距离 d 与余弦相似度 s 的关系是 d² = 2 − 2s。
        """
        if self._min_similarity <= 0:
            return None
        return math.sqrt(max(0.0, 2.0 - 2.0 * self._min_similarity))

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """按输入顺序返回每段文字的向量。"""
        if not texts:
            return []
        try:
            response = await self._http.post(
                self._url, json={"model": self._model, "input": list(texts)}
            )
            response.raise_for_status()
            items = response.json()["data"]
            ordered = sorted(items, key=lambda item: item["index"])
            vectors = [[float(x) for x in item["embedding"]] for item in ordered]
        except httpx.HTTPStatusError as e:
            raise EmbeddingError(f"嵌入服务返回 {e.response.status_code}") from e
        except httpx.HTTPError as e:
            raise EmbeddingError(f"连不上嵌入服务：{type(e).__name__}") from e
        except (KeyError, TypeError, ValueError) as e:
            raise EmbeddingError("嵌入服务的响应格式不对") from e
        if len(vectors) != len(texts):
            raise EmbeddingError(f"嵌入服务返回了 {len(vectors)} 个向量，应当是 {len(texts)} 个")
        for vector in vectors:
            if len(vector) != self._dimensions:
                raise EmbeddingDimensionError(
                    f"嵌入模型输出的向量是 {len(vector)} 维，与配置的 embedding.dimensions = "
                    f"{self._dimensions} 不一致。请把配置改成 {len(vector)}"
                    "（已有数据的话需要另指一个数据目录，向量表的维度建库后不能改）。"
                )
        # 归一化成单位长度：相关度门槛按余弦相似度定，靠这一步才能换算成向量表里的欧氏距离。
        # （嵌入服务通常已经归一化过，这里再做一次没有副作用。）
        return [_unit(vector) for vector in vectors]

    async def embed_query(self, text: str) -> list[float]:
        """查询的向量：前面加上配置的任务指令（被检索的发言不加）。"""
        return (await self.embed([self._query_prefix + text]))[0]


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm > 0 else vector


class EmbeddingWorker:
    """后台回填。``start()`` / ``stop()`` 管理任务；``run_once()`` 处理一批，测试可以直接调。"""

    def __init__(
        self,
        store: Store,
        client: EmbeddingClient,
        *,
        interval_secs: float = BACKFILL_INTERVAL_SECS,
        batch_size: int = BACKFILL_BATCH,
        max_backoff_secs: float = BACKFILL_MAX_BACKOFF_SECS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._sleep = sleep
        self._store = store
        self._client = client
        self._interval = interval_secs
        self._batch = batch_size
        self._max_backoff = max_backoff_secs
        self._task: asyncio.Task | None = None
        self._failing = False
        self.disabled_reason: str | None = None  # 维度不符之类的配置问题：停止回填

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="embedding-backfill")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def run_once(self) -> int:
        """取一批没有向量的发言，算向量、写回。返回写入的条数；嵌入服务出错时抛 ``EmbeddingError``。"""
        pending = await self._store.unembedded_utterances(self._batch)
        if not pending:
            return 0
        vectors = await self._client.embed([text for _, text in pending])
        return await self._store.set_embeddings(
            [(uid, vec) for (uid, _), vec in zip(pending, vectors, strict=True)]
        )

    async def _loop(self) -> None:
        delay = self._interval
        while True:
            try:
                written = await self.run_once()
            except EmbeddingDimensionError as e:
                self.disabled_reason = str(e)
                logger.error(f"嵌入回填已停止：{e}")
                return
            except EmbeddingError as e:
                if not self._failing:  # 一次故障只记一行，恢复时再记一行
                    logger.warning(f"嵌入服务暂时不可用，稍后重试（按语义召回暂时用不了）：{e}")
                self._failing = True
                delay = min(self._max_backoff, max(self._interval, delay * 2))
            except Exception:
                logger.exception("嵌入回填出错")
                delay = min(self._max_backoff, max(self._interval, delay * 2))
            else:
                if self._failing:
                    logger.info("嵌入服务已恢复")
                self._failing = False
                delay = self._interval
                if written >= self._batch:
                    continue  # 还有积压：不等，接着处理下一批
            await self._sleep(delay)


async def recall(
    store: Store,
    embedder: EmbeddingClient | None,
    session_id: str,
    *,
    query: str | None = None,
    embed_timeout_secs: float = QUERY_EMBED_TIMEOUT_SECS,
    **filters: Any,
) -> list[NamedUtterance]:
    """召回的入口：关键词 + （来得及的话）向量。``filters`` 原样交给 ``Store.recall``。"""
    vector: list[float] | None = None
    text = (query or "").strip()
    if text and embedder is not None:
        try:
            vector = await asyncio.wait_for(embedder.embed_query(text), embed_timeout_secs)
        except TimeoutError:
            logger.debug("查询的嵌入超时，这次只用关键词召回")
        except EmbeddingError as e:
            logger.debug(f"查询的嵌入失败，这次只用关键词召回：{e}")
    return await store.recall(
        session_id,
        query=query,
        query_vector=vector,
        max_distance=embedder.max_distance if embedder is not None else None,
        **filters,
    )
