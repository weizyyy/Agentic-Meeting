"""把截图原图附给模型（screen/attach.py），以及会后报告、滚动纪要带图。"""

from __future__ import annotations

import base64

import pytest

from agentic_meeting.pipeline.digest import DigestWorker
from agentic_meeting.pipeline.report import ReportWorker
from agentic_meeting.screen.attach import ATTACH_INTRO, FrameAttacher, select_frames
from agentic_meeting.store.db import Store
from agentic_meeting.types import ScreenFrame, Utterance

STARTED = 1_700_000_000.0


def shot(fid: int, t: float, caption: str | None) -> ScreenFrame:
    return ScreenFrame(
        "s1", t, f"sessions/s1/frames/{fid:06d}.webp", 16, 9, caption=caption, id=fid
    )


# --------------------------------------------------------------------------- #
# 挑哪些
# --------------------------------------------------------------------------- #


def test_select_drops_irrelevant_and_unchanged_frames_and_keeps_uncaptioned_ones():
    frames = [
        shot(3, 180.0, "无关画面"),
        shot(1, 60.0, "幻灯片：数据集划分"),
        shot(2, 120.0, "幻灯片：数据集划分"),  # 画面没变的兜底截图
        shot(4, 240.0, None),  # 摘要没生成：图本身还在，留着
        shot(5, 300.0, "幻灯片：结论"),
        shot(6, 360.0, "幻灯片：数据集划分"),  # 翻回去了：不相邻，算新的一张
    ]
    assert [f.id for f in select_frames(frames, 10)] == [1, 4, 5, 6]


def test_select_samples_evenly_when_over_the_limit_and_keeps_both_ends():
    frames = [shot(i, 60.0 * i, f"第 {i} 页") for i in range(1, 11)]
    assert [f.id for f in select_frames(frames, 4)] == [1, 4, 7, 10]
    assert [f.id for f in select_frames(frames, 1)] == [10]
    assert select_frames(frames, 0) == []
    assert len(select_frames(frames, 99)) == 10


# --------------------------------------------------------------------------- #
# 怎么排成消息
# --------------------------------------------------------------------------- #


def attacher_for(tmp_path, limit=10) -> FrameAttacher:
    return FrameAttacher(lambda frame: tmp_path / frame.path, limit)


def write(tmp_path, frame: ScreenFrame, data: bytes) -> None:
    path = tmp_path / frame.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


async def test_message_puts_the_prompt_first_then_each_image_with_its_time(tmp_path):
    first, second = shot(1, 65.0, "第一页"), shot(2, 3725.0, "第二页")
    write(tmp_path, first, b"one")
    write(tmp_path, second, b"two")
    message = await attacher_for(tmp_path).user_message("请写报告", [second, first])
    assert message["role"] == "user"
    kinds = [part["type"] for part in message["content"]]
    assert kinds == ["text", "text", "text", "image_url", "text", "image_url"]
    texts = [part["text"] for part in message["content"] if part["type"] == "text"]
    assert texts == ["请写报告", ATTACH_INTRO, "[画面 00:01:05]", "[画面 01:02:05]"]
    urls = [p["image_url"]["url"] for p in message["content"] if p["type"] == "image_url"]
    assert urls[0] == "data:image/webp;base64," + base64.b64encode(b"one").decode()


async def test_unreadable_files_are_skipped_and_no_images_means_a_plain_text_message(tmp_path):
    readable, missing = shot(1, 60.0, "第一页"), shot(2, 120.0, "第二页")
    write(tmp_path, readable, b"one")
    message = await attacher_for(tmp_path).user_message("p", [readable, missing])
    assert sum(part["type"] == "image_url" for part in message["content"]) == 1
    assert await attacher_for(tmp_path).user_message("p", [missing]) == {
        "role": "user",
        "content": "p",
    }
    assert await attacher_for(tmp_path).user_message("p", [shot(3, 1.0, "无关画面")]) == {
        "role": "user",
        "content": "p",
    }


# --------------------------------------------------------------------------- #
# 报告和纪要带图
# --------------------------------------------------------------------------- #


class RecordingModel:
    def __init__(self):
        self.messages: list[dict] = []

    async def run(self, messages, *, system="", max_tokens):
        self.messages.append(messages[0])
        return "模型写的内容"

    async def wait_resumed(self):
        return None


def image_count(message: dict) -> int:
    content = message["content"]
    return 0 if isinstance(content, str) else sum(p["type"] == "image_url" for p in content)


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    yield s
    await s.close()


async def meeting_with_frames(store, tmp_path, lines=6):
    session = await store.create_session("周三组会", now=STARTED)
    for i in range(lines):
        await store.add_utterance(
            Utterance(session.id, 1, i * 60.0, i * 60.0 + 5, f"第{i}句讨论学习率的设置")
        )
    for t, caption in ((30.0, "幻灯片：第一页"), (150.0, "无关画面"), (270.0, "幻灯片：第二页")):
        frame = await store.add_frame(session.id, t=t, width=16, height=9, suffix=".webp")
        write(tmp_path, frame, b"img")
        await store.set_frame_caption(frame.id, status="done", caption=caption)
    return session


def report_worker(store, model, tmp_path, *, attach: bool, max_input_chars=2000):
    return ReportWorker(
        store=store,
        model=model,
        provider="agent_llm",
        render_report=lambda **v: f"写报告：{v['material']}",
        render_section=lambda **v: f"提要点 {v['part']}：{v['transcript']}",
        max_input_chars=max_input_chars,
        attacher=attacher_for(tmp_path) if attach else None,
        now=lambda: STARTED + 4000,
    )


async def test_report_attaches_the_relevant_frames_only_when_asked(store, tmp_path):
    session = await meeting_with_frames(store, tmp_path)
    model = RecordingModel()
    await report_worker(store, model, tmp_path, attach=True).generate(session.id)
    assert image_count(model.messages[-1]) == 2  # 「无关画面」不带

    plain = RecordingModel()
    await report_worker(store, plain, tmp_path, attach=False).generate(session.id)
    assert isinstance(plain.messages[-1]["content"], str)


async def test_sectioned_report_attaches_each_sections_own_frames_and_none_to_the_merge(
    store, tmp_path
):
    session = await meeting_with_frames(store, tmp_path, lines=60)
    model = RecordingModel()
    worker = report_worker(store, model, tmp_path, attach=True, max_input_chars=500)
    await worker.generate(session.id)
    *sections, merge = model.messages
    assert len(sections) >= 2
    assert sum(image_count(m) for m in sections) == 2  # 每张图只跟着自己那一段
    assert image_count(merge) == 0


async def test_digest_attaches_the_frames_of_its_own_window(store, tmp_path):
    session = await meeting_with_frames(store, tmp_path)
    model = RecordingModel()
    worker = DigestWorker(
        store=store,
        model=model,
        render=lambda previous, new: f"此前：{previous}\n新增：{new}",
        interval_secs=300.0,
        current_session=lambda: None,
        attacher=attacher_for(tmp_path),
    )
    assert await worker.run_once(session.id) is not None
    assert image_count(model.messages[0]) == 2

    await store.add_utterance(Utterance(session.id, 1, 600.0, 605.0, "后面又说了一句"))
    assert await worker.run_once(session.id) is not None
    assert image_count(model.messages[1]) == 0  # 这一轮的时间段里没有新截图
