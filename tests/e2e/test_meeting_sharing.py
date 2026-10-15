"""真实应用的会议凭据、精确登录来源、在途失效和秘密边界。"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import sqlite3
import sys
import time

import httpx
import pytest
from PIL import Image

from agentic_meeting.config import AppConfig
from agentic_meeting.pipeline.services import build_realtime_llm
from agentic_meeting.store.db import Store
from agentic_meeting.types import Utterance
from agentic_meeting.web.app import create_app, make_server

from .conftest import app_config
from .inference import Reply
from .meeting_client import MeetingClient, WatchingDevice
from .test_meeting_access import PASSWORD, USERS
from .waiting import until, until_sync

SITE = "synthetic-site-password"
MEETING_PASSWORD = "synthetic-meeting-password"


@pytest.fixture
async def shared_app(start_app):
    return await start_app(
        {
            "server.meeting_access": True,
            "server.local_users": USERS,
            "server.password_env": "E2E_SITE_PASSWORD",
        },
        env={"E2E_LOCAL_PASSWORD": PASSWORD, "E2E_SITE_PASSWORD": SITE},
    )


async def sign_in(http, user="alpha"):
    response = await http.post(
        "/api/auth/login", json={"user_id": user, "password": PASSWORD, "site_password": SITE}
    )
    assert response.status_code == 200
    http.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response.json()


async def make_link(http, sid, **options):
    response = await http.post(f"/api/sessions/{sid}/shares/links", json=options)
    assert response.status_code == 201, response.text
    result = response.json()
    return {
        "session_id": sid,
        "kind": "link",
        "share_id": result["share"]["id"],
        "token": result["token"],
        "site_password": SITE,
    }


async def redeem(http, app, value):
    response = await http.post("/api/shares/exchange", json=value, headers={"Origin": app.base_url})
    if response.status_code == 200:
        http.headers["X-CSRF-Token"] = response.json()["auth"]["csrf_token"]
    return response


async def test_guest_and_personal_exact_login_sources_and_scope(shared_app, inference):
    app = shared_app
    async with (
        app.http() as host,
        app.http() as guest,
        app.http() as guest2,
        app.http() as viewer,
        app.http() as other_login,
    ):
        await sign_in(host)
        original = await sign_in(viewer, "viewer")
        await sign_in(other_login, "viewer")
        recorder = MeetingClient(host)
        try:
            sid = (await recorder.connect())["id"]
            link = await make_link(host, sid)
            for browser in (guest, guest2, viewer):
                response = await redeem(browser, app, link)
                assert response.status_code == 200, response.text
                assert "no-store" in response.headers["cache-control"]
                assert response.headers["referrer-policy"] == "no-referrer"
            one, two = (await guest.get("/api/auth")).json(), (await guest2.get("/api/auth")).json()
            assert one["principal"]["guest"] and two["principal"]["guest"]
            assert one["principal"]["id"] != two["principal"]["id"]
            assert not one["principal"]["can_host"]
            assert (await viewer.get("/api/auth")).json() == original
            assert (await other_login.get(f"/api/sessions/{sid}")).status_code == 404
            assert (await guest.get("/api/session")).json()["id"] == sid
            assert [
                r["id"] for r in (await guest.get("/api/sessions?limit=1")).json()["items"]
            ] == [sid]
            assert (await guest.get(f"/api/export/{sid}.json")).status_code == 200
            assert (await guest.get(f"/api/sessions/{'0' * 32}")).status_code == 404
            assert (
                await guest.post(f"/api/sessions/{sid}/text", json={"text": "不应派发"})
            ).status_code == 403
            for method, path, data in (
                ("PATCH", f"/api/sessions/{sid}", {"title": "forbidden"}),
                ("GET", f"/api/sessions/{sid}/members", None),
                ("POST", f"/api/sessions/{sid}/shares/links", {}),
                ("POST", "/api/offer", {"sdp": "invalid", "type": "offer"}),
            ):
                assert (await guest.request(method, path, json=data)).status_code in (403, 422)
            assert (
                await host.put(
                    f"/api/sessions/{sid}/members/{one['principal']['id']}",
                    json={"role": "watcher"},
                )
            ).status_code == 400
            # 独立ACL不会被撤销分享来源误伤。
            assert (
                await host.put(
                    f"/api/sessions/{sid}/members/local:viewer", json={"role": "watcher"}
                )
            ).status_code == 200
            assert (
                await host.delete(f"/api/sessions/{sid}/shares/{link['share_id']}")
            ).status_code == 200
            assert (await guest.get(f"/api/sessions/{sid}")).status_code == 404
            assert (await viewer.get(f"/api/sessions/{sid}")).status_code == 200
            assert (await host.get(f"/api/sessions/{sid}")).json()["access"]["role"] == "host"
            assert (await guest.get("/api/auth")).json()["authenticated"]
            assert (await guest.post("/api/auth/logout")).status_code == 200
            with sqlite3.connect(app.data_dir / "meetings.db") as db:
                assert not db.execute(
                    "SELECT 1 FROM principals WHERE id = ?", (one["principal"]["id"],)
                ).fetchone()
        finally:
            await recorder.close()


async def test_password_rotation_link_sources_and_private_storage(shared_app):
    app = shared_app
    async with app.http() as host, app.http() as guest:
        await sign_in(host)
        recorder = MeetingClient(host)
        try:
            sid = (await recorder.connect())["id"]
            path = f"/api/sessions/{sid}/shares/password"
            assert (
                await host.put(path, json={"password": MEETING_PASSWORD, "allow_questions": True})
            ).status_code == 200
            credential = {
                "session_id": sid,
                "kind": "password",
                "password": MEETING_PASSWORD,
                "site_password": SITE,
            }
            assert (await redeem(guest, app, credential)).status_code == 200
            assert (await guest.get(f"/api/sessions/{sid}")).json()["access"]["allow_questions"]
            extra = await make_link(host, sid)
            assert (await redeem(guest, app, extra)).status_code == 200
            rotated = await host.put(path, json={"password": MEETING_PASSWORD + "-new"})
            assert rotated.status_code == 200
            detail = (await guest.get(f"/api/sessions/{sid}")).json()
            assert not detail["access"]["allow_questions"] and detail["access"]["role"] == "watcher"
            assert (await redeem(guest, app, credential)).status_code == 404
            assert (
                await host.delete(f"/api/sessions/{sid}/shares/{extra['share_id']}")
            ).status_code == 200
            assert (await guest.get(f"/api/sessions/{sid}")).status_code == 404
            assert (
                await redeem(guest, app, {**credential, "password": MEETING_PASSWORD + "-new"})
            ).status_code == 200
            with sqlite3.connect(app.data_dir / "meetings.db") as db:
                dump = repr(
                    [
                        db.execute(f"SELECT * FROM {table}").fetchall()
                        for table in (
                            "meeting_shares",
                            "share_access",
                            "guest_logins",
                            "login_sessions",
                        )
                    ]
                )
                assert MEETING_PASSWORD not in dump and extra["token"] not in dump
                assert not db.execute(
                    "SELECT 1 FROM meeting_members WHERE principal_id LIKE 'guest:%'"
                ).fetchone()
            assert MEETING_PASSWORD not in app.log_text() and extra["token"] not in app.log_text()
            listing = (await host.get(f"/api/sessions/{sid}/shares")).json()
            assert "credential_hash" not in json.dumps(listing) and "salt" not in json.dumps(
                listing
            )
        finally:
            await recorder.close()


@pytest.mark.parametrize("mode", ["revoke", "rotate", "expiry"])
async def test_share_invalidates_two_streams_and_reconnect(shared_app, mode):
    app = shared_app
    async with app.http() as host, app.http() as guest, app.http() as other:
        await sign_in(host)
        recorder, streams = MeetingClient(host), []
        try:
            sid = (await recorder.connect())["id"]
            link = await make_link(host, sid, expires_in=3 if mode == "expiry" else 86400)
            for browser in (guest, other):
                assert (await redeem(browser, app, link)).status_code == 200
            for browser in (guest, guest, other):
                stream = WatchingDevice(browser, sid, "sharing")
                streams.append(stream)
                assert await stream.open() == 200
                await stream.next_message("state")
            expires = (await host.get(f"/api/sessions/{sid}/shares")).json()["items"][0][
                "expires_at"
            ]
            began = time.monotonic()
            if mode == "revoke":
                assert (
                    await host.delete(f"/api/sessions/{sid}/shares/{link['share_id']}")
                ).status_code == 200
            elif mode == "rotate":
                result = await host.post(
                    f"/api/sessions/{sid}/shares/{link['share_id']}/rotate", json={}
                )
                assert (
                    result.status_code == 200 and result.json()["share"]["id"] != link["share_id"]
                )
            await asyncio.gather(
                *(
                    s.wait_for(
                        lambda stream=s: stream.ended,
                        "分享双流失效",
                        timeout_secs=5 if mode == "expiry" else 1,
                    )
                    for s in streams
                )
            )
            if mode == "expiry":
                assert 0 <= time.time() - expires < 1
            else:
                assert time.monotonic() - began < 1
            assert (await guest.get(f"/api/sessions/{sid}/events?after=old:1")).status_code == 404
            assert (await guest.get(f"/api/export/{sid}.json")).status_code == 404
            assert (await redeem(guest, app, link)).status_code == 404
            assert (
                await host.patch(f"/api/sessions/{sid}", json={"title": "host persists"})
            ).status_code == 200
            assert not recorder.of_type("session_closed")
        finally:
            for stream in streams:
                await stream.close()
            await recorder.close()


async def test_exchange_origin_csrf_site_and_strict_options(shared_app):
    app = shared_app
    async with app.http() as host, app.http() as guest, app.http() as viewer:
        await sign_in(host)
        await sign_in(viewer, "viewer")
        recorder = MeetingClient(host)
        try:
            sid = (await recorder.connect())["id"]
            link = await make_link(host, sid)
            for origin in (None, "https://foreign.invalid"):
                assert (
                    await guest.post(
                        "/api/shares/exchange",
                        json=link,
                        headers={} if origin is None else {"Origin": origin},
                    )
                ).status_code == 403
            assert (
                await viewer.post(
                    "/api/shares/exchange",
                    json=link,
                    headers={"Origin": app.base_url, "X-CSRF-Token": "bad"},
                )
            ).status_code == 403
            assert (
                await redeem(guest, app, {**link, "site_password": MEETING_PASSWORD})
            ).status_code == 404
            assert (await redeem(viewer, app, {**link, "site_password": ""})).status_code == 404
            assert (await redeem(guest, app, link)).status_code == 200
            for ttl in (0, -1, 604801, True, "3600"):
                assert (
                    await host.post(f"/api/sessions/{sid}/shares/links", json={"expires_in": ttl})
                ).status_code == 400
            assert (
                await host.put(f"/api/sessions/{sid}/shares/password", json={"password": "short"})
            ).status_code == 400
            assert (
                await guest.post(
                    "/api/shares/exchange",
                    content="x" * 17000,
                    headers={"Origin": app.base_url, "Content-Type": "application/json"},
                )
            ).status_code == 413
        finally:
            await recorder.close()


async def test_link_limit_and_shared_concurrent_failure_budget(shared_app):
    app = shared_app
    async with app.http() as host, app.http() as guest:
        await sign_in(host)
        recorder = MeetingClient(host)
        try:
            sid = (await recorder.connect())["id"]
            links = [await make_link(host, sid) for _ in range(8)]
            assert (
                await host.post(f"/api/sessions/{sid}/shares/links", json={})
            ).status_code == 409
            wrong = {
                "session_id": sid,
                "kind": "password",
                "password": "incorrect",
                "site_password": SITE,
            }
            # 先留下两次失败，成功链接不能洗掉它们；后续并发只再允许三次昂贵尝试。
            for _ in range(2):
                assert (await redeem(guest, app, wrong)).status_code == 404
            assert (await redeem(guest, app, links[0])).status_code == 200
            results = await asyncio.gather(*(redeem(guest, app, wrong) for _ in range(10)))
            assert [r.status_code for r in results].count(404) == 3
            assert [r.status_code for r in results].count(429) == 7
            assert all("Retry-After" in r.headers for r in results if r.status_code == 429)
            login = await guest.post(
                "/api/auth/login",
                json={"user_id": "viewer", "password": PASSWORD, "site_password": SITE},
            )
            assert login.status_code == 429
        finally:
            await recorder.close()


async def test_deleted_wrong_meeting_and_forged_link_do_not_create_guest(shared_app):
    app = shared_app
    async with app.http() as host, app.http() as guest:
        await sign_in(host)
        recorder = MeetingClient(host)
        try:
            sid = (await recorder.connect())["id"]
            link = await make_link(host, sid)
            responses = [
                await redeem(guest, app, {**link, "session_id": "0" * 32}),
                await redeem(guest, app, {**link, "token": "A" * 43}),
            ]
            await recorder.close()

            async def stopped():
                return (await host.get(f"/api/sessions/{sid}")).json()["state"] == "interrupted"

            await until(stopped, "录音停止")
            assert (await host.delete(f"/api/sessions/{sid}")).status_code == 200
            responses.append(await redeem(guest, app, link))
            assert all(
                r.status_code == 404 and r.json()["code"] == "share.invalid" for r in responses
            )
            assert not (await guest.get("/api/auth")).json()["authenticated"]
            with sqlite3.connect(app.data_dir / "meetings.db") as db:
                assert not db.execute("SELECT 1 FROM guest_logins").fetchone()
                assert not db.execute("SELECT 1 FROM meeting_shares").fetchone()
                assert not db.execute("SELECT 1 FROM share_access").fetchone()
        finally:
            await recorder.close()


async def test_legacy_sharing_endpoints_remain_absent(start_app):
    app = await start_app()
    async with app.http() as http:
        assert (await http.post("/api/shares/exchange", json={})).status_code == 404
        assert (
            await http.post(f"/api/sessions/{'0' * 32}/shares/links", json={})
        ).status_code == 404


async def test_guest_question_source_cancellation_and_personal_switch(shared_app, inference):
    app = shared_app
    async with app.http() as host, app.http() as guest:
        await sign_in(host)
        recorder = MeetingClient(host)
        try:
            sid = (await recorder.connect())["id"]
            read = await make_link(host, sid)
            question = await make_link(host, sid, allow_questions=True)
            assert (await redeem(guest, app, read)).status_code == 200
            assert (await redeem(guest, app, question)).status_code == 200
            inference.llm.when("访客只读问答", Reply(text="访客的合成答案"))
            response = await guest.post(f"/api/sessions/{sid}/text", json={"text": "访客只读问答"})
            assert response.status_code == 200 and response.json()["accepted"]
            request = next(
                r
                for r in inference.llm.requests
                if "访客只读问答" in json.dumps(r, ensure_ascii=False)
            )
            assert not request.get("tools")
            inference.llm.when("将撤销的访客问题", Reply(text="禁止访客迟到写入", delay_secs=3))
            pending = asyncio.create_task(
                guest.post(f"/api/sessions/{sid}/text", json={"text": "将撤销的访客问题"})
            )
            await until_sync(
                lambda: any("将撤销的访客问题" in t for t in inference.llm.texts()), "访客问答派发"
            )
            began = time.monotonic()
            assert (
                await host.delete(f"/api/sessions/{sid}/shares/{question['share_id']}")
            ).status_code == 200
            assert (await pending).status_code == 403
            assert time.monotonic() - began < 1
            assert (await guest.get(f"/api/sessions/{sid}")).status_code == 200
            await asyncio.sleep(3.2)
            rows = (await host.get(f"/api/utterances?session_id={sid}")).json()["items"]
            assert any(r["text"] == "访客的合成答案" for r in rows)
            assert not any(r["text"] == "禁止访客迟到写入" for r in rows)
            guest_cookie = dict(guest.cookies)
            await sign_in(guest, "viewer")
            assert (await guest.get(f"/api/sessions/{sid}")).status_code == 404
            assert (await redeem(guest, app, read)).status_code == 200
            assert (await guest.post("/api/auth/logout-all")).status_code == 200
            assert (await guest.get(f"/api/sessions/{sid}")).status_code == 401
            async with app.http(cookies=guest_cookie) as old_guest:
                assert (await old_guest.get(f"/api/sessions/{sid}")).status_code == 200
        finally:
            await recorder.close()


@pytest.mark.parametrize("channel", ["events", "download"])
@pytest.mark.parametrize("mode", ["revoke", "rotate", "expiry"])
async def test_real_send_backpressure_respects_share_lifecycle(
    inference, tmp_path, monkeypatch, caplog, channel, mode
):
    """测量真实TCP背压下应用任务结束；不把OS缓存排空等同于服务端撤权。"""
    monkeypatch.setenv("E2E_LOCAL_PASSWORD", PASSWORD)
    monkeypatch.setenv("E2E_SITE_PASSWORD", SITE)
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    config = app_config(inference, tmp_path / "data", port)
    config["server"].update(
        meeting_access=True, local_users=USERS, password_env="E2E_SITE_PASSWORD"
    )
    cfg = AppConfig.model_validate(config)
    store = await Store.open(tmp_path / "data/meetings.db", 64, assistant_name="Jarvis")
    application = create_app(
        cfg, store=store, background_llm=build_realtime_llm(cfg, "", background=True)
    )
    clock = asyncio.get_running_loop().time
    wall_offset = time.time() - clock()
    network = {"sends": [], "mode": mode, "channel": channel, "wall_offset": wall_offset}

    async def observed(scope, receive, send):
        if scope["type"] != "http" or (b"user-agent", b"slow-share") not in scope["headers"]:
            return await application(scope, receive, send)

        async def measured(message):
            network["sending"] = clock()
            network["sending_bytes"] = len(message.get("body", b""))
            try:
                await send(message)
            finally:
                network["sends"].append(
                    (len(message.get("body", b"")), clock() - network["sending"])
                )
                network["sending"] = None

        try:
            await application(scope, receive, measured)
        finally:
            network["finished"] = clock()

    observed.state = application.state
    server = make_server(cfg, app=observed)
    log = logging.getLogger("uvicorn.error")
    log.addHandler(caplog.handler)
    running = asyncio.create_task(server.serve(sockets=[listener]))
    recorder = writer = None
    try:
        await until_sync(lambda: server.started, "分享背压应用启动")
        origin = f"http://127.0.0.1:{port}"
        async with (
            httpx.AsyncClient(base_url=origin, trust_env=False) as host,
            httpx.AsyncClient(base_url=origin, trust_env=False) as guest,
        ):
            await sign_in(host)
            recorder = MeetingClient(host)
            sid = (await recorder.connect())["id"]
            network["payload_started_wall"] = time.time()
            for index in range(64):
                await store.add_utterance(
                    Utterance(sid, 0, index, index + 0.1, "SYNTHETIC" * 16384)
                )
            frame = await store.add_frame(
                sid, t=0, width=2048, height=2048, suffix=".png", caption_status="done"
            )
            frame_path = tmp_path / "data" / frame.path
            frame_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (2048, 2048)).save(frame_path, compress_level=0)
            network["payload_finished_wall"] = time.time()
            # 先准备真实负载，再开始原10秒到期窗口；不让磁盘/编码耗时占据背压阶段。
            link = await make_link(host, sid, expires_in=10 if mode == "expiry" else 86400)
            response = await guest.post(
                "/api/shares/exchange", json=link, headers={"Origin": origin}
            )
            assert response.status_code == 200
            expires = response.json()["access"]["expires_at"]
            network.update(expires_wall=expires, source_ready_wall=time.time())
            path = (
                f"/api/sessions/{sid}/events"
                if channel == "events"
                else f"/api/frames/{frame.id}/image"
            )
            raw = socket.socket()
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            network["receive_buffer"] = raw.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
            network["listener_send_buffer"] = listener.getsockopt(
                socket.SOL_SOCKET, socket.SO_SNDBUF
            )
            raw.setblocking(False)
            await asyncio.get_running_loop().sock_connect(raw, ("127.0.0.1", port))
            reader, writer = await asyncio.open_connection(sock=raw, limit=1024)
            cookie = "; ".join(f"{k}={v}" for k, v in guest.cookies.items())
            writer.write(
                f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUser-Agent: slow-share\r\nCookie: {cookie}\r\n\r\n".encode()
            )
            await writer.drain()
            headers = await reader.readuntil(b"\r\n\r\n")
            network.update(headers_wall=time.time(), status_line=headers.split(b"\r\n")[0].decode())
            assert headers.startswith(b"HTTP/1.1 200")
            captured = asyncio.Event()

            async def produce_changes():
                network["producer_started_wall"] = time.time()
                if channel == "events":
                    for index in range(400):
                        # 复用R02的有限真实HTTP变更，不让推理往返消耗到期前的压力预算。
                        result = await host.put(
                            "/api/speakers/0",
                            json={"session_id": sid, "display_name": f"合成{index}:" + "s" * 40},
                        )
                        assert result.status_code == 200, result.text
                        network["http_changes"] = index + 1
                network["producer_finished_wall"] = time.time()

            async def observe_backpressure():
                while not (
                    network.get("sending") is not None
                    and network["sending_bytes"] > 0
                    and clock() - network["sending"] > 0.2
                ):
                    assert "finished" not in network, network
                    await asyncio.sleep(0.05)
                network["backpressure_observed"] = clock()
                assert "finished" not in network, network
                if mode == "expiry":
                    assert wall_offset + network["backpressure_observed"] < expires, network
                captured.set()
                began = clock()
                if mode == "revoke":
                    assert (
                        await host.delete(f"/api/sessions/{sid}/shares/{link['share_id']}")
                    ).status_code == 200
                elif mode == "rotate":
                    assert (
                        await host.post(
                            f"/api/sessions/{sid}/shares/{link['share_id']}/rotate", json={}
                        )
                    ).status_code == 200
                await until_sync(
                    lambda: "finished" in network,
                    "失效取消阻塞ASGI任务",
                    timeout_secs=12 if mode == "expiry" else 1,
                )
                observer_wall, observer_monotonic = time.time(), clock()
                actual = (
                    wall_offset + network["finished"] - expires
                    if mode == "expiry"
                    else network["finished"] - began
                )
                observed = observer_wall - expires if mode == "expiry" else actual
                network.update(
                    mode=mode,
                    channel=channel,
                    wall_offset=wall_offset,
                    expires_wall=expires,
                    finished_wall=wall_offset + network["finished"],
                    observer_wall=observer_wall,
                    observer_monotonic=observer_monotonic,
                    invalidation_seconds=actual,
                    observer_invalidation_seconds=observed,
                )
                (tmp_path / "network.json").write_text(json.dumps(network), encoding="utf-8")
                assert actual < 1, actual
                assert observed < 1, observed
                assert (await guest.get(path)).status_code == 404
                assert all(
                    lease.identity.principal != response.json()["auth"]["principal"]["id"]
                    for lease in application.state.resources.access.leases
                )
                assert not application.state.resources.sessions.watchers.for_session(
                    sid
                ).subscribers
                assert not recorder.of_type("session_closed")

            async with asyncio.TaskGroup() as tasks:
                producer = tasks.create_task(produce_changes())
                tasks.create_task(observe_backpressure())
                await producer
                if channel == "events":
                    assert network["http_changes"] == 400
                await until_sync(captured.is_set, "真实TCP背压实时观测", timeout_secs=3)
            # 故障注入可能让终止帧也阻塞；有界超时保留可见，不能吞错。
            assert "Task was destroyed" not in caplog.text
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        if recorder is not None:
            await recorder.close()
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(running), 10)
        finally:
            log.removeHandler(caplog.handler)
            await store.close()
            network["diagnostic_finished_wall"] = time.time()
            (tmp_path / "network.json").write_text(json.dumps(network), encoding="utf-8")


async def test_share_restart_migration_and_guest_cookie_recovery(start_app):
    settings = {
        "server.meeting_access": True,
        "server.local_users": USERS,
        "server.password_env": "E2E_SITE_PASSWORD",
    }
    env = {"E2E_LOCAL_PASSWORD": PASSWORD, "E2E_SITE_PASSWORD": SITE}
    first = await start_app(settings, env=env)
    async with first.http() as host, first.http() as guest:
        await sign_in(host)
        recorder = MeetingClient(host)
        sid = (await recorder.connect())["id"]
        link = await make_link(host, sid)
        assert (await redeem(guest, first, link)).status_code == 200
        cookie = dict(guest.cookies)
        await recorder.close()
    first_exit = await first.shutdown()
    assert first_exit in (0, 3) if sys.platform == "win32" else first_exit == 0
    assert "Application shutdown complete." in first.log_text()
    assert "ERROR" not in first.log_text()
    restarted = await start_app(settings, env=env, data_dir=first.data_dir)
    async with restarted.http(cookies=cookie) as guest:
        assert (await guest.get(f"/api/sessions/{sid}")).status_code == 200
        assert (await guest.get("/api/auth")).json()["principal"]["guest"]
    restart_exit = await restarted.shutdown()
    assert restart_exit in (0, 3) if sys.platform == "win32" else restart_exit == 0
    assert "Application shutdown complete." in restarted.log_text()
    assert "ERROR" not in restarted.log_text()
    (first.data_dir.parent / "restart-receipt.json").write_text(
        json.dumps(
            {
                "first_exit": first_exit,
                "restart_exit": restart_exit,
                "signal": "CTRL_BREAK_EVENT" if sys.platform == "win32" else "SIGINT",
                "shutdown_complete": True,
                "share_survived_restart": True,
            }
        ),
        encoding="utf-8",
    )
    # 合成旧版本数据库缺少新表：升级不得把旧会议或ACL自动公开。
    with sqlite3.connect(first.data_dir / "meetings.db") as db:
        db.executescript(
            "DROP TABLE share_access; DROP TABLE guest_logins; DROP TABLE meeting_shares;"
        )
    migrated = await start_app(settings, env=env, data_dir=first.data_dir)
    async with migrated.http() as host, migrated.http(cookies=cookie) as old_guest:
        await sign_in(host)
        assert (await host.get(f"/api/sessions/{sid}/shares")).json()["items"] == []
        assert (await old_guest.get(f"/api/sessions/{sid}")).status_code == 401
        assert (await host.get(f"/api/sessions/{sid}")).json()["access"]["role"] == "host"


async def test_expired_guest_login_requires_explicit_retry_and_prunes_only_guest(start_app):
    app = await start_app(
        {
            "server.meeting_access": True,
            "server.local_users": USERS,
            "server.password_env": "E2E_SITE_PASSWORD",
            "server.auth_session_days": 4 / 86400,
        },
        env={"E2E_LOCAL_PASSWORD": PASSWORD, "E2E_SITE_PASSWORD": SITE},
    )
    async with app.http() as host, app.http() as guest:
        await sign_in(host)
        recorder = MeetingClient(host)
        stream = None
        try:
            sid = (await recorder.connect())["id"]
            link = await make_link(host, sid)
            response = await redeem(guest, app, link)
            assert response.status_code == 200
            before = response.json()["auth"]["principal"]["id"]
            old_cookie = guest.cookies.get("am_session")
            stream = WatchingDevice(guest, sid, "guest-login-expiry")
            assert await stream.open() == 200
            await stream.next_message("state")
            await stream.wait_for(lambda: stream.ended, "guest登录到期关闭流", timeout_secs=6)
            assert (
                await guest.get(
                    f"/api/sessions/{sid}", headers={"Cookie": f"am_session={old_cookie}"}
                )
            ).status_code == 401
            # httpx不会自动发送已到期cookie；显式回放才验证服务端拒绝旧cookie并要求重试。
            assert (
                await guest.post(
                    "/api/shares/exchange",
                    json=link,
                    headers={"Origin": app.base_url, "Cookie": f"am_session={old_cookie}"},
                )
            ).status_code == 401
            assert not guest.cookies
            fresh = await redeem(guest, app, link)
            assert fresh.status_code == 200
            assert fresh.json()["auth"]["principal"]["id"] != before
            await sign_in(host)
            assert (await host.get(f"/api/sessions/{sid}")).json()["access"]["role"] == "host"
            with sqlite3.connect(app.data_dir / "meetings.db") as db:
                assert not db.execute("SELECT 1 FROM principals WHERE id = ?", (before,)).fetchone()
                assert db.execute("SELECT 1 FROM principals WHERE id = 'local:alpha'").fetchone()
        finally:
            if stream is not None:
                await stream.close()
            await recorder.close()


async def test_guest_exchange_storage_failure_rolls_back_and_does_not_log_credentials(shared_app):
    app = shared_app
    async with app.http() as host, app.http() as guest:
        await sign_in(host)
        recorder = MeetingClient(host)
        try:
            sid = (await recorder.connect())["id"]
            link = await make_link(host, sid)
            # 在真实SQLite边界注入写失败，验证principal/login之后的失败也整体回滚。
            with sqlite3.connect(app.data_dir / "meetings.db") as db:
                db.execute(
                    "CREATE TRIGGER s05_fail_guest BEFORE INSERT ON guest_logins BEGIN SELECT RAISE(ABORT, 'synthetic guest insert failure'); END"
                )
            # 未处理的SQLite故障在500发出后由Uvicorn关闭TCP；明确禁止复用这条连接。
            failed = await guest.post(
                "/api/shares/exchange",
                json=link,
                headers={"Origin": app.base_url, "Connection": "close"},
            )
            assert failed.status_code == 500 and not guest.cookies
            assert failed.headers["connection"] == "close"
            with sqlite3.connect(app.data_dir / "meetings.db") as db:
                assert not db.execute("SELECT 1 FROM principals WHERE id LIKE 'guest:%'").fetchone()
                assert not db.execute("SELECT 1 FROM guest_logins").fetchone()
                assert not db.execute("SELECT 1 FROM share_access").fetchone()
                db.execute("DROP TRIGGER s05_fail_guest")
            assert link["token"] not in app.log_text() and SITE not in app.log_text()
            assert link["token"] not in failed.text
            assert (await redeem(guest, app, link)).status_code == 200
            assert (await guest.get(f"/api/sessions/{sid}")).status_code == 200
        finally:
            await recorder.close()
