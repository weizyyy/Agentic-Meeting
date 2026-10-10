"""存储层（store/db.py）：临时目录里的真实 SQLite 文件，不用任何假实现。"""

from __future__ import annotations

import asyncio
import sqlite3
import struct

import pytest

from agentic_meeting.store.db import Store, StoreError, default_speaker_name
from agentic_meeting.types import (
    SPEAKER_ASSISTANT,
    SPEAKER_TYPED,
    SPEAKER_UNKNOWN,
    Utterance,
)

DIMS = 4


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "meetings.db", DIMS, assistant_name="Nova")
    yield s
    await s.close()


def utt(session_id, text, *, t=0.0, dur=2.0, speaker=1, source="asr", addressed=False):
    return Utterance(
        session_id=session_id,
        speaker_idx=speaker,
        t_start=t,
        t_end=t + dur,
        text=text,
        source=source,
        addressed_to_assistant=addressed,
    )


# --------------------------------------------------------------------------- #
# 建库
# --------------------------------------------------------------------------- #


async def test_availability_check_reads_only_constant_without_changing_data(store):
    session = await store.create_session("虚构会议")
    uid = await store.add_utterance(utt(session.id, "虚构发言"))
    before = await store._all("SELECT name FROM sqlite_master WHERE type = 'table'")
    counts = {row[0]: (await store._one(f'SELECT COUNT(*) FROM "{row[0]}"'))[0] for row in before}
    changes = store._db.total_changes
    queries = []
    await store._db.set_trace_callback(queries.append)
    await store.check_available()
    await store._db.set_trace_callback(None)
    assert queries == ["SELECT 1"] and store._db.total_changes == changes
    assert counts == {
        row[0]: (await store._one(f'SELECT COUNT(*) FROM "{row[0]}"'))[0] for row in before
    }
    assert (await store.get_session(session.id)).title == "虚构会议"
    assert (await store._one("SELECT text FROM utterances WHERE id = ?", (uid,)))[0] == "虚构发言"


async def test_availability_check_closed_connection_fails(tmp_path):
    closed = await Store.open(tmp_path / "closed.db", DIMS)
    await closed.close()
    with pytest.raises(ValueError, match="no active connection"):
        await closed.check_available()


async def test_availability_check_preserves_query_failure_and_cancellation(store, monkeypatch):
    def fail(sql, *args):
        assert sql == "SELECT 1"
        raise sqlite3.OperationalError("虚构数据库错误")

    monkeypatch.setattr(store._db, "execute", fail)
    with pytest.raises(sqlite3.OperationalError):
        await store.check_available()

    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def slow(sql):
        assert sql == "SELECT 1"
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(store, "_one", slow)
    task = asyncio.create_task(store.check_available())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


async def test_open_twice_is_idempotent(tmp_path):
    path = tmp_path / "sub" / "meetings.db"  # 目录不存在也能建
    first = await Store.open(path, DIMS)
    session = await first.create_session("甲")
    await first.close()
    second = await Store.open(path, DIMS)
    assert (await second.get_session(session.id)).title == "甲"  # 重开不丢数据
    await second.close()


async def test_pragmas_and_schema_version(store):
    async with store._db.execute("PRAGMA journal_mode") as c:
        assert (await c.fetchone())[0].lower() == "wal"
    async with store._db.execute("PRAGMA foreign_keys") as c:
        assert (await c.fetchone())[0] == 1
    async with store._db.execute("SELECT value FROM meta WHERE key = 'schema_version'") as c:
        assert (await c.fetchone())[0] == "1"


async def test_vector_table_has_the_configured_dimension(store):
    s = await store.create_session()
    uid = await store.add_utterance(utt(s.id, "向量测试"))
    blob = struct.pack(f"{DIMS}f", 0.1, 0.2, 0.3, 0.4)
    await store._db.execute(
        "INSERT INTO utterances_vec(rowid, embedding) VALUES (?, ?)", (uid, blob)
    )
    async with store._db.execute(
        "SELECT rowid FROM utterances_vec WHERE embedding MATCH ? AND k = 1", (blob,)
    ) as c:
        assert (await c.fetchone())[0] == uid
    with pytest.raises(sqlite3.Error):
        bad = struct.pack("3f", 0.1, 0.2, 0.3)  # 维度不对
        await store._db.execute(
            "INSERT INTO utterances_vec(rowid, embedding) VALUES (?, ?)", (99, bad)
        )


