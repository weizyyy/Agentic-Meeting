"""多主体会议授权：通过真实应用登录、WebRTC、HTTP 访问与撤权。"""

from __future__ import annotations

import asyncio
import io
import json
import socket
import sqlite3
import time

import pytest
from PIL import Image

from agentic_meeting.pipeline.context import estimate_messages

from .conftest import app_config
from .inference import Inference, Reply
from .meeting_client import MeetingClient, WatchingDevice
from .test_cli import run_cli
from .test_session_events import Watcher
from .waiting import until, until_sync

PASSWORD = "e2e-local-password"
USERS = [
    {
        "id": name,
        "display_name": name,
        "password_env": "E2E_LOCAL_PASSWORD",
        "can_host": name in ("alpha", "beta"),
    }
    for name in ("alpha", "beta", "viewer", "outsider")
]


@pytest.fixture
async def protected(start_app):
    return await start_app(
        {"server.meeting_access": True, "server.local_users": USERS},
        env={"E2E_LOCAL_PASSWORD": PASSWORD},
    )


async def login(http, user):
    result = await http.post("/api/auth/login", json={"user_id": user, "password": PASSWORD})
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["principal"]["id"] == f"local:{user}"
    http.headers["X-CSRF-Token"] = body["csrf_token"]
    return body


async def test_authorized_replay_reset_and_dual_stream_revocation(protected, inference):
    async with protected.http() as alpha, protected.http() as beta, protected.http() as viewer:
        for http, name in ((alpha, "alpha"), (beta, "beta"), (viewer, "viewer")):
            await login(http, name)
        first, second = MeetingClient(alpha), MeetingClient(beta)
        streams = []
        try:
            aid = (await first.connect())["id"]
            inference.asr.say("仅甲会的私有材料")
            await first.speak()
            await first.next_message("utterance", text="仅甲会的私有材料")
            await first.close()

            async def interrupted():
                return (await alpha.get(f"/api/sessions/{aid}")).json()["state"] == "interrupted"

            await until(interrupted, "甲录音已断开")
            bid = (await second.connect())["id"]
            inference.asr.say("仅乙会的私有材料")
            await second.speak()
            await second.next_message("utterance", text="仅乙会的私有材料")
            assert (
                await alpha.put(
                    f"/api/sessions/{aid}/members/local:viewer", json={"role": "watcher"}
                )
            ).status_code == 200
            async with Watcher(beta, bid) as b:
                await b.find("snapshot")
                foreign = b.cursor
                async with Watcher(viewer, aid, foreign) as a:
                    await a.find("reset", reason="cursor_unavailable")
                    snapshot = await a.find("snapshot")
                    assert snapshot["session_id"] == aid
                    assert "仅甲会的私有材料" in json.dumps(snapshot, ensure_ascii=False)
                    assert "仅乙会的私有材料" not in json.dumps(a.events, ensure_ascii=False)
                    cursor = a.cursor
                changed = await alpha.patch(f"/api/sessions/{aid}", json={"title": "授权重放"})
                assert changed.status_code == 200
                async with Watcher(viewer, aid, cursor) as replay:
                    await replay.find(type="session_updated", title="授权重放")
                    assert not any(kind == "snapshot" for kind, _ in replay.events)
                for label in ("tab-one", "tab-two"):
                    stream = WatchingDevice(viewer, aid, label)
                    streams.append(stream)
                    assert await stream.open() == 200
                    await stream.next_message("state")
                started = time.monotonic()
                assert (
                    await alpha.delete(f"/api/sessions/{aid}/members/local:viewer")
                ).status_code == 200
                await asyncio.gather(
                    *(
                        stream.wait_for(lambda s=stream: s.ended, "双流撤权", timeout_secs=1)
                        for stream in streams
                    )
                )
                assert time.monotonic() - started < 1
                for path in (
                    f"/api/sessions/{aid}/events?after={cursor}",
                    f"/api/export/{aid}.json",
                ):
                    assert (await viewer.get(path)).status_code == 404
                await login(viewer, "viewer")
                assert (
                    await viewer.get(f"/api/sessions/{aid}/events?after={cursor}")
                ).status_code == 404
                assert (
                    await beta.put(
                        f"/api/sessions/{bid}/members/local:viewer", json={"role": "watcher"}
                    )
                ).status_code == 200
                async with Watcher(viewer, bid, cursor) as new:
                    await new.find("reset", reason="cursor_unavailable")
                    snapshot = await new.find("snapshot")
                    assert "仅甲会的私有材料" not in json.dumps(new.events, ensure_ascii=False)
                    assert "仅乙会的私有材料" in json.dumps(snapshot, ensure_ascii=False)
                assert (
                    await beta.patch(f"/api/sessions/{bid}", json={"title": "乙会未受影响"})
                ).status_code == 200
                await b.find(type="session_updated", title="乙会未受影响")
                assert not second.of_type("session_closed")
        finally:
            for stream in streams:
                await stream.close()
            await first.close()
            await second.close()


