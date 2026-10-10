"""滚动纪要：假的后台模型 + 真实的 SQLite 临时库。"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from waiting import wait_until

from agentic_meeting.pipeline.background import Preempted
from agentic_meeting.pipeline.digest import (
    NO_PREVIOUS_DIGEST,
    DigestWorker,
    transcript_text,
)
from agentic_meeting.pipeline.prompts import load_prompt
from agentic_meeting.pipeline.session import SessionManager
from agentic_meeting.store.db import Store
from agentic_meeting.types import (
    SPEAKER_ASSISTANT,
    NamedUtterance,
    ScreenFrame,
    Utterance,
)


class FakeModel:
    def __init__(self):
        self.calls: list[dict] = []
        self.replies: list = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.started = asyncio.Event()

    async def run(self, messages, *, system="", max_tokens):
        self.calls.append({"messages": messages, "system": system, "max_tokens": max_tokens})
        self.started.set()
        await self.gate.wait()
        reply = self.replies.pop(0) if self.replies else f"纪要第{len(self.calls)}版"
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def prompt(self, index=-1) -> str:
        return self.calls[index]["messages"][0]["content"]


def render(previous: str, new: str) -> str:
    return f"此前：{previous}\n新增：\n{new}"


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    yield s
    await s.close()


async def say(store, sid, text, t, speaker=1, source="asr"):
    u = Utterance(sid, speaker, t, t + 2.0, text, source=source)
    await store.add_utterance(u)
    return u.id


def worker_for(store, model, **kw):
    kw.setdefault("render", render)
    kw.setdefault("interval_secs", 300.0)
    kw.setdefault("current_session", lambda: None)
    return DigestWorker(store=store, model=model, **kw)


# --------------------------------------------------------------------------- #
# 转录文本
# --------------------------------------------------------------------------- #


def named(t, name, text):
    return NamedUtterance(Utterance("s", 1, t, t + 1, text), name)


def frame(t, caption, fid=1):
    return ScreenFrame("s", t, "p", 1, 1, caption=caption, id=fid)


def test_transcript_text_merges_lines_and_screen_captions_by_time():
    text = transcript_text(
        [named(62.0, "王老师", "学习率是不是大了"), named(5.0, "说话人 2", "开始吧")],
        [
            frame(30.0, "幻灯片：实验设置", 1),
            frame(90.0, "幻灯片：实验设置", 2),  # 和上一张一样：不重复
            frame(95.0, "无关画面", 3),
            frame(96.0, None, 4),  # 还没有摘要
            frame(120.0, "结果表", 5),
        ],
    )
    assert text.splitlines() == [
        "[00:00:05 说话人 2] 开始吧",
        "[画面 00:00:30] 幻灯片：实验设置",
        "[00:01:02 王老师] 学习率是不是大了",
        "[画面 00:02:00] 结果表",
    ]
    assert transcript_text([], []) == ""


def test_same_time_utterance_comes_before_screen_line():
    text = transcript_text([named(10.0, "甲", "看这页")], [frame(10.0, "一页幻灯片")])
    assert text.splitlines() == ["[00:00:10 甲] 看这页", "[画面 00:00:10] 一页幻灯片"]


def test_real_prompt_template_renders():
    text = load_prompt("digest", previous_digest="旧纪要", new_transcript="[00:00:01 甲] 你好")
    assert "旧纪要" in text and "[00:00:01 甲] 你好" in text


# --------------------------------------------------------------------------- #
# 生成一份
# --------------------------------------------------------------------------- #


async def test_no_new_utterances_means_no_model_call(store):
    sid = (await store.create_session()).id
    model = FakeModel()
    worker = worker_for(store, model)
    assert await worker.run_once(sid) is None
    assert model.calls == []
    await say(store, sid, "开始吧", 1.0)
    assert (await worker.run_once(sid)).text == "纪要第1版"
    assert await worker.run_once(sid) is None  # 没有更新的发言了
    assert len(model.calls) == 1


async def test_digest_rolls_previous_text_plus_new_lines_and_ranges_are_contiguous(store):
    sid = (await store.create_session()).id
    model = FakeModel()
    worker = worker_for(store, model)
    await say(store, sid, "第一句", 1.0)
    await say(store, sid, "第二句", 10.0, speaker=2)
    first = await worker.run_once(sid)
    assert (first.t_from, first.t_to) == (0.0, 12.0)
    prompt = model.prompt()
    assert prompt.startswith(f"此前：{NO_PREVIOUS_DIGEST}\n")
    assert prompt.splitlines()[-2:] == ["[00:00:01 说话人 1] 第一句", "[00:00:10 说话人 2] 第二句"]

    last = await say(store, sid, "第三句", 20.0)
    second = await worker.run_once(sid)
    assert (second.t_from, second.t_to) == (12.0, 22.0)  # 首尾相接
    assert second.last_utterance_id == last
    prompt = model.prompt()
    assert "此前：纪要第1版" in prompt
    assert "第三句" in prompt and "第一句" not in prompt  # 只给新增的转录
    assert model.calls[-1]["max_tokens"] > 0 and model.calls[-1]["system"] == ""

    assert [d.id for d in await store.list_digests(sid)] == [first.id, second.id]
    assert (await store.latest_digest(sid)).text == "纪要第2版"


async def test_new_means_by_id_not_by_time(store):
    """助理的话在一轮结束时才落库，开始时间可能早于上一份纪要的终点——不能漏。"""
    sid = (await store.create_session()).id
    model = FakeModel()
    worker = worker_for(store, model)
    await say(store, sid, "有人说了很长一段", 50.0)
    first = await worker.run_once(sid)
    await say(
        store, sid, "助理早些时候开始说的话", 40.0, speaker=SPEAKER_ASSISTANT, source="assistant"
    )
    second = await worker.run_once(sid)
    assert second is not None and "助理早些时候开始说的话" in model.prompt()
    assert second.t_from == first.t_to and second.t_to >= second.t_from  # 时间范围不倒退


async def test_screen_captions_in_range_are_included(store):
    sid = (await store.create_session()).id
    for t, caption, status in [(3.0, "幻灯片：背景", "done"), (500.0, "之后的画面", "done")]:
        f = await store.add_frame(sid, t=t, width=1, height=1, suffix=".webp")
        await store.set_frame_caption(f.id, status=status, caption=caption)
    await say(store, sid, "看这页", 5.0)
    model = FakeModel()
    await worker_for(store, model).run_once(sid)
    assert "[画面 00:00:03] 幻灯片：背景" in model.prompt()
    assert "之后的画面" not in model.prompt()  # 超出这一段时间范围的不带


async def test_backlog_is_digested_in_several_rounds(store):
    sid = (await store.create_session()).id
    for i in range(5):
        await say(store, sid, f"第{i}句", float(i))
    model = FakeModel()
    worker = worker_for(store, model, max_new_utterances=2)
    latest = await worker.catch_up(sid)
    assert len(model.calls) == 3  # 2 + 2 + 1
    assert latest.text == "纪要第3版"
    digests = await store.list_digests(sid)
    assert [d.t_from for d in digests[1:]] == [d.t_to for d in digests[:-1]]
    assert "第4句" in model.prompt() and "第3句" not in model.prompt()
    assert await worker.catch_up(sid) is None


async def test_empty_reply_keeps_previous_digest_and_retries_later(store):
    sid = (await store.create_session()).id
    await say(store, sid, "第一句", 1.0)
    model = FakeModel()
    model.replies = ["", "这次有内容"]
    worker = worker_for(store, model)
    assert await worker.run_once(sid) is None
    assert await store.latest_digest(sid) is None
    assert (await worker.run_once(sid)).text == "这次有内容"


async def test_errors_and_preemption_propagate_from_run_once(store):
    sid = (await store.create_session()).id
    await say(store, sid, "第一句", 1.0)
    model = FakeModel()
    model.replies = [RuntimeError("500"), Preempted()]
    worker = worker_for(store, model)
    with pytest.raises(RuntimeError):
        await worker.run_once(sid)
    with pytest.raises(Preempted):
        await worker.run_once(sid)
    assert await store.latest_digest(sid) is None
    assert (await worker.run_once(sid)) is not None  # 之后照常


async def test_concurrent_runs_do_not_digest_the_same_lines_twice(store):
    sid = (await store.create_session()).id
    await say(store, sid, "第一句", 1.0)
    model = FakeModel()
    model.gate.clear()
    worker = worker_for(store, model)
    a = asyncio.create_task(worker.run_once(sid))
    b = asyncio.create_task(worker.run_once(sid))
    await model.started.wait()
    model.gate.set()
    results = await asyncio.gather(a, b)
    assert sum(r is not None for r in results) == 1 and len(model.calls) == 1


async def test_without_model_nothing_happens(store):
    sid = (await store.create_session()).id
    await say(store, sid, "第一句", 1.0)
    worker = worker_for(store, None)
    assert not worker.enabled
    assert await worker.run_once(sid) is None and await worker.catch_up(sid) is None
    worker.start()
    worker.finalize(sid)
    assert worker._task is None and worker._finalizing == set()
    await worker.stop()


# --------------------------------------------------------------------------- #
# 定时与收尾
# --------------------------------------------------------------------------- #


class Clock:
    """可控的 sleep：记录每次要睡多久，放行一次才醒一次。"""

    def __init__(self):
        self.delays: list[float] = []
        self._ticks: asyncio.Queue[None] = asyncio.Queue()

    async def sleep(self, secs):
        self.delays.append(secs)
        await self._ticks.get()

    async def tick(self):
        self._ticks.put_nowait(None)
        for _ in range(20):
            await asyncio.sleep(0.005)


async def test_loop_digests_the_live_session_every_interval(store):
    sid = (await store.create_session()).id
    live: list[str | None] = [None]
    model, clock = FakeModel(), Clock()
    worker = worker_for(
        store, model, interval_secs=300.0, current_session=lambda: live[0], sleep=clock.sleep
    )
    worker.start()
    worker.start()
    try:
        await say(store, sid, "第一句", 1.0)
        await clock.tick()
        assert model.calls == []  # 没有进行中的会议
        live[0] = sid
        await clock.tick()
        assert len(model.calls) == 1
        await clock.tick()
        assert len(model.calls) == 1  # 没有新发言
        await say(store, sid, "第二句", 400.0)
        await clock.tick()
        assert len(model.calls) == 2
        assert set(clock.delays) == {300.0}
    finally:
        await worker.stop()


async def test_loop_retries_soon_after_preemption_and_survives_errors(store):
    sid = (await store.create_session()).id
    await say(store, sid, "第一句", 1.0)
    model, clock = FakeModel(), Clock()
    model.replies = [Preempted(), RuntimeError("boom"), "成了"]
    worker = worker_for(
        store,
        model,
        interval_secs=300.0,
        retry_secs=15.0,
        current_session=lambda: sid,
        sleep=clock.sleep,
    )
    worker.start()
    try:
        await clock.tick()  # 被抢占
        await clock.tick()  # 出错
        await clock.tick()  # 成功
        assert (await store.latest_digest(sid)).text == "成了"
        assert clock.delays[:4] == [300.0, 15.0, 300.0, 300.0]
    finally:
        await worker.stop()


async def test_retry_delay_never_exceeds_the_interval(store):
    sid = (await store.create_session()).id
    await say(store, sid, "第一句", 1.0)
    model, clock = FakeModel(), Clock()
    model.replies = [Preempted()]
    worker = worker_for(
        store,
        model,
        interval_secs=5.0,
        retry_secs=15.0,
        current_session=lambda: sid,
        sleep=clock.sleep,
    )
    worker.start()
    try:
        await clock.tick()
        assert clock.delays[:2] == [5.0, 5.0]
    finally:
        await worker.stop()


async def wait_for_digest(store, sid, tries=400):
    for _ in range(tries):
        digest = await store.latest_digest(sid)
        if digest is not None:
            return digest
        await asyncio.sleep(0.005)
    raise AssertionError("纪要一直没有生成")


async def test_finalize_runs_in_background_and_reports_nothing_on_failure(store):
    sid = (await store.create_session()).id
    await say(store, sid, "最后一句", 1.0)
    model = FakeModel()
    worker = worker_for(store, model)
    try:
        worker.finalize(sid)  # 不等
        await wait_until(lambda: not worker._finalizing, description="纪要收尾任务完成")
        assert (await wait_for_digest(store, sid)).text == "纪要第1版"

        for failure in (RuntimeError("boom"), Preempted()):
            await say(store, sid, "又一句", 9.0)
            model.replies = [failure]
            worker.finalize(sid)
            await wait_until(lambda: not worker._finalizing, description="失败纪要任务收尾")
        assert len(await store.list_digests(sid)) == 1
    finally:
        await worker.stop()


async def test_finalize_times_out_and_stop_cancels_pending_work(store):
    sid = (await store.create_session()).id
    await say(store, sid, "最后一句", 1.0)
    model = FakeModel()
    model.gate.clear()
    worker = worker_for(store, model)
    try:
        worker.finalize(sid, timeout_secs=0.05)
        await wait_until(lambda: not worker._finalizing, description="纪要超时取消并收尾")
        assert worker._finalizing == set() and await store.latest_digest(sid) is None

        model.started.clear()
        worker.finalize(sid, timeout_secs=30)
        await asyncio.wait_for(model.started.wait(), 2.0)
    finally:
        await worker.stop()
    assert worker._finalizing == set()


# --------------------------------------------------------------------------- #
# 存储与旧库迁移
# --------------------------------------------------------------------------- #


async def test_digests_are_deleted_with_their_session(store):
    sid = (await store.create_session()).id
    other = (await store.create_session()).id
    await store.add_digest(sid, t_from=0, t_to=5, text="a", last_utterance_id=3, now=100.0)
    kept = await store.add_digest(other, t_from=0, t_to=5, text="b", last_utterance_id=1)
    assert (await store.latest_digest(sid)).created_at == 100.0
    await store.delete_session(sid)
    assert await store.latest_digest(sid) is None
    assert (await store.latest_digest(other)).id == kept.id


async def test_old_database_without_the_watermark_column_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
            started_at REAL NOT NULL, ended_at REAL, last_active_at REAL NOT NULL DEFAULT 0);
        CREATE TABLE digests (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id)
            ON DELETE CASCADE, t_from REAL NOT NULL, t_to REAL NOT NULL, text TEXT NOT NULL,
            created_at REAL NOT NULL);
        INSERT INTO sessions (id, started_at) VALUES ('s1', 1);
        INSERT INTO digests (session_id, t_from, t_to, text, created_at) VALUES ('s1', 0, 9, '旧纪要', 5);
        """
    )
    db.commit()
    db.close()
    store = await Store.open(path, 4)
    try:
        old = await store.latest_digest("s1")
        assert (old.text, old.last_utterance_id) == ("旧纪要", 0)
        new = await store.add_digest("s1", t_from=9, t_to=20, text="新的", last_utterance_id=7)
        assert (await store.latest_digest("s1")).last_utterance_id == 7 and new.t_from == 9
    finally:
        await store.close()
    # 再打开一次不会重复加列
    again = await Store.open(path, 4)
    await again.close()


# --------------------------------------------------------------------------- #
# 接线：连接结束时收尾
# --------------------------------------------------------------------------- #


async def test_session_manager_runs_finish_hooks_with_the_session_id(store):
    manager = SessionManager(store, notify_grace_secs=0.0)
    seen: list[str] = []

    def broken(_sid):
        raise RuntimeError("boom")

    async def record(sid):
        seen.append(sid)

    manager.on_finished.append(broken)  # 钩子出错不影响收尾，也不影响后面的钩子
    manager.on_finished.append(record)
    manager.on_finished.append(seen.append)
    live = await manager.begin()
    await manager.finish(live)
    assert seen == [live.session.id, live.session.id]
    assert manager.live is None and live.done.is_set()