async def test_reopening_with_another_dimension_is_refused_without_touching_the_table(tmp_path):
    path = tmp_path / "meetings.db"
    await (await Store.open(path, 4)).close()
    with pytest.raises(StoreError, match="维度"):
        await Store.open(path, 8)
    reopened = await Store.open(path, 4)  # 被拒绝时没有删表，原来的维度照常可用
    await reopened.close()


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #


async def test_create_get_end_reopen_touch(store):
    s = await store.create_session("组会", now=1000.0)
    assert len(s.id) == 32 and int(s.id, 16) >= 0
    assert (s.title, s.started_at, s.ended_at, s.last_active_at) == ("组会", 1000.0, None, 1000.0)
    assert await store.get_session(s.id) == s
    assert await store.get_session("nope") is None

    ended = await store.end_session(s.id, now=1100.0)
    assert ended.ended_at == 1100.0
    again = await store.end_session(s.id, now=1200.0)
    assert again.ended_at == 1100.0  # 重复结束不改写第一次的时间

    reopened = await store.reopen_session(s.id)
    assert reopened.ended_at is None
    await store.touch_session(s.id, now=1300.0)
    assert (await store.get_session(s.id)).last_active_at == 1300.0
    assert await store.end_session("nope") is None
    assert await store.reopen_session("nope") is None


async def test_latest_unended_session_is_the_most_recently_active_one(store):
    assert await store.latest_unended_session() is None
    a = await store.create_session("a", now=100.0)
    b = await store.create_session("b", now=200.0)
    assert (await store.latest_unended_session()).id == b.id
    await store.touch_session(a.id, now=300.0)
    assert (await store.latest_unended_session()).id == a.id
    await store.end_session(a.id, now=310.0)
    assert (await store.latest_unended_session()).id == b.id
    await store.end_session(b.id, now=320.0)
    assert await store.latest_unended_session() is None


async def test_end_unended_sessions_stops_them_at_their_last_activity(store):
    done = await store.create_session("早就结束了", now=100.0)
    await store.end_session(done.id, now=150.0)
    a = await store.create_session("a", now=200.0)
    await store.touch_session(a.id, now=260.0)
    b = await store.create_session("b", now=300.0)

    assert await store.end_unended_sessions() == 2
    assert (await store.get_session(done.id)).ended_at == 150.0  # 已结束的不动
    assert (await store.get_session(a.id)).ended_at == 260.0  # 取最后一次活动，而不是现在
    assert (await store.get_session(b.id)).ended_at == 300.0
    assert await store.latest_unended_session() is None
    assert await store.end_unended_sessions() == 0


async def test_rename_session(store):
    s = await store.create_session()
    assert (await store.rename_session(s.id, "新标题")).title == "新标题"
    assert (await store.get_session(s.id)).title == "新标题"
    assert await store.rename_session("nope", "x") is None


# --------------------------------------------------------------------------- #
# 会议列表与摘要
# --------------------------------------------------------------------------- #


async def test_list_sessions_orders_by_last_activity_and_pages_with_before(store):
    ids = []
    for i in range(5):
        s = await store.create_session(f"会{i}", now=100.0 * (i + 1))
        ids.append(s.id)
    await store.touch_session(ids[0], now=900.0)  # 最旧的一场刚刚活跃过

    listed = await store.list_sessions(limit=3)
    assert [x.session.id for x in listed] == [ids[0], ids[4], ids[3]]

    page2 = await store.list_sessions(limit=3, before=listed[-1].session.last_active_at)
    assert [x.session.id for x in page2] == [ids[2], ids[1]]


async def test_summary_counts_speakers_preview_and_duration(store):
    s = await store.create_session("组会", now=1000.0)
    await store.add_utterance(utt(s.id, "第一句", t=0, speaker=1), now=1001.0)
    await store.add_utterance(utt(s.id, "第二句", t=3, speaker=2), now=1004.0)
    await store.add_utterance(
        utt(s.id, "助理的话", t=6, speaker=SPEAKER_ASSISTANT, source="assistant"), now=1007.0
    )
    await store.rename_speaker(s.id, 2, "王老师")
    c1 = await store.open_connection(s.id, connected_at=1000.0, t_from=0.0)
    await store.close_connection(c1, disconnected_at=1010.0, t_to=10.0)
    c2 = await store.open_connection(s.id, connected_at=2000.0, t_from=1000.0)
    await store.close_connection(c2, disconnected_at=2005.0, t_to=1005.0)

    (summary,) = await store.list_sessions()
    assert summary.utterance_count == 3
    assert summary.speakers == ["说话人 1", "王老师"]  # 说话的人，不含助理
    assert summary.preview == [("王老师", "第二句"), ("Nova", "助理的话")]  # 最后两条
    assert summary.duration_secs == 15.0  # 10 + 5，不含中间中断的空档
    assert summary.has_open_connection is False