@pytest.mark.parametrize("mode", ["logout", "logout-all", "expiry"])
async def test_two_ordered_streams_follow_exact_login_lifetime(start_app, mode):
    app = await start_app(
        {
            "server.meeting_access": True,
            "server.local_users": USERS,
            "server.auth_session_days": (10 if mode == "expiry" else 86400) / 86400,
        },
        env={"E2E_LOCAL_PASSWORD": PASSWORD},
    )
    async with app.http() as host, app.http() as viewer, app.http() as other_login:
        await login(host, "alpha")
        recorder = MeetingClient(host)
        streams = []
        try:
            sid = (await recorder.connect())["id"]
            await recorder.close()
            await host.put(f"/api/sessions/{sid}/members/local:viewer", json={"role": "watcher"})
            await login(viewer, "viewer")
            old_cookie = dict(viewer.cookies)
            expires = float(next(iter(old_cookie.values())).split(".")[0])
            await login(other_login, "viewer")
            for label in ("one", "two"):
                stream = WatchingDevice(viewer, sid, label)
                streams.append(stream)
                assert await stream.open() == 200
                await stream.next_message("state")
            if mode != "expiry":
                started = time.monotonic()
                assert (await viewer.post(f"/api/auth/{mode}")).status_code == 200
            await asyncio.gather(
                *(
                    stream.wait_for(lambda s=stream: s.ended, "登录终止关闭双流", timeout_secs=11)
                    for stream in streams
                )
            )
            if mode == "expiry":
                assert time.time() - expires < 1
            else:
                assert time.monotonic() - started < 1
            async with app.http(cookies=old_cookie) as old:
                assert (await old.get(f"/api/sessions/{sid}/events?after=old:0")).status_code == 401
            if mode == "logout":
                assert (await other_login.get(f"/api/sessions/{sid}")).status_code == 200
            elif mode == "logout-all":
                assert (await other_login.get(f"/api/sessions/{sid}")).status_code == 401
            await login(viewer, "outsider")
            assert (await viewer.get(f"/api/sessions/{sid}/events?after=old:0")).status_code == 404
        finally:
            for stream in streams:
                await stream.close()
            await recorder.close()


async def test_local_login_revocation_and_csrf(protected):
    async with protected.http() as first, protected.http() as second:
        assert (await first.get("/api/sessions")).status_code == 401
        one = await login(first, "alpha")
        two = await login(second, "alpha")
        assert one["csrf_token"] != two["csrf_token"]
        assert (
            await first.post("/api/session/end", headers={"X-CSRF-Token": "forged"})
        ).status_code == 403
        assert (await first.post("/api/auth/logout")).status_code == 200
        assert (await first.get("/api/sessions")).status_code == 401
        assert (await second.get("/api/sessions")).status_code == 200
        await login(first, "alpha")
        assert (await first.post("/api/auth/logout-all")).status_code == 200
        assert (await second.get("/api/sessions")).status_code == 401


async def test_meeting_grants_default_deny_and_direct_urls(protected, inference):
    async with protected.http() as host, protected.http() as viewer, protected.http() as stranger:
        await login(host, "alpha")
        await login(viewer, "viewer")
        await login(stranger, "outsider")
        client = MeetingClient(host)
        try:
            meeting = await client.connect()
            sid = meeting["id"]
            inference.asr.say("这是会议甲的内容")
            await client.speak()
            await client.next_message("utterance")
            assert (await stranger.get("/api/sessions")).json() == {"items": []}
            for path in (
                f"/api/sessions/{sid}",
                f"/api/utterances?session_id={sid}",
                f"/api/speakers?session_id={sid}",
                f"/api/frames?session_id={sid}",
                f"/api/tasks?session_id={sid}",
                f"/api/export/{sid}.json",
                f"/api/export/{sid}.md",
                f"/api/export/{sid}.zip",
                f"/api/sessions/{sid}/events",
            ):
                denied = await stranger.get(path)
                assert denied.status_code == 404, (path, denied.text)
            granted = await host.put(
                f"/api/sessions/{sid}/members/local:viewer", json={"role": "watcher"}
            )
            assert granted.status_code == 200, granted.text
            assert (await viewer.get(f"/api/sessions/{sid}")).status_code == 200
            assert (await viewer.get("/api/session")).json()["id"] == sid
            assert (
                await viewer.patch(f"/api/sessions/{sid}", json={"title": "intrusion"})
            ).status_code == 403
            assert (
                await viewer.post(f"/api/sessions/{sid}/text", json={"text": "问题"})
            ).status_code == 403
            assert (
                await host.delete(f"/api/sessions/{sid}/members/local:alpha")
            ).status_code == 409
            assert (
                await host.delete(f"/api/sessions/{sid}/members/local:viewer")
            ).status_code == 200
            assert (await viewer.get(f"/api/export/{sid}.json")).status_code == 404
        finally:
            await client.close()


