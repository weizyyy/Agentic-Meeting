"""保留、完整删除与不可复用身份：真实临时 SQLite 和实际文件系统。"""

from __future__ import annotations

import asyncio
import threading

import pytest
from pydantic import ValidationError

from agentic_meeting.config import RetentionConfig
from agentic_meeting.pipeline.session import SessionManager
from agentic_meeting.store.db import SessionBusy, Store
from agentic_meeting.store.retention import OwnedFiles, RetentionWorker
from agentic_meeting.types import Utterance

NOW = 1_000_000.0


@pytest.fixture
async def resources(tmp_path):
    store = await Store.open(tmp_path / "meetings.db", 3)
    manager = SessionManager(store, now=lambda: NOW, notify_grace_secs=0)
    worker = RetentionWorker(store, manager, RetentionConfig(), tmp_path, now=lambda: NOW)
    yield store, manager, worker
    await worker.stop()
    await store.close()


async def content(store, root, *, at=1.0):
    session = await store.create_session("虚构会议", now=at)
    utterance = Utterance(session.id, 1, 0, 1, "虚构转录")
    await store.add_utterance(utterance, now=at)
    await store.set_embeddings([(utterance.id, [1, 0, 0])])
    frame = await store.add_frame(session.id, t=0, width=1, height=1, suffix=".webp", now=at)
    path = root / frame.path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fake-image")
    report = await store.create_report(session.id, now=at)
    await store.finish_report(report, "虚构报告", now=at)
    digest = await store.add_digest(
        session.id, t_from=0, t_to=1, text="虚构纪要", last_utterance_id=utterance.id, now=at
    )
    task = await store.create_task(session.id, goal="虚构目标", frame_ids=[frame.id], now=at)
    await store.update_task(
        task.id, status="succeeded", finished_at=at, detail_md="虚构结果", artifacts=["result.txt"]
    )
    directory = root / "sessions" / session.id / "tasks" / task.label
    directory.mkdir(parents=True)
    (directory / "result.txt").write_text("fake-artifact")
    await store.add_task_event(task.id, "note", "虚构事件", now=at)
    return session, utterance, frame, report, digest, task


@pytest.mark.parametrize(
    "field", ["transcript_days", "screenshots_days", "reports_days", "task_artifacts_days"]
)
@pytest.mark.parametrize("value", [-1, 36501, True, 1.5, float("nan")])
def test_retention_rejects_non_integer_and_out_of_range(field, value):
    with pytest.raises(ValidationError):
        RetentionConfig(**{field: value})


async def test_default_and_keep_preserve_all(resources, tmp_path):
    store, _, worker = resources
    session, utterance, frame, report, _, task = await content(store, tmp_path)
    assert await worker.run_once() == 0
    worker.policy = RetentionConfig(
        transcript_days=1, screenshots_days=1, reports_days=1, task_artifacts_days=1
    )
    await store.update_session(session.id, keep=True)
    assert await worker.run_once() == 0
    assert await store.get_frame(frame.id) and (tmp_path / frame.path).is_file()
    assert await store.get_report(report)
    assert await store.list_utterances(session.id)
    assert (await store.get_task(task.id)).artifacts


@pytest.mark.parametrize("category", ["transcript", "screenshots", "reports", "task_artifacts"])
async def test_categories_are_independent(resources, tmp_path, category):
    store, _, worker = resources
    session, utterance, frame, report, _, task = await content(store, tmp_path)
    worker.policy = RetentionConfig(**{category + "_days": 1})
    assert await worker.run_once() == 1
    assert bool(await store.list_utterances(session.id)) is (category != "transcript")
    assert bool(await store.get_frame(frame.id)) is (category != "screenshots")
    assert (tmp_path / frame.path).exists() is (category != "screenshots")
    assert bool(await store.get_report(report)) is (category != "reports")
    assert bool(await store.list_digests(session.id)) is (category != "reports")
    after = await store.get_task(task.id)
    assert after.goal == "虚构目标" and after.detail_md == "虚构结果"
    assert after.frame_ids == [frame.id] and len(await store.list_task_events(task.id)) == 1
    assert bool(after.artifacts) is (category != "task_artifacts")
    assert (tmp_path / "sessions" / session.id / "tasks" / task.label).exists() is (
        category != "task_artifacts"
    )
    vectors = await store._all("SELECT rowid FROM utterances_vec")
    assert bool(vectors) is (category != "transcript")
    assert await store.get_session(session.id)