async def test_summary_flags_an_unclosed_connection_and_counts_it_up_to_last_activity(store):
    s = await store.create_session(now=1000.0)
    await store.open_connection(s.id, connected_at=1000.0, t_from=0.0)
    await store.touch_session(s.id, now=1030.0)
    (summary,) = await store.list_sessions()
    assert summary.has_open_connection is True
    assert summary.duration_secs == 30.0


async def test_summary_counts_an_open_connection_up_to_now_when_given(store):
    s = await store.create_session(now=1000.0)
    await store.open_connection(s.id, connected_at=1000.0, t_from=0.0)
    (summary,) = await store.list_sessions(now=1075.0)
    assert summary.duration_secs == 75.0  # 正在进行的会议：算到现在
    assert (await store.get_summary(s.id, now=1090.0)).duration_secs == 90.0


async def test_summary_of_an_empty_session(store):
    await store.create_session("空", now=1.0)
    (summary,) = await store.list_sessions()
    assert (summary.utterance_count, summary.speakers, summary.preview) == (0, [], [])
    assert summary.duration_secs == 0.0


async def test_get_summary_for_one_session(store):
    s = await store.create_session("单个", now=5.0)
    summary = await store.get_summary(s.id)
    assert summary.session.id == s.id
    assert await store.get_summary("nope") is None


# --------------------------------------------------------------------------- #
# 删除
# --------------------------------------------------------------------------- #


async def test_delete_session_removes_everything_under_it(store):
    a = await store.create_session("a", now=1.0)
    b = await store.create_session("b", now=2.0)
    uid = await store.add_utterance(utt(a.id, "要删除的验证集讨论", speaker=1))
    keep = await store.add_utterance(utt(b.id, "留下来的验证集讨论", speaker=1))
    blob = struct.pack(f"{DIMS}f", 1, 0, 0, 0)
    for rid in (uid, keep):
        await store._db.execute(
            "INSERT INTO utterances_vec(rowid, embedding) VALUES (?, ?)", (rid, blob)
        )
    await store.open_connection(a.id, connected_at=1.0, t_from=0.0)

    assert await store.delete_session(a.id) is True
    assert await store.get_session(a.id) is None
    assert await store.list_utterances(a.id) == []
    assert await store.list_speakers(a.id) == []
    assert await store.list_connections(a.id) == []
    async with store._db.execute("SELECT rowid FROM utterances_vec") as c:
        assert [r[0] for r in await c.fetchall()] == [keep]  # 向量也清掉了，别的会话的还在
    # 全文索引同步清掉：删掉的那条搜不到，留下的还能搜到
    found = await store.recall(b.id, query="验证集")
    assert [x.utterance.text for x in found] == ["留下来的验证集讨论"]
    async with store._db.execute(
        "SELECT count(*) FROM utterances_fts WHERE utterances_fts MATCH '\"要删除\"'"
    ) as c:
        assert (await c.fetchone())[0] == 0

    assert await store.delete_session(a.id) is False


# --------------------------------------------------------------------------- #
# 连接记录
# --------------------------------------------------------------------------- #


async def test_connections_roundtrip(store):
    s = await store.create_session(now=1000.0)
    c1 = await store.open_connection(s.id, connected_at=1000.0, t_from=0.0)
    listed = await store.list_connections(s.id)
    assert [(x.id, x.disconnected_at, x.t_to) for x in listed] == [(c1, None, None)]
    await store.close_connection(c1, disconnected_at=1050.0, t_to=50.0)
    c2 = await store.open_connection(s.id, connected_at=1200.0, t_from=200.0)
    listed = await store.list_connections(s.id)
    assert [(x.id, x.connected_at, x.t_from, x.disconnected_at, x.t_to) for x in listed] == [
        (c1, 1000.0, 0.0, 1050.0, 50.0),
        (c2, 1200.0, 200.0, None, None),
    ]