async def test_other_host_and_same_principal_other_login_are_read_only(protected, inference):
    async with protected.http() as host, protected.http() as other, protected.http() as same:
        await login(host, "alpha")
        await login(other, "beta")
        await login(same, "alpha")
        client = MeetingClient(host)
        try:
            sid = (await client.connect())["id"]
            assert (
                await host.put(f"/api/sessions/{sid}/members/local:beta", json={"role": "host"})
            ).status_code == 200
            for http, question in ((other, "第二位主持人的问题"), (same, "同主体其他登录的问题")):
                inference.llm.when(
                    question,
                    Reply(text="只读回答"),
                )
                response = await http.post(f"/api/sessions/{sid}/text", json={"text": question})
                assert response.status_code == 200, response.text
                sent = next(
                    r
                    for r in inference.llm.requests
                    if question in json.dumps(r["messages"], ensure_ascii=False)
                )
                assert not sent.get("tools") and sent["stream"] is False
                assert sent.get("max_completion_tokens", sent.get("max_tokens")) <= 512
            inference.llm.when(
                "诱导工具", Reply(tool="delegate_task", arguments={"goal": "不得执行"})
            )
            refused = await other.post(f"/api/sessions/{sid}/text", json={"text": "诱导工具"})
            assert refused.status_code == 502
            assert (await host.get(f"/api/tasks?session_id={sid}")).json()["items"] == []
            assert (
                await other.patch(f"/api/sessions/{sid}", json={"title": "经HTTP管理"})
            ).status_code == 200
            stored = (await host.get(f"/api/utterances?session_id={sid}")).json()["items"]
            assert [r["text"] for r in stored].count("只读回答") == 2
        finally:
            await client.close()


async def test_read_only_budget_refuses_without_inference_or_storage(start_app, inference):
    app = await start_app(
        {
            "server.meeting_access": True,
            "server.local_users": USERS,
            "realtime.context_budget_tokens": 2000,
        },
        env={"E2E_LOCAL_PASSWORD": PASSWORD},
    )
    async with app.http() as host, app.http() as other:
        await login(host, "alpha")
        await login(other, "alpha")
        client = MeetingClient(host)
        try:
            sid = (await client.connect())["id"]
            before = (await host.get(f"/api/utterances?session_id={sid}")).json()
            count = inference.llm.count()
            response = await other.post(f"/api/sessions/{sid}/text", json={"text": "界" * 2000})
            assert response.status_code == 413, response.text
            assert response.json()["code"] == "text.context_too_large"
            assert inference.llm.count() == count
            assert (await host.get(f"/api/utterances?session_id={sid}")).json() == before
        finally:
            await client.close()


async def test_read_only_revocation_cancels_late_answer(protected, inference):
    async with protected.http() as host, protected.http() as viewer:
        await login(host, "alpha")
        await login(viewer, "viewer")
        client = MeetingClient(host)
        try:
            sid = (await client.connect())["id"]
            await host.put(
                f"/api/sessions/{sid}/members/local:viewer",
                json={"role": "watcher", "allow_questions": True},
            )
            inference.llm.when("撤权中的问题", Reply(text="禁止迟到的答案", delay_secs=3))
            pending = asyncio.create_task(
                viewer.post(f"/api/sessions/{sid}/text", json={"text": "撤权中的问题"})
            )
            await until_sync(
                lambda: any("撤权中的问题" in t for t in inference.llm.texts()), "只读推理已经发出"
            )
            revoked = await host.delete(f"/api/sessions/{sid}/members/local:viewer")
            assert revoked.status_code == 200
            assert (await pending).status_code in (403, 404)
            await asyncio.sleep(3.2)
            rows = (await host.get(f"/api/utterances?session_id={sid}")).json()["items"]
            assert not any(row["text"] == "禁止迟到的答案" for row in rows)
        finally:
            await client.close()