@pytest.mark.parametrize("offset", [-1, 0, 1, 1_000_000])
async def test_utc_cutoff_is_strict(resources, tmp_path, offset):
    store, _, worker = resources
    session, *_ = await content(store, tmp_path, at=NOW - 86400 + offset)
    worker.policy = RetentionConfig(transcript_days=1)
    await worker.run_once()
    assert bool(await store.list_utterances(session.id)) is (offset >= 0)


async def test_manual_delete_ignores_keep_and_removes_everything(resources, tmp_path):
    store, _, worker = resources
    session, *_ = await content(store, tmp_path)
    other = await store.create_session("其他会议")
    await store.update_session(session.id, keep=True)
    await worker.delete_session(session.id)
    assert await store.get_session(session.id) is None
    assert await store.get_session(other.id)
    assert not (tmp_path / "sessions" / session.id).exists()
    for table in (
        "utterances",
        "utterances_vec",
        "utterances_fts",
        "frames",
        "reports",
        "digests",
        "tasks",
        "task_events",
    ):
        assert not await store._all(f"SELECT * FROM {table}")
    with pytest.raises(LookupError):
        await worker.delete_session(session.id)


async def test_file_failure_pending_survives_restart_with_zero_periods(
    resources, tmp_path, monkeypatch
):
    store, manager, worker = resources
    session, *_ = await content(store, tmp_path)

    def failure(_):
        raise PermissionError("private-error-sentinel")

    monkeypatch.setattr(worker.files, "remove_session", failure)
    with pytest.raises(PermissionError):
        await worker.delete_session(session.id)
    assert (await store.get_session(session.id)).deletion_pending
    with pytest.raises(SessionBusy):
        await manager.attach(session.id)
    with pytest.raises(SessionBusy):
        await store.update_session(session.id, keep=True)
    assert (tmp_path / "sessions" / session.id).exists()
    second = await Store.open(tmp_path / "meetings.db", 3)
    try:
        retry = RetentionWorker(second, SessionManager(second), RetentionConfig(), tmp_path)
        assert await retry.run_once() == 1
        assert await second.get_session(session.id) is None
        assert not (tmp_path / "sessions" / session.id).exists()
    finally:
        await second.close()


async def test_claim_protects_work_and_keep_order(resources):
    store, manager, worker = resources
    session = await store.create_session(now=0)
    worker.policy = RetentionConfig(transcript_days=1)
    async with store.session_work(session.id):
        with pytest.raises(SessionBusy):
            await worker.delete_session(session.id)
    await manager.update_session(session.id, keep=True)
    assert await manager.claim_cleanup(session.id, worker.policy, NOW) is None
    await manager.update_session(session.id, keep=False)
    plan = await manager.claim_cleanup(session.id, worker.policy, NOW)
    with pytest.raises(SessionBusy):
        await manager.attach(session.id)
    with pytest.raises(SessionBusy):
        await manager.update_session(session.id, keep=True)
    await store.release_cleanup(plan.session_id)


async def test_assembly_and_unfinished_takeover_are_busy(resources):
    store, manager, worker = resources
    live = await manager.begin()
    with pytest.raises(SessionBusy):
        await worker.delete_session(live.session.id)
    manager._live = None  # 接管超时清掉位置，旧worker仍没有真正finish。
    with pytest.raises(SessionBusy):
        await worker.delete_session(live.session.id)
    await manager.finish(live)
    await worker.delete_session(live.session.id)


async def test_highwater_preserves_cursors_references_and_restarts(resources, tmp_path):
    store, _, worker = resources
    session, utterance, frame, _, digest, task = await content(store, tmp_path)
    worker.policy = RetentionConfig(transcript_days=1, screenshots_days=1)
    await worker.run_once()
    new = Utterance(session.id, 1, 2, 3, "新虚构转录")
    await store.add_utterance(new)
    image = await store.add_frame(session.id, t=2, width=1, height=1, suffix=".webp")
    assert new.id > digest.last_utterance_id
    assert (await store.list_utterances(session.id, after_id=digest.last_utterance_id))[
        0
    ].utterance.id == new.id
    assert image.id > frame.id
    assert (await store.get_task(task.id)).frame_ids == [frame.id]
    assert await store.get_frame(frame.id) is None
    await store._db.execute("UPDATE meta SET value = '100' WHERE key LIKE '%_id_high_water'")
    await store._db.commit()
    second = await Store.open(tmp_path / "meetings.db", 3)
    try:
        latest = Utterance(session.id, 1, 3, 4, "再次虚构转录")
        await second.add_utterance(latest)
        image = await second.add_frame(session.id, t=3, width=1, height=1, suffix=".webp")
        assert latest.id == image.id == 101
    finally:
        await second.close()