async def test_close_dangling_connections_after_a_crash(store):
    s = await store.create_session(now=1000.0)
    await store.add_utterance(utt(s.id, "崩溃前最后一句", t=40.0, dur=5.0), now=1045.0)
    await store.open_connection(s.id, connected_at=1000.0, t_from=0.0)
    done = await store.open_connection(s.id, connected_at=900.0, t_from=0.0)
    await store.close_connection(done, disconnected_at=950.0, t_to=50.0)

    assert await store.close_dangling_connections() == 1
    by_id = {x.connected_at: x for x in await store.list_connections(s.id)}
    closed = by_id[1000.0]
    assert closed.disconnected_at == 1045.0  # 取会话最后一次活动的时间
    assert closed.t_to == 45.0  # 取最后一条发言的结束时间
    assert by_id[900.0].disconnected_at == 950.0  # 本来就关好的不动
    assert await store.close_dangling_connections() == 0


# --------------------------------------------------------------------------- #
# 说话人
# --------------------------------------------------------------------------- #


def test_default_speaker_names():
    assert default_speaker_name(3, "Nova") == "说话人 3"
    assert default_speaker_name(SPEAKER_UNKNOWN, "Nova") == "未知"
    assert default_speaker_name(SPEAKER_ASSISTANT, "Nova") == "Nova"
    assert default_speaker_name(SPEAKER_TYPED, "Nova") == "文字输入"


async def test_ensure_speaker_creates_once_and_keeps_renames(store):
    s = await store.create_session()
    assert (await store.ensure_speaker(s.id, 1)).display_name == "说话人 1"
    assert (await store.ensure_speaker(s.id, SPEAKER_ASSISTANT)).display_name == "Nova"
    assert await store.rename_speaker(s.id, 1, "王老师") is True
    assert (await store.ensure_speaker(s.id, 1)).display_name == "王老师"  # 不覆盖改过的名字
    assert (await store.ensure_speaker(s.id, 2, "李同学")).display_name == "李同学"
    assert [(x.idx, x.display_name) for x in await store.list_speakers(s.id)] == [
        (-1, "Nova"),
        (1, "王老师"),
        (2, "李同学"),
    ]


async def test_speaker_name_takes_effect_immediately_and_falls_back_to_the_default(store):
    s = await store.create_session()
    await store.ensure_speaker(s.id, 1)
    await store.rename_speaker(s.id, 1, "王老师")
    assert await store.speaker_name(s.id, 1) == "王老师"
    assert await store.speaker_name(s.id, 7) == "说话人 7"  # 还没出现过：给默认名，不建记录
    assert await store.speaker_name(s.id, SPEAKER_TYPED) == "文字输入"
    assert [x.idx for x in await store.list_speakers(s.id)] == [1]


async def test_rename_unknown_speaker_or_blank_name(store):
    s = await store.create_session()
    assert await store.rename_speaker(s.id, 9, "谁") is False
    await store.ensure_speaker(s.id, 1)
    with pytest.raises(ValueError):
        await store.rename_speaker(s.id, 1, "   ")


# --------------------------------------------------------------------------- #
# 发言
# --------------------------------------------------------------------------- #


async def test_add_utterance_roundtrip_and_side_effects(store):
    s = await store.create_session(now=100.0)
    uid = await store.add_utterance(
        utt(s.id, "你好", t=1.5, dur=2.0, speaker=2, addressed=True), now=105.0
    )
    (row,) = await store.list_utterances(s.id)
    u = row.utterance
    assert (u.id, u.session_id, u.speaker_idx, u.t_start, u.t_end, u.text) == (
        uid, s.id, 2, 1.5, 3.5, "你好",
    )  # fmt: skip
    assert (u.source, u.addressed_to_assistant) == ("asr", True)
    assert row.speaker_name == "说话人 2"
    assert (await store.get_session(s.id)).last_active_at == 105.0
    assert [x.idx for x in await store.list_speakers(s.id)] == [
        2
    ]  # 说话人记录自动建好，之后才能改名


async def test_text_and_assistant_sources(store):
    s = await store.create_session()
    await store.add_utterance(
        utt(s.id, "打字提问", speaker=SPEAKER_TYPED, source="text", addressed=True)
    )
    await store.add_utterance(utt(s.id, "助理回答", speaker=SPEAKER_ASSISTANT, source="assistant"))
    rows = await store.list_utterances(s.id)
    assert [(r.utterance.source, r.speaker_name) for r in rows] == [
        ("text", "文字输入"),
        ("assistant", "Nova"),
    ]