async def test_offline_claim_and_account_recovery(start_app, inference, tmp_path):
    legacy = await start_app()
    async with legacy.http() as http:
        client = MeetingClient(http)
        sid = (await client.connect())["id"]
        await client.close()
    await legacy.shutdown()
    settings = {"server.meeting_access": True, "server.local_users": USERS}
    cfg = app_config(inference, legacy.data_dir, 7860)
    cfg["server"].update(meeting_access=True, local_users=USERS)
    enabled = await start_app(
        settings, env={"E2E_LOCAL_PASSWORD": PASSWORD}, data_dir=legacy.data_dir
    )
    async with enabled.http() as http:
        await login(http, "alpha")
        assert (await http.get("/api/sessions")).json() == {"items": []}
        active_claim = await asyncio.to_thread(
            run_cli, tmp_path, cfg, "access", "claim", sid, "--owner", "alpha"
        )
        assert active_claim.returncode == 1 and "正在使用" in active_claim.stdout
        old_cookies = dict(http.cookies)
    await enabled.shutdown()
    listed = await asyncio.to_thread(run_cli, tmp_path, cfg, "access", "list-unclaimed")
    assert listed.returncode == 0 and sid in listed.stdout
    for owner, code in (("alpha", 0), ("alpha", 0), ("beta", 1)):
        claimed = await asyncio.to_thread(
            run_cli, tmp_path, cfg, "access", "claim", sid, "--owner", owner
        )
        assert claimed.returncode == code, claimed.stdout + claimed.stderr
    restarted = await start_app(
        settings, env={"E2E_LOCAL_PASSWORD": PASSWORD + "-rotated"}, data_dir=legacy.data_dir
    )
    async with restarted.http(cookies=old_cookies) as http:
        assert (await http.get("/api/sessions")).status_code == 401
        response = await http.post(
            "/api/auth/login", json={"user_id": "alpha", "password": PASSWORD + "-rotated"}
        )
        assert response.status_code == 200
        assert response.json()["principal"]["id"] == "local:alpha"
        assert (await http.get("/api/sessions")).json()["items"][0]["id"] == sid


async def test_two_meetings_cannot_cross_ids_or_take_over(protected, inference):
    async with protected.http() as first, protected.http() as second, protected.http() as same:
        await login(first, "alpha")
        await login(second, "beta")
        await login(same, "alpha")
        a = MeetingClient(first)
        b = MeetingClient(second)
        try:
            aid = (await a.connect())["id"]
            for http in (second, same):
                response = await http.patch("/api/offer", json={"pc_id": a.pc_id, "candidates": []})
                assert response.status_code == 404
                response = await http.post(
                    "/api/offer", json={"pc_id": a.pc_id, "sdp": "forged", "type": "offer"}
                )
                assert response.status_code == 404
            busy = await second.post("/api/offer", json={"sdp": "unused", "type": "offer"})
            assert busy.status_code == 409 and busy.json()["code"] == "access.busy"
            assert (
                await second.post(
                    "/api/offer",
                    json={"sdp": "unused", "type": "offer", "requestData": {"session_id": aid}},
                )
            ).status_code == 404
            inference.asr.say("甲会内容")
            await a.speak()
            au = await a.next_message("utterance")
            await a.close()

            async def not_live():
                return (await first.get(f"/api/sessions/{aid}")).json()["state"] != "live"

            await until(not_live, "甲已断开")
            bid = (await b.connect())["id"]
            inference.asr.say("乙会内容")
            await b.speak()
            bu = await b.next_message("utterance")
            assert (await first.get("/api/session")).json()["id"] == aid
            for path in ("utterances", "speakers", "frames", "tasks"):
                assert (await first.get(f"/api/{path}?session_id={bid}")).status_code == 404
                assert (await second.get(f"/api/{path}?session_id={aid}")).status_code == 404
            mixed = await first.post(
                "/api/utterances/speaker",
                json={"session_id": aid, "ids": [au["id"], bu["id"]], "new_speaker": "不能创建"},
            )
            assert mixed.status_code == 404
            assert not any(
                s["display_name"] == "不能创建"
                for s in (await first.get(f"/api/speakers?session_id={aid}")).json()["items"]
            )
            for method, path, body in (
                ("patch", f"/api/sessions/{aid}", {"title": "侵入"}),
                ("post", f"/api/sessions/{aid}/end", {}),
                ("delete", f"/api/sessions/{aid}", {}),
                ("post", f"/api/sessions/{aid}/report", {}),
                ("put", "/api/speakers/1", {"session_id": aid, "display_name": "侵入"}),
                ("post", "/api/speakers/1/merge", {"session_id": aid, "into": 2}),
            ):
                assert (await second.request(method, path, json=body)).status_code == 404, path
        finally:
            await a.close()
            await b.close()