async def test_original_tokens_reject_stale_callbacks(resources):
    store, _, _ = resources
    session = await store.create_session()
    utterance = Utterance(session.id, 1, 0, 1, "原虚构文字")
    await store.add_utterance(utterance)
    old = utterance.write_token
    assert await store.extend_utterance(
        utterance.id, text="扩展虚构文字", t_end=2, session_id=session.id, write_token=old
    )
    assert (
        await store.set_embeddings(
            [(utterance.id, [1, 0, 0])], identities={utterance.id: (session.id, old)}
        )
        == 0
    )
    report = await store.create_report(session.id)
    token = (await store.get_report(report)).write_token
    await store._db.execute("DELETE FROM reports WHERE id = ?", (report,))
    await store._db.commit()
    reused = await store.create_report(session.id)
    assert reused == report
    assert not await store.finish_report(
        report, "迟到报告", session_id=session.id, write_token=token
    )
    assert not await store.fail_report(report, "迟到失败", session_id=session.id, write_token=token)
    assert (await store.get_report(reused)).status == "running"


async def test_cancelled_file_thread_is_drained_before_claim_release(
    resources, tmp_path, monkeypatch
):
    store, manager, worker = resources
    session, *_ = await content(store, tmp_path)
    entered, released = threading.Event(), threading.Event()
    remove = worker.files.remove_session

    def paused(sid):
        entered.set()
        assert released.wait(5)
        remove(sid)

    monkeypatch.setattr(worker.files, "remove_session", paused)
    deletion = asyncio.create_task(worker.delete_session(session.id))
    await asyncio.to_thread(entered.wait, 5)
    deletion.cancel()
    await asyncio.sleep(0)
    assert not deletion.done()
    with pytest.raises(SessionBusy):
        await manager.attach(session.id)
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await deletion
    assert not (tmp_path / "sessions" / session.id).exists()
    assert (await store.get_session(session.id)).deletion_pending
    await worker.delete_session(session.id)
    assert await store.get_session(session.id) is None


@pytest.mark.parametrize("component", ["sessions", "session", "frames", "tasks"])
async def test_intermediate_symlink_refused_external_sentinel_safe(resources, tmp_path, component):
    store, _, worker = resources
    session = await store.create_session(now=0)
    external = tmp_path / "outside"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("safe")
    owned = tmp_path / "sessions" / session.id
    target = {
        "sessions": tmp_path / "sessions",
        "session": owned,
        "frames": owned / "frames",
        "tasks": owned / "tasks",
    }[component]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError):
        if component == "tasks":
            worker.files.remove_task(session.id, "t1")
        elif component == "frames":
            worker.files.remove_frames(session.id)
        else:
            await worker.delete_session(session.id)
    assert sentinel.read_text() == "safe"


def test_owned_leaf_link_is_unlinked_without_following(tmp_path):
    files = OwnedFiles(tmp_path)
    sid = "a" * 32
    root = tmp_path / "sessions" / sid / "tasks"
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("safe")
    (root / "t1").symlink_to(outside, target_is_directory=True)
    files.remove_task(sid, "t1")
    assert sentinel.read_text() == "safe" and not (root / "t1").exists()


