"""会后报告：假模型 + 真实的 SQLite 临时库；接口用 ``httpx.ASGITransport`` 直接调应用。"""

from __future__ import annotations

import asyncio
from datetime import timedelta, timezone
from urllib.parse import unquote

import httpx
import pytest

from agentic_meeting.pipeline.background import Preempted
from agentic_meeting.pipeline.prompts import load_prompt
from agentic_meeting.pipeline.report import (
    INTERRUPTED_REASON,
    NOTHING,
    ReportBusy,
    ReportUnavailable,
    ReportWorker,
    frames_text,
    split_sections,
    tasks_text,
    transcript_lines,
)
from agentic_meeting.store.db import Store
from agentic_meeting.types import (
    SPEAKER_ASSISTANT,
    SPEAKER_TYPED,
    NamedUtterance,
    ScreenFrame,
    TaskRecord,
    Utterance,
)
from agentic_meeting.web.app import create_app

TZ = timezone(timedelta(hours=8))
STARTED = 1791440580.0  # 2026-10-08 14:23（东八区）


class FakeModel:
    """报告用的假模型：记下每次的提示词；``replies`` 里可以放文字或要抛出的异常。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.replies: list = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.resumes = 0

    async def run(self, messages, *, system="", max_tokens):
        self.calls.append({"prompt": messages[0]["content"], "max_tokens": max_tokens})
        await self.gate.wait()
        reply = self.replies.pop(0) if self.replies else f"## 概要\n\n第 {len(self.calls)} 次的回答"
        if isinstance(reply, BaseException):
            raise reply
        return reply

    async def wait_resumed(self):
        self.resumes += 1


class FakeDigests:
    def __init__(self, error: BaseException | None = None):
        self.caught_up: list[str] = []
        self.error = error

    async def catch_up(self, session_id):
        self.caught_up.append(session_id)
        if self.error is not None:
            raise self.error


def render_report(**values):
    return "报告|" + "|".join(f"{k}={v}" for k, v in sorted(values.items()))


def render_section(**values):
    return f"分段 {values['part']}/{values['total']} {values['span']}\n{values['transcript']}"


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    yield s
    await s.close()


def worker_for(store, model, **kw):
    kw.setdefault("provider", "realtime_llm")
    kw.setdefault("render_report", render_report)
    kw.setdefault("render_section", render_section)
    kw.setdefault("max_input_chars", 2000)
    kw.setdefault("now", lambda: STARTED + 4000)
    kw.setdefault("tz", TZ)
    return ReportWorker(store=store, model=model, **kw)


async def meeting(store, lines=6, *, title="周三组会"):
    session = await store.create_session(title, now=STARTED)
    conn = await store.open_connection(session.id, connected_at=STARTED, t_from=0.0)
    for i in range(lines):
        await store.add_utterance(
            Utterance(session.id, 1 + i % 2, i * 60.0, i * 60.0 + 5, f"第{i}句讨论学习率")
        )
    await store.rename_speaker(session.id, 1, "王老师")
    await store.close_connection(conn, disconnected_at=STARTED + 1500, t_to=1500.0)
    await store.end_session(session.id, now=STARTED + 1500)
    return session


# --------------------------------------------------------------------------- #
# 整理素材（纯函数）
# --------------------------------------------------------------------------- #


def named(uid, speaker, name, t, text, source="asr"):
    return NamedUtterance(Utterance("s1", speaker, t, t + 2, text, source=source, id=uid), name)


def shot(fid, t, caption):
    return ScreenFrame(
        "s1", t, f"sessions/s1/frames/{fid:06d}.webp", 16, 9, caption=caption, id=fid
    )


def test_transcript_lines_mark_typed_text_and_interleave_screens():
    lines = transcript_lines(
        [
            named(1, 1, "王老师", 5.0, "先看基线"),
            named(2, SPEAKER_TYPED, "文字输入", 65.0, "把学习率记一下", source="text"),
            named(3, SPEAKER_ASSISTANT, "Nova", 66.0, "记下了", source="assistant"),
        ],
        [
            shot(1, 5.0, "幻灯片：基线"),
            shot(2, 30.0, "幻灯片：基线"),  # 画面没变：不重复
            shot(3, 40.0, "无关画面"),
            shot(4, 50.0, None),
            shot(5, 70.0, "消融表"),
        ],
    )
    assert lines == [
        (5.0, "[00:00:05 王老师] 先看基线"),
        (5.0, "[画面 00:00:05] 幻灯片：基线"),
        (65.0, "[00:01:05 文字输入（打字）] 把学习率记一下"),
        (66.0, "[00:01:06 Nova] 记下了"),
        (70.0, "[画面 00:01:10] 消融表"),
    ]


def test_sections_fill_up_to_the_limit_and_prefer_digest_boundaries():
    lines = [(float(i * 10), "字" * 9) for i in range(10)]  # 每行算 10 个字
    # 没有纪要的边界：写满才切
    assert [len(s) for s in split_sections(lines, [], 40)] == [4, 4, 2]
    # 第 30 秒是一份纪要的终点：那时这一段才 3 行（不到一半的 60），不切；到 60 秒那个边界时已经过半，切
    assert [len(s) for s in split_sections(lines, [30.0, 60.0], 100)] == [6, 4]
    # 一行就超过上限：自成一段，不丢
    long = [(0.0, "短"), (1.0, "长" * 500), (2.0, "短")]
    assert [len(s) for s in split_sections(long, [], 50)] == [1, 1, 1]
    assert split_sections([], [10.0], 50) == []
    # 每一行都在，顺序不变
    flat = [line for section in split_sections(lines, [25.0, 55.0], 30) for line in section]
    assert flat == lines


def test_tasks_and_frames_text():
    tasks = [
        TaskRecord(
            id="s1.t1", session_id="s1", goal="核实\n引用数", status="succeeded", brief="1243 次"
        ),
        TaskRecord(id="s1.t2", session_id="s1", goal="查别的", status="failed", error="连不上"),
        TaskRecord(id="s1.t3", session_id="s1", goal="还在做", status="running"),
    ]
    assert tasks_text(tasks).splitlines() == [
        "t1（已完成）目标：核实 引用数；结论：1243 次",
        "t2（失败）目标：查别的；原因：连不上",
        "t3（进行中）目标：还在做",
    ]
    assert tasks_text([]) == NOTHING
    frames = [shot(i, i * 10.0, f"画面{i // 2}") for i in range(8)]
    assert frames_text(frames).splitlines() == [
        "00:00:00 画面0",
        "00:00:20 画面1",
        "00:00:40 画面2",
        "00:01:00 画面3",
    ]
    assert frames_text(frames, limit=2).splitlines()[-1] == "（后面还有 2 张，从略）"
    assert frames_text([shot(1, 0.0, "无关画面"), shot(2, 1.0, None)]) == NOTHING


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #


async def test_a_short_transcript_is_one_request(store):
    session = await meeting(store)
    frame = await store.add_frame(session.id, t=70.0, width=16, height=9, suffix=".webp")
    await store.set_frame_caption(frame.id, status="done", caption="消融表")
    await store.add_digest(session.id, t_from=0, t_to=300, text="一、学习率", last_utterance_id=6)
    task = await store.create_task(session.id, goal="核实引用数")
    await store.update_task(task.id, status="succeeded", brief="1243 次")
    model, digests = FakeModel(), FakeDigests()
    model.replies = ["## 概要\n\n讨论了学习率。"]
    worker = worker_for(store, model, digests=digests)

    report_id = await worker.start(session.id)
    assert worker.is_running(session.id)
    await worker.wait(session.id)
    assert not worker.is_running(session.id)

    assert digests.caught_up == [session.id]  # 先把纪要补到最后
    assert len(model.calls) == 1 and model.calls[0]["max_tokens"] == 4096
    prompt = model.calls[0]["prompt"]
    assert "material_name=完整的会议转录" in prompt
    assert "[00:00:00 王老师] 第0句讨论学习率" in prompt and "[00:05:00 说话人 2] 第5句" in prompt
    assert "[画面 00:01:10] 消融表" in prompt
    assert "digest=一、学习率" in prompt
    assert "tasks=t1（已完成）目标：核实引用数；结论：1243 次" in prompt
    assert "frames=00:01:10 消融表" in prompt
    assert "标题：周三组会" in prompt and "发言人：王老师、说话人 2" in prompt

    report = await store.latest_report(session.id)
    assert (report.id, report.status, report.provider, report.error) == (
        report_id,
        "done",
        "realtime_llm",
        None,
    )
    assert report.text_md == (
        "# 周三组会 · 会后报告\n"
        "\n"
        "- 开始时间：2026-10-08 14:23\n"
        "- 实际时长：25 分钟\n"
        "- 发言人：王老师、说话人 2\n"
        "- 报告生成于：2026-10-08 15:29\n"
        "\n"
        "## 概要\n"
        "\n"
        "讨论了学习率。\n"
    )


async def test_a_long_transcript_is_summarised_in_sections_then_merged(store):
    session = await meeting(store, lines=30)
    await store.add_digest(session.id, t_from=0, t_to=600, text="旧纪要", last_utterance_id=10)
    await store.add_digest(session.id, t_from=600, t_to=1750, text="新纪要", last_utterance_id=30)
    model = FakeModel()
    worker = worker_for(store, model, max_input_chars=300)
    await worker.start(session.id)
    await worker.wait(session.id)

    section_calls, final = model.calls[:-1], model.calls[-1]
    assert len(section_calls) >= 3
    total = len(section_calls)
    seen = []
    for index, call in enumerate(section_calls, start=1):
        head, *body = call["prompt"].splitlines()
        assert head.startswith(f"分段 {index}/{total} ")
        assert call["max_tokens"] == 1024
        assert sum(len(line) + 1 for line in body) <= 300
        seen += body
    assert len(seen) == 30 and seen[0].startswith("[00:00:00 ") and "第29句" in seen[-1]
    # 合并：给模型的是各段的要点，不再是转录
    prompt = final["prompt"]
    assert f"material_name=各段的要点（转录太长，已经分成 {total} 段分别提过要点）" in prompt
    assert "【第 1 段 00:00:00–" in prompt and f"【第 {total} 段 " in prompt
    assert "第0句讨论学习率" not in prompt
    assert "digest=新纪要" in prompt  # 最新那份累积纪要
    assert (await store.latest_report(session.id)).status == "done"


async def test_failures_are_recorded_with_a_reason(store):
    session = await meeting(store)
    model = FakeModel()
    model.replies = [RuntimeError("模型服务 500")]
    worker = worker_for(store, model)
    await worker.start(session.id)
    await worker.wait(session.id)
    report = await store.latest_report(session.id)
    assert report.status == "failed" and report.error == "RuntimeError: 模型服务 500"
    assert await store.latest_report(session.id, done_only=True) is None

    model.replies = [""]  # 模型给了空的
    await worker.start(session.id)  # 失败之后可以重新生成
    await worker.wait(session.id)
    assert (await store.latest_report(session.id)).error.startswith(
        "RuntimeError: 模型给出的报告是空的"
    )

    empty = await store.create_session("没人说话", now=STARTED)
    await worker.start(empty.id)
    await worker.wait(empty.id)
    assert "没有发言记录" in (await store.latest_report(empty.id)).error

    slow = FakeModel()
    slow.gate.clear()
    timed = worker_for(store, slow, timeout_secs=0.05)
    await timed.start(session.id)
    await timed.wait(session.id)
    assert (await store.latest_report(session.id)).error == "生成超时"


async def test_only_one_report_per_session_at_a_time_and_none_without_a_model(store):
    session = await meeting(store)
    other = await meeting(store, title="另一场")
    model = FakeModel()
    model.gate.clear()
    worker = worker_for(store, model)
    first = await worker.start(session.id)
    with pytest.raises(ReportBusy):
        await worker.start(session.id)
    await worker.start(other.id)  # 别的会议不受影响
    model.gate.set()
    await worker.wait(session.id)
    await worker.wait(other.id)
    second = await worker.start(session.id)  # 生成完了可以再来一份，页面显示最近的
    await worker.wait(session.id)
    assert second > first and (await store.latest_report(session.id)).id == second

    none = worker_for(store, None)
    assert not none.enabled
    with pytest.raises(ReportUnavailable):
        await none.start(session.id)


async def test_a_preempted_request_waits_for_the_assistant_and_retries(store):
    session = await meeting(store)
    model = FakeModel()
    model.replies = [Preempted(), Preempted(), "## 概要\n\n写完了"]
    # 补纪要被抢占、出错都不拦着报告
    worker = worker_for(store, model, digests=FakeDigests(Preempted()))
    await worker.start(session.id)
    await worker.wait(session.id)
    assert model.resumes == 2 and len(model.calls) == 3
    assert (await store.latest_report(session.id)).status == "done"

    broken = worker_for(store, FakeModel(), digests=FakeDigests(RuntimeError("纪要坏了")))
    await broken.start(session.id)
    await broken.wait(session.id)
    assert (await store.latest_report(session.id)).status == "done"


async def test_stopping_and_restarting_marks_unfinished_reports_failed(store):
    session = await meeting(store)
    model = FakeModel()
    model.gate.clear()
    worker = worker_for(store, model)
    await worker.start(session.id)
    await asyncio.sleep(0.05)
    await worker.stop()  # 服务停止
    report = await store.latest_report(session.id)
    assert (report.status, report.error) == ("failed", INTERRUPTED_REASON)

    # 进程被强杀时来不及改状态：下次启动时补上
    stuck = await store.create_report(session.id, provider="realtime_llm")
    assert await worker_for(store, FakeModel()).recover() == 1
    report = await store.latest_report(session.id)
    assert (report.id, report.status, report.error) == (stuck, "failed", INTERRUPTED_REASON)


def test_the_real_prompts_take_every_variable_the_worker_provides():
    report = load_prompt(
        "report",
        meeting_info="标题：周三组会",
        digest="一、学习率",
        material_name="完整的会议转录",
        material="[00:00:05 王老师] 先看基线",
        tasks=NOTHING,
        frames=NOTHING,
    )
    assert "未提及" in report and "不要编造" in report  # 没有的信息不许编
    assert "（打字）" in report and "[00:00:05 王老师] 先看基线" in report
    section = load_prompt(
        "report_section", part=2, total=5, span="00:10:00–00:20:00", transcript="[00:10:00 甲] 话"
    )
    assert "第 2 段" in section and "5 段" in section and "[00:10:00 甲] 话" in section


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #


class FakeLLM:
    """``create_app(background_llm=…)`` 要的那种模型服务：``run_inference`` 返回文字。"""

    def __init__(self):
        self.gate = asyncio.Event()
        self.gate.set()
        self.prompts: list[str] = []

    async def run_inference(self, context, max_tokens=None, system_instruction=None):
        self.prompts.append(str(context.get_messages()[0]["content"]))
        await self.gate.wait()
        return "## 概要\n\n接口生成的报告"


@pytest.fixture
async def env(make_cfg, tmp_path):
    store = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    llm = FakeLLM()
    app = create_app(make_cfg(), store=store, background_llm=llm, static_dir=tmp_path / "nope")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, store, app, llm
    await store.close()


async def test_report_endpoints_generate_poll_and_download(env):
    client, store, app, llm = env
    session = await meeting(store)
    url = f"/api/sessions/{session.id}/report"
    missing = await client.get(url)
    assert missing.status_code == 404 and missing.json() == {"error": "这场会议还没有报告"}
    assert (await client.get(f"{url}.md")).status_code == 404

    llm.gate.clear()
    started = await client.post(url)
    assert started.status_code == 202
    report_id = started.json()["report_id"]
    assert started.json() == {"report_id": report_id, "status": "running"}
    running = (await client.get(url)).json()
    assert (running["id"], running["status"], running["text_md"]) == (report_id, "running", "")
    busy = await client.post(url)
    assert busy.status_code == 409 and "正在生成" in busy.json()["error"]
    assert (await client.get(f"{url}.md")).status_code == 404  # 还没生成好

    llm.gate.set()
    await app.state.resources.reports.wait(session.id)
    done = (await client.get(url)).json()
    assert set(done) == {"id", "status", "created_at", "provider", "text_md", "error"}
    assert (done["status"], done["provider"], done["error"]) == ("done", "realtime_llm", None)
    assert (
        done["text_md"].startswith("# 周三组会 · 会后报告\n")
        and "接口生成的报告" in done["text_md"]
    )
    # 真实的提示词 + 这场会议的转录交给了模型（滚动纪要那一次请求在前，报告在最后）
    assert "你在为一场课题组组会写会后报告" in llm.prompts[-1]
    assert "[00:00:00 王老师] 第0句讨论学习率" in llm.prompts[-1]

    download = await client.get(f"{url}.md")
    assert download.status_code == 200 and download.text == done["text_md"]
    assert download.headers["content-type"] == "text/markdown; charset=utf-8"
    disposition = download.headers["content-disposition"]
    assert f'filename="report-{session.id[:8]}.md"' in disposition
    assert unquote(disposition.split("filename*=UTF-8''", 1)[1]) == "周三组会 会后报告.md"


async def test_report_endpoint_errors(env, make_cfg, tmp_path):
    client, store, app, _ = env
    for method, path in (
        ("POST", "/api/sessions/nope/report"),
        ("GET", "/api/sessions/nope/report"),
        ("GET", "/api/sessions/nope/report.md"),
    ):
        r = await client.request(method, path)
        assert r.status_code == 404 and r.json() == {"error": "找不到这场会议"}, path

    manager = app.state.resources.sessions
    live = await manager.begin()
    r = await client.post(f"/api/sessions/{live.session.id}/report")
    assert r.status_code == 409 and "正在进行" in r.json()["error"]
    await manager.finish(live)

    # 没有后台模型（比如只注入了存储）：说清楚生成不了
    bare = await Store.open(tmp_path / "bare.db", 4)
    bare_app = create_app(make_cfg(), store=bare, static_dir=tmp_path / "nope")
    async with (
        bare_app.router.lifespan_context(bare_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=bare_app), base_url="http://test"
        ) as bare_client,
    ):
        session = await bare.create_session("会")
        r = await bare_client.post(f"/api/sessions/{session.id}/report")
        assert r.status_code == 503 and "没有可用的模型" in r.json()["error"]
    await bare.close()


async def test_startup_fails_reports_left_running_by_a_crash(make_cfg, tmp_path):
    store = await Store.open(tmp_path / "crash.db", 4)
    session = await store.create_session("会")
    await store.create_report(session.id, provider="realtime_llm")
    app = create_app(make_cfg(), store=store, static_dir=tmp_path / "nope")
    async with app.router.lifespan_context(app):
        report = await store.latest_report(session.id)
        assert (report.status, report.error) == ("failed", INTERRUPTED_REASON)
    await store.close()


async def test_agent_llm_provider_uses_the_remote_model(make_cfg, tmp_path):
    cfg = make_cfg()
    cfg.report.provider = "agent_llm"
    local, remote = FakeLLM(), FakeLLM()
    store = await Store.open(tmp_path / "remote.db", 4)
    app = create_app(
        cfg, store=store, background_llm=local, agent_llm=remote, static_dir=tmp_path / "nope"
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        session = await meeting(store)
        assert (await client.post(f"/api/sessions/{session.id}/report")).status_code == 202
        await app.state.resources.reports.wait(session.id)
        report = (await client.get(f"/api/sessions/{session.id}/report")).json()
    await store.close()
    assert (report["status"], report["provider"]) == ("done", "agent_llm")
    assert any("会后报告" in p for p in remote.prompts)
    assert not any("会后报告" in p for p in local.prompts)  # 本机的后台模型没有被用来写报告