async def test_idle_event_stream_closes_on_revoke_and_logout(protected):
    async with protected.http() as host, protected.http() as viewer:
        await login(host, "alpha")
        await login(viewer, "viewer")
        client = MeetingClient(host)
        sid = (await client.connect())["id"]
        await host.put(f"/api/sessions/{sid}/members/local:viewer", json={"role": "watcher"})
        stream = WatchingDevice(viewer, sid, "test")
        try:
            assert await stream.open() == 200
            await stream.next_message("state")
            assert (
                await host.delete(f"/api/sessions/{sid}/members/local:viewer")
            ).status_code == 200
            await stream.wait_for(lambda: stream.ended, "撤权关闭闲置SSE", timeout_secs=1)
            assert not stream.of_type("utterance")
        finally:
            await stream.close()
            await client.close()


async def test_idle_login_expiry_closes_sse_without_next_event(start_app):
    app = await start_app(
        {
            "server.meeting_access": True,
            "server.local_users": USERS,
            "server.auth_session_days": 10 / 86400,
        },
        env={"E2E_LOCAL_PASSWORD": PASSWORD},
    )
    async with app.http() as host, app.http() as other:
        await login(host, "alpha")
        client = MeetingClient(host)
        sid = (await client.connect())["id"]
        await client.close()
        await login(other, "alpha")
        stream = WatchingDevice(other, sid, "test")
        try:
            assert await stream.open() == 200
            await stream.next_message("state")
            await stream.wait_for(lambda: stream.ended, "到期关闭闲置SSE", timeout_secs=11)
            assert (await other.get(f"/api/sessions/{sid}")).status_code == 401
        finally:
            await stream.close()


async def test_realtime_preempts_read_only_and_does_not_retry(protected, inference):
    async with protected.http() as host, protected.http() as other:
        await login(host, "alpha")
        await login(other, "alpha")
        client = MeetingClient(host)
        try:
            sid = (await client.connect())["id"]
            inference.llm.when("被抢占的旁听问题", Reply(text="不应发布", delay_secs=3))
            pending = asyncio.create_task(
                other.post(f"/api/sessions/{sid}/text", json={"text": "被抢占的旁听问题"})
            )
            await until_sync(
                lambda: any("被抢占的旁听问题" in t for t in inference.llm.texts()),
                "后台推理已开始",
            )
            client.type_text("Jarvis 立即回答主持人的问题")
            response = await pending
            assert response.status_code == 409 and response.json()["code"] == "text.interrupted", (
                response.text
            )
            await asyncio.sleep(3.2)
            assert sum("被抢占的旁听问题" == t for t in inference.llm.texts()) == 1
            rows = (await host.get(f"/api/utterances?session_id={sid}")).json()["items"]
            assert not any(r["text"] == "不应发布" for r in rows)
        finally:
            await client.close()


async def test_logout_all_closes_recording_and_discards_queued_text(protected, inference):
    async with protected.http() as host, protected.http() as other:
        await login(host, "alpha")
        await login(other, "alpha")
        client = MeetingClient(host)
        try:
            sid = (await client.connect())["id"]
            inference.llm.when(
                "第一项在途", Reply(text="撤销后不应输出", delay_secs=3), sticky=True
            )
            client.type_text("Jarvis 第一项在途")
            await until_sync(
                lambda: any(
                    r.get("stream") and "第一项在途" in t
                    for r, t in zip(inference.llm.requests, inference.llm.texts(), strict=True)
                ),
                "第一项开始流式推理",
            )
            queued = await host.post(
                f"/api/sessions/{sid}/text", json={"text": "Jarvis 第二项排队"}
            )
            assert queued.json()["notice"]["code"] == "text.queued", queued.text
            assert (await other.post("/api/auth/logout-all")).status_code == 200
            await until_sync(
                lambda: client._channel.readyState == "closed",
                "撤销关闭录音数据通道",
                timeout_secs=1,
            )
            await asyncio.sleep(3.2)
            assert not any(
                r.get("stream") and "第二项排队" in t
                for r, t in zip(inference.llm.requests, inference.llm.texts(), strict=True)
            )
            assert "撤销后不应输出" not in client.bot_text()
            await login(other, "alpha")
            rows = (await other.get(f"/api/utterances?session_id={sid}")).json()["items"]
            assert not any(r["text"] == "撤销后不应输出" for r in rows)
        finally:
            await client.close()


