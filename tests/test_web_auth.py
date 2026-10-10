"""访问口令（web/auth.py，docs/interfaces.md §5.7）：登录、会话 Cookie、CSRF、登录限速。

用 ``httpx.ASGITransport`` 直接调应用；口令放在虚构的环境变量里，会话密钥写在临时数据目录下。
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import httpx
import pytest

from agentic_meeting.web import auth
from agentic_meeting.web.app import create_app

PASSWORD_ENV = "FAKE_MEETING_PASSWORD"
PASSWORD = "correct horse battery"


def client_for(app, base_url: str = "http://test") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url)


@pytest.fixture
def cfg(make_cfg, monkeypatch):
    monkeypatch.setenv(PASSWORD_ENV, PASSWORD)
    cfg = make_cfg()
    cfg.server.password_env = PASSWORD_ENV
    return cfg


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    root.mkdir()
    (root / "index.html").write_text("<!doctype html><title>组会助理</title>", "utf-8")
    return root


async def login(client: httpx.AsyncClient, password: str = PASSWORD) -> httpx.Response:
    return await client.post("/api/auth/login", json={"password": password})


# --------------------------------------------------------------------------- #
# 未启用
# --------------------------------------------------------------------------- #


async def test_without_a_password_everything_stays_open(make_cfg, tmp_path):
    cfg = make_cfg()
    app = create_app(cfg, static_dir=tmp_path / "nope")
    async with client_for(app) as client:
        status = await client.get("/api/auth")
        time_ = await client.get("/api/time")
        login_ = await login(client)
    assert status.json() == {"enabled": False, "authenticated": True, "csrf_token": None}
    assert time_.status_code == 200
    assert login_.status_code == 404 and login_.json()["error"]
    # 没启用就不生成会话密钥
    assert not (cfg.resolve(cfg.session.data_dir) / auth.SECRET_FILE).exists()


# --------------------------------------------------------------------------- #
# 启用之后
# --------------------------------------------------------------------------- #


async def test_api_requires_login_but_the_static_site_does_not(cfg, dist):
    app = create_app(cfg, static_dir=dist)
    async with client_for(app) as client:
        status = await client.get("/api/auth")
        guarded = [
            await client.get("/api/time"),
            await client.get("/api/sessions"),
            await client.get("/api/frames/1/image"),
            await client.get("/api/export/abc.zip"),
            await client.get("/api/tasks/abc.t1/artifacts/x.png"),
            await client.post("/api/offer", json={"sdp": "s", "type": "offer"}),
            await client.patch("/api/offer", json={"pc_id": "p", "candidates": []}),
            await client.delete("/api/sessions/abc"),
            await client.get("/api/no-such-endpoint"),
        ]
        page = await client.get("/")
    assert status.json() == {"enabled": True, "authenticated": False, "csrf_token": None}
    for response in guarded:
        assert response.status_code == 401, response.request.url
        assert response.json() == {"error": auth.NEED_LOGIN}
    assert page.status_code == 200 and "组会助理" in page.text


async def test_login_sets_a_strict_http_only_cookie_and_unlocks_the_api(cfg, tmp_path):
    app = create_app(cfg, static_dir=tmp_path / "nope")
    async with client_for(app) as client:
        response = await login(client)
        status = await client.get("/api/auth")
        time_ = await client.get("/api/time")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] and body["authenticated"] and body["csrf_token"]
    assert status.json() == body  # 刷新页面后从状态接口取回同一个令牌
    assert time_.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{auth.COOKIE_NAME}=")
    lowered = cookie.lower()
    assert "httponly" in lowered and "samesite=strict" in lowered and "path=/" in lowered
    assert f"max-age={7 * 86400}" in lowered
    assert "secure" not in lowered  # 纯 HTTP 下浏览器不会回传 Secure Cookie
    assert PASSWORD not in cookie


async def test_cookie_is_secure_over_https(cfg, tmp_path):
    app = create_app(cfg, static_dir=tmp_path / "nope")
    async with client_for(app, "https://test") as client:
        response = await login(client)
    assert "secure" in response.headers["set-cookie"].lower()


async def test_wrong_password_is_refused(cfg, tmp_path):
    app = create_app(cfg, static_dir=tmp_path / "nope")
    async with client_for(app) as client:
        wrong = await login(client, "not the password")
        after = await client.get("/api/time")
        malformed = await client.post("/api/auth/login", json={"pw": PASSWORD})
    assert wrong.status_code == 401 and wrong.json() == {"error": auth.WRONG_PASSWORD}
    assert "set-cookie" not in wrong.headers
    assert after.status_code == 401
    assert malformed.status_code == 400


async def test_state_changing_requests_need_the_csrf_token(cfg, tmp_path):
    app = create_app(cfg, static_dir=tmp_path / "nope")
    async with client_for(app) as client:
        token = (await login(client)).json()["csrf_token"]
        missing = await client.patch("/api/sessions/abc", json={"title": "x"})
        wrong = await client.patch(
            "/api/sessions/abc", json={"title": "x"}, headers={auth.CSRF_HEADER: "nope"}
        )
        ok_get = await client.get("/api/time")  # 读取不需要令牌
        with_token = await client.post("/api/auth/logout", headers={auth.CSRF_HEADER: token})
    assert missing.status_code == 403 and missing.json() == {"error": auth.BAD_CSRF}
    assert wrong.status_code == 403
    assert ok_get.status_code == 200
    assert with_token.status_code == 200


async def test_offer_goes_through_with_cookie_and_token(cfg, tmp_path):
    """WebRTC 信令也在保护范围内：带上 Cookie 和令牌才交给处理器。"""

    class Handler:
        def __init__(self):
            self.requests = []
            self.patches = []

        async def handle_web_request(self, request, webrtc_connection_callback):
            self.requests.append(request)
            return {"sdp": "answer", "type": "answer", "pc_id": "pc-1"}

        async def handle_patch_request(self, request):
            self.patches.append(request)

        async def close(self):
            pass

    async def bot(*_args):
        return None

    handler = Handler()
    app = create_app(cfg, handler=handler, bot=bot, static_dir=tmp_path / "nope")
    offer = {"sdp": "s", "type": "offer", "requestData": {}}
    patch = {"pc_id": "pc-1", "candidates": []}
    async with app.router.lifespan_context(app), client_for(app) as client:
        token = (await login(client)).json()["csrf_token"]
        refused = await client.post("/api/offer", json=offer)
        accepted = await client.post("/api/offer", json=offer, headers={auth.CSRF_HEADER: token})
        patched = await client.patch("/api/offer", json=patch, headers={auth.CSRF_HEADER: token})
    assert refused.status_code == 403
    assert accepted.status_code == 200 and len(handler.requests) == 1
    assert patched.status_code == 200 and len(handler.patches) == 1


async def test_logout_clears_the_cookie(cfg, tmp_path):
    app = create_app(cfg, static_dir=tmp_path / "nope")
    async with client_for(app) as client:
        token = (await login(client)).json()["csrf_token"]
        response = await client.post("/api/auth/logout", headers={auth.CSRF_HEADER: token})
        after = await client.get("/api/time")
    assert response.json() == {"enabled": True, "authenticated": False, "csrf_token": None}
    assert 'am_session=""' in response.headers["set-cookie"]
    assert after.status_code == 401


async def test_changing_the_password_logs_everyone_out(cfg, tmp_path, monkeypatch):
    app = create_app(cfg, static_dir=tmp_path / "nope")
    async with client_for(app) as client:
        await login(client)
        cookie = client.cookies[auth.COOKIE_NAME]
    # 同一个数据目录（同一个会话密钥）：重启后旧 Cookie 仍然有效
    same = create_app(cfg, static_dir=tmp_path / "nope")
    async with client_for(same) as client:
        client.cookies.set(auth.COOKIE_NAME, cookie)
        assert (await client.get("/api/time")).status_code == 200
    monkeypatch.setenv(PASSWORD_ENV, "a different password")
    changed = create_app(cfg, static_dir=tmp_path / "nope")
    async with client_for(changed) as client:
        client.cookies.set(auth.COOKIE_NAME, cookie)
        assert (await client.get("/api/time")).status_code == 401


async def test_repeated_failures_are_rate_limited(cfg, tmp_path):
    app = create_app(cfg, static_dir=tmp_path / "nope")
    async with client_for(app) as client:
        failures = [await login(client, f"guess {i}") for i in range(auth.MAX_FAILURES)]
        blocked = await login(client)  # 口令对了也要等
    assert all(r.status_code == 401 for r in failures)
    assert blocked.status_code == 429 and blocked.json() == {"error": auth.TOO_MANY}
    assert 0 < int(blocked.headers["retry-after"]) <= auth.FAILURE_WINDOW_SECS + 1


def test_missing_password_variable_refuses_to_start(cfg, monkeypatch, tmp_path):
    monkeypatch.delenv(PASSWORD_ENV)
    with pytest.raises(RuntimeError, match=PASSWORD_ENV):
        create_app(cfg, static_dir=tmp_path / "nope")


# --------------------------------------------------------------------------- #
# 组成部分
# --------------------------------------------------------------------------- #


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_guard(clock: Clock, password: str = PASSWORD) -> auth.AuthGuard:
    return auth.AuthGuard(password, b"s" * 32, session_secs=60.0, clock=clock)


def test_session_cookie_expires_and_rejects_tampering():
    clock = Clock()
    guard = make_guard(clock)
    cookie, csrf = guard.issue()
    nonce = guard.verify(cookie)
    assert nonce is not None and guard.csrf_ok(nonce, csrf)
    expires, nonce_text, signature = cookie.split(".")
    for bad in (
        f"{int(expires) + 3600}.{nonce_text}.{signature}",  # 改了到期时刻
        f"{expires}.other.{signature}",
        f"{expires}.{nonce_text}.{signature[:-2]}AA",
        f"{expires}.{nonce_text}",
        f"²{expires}.{nonce_text}.{signature}",
        "",
        None,
        "a.b.c.d",
        "1.是.x",
    ):
        assert guard.verify(bad) is None, bad
    assert not guard.csrf_ok(nonce, None) and not guard.csrf_ok(nonce, "x")
    # 别的会话的令牌不能拿来用
    other_cookie, other_csrf = guard.issue()
    assert not guard.csrf_ok(nonce, other_csrf)
    clock.now += 61
    assert guard.verify(cookie) is None
    # 不同口令签出的 Cookie 互不相认
    assert make_guard(Clock(), "another password").verify(other_cookie) is None


def test_limiter_window_slides_and_success_resets():
    clock = Clock(0.0)
    limiter = auth.LoginLimiter(3, 100.0, clock=clock)
    for _ in range(3):
        assert limiter.retry_after("a") == 0
        limiter.failed("a")
        clock.now += 10
    assert limiter.retry_after("a") == pytest.approx(70.0)
    assert limiter.retry_after("b") == 0  # 按地址分开计
    clock.now = 101.0  # 第一次失败移出窗口
    assert limiter.retry_after("a") == 0
    limiter.failed("a")
    assert limiter.retry_after("a") > 0
    limiter.succeeded("a")
    assert limiter.retry_after("a") == 0


def test_limiter_forgets_the_stalest_address_when_full():
    clock = Clock(0.0)
    limiter = auth.LoginLimiter(1, 100.0, clock=clock, max_addresses=2)
    limiter.failed("a")
    clock.now += 1
    limiter.failed("b")
    limiter.failed("c")
    assert limiter.retry_after("a") == 0
    assert limiter.retry_after("b") > 0 and limiter.retry_after("c") > 0


def test_secret_file_is_created_once_and_private(tmp_path):
    first = auth.load_or_create_secret(tmp_path / "data")
    again = auth.load_or_create_secret(tmp_path / "data")
    assert first == again and len(first) == auth.SECRET_BYTES
    path = tmp_path / "data" / auth.SECRET_FILE
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    path.write_text("not hex", "ascii")
    with pytest.raises(RuntimeError, match="auth_secret"):
        auth.load_or_create_secret(tmp_path / "data")
