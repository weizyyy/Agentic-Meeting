"""画面摘要：假的后台模型 + 真实的 SQLite 临时库。"""

from __future__ import annotations

import asyncio
import base64

import pytest

from agentic_meeting.pipeline.background import BackgroundModel, Preempted
from agentic_meeting.screen.caption import (
    CAPTION_MAX_CHARS,
    IRRELEVANT_CAPTION,
    CaptionWorker,
    clean_caption,
    screen_line,
)
from agentic_meeting.screen.ingest import IngestedFrame
from agentic_meeting.store.db import Store

PROMPT = "用一句话描述截图"


class FakeModel:
    """假的后台模型入口：每次调用按顺序给出回答 / 抛异常；可以卡住等放行。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.replies: list = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.resumed = asyncio.Event()
        self.resumed.set()
        self.started = asyncio.Event()

    async def wait_resumed(self):
        await self.resumed.wait()

    async def run(self, messages, *, system="", max_tokens):
        self.calls.append({"messages": messages, "system": system, "max_tokens": max_tokens})
        self.started.set()
        await self.gate.wait()
        reply = self.replies.pop(0) if self.replies else f"摘要{len(self.calls)}"
        if isinstance(reply, BaseException):
            raise reply
        return reply


class Rig:
    def __init__(self, store, tmp_path, model):
        self.store, self.tmp_path, self.model = store, tmp_path, model
        self.messages: list[tuple[str, dict]] = []
        self.context: list[tuple[str, float, str]] = []
        self.worker = CaptionWorker(
            store=store,
            model=model,
            prompt=PROMPT,
            path_of=lambda frame: tmp_path / frame.path,
            notify=self.notify,
            append_context=self.append,
        )
        self.session_id = ""

    async def notify(self, session_id, data):
        self.messages.append((session_id, data))

    async def append(self, session_id, t, line):
        self.context.append((session_id, t, line))

    async def frame(self, t, *, changed=True, same_as=None, content=b"img"):
        frame = await self.store.add_frame(self.session_id, t=t, width=16, height=9, suffix=".webp")
        path = self.tmp_path / frame.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return IngestedFrame(frame, changed, same_as)

    async def status(self, ingested):
        f = await self.store.get_frame(ingested.frame.id)
        return f.caption_status, f.caption

    async def idle(self):
        """等后台循环把手头的事做完。"""
        for _ in range(400):
            await asyncio.sleep(0.005)
            if self.worker._pending is None and self.worker._current is None:
                return
        raise AssertionError("画面摘要一直没有做完")


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    yield s
    await s.close()


@pytest.fixture
async def rig(store, tmp_path):
    r = Rig(store, tmp_path, FakeModel())
    r.session_id = (await store.create_session(now=1000.0)).id
    r.worker.start()
    yield r
    await r.worker.stop()


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #


def test_screen_line_format():
    assert screen_line(870.4, "幻灯片：消融实验结果表") == "[画面 00:14:30] 幻灯片：消融实验结果表"


def test_clean_caption():
    assert clean_caption("  幻灯片：\n  结果表\t对比 ") == "幻灯片： 结果表 对比"
    assert clean_caption("") == ""
    long = "字" * (CAPTION_MAX_CHARS + 50)
    cleaned = clean_caption(long)
    assert len(cleaned) == CAPTION_MAX_CHARS + 1 and cleaned.endswith("…")
    assert clean_caption("字" * CAPTION_MAX_CHARS) == "字" * CAPTION_MAX_CHARS


# --------------------------------------------------------------------------- #
# 正常流程
# --------------------------------------------------------------------------- #


async def test_caption_is_stored_pushed_and_appended_to_context(rig):
    rig.model.replies = ["幻灯片：\n消融实验结果表"]
    item = await rig.frame(870.0, content=b"\x01\x02picture")
    await rig.worker.submit(item)
    await rig.idle()
    assert await rig.status(item) == ("done", "幻灯片： 消融实验结果表")
    assert rig.messages == [
        (
            rig.session_id,
            {"type": "frame_caption", "id": item.frame.id, "caption": "幻灯片： 消融实验结果表"},
        )
    ]
    assert rig.context == [(rig.session_id, 870.0, "[画面 00:14:30] 幻灯片： 消融实验结果表")]

    call = rig.model.calls[0]
    assert call["system"] == PROMPT and call["max_tokens"] > 0
    (message,) = call["messages"]
    assert message["role"] == "user"
    image = next(p for p in message["content"] if p["type"] == "image_url")
    expected = base64.b64encode(b"\x01\x02picture").decode()
    assert image["image_url"]["url"] == f"data:image/webp;base64,{expected}"
    assert any(p["type"] == "text" and p["text"] for p in message["content"])


async def test_irrelevant_screen_is_recorded_but_kept_out_of_context(rig):
    rig.model.replies = [IRRELEVANT_CAPTION]
    item = await rig.frame(5.0)
    await rig.worker.submit(item)
    await rig.idle()
    assert await rig.status(item) == ("done", IRRELEVANT_CAPTION)
    assert len(rig.messages) == 1 and rig.context == []


# --------------------------------------------------------------------------- #
# 积压、暂停、出错
# --------------------------------------------------------------------------- #


async def test_backlog_only_latest_frame_is_captioned(rig):
    rig.model.gate.clear()
    first = await rig.frame(1.0)
    await rig.worker.submit(first)
    await rig.model.started.wait()  # 第一张已经在做了
    second, third, fourth = [await rig.frame(t) for t in (2.0, 3.0, 4.0)]
    for item in (second, third, fourth):
        await rig.worker.submit(item)
    rig.model.gate.set()
    await rig.idle()
    assert (await rig.status(first))[0] == "done"
    assert (await rig.status(second))[0] == "skipped"
    assert (await rig.status(third))[0] == "skipped"
    assert (await rig.status(fourth))[0] == "done"
    assert len(rig.model.calls) == 2
    assert [line for _, _, line in rig.context] == [
        "[画面 00:00:01] 摘要1",
        "[画面 00:00:04] 摘要2",
    ]


async def test_pending_count_excludes_current_and_followers_during_replacement(rig, monkeypatch):
    completed = asyncio.Event()
    append = rig.worker._append_context

    async def appended(session_id, t, line):
        await append(session_id, t, line)
        if t == 4.0:
            completed.set()

    monkeypatch.setattr(rig.worker, "_append_context", appended)
    assert rig.worker.enabled and rig.worker.pending_count == 0
    rig.model.resumed.clear()
    first = await rig.frame(1.0)
    await rig.worker.submit(first)
    same = await rig.frame(2.0, changed=False, same_as=first.frame.id)
    await rig.worker.submit(same)
    assert rig.worker.pending_count == 1
    latest = await rig.frame(3.0)
    await rig.worker.submit(latest)
    assert rig.worker.pending_count == 1
    assert (await rig.status(first))[0] == (await rig.status(same))[0] == "skipped"
    rig.model.gate.clear()
    rig.model.resumed.set()
    await asyncio.wait_for(rig.model.started.wait(), 1.0)
    assert rig.worker.pending_count == 0  # 处理中不计槽位
    follower = await rig.frame(3.5, changed=False, same_as=latest.frame.id)
    await rig.worker.submit(follower)
    assert rig.worker.pending_count == 0
    pending = await rig.frame(4.0)
    await rig.worker.submit(pending)
    assert rig.worker.pending_count == 1
    rig.model.gate.set()
    await asyncio.wait_for(completed.wait(), 1.0)
    assert rig.worker.pending_count == 0
    assert await rig.status(follower) == ("done", "摘要1")
    assert await rig.status(pending) == ("done", "摘要2")
    assert [m[1]["id"] for m in rig.messages] == [
        latest.frame.id,
        follower.frame.id,
        pending.frame.id,
    ]


async def test_no_requests_while_paused_and_latest_wins_after_resume(rig):
    rig.model.resumed.clear()
    older = await rig.frame(1.0)
    await rig.worker.submit(older)
    await asyncio.sleep(0.05)
    assert rig.model.calls == []  # 暂停期间不发请求
    newer = await rig.frame(2.0)
    await rig.worker.submit(newer)
    await asyncio.sleep(0.05)
    assert rig.model.calls == []
    rig.model.resumed.set()
    await rig.idle()
    assert len(rig.model.calls) == 1
    assert (await rig.status(older))[0] == "skipped"
    assert (await rig.status(newer))[0] == "done"


async def test_preempted_frame_is_retried_after_resume(rig):
    rig.model.replies = [Preempted(), "终于写出来了"]
    item = await rig.frame(1.0)
    await rig.worker.submit(item)
    await rig.idle()
    assert await rig.status(item) == ("done", "终于写出来了")
    assert len(rig.model.calls) == 2


async def test_preempted_frame_is_dropped_when_a_newer_one_arrived(rig):
    rig.model.gate.clear()
    rig.model.replies = [Preempted(), "新画面"]
    old = await rig.frame(1.0)
    await rig.worker.submit(old)
    await rig.model.started.wait()
    new = await rig.frame(2.0)
    await rig.worker.submit(new)
    rig.model.gate.set()
    await rig.idle()
    assert (await rig.status(old))[0] == "skipped"
    assert await rig.status(new) == ("done", "新画面")


async def test_with_real_background_model_pause_cancels_and_resume_retries(store, tmp_path):
    class SlowLLM:
        def __init__(self):
            self.calls = 0
            self.started = asyncio.Event()
            self.gate = asyncio.Event()

        async def run_inference(self, context, max_tokens=None, system_instruction=None):
            self.calls += 1
            self.started.set()
            await self.gate.wait()
            return "一张表"

    llm = SlowLLM()
    model = BackgroundModel(llm)
    rig = Rig(store, tmp_path, model)
    rig.session_id = (await store.create_session(now=1000.0)).id
    rig.worker.start()
    try:
        item = await rig.frame(1.0)
        await rig.worker.submit(item)
        await llm.started.wait()
        model.pause()  # 助理被叫到名字
        await asyncio.sleep(0.05)
        assert (await rig.status(item))[0] == "pending" and llm.calls == 1
        llm.gate.set()
        model.resume()  # 助理这一轮结束
        await rig.idle()
        assert await rig.status(item) == ("done", "一张表")
        assert llm.calls == 2
    finally:
        await rig.worker.stop()


async def test_model_error_marks_failed_and_later_frames_still_work(rig):
    rig.model.replies = [RuntimeError("500"), "", "后面的正常"]
    bad, empty, good = [await rig.frame(t) for t in (1.0, 2.0, 3.0)]
    await rig.worker.submit(bad)
    await rig.idle()
    await rig.worker.submit(empty)
    await rig.idle()
    await rig.worker.submit(good)
    await rig.idle()
    assert await rig.status(bad) == ("failed", None)
    assert await rig.status(empty) == ("failed", None)  # 空的摘要也算失败
    assert await rig.status(good) == ("done", "后面的正常")
    assert [line for _, _, line in rig.context] == ["[画面 00:00:03] 后面的正常"]


async def test_missing_image_file_marks_failed(rig):
    item = await rig.frame(1.0)
    (rig.tmp_path / item.frame.path).unlink()
    await rig.worker.submit(item)
    await rig.idle()
    assert (await rig.status(item))[0] == "failed"
    assert rig.model.calls == []


async def test_notify_or_context_failure_does_not_lose_the_caption(store, tmp_path):
    rig = Rig(store, tmp_path, FakeModel())
    rig.session_id = (await store.create_session(now=1000.0)).id

    async def broken(*_args):
        raise RuntimeError("页面不在了")

    rig.worker._notify = broken
    rig.worker._append_context = broken
    rig.worker.start()
    try:
        first, second = await rig.frame(1.0), await rig.frame(2.0)
        await rig.worker.submit(first)
        await rig.idle()
        await rig.worker.submit(second)
        await rig.idle()
        assert (await rig.status(first))[0] == "done"
        assert (await rig.status(second))[0] == "done"
    finally:
        await rig.worker.stop()


# --------------------------------------------------------------------------- #
# 画面没变的截图
# --------------------------------------------------------------------------- #


async def test_unchanged_frame_reuses_finished_caption_without_calling_model(rig):
    first = await rig.frame(1.0)
    await rig.worker.submit(first)
    await rig.idle()
    same = await rig.frame(61.0, changed=False, same_as=first.frame.id)
    await rig.worker.submit(same)
    await rig.idle()
    assert await rig.status(same) == ("done", "摘要1")
    assert len(rig.model.calls) == 1
    # 页面收到这一张的摘要，但上下文里不再多一行
    assert [m[1]["id"] for m in rig.messages] == [first.frame.id, same.frame.id]
    assert len(rig.context) == 1


async def test_unchanged_frame_waits_for_caption_still_in_progress(rig):
    rig.model.gate.clear()
    first = await rig.frame(1.0)
    await rig.worker.submit(first)
    await rig.model.started.wait()
    same = await rig.frame(3.0, changed=False, same_as=first.frame.id)
    await rig.worker.submit(same)
    assert (await rig.status(same))[0] == "pending"
    rig.model.gate.set()
    await rig.idle()
    assert await rig.status(same) == ("done", "摘要1")
    assert len(rig.model.calls) == 1 and len(rig.context) == 1


async def test_unchanged_frame_waits_for_caption_still_queued(rig):
    rig.model.resumed.clear()
    first = await rig.frame(1.0)
    await rig.worker.submit(first)
    same = await rig.frame(3.0, changed=False, same_as=first.frame.id)
    await rig.worker.submit(same)
    rig.model.resumed.set()
    await rig.idle()
    assert await rig.status(first) == ("done", "摘要1")
    assert await rig.status(same) == ("done", "摘要1")
    assert len(rig.model.calls) == 1


async def test_followers_share_the_fate_of_a_skipped_or_failed_frame(rig):
    rig.model.resumed.clear()
    first = await rig.frame(1.0)
    await rig.worker.submit(first)
    same = await rig.frame(2.0, changed=False, same_as=first.frame.id)
    await rig.worker.submit(same)
    newer = await rig.frame(3.0)
    await rig.worker.submit(newer)  # first 被挤掉
    assert (await rig.status(first))[0] == "skipped"
    assert (await rig.status(same))[0] == "skipped"
    rig.model.replies = [RuntimeError("boom")]
    follower = await rig.frame(4.0, changed=False, same_as=newer.frame.id)
    await rig.worker.submit(follower)
    rig.model.resumed.set()
    await rig.idle()
    assert (await rig.status(newer))[0] == "failed"
    assert (await rig.status(follower))[0] == "failed"


async def test_unchanged_frame_retries_when_previous_caption_did_not_succeed(rig):
    rig.model.replies = [RuntimeError("boom"), "这次成了"]
    first = await rig.frame(1.0)
    await rig.worker.submit(first)
    await rig.idle()
    assert (await rig.status(first))[0] == "failed"
    # 一分钟后的兜底截图：画面没变，但上一张没有摘要 → 当作新画面再试一次
    heartbeat = await rig.frame(61.0, changed=False, same_as=first.frame.id)
    await rig.worker.submit(heartbeat)
    await rig.idle()
    assert await rig.status(heartbeat) == ("done", "这次成了")
    assert [line for _, _, line in rig.context] == ["[画面 00:01:01] 这次成了"]


# --------------------------------------------------------------------------- #
# 不生成摘要
# --------------------------------------------------------------------------- #


async def test_disabled_worker_marks_frames_skipped(store, tmp_path):
    rig = Rig(store, tmp_path, None)
    rig.session_id = (await store.create_session(now=1000.0)).id
    assert not rig.worker.enabled
    assert rig.worker.pending_count == 0
    rig.worker.start()  # 不启动任何任务
    assert rig.worker._task is None
    item = await rig.frame(1.0)
    await rig.worker.submit(item)
    assert rig.worker.pending_count == 0
    assert (await rig.status(item))[0] == "skipped"
    assert rig.messages == [] and rig.context == []
    await rig.worker.stop()


async def test_stop_is_idempotent_and_cancels_inflight_work(rig):
    rig.model.gate.clear()
    item = await rig.frame(1.0)
    await rig.worker.submit(item)
    await rig.model.started.wait()
    await rig.worker.stop()
    await rig.worker.stop()
    assert (await rig.status(item))[0] == "pending"
