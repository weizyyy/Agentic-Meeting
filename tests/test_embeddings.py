"""嵌入回填与语义召回。嵌入服务用 ``httpx.MockTransport`` 假扮，向量是手工设计的 4 维。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from loguru import logger
from waiting import wait_until

from agentic_meeting.store import embeddings as module
from agentic_meeting.store.db import Store, pack_vector
from agentic_meeting.store.embeddings import (
    EmbeddingClient,
    EmbeddingDimensionError,
    EmbeddingError,
    EmbeddingWorker,
    recall,
)
from agentic_meeting.types import Utterance

# 每段文字对应的向量：按「主题」分到 4 个方向上
TOPICS = {
    "学习率": [1.0, 0.0, 0.0, 0.0],
    "调小": [0.9, 0.1, 0.0, 0.0],
    "截止": [0.0, 1.0, 0.0, 0.0],
    "服务器": [0.0, 0.0, 1.0, 0.0],
}
DEFAULT = [0.0, 0.0, 0.0, 1.0]


def vector_for(text: str) -> list[float]:
    for key, vec in TOPICS.items():
        if key in text:
            return vec
    return DEFAULT


class FakeService:
    """假的嵌入服务：记录请求；可以设成报错、乱序返回、返回错误的维度。"""

    def __init__(self):
        self.requests: list[dict] = []
        self.fail_status: int | None = None
        self.raise_error: Exception | None = None
        self.dims = 4
        self.shuffle = False
        self.drop_one = False
        self.delay = 0.0
        self.body_override = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append({"url": str(request.url), **payload})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raise_error is not None:
            raise self.raise_error
        if self.fail_status is not None:
            return httpx.Response(self.fail_status, json={"error": "boom"})
        if self.body_override is not None:
            return httpx.Response(200, json=self.body_override)
        data = [
            {"index": i, "embedding": (vector_for(text) + [0.0] * 8)[: self.dims]}
            for i, text in enumerate(payload["input"])
        ]
        if self.shuffle:
            data.reverse()
        if self.drop_one:
            data = data[1:]
        return httpx.Response(200, json={"data": data})

    def client(self, **kw) -> EmbeddingClient:
        kw.setdefault("dimensions", 4)
        return EmbeddingClient(
            base_url="http://embed.test/v1/",
            model="fake-embedding",
            http=httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
            **kw,
        )


@pytest.fixture
def service():
    return FakeService()


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    yield s
    await s.close()


async def add(store, session_id, text, t, speaker=1):
    u = Utterance(session_id, speaker, t, t + 1, text)
    await store.add_utterance(u)
    return u.id


# --------------------------------------------------------------------------- #
# EmbeddingClient
# --------------------------------------------------------------------------- #


async def test_embed_request_format_and_order(service):
    service.shuffle = True  # 服务乱序返回也要按 index 排回来
    client = service.client()
    vectors = await client.embed(["学习率是多少", "服务器重启了", "别的"])
    assert vectors == [TOPICS["学习率"], TOPICS["服务器"], DEFAULT]
    assert service.requests == [
        {
            "url": "http://embed.test/v1/embeddings",
            "model": "fake-embedding",
            "input": ["学习率是多少", "服务器重启了", "别的"],
        }
    ]
    assert await client.embed([]) == []
    assert len(service.requests) == 1  # 空输入不发请求


async def test_query_prefix_only_applies_to_queries(service):
    client = service.client(query_prefix="指令：检索相关发言\n查询：")
    await client.embed_query("学习率")
    await client.embed(["学习率"])
    assert service.requests[0]["input"] == ["指令：检索相关发言\n查询：学习率"]
    assert service.requests[1]["input"] == ["学习率"]


async def test_from_config_uses_configured_values(make_cfg, service):
    cfg = make_cfg()
    cfg.embedding.query_prefix = "Q: "
    cfg.embedding.dimensions = 4
    cfg.embedding.base_url = "http://embed.test/v1"
    client = EmbeddingClient.from_config(
        cfg.embedding, http=httpx.AsyncClient(transport=httpx.MockTransport(service.handle))
    )
    assert await client.embed_query("截止日期") == TOPICS["截止"]
    assert service.requests[0]["model"] == cfg.embedding.model
    assert service.requests[0]["input"] == ["Q: 截止日期"]
    assert client.dimensions == 4


def test_query_prefix_defaults_to_empty(make_cfg):
    assert make_cfg().embedding.query_prefix == ""


async def test_dimension_mismatch_is_a_clear_error(service):
    service.dims = 3
    with pytest.raises(EmbeddingDimensionError) as e:
        await service.client().embed(["学习率"])
    message = str(e.value)
    assert "3 维" in message and "embedding.dimensions = 4" in message


@pytest.mark.parametrize(
    "setup",
    [
        lambda s: setattr(s, "fail_status", 503),
        lambda s: setattr(s, "raise_error", httpx.ConnectError("refused")),
        lambda s: setattr(s, "body_override", {"oops": 1}),
        lambda s: setattr(s, "body_override", {"data": [{"index": 0}]}),
        lambda s: setattr(s, "body_override", {"data": [{"index": 0, "embedding": ["x"] * 4}]}),
        lambda s: setattr(s, "drop_one", True),
    ],
)
async def test_service_problems_become_embedding_error(service, setup):
    setup(service)
    with pytest.raises(EmbeddingError) as e:
        await service.client().embed(["a", "b"])
    assert not isinstance(e.value, EmbeddingDimensionError)


# --------------------------------------------------------------------------- #
# 回填
# --------------------------------------------------------------------------- #


async def test_backfill_writes_vectors_and_marks_embedded(store, service):
    s = await store.create_session()
    ids = [await add(store, s.id, t, i) for i, t in enumerate(["学习率调了", "服务器坏了", "别的"])]
    assert [i for i, _ in await store.unembedded_utterances()] == ids
    worker = EmbeddingWorker(store, service.client(), batch_size=2)
    assert await worker.run_once() == 2
    assert [i for i, _ in await store.unembedded_utterances()] == ids[2:]
    assert await worker.run_once() == 1
    assert await worker.run_once() == 0
    assert len(service.requests) == 2  # 没有积压时不调服务
    # 向量真的写进去了：用同方向的查询能找回来
    rows = await store._all(
        "SELECT rowid FROM utterances_vec WHERE embedding MATCH ? AND k = 1 ORDER BY distance",
        (pack_vector(TOPICS["服务器"]),),
    )
    assert rows[0][0] == ids[1]


async def test_backfill_skips_utterances_deleted_meanwhile(store, service):
    s = await store.create_session()
    kept = await add(store, s.id, "学习率", 0)
    gone = Utterance(s.id, 1, 1, 2, "服务器")
    await store.add_utterance(gone)
    assert await store.set_embeddings([(kept, TOPICS["学习率"]), (99999, DEFAULT)]) == 1
    # 重复写同一条不报错（vec0 不支持 INSERT OR REPLACE）
    assert await store.set_embeddings([(kept, TOPICS["调小"])]) == 1


async def test_backfill_error_leaves_rows_pending(store, service):
    s = await store.create_session()
    await add(store, s.id, "学习率", 0)
    service.fail_status = 500
    worker = EmbeddingWorker(store, service.client())
    with pytest.raises(EmbeddingError):
        await worker.run_once()
    assert len(await store.unembedded_utterances()) == 1
    service.fail_status = None
    assert await worker.run_once() == 1


def capture_logs(level="DEBUG"):
    lines: list[tuple[str, str]] = []
    sink = logger.add(
        lambda m: lines.append((m.record["level"].name, m.record["message"])), level=level
    )
    return lines, sink


async def test_loop_retries_with_backoff_and_logs_outage_once(store, service):
    s = await store.create_session()
    await add(store, s.id, "学习率", 0)
    service.raise_error = httpx.ConnectError("refused")
    lines, sink = capture_logs("INFO")
    worker = EmbeddingWorker(store, service.client(), interval_secs=0.01, max_backoff_secs=0.04)
    worker.start()
    worker.start()  # 重复启动无害
    try:
        await wait_until(lambda: len(service.requests) >= 4)
        assert len(await store.unembedded_utterances()) == 1
        service.raise_error = None
        await wait_until(lambda: any("已恢复" in m for _, m in lines))
        assert await store.unembedded_utterances() == []
    finally:
        await worker.stop()
        logger.remove(sink)
    warnings = [m for level, m in lines if level == "WARNING"]
    assert len(warnings) == 1 and "稍后重试" in warnings[0]  # 一次故障只记一行
    assert worker.disabled_reason is None
    await worker.stop()  # 重复停止无害


async def test_loop_backoff_grows_and_is_capped(store, service):
    s = await store.create_session()
    await add(store, s.id, "学习率", 0)
    service.fail_status = 500
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(secs):
        delays.append(secs)
        if len(delays) >= 6:
            service.fail_status = None
        await real_sleep(0)

    worker = EmbeddingWorker(
        store, service.client(), interval_secs=1.0, max_backoff_secs=5.0, sleep=fake_sleep
    )
    worker.start()
    try:
        await wait_until(lambda: len(delays) >= 8, description="嵌入退避完成八次")
    finally:
        await worker.stop()
    assert delays[:6] == [2.0, 4.0, 5.0, 5.0, 5.0, 5.0]
    assert delays[6] == 1.0  # 恢复后回到正常间隔


async def test_loop_drains_backlog_without_waiting(store, service):
    s = await store.create_session()
    for i in range(5):
        await add(store, s.id, f"学习率 {i}", i)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(secs):
        sleeps.append(secs)
        await real_sleep(0.01)

    worker = EmbeddingWorker(
        store, service.client(), interval_secs=9.0, batch_size=2, sleep=fake_sleep
    )
    worker.start()
    try:
        await wait_until(lambda: len(sleeps) >= 1)
    finally:
        await worker.stop()
    # 2 + 2 + 1：前两批是满的，不等就接着处理；第三批不满才睡
    assert [len(r["input"]) for r in service.requests[:3]] == [2, 2, 1]
    assert sleeps[0] == 9.0


async def test_loop_stops_for_good_on_dimension_mismatch(store, service):
    s = await store.create_session()
    await add(store, s.id, "学习率", 0)
    service.dims = 3
    lines, sink = capture_logs("ERROR")
    worker = EmbeddingWorker(store, service.client(), interval_secs=0.01)
    worker.start()
    try:
        await wait_until(lambda: worker.disabled_reason is not None)
        await asyncio.sleep(0.05)
    finally:
        await worker.stop()
        logger.remove(sink)
    assert len(service.requests) == 1  # 配置问题，不再重试
    assert "embedding.dimensions" in worker.disabled_reason
    assert any("嵌入回填已停止" in m for _, m in lines)
    assert len(await store.unembedded_utterances()) == 1


# --------------------------------------------------------------------------- #
# 语义召回
# --------------------------------------------------------------------------- #


async def filled(store, service, lines):
    """建一场会议，写入发言并全部回填。返回 (会话编号, [发言编号])。"""
    s = await store.create_session()
    ids = [
        await add(store, s.id, text, float(i * 10), speaker)
        for i, (speaker, text) in enumerate(lines)
    ]
    worker = EmbeddingWorker(store, service.client(), batch_size=100)
    await worker.run_once()
    return s.id, ids


LINES = [
    (1, "我把 baseline 的学习率降了一半"),
    (2, "下个月十五号截止，摘要提前一周交"),
    (1, "服务器上周五重启过一次"),
    (2, "把步长调小以后收敛慢了"),
    (1, "中午吃什么"),
]


def texts(items):
    return [n.utterance.text for n in items]


async def test_semantic_recall_finds_what_keywords_miss(store, service):
    sid, _ = await filled(store, service, LINES)
    client = service.client()
    # 「调小」只出现在第 4 句，「学习率」只出现在第 1 句；查询是一整句话，关键词（整句短语）一条都匹配不上
    question = "谁提到过把学习率调小"
    assert await store.recall(sid, query=question) == []
    got = await recall(store, client, sid, query=question, limit=2)
    assert texts(got) == ["我把 baseline 的学习率降了一半", "把步长调小以后收敛慢了"]
    # 只要最接近的一条
    got = await recall(store, client, sid, query=question, limit=1)
    assert texts(got) == ["我把 baseline 的学习率降了一半"]


async def test_semantic_and_keyword_results_are_merged_without_duplicates(store, service):
    sid, _ = await filled(store, service, LINES)
    client = service.client()
    # 关键词「服务器」命中第 3 句；向量也把它排第一——只出现一次；结果按时间升序
    got = await recall(store, client, sid, query="服务器", limit=3)
    assert texts(got).count("服务器上周五重启过一次") == 1
    assert [n.utterance.t_start for n in got] == sorted(n.utterance.t_start for n in got)
    assert len(got) == 3


async def test_merge_alternates_so_neither_side_starves(store, service):
    s = await store.create_session()
    # 6 句关键词命中「周报」但语义上是别的；1 句语义相关但不含关键词
    for i in range(6):
        await add(store, s.id, f"周报第 {i} 条", float(i))
    await add(store, s.id, "把学习率调小试试", 100.0)
    await EmbeddingWorker(store, service.client(), batch_size=100).run_once()

    class Fixed(EmbeddingClient):
        async def embed_query(self, text):
            return TOPICS["学习率"]

    client = Fixed(base_url="http://x", model="fake-embedding", dimensions=4)
    got = await recall(store, client, s.id, query="周报", limit=4, vector_k=1)
    await client.close()
    # 关键词 3 条（最近的）+ 向量 1 条
    assert texts(got) == ["周报第 3 条", "周报第 4 条", "周报第 5 条", "把学习率调小试试"]


async def test_vector_search_respects_filters_and_session(store, service):
    sid, _ = await filled(store, service, LINES)
    other, _ = await filled(store, service, [(1, "另一场会议里的学习率讨论")])
    client = service.client()
    got = await recall(store, client, sid, query="学习率怎么调", limit=10, vector_k=10)
    assert "另一场会议里的学习率讨论" not in texts(got)
    assert len(got) == len(LINES)  # k 够大时这场会议的都能回来，但不越界
    by_speaker = await recall(store, client, sid, query="学习率怎么调", speaker_idx=2, vector_k=10)
    assert {n.utterance.speaker_idx for n in by_speaker} == {2}
    by_time = await recall(store, client, sid, query="学习率怎么调", t_from=15.0, t_to=25.0)
    assert texts(by_time) == ["服务器上周五重启过一次"]
    assert await recall(store, client, other, query="学习率怎么调", t_from=999.0) == []


async def test_vector_search_ignores_rows_not_yet_embedded(store, service):
    sid, _ = await filled(store, service, LINES[:2])
    await add(store, sid, "学习率又调了一次", 500.0)  # 还没回填
    got = await recall(store, service.client(), sid, query="关于学习率的讨论", vector_k=1)
    assert texts(got) == ["我把 baseline 的学习率降了一半"]


async def test_vector_k_is_capped_by_limit_and_default(store, service):
    s = await store.create_session()
    for i in range(12):
        await add(store, s.id, f"学习率实验 {i:02d}", float(i))
    await EmbeddingWorker(store, service.client(), batch_size=100).run_once()
    client = service.client()
    question = "学习率相关的实验有哪些呢"
    assert len(await recall(store, client, s.id, query=question, limit=20)) == 8  # 默认最多 8 条
    assert len(await recall(store, client, s.id, query=question, limit=3)) == 3
    assert await recall(store, client, s.id, query=question, vector_k=0) == []


async def test_recall_without_query_does_not_call_embedding_service(store, service):
    sid, _ = await filled(store, service, LINES)
    before = len(service.requests)
    got = await recall(store, service.client(), sid, limit=2)
    assert texts(got) == ["把步长调小以后收敛慢了", "中午吃什么"]  # 最近两条
    assert await recall(store, service.client(), sid, query="   ", limit=1)
    assert len(service.requests) == before


async def test_recall_falls_back_to_keywords_when_embedding_fails_or_is_slow(store, service):
    sid, _ = await filled(store, service, LINES)
    client = service.client()
    service.fail_status = 503
    assert texts(await recall(store, client, sid, query="服务器")) == ["服务器上周五重启过一次"]
    service.fail_status = None
    service.delay = 0.5
    started = asyncio.get_running_loop().time()
    got = await recall(store, client, sid, query="服务器", embed_timeout_secs=0.05)
    assert asyncio.get_running_loop().time() - started < 0.4
    assert texts(got) == ["服务器上周五重启过一次"]
    # 没有嵌入客户端（配置里关了）也一样
    assert texts(await recall(store, None, sid, query="服务器")) == ["服务器上周五重启过一次"]


async def test_app_backfills_in_background_with_injected_embedder(make_cfg, tmp_path, service):
    from agentic_meeting.web.app import create_app

    store = await Store.open(tmp_path / "app.db", 4, assistant_name="Nova")
    s = await store.create_session()
    await add(store, s.id, "学习率", 0)
    client = service.client()
    app = create_app(make_cfg(), store=store, static_dir=tmp_path / "nope", embedder=client)
    async with app.router.lifespan_context(app):
        assert app.state.resources.embedder is client
        await wait_until(lambda: len(service.requests) >= 1)
        await wait_until_async(store)
    assert await store.unembedded_utterances() == []
    await store.close()


async def wait_until_async(store):
    async def completed():
        return not await store.unembedded_utterances()

    await wait_until(completed, description="嵌入回填完成")


async def test_app_does_no_embedding_when_only_store_is_injected(make_cfg, tmp_path):
    from agentic_meeting.web.app import create_app

    store = await Store.open(tmp_path / "app.db", 4, assistant_name="Nova")
    app = create_app(make_cfg(), store=store, static_dir=tmp_path / "nope")
    async with app.router.lifespan_context(app):
        assert app.state.resources.embedder is None
    await store.close()


async def test_default_query_timeout_matches_documented_budget():
    assert module.QUERY_EMBED_TIMEOUT_SECS == 0.3


# --------------------------------------------------------------------------- #
# 相关度门槛
# --------------------------------------------------------------------------- #


def test_min_similarity_is_configurable_and_defaults_to_an_empirical_value(make_cfg):
    cfg = make_cfg()
    assert cfg.embedding.min_similarity == 0.4
    client = EmbeddingClient.from_config(cfg.embedding)
    assert client.max_distance == pytest.approx((2 - 2 * 0.4) ** 0.5)
    cfg.embedding.min_similarity = 0.0
    assert EmbeddingClient.from_config(cfg.embedding).max_distance is None


async def test_vectors_are_normalized_to_unit_length(service):
    service.body_override = {
        "data": [
            {"index": 0, "embedding": [3.0, 4.0, 0.0, 0.0]},
            {"index": 1, "embedding": [0.0] * 4},
        ]
    }
    first, zero = await service.client().embed(["a", "b"])
    assert first == pytest.approx([0.6, 0.8, 0.0, 0.0])
    assert zero == [0.0, 0.0, 0.0, 0.0]  # 零向量原样返回，不除以零


async def test_similarity_threshold_drops_unrelated_hits(store, service):
    sid, _ = await filled(store, service, LINES)
    question = "谁提到过把学习率调小"
    loose = await recall(store, service.client(), sid, query=question, vector_k=10)
    assert len(loose) == len(LINES)  # 不设门槛：最近的 k 条都回来，包括「中午吃什么」
    strict = await recall(
        store, service.client(min_similarity=0.4), sid, query=question, vector_k=10
    )
    assert texts(strict) == ["我把 baseline 的学习率降了一半", "把步长调小以后收敛慢了"]
    # 门槛只管向量一路：关键词命中的照常返回
    by_keyword = await recall(store, service.client(min_similarity=0.99), sid, query="吃什么")
    assert texts(by_keyword) == ["中午吃什么"]
    # 没有一条够像：空结果，而不是硬凑
    nothing = await recall(
        store, service.client(min_similarity=0.4), sid, query="关于截止日期的事", t_from=15.0
    )
    assert nothing == []


async def test_threshold_boundary_is_inclusive(store, service):
    s = await store.create_session()
    uid = await add(store, s.id, "别的", 0.0)
    await store.set_embeddings(
        [(uid, [0.6, 0.8, 0.0, 0.0])]
    )  # 与查询 [1,0,0,0] 的余弦相似度正好 0.6
    exact = (2 - 2 * 0.6) ** 0.5
    q = [1.0, 0.0, 0.0, 0.0]
    question = "一句匹配不上关键词的话"
    assert (
        len(await store.recall(s.id, query=question, query_vector=q, max_distance=exact + 1e-6))
        == 1
    )
    assert await store.recall(s.id, query=question, query_vector=q, max_distance=exact - 1e-3) == []