async def test_unknown_source_is_rejected_by_the_schema(store):
    s = await store.create_session()
    with pytest.raises(sqlite3.IntegrityError):
        await store.add_utterance(utt(s.id, "x", source="tv"))


async def test_update_utterance_speaker(store):
    s = await store.create_session()
    uid = await store.add_utterance(utt(s.id, "一句话", speaker=0))
    await store.update_utterance_speaker(uid, 3)
    (row,) = await store.list_utterances(s.id)
    assert (row.utterance.speaker_idx, row.speaker_name) == (3, "说话人 3")
    assert [x.idx for x in await store.list_speakers(s.id)] == [0, 3]
    assert await store.update_utterance_speaker(99999, 1) is False


async def _fill(store, n=10):
    s = await store.create_session()
    ids = [await store.add_utterance(utt(s.id, f"第{i}句", t=float(i))) for i in range(n)]
    return s, ids


async def test_list_utterances_paging_modes(store):
    s, ids = await _fill(store, 10)
    texts = lambda rows: [r.utterance.text for r in rows]  # noqa: E731

    assert texts(await store.list_utterances(s.id, limit=3)) == ["第0句", "第1句", "第2句"]
    assert texts(await store.list_utterances(s.id, after_id=ids[6])) == ["第7句", "第8句", "第9句"]
    assert texts(await store.list_utterances(s.id, after_id=ids[6], limit=2)) == ["第7句", "第8句"]
    assert texts(await store.list_utterances(s.id, tail=4)) == ["第6句", "第7句", "第8句", "第9句"]
    assert texts(await store.list_utterances(s.id, before_id=ids[5], limit=3)) == [
        "第2句",
        "第3句",
        "第4句",
    ]
    assert texts(await store.list_utterances(s.id, tail=50)) == [f"第{i}句" for i in range(10)]
    assert await store.list_utterances(s.id, after_id=ids[-1]) == []


async def test_list_utterances_modes_are_mutually_exclusive(store):
    s, ids = await _fill(store, 3)
    with pytest.raises(ValueError):
        await store.list_utterances(s.id, after_id=ids[0], tail=2)
    with pytest.raises(ValueError):
        await store.list_utterances(s.id, before_id=ids[2], after_id=ids[0])
    with pytest.raises(ValueError):
        await store.list_utterances(s.id, tail=0)


async def test_list_utterances_is_scoped_to_one_session(store):
    a, _ = await _fill(store, 2)
    b = await store.create_session()
    await store.add_utterance(utt(b.id, "别的会议"))
    assert len(await store.list_utterances(a.id)) == 2
    assert [r.utterance.text for r in await store.list_utterances(b.id)] == ["别的会议"]


async def test_concurrent_writes_all_land(store):
    s = await store.create_session()
    ids = await asyncio.gather(
        *(
            store.add_utterance(utt(s.id, f"并发{i}", t=float(i), speaker=i % 3 + 1))
            for i in range(40)
        )
    )
    assert len(set(ids)) == 40
    assert len(await store.list_utterances(s.id, limit=100)) == 40


# --------------------------------------------------------------------------- #
# 召回
# --------------------------------------------------------------------------- #


@pytest.fixture
async def recall_session(store):
    s = await store.create_session(now=0.0)
    rows = [
        (1, 10.0, "我们先看一下验证集的划分方式"),
        (2, 40.0, "验证集太小了，应该重新划分"),
        (1, 70.0, "好的，我改成五折交叉验证"),
        (3, 100.0, "引用数那篇论文我去核对"),
        (2, 130.0, "A/B 测试的结果也要补上"),
    ]
    for speaker, t, text in rows:
        await store.add_utterance(utt(s.id, text, t=t, speaker=speaker))
    await store.rename_speaker(s.id, 2, "王老师")
    return s


def texts(items):
    return [x.utterance.text for x in items]


async def test_recall_without_a_query_returns_the_latest_in_time_order(store, recall_session):
    s = recall_session
    assert len(await store.recall(s.id)) == 5
    last_two = await store.recall(s.id, limit=2)
    assert texts(last_two) == [
        "引用数那篇论文我去核对",
        "A/B 测试的结果也要补上",
    ]  # 最近的 N 条，仍按时间升序


