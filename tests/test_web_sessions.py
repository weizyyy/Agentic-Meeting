"""会话与转录相关的 HTTP 接口（interfaces.md §5.1、§5.4）。

用 ``httpx.ASGITransport`` 直接调应用 + 真实的 SQLite 临时库；活动连接用假的 worker 模拟。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame

from agentic_meeting.store.db import Store
from agentic_meeting.types import SPEAKER_ASSISTANT, SPEAKER_TYPED, Utterance
from agentic_meeting.web.app import create_app


class FakeWorker:
    def __init__(self):
        self.frames = []
        self.cancelled = asyncio.Event()

    async def queue_frame(self, frame):
        self.frames.append(frame)

    async def cancel(self):
        self.cancelled.set()

    def messages(self, kind):
        return [
            f.data
            for f in self.frames
            if isinstance(f, RTVIServerMessageFrame) and f.data.get("type") == kind
        ]


class FakeRecorder:
    def __init__(self):
        self.names = {}
        self.elapsed_secs = 3.0

    def set_speaker_name(self, idx, name):
        self.names[idx] = name


@pytest.fixture
def cfg(make_cfg):
    return make_cfg()


@pytest.fixture
async def env(cfg, tmp_path):
    store = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    app = create_app(cfg, store=store, static_dir=tmp_path / "nope")
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            manager = app.state.resources.sessions
            yield SimpleEnv(app, client, store, manager)
        if manager.live is not None:  # 测试里没收尾的活动连接：收掉，免得应用关闭时干等
            await manager.finish(manager.live)
    await store.close()


class SimpleEnv:
    def __init__(self, app, client, store, manager):
        self.app, self.client, self.store, self.manager = app, client, store, manager

    async def go_live(self):
        """模拟一路活动连接：返回 (live, worker, recorder)。"""
        live = await self.manager.begin()
        worker, recorder = FakeWorker(), FakeRecorder()
        await self.manager.register(live, worker, recorder)
        return live, worker, recorder

    async def finish(self, live):
        await self.manager.finish(live)

    async def meeting(self, title, lines, *, now=1000.0, ended=False):
        s = await self.store.create_session(title, now=now)
        for i, (speaker, text) in enumerate(lines):
            await self.store.add_utterance(
                Utterance(s.id, speaker, float(i * 3), float(i * 3 + 2), text), now=now + i
            )
        if ended:
            await self.store.end_session(s.id, now=now + 100)
        return s


# --------------------------------------------------------------------------- #
# 会议列表与详情
# --------------------------------------------------------------------------- #


async def test_session_list_shows_state_summary_and_orders_by_activity(env):
    ended = await env.meeting("上周组会", [(1, "旧的话")], now=1000.0, ended=True)
    live, _, _ = await env.go_live()
    interrupted = await env.meeting("断线的会", [(1, "第一句"), (2, "第二句")], now=2000.0)

    response = await env.client.get("/api/sessions")
    assert response.status_code == 200
    items = response.json()["items"]
    assert [i["id"] for i in items] == [live.session.id, interrupted.id, ended.id]
    by_id = {i["id"]: i for i in items}
    assert by_id[live.session.id]["state"] == "live"
    assert by_id[interrupted.id]["state"] == "interrupted"
    assert by_id[ended.id]["state"] == "ended"

    row = by_id[interrupted.id]
    assert row["title"] == "断线的会" and row["utterance_count"] == 2
    assert row["speakers"] == ["说话人 1", "说话人 2"]
    assert row["preview"] == [
        {"speaker": "说话人 1", "text": "第一句"},
        {"speaker": "说话人 2", "text": "第二句"},
    ]
    assert set(row) == {
        "id", "title", "started_at", "ended_at", "last_active_at", "state",
        "duration_secs", "utterance_count", "speakers", "preview", "keep", "deletion_pending",
    }  # fmt: skip
    assert by_id[ended.id]["ended_at"] == 1100.0 and row["ended_at"] is None


async def test_session_list_paging(env):
    for i in range(5):
        await env.meeting(f"会{i}", [], now=100.0 * (i + 1))
    first = (await env.client.get("/api/sessions", params={"limit": 2})).json()["items"]
    assert [i["title"] for i in first] == ["会4", "会3"]
    second = (
        await env.client.get(
            "/api/sessions", params={"limit": 2, "before": first[-1]["last_active_at"]}
        )
    ).json()["items"]
    assert [i["title"] for i in second] == ["会2", "会1"]
    assert (await env.client.get("/api/sessions", params={"limit": 0})).status_code == 422


async def test_live_session_duration_counts_up_to_now(env, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(env.manager, "_now", lambda: clock[0])
    monkeypatch.setattr("agentic_meeting.web.sessions_api.time.time", lambda: clock[0])
    live, _, _ = await env.go_live()
    clock[0] += 5.0
    (row,) = (await env.client.get("/api/sessions")).json()["items"]
    assert row["state"] == "live" and row["duration_secs"] == 5.0


async def test_session_detail_has_screen_settings_and_connections(env, cfg):
    cfg.session.members = ["王老师", "李同学"]
    s = await env.meeting("详情", [(1, "你好")], now=1000.0)
    c = await env.store.open_connection(s.id, connected_at=1000.0, t_from=0.0)
    await env.store.close_connection(c, disconnected_at=1010.0, t_to=10.0)
    body = (await env.client.get(f"/api/sessions/{s.id}")).json()
    assert body["id"] == s.id and body["state"] == "interrupted"
    assert body["screen"] == cfg.screen.model_dump()
    assert body["members"] == ["王老师", "李同学"]  # 配置里的成员名单，给说话人改名当候选
    assert body["connections"] == [
        {"connected_at": 1000.0, "disconnected_at": 1010.0, "t_from": 0.0, "t_to": 10.0}
    ]


async def test_unknown_session_is_a_404_with_a_chinese_message(env):
    response = await env.client.get("/api/sessions/不存在")
    assert response.status_code == 404
    assert response.json() == {"error": "找不到这场会议"}


# --------------------------------------------------------------------------- #
# 当前会话
# --------------------------------------------------------------------------- #


async def test_current_session_is_the_live_one_then_the_latest_unended_then_404(env):
    assert (await env.client.get("/api/session")).status_code == 404
    assert (await env.client.get("/api/session")).json() == {"error": "现在没有会议"}

    old = await env.meeting("中断的", [(1, "话")], now=1000.0)
    body = (await env.client.get("/api/session")).json()
    assert (body["id"], body["state"]) == (old.id, "interrupted")

    live, _, _ = await env.go_live()
    body = (await env.client.get("/api/session")).json()
    assert (body["id"], body["state"]) == (live.session.id, "live")

    await env.finish(live)
    await env.manager.end(old.id)
    body = (await env.client.get("/api/session")).json()
    assert body["id"] == live.session.id and body["state"] == "interrupted"  # 断开后是已中断


# --------------------------------------------------------------------------- #
# 重命名、结束、删除
# --------------------------------------------------------------------------- #


async def test_rename_a_session(env):
    s = await env.meeting("旧标题", [])
    response = await env.client.patch(f"/api/sessions/{s.id}", json={"title": "  新标题  "})
    assert response.status_code == 200 and response.json()["title"] == "新标题"
    assert (await env.store.get_session(s.id)).title == "新标题"


@pytest.mark.parametrize(
    ("body", "status"),
    [({"title": 5}, 400), ({}, 400), ({"title": "长" * 201}, 400), ([], 400)],
)
async def test_rename_rejects_bad_titles(env, body, status):
    s = await env.meeting("x", [])
    response = await env.client.patch(f"/api/sessions/{s.id}", json=body)
    assert response.status_code == status and "error" in response.json()


async def test_rename_unknown_session_is_404(env):
    assert (await env.client.patch("/api/sessions/nope", json={"title": "x"})).status_code == 404


async def test_end_an_interrupted_session(env):
    s = await env.meeting("会", [])
    response = await env.client.post(f"/api/sessions/{s.id}/end")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == s.id and body["ended_at"] is not None
    assert (await env.store.get_session(s.id)).ended_at == body["ended_at"]


async def test_end_the_live_session_stops_the_connection(env):
    live, worker, _ = await env.go_live()
    ender = asyncio.create_task(env.client.post(f"/api/sessions/{live.session.id}/end"))
    await asyncio.wait_for(worker.cancelled.wait(), 3)
    await env.finish(live)  # 被取消的管线收尾
    response = await ender
    assert response.status_code == 200 and response.json()["ended_at"] is not None
    assert worker.messages("session_closed") == [{"type": "session_closed", "reason": "ended"}]


async def test_end_current_session_shortcut(env):
    assert (await env.client.post("/api/session/end")).status_code == 404
    s = await env.meeting("会", [])
    body = (await env.client.post("/api/session/end")).json()
    assert body["id"] == s.id and body["ended_at"] is not None


async def test_end_unknown_session_is_404(env):
    assert (await env.client.post("/api/sessions/nope/end")).status_code == 404


async def test_delete_removes_the_session_its_rows_and_its_files(env, cfg):
    s = await env.meeting("要删的", [(1, "一句话")], ended=True)
    files = cfg.resolve(cfg.session.data_dir) / "sessions" / s.id / "frames"
    files.mkdir(parents=True)
    (files / "000001.webp").write_bytes(b"x")
    keep = await env.meeting("留下的", [(1, "别的")], ended=True)

    response = await env.client.delete(f"/api/sessions/{s.id}")
    assert response.status_code == 200 and response.json() == {"id": s.id}
    assert await env.store.get_session(s.id) is None
    assert not files.parent.exists()  # 截图和任务目录一起清掉
    assert (await env.store.get_session(keep.id)) is not None
    assert (await env.client.delete(f"/api/sessions/{s.id}")).status_code == 404


async def test_a_live_session_cannot_be_deleted(env):
    live, _, _ = await env.go_live()
    response = await env.client.delete(f"/api/sessions/{live.session.id}")
    assert response.status_code == 409 and "处理" in response.json()["error"]
    assert await env.store.get_session(live.session.id) is not None


async def test_delete_does_not_touch_files_outside_the_session_directory(env, cfg):
    s = await env.meeting("会", [], ended=True)
    outside = cfg.resolve(cfg.session.data_dir) / "sessions" / "别的目录"
    outside.mkdir(parents=True)
    (outside / "keep.txt").write_text("x")
    await env.client.delete(f"/api/sessions/{s.id}")
    assert (outside / "keep.txt").exists()


# --------------------------------------------------------------------------- #
# 发言
# --------------------------------------------------------------------------- #


async def _fill(env, n=10):
    s = await env.meeting("长会", [(1 + i % 2, f"第{i}句") for i in range(n)])
    rows = (
        await env.client.get("/api/utterances", params={"session_id": s.id, "limit": 500})
    ).json()
    return s, [r["id"] for r in rows["items"]]


async def test_utterances_tail_after_and_before(env):
    s, ids = await _fill(env, 10)
    texts = lambda r: [i["text"] for i in r.json()["items"]]  # noqa: E731

    r = await env.client.get("/api/utterances", params={"session_id": s.id, "tail": 3})
    assert texts(r) == ["第7句", "第8句", "第9句"]
    r = await env.client.get("/api/utterances", params={"session_id": s.id, "after_id": ids[6]})
    assert texts(r) == ["第7句", "第8句", "第9句"]
    r = await env.client.get(
        "/api/utterances", params={"session_id": s.id, "before_id": ids[5], "limit": 2}
    )
    assert texts(r) == ["第3句", "第4句"]
    r = await env.client.get("/api/utterances", params={"session_id": s.id, "limit": 2})
    assert texts(r) == ["第0句", "第1句"]


async def test_utterance_items_carry_names_times_and_source(env):
    s = await env.meeting("会", [(1, "你好")])
    await env.store.add_utterance(
        Utterance(s.id, SPEAKER_ASSISTANT, 5.0, 6.0, "回答", source="assistant")
    )
    await env.store.add_utterance(Utterance(s.id, SPEAKER_TYPED, 7.0, 7.0, "打字", source="text"))
    await env.store.rename_speaker(s.id, 1, "王老师")
    items = (await env.client.get("/api/utterances", params={"session_id": s.id})).json()["items"]
    assert [(i["speaker_idx"], i["speaker_name"], i["source"], i["text"]) for i in items] == [
        (1, "王老师", "asr", "你好"),
        (-1, "Nova", "assistant", "回答"),
        (-2, "文字输入", "text", "打字"),
    ]
    assert set(items[0]) == {
        "id",
        "speaker_idx",
        "speaker_name",
        "t_start",
        "t_end",
        "text",
        "source",
    }
    assert (items[0]["t_start"], items[0]["t_end"]) == (0.0, 2.0)


async def test_utterances_default_to_the_current_session(env):
    assert (await env.client.get("/api/utterances")).status_code == 404
    s = await env.meeting("会", [(1, "你好")])
    items = (await env.client.get("/api/utterances", params={"tail": 5})).json()["items"]
    assert [i["text"] for i in items] == ["你好"]
    assert (
        await env.client.get("/api/utterances", params={"session_id": "nope"})
    ).status_code == 404
    assert s.id  # 用到了


async def test_utterance_query_modes_are_exclusive_and_bounded(env):
    s, ids = await _fill(env, 3)
    r = await env.client.get(
        "/api/utterances", params={"session_id": s.id, "tail": 2, "after_id": ids[0]}
    )
    assert r.status_code == 400 and "只能给一个" in r.json()["error"]
    for params in ({"tail": 0}, {"tail": 501}, {"limit": 0}, {"limit": 501}):
        r = await env.client.get("/api/utterances", params={"session_id": s.id, **params})
        assert r.status_code == 422


# --------------------------------------------------------------------------- #
# 说话人
# --------------------------------------------------------------------------- #


async def test_speakers_list_and_rename(env):
    s = await env.meeting("会", [(1, "甲"), (2, "乙")])
    body = (await env.client.get("/api/speakers", params={"session_id": s.id})).json()
    assert body == {
        "items": [{"idx": 1, "display_name": "说话人 1"}, {"idx": 2, "display_name": "说话人 2"}]
    }

    response = await env.client.put(
        "/api/speakers/1", json={"session_id": s.id, "display_name": "  王老师 "}
    )
    assert response.status_code == 200 and response.json() == {"idx": 1, "display_name": "王老师"}
    assert await env.store.speaker_name(s.id, 1) == "王老师"


async def test_renaming_the_live_sessions_speaker_updates_the_recorder_and_the_browser(env):
    live, worker, recorder = await env.go_live()
    await env.store.add_utterance(Utterance(live.session.id, 2, 0.0, 1.0, "话"))
    response = await env.client.put(
        "/api/speakers/2", json={"display_name": "李同学"}
    )  # 不带 session_id = 当前会话
    assert response.status_code == 200
    assert recorder.names == {2: "李同学"}
    assert worker.messages("speaker") == [{"type": "speaker", "idx": 2, "display_name": "李同学"}]


async def test_renaming_an_interrupted_sessions_speaker_pushes_nothing(env):
    s = await env.meeting("会", [(1, "话")])
    response = await env.client.put(
        "/api/speakers/1", json={"session_id": s.id, "display_name": "x"}
    )
    assert response.status_code == 200  # 没有活动连接：只写库


async def test_rename_errors(env):
    s = await env.meeting("会", [(1, "话")])
    r = await env.client.put("/api/speakers/9", json={"session_id": s.id, "display_name": "x"})
    assert r.status_code == 404 and r.json() == {"error": "这场会议里没有这个说话人"}
    for body in (
        {"session_id": s.id, "display_name": "   "},
        {"session_id": s.id},
        {"session_id": s.id, "display_name": 3},
    ):
        r = await env.client.put("/api/speakers/1", json=body)
        assert r.status_code == 400
    r = await env.client.put("/api/speakers/1", json={"session_id": "nope", "display_name": "x"})
    assert r.status_code == 404 and r.json() == {"error": "找不到这场会议"}
    r = await env.client.put(
        "/api/speakers/1", json={"session_id": s.id, "display_name": "长" * 51}
    )
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# 信令里的 session_id
# --------------------------------------------------------------------------- #


class OfferHandler:
    def __init__(self):
        self.requests = []

    async def handle_web_request(self, request, webrtc_connection_callback):
        self.requests.append(request)
        await webrtc_connection_callback(object())
        return {"sdp": "a", "type": "answer", "pc_id": "1"}

    async def handle_patch_request(self, request): ...

    async def close(self): ...


async def test_the_session_to_continue_is_checked_before_negotiating(cfg, tmp_path):
    handler, calls = OfferHandler(), []

    async def bot(connection, request_data, resources):
        calls.append(request_data)

    store = await Store.open(tmp_path / "m.db", 4)
    known = await store.create_session("上午的会")
    app = create_app(cfg, handler=handler, bot=bot, store=store, static_dir=Path(tmp_path) / "nope")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        base = {"sdp": "offer", "type": "offer", "pc_id": None, "restart_pc": False}
        missing = await client.post(
            "/api/offer", json={**base, "requestData": {"session_id": "abc"}}
        )
        bad = [
            await client.post("/api/offer", json={**base, "requestData": {"session_id": value}})
            for value in (3, "", "  ")
        ]
        assert handler.requests == []  # 被拒绝的请求没有交给 WebRTC 处理器，也没有建管线
        resumed = await client.post(
            "/api/offer", json={**base, "requestData": {"session_id": known.id}}
        )
        fresh = await client.post("/api/offer", json={**base, "requestData": {"other": 1}})
        explicit_new = await client.post(
            "/api/offer", json={**base, "requestData": {"session_id": None}}
        )
    await store.close()
    assert missing.status_code == 404 and missing.json() == {"error": "找不到这场会议"}
    assert [r.status_code for r in bad] == [400, 400, 400]
    assert resumed.status_code == fresh.status_code == explicit_new.status_code == 200
    assert calls == [{"session_id": known.id}, {"other": 1}, {"session_id": None}]


# --------------------------------------------------------------------------- #
# 合并说话人
# --------------------------------------------------------------------------- #


async def test_merge_speakers_endpoint(env):
    live, worker, recorder = await env.go_live()
    sid = live.session.id
    await env.store.add_utterance(Utterance(sid, 1, 0.0, 1.0, "甲"))
    await env.store.add_utterance(Utterance(sid, 2, 2.0, 3.0, "还是甲"))
    await env.client.put("/api/speakers/1", json={"display_name": "王老师"})

    r = await env.client.post("/api/speakers/2/merge", json={"into": 1})
    assert r.status_code == 200
    assert r.json() == {"from": 2, "into": 1, "display_name": "王老师", "moved": 1}
    speakers = (await env.client.get("/api/speakers")).json()["items"]
    assert [s["idx"] for s in speakers] == [1]
    items = (await env.client.get("/api/utterances")).json()["items"]
    assert [(i["speaker_idx"], i["speaker_name"]) for i in items] == [(1, "王老师")] * 2
    assert worker.messages("speakers_merged") == [
        {"type": "speakers_merged", "from": 2, "into": 1, "display_name": "王老师"}
    ]
    await env.finish(live)


async def test_merge_speakers_endpoint_errors(env):
    s = await env.meeting("回看的会", [(1, "甲"), (2, "乙")])
    for idx, body in (
        (2, {"session_id": s.id}),
        (2, {"session_id": s.id, "into": "1"}),
        (2, {"session_id": s.id, "into": True}),
        (2, {"session_id": s.id, "into": 2}),
        (2, {"session_id": s.id, "into": 0}),
        (0, {"session_id": s.id, "into": 1}),
        (-1, {"session_id": s.id, "into": 1}),
        (2, {"session_id": 5, "into": 1}),
    ):
        r = await env.client.post(f"/api/speakers/{idx}/merge", json=body)
        assert r.status_code == 400, (idx, body)
    r = await env.client.post("/api/speakers/2/merge", json={"session_id": "nope", "into": 1})
    assert r.status_code == 404 and r.json() == {"error": "找不到这场会议"}
    r = await env.client.post("/api/speakers/7/merge", json={"session_id": s.id, "into": 1})
    assert r.status_code == 404 and r.json() == {"error": "这场会议里没有这个说话人"}
    # 不在进行的会议也能合并（回看时收拾）
    r = await env.client.post("/api/speakers/2/merge", json={"session_id": s.id, "into": 1})
    assert r.status_code == 200 and r.json()["moved"] == 1


async def test_closing_the_app_tells_the_live_page_the_server_is_stopping(cfg, tmp_path):
    store = await Store.open(tmp_path / "stop.db", 4)
    app = create_app(cfg, store=store, static_dir=tmp_path / "nope")
    async with app.router.lifespan_context(app):
        manager = app.state.resources.sessions
        live = await manager.begin()
        worker = FakeWorker()
        await manager.register(live, worker, FakeRecorder())

        async def run_until_cancelled():
            await worker.cancelled.wait()
            await manager.finish(live)

        task = asyncio.create_task(run_until_cancelled())
    await asyncio.wait_for(task, 2)
    assert worker.messages("session_closed") == [
        {"type": "session_closed", "reason": "server_stopping"}
    ]
    await store.close()


async def test_startup_closes_connection_records_left_open_by_a_crash(cfg, tmp_path):
    store = await Store.open(tmp_path / "crash.db", 4)
    s = await store.create_session(now=1000.0)
    await store.open_connection(s.id, connected_at=1000.0, t_from=0.0)  # 上次崩溃，没来得及关
    app = create_app(cfg, store=store, static_dir=tmp_path / "nope")
    async with app.router.lifespan_context(app):
        (conn,) = await store.list_connections(s.id)
        assert conn.disconnected_at is not None
    await store.close()


async def test_the_default_store_lives_in_the_data_dir_and_is_closed_with_the_app(cfg, tmp_path):
    app = create_app(cfg, static_dir=tmp_path / "nope")
    async with app.router.lifespan_context(app):
        store = app.state.resources.store
        assert (cfg.resolve(cfg.session.data_dir) / "meetings.db").is_file()
        await store.create_session("x")
    with pytest.raises(ValueError, match="no active connection"):
        await store.create_session("after close")  # 应用关闭时库一起关了


# --------------------------------------------------------------------------- #
# 选中字幕改发言人
# --------------------------------------------------------------------------- #


async def test_reassigning_selected_utterances_to_an_existing_speaker(env):
    live, worker, recorder = await env.go_live()
    sid = live.session.id
    ids = [
        await env.store.add_utterance(Utterance(sid, 1, float(i), i + 1.0, f"第{i}句"))
        for i in range(3)
    ]
    await env.store.add_utterance(Utterance(sid, 2, 9.0, 10.0, "乙"))
    await env.client.put("/api/speakers/2", json={"display_name": "小李"})

    r = await env.client.post("/api/utterances/speaker", json={"ids": ids[1:], "speaker_idx": 2})
    assert r.status_code == 200
    assert r.json() == {"speaker": {"idx": 2, "display_name": "小李"}, "ids": ids[1:]}
    items = (await env.client.get("/api/utterances")).json()["items"]
    assert [(i["speaker_idx"], i["speaker_name"]) for i in items] == [
        (1, "说话人 1"),
        (2, "小李"),
        (2, "小李"),
        (2, "小李"),
    ]
    # 会议正在进行：页面逐条收到更正
    assert worker.messages("utterance_update") == [
        {"type": "utterance_update", "id": i, "speaker_idx": 2, "speaker_name": "小李"}
        for i in ids[1:]
    ]
    await env.finish(live)


async def test_reassigning_to_a_new_speaker_and_in_a_past_meeting(env):
    s = await env.meeting("回看的会", [(1, "甲"), (1, "其实是旁听的人说的")])
    items = (await env.client.get("/api/utterances", params={"session_id": s.id})).json()["items"]
    target = items[1]["id"]
    r = await env.client.post(
        "/api/utterances/speaker",
        json={"session_id": s.id, "ids": [target], "new_speaker": " 张老师 "},
    )
    assert r.status_code == 200
    assert r.json() == {"speaker": {"idx": 1000, "display_name": "张老师"}, "ids": [target]}
    speakers = (await env.client.get("/api/speakers", params={"session_id": s.id})).json()["items"]
    assert {"idx": 1000, "display_name": "张老师"} in speakers
    # 改成「未知」
    r = await env.client.post(
        "/api/utterances/speaker", json={"session_id": s.id, "ids": [target], "speaker_idx": 0}
    )
    assert r.status_code == 200 and r.json()["speaker"] == {"idx": 0, "display_name": "未知"}


async def test_reassign_endpoint_errors(env):
    s = await env.meeting("会", [(1, "甲")])
    base = {"session_id": s.id}
    bad_bodies = [
        {**base, "speaker_idx": 1},  # 没给 ids
        {**base, "ids": [], "speaker_idx": 1},
        {**base, "ids": ["1"], "speaker_idx": 1},
        {**base, "ids": [True], "speaker_idx": 1},
        {**base, "ids": list(range(501)), "speaker_idx": 1},
        {**base, "ids": [1]},  # 没说改成谁
        {**base, "ids": [1], "speaker_idx": 1, "new_speaker": "张"},  # 两个都给了
        {**base, "ids": [1], "speaker_idx": -1},  # 助理不能当发言人
        {**base, "ids": [1], "speaker_idx": "1"},
        {**base, "ids": [1], "new_speaker": "  "},
        {**base, "ids": [1], "new_speaker": "长" * 51},
        {"session_id": 5, "ids": [1], "speaker_idx": 1},
    ]
    for body in bad_bodies:
        r = await env.client.post("/api/utterances/speaker", json=body)
        assert r.status_code == 400, body
    r = await env.client.post(
        "/api/utterances/speaker", json={**base, "ids": [1], "speaker_idx": 7}
    )
    assert r.status_code == 404 and r.json() == {"error": "这场会议里没有这个说话人"}
    r = await env.client.post(
        "/api/utterances/speaker", json={"session_id": "nope", "ids": [1], "speaker_idx": 1}
    )
    assert r.status_code == 404
    # 选中的编号都不在这场会议里：不算错，只是什么都没改
    r = await env.client.post(
        "/api/utterances/speaker", json={**base, "ids": [9999], "speaker_idx": 1}
    )
    assert r.status_code == 200 and r.json()["ids"] == []


async def test_keep_patch_preserves_title_and_supports_atomic_combined_update(env):
    meeting = await env.meeting("保留原标题", [])
    result = await env.client.patch(f"/api/sessions/{meeting.id}", json={"keep": True})
    assert result.status_code == 200
    assert result.json()["title"] == "保留原标题" and result.json()["keep"] is True
    result = await env.client.patch(
        f"/api/sessions/{meeting.id}", json={"keep": False, "title": "新标题"}
    )
    assert result.status_code == 200
    assert result.json()["title"] == "新标题" and result.json()["keep"] is False
    for body in ({}, {"keep": 1}, {"keep": "false"}, {"keep": None}, {"title": None}, {"other": 1}):
        assert (await env.client.patch(f"/api/sessions/{meeting.id}", json=body)).status_code == 400
    live, _, _ = await env.go_live()
    assert (
        await env.client.patch(f"/api/sessions/{live.session.id}", json={"keep": True})
    ).status_code == 200


async def test_delete_failure_is_visible_pending_only_metadata_and_retry_allowed(env, monkeypatch):
    meeting = await env.meeting("虚构失败会议", [(1, "虚构文本")])
    frame = await env.store.add_frame(meeting.id, t=1, width=1, height=1, suffix=".webp")
    task = await env.store.create_task(meeting.id, goal="虚构任务")
    await env.store.update_task(task.id, status="succeeded", finished_at=2)
    cleaner = env.app.state.resources.retention
    original = cleaner.files.remove_session

    def broken(_sid):
        raise OSError("PRIVATE_SENTINEL")

    monkeypatch.setattr(cleaner.files, "remove_session", broken)
    response = await env.client.delete(f"/api/sessions/{meeting.id}")
    assert response.status_code == 500 and "PRIVATE_SENTINEL" not in response.text
    detail = await env.client.get(f"/api/sessions/{meeting.id}")
    assert detail.status_code == 200 and detail.json()["deletion_pending"] is True
    assert (await env.client.get("/api/sessions")).json()["items"][0]["deletion_pending"] is True
    assert (await env.client.get("/api/session")).status_code == 404
    for path in (
        f"/api/utterances?session_id={meeting.id}",
        f"/api/speakers?session_id={meeting.id}",
        f"/api/frames?session_id={meeting.id}",
        f"/api/frames/{frame.id}/image",
        f"/api/tasks?session_id={meeting.id}",
        f"/api/tasks/{task.id}",
        f"/api/tasks/{task.id}/artifacts/result.txt",
        f"/api/sessions/{meeting.id}/report",
        f"/api/sessions/{meeting.id}/report.md",
        f"/api/export/{meeting.id}.json",
    ):
        assert (await env.client.get(path)).status_code == 409, path
    assert (
        await env.client.patch(f"/api/sessions/{meeting.id}", json={"keep": True})
    ).status_code == 409
    assert (await env.client.post(f"/api/sessions/{meeting.id}/end")).status_code == 409
    assert (await env.client.post(f"/api/sessions/{meeting.id}/report")).status_code == 409
    assert (
        await env.client.post(
            "/api/offer",
            json={"sdp": "fake", "type": "offer", "requestData": {"session_id": meeting.id}},
        )
    ).status_code == 409
    assert (await env.store.get_session(meeting.id)).deletion_pending
    monkeypatch.setattr(cleaner.files, "remove_session", original)
    assert (await env.client.delete(f"/api/sessions/{meeting.id}")).status_code == 200
    assert (await env.client.delete(f"/api/sessions/{meeting.id}")).status_code == 404