async def test_late_tool_result_stays_with_original_meeting(protected, inference):
    async with protected.http() as host:
        await login(host, "alpha")
        first = MeetingClient(host)
        second = MeetingClient(host)
        try:
            aid = (await first.connect())["id"]
            inference.llm.when(
                "迟到工具", Reply(tool="delegate_task", arguments={"goal": "只属于甲的结果"})
            )
            inference.agent.when(
                "只属于甲的结果",
                Reply(
                    text=json.dumps(
                        {"brief": "甲的迟到简报", "detail_md": "甲的任务"}, ensure_ascii=False
                    ),
                    delay_secs=5,
                ),
            )
            first.type_text("Jarvis 迟到工具")
            await until_sync(lambda: inference.agent.count() > 0, "旧会工具已经发出")
            await first.close()
            bid = (await second.connect())["id"]
            assert aid != bid
            await asyncio.sleep(5.5)
            assert (await host.get(f"/api/tasks?session_id={bid}")).json()["items"] == []
            rows = (await host.get(f"/api/utterances?session_id={bid}")).json()["items"]
            assert not any("甲" in r["text"] for r in rows)
            assert "甲的迟到简报" not in second.bot_text()
            assert all(
                "甲的迟到简报" not in json.dumps(r["messages"], ensure_ascii=False)
                for r in inference.llm.requests
                if r.get("stream")
            )
        finally:
            await first.close()
            await second.close()


async def test_files_reports_and_task_artifacts_check_parent_meeting(start_app, inference):
    settings = {"server.meeting_access": True, "server.local_users": USERS}
    app = await start_app(settings, env={"E2E_LOCAL_PASSWORD": PASSWORD})
    async with app.http() as host:
        await login(host, "alpha")
        client = MeetingClient(host)
        sid = (await client.connect())["id"]
        pic = io.BytesIO()
        Image.new("RGB", (320, 180), "#228844").save(pic, format="JPEG")
        response = await host.post(
            "/api/frames",
            data={"session_id": sid, "captured_at": str(time.time())},
            files={"image": ("screen.jpg", pic.getvalue(), "image/jpeg")},
        )
        assert response.status_code == 200, response.text
        fid = response.json()["id"]
        await client.close()
    await app.shutdown()
    # 已停止的合成库模拟一次已完成任务及其真实落盘产物，不触碰用户资料。
    artifact = app.data_dir / "sessions" / sid / "tasks" / "t1" / "result.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("only alpha artifact", encoding="utf-8")
    with sqlite3.connect(app.data_dir / "meetings.db") as db:
        db.execute(
            "INSERT INTO tasks(id,session_id,goal,requested_t,status,brief,detail_md,created_at,finished_at,artifacts_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                sid + ".t1",
                sid,
                "synthetic",
                0,
                "succeeded",
                "task-brief-excluded",
                "details",
                time.time(),
                time.time(),
                '["result.txt"]',
            ),
        )
        db.execute(
            "INSERT INTO reports(session_id,created_at,status,text_md,finished_at) VALUES(?,?,?,?,?)",
            (sid, time.time(), "done", "only alpha report", time.time()),
        )
    resumed = await start_app(settings, env={"E2E_LOCAL_PASSWORD": PASSWORD}, data_dir=app.data_dir)
    async with resumed.http() as host, resumed.http() as stranger, resumed.http() as viewer:
        await login(host, "alpha")
        await login(stranger, "beta")
        await login(viewer, "viewer")
        await host.put(f"/api/sessions/{sid}/members/local:viewer", json={"role": "watcher"})
        paths = (
            f"/api/frames/{fid}/image",
            f"/api/tasks/{sid}.t1",
            f"/api/tasks/{sid}.t1/artifacts/result.txt",
            f"/api/sessions/{sid}/report",
            f"/api/sessions/{sid}/report.md",
            f"/api/export/{sid}.zip",
        )
        for path in paths:
            denied = await stranger.get(path)
            assert denied.status_code == 404, (path, denied.text)
            assert (await viewer.get(path)).status_code == 200, path
        assert (await stranger.get(f"/api/frames/{fid}/image")).json() == (
            await stranger.get("/api/frames/999999/image")
        ).json()
        assert (await stranger.get(f"/api/tasks/{sid}.t1")).json() == (
            await stranger.get("/api/tasks/unknown.t1")
        ).json()
        assert (await stranger.post(f"/api/tasks/{sid}.t1/cancel")).status_code == 404
        assert (await viewer.post(f"/api/tasks/{sid}.t1/cancel")).status_code == 403
        assert (await viewer.post(f"/api/sessions/{sid}/report")).status_code == 403
        for path in (
            f"/api/tasks/{sid}.t1/artifacts/..%2F..%2Fmeetings.db",
            f"/api/tasks/{sid}.t1/artifacts/unreported.txt",
        ):
            assert (await host.get(path)).status_code == 404, path
        image = await viewer.get(f"/api/frames/{fid}/image")
        assert image.headers["cache-control"] == "private, no-cache"
        artifact_response = await viewer.get(f"/api/tasks/{sid}.t1/artifacts/result.txt")
        assert artifact_response.headers["x-content-type-options"] == "nosniff"


