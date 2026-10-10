"""访问口令：没登录什么都看不到；登录、CSRF、限流、超大请求体；会议照常能开。"""

from __future__ import annotations

import pytest

from .meeting_client import MeetingClient

PASSWORD = "e2e-password-123"


@pytest.fixture
async def guarded(start_app):
    return await start_app(
        {"server.password_env": "E2E_ACCESS_PASSWORD"}, env={"E2E_ACCESS_PASSWORD": PASSWORD}
    )


async def test_everything_under_api_needs_a_login(guarded):
    async with guarded.http() as http:
        assert (await http.get("/api/auth")).json() == {
            "enabled": True,
            "authenticated": False,
            "csrf_token": None,
        }
        for path in ("/api/sessions", "/api/session", "/api/ice", "/api/utterances"):
            response = await http.get(path)
            assert response.status_code == 401, path
            assert response.json() == {"error": "请先登录"}
        offer = await http.post("/api/offer", json={"sdp": "x", "type": "offer"})
        assert offer.status_code == 401
        # 运维用的三个接口不需要登录，也不含会议内容
        for path in ("/healthz", "/readyz", "/metrics"):
            assert (await http.get(path)).status_code == 200, path


async def test_login_csrf_and_logout(guarded, inference):
    async with guarded.http() as http:
        wrong = await http.post("/api/auth/login", json={"password": "not-it"})
        assert wrong.status_code == 401
        login = await http.post("/api/auth/login", json={"password": PASSWORD})
        assert login.status_code == 200
        token = login.json()["csrf_token"]
        assert "httponly" in login.headers["set-cookie"].lower()

        assert (await http.get("/api/sessions")).status_code == 200
        # 改动类请求还要带 CSRF 令牌
        assert (await http.post("/api/session/end")).status_code == 403
        assert (
            await http.post("/api/session/end", headers={"X-CSRF-Token": "forged"})
        ).status_code == 403

        # 登录之后会议照常：页面把令牌随 offer 一起发
        http.headers["X-CSRF-Token"] = token
        client = MeetingClient(http)
        try:
            session = await client.connect()
            inference.asr.say("登录之后的发言")
            await client.speak()
            assert (await client.next_message("utterance"))["text"] == "登录之后的发言"
        finally:
            await client.close()
        ended = await http.post(f"/api/sessions/{session['id']}/end")
        assert ended.status_code == 200

        assert (await http.post("/api/auth/logout")).status_code == 200
        assert (await http.get("/api/sessions")).status_code == 401


async def test_repeated_wrong_passwords_are_rate_limited(guarded):
    async with guarded.http() as http:
        for _ in range(5):
            assert (
                await http.post("/api/auth/login", json={"password": "guess"})
            ).status_code == 401
        limited = await http.post("/api/auth/login", json={"password": PASSWORD})
        assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0


async def test_oversized_login_bodies_are_refused(guarded):
    async with guarded.http() as http:
        huge = await http.post(
            "/api/auth/login",
            content=b'{"password": "' + b"x" * (1024 * 1024) + b'"}',
            headers={"Content-Type": "application/json"},
        )
        assert huge.status_code == 413
        # 不算一次失败的登录
        assert (await http.post("/api/auth/login", json={"password": PASSWORD})).status_code == 200


async def test_without_a_password_the_api_is_open(app, http):
    assert (await http.get("/api/auth")).json()["enabled"] is False
    assert (await http.get("/api/sessions")).json() == {"items": []}
    assert (await http.post("/api/auth/login", json={"password": "x"})).status_code == 404