async def test_recall_by_speaker_index_and_by_display_name(store, recall_session):
    s = recall_session
    assert texts(await store.recall(s.id, speaker_idx=2)) == [
        "验证集太小了，应该重新划分",
        "A/B 测试的结果也要补上",
    ]
    by_name = await store.recall(s.id, speaker_name="王老师")
    assert texts(by_name) == texts(await store.recall(s.id, speaker_idx=2))
    assert all(x.speaker_name == "王老师" for x in by_name)
    assert await store.recall(s.id, speaker_name="不存在的人") == []


async def test_recall_by_time_range(store, recall_session):
    s = recall_session
    assert texts(await store.recall(s.id, t_from=40.0, t_to=100.0)) == [
        "验证集太小了，应该重新划分",
        "好的，我改成五折交叉验证",
        "引用数那篇论文我去核对",
    ]
    assert texts(await store.recall(s.id, t_from=125.0)) == ["A/B 测试的结果也要补上"]


async def test_recall_keyword_of_three_or_more_chars_uses_full_text_search(store, recall_session):
    s = recall_session
    assert texts(await store.recall(s.id, query="验证集")) == [
        "我们先看一下验证集的划分方式",
        "验证集太小了，应该重新划分",
    ]
    assert texts(await store.recall(s.id, query="交叉验证")) == ["好的，我改成五折交叉验证"]


async def test_recall_keyword_of_one_or_two_chars_falls_back_to_like(store, recall_session):
    s = recall_session
    assert texts(await store.recall(s.id, query="论文")) == ["引用数那篇论文我去核对"]
    assert texts(await store.recall(s.id, query="五")) == ["好的，我改成五折交叉验证"]


async def test_recall_combines_query_with_speaker_and_range(store, recall_session):
    s = recall_session
    assert texts(await store.recall(s.id, query="验证集", speaker_idx=2)) == [
        "验证集太小了，应该重新划分"
    ]
    assert await store.recall(s.id, query="验证集", t_from=50.0) == []


async def test_recall_multiple_terms_match_any(store, recall_session):
    s = recall_session
    assert texts(await store.recall(s.id, query="论文 测试")) == [
        "引用数那篇论文我去核对",
        "A/B 测试的结果也要补上",
    ]


async def test_recall_survives_characters_that_are_special_to_the_index(store, recall_session):
    s = recall_session
    for q in [
        '"',
        '验证集"',
        "A/B",
        "NEAR(",
        "* OR",
        "验证集'; DROP TABLE utterances;--",
        "%",
        "_",
    ]:
        await store.recall(s.id, query=q)  # 不抛异常即可
    assert texts(await store.recall(s.id, query="A/B")) == ["A/B 测试的结果也要补上"]
    # LIKE 的通配符 % 和 _ 按字面处理：数据里没有这两个字符，就什么都匹配不到
    assert await store.recall(s.id, query="%") == []
    assert await store.recall(s.id, query="_") == []
    assert len(await store.list_utterances(s.id)) == 5  # 表还在


async def test_recall_is_scoped_to_the_session(store, recall_session):
    other = await store.create_session()
    await store.add_utterance(utt(other.id, "另一场会议里也提到验证集"))
    assert texts(await store.recall(recall_session.id, query="验证集")) == [
        "我们先看一下验证集的划分方式",
        "验证集太小了，应该重新划分",
    ]


# --------------------------------------------------------------------------- #
# 合并说话人、最大编号、时间轴终点
# --------------------------------------------------------------------------- #


async def test_merge_speakers_moves_everything_and_removes_the_source(store):
    s = await store.create_session("会")
    other = await store.create_session("别的会")
    await store.add_utterance(utt(s.id, "甲一", t=0.0, speaker=1))
    await store.add_utterance(utt(s.id, "丙一", t=3.0, speaker=3))
    await store.add_utterance(utt(s.id, "丙二", t=6.0, speaker=3))
    await store.add_utterance(utt(other.id, "别的会的 3 号", speaker=3))
    await store.add_utterance(utt(other.id, "别的会的 1 号", speaker=1))
    await store.rename_speaker(s.id, 1, "王老师")
    task = await store.create_task(s.id, goal="丙交办的", requested_by=3, requested_t=4.0)

    assert await store.merge_speakers(s.id, 3, 1) == 2
    rows = await store.all_utterances(s.id)
    assert [(r.utterance.speaker_idx, r.speaker_name) for r in rows] == [(1, "王老师")] * 3
    assert [x.idx for x in await store.list_speakers(s.id)] == [1]
    assert (await store.get_task(task.id)).requested_by == 1
    # 别的会议不受影响
    assert [r.utterance.speaker_idx for r in await store.all_utterances(other.id)] == [3, 1]
    assert await store.max_speaker_idx(other.id) == 3


