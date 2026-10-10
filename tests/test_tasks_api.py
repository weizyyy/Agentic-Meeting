"""后台任务的 HTTP 接口（interfaces.md §5.5）与导出里的任务一节。"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta, timezone
from pathlib import Path

import httpx
import pytest

from agentic_meeting.store.db import Store
from agentic_meeting.types import (
    Session,
    SessionSummary,
    TaskRecord,
    TaskResult,
    Utterance,
)
from agentic_meeting.web.app import create_app
from agentic_meeting.web.export import render_markdown
from agentic_meeting.web.tasks_api import _inside


class GatedRunner:
    def __init__(self):
        self.gate = asyncio.Event()
        self.started = asyncio.Event()

    async def __call__(self, task, on_event):
        await on_event("tool_call", "正在检索「引用数」", {"query": "引用数"})
        self.started.set()
        await self.gate.wait()
        return TaskResult(
            brief="被引 1243 次",
            detail_md="## 结论\n1243 次",
            sources=["https://example.org/a"],
            artifacts=[],
        )


class Env:
    def __init__(self, cfg, app, client, store, runner):
        self.cfg, self.app, self.client, self.store, self.runner = cfg, app, client, store, runner
        self.resources = app.state.resources
        self.data_dir = Path(cfg.resolve(cfg.session.data_dir))


@pytest.fixture
async def env(make_cfg, tmp_path):
    cfg = make_cfg()
    cfg.agent.base_url = "https://agent.example/v1"
    store = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    runner = GatedRunner()
    app = create_app(cfg, store=store, static_dir=tmp_path / "nope", task_runner=runner)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield Env(cfg, app, client, store, runner)
    await store.close()


async def test_list_detail_and_cancel(env):
    session = await env.store.create_session(now=1000.0)
    await env.store.add_utterance(Utterance(session.id, 2, 5.0, 7.0, "这篇引用过千了吧"))
    await env.store.rename_speaker(session.id, 2, "王老师")
    frame = await env.store.add_frame(session.id, t=6.0, width=16, height=9, suffix=".webp")
    task = await env.resources.tasks.submit(
        session_id=session.id,
        goal="核实这篇论文的引用数",
        requested_by=2,
        requested_t=8.0,
        transcript_window=(0.0, 8.0),
        frame_ids=[frame.id, 9999],
        modality="text",
    )
    await env.runner.started.wait()

    listing = (await env.client.get("/api/tasks", params={"session_id": session.id})).json()
    (item,) = listing["items"]
    assert (item["id"], item["label"], item["status"]) == (task.id, "t1", "running")
    assert item["goal"] == "核实这篇论文的引用数" and item["modality"] == "text"
    assert "detail_md" not in item  # 列表不含详细结果

    detail = (await env.client.get(f"/api/tasks/{task.id}")).json()
    assert detail["requested_by"] == "王老师" and detail["requested_t"] == 8.0
    assert [e["summary"] for e in detail["events"]] == ["开始处理", "正在检索「引用数」"]
    assert set(detail["events"][0]) == {"at", "kind", "summary"}  # 不把工具参数等细节给页面
    # 这次任务外发了什么：目标、转录范围、截图、发给谁
    assert detail["outbound"] == {
        "goal": "核实这篇论文的引用数",
        "t_from": 0.0,
        "t_to": 8.0,
        "frames": [{"id": frame.id, "t": 6.0}],
        "model_host": "agent.example",
    }

    env.runner.gate.set()
    await env.resources.tasks.wait(task.id)
    done = (await env.client.get(f"/api/tasks/{task.id}")).json()
    assert (done["status"], done["brief"]) == ("succeeded", "被引 1243 次")
    assert done["detail_md"] == "## 结论\n1243 次" and done["sources"] == ["https://example.org/a"]
    # 已经结束的任务：取消不报错，原样返回
    again = await env.client.post(f"/api/tasks/{task.id}/cancel")
    assert again.status_code == 200 and again.json()["status"] == "succeeded"


async def test_cancel_a_running_task(env):
    session = await env.store.create_session()
    task = await env.resources.tasks.submit(session_id=session.id, goal="查一下")
    await env.runner.started.wait()
    r = await env.client.post(f"/api/tasks/{task.id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert (await env.store.get_task(task.id)).status == "cancelled"


async def test_not_found_and_current_session(env):
    assert (await env.client.get("/api/tasks/nope.t1")).status_code == 404
    assert (await env.client.post("/api/tasks/nope.t1/cancel")).status_code == 404
    assert (await env.client.get("/api/tasks")).status_code == 404  # 没有任何会议
    r = await env.client.get("/api/tasks", params={"session_id": "nope"})
    assert r.status_code == 404 and r.json()["error"] == "找不到这场会议"
    session = await env.store.create_session()
    assert (await env.client.get("/api/tasks")).json() == {"items": []}  # 省略 = 当前会话
    other = await env.store.create_session(now=1.0)
    await env.store.create_task(other.id, goal="别的会议的任务")
    assert (await env.client.get("/api/tasks", params={"session_id": session.id})).json() == {
        "items": []
    }


async def test_artifacts_are_served_only_if_reported_and_inside_the_task_dir(env):
    session = await env.store.create_session()
    task = await env.store.create_task(session.id, goal="画图")
    await env.store.update_task(
        task.id, status="succeeded", artifacts=["plot.png", "out/data.csv", "page.html"]
    )
    workdir = env.data_dir / "sessions" / session.id / "tasks" / "t1"
    (workdir / "out").mkdir(parents=True)
    (workdir / "plot.png").write_bytes(b"\x89PNG")
    (workdir / "out" / "data.csv").write_bytes(b"a,b")
    (workdir / "page.html").write_bytes(b"<script>alert(1)</script>")
    (workdir / "secret.txt").write_bytes(b"not reported")

    base = f"/api/tasks/{task.id}/artifacts"
    image = await env.client.get(f"{base}/plot.png")
    assert image.status_code == 200 and image.content == b"\x89PNG"
    assert image.headers["content-type"] == "image/png"
    assert image.headers["x-content-type-options"] == "nosniff"
    nested = await env.client.get(f"{base}/out/data.csv")
    assert nested.status_code == 200 and nested.content == b"a,b"
    assert "attachment" in nested.headers["content-disposition"]
    # 网页之类的不让浏览器直接打开执行：一律当附件下载
    page = await env.client.get(f"{base}/page.html")
    assert page.headers["content-type"] == "application/octet-stream"
    assert "attachment" in page.headers["content-disposition"]
    # 没报告过的文件、目录之外的路径、不存在的文件
    assert (await env.client.get(f"{base}/secret.txt")).status_code == 404
    assert (await env.client.get(f"{base}/..%2F..%2F..%2Fmeetings.db")).status_code == 404
    (workdir / "plot.png").unlink()
    assert (await env.client.get(f"{base}/plot.png")).status_code == 404
    assert (await env.client.get("/api/tasks/nope.t1/artifacts/plot.png")).status_code == 404


def test_inside_accepts_only_paths_under_the_base(tmp_path):
    base = tmp_path / "base"
    (base / "sub").mkdir(parents=True)
    (base / "sub" / "a.txt").write_bytes(b"a")
    (tmp_path / "outside.txt").write_bytes(b"x")
    real = os.path.realpath(base / "sub" / "a.txt")

    assert _inside(str(base), "sub/a.txt") == real
    assert _inside(str(base), str(base / "sub" / "a.txt")) == real  # 绝对路径也行，只要在里面
    assert _inside(str(base), "missing.txt") is not None  # 存不存在由调用方判断
    assert _inside(str(base), "../outside.txt") is None
    assert _inside(str(base), "sub/../../outside.txt") is None
    assert _inside(str(base), str(tmp_path / "outside.txt")) is None
    assert _inside(str(base), ".") is None  # 目录本身不算「之内」
    assert _inside(str(base), str(tmp_path / "base-other" / "a.txt")) is None  # 只是前缀相同


def test_inside_rejects_a_symlink_that_points_outside(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    (tmp_path / "outside.txt").write_bytes(b"x")
    try:
        (base / "link.txt").symlink_to(tmp_path / "outside.txt")
    except OSError:
        pytest.skip("这个系统不允许当前用户创建符号链接")
    assert _inside(str(base), "link.txt") is None


async def test_app_recovers_leftover_tasks_on_startup_and_has_no_manager_when_disabled(
    make_cfg, tmp_path
):
    store = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    session = await store.create_session()
    leftover = await store.create_task(session.id, goal="上次没做完的")
    await store.update_task(leftover.id, status="running")
    app = create_app(make_cfg(), store=store, static_dir=tmp_path / "nope")
    async with app.router.lifespan_context(app):
        assert app.state.resources.tasks is not None
        stored = await store.get_task(leftover.id)
        assert stored.status == "failed" and "服务重启" in stored.error

    cfg = make_cfg()
    cfg.agent.enabled = False
    app = create_app(cfg, store=store, static_dir=tmp_path / "nope")
    async with app.router.lifespan_context(app):
        assert app.state.resources.tasks is None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            queued = await store.create_task(session.id, goal="开关关掉之后")
            r = await client.post(f"/api/tasks/{queued.id}/cancel")
            assert r.status_code == 503 and "没有开启" in r.json()["error"]
            assert (await client.get(f"/api/tasks/{queued.id}")).status_code == 200  # 仍然能看
    await store.close()


# --------------------------------------------------------------------------- #
# 导出里的任务一节
# --------------------------------------------------------------------------- #


def test_export_lists_task_results():
    tz = timezone(timedelta(hours=8))
    session = Session(id="s1", started_at=1791440580.0, title="周三组会", ended_at=1791444380.0)
    summary = SessionSummary(session, 3725.0, 0, [], [], False)
    tasks = [
        TaskRecord(
            id="s1.t1",
            session_id="s1",
            goal="核实这篇论文\n的引用数",
            status="succeeded",
            brief="被引 1243 次",
            detail_md="一、结论\n1243 次，见来源。\n\n\n二、*不确定*之处\n  # 无\n",
            sources=["https://example.org/a"],
            artifacts=["plot.png"],
        ),
        TaskRecord(
            id="s1.t2", session_id="s1", goal="查 *别的*", status="failed", error="连不上远端模型"
        ),
        TaskRecord(id="s1.t3", session_id="s1", goal="还在做的", status="running"),
    ]
    text = render_markdown(
        summary, state="ended", digest=None, utterances=[], frames=[], tasks=tasks, tz=tz
    )
    section = text.split("## 后台任务\n\n", 1)[1]
    assert section == (
        "### t1：核实这篇论文 的引用数\n"
        "\n"
        "- 状态：已完成\n"
        "- 结论：被引 1243 次\n"
        "\n"
        "一、结论  \n"
        "1243 次，见来源。\n"
        "\n"
        "二、\\*不确定\\*之处  \n"
        "\\# 无\n"
        "\n"
        "来源：\n"
        "\n"
        "- https://example.org/a\n"
        "\n"
        "产物文件：\n"
        "\n"
        "- `tasks/t1/plot.png`\n"
        "\n"
        "### t2：查 \\*别的\\*\n"
        "\n"
        "- 状态：失败\n"
        "- 原因：连不上远端模型\n"
        "\n"
        "### t3：还在做的\n"
        "\n"
        "- 状态：进行中\n"
    )
    # 没有任务时不出现这一节
    without = render_markdown(summary, state="ended", digest=None, utterances=[], frames=[], tz=tz)
    assert "后台任务" not in without


async def test_export_endpoint_includes_tasks(env):
    session = await env.store.create_session("周三组会", now=1000.0)
    task = await env.store.create_task(session.id, goal="核实引用数")
    await env.store.update_task(task.id, status="succeeded", brief="被引 1243 次")
    text = (await env.client.get(f"/api/export/{session.id}.md")).text
    assert "## 后台任务" in text and "### t1：核实引用数" in text and "被引 1243 次" in text