async def test_cancel_release_always_drops_attempt(resources, tmp_path, monkeypatch):
    store, _, worker = resources
    session, *_ = await content(store, tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.release_cleanup

    async def paused(sid):
        entered.set()
        await release.wait()
        await original(sid)

    monkeypatch.setattr(store, "release_cleanup", paused)
    deletion = asyncio.create_task(worker.delete_session(session.id))
    await entered.wait()
    deletion.cancel()
    await asyncio.sleep(0)
    deletion.cancel()
    assert not deletion.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await deletion
    assert not worker._attempts and not store._claims
    await worker.stop()


async def test_finish_hook_cancellation_drains_and_unlocks(resources):
    store, manager, _ = resources
    live = await manager.begin()
    entered, release, done = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def hook(_sid):
        entered.set()
        await release.wait()
        done.set()

    manager.on_finished.append(hook)
    finish = asyncio.create_task(manager.finish(live))
    await entered.wait()
    finish.cancel()
    await asyncio.sleep(0)
    finish.cancel()
    with pytest.raises(SessionBusy):
        await manager.claim_cleanup(live.session.id, RetentionConfig(), NOW, manual=True)
    assert not finish.done() and not done.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await finish
    assert done.is_set() and not manager._unfinished
    plan = await manager.claim_cleanup(live.session.id, RetentionConfig(), NOW, manual=True)
    assert plan.manual
    await store.release_cleanup(live.session.id)


async def test_embedding_late_result_cannot_recreate_deleted_vectors(resources, tmp_path):
    from agentic_meeting.store.embeddings import EmbeddingWorker

    store, _, cleaner = resources
    session, *_ = await content(store, tmp_path)
    utterance = Utterance(session.id, 1, 2, 3, "虚构待嵌入")
    await store.add_utterance(utterance)
    entered, release = asyncio.Event(), asyncio.Event()

    class Embedder:
        async def embed(self, texts):
            entered.set()
            await release.wait()
            return [[1, 0, 0] for _ in texts]

    embedding = EmbeddingWorker(store, Embedder())
    request = asyncio.create_task(embedding.run_once())
    await entered.wait()
    await cleaner.delete_session(session.id)
    other = await store.create_session()
    new = Utterance(other.id, 1, 0, 1, "新的虚构发言")
    await store.add_utterance(new)
    release.set()
    assert await request == 0
    assert new.id > utterance.id
    assert (await store._one("SELECT COUNT(*) FROM utterances_vec"))[0] == 0


async def test_ingest_cancel_waits_for_actual_write_before_delete(resources, tmp_path, monkeypatch):
    import io

    from PIL import Image

    from agentic_meeting.screen.ingest import FrameIngestor

    store, _, cleaner = resources
    session = await store.create_session(now=NOW)
    ingest = FrameIngestor(store, tmp_path, max_side_px=32, change_threshold=0.1, now=lambda: NOW)
    data = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(data, "WEBP")
    entered, release = threading.Event(), threading.Event()
    original = ingest._write

    def paused(path, content):
        entered.set()
        assert release.wait(5)
        original(path, content)

    monkeypatch.setattr(ingest, "_write", paused)
    upload = asyncio.create_task(ingest.ingest(session, data.getvalue(), NOW))
    assert await asyncio.to_thread(entered.wait, 5)
    upload.cancel()
    await asyncio.sleep(0)
    upload.cancel()
    with pytest.raises(SessionBusy):
        await cleaner.delete_session(session.id)
    assert not upload.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await upload
    await cleaner.delete_session(session.id)
    assert not (tmp_path / "sessions" / session.id).exists()
    assert not store._work


async def test_digest_uses_global_cursor_after_transcript_cleanup(resources, tmp_path):
    from agentic_meeting.pipeline.digest import DigestWorker

    store, _, cleaner = resources
    session, utterance, *_ = await content(store, tmp_path)
    cleaner.policy = RetentionConfig(transcript_days=1)
    assert await cleaner.run_once() == 1
    new = Utterance(session.id, 1, 2, 3, "虚构新发言")
    await store.add_utterance(new, now=NOW)
    assert new.id > utterance.id
    await store.end_session(session.id, now=NOW)
    cleaner.policy = RetentionConfig(reports_days=1)
    entered, release = asyncio.Event(), asyncio.Event()

    class Model:
        async def run(self, messages, *, max_tokens):
            assert "虚构新发言" in messages[0]["content"]
            entered.set()
            await release.wait()
            return "虚构新纪要"

    digest = DigestWorker(
        store=store,
        model=Model(),
        render=lambda old, new: old + new,
        interval_secs=60,
        current_session=lambda: None,
    )
    task = asyncio.create_task(digest.run_once(session.id))
    await entered.wait()
    assert (await store.get_session(session.id)).ended_at is not None
    assert await cleaner.run_once() == 0
    with pytest.raises(SessionBusy):
        await cleaner.delete_session(session.id)
    release.set()
    assert (await task).last_utterance_id == new.id
    assert [
        item.utterance.id for item in await store.list_utterances(session.id, after_id=utterance.id)
    ] == [new.id]


async def test_pending_caption_and_followers_protect_session(resources, tmp_path):
    from agentic_meeting.screen.caption import CaptionWorker
    from agentic_meeting.screen.ingest import IngestedFrame

    store, _, cleaner = resources
    session, _, frame, *_ = await content(store, tmp_path)
    messages = []

    class Model:
        async def wait_resumed(self):
            await asyncio.Event().wait()

    async def notify(*args):
        messages.append(args)

    caption = CaptionWorker(
        store=store,
        model=Model(),
        prompt="虚构提示",
        path_of=lambda f: tmp_path / f.path,
        notify=notify,
        append_context=notify,
    )
    caption.start()
    await caption.submit(IngestedFrame(frame, True))
    follower = await store.add_frame(session.id, t=2, width=1, height=1, suffix=".webp")
    await caption.submit(IngestedFrame(follower, False, frame.id))
    with pytest.raises(SessionBusy):
        await cleaner.delete_session(session.id)
    await caption.stop()
    assert not store._work
    await cleaner.delete_session(session.id)
    assert not messages


async def test_report_cancelled_admission_and_immediate_stop_has_no_orphan(resources, monkeypatch):
    from agentic_meeting.pipeline.report import ReportWorker

    store, _, cleaner = resources
    session = await store.create_session()
    entered, release = asyncio.Event(), asyncio.Event()
    original = store.get_report

    async def paused(rid):
        entered.set()
        await release.wait()
        return await original(rid)

    monkeypatch.setattr(store, "get_report", paused)
    report = ReportWorker(
        store=store,
        model=object(),
        provider="fake",
        render_report=lambda **kw: "",
        render_section=lambda **kw: "",
        max_input_chars=100,
    )
    admission = asyncio.create_task(report.start(session.id))
    await entered.wait()
    admission.cancel()
    stop = asyncio.create_task(report.stop())
    await asyncio.sleep(0)
    assert not admission.done() and not stop.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await admission
    await stop
    assert (await store.latest_report(session.id)).status != "running"
    await cleaner.delete_session(session.id)


async def test_task_terminal_notify_and_cancelled_admission_remain_protected(resources):
    from agentic_meeting.agent.tasks import TaskManager
    from agentic_meeting.types import TaskResult

    store, _, cleaner = resources
    session = await store.create_session()
    submitted, allow_submit, terminal, allow_terminal = [asyncio.Event() for _ in range(4)]

    async def notify(_sid, message):
        if message.get("type") == "task" and message.get("status") == "queued":
            submitted.set()
            await allow_submit.wait()
        if message.get("type") == "task" and message.get("status") == "succeeded":
            terminal.set()
            await allow_terminal.wait()

    async def run(task, on_event):
        return TaskResult("虚构完成", "虚构结果")

    tasks = TaskManager(store=store, runner=run, notify=notify, max_concurrent=1, timeout_secs=5)
    admission = asyncio.create_task(tasks.submit(session_id=session.id, goal="虚构任务"))
    await submitted.wait()
    await store.end_session(session.id)
    admission.cancel()
    with pytest.raises(SessionBusy):
        await cleaner.delete_session(session.id)
    allow_submit.set()
    with pytest.raises(asyncio.CancelledError):
        await admission
    await terminal.wait()
    assert (await store.list_tasks(session.id))[0].finished
    with pytest.raises(SessionBusy):
        await cleaner.delete_session(session.id)
    allow_terminal.set()
    await tasks.wait(f"{session.id}.t1")
    await tasks.close()
    await cleaner.delete_session(session.id)


async def test_user_cancel_during_task_admission_sets_terminal_and_done(resources, monkeypatch):
    from agentic_meeting.agent.tasks import TaskManager
    from agentic_meeting.types import TaskResult

    store, _, cleaner = resources
    session = await store.create_session()
    entered = asyncio.Event()
    original = store._require_writable
    current_admission = None

    async def paused(sid):
        if asyncio.current_task() is current_admission:
            entered.set()
            await asyncio.Event().wait()
        await original(sid)

    async def run(task, on_event):
        return TaskResult("虚构完成", "")

    async def notify(*args):
        pass

    tasks = TaskManager(store=store, runner=run, notify=notify, max_concurrent=1, timeout_secs=5)
    record = await tasks.submit(session_id=session.id, goal="虚构任务")
    current_admission = tasks._running[record.id]
    monkeypatch.setattr(store, "_require_writable", paused)
    await asyncio.wait_for(entered.wait(), 1)
    cancelled = await asyncio.wait_for(tasks.cancel(record.id), 1)
    assert cancelled.status == "cancelled"
    assert tasks._done[record.id].is_set() and record.id not in tasks._running
    assert not store._work
    await tasks.close()
    await cleaner.delete_session(session.id)


@pytest.mark.parametrize("enabled", [False, True])
async def test_ready_message_reads_current_keep_and_optional_notice(resources, make_cfg, enabled):
    from agentic_meeting.pipeline.bot import AppResources, send_session_ready

    store, manager, _ = resources
    live = await manager.begin()
    await manager.update_session(live.session.id, keep=True)
    cfg = make_cfg()
    cfg.session.recording_notice = enabled
    messages = []

    async def send(message):
        messages.append(message)

    await send_session_ready(AppResources(cfg, store=store, sessions=manager), live, send)
    assert messages[0]["keep"] is True
    assert messages[0]["id"] == live.session.id and messages[0]["state"] == "live"
    assert len(messages) == 1 + int(enabled)
    if enabled:
        assert messages[1] == {
            "type": "notice",
            "level": "info",
            "text": "会议正在转录，发言和共享画面会保存在服务器上",
        }
    await manager.finish(live)


async def test_migration_seeds_retained_references_and_rollback_does_not_consume_id(
    resources, tmp_path
):
    import sqlite3

    store, _, _ = resources
    session = await store.create_session()
    await store.add_digest(session.id, t_from=0, t_to=1, text="虚构旧纪要", last_utterance_id=500)
    await store.create_task(session.id, goal="虚构历史任务", frame_ids=[600])
    await store._db.execute("DELETE FROM meta WHERE key LIKE '%_id_high_water'")
    await store._db.commit()
    reopened = await Store.open(tmp_path / "meetings.db", 3)
    try:
        await reopened._db.execute(
            "CREATE TRIGGER reject_new BEFORE INSERT ON utterances BEGIN SELECT RAISE(ABORT, 'fake failure'); END"
        )
        await reopened._db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            await reopened.add_utterance(Utterance(session.id, 1, 0, 1, "虚构失败发言"))
        await reopened._db.execute("DROP TRIGGER reject_new")
        await reopened._db.commit()
        fresh = Utterance(session.id, 1, 1, 2, "虚构新发言")
        await reopened.add_utterance(fresh)
        frame = await reopened.add_frame(session.id, t=1, width=1, height=1, suffix=".webp")
        assert fresh.id == 501 and frame.id == 601
    finally:
        await reopened.close()


async def test_cleanup_sql_failure_is_pending_then_zero_policy_retry(resources, tmp_path):
    import sqlite3

    store, _, cleaner = resources
    session, *_ = await content(store, tmp_path)
    await store._db.execute(
        "CREATE TRIGGER reject_delete BEFORE DELETE ON sessions BEGIN SELECT RAISE(ABORT, 'fake failure'); END"
    )
    await store._db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        await cleaner.delete_session(session.id)
    assert (await store.get_session(session.id)).deletion_pending
    assert await store.list_utterances(session.id)
    assert not (tmp_path / "sessions" / session.id).exists()
    await store._db.execute("DROP TRIGGER reject_delete")
    await store._db.commit()
    assert await cleaner.run_once() == 1
    assert await store.get_session(session.id) is None


async def test_legacy_schema_migrates_tokens_and_finished_times_without_replacing_rows(tmp_path):
    import re
    import sqlite3

    from agentic_meeting.store.db import SCHEMA_PATH

    schema = "\n".join(
        line
        for line in SCHEMA_PATH.read_text(encoding="utf-8").splitlines()
        if not any(
            key in line
            for key in (
                "write_token TEXT",
                "    keep INTEGER",
                "deletion_pending INTEGER",
                "    finished_at REAL",
            )
        )
    )
    schema = re.sub(r",(\s*--[^\n]*)?\n\);", r"\1\n);", schema)
    database = tmp_path / "legacy.db"
    sid = "d" * 32
    with sqlite3.connect(database) as db:
        db.executescript(schema)
        db.execute(
            "INSERT INTO sessions (id, title, started_at, last_active_at) VALUES (?, '虚构旧库', 1, 1)",
            (sid,),
        )
        db.execute(
            "INSERT INTO utterances (id, session_id, t_start, t_end, text) VALUES (10, ?, 0, 1, '虚构旧发言')",
            (sid,),
        )
        db.execute(
            "INSERT INTO frames (id, session_id, t, path, width, height) VALUES (20, ?, 1, 'fake.webp', 1, 1)",
            (sid,),
        )
        db.execute(
            "INSERT INTO reports (id, session_id, created_at, status, text_md) VALUES (30, ?, 1, 'done', '虚构旧报告')",
            (sid,),
        )
    store = await Store.open(database, 3)
    try:
        session = await store.get_session(sid)
        assert session.title == "虚构旧库" and not session.keep and not session.deletion_pending
        utterance = (await store.list_utterances(sid))[0].utterance
        frame = await store.get_frame(20)
        report = await store.get_report(30)
        tokens = (utterance.write_token, frame.write_token, report.write_token)
        assert all(len(token) == 32 for token in tokens) and report.finished_at == 1
        await store.close()
        store = await Store.open(database, 3)
        assert (await store.get_report(30)).write_token == tokens[2]
        assert (await store.get_frame(20)).write_token == tokens[1]
        fresh = Utterance(sid, 1, 2, 3, "虚构新发言")
        await store.add_utterance(fresh)
        assert fresh.id == 11
        assert (await store.add_frame(sid, t=2, width=1, height=1, suffix=".webp")).id == 21
    finally:
        await store.close()


async def test_startup_recovers_disabled_agent_tasks_before_retention(
    resources, make_cfg, tmp_path
):
    from agentic_meeting.web.app import create_app

    store, _, _ = resources
    session = await store.create_session(now=1)
    queued = await store.create_task(session.id, goal="虚构崩溃前任务", now=1)
    cfg = make_cfg()
    cfg.agent.enabled = False
    cfg.retention.transcript_days = 1
    app = create_app(cfg, store=store, static_dir=tmp_path / "no-static")
    async with app.router.lifespan_context(app):
        assert app.state.resources.tasks is None
        assert (await store.get_task(queued.id)).status == "failed"
        await app.state.resources.retention.delete_session(session.id)
        assert await store.get_session(session.id) is None


async def test_shutdown_drains_old_owner_and_finish_hook_after_slot_cleared(resources):
    _, manager, _ = resources
    live = await manager.begin()
    live.owner = asyncio.current_task()
    manager._live = None
    entered, release = asyncio.Event(), asyncio.Event()

    async def hook(_sid):
        entered.set()
        await release.wait()

    manager.on_finished.append(hook)
    finish = asyncio.create_task(manager.finish(live))
    await entered.wait()
    draining = asyncio.create_task(manager.drain())
    await asyncio.sleep(0)
    assert not draining.done()
    release.set()
    await finish
    await draining
    assert not manager._unfinished


async def test_recorder_recheck_retains_token_before_async_diarization(resources, monkeypatch):
    from agentic_meeting.pipeline.recorder import MeetingRecorder

    store, _, _ = resources
    session = await store.create_session()
    utterance = Utterance(session.id, 1, 0, 1, "虚构原话")
    await store.add_utterance(utterance)
    recorder = MeetingRecorder(
        store=store, session_id=session.id, recheck_interval_secs=0, recheck_attempts=1
    )
    entered, release = asyncio.Event(), asyncio.Event()
    messages = []

    async def segments(_since):
        entered.set()
        await release.wait()
        return []

    async def send(data):
        messages.append(data)

    monkeypatch.setattr(recorder, "_segments", segments)
    monkeypatch.setattr(recorder, "_send", send)
    monkeypatch.setattr(recorder._assembler, "recheck", lambda current, segments: 2)
    correction = asyncio.create_task(recorder._recheck(utterance))
    await entered.wait()
    assert await store.extend_utterance(
        utterance.id,
        text="虚构扩展话",
        t_end=2,
        session_id=session.id,
        write_token=utterance.write_token,
        next_token="new-version",
    )
    utterance.write_token = "new-version"  # 实际merge也更新同一对象；旧recheck仍须保留旧版本。
    release.set()
    await correction
    assert (await store.list_utterances(session.id))[0].utterance.speaker_idx == 1
    assert not messages


async def test_reports_and_task_artifacts_expire_from_completion_not_old_session(
    resources, tmp_path
):
    store, _, cleaner = resources
    session = await store.create_session(now=1)
    report = await store.create_report(session.id, now=1)
    await store.finish_report(report, "虚构新报告", now=NOW)
    task = await store.create_task(session.id, goal="虚构新完成任务", now=1)
    await store.update_task(task.id, status="succeeded", finished_at=NOW, artifacts=["fresh.txt"])
    path = tmp_path / "sessions" / session.id / "tasks" / task.label
    path.mkdir(parents=True)
    (path / "fresh.txt").write_text("fake")
    cleaner.policy = RetentionConfig(reports_days=1, task_artifacts_days=1)
    assert await cleaner.run_once() == 0
    assert await store.get_report(report)
    assert (path / "fresh.txt").is_file()


async def test_ended_session_report_inflight_blocks_manual_and_automatic_cleanup(resources):
    from agentic_meeting.pipeline.report import ReportWorker

    store, _, cleaner = resources
    session = await store.create_session(now=1)
    await store.add_utterance(Utterance(session.id, 1, 0, 1, "虚构发言"), now=1)
    await store.end_session(session.id, now=2)
    entered, release = asyncio.Event(), asyncio.Event()

    class Model:
        async def run(self, messages, *, max_tokens):
            entered.set()
            await release.wait()
            return "虚构报告"

    report = ReportWorker(
        store=store,
        model=Model(),
        provider="fake",
        render_report=lambda **kw: "虚构提示",
        render_section=lambda **kw: "虚构分段",
        max_input_chars=1000,
        now=lambda: NOW,
    )
    rid = await report.start(session.id)
    await entered.wait()
    cleaner.policy = RetentionConfig(transcript_days=1)
    assert await cleaner.run_once() == 0
    with pytest.raises(SessionBusy):
        await cleaner.delete_session(session.id)
    release.set()
    await report.wait(session.id)
    assert (await store.get_report(rid)).status == "done"
    await report.stop()
    await cleaner.delete_session(session.id)
    assert await store.get_report(rid) is None


async def test_rmtree_executes_in_worker_thread_and_event_loop_remains_responsive(
    resources, tmp_path, monkeypatch
):
    from agentic_meeting.store import retention

    store, _, cleaner = resources
    session, *_ = await content(store, tmp_path)
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    original = retention.shutil.rmtree

    def paused(path, *args, **kwargs):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(5)
        original(path, *args, **kwargs)

    monkeypatch.setattr(retention.shutil, "rmtree", paused)
    deletion = asyncio.create_task(cleaner.delete_session(session.id))
    assert await asyncio.to_thread(entered.wait, 5)
    await asyncio.wait_for(asyncio.sleep(0.01), 1)
    assert not deletion.done()
    release.set()
    await deletion
    assert await store.get_session(session.id) is None


async def test_task_artifact_write_thread_drains_after_cancel_before_deletion(
    resources, tmp_path, monkeypatch
):
    import io

    from agentic_meeting.agent import sandbox
    from agentic_meeting.agent.tasks import TaskManager, task_dir
    from agentic_meeting.types import TaskResult

    store, _, cleaner = resources
    session = await store.create_session()
    entered, release = threading.Event(), threading.Event()
    original = sandbox._write_file

    class Box:
        async def read(self, path):
            return io.BytesIO(b"fake-artifact")

    def paused(path, data):
        entered.set()
        assert release.wait(5)
        original(path, data)

    async def run(task, on_event):
        names = await sandbox.SdkSandbox(Box()).fetch(["result.txt"], task_dir(tmp_path, task))
        return TaskResult("虚构完成", "", artifacts=names)

    async def notify(*args):
        pass

    monkeypatch.setattr(sandbox, "_write_file", paused)
    tasks = TaskManager(store=store, runner=run, notify=notify, max_concurrent=1, timeout_secs=5)
    record = await tasks.submit(session_id=session.id, goal="虚构任务")
    assert await asyncio.to_thread(entered.wait, 5)
    await store.end_session(session.id)
    cancellation = asyncio.create_task(tasks.cancel(record.id))
    await asyncio.sleep(0)
    cancellation.cancel()
    await asyncio.sleep(0)
    with pytest.raises(SessionBusy):
        await cleaner.delete_session(session.id)
    assert not cancellation.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await cancellation
    assert (await store.get_task(record.id)).status == "cancelled"
    await tasks.close()
    await cleaner.delete_session(session.id)
    assert not (tmp_path / "sessions" / session.id).exists()
    assert not store._work
