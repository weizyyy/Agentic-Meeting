"""任务管理器：假的运行器 + 真实的 SQLite 临时库。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from agentic_meeting.agent.tasks import (
    RESTART_REASON,
    SHUTDOWN_REASON,
    RunnerError,
    TaskFailed,
    TaskManager,
    task_dir,
    task_message,
)
from agentic_meeting.store.db import Store
from agentic_meeting.types import TaskRecord, TaskResult


class FakeRunner:
    """每个任务一个闸门：放行才结束。可以让它报错、发进度。"""

    def __init__(self):
        self.started: list[str] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.outcomes: dict[str, object] = {}
        self.cancelled: list[str] = []
        self.active = 0
        self.max_active = 0

    def gate(self, label: str) -> asyncio.Event:
        return self.gates.setdefault(label, asyncio.Event())

    async def __call__(self, task: TaskRecord, on_event) -> TaskResult:
        self.started.append(task.label)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await on_event("tool_call", f"正在检索「{task.goal}」", {"query": task.goal})
            await self.gate(task.label).wait()
            outcome = self.outcomes.get(task.label)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome or TaskResult(
                brief=f"{task.goal}：查到了", detail_md="## 详情", sources=["https://example.org"]
            )
        except asyncio.CancelledError:
            self.cancelled.append(task.label)
            raise
        finally:
            self.active -= 1


class Rig:
    def __init__(self, store, session_id, **kw):
        self.store, self.session_id = store, session_id
        self.runner = FakeRunner()
        self.messages: list[tuple[str, dict]] = []
        self.clock = 1000.0
        kw.setdefault("max_concurrent", 2)
        kw.setdefault("timeout_secs", 30.0)
        self.manager = TaskManager(
            store=store, runner=self.runner, notify=self.notify, now=lambda: self.clock, **kw
        )

    async def notify(self, session_id, data):
        self.messages.append((session_id, data))

    async def submit(self, goal="查引用数", **kw) -> TaskRecord:
        return await self.manager.submit(session_id=self.session_id, goal=goal, **kw)

    def statuses(self, task_id) -> list[str]:
        return [d["status"] for _, d in self.messages if d["type"] == "task" and d["id"] == task_id]

    def events(self, task_id) -> list[tuple[str, str]]:
        return [
            (d["kind"], d["summary"])
            for _, d in self.messages
            if d["type"] == "task_event" and d["task_id"] == task_id
        ]


async def settle(times=30):
    for _ in range(times):
        await asyncio.sleep(0.002)


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    yield s
    await s.close()


@pytest.fixture
async def rig(store):
    session = await store.create_session(now=900.0)
    r = Rig(store, session.id)
    yield r
    await r.manager.close()


# --------------------------------------------------------------------------- #
# 状态流转
# --------------------------------------------------------------------------- #


async def test_submit_runs_the_task_to_success_and_records_everything(rig):
    task = await rig.submit(
        "查一下这篇论文的引用数",
        requested_by=2,
        requested_t=842.0,
        transcript_window=(542.0, 842.0),
        frame_ids=[7, 9],
        modality="text",
    )
    assert task.label == "t1" and task.id == f"{rig.session_id}.t1"
    assert (task.status, task.modality, task.frame_ids) == ("queued", "text", [7, 9])
    assert (task.t_from, task.t_to, task.requested_by, task.requested_t) == (542.0, 842.0, 2, 842.0)
    await settle()
    assert (await rig.store.get_task(task.id)).status == "running"
    rig.clock = 1060.0
    rig.runner.gate("t1").set()
    result = await rig.manager.wait(task.id)
    assert result == TaskResult(
        brief="查一下这篇论文的引用数：查到了", detail_md="## 详情", sources=["https://example.org"]
    )

    stored = await rig.store.get_task(task.id)
    assert (stored.status, stored.brief, stored.detail_md) == (
        "succeeded",
        result.brief,
        "## 详情",
    )
    assert stored.sources == ["https://example.org"] and stored.artifacts == []
    assert (stored.created_at, stored.started_at, stored.finished_at) == (1000.0, 1000.0, 1060.0)
    assert stored.finished and stored.error is None
    # 每次状态变化都发了消息；进度事件按顺序落库并推送
    assert rig.statuses(task.id) == ["queued", "running", "succeeded"]
    assert rig.events(task.id) == [
        ("status", "开始处理"),
        ("tool_call", "正在检索「查一下这篇论文的引用数」"),
        ("status", "已完成"),
    ]
    events = await rig.store.list_task_events(task.id)
    assert [e.summary for e in events] == [s for _, s in rig.events(task.id)]
    assert events[1].payload == {"query": "查一下这篇论文的引用数"}
    assert all(sid == rig.session_id for sid, _ in rig.messages)
    # 结束之后再等一次，直接从库里拿结果
    assert await rig.manager.wait(task.id) == result


async def test_labels_count_up_within_a_session_and_restart_in_another(rig, store):
    first, second = await rig.submit("a"), await rig.submit("b")
    assert [first.label, second.label] == ["t1", "t2"]
    other = await store.create_session()
    other_task = await rig.manager.submit(session_id=other.id, goal="c")
    assert other_task.label == "t1" and other_task.id != first.id
    assert [t.label for t in await store.list_tasks(rig.session_id)] == ["t1", "t2"]
    for label in ("t1", "t2"):
        rig.runner.gate(label).set()
    await rig.manager.wait(first.id)
    await rig.manager.wait(second.id)
    await rig.manager.wait(other_task.id)


async def test_concurrency_limit_keeps_the_third_task_queued(rig):
    tasks = [await rig.submit(f"任务{i}") for i in range(3)]
    await settle()
    assert rig.runner.started == ["t1", "t2"]
    assert [(await rig.store.get_task(t.id)).status for t in tasks] == [
        "running",
        "running",
        "queued",
    ]
    rig.runner.gate("t1").set()
    await rig.manager.wait(tasks[0].id)
    await settle()
    assert rig.runner.started == ["t1", "t2", "t3"]
    for label in ("t2", "t3"):
        rig.runner.gate(label).set()
    for t in tasks[1:]:
        await rig.manager.wait(t.id)
    assert rig.runner.max_active == 2


# --------------------------------------------------------------------------- #
# 失败、超时、取消
# --------------------------------------------------------------------------- #


async def test_runner_error_fails_the_task_with_its_reason(rig):
    task = await rig.submit()
    rig.runner.outcomes["t1"] = RunnerError("远端模型连不上")
    rig.runner.gate("t1").set()
    with pytest.raises(TaskFailed) as e:
        await rig.manager.wait(task.id)
    assert (e.value.status, e.value.reason) == ("failed", "远端模型连不上")
    stored = await rig.store.get_task(task.id)
    assert (stored.status, stored.error) == ("failed", "远端模型连不上")
    assert rig.events(task.id)[-1] == ("status", "失败：远端模型连不上")
    assert rig.statuses(task.id)[-1] == "failed"


async def test_unexpected_exception_is_reported_without_leaking_internals(rig):
    task = await rig.submit()
    rig.runner.outcomes["t1"] = KeyError("secret-token-123")
    rig.runner.gate("t1").set()
    with pytest.raises(TaskFailed) as e:
        await rig.manager.wait(task.id)
    assert e.value.reason == "内部错误（KeyError）"


async def test_timeout_fails_the_task_and_says_so(store):
    session = await store.create_session()
    rig = Rig(store, session.id, timeout_secs=0.05)
    task = await rig.submit()
    with pytest.raises(TaskFailed) as e:
        await rig.manager.wait(task.id)
    assert e.value.status == "failed" and "超时" in e.value.reason
    assert rig.runner.cancelled == ["t1"]  # 运行器被中断了
    assert (await store.get_task(task.id)).status == "failed"
    await rig.manager.close()


async def test_cancel_interrupts_a_running_task(rig):
    task = await rig.submit()
    await settle()
    cancelled = await rig.manager.cancel(task.id)
    assert (cancelled.status, cancelled.error) == ("cancelled", "已取消")
    assert rig.runner.cancelled == ["t1"]
    with pytest.raises(TaskFailed) as e:
        await rig.manager.wait(task.id)
    assert (e.value.status, e.value.reason) == ("cancelled", "已取消")
    assert rig.statuses(task.id) == ["queued", "running", "cancelled"]
    assert rig.events(task.id)[-1] == ("status", "已取消")
    # 位置放出来了，后面的任务照常能跑
    later = await rig.submit("后面的")
    rig.runner.gate("t2").set()
    assert (await rig.manager.wait(later.id)).brief == "后面的：查到了"


async def test_cancel_a_queued_task_before_it_ever_runs(rig):
    tasks = [await rig.submit(f"任务{i}") for i in range(3)]
    await settle()
    cancelled = await rig.manager.cancel(tasks[2].id)
    assert cancelled.status == "cancelled"
    assert "t3" not in rig.runner.started
    assert rig.statuses(tasks[2].id) == ["queued", "cancelled"]
    for label in ("t1", "t2"):
        rig.runner.gate(label).set()
    await rig.manager.wait(tasks[0].id)
    await rig.manager.wait(tasks[1].id)
    await settle()
    assert "t3" not in rig.runner.started  # 之后也不会被捡起来跑


async def test_cancel_is_a_no_op_for_finished_or_unknown_tasks(rig):
    task = await rig.submit()
    rig.runner.gate("t1").set()
    await rig.manager.wait(task.id)
    assert (await rig.manager.cancel(task.id)).status == "succeeded"
    assert await rig.manager.cancel("nope.t9") is None
    with pytest.raises(LookupError):
        await rig.manager.wait("nope.t9")


# --------------------------------------------------------------------------- #
# 进度查询
# --------------------------------------------------------------------------- #


async def test_status_reports_recent_steps_for_a_label_or_the_latest_task(rig):
    assert await rig.manager.status(rig.session_id) is None
    first = await rig.submit("第一件事")
    rig.clock = 1001.0
    second = await rig.submit("第二件事")
    await settle()
    for i in range(4):
        await rig.manager.add_event(second.id, "step", f"第 {i} 步")
    latest = await rig.manager.status(rig.session_id)
    assert latest == {
        "task_id": "t2",
        "status": "running",
        "goal": "第二件事",
        "recent_steps": ["第 1 步", "第 2 步", "第 3 步"],
    }
    assert (await rig.manager.status(rig.session_id, " T1 "))["goal"] == "第一件事"
    assert await rig.manager.status(rig.session_id, "t9") is None

    rig.runner.gate("t1").set()
    await rig.manager.wait(first.id)
    done = await rig.manager.status(rig.session_id, "t1")
    assert done["status"] == "succeeded" and done["brief"] == "第一件事：查到了"
    await rig.manager.cancel(second.id)
    assert (await rig.manager.status(rig.session_id, "t2"))["reason"] == "已取消"


async def test_event_or_notify_failures_do_not_break_the_task(store):
    session = await store.create_session()
    rig = Rig(store, session.id)

    async def broken(_sid, _data):
        raise RuntimeError("页面不在了")

    rig.manager._notify = broken
    task = await rig.submit()
    rig.runner.gate("t1").set()
    assert (await rig.manager.wait(task.id)).brief
    assert (await store.get_task(task.id)).status == "succeeded"
    await rig.manager.close()


async def test_mark_announced(rig):
    task = await rig.submit()
    rig.runner.gate("t1").set()
    await rig.manager.wait(task.id)
    assert (await rig.store.get_task(task.id)).announced is False
    await rig.manager.mark_announced(task.id)
    assert (await rig.store.get_task(task.id)).announced is True


# --------------------------------------------------------------------------- #
# 重启与关闭
# --------------------------------------------------------------------------- #


async def test_recover_fails_tasks_left_over_from_a_previous_run(store):
    session = await store.create_session()
    queued = await store.create_task(session.id, goal="排着的")
    running = await store.create_task(session.id, goal="跑着的")
    await store.update_task(running.id, status="running", started_at=5.0)
    done = await store.create_task(session.id, goal="做完的")
    await store.update_task(done.id, status="succeeded", brief="好了")

    rig = Rig(store, session.id)
    assert await rig.manager.recover() == 2
    for task in (queued, running):
        stored = await store.get_task(task.id)
        assert (stored.status, stored.error, stored.finished_at) == (
            "failed",
            RESTART_REASON,
            1000.0,
        )
        with pytest.raises(TaskFailed) as e:
            await rig.manager.wait(task.id)
        assert e.value.reason == RESTART_REASON
    assert (await store.get_task(done.id)).status == "succeeded"
    assert await rig.manager.recover() == 0
    # 重启后的新任务接着编号，不和旧的撞
    assert (await rig.submit("新的")).label == "t4"
    await rig.manager.close()


async def test_close_stops_running_tasks_and_marks_them_failed(store):
    session = await store.create_session()
    rig = Rig(store, session.id, max_concurrent=1)
    running, queued = await rig.submit("跑着的"), await rig.submit("排着的")
    await settle()
    await rig.manager.close()
    for task in (running, queued):
        stored = await store.get_task(task.id)
        assert (stored.status, stored.error) == ("failed", SHUTDOWN_REASON)
    assert rig.runner.cancelled == ["t1"]


# --------------------------------------------------------------------------- #
# 存储与辅助
# --------------------------------------------------------------------------- #


async def test_store_task_methods(store):
    session = await store.create_session()
    task = await store.create_task(session.id, goal="查一下", now=10.0)
    assert await store.find_task(session.id, None) == task
    assert await store.find_task(session.id, "T1") == task
    assert await store.find_task(session.id, "t2") is None
    assert await store.find_task("nope", None) is None
    updated = await store.update_task(
        task.id, status="succeeded", sources=["a", "中文"], artifacts=["plot.png"], announced=True
    )
    assert (updated.sources, updated.artifacts, updated.announced) == (
        ["a", "中文"],
        ["plot.png"],
        True,
    )
    with pytest.raises(ValueError):
        await store.update_task(task.id, goal="不能改目标")
    assert await store.update_task("nope.t1", status="failed") is None
    assert (await store.update_task(task.id)).status == "succeeded"  # 什么都不改也行

    for i in range(5):
        await store.add_task_event(task.id, "step", f"第 {i} 步", now=float(i))
    assert [e.summary for e in await store.list_task_events(task.id, last=2)] == [
        "第 3 步",
        "第 4 步",
    ]
    assert len(await store.list_task_events(task.id)) == 5
    await store.delete_session(session.id)
    assert await store.get_task(task.id) is None
    assert await store.list_task_events(task.id) == []  # 级联删除


async def test_old_database_gets_the_new_task_columns(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
            started_at REAL NOT NULL, ended_at REAL, last_active_at REAL NOT NULL DEFAULT 0);
        CREATE TABLE tasks (id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id)
            ON DELETE CASCADE, goal TEXT NOT NULL, requested_by INTEGER NOT NULL DEFAULT 0,
            requested_t REAL NOT NULL, status TEXT NOT NULL DEFAULT 'queued', brief TEXT,
            detail_md TEXT, sources_json TEXT NOT NULL DEFAULT '[]',
            artifacts_json TEXT NOT NULL DEFAULT '[]', error TEXT, created_at REAL NOT NULL,
            started_at REAL, finished_at REAL, announced INTEGER NOT NULL DEFAULT 0,
            modality TEXT NOT NULL DEFAULT 'voice');
        INSERT INTO sessions (id, started_at) VALUES ('s1', 1);
        """
    )
    db.commit()
    db.close()
    store = await Store.open(path, 4)
    try:
        task = await store.create_task("s1", goal="查", t_from=1.0, t_to=2.0, frame_ids=[3])
        assert (task.t_from, task.t_to, task.frame_ids) == (1.0, 2.0, [3])
    finally:
        await store.close()


def test_task_dir_and_message():
    task = TaskRecord(id="abc.t3", session_id="abc", goal="查", created_at=5.0, modality="text")
    assert task_dir(Path("data"), task) == Path("data") / "sessions" / "abc" / "tasks" / "t3"
    assert task_message(task) == {
        "type": "task",
        "id": "abc.t3",
        "label": "t3",
        "goal": "查",
        "status": "queued",
        "brief": None,
        "error": None,
        "modality": "text",
        "created_at": 5.0,
    }