async def test_merge_speakers_refuses_what_cannot_be_merged(store):
    s = await store.create_session("会")
    await store.add_utterance(utt(s.id, "甲", speaker=1))
    await store.add_utterance(utt(s.id, "助理的话", speaker=SPEAKER_ASSISTANT, source="assistant"))
    await store.add_utterance(utt(s.id, "未知", speaker=SPEAKER_UNKNOWN))
    for src, dst in ((1, 1), (1, 2), (2, 1), (SPEAKER_UNKNOWN, 1), (1, SPEAKER_ASSISTANT)):
        assert await store.merge_speakers(s.id, src, dst) is None
    assert len(await store.list_speakers(s.id)) == 3  # 什么都没动
    assert await store.merge_speakers("不存在", 1, 2) is None


async def test_max_speaker_idx_counts_only_diarized_speakers(store):
    s = await store.create_session("会")
    assert await store.max_speaker_idx(s.id) == 0
    await store.add_utterance(utt(s.id, "打字", speaker=SPEAKER_TYPED, source="text"))
    assert await store.max_speaker_idx(s.id) == 0
    await store.add_utterance(utt(s.id, "乙", speaker=2))
    await store.ensure_speaker(s.id, 4)  # 出现过但还没有落库的发言
    assert await store.max_speaker_idx(s.id) == 4


async def test_timeline_end_is_the_latest_of_connections_and_utterances(store):
    s = await store.create_session("会", now=1000.0)
    assert await store.timeline_end(s.id) == 0.0
    first = await store.open_connection(s.id, connected_at=1000.0, t_from=0.0)
    await store.close_connection(first, disconnected_at=1060.0, t_to=60.0)
    assert await store.timeline_end(s.id) == 60.0
    await store.add_utterance(utt(s.id, "收尾那句", t=59.0, dur=2.5))
    assert await store.timeline_end(s.id) == 61.5
    await store.open_connection(
        s.id, connected_at=2000.0, t_from=1000.0
    )  # 还没关的连接算到它的起点
    assert await store.timeline_end(s.id) == 1000.0
    assert await store.timeline_end("不存在") == 0.0


# --------------------------------------------------------------------------- #
# 会后报告
# --------------------------------------------------------------------------- #


async def test_reports_lifecycle(store):
    s = await store.create_session("会")
    assert await store.latest_report(s.id) is None
    first = await store.create_report(s.id, provider="realtime_llm", now=100.0)
    report = await store.latest_report(s.id)
    assert (report.id, report.status, report.provider, report.text_md, report.error) == (
        first,
        "running",
        "realtime_llm",
        "",
        None,
    )
    assert report.created_at == 100.0
    assert await store.latest_report(s.id, done_only=True) is None
    await store.finish_report(first, "# 报告")
    assert (await store.latest_report(s.id)).status == "done"

    second = await store.create_report(s.id, provider="agent_llm", now=200.0)
    await store.fail_report(second, "模型服务 500")
    latest = await store.latest_report(s.id)
    assert (latest.id, latest.status, latest.error) == (second, "failed", "模型服务 500")
    # 导出要的是最近一份生成好的
    assert (await store.latest_report(s.id, done_only=True)).id == first

    third = await store.create_report(s.id, now=300.0)
    assert await store.fail_running_reports("服务停了") == 1
    assert (await store.latest_report(s.id)).error == "服务停了"
    assert third > second
    await store.delete_session(s.id)
    assert await store.latest_report(s.id) is None  # 随会议一起删掉


# --------------------------------------------------------------------------- #
# 相邻片段并成一条：把文字并进已有的发言
# --------------------------------------------------------------------------- #