async def test_question_capacity_is_bounded_and_revoke_drains_waiters(start_app, inference):
    users = USERS + [
        {"id": f"viewer{i}", "display_name": f"Viewer {i}", "password_env": "E2E_LOCAL_PASSWORD"}
        for i in range(6)
    ]
    app = await start_app(
        {"server.meeting_access": True, "server.local_users": users},
        env={"E2E_LOCAL_PASSWORD": PASSWORD},
    )
    async with app.http() as host:
        await login(host, "alpha")
        client = MeetingClient(host)
        others = [app.http() for _ in range(6)]
        pending = []
        try:
            sid = (await client.connect())["id"]
            for i, http in enumerate(others):
                await login(http, f"viewer{i}")
                await host.put(
                    f"/api/sessions/{sid}/members/local:viewer{i}",
                    json={"role": "watcher", "allow_questions": True},
                )
                inference.llm.when(f"排队问题{i}", Reply(text="排队答案", delay_secs=5))
            pending = [
                asyncio.create_task(
                    http.post(f"/api/sessions/{sid}/text", json={"text": f"排队问题{i}"})
                )
                for i, http in enumerate(others[:5])
            ]

            async def all_accepted():
                rows = (await host.get(f"/api/utterances?session_id={sid}")).json()["items"]
                return sum(r["text"].startswith("排队问题") for r in rows) == 5

            await until(all_accepted, "五个请求已受理")
            for http in (others[0], others[5]):
                response = await http.post(f"/api/sessions/{sid}/text", json={"text": "超容量"})
                assert response.status_code == 429 and response.json()["code"] == "text.busy"
            for i in range(5):
                await host.delete(f"/api/sessions/{sid}/members/local:viewer{i}")
            assert all(r.status_code in (403, 404) for r in await asyncio.gather(*pending))
            rows = (await host.get(f"/api/utterances?session_id={sid}")).json()["items"]
            assert not any(r["text"] in ("排队答案", "超容量") for r in rows)
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for http in others:
                await http.aclose()
            await client.close()


async def test_read_only_deadline_includes_provider_wait(protected, inference):
    async with protected.http() as host, protected.http() as other:
        await login(host, "alpha")
        await login(other, "alpha")
        client = MeetingClient(host)
        try:
            sid = (await client.connect())["id"]
            inference.llm.when("超过截止时间", Reply(text="过期答案", delay_secs=65))
            started = time.monotonic()
            result = await other.post(
                f"/api/sessions/{sid}/text", json={"text": "超过截止时间"}, timeout=70
            )
            elapsed = time.monotonic() - started
            assert result.status_code == 504 and result.json()["code"] == "text.timeout"
            assert 59 <= elapsed < 64
            rows = (await host.get(f"/api/utterances?session_id={sid}")).json()["items"]
            assert not any(r["text"] == "过期答案" for r in rows)
        finally:
            await client.close()


