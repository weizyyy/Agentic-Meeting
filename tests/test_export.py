"""导出：Markdown 转录、结构化 JSON 和压缩包。给定一组假数据，对导出的内容做快照式断言。"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import timedelta, timezone
from urllib.parse import unquote

import httpx
import pytest

from agentic_meeting.store.db import Store
from agentic_meeting.types import (
    SPEAKER_ASSISTANT,
    SPEAKER_TYPED,
    Digest,
    NamedUtterance,
    ScreenFrame,
    Session,
    SessionSummary,
    Utterance,
)
from agentic_meeting.web.app import create_app
from agentic_meeting.web.export import (
    download_headers,
    escape,
    format_duration,
    join_text,
    render_markdown,
)

TZ = timezone(timedelta(hours=8))
STARTED = 1791440580.0  # 2026-10-08 14:23（东八区）


def said(uid, speaker, name, t, text, *, source="asr", length=2.0):
    return NamedUtterance(
        Utterance("s1", speaker, t, t + length, text, source=source, id=uid), name
    )


def shot(fid, t, caption):
    return ScreenFrame(
        "s1", t, f"sessions/s1/frames/{fid:06d}.webp", 16, 9, caption=caption, id=fid
    )


def summary(title="周三组会", ended=True, speakers=("王老师", "说话人 2"), duration=3725.0):
    session = Session(
        id="s1",
        started_at=STARTED,
        title=title,
        ended_at=STARTED + 3800 if ended else None,
        last_active_at=STARTED + 3800,
    )
    return SessionSummary(session, duration, 6, list(speakers), [], False)


def test_markdown_snapshot():
    text = render_markdown(
        summary(),
        state="ended",
        digest=Digest(1, "s1", 0, 2530.0, "一、基线复现\n王老师汇报了新的学习率设置。", 0.0),
        utterances=[
            said(1, 1, "王老师", 842.0, "这个 baseline 的学习率"),
            said(2, 1, "王老师", 845.0, "是不是设大了"),
            said(3, 2, "说话人 2", 849.0, "我回去再跑一组对比"),
            said(4, SPEAKER_TYPED, "文字输入", 860.0, "帮我查一下这篇论文的引用数", source="text"),
            said(5, SPEAKER_ASSISTANT, "Nova", 861.0, "好的，我去查。", source="assistant"),
            said(6, 1, "王老师", 900.0, "下一页"),
        ],
        frames=[
            shot(1, 847.0, "幻灯片：消融实验结果表"),
            shot(2, 880.0, "幻灯片：消融实验结果表"),  # 画面没变：不重复
            shot(3, 890.0, "无关画面"),
            shot(4, 905.0, None),  # 没有摘要：只放图
        ],
        tz=TZ,
    )
    assert text == (
        "# 周三组会\n"
        "\n"
        "- 开始时间：2026-10-08 14:23\n"
        "- 结束时间：2026-10-08 15:26\n"
        "- 状态：已结束\n"
        "- 时长：1 小时 02 分\n"
        "- 发言人：王老师、说话人 2\n"
        "\n"
        "## 纪要\n"
        "\n"
        "一、基线复现\n"
        "王老师汇报了新的学习率设置。\n"
        "\n"
        "（纪要覆盖到 00:42:10。）\n"
        "\n"
        "## 转录\n"
        "\n"
        "**王老师** `00:14:02`\n"
        "\n"
        "这个 baseline 的学习率是不是设大了\n"
        "\n"
        "> **画面** `00:14:07`：幻灯片：消融实验结果表\n"
        ">\n"
        "> ![00:14:07 的屏幕截图](frames/000001.webp)\n"
        "\n"
        "**说话人 2** `00:14:09`\n"
        "\n"
        "我回去再跑一组对比\n"
        "\n"
        "**文字输入**（文字） `00:14:20`\n"
        "\n"
        "帮我查一下这篇论文的引用数\n"
        "\n"
        "**Nova** `00:14:21`\n"
        "\n"
        "好的，我去查。\n"
        "\n"
        "**王老师** `00:15:00`\n"
        "\n"
        "下一页\n"
        "\n"
        "> **画面** `00:15:05`\n"
        ">\n"
        "> ![00:15:05 的屏幕截图](frames/000004.webp)\n"
    )


def test_empty_meeting_and_untitled():
    text = render_markdown(
        summary(title="  ", ended=False, speakers=(), duration=20.0),
        state="interrupted",
        digest=None,
        utterances=[],
        frames=[],
        tz=TZ,
    )
    assert text == (
        "# 未命名会议 2026-10-08 14:23\n"
        "\n"
        "- 开始时间：2026-10-08 14:23\n"
        "- 状态：已中断\n"
        "- 时长：不到 1 分钟\n"
        "\n"
        "## 纪要\n"
        "\n"
        "（这场会议没有生成纪要。）\n"
        "\n"
        "## 转录\n"
        "\n"
        "（这场会议没有发言记录。）\n"
    )


def body_of(text: str) -> list[str]:
    return text.split("## 转录\n\n", 1)[1].splitlines()


def test_same_speaker_merges_only_when_adjacent_close_and_same_source():
    text = render_markdown(
        summary(),
        state="live",
        digest=None,
        utterances=[
            said(1, 1, "甲", 10.0, "第一句"),
            said(2, 1, "甲", 13.0, "第二句"),
            said(3, 1, "甲", 13.0 + 2.0 + 121.0, "隔了两分多钟才说的"),  # 另起一段
            said(4, 2, "乙", 200.0, "插一句"),
            said(5, 1, "甲", 203.0, "接着说"),  # 中间隔了别人：另起一段
            said(6, SPEAKER_TYPED, "文字输入", 210.0, "第一条", source="text"),
            said(7, SPEAKER_TYPED, "文字输入", 211.0, "第二条", source="text"),
        ],
        frames=[],
        tz=TZ,
    )
    lines = [line for line in body_of(text) if line]
    assert lines == [
        "**甲** `00:00:10`",
        "第一句第二句",
        "**甲** `00:02:16`",
        "隔了两分多钟才说的",
        "**乙** `00:03:20`",
        "插一句",
        "**甲** `00:03:23`",
        "接着说",
        "**文字输入**（文字） `00:03:30`",
        "第一条第二条",
    ]
    assert "- 状态：进行中" in text


def test_a_screen_change_splits_a_speakers_paragraph():
    text = render_markdown(
        summary(),
        state="ended",
        digest=None,
        utterances=[said(1, 1, "甲", 10.0, "看这一页"), said(2, 1, "甲", 14.0, "再看这一页")],
        frames=[shot(7, 12.0, "第二页")],
        tz=TZ,
    )
    lines = [line for line in body_of(text) if line]
    assert lines == [
        "**甲** `00:00:10`",
        "看这一页",
        "> **画面** `00:00:12`：第二页",
        ">",
        "> ![00:00:12 的屏幕截图](frames/000007.webp)",
        "**甲** `00:00:14`",
        "再看这一页",
    ]


def test_helpers():
    assert join_text("用了 LoRA", "微调") == "用了 LoRA微调"
    assert join_text("learning", "rate") == "learning rate"
    assert join_text("第 3", "5 组") == "第 3 5 组"
    assert join_text("", "开头") == "开头"
    assert escape("a*b_c #1 [x] <y> `z` | \\") == "a\\*b\\_c \\#1 \\[x\\] \\<y\\> \\`z\\` \\| \\\\"
    assert [format_duration(s) for s in (0, 59, 60, 3599, 3600, 3725)] == [
        "不到 1 分钟",
        "不到 1 分钟",
        "1 分钟",
        "59 分钟",
        "1 小时 00 分",
        "1 小时 02 分",
    ]


def test_markdown_symbols_in_transcript_are_escaped():
    text = render_markdown(
        summary(title="# 不是标题", speakers=("*甲*",)),
        state="ended",
        digest=None,
        utterances=[said(1, 1, "*甲*", 1.0, "# 这不是标题 *也不加粗*")],
        frames=[shot(1, 2.0, "表格 | 两列")],
        tz=TZ,
    )
    assert text.startswith("# \\# 不是标题\n")
    assert "**\\*甲\\*** `00:00:01`" in text
    assert "\\# 这不是标题 \\*也不加粗\\*" in text
    assert "表格 \\| 两列" in text


def test_download_headers_carry_the_title_safely():
    headers = download_headers('周三/组会: "基线"\n复现', "abcdef0123456789")
    value = headers["Content-Disposition"]
    assert 'filename="meeting-abcdef01.md"' in value  # 纯 ASCII 的后备名
    encoded = value.split("filename*=UTF-8''", 1)[1]
    assert unquote(encoded) == "周三 组会 基线 复现.md"
    assert "\n" not in value and '"基线"' not in value
    assert unquote(download_headers("  ", "s").get("Content-Disposition")).endswith("meeting.md")


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #


@pytest.fixture
async def env(make_cfg, tmp_path):
    store = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    app = create_app(make_cfg(), store=store, static_dir=tmp_path / "nope")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, store, app
    await store.close()


async def test_export_endpoint_returns_markdown_download(env):
    client, store, _ = env
    session = await store.create_session("周三组会", now=STARTED)
    other = await store.create_session("别的会", now=STARTED)
    await store.add_utterance(Utterance(session.id, 1, 5.0, 7.0, "开始吧"))
    await store.add_utterance(Utterance(session.id, 1, 8.0, 9.0, "先看基线"))
    await store.add_utterance(Utterance(other.id, 1, 1.0, 2.0, "别的会议的话"))
    await store.rename_speaker(session.id, 1, "王老师")
    frame = await store.add_frame(session.id, t=6.0, width=16, height=9, suffix=".webp")
    await store.set_frame_caption(frame.id, status="done", caption="幻灯片：议程")
    await store.add_digest(session.id, t_from=0, t_to=9.0, text="一、议程", last_utterance_id=2)
    await store.end_session(session.id, now=STARTED + 600)

    r = await client.get(f"/api/export/{session.id}.md")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/markdown; charset=utf-8"
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert unquote(disposition.split("filename*=UTF-8''", 1)[1]) == "周三组会.md"
    text = r.text
    assert text.startswith("# 周三组会\n")
    assert "- 状态：已结束" in text and "- 发言人：王老师" in text
    assert "一、议程" in text
    assert "**王老师** `00:00:05`" in text
    assert f"frames/{frame.id:06d}.webp" in text and "幻灯片：议程" in text
    assert "别的会议的话" not in text
    # 画面把同一个人的两句隔开了
    assert text.index("开始吧") < text.index("幻灯片：议程") < text.index("先看基线")


async def test_export_endpoint_states_and_errors(env):
    client, store, app = env
    assert (await client.get("/api/export/nope.md")).status_code == 404
    session = await store.create_session(now=STARTED)
    interrupted = await client.get(f"/api/export/{session.id}.md")
    assert "- 状态：已中断" in interrupted.text
    for suffix in ("json", "zip"):
        assert (await client.get(f"/api/export/nope.{suffix}")).status_code == 404
    assert (await client.get(f"/api/export/{session.id}.pdf")).status_code == 404
    assert (await client.get(f"/api/export/{session.id}")).status_code == 404
    assert (await client.get("/api/export/.md")).status_code == 404

    manager = app.state.resources.sessions
    live = await manager.begin()
    r = await client.get(f"/api/export/{live.session.id}.md")
    assert r.status_code == 200 and "- 状态：进行中" in r.text
    await manager.finish(live)


async def test_export_includes_every_utterance_of_a_long_meeting(env):
    client, store, _ = env
    session = await store.create_session(now=STARTED)
    for i in range(650):  # 超过列表接口一页的上限
        await store.add_utterance(
            Utterance(session.id, 1 + i % 2, float(i * 3), i * 3 + 2.0, f"第{i}句")
        )
    text = (await client.get(f"/api/export/{session.id}.md")).text
    assert "第0句" in text and "第649句" in text
    assert text.count("**说话人") == 650


# --------------------------------------------------------------------------- #
# 结构化 JSON 与压缩包
# --------------------------------------------------------------------------- #


async def full_meeting(store, data_dir):
    """一场什么都有的会议：两次连接、发言、截图（带文件）、纪要、任务（带产物和进度）、报告。"""
    session = await store.create_session("周三组会", now=STARTED)
    first = await store.open_connection(session.id, connected_at=STARTED, t_from=0.0)
    await store.close_connection(first, disconnected_at=STARTED + 60, t_to=60.0)
    second = await store.open_connection(session.id, connected_at=STARTED + 780, t_from=780.0)
    await store.close_connection(second, disconnected_at=STARTED + 900, t_to=900.0)
    await store.add_utterance(Utterance(session.id, 1, 5.0, 7.0, "先看基线"))
    await store.add_utterance(
        Utterance(session.id, SPEAKER_TYPED, 800.0, 800.0, "查一下引用数", source="text")
    )
    await store.rename_speaker(session.id, 1, "王老师")
    frame = await store.add_frame(session.id, t=6.0, width=16, height=9, suffix=".webp")
    await store.set_frame_caption(frame.id, status="done", caption="幻灯片：议程")
    frame_path = data_dir / frame.path
    frame_path.parent.mkdir(parents=True, exist_ok=True)
    frame_path.write_bytes(b"WEBP-BYTES" * 1000)
    await store.add_digest(session.id, t_from=0, t_to=7.0, text="一、议程", last_utterance_id=1)
    task = await store.create_task(
        session.id, goal="核实引用数", requested_by=1, requested_t=800.0, modality="text"
    )
    await store.add_task_event(task.id, "status", "开始处理", now=STARTED + 801)
    await store.update_task(
        task.id,
        status="succeeded",
        brief="1243 次",
        detail_md="一、结论\n1243 次",
        sources=["https://example.org/a"],
        artifacts=["plot.png", "out/data.csv", "../../../meetings.db", "/etc/passwd", "gone.txt"],
    )
    task_root = data_dir / "sessions" / session.id / "tasks" / "t1"
    (task_root / "out").mkdir(parents=True)
    (task_root / "plot.png").write_bytes(b"PNG")
    (task_root / "out" / "data.csv").write_bytes(b"a,b\n1,2\n")
    (data_dir / "meetings-secret.txt").write_text("不该被带出去", encoding="utf-8")
    report = await store.create_report(session.id, provider="realtime_llm", now=STARTED + 1000)
    await store.finish_report(report, "# 周三组会 · 会后报告\n\n## 概要\n\n讨论了基线。\n")
    await store.create_report(
        session.id, provider="realtime_llm", now=STARTED + 1100
    )  # 还在生成的不算
    await store.end_session(session.id, now=STARTED + 900)
    return session, frame, task


@pytest.fixture
async def full(make_cfg, tmp_path):
    cfg = make_cfg()
    data_dir = cfg.resolve(cfg.session.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    store = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    app = create_app(cfg, store=store, static_dir=tmp_path / "nope")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            session, frame, task = await full_meeting(store, data_dir)
            yield client, store, session, frame, task
    await store.close()


async def test_json_export_snapshot(full):
    client, _store, session, frame, task = full
    r = await client.get(f"/api/export/{session.id}.json")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/json; charset=utf-8"
    disposition = r.headers["content-disposition"]
    assert f'filename="meeting-{session.id[:8]}.json"' in disposition
    assert unquote(disposition.split("filename*=UTF-8''", 1)[1]) == "周三组会.json"
    assert "周三组会" in r.text  # 中文不转义，文件打开就能读
    body = r.json()
    assert set(body) == {
        "format_version", "session", "connections", "speakers", "utterances",
        "frames", "digests", "tasks", "report",
    }  # fmt: skip
    assert body["format_version"] == 1
    assert body["session"] == {
        "id": session.id,
        "title": "周三组会",
        "started_at": STARTED,
        "ended_at": STARTED + 900,
        "last_active_at": body["session"]["last_active_at"],
        "state": "ended",
        "duration_secs": 180.0,
    }
    assert body["connections"] == [
        {"connected_at": STARTED, "disconnected_at": STARTED + 60, "t_from": 0.0, "t_to": 60.0},
        {
            "connected_at": STARTED + 780,
            "disconnected_at": STARTED + 900,
            "t_from": 780.0,
            "t_to": 900.0,
        },
    ]
    assert body["speakers"] == [
        {"idx": -2, "display_name": "文字输入"},
        {"idx": 1, "display_name": "王老师"},
    ]
    assert body["utterances"] == [
        {
            "id": 1,
            "speaker_idx": 1,
            "speaker_name": "王老师",
            "t_start": 5.0,
            "t_end": 7.0,
            "text": "先看基线",
            "source": "asr",
        },
        {
            "id": 2,
            "speaker_idx": -2,
            "speaker_name": "文字输入",
            "t_start": 800.0,
            "t_end": 800.0,
            "text": "查一下引用数",
            "source": "text",
        },
    ]
    assert body["frames"] == [
        {
            "id": frame.id,
            "t": 6.0,
            "file": f"frames/{frame.id:06d}.webp",
            "width": 16,
            "height": 9,
            "caption": "幻灯片：议程",
            "caption_status": "done",
        }
    ]
    (digest,) = body["digests"]
    assert (digest["t_from"], digest["t_to"], digest["text"]) == (0, 7.0, "一、议程")
    (exported,) = body["tasks"]
    assert exported["id"] == task.id and exported["label"] == "t1"
    assert (exported["status"], exported["brief"], exported["modality"]) == (
        "succeeded",
        "1243 次",
        "text",
    )
    assert exported["detail_md"] == "一、结论\n1243 次"
    assert exported["sources"] == ["https://example.org/a"]
    # 文件名不规矩的产物不出现在导出里
    assert exported["artifacts"] == [
        "tasks/t1/plot.png",
        "tasks/t1/out/data.csv",
        "tasks/t1/gone.txt",
    ]
    assert exported["events"] == [{"at": STARTED + 801, "kind": "status", "summary": "开始处理"}]
    assert (exported["requested_by"], exported["requested_t"]) == (1, 800.0)
    assert body["report"]["provider"] == "realtime_llm"
    assert body["report"]["text_md"].startswith("# 周三组会 · 会后报告")  # 最近一份生成好的


async def test_zip_export_members_and_contents(full):
    client, _store, session, frame, _task = full
    r = await client.get(f"/api/export/{session.id}.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert (
        unquote(r.headers["content-disposition"].split("filename*=UTF-8''", 1)[1]) == "周三组会.zip"
    )
    archive = zipfile.ZipFile(io.BytesIO(r.content))
    assert archive.testzip() is None
    assert archive.namelist() == [
        "transcript.md",
        "session.json",
        "report.md",
        f"frames/{frame.id:06d}.webp",
        "tasks/t1/plot.png",
        "tasks/t1/out/data.csv",
    ]  # 带 .. 的、绝对路径的、磁盘上已经没有的产物都不在里面
    transcript = archive.read("transcript.md").decode("utf-8")
    assert transcript == (await client.get(f"/api/export/{session.id}.md")).text
    assert f"](frames/{frame.id:06d}.webp)" in transcript  # 解压后 Markdown 里的图能直接显示
    assert "`tasks/t1/plot.png`" in transcript and "meetings.db" not in transcript
    assert json.loads(archive.read("session.json"))["session"]["id"] == session.id
    assert archive.read("report.md").decode("utf-8").startswith("# 周三组会 · 会后报告")
    assert archive.read(f"frames/{frame.id:06d}.webp") == b"WEBP-BYTES" * 1000
    assert archive.read("tasks/t1/out/data.csv") == b"a,b\n1,2\n"
    assert not any(
        "secret" in name or "passwd" in name or ".." in name for name in archive.namelist()
    )


async def test_zip_export_of_a_bare_meeting_and_missing_ones(env):
    client, store, _ = env
    session = await store.create_session(now=STARTED)
    r = await client.get(f"/api/export/{session.id}.zip")
    archive = zipfile.ZipFile(io.BytesIO(r.content))
    assert archive.namelist() == ["transcript.md", "session.json"]  # 没有报告就没有 report.md
    assert json.loads(archive.read("session.json"))["report"] is None
    assert (await client.get("/api/export/nope.zip")).status_code == 404


def test_archive_members_never_leave_the_session_directory(tmp_path):
    from agentic_meeting.types import TaskRecord
    from agentic_meeting.web.export import ExportData, archive_members, safe_artifacts, zip_stream

    data_dir = tmp_path / "data"
    session_dir = data_dir / "sessions" / "s1"
    (session_dir / "frames").mkdir(parents=True)
    (session_dir / "tasks" / "t1").mkdir(parents=True)
    (session_dir / "frames" / "000001.webp").write_bytes(b"ok")
    (session_dir / "tasks" / "t1" / "plot.png").write_bytes(b"ok")
    other = data_dir / "sessions" / "s2" / "frames"
    other.mkdir(parents=True)
    (other / "000009.webp").write_bytes(b"another meeting")
    (data_dir / "meetings.db").write_bytes(b"db")

    task = TaskRecord(
        id="s1.t1",
        session_id="s1",
        goal="g",
        artifacts=[
            "plot.png",
            "../../s2/frames/000009.webp",
            "C:/Windows/win.ini",
            "",
            "a/../../x",
        ],
    )
    assert safe_artifacts(task) == ["plot.png"]
    frames = [
        ScreenFrame("s1", 1.0, "sessions/s1/frames/000001.webp", 16, 9, id=1),
        ScreenFrame(
            "s1", 2.0, "sessions/s2/frames/000009.webp", 16, 9, id=9
        ),  # 数据库里的路径指到了别处
        ScreenFrame("s1", 3.0, "../meetings.db", 16, 9, id=3),
        ScreenFrame("s1", 4.0, "sessions/s1/frames/000404.webp", 16, 9, id=4),  # 文件已经没了
    ]
    data = ExportData(
        summary=summary(), state="ended", speakers=[], utterances=[], frames=frames, digests=[],
        tasks=[task],
    )  # fmt: skip
    members = archive_members(data, data_dir)
    assert [name for name, _ in members] == ["frames/000001.webp", "tasks/t1/plot.png"]

    # 取完清单之后文件被删了：跳过它，其余照常
    gone = session_dir / "frames" / "gone.webp"
    payload = b"".join(zip_stream([("a.txt", "文字")], [("frames/gone.webp", gone), *members]))
    archive = zipfile.ZipFile(io.BytesIO(payload))
    assert archive.namelist() == ["a.txt", "frames/000001.webp", "tasks/t1/plot.png"]
    assert archive.read("a.txt").decode("utf-8") == "文字"


def test_zip_stream_yields_as_it_goes(tmp_path):
    from agentic_meeting.web.export import zip_stream

    big = tmp_path / "big.bin"
    big.write_bytes(bytes(range(256)) * 4096)  # 1 MB
    chunks = list(zip_stream([("a.txt", "x")], [("big.bin", big)], chunk_size=1 << 16))
    assert len(chunks) > 8  # 边读边出，不是攒成一整块
    assert max(len(c) for c in chunks) < 200_000
    archive = zipfile.ZipFile(io.BytesIO(b"".join(chunks)))
    assert archive.read("big.bin") == big.read_bytes()