async def test_extend_utterance_replaces_the_text_and_keeps_search_in_step(store):
    s = await store.create_session("会", now=100.0)
    first = await store.add_utterance(utt(s.id, "我们先看一下", t=10.0, dur=1.5), now=110.0)
    await store.set_embeddings([(first, [1.0, 0.0, 0.0, 0.0])])
    assert await store.unembedded_utterances() == []

    assert await store.extend_utterance(first, text="我们先看一下消融实验", t_end=14.0, now=120.0)
    (row,) = await store.all_utterances(s.id)
    assert (row.utterance.text, row.utterance.t_start, row.utterance.t_end) == (
        "我们先看一下消融实验",
        10.0,
        14.0,
    )
    assert (await store.get_session(s.id)).last_active_at == 120.0
    # 关键词检索跟着新文字走；向量要重算
    hits = await store.recall(s.id, query="消融实验")
    assert [h.utterance.id for h in hits] == [first]
    assert await store.unembedded_utterances() == [(first, "我们先看一下消融实验")]
    # 结束时间只会往后
    await store.extend_utterance(first, text="我们先看一下消融实验的结果", t_end=12.0)
    assert (await store.all_utterances(s.id))[0].utterance.t_end == 14.0


async def test_extend_utterance_refuses_rows_a_digest_has_already_read(store):
    s = await store.create_session("会")
    first = await store.add_utterance(utt(s.id, "纪要已经看过的话", t=1.0))
    await store.add_digest(s.id, t_from=0, t_to=3.0, text="一、…", last_utterance_id=first)
    second = await store.add_utterance(utt(s.id, "纪要之后的话", t=5.0))
    assert (
        await store.extend_utterance(first, text="纪要已经看过的话，又加了半句", t_end=4.0) is False
    )
    assert (await store.all_utterances(s.id))[0].utterance.text == "纪要已经看过的话"
    assert await store.extend_utterance(second, text="纪要之后的话可以接着并", t_end=9.0) is True
    assert await store.extend_utterance(9999, text="没有这条", t_end=1.0) is False


# --------------------------------------------------------------------------- #
# 手动改发言的说话人
# --------------------------------------------------------------------------- #


async def test_set_utterances_speaker_changes_only_spoken_rows_of_that_session(store):
    s = await store.create_session("会")
    other = await store.create_session("别的会")
    a = await store.add_utterance(utt(s.id, "甲说的", t=0.0, speaker=1))
    b = await store.add_utterance(utt(s.id, "其实是乙说的", t=3.0, speaker=1))
    typed = await store.add_utterance(
        utt(s.id, "打的字", t=5.0, speaker=SPEAKER_TYPED, source="text")
    )
    said = await store.add_utterance(
        utt(s.id, "助理的话", t=6.0, speaker=SPEAKER_ASSISTANT, source="assistant")
    )
    foreign = await store.add_utterance(utt(other.id, "别的会议的", speaker=1))
    await store.ensure_speaker(s.id, 2)

    changed = await store.set_utterances_speaker(s.id, [b, typed, said, foreign, 9999, b], 2)
    assert changed == [b]  # 键入的文字、助理的话、别的会议的、不存在的都不动
    rows = {r.utterance.id: r.utterance.speaker_idx for r in await store.all_utterances(s.id)}
    assert rows == {a: 1, b: 2, typed: SPEAKER_TYPED, said: SPEAKER_ASSISTANT}
    assert (await store.all_utterances(other.id))[0].utterance.speaker_idx == 1
    assert await store.set_utterances_speaker(s.id, [], 2) == []
    # 改成「未知」也可以；说话人的记录按需建立
    assert await store.set_utterances_speaker(s.id, [a], SPEAKER_UNKNOWN) == [a]
    assert await store.has_speaker(s.id, SPEAKER_UNKNOWN)


async def test_manual_speakers_are_numbered_apart_from_diarized_ones(store):
    s = await store.create_session("会")
    await store.add_utterance(utt(s.id, "甲", speaker=3))
    first = await store.add_speaker(s.id, "  旁听的张老师 ")
    second = await store.add_speaker(s.id, "李工")
    assert (first.idx, first.display_name) == (1000, "旁听的张老师")
    assert second.idx == 1001
    assert await store.has_speaker(s.id, 1000) and not await store.has_speaker(s.id, 7)
    # 服务重启后新流的编号偏移只看说话人区分给出过的编号
    assert await store.max_speaker_idx(s.id) == 3
    with pytest.raises(ValueError):
        await store.add_speaker(s.id, "   ")
    other = await store.create_session("另一场")
    assert (await store.add_speaker(other.id, "王")).idx == 1000  # 各场会议各自编号