async def test_bounded_context_uses_configured_nonloopback_provider(start_app):
    address = socket.gethostbyname(socket.gethostname())
    if address.startswith("127."):
        pytest.skip("本机没有可供假提供方绑定的非回环IPv4地址")
    remote = Inference(host=address)
    remote.start()
    try:
        settings = {
            "server.meeting_access": True,
            "server.local_users": USERS,
            "realtime.context_budget_tokens": 2000,
            "realtime_llm.mode": "openai_api",
            "realtime_llm.openai_api.base_url": remote.llm.origin + "/v1",
            "realtime_llm.openai_api.model": "fake-configured-model",
            "realtime_llm.openai_api.api_key_env": "E2E_PROVIDER_KEY",
        }
        env = {"E2E_LOCAL_PASSWORD": PASSWORD, "E2E_PROVIDER_KEY": "fake-provider-key"}
        app = await start_app(settings, env=env)
        async with app.http() as host:
            await login(host, "alpha")
            client = MeetingClient(host)
            sid = (await client.connect())["id"]
            await client.close()
        await app.shutdown()
        with sqlite3.connect(app.data_dir / "meetings.db") as db:
            db.execute(
                "INSERT INTO digests(session_id,t_from,t_to,text,created_at) VALUES(?,?,?,?,?)",
                (sid, 0, 0, "摘要" * 100000, time.time()),
            )
            for i in range(110):
                db.execute(
                    "INSERT INTO utterances(session_id,speaker_idx,t_start,t_end,text) VALUES(?,?,?,?,?)",
                    (sid, 0, i, i, f"line{i}:" + "会" * 3000),
                )
            db.execute(
                "INSERT INTO tasks(id,session_id,goal,requested_t,status,brief,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    sid + ".t1",
                    sid,
                    "任务",
                    0,
                    "succeeded",
                    "NEVER_INCLUDE_TASK_BRIEF",
                    time.time(),
                    time.time(),
                ),
            )
        resumed = await start_app(settings, env=env, data_dir=app.data_dir)
        async with resumed.http() as host, resumed.http() as other:
            await login(host, "alpha")
            await login(other, "alpha")
            client = MeetingClient(host)
            try:
                await client.connect(sid)
                response = await other.post(
                    f"/api/sessions/{sid}/text", json={"text": "有界材料问题"}
                )
                assert response.status_code == 200, response.text
                sent = next(
                    r
                    for r in remote.llm.requests
                    if r["messages"][-1].get("content") == "有界材料问题"
                )
                encoded = json.dumps(
                    sent["messages"], ensure_ascii=False, separators=(",", ":")
                ).encode()
                reserve = sent.get("max_completion_tokens", sent.get("max_tokens"))
                assert len(encoded) <= 4 * (2000 - reserve)
                assert sent["model"] == "fake-configured-model" and not sent.get("tools")
                assert "NEVER_INCLUDE_TASK_BRIEF" not in encoded.decode()
                assert "line0:" not in encoded.decode()
                assert "line109:" in encoded.decode()
                assert estimate_messages(sent["messages"]) + reserve <= 2000
                assert "省略" in encoded.decode()
            finally:
                await client.close()
    finally:
        remote.stop()


@pytest.mark.parametrize(
    ("marker", "long_text", "question"),
    [
        ("最新证据标记:", "会" * 3000, "最近记录的重点是什么？"),
        ("LATEST_EVIDENCE_MARKER:", '\\"' * 3000, "Explain this evidence: " + "x" * 1900),
    ],
    ids=["chinese-token-limit", "english-json-byte-limit"],
)
async def test_latest_long_material_keeps_bounded_excerpt(
    start_app, inference, marker, long_text, question
):
    settings = {
        "server.meeting_access": True,
        "server.local_users": USERS,
        "realtime.context_budget_tokens": 2000,
    }
    app = await start_app(settings, env={"E2E_LOCAL_PASSWORD": PASSWORD})
    async with app.http() as host:
        await login(host, "alpha")
        client = MeetingClient(host)
        sid = (await client.connect())["id"]
        await client.close()
    await app.shutdown()
    with sqlite3.connect(app.data_dir / "meetings.db") as db:
        db.execute(
            "INSERT INTO utterances(session_id,speaker_idx,t_start,t_end,text) VALUES(?,?,?,?,?)",
            (sid, 0, 0, 0, marker + long_text),
        )
    resumed = await start_app(settings, env={"E2E_LOCAL_PASSWORD": PASSWORD}, data_dir=app.data_dir)
    async with resumed.http() as host, resumed.http() as other:
        await login(host, "alpha")
        await login(other, "alpha")
        client = MeetingClient(host)
        try:
            await client.connect(sid)
            response = await other.post(f"/api/sessions/{sid}/text", json={"text": question})
            assert response.status_code == 200, response.text
            sent = next(
                r for r in inference.llm.requests if r["messages"][-1].get("content") == question
            )
            material = sent["messages"][-2]["content"]
            assert marker in material and "…[截断]" in material
            assert long_text not in material
            assert sent["messages"][-1]["content"] == question
            reserve = sent.get("max_completion_tokens", sent.get("max_tokens"))
            encoded = json.dumps(
                sent["messages"], ensure_ascii=False, separators=(",", ":")
            ).encode()
            assert len(encoded) <= min(32768, 4 * (2000 - reserve))
            assert estimate_messages(sent["messages"]) + reserve <= 2000
        finally:
            await client.close()
