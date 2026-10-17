"""单点登录：用机构账号（OpenID Connect）或反向代理的身份头登录，访问口令照常可用。

两个假身份提供方的风格不同（签名算法、客户端认证方式、用户组放在哪里），见 ``identity_provider.py``。
"""

from __future__ import annotations

import httpx

from .identity_provider import CLIENT_SECRET

PASSWORD = "e2e-password-123"
SECRET_ENV = {"E2E_OIDC_SECRET": CLIENT_SECRET, "E2E_ACCESS_PASSWORD": PASSWORD}


async def sign_in(http: httpx.AsyncClient, provider: str, next_path: str = "/") -> httpx.Response:
    """像浏览器一样走一遍：点按钮 → 跳到身份提供方 → 带着授权码跳回来。返回回调的响应。"""
    start = await http.get(f"/api/auth/oidc/{provider}/start", params={"next": next_path})
    if start.status_code != 302:
        return start
    async with httpx.AsyncClient(trust_env=False) as browser:
        at_provider = await browser.get(start.headers["location"])
    assert at_provider.status_code == 302, at_provider.text
    return await http.get(at_provider.headers["location"])


def login_error(response: httpx.Response) -> str | None:
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    return location.split("#login_error=", 1)[1] if "#login_error=" in location else None


async def test_sign_in_with_an_organization_account_next_to_the_password(start_app, identity):
    keys = identity.keys
    app = await start_app(
        {
            "server.password_env": "E2E_ACCESS_PASSWORD",
            "server.oidc": [identity.config(keys, allowed_groups=["lab"])],
        },
        env=SECRET_ENV,
    )
    async with app.http() as http:
        status = (await http.get("/api/auth")).json()
        assert status["authenticated"] is False
        assert status["methods"] == {
            "password": True,
            "oidc": [{"id": "keys", "display_name": "keys 账号"}],
            "proxy": False,
        }
        # 单点登录的入口和回调不需要登录，其他接口照旧要
        assert (await http.get("/api/sessions")).status_code == 401

        # 身份提供方离线：点按钮会回到登录页并说明原因，口令照常能登录
        keys.down = True
        offline = await http.get("/api/auth/oidc/keys/start")
        assert login_error(offline) == "sso.unavailable"
        assert (await http.post("/api/auth/login", json={"password": PASSWORD})).status_code == 200
        assert (await http.get("/api/sessions")).status_code == 200
        http.cookies.clear()
        keys.down = False

    async with app.http() as http:
        keys.user = {
            "sub": "u-1",
            "preferred_username": "alice",
            "name": "Alice",
            "groups": ["lab"],
        }
        done = await sign_in(http, "keys", "/?session=demo")
        assert login_error(done) is None
        assert done.headers["location"] == "/?session=demo"
        assert "am_session=" in done.headers.get("set-cookie", "")
        authorize = keys.requests[-1]["authorize"]
        assert authorize["redirect_uri"] == f"{app.base_url}/api/auth/oidc/keys/callback"
        status = (await http.get("/api/auth")).json()
        assert status["authenticated"] is True and status["csrf_token"]
        assert (await http.get("/api/sessions")).status_code == 200

        # 同一个回调地址只能用一次
        replay = await http.get(done.request.url)
        assert login_error(replay) == "sso.expired"

        # 已经登录的设备不受身份提供方离线影响
        keys.down = True
        assert (await http.get("/api/sessions")).status_code == 200
        keys.down = False

        # 退出登录照常需要令牌
        assert (await http.post("/api/auth/logout")).status_code == 403
        out = await http.post("/api/auth/logout", headers={"X-CSRF-Token": status["csrf_token"]})
        assert out.status_code == 200
        assert (await http.get("/api/sessions")).status_code == 401

    # 不在允许的用户组里、在身份提供方那边取消：都回到登录页，不发会话
    for user, deny, expected in (
        (
            {"sub": "u-2", "preferred_username": "mallory", "groups": ["guests"]},
            False,
            "sso.denied",
        ),
        ({"sub": "u-1", "preferred_username": "alice", "groups": ["lab"]}, True, "sso.cancelled"),
    ):
        keys.user, keys.deny = user, deny
        async with app.http() as http:
            refused = await sign_in(http, "keys")
            assert login_error(refused) == expected
            assert "am_session=" not in refused.headers.get("set-cookie", "")
            assert (await http.get("/api/sessions")).status_code == 401
    keys.deny = False

    log = app.log_text()
    assert "单点登录成功：alice（keys）" in log
    assert "mallory 不在允许登录的名单里" in log
    assert CLIENT_SECRET not in log


async def test_another_provider_style_and_tokens_that_must_be_refused(start_app, identity):
    keys, portal = identity.keys, identity.portal
    app = await start_app(
        {
            "server.oidc": [
                identity.config(portal, username_claim="email", allowed_users=["bob@example.org"]),
                identity.config(keys),
            ]
        },
        env=SECRET_ENV,
    )
    async with app.http() as http:
        status = (await http.get("/api/auth")).json()
        assert status["enabled"] is True
        assert status["methods"]["password"] is False
        assert [p["id"] for p in status["methods"]["oidc"]] == ["portal", "keys"]
        no_password = await http.post("/api/auth/login", json={"password": "anything"})
        assert no_password.status_code == 404 and no_password.json()["code"] == "auth.not_enabled"
        unknown = await http.get("/api/auth/oidc/nobody/start")
        assert unknown.status_code == 404
        assert unknown.json()["code"] == "auth.provider_not_found"

        # 这个提供方的 ID 令牌里没有 email，应用要去 userinfo 补全才知道是 bob
        portal.user = {"sub": "p-7", "email": "bob@example.org", "name": "Bob", "groups": ["staff"]}
        done = await sign_in(http, "portal", "//elsewhere.example/steal")
        assert login_error(done) is None
        assert done.headers["location"] == "/"  # 不跳到别的网站
        assert (await http.get("/api/sessions")).status_code == 200

    # 签名、受众、nonce、有效期不对，或者干脆没签名的 ID 令牌：一律拒收
    keys.user = {"sub": "u-1", "preferred_username": "alice"}
    for tamper in ("audience", "nonce", "expired", "signature", "unsigned"):
        keys.tamper = tamper
        async with app.http() as http:
            refused = await sign_in(http, "keys")
            assert login_error(refused) == "sso.failed", tamper
            assert (await http.get("/api/sessions")).status_code == 401, tamper

    # 换了个浏览器去完成别人发起的登录（没有发起时的绑定 Cookie）：不算数
    async with app.http() as victim, app.http() as attacker:
        start = await attacker.get("/api/auth/oidc/keys/start")
        async with httpx.AsyncClient(trust_env=False) as browser:
            back = await browser.get(start.headers["location"])
        forced = await victim.get(back.headers["location"])
        assert login_error(forced) == "sso.expired"
        assert (await victim.get("/api/sessions")).status_code == 401

    log = app.log_text()
    assert "单点登录成功：bob@example.org（portal）" in log
    assert CLIENT_SECRET not in log


async def test_identity_headers_count_only_from_the_trusted_proxy(start_app, tmp_path):
    app = await start_app(
        {
            "server.proxy_auth.trusted_proxies": ["127.0.0.1"],
            "server.proxy_auth.name_header": "X-Forwarded-Name",
            "server.proxy_auth.groups_header": "X-Forwarded-Groups",
            "server.proxy_auth.allowed_groups": ["lab"],
        }
    )
    # 代理一般会带上 X-Forwarded-For：应用要看的是谁直接连过来，而不是这个头里写的地址
    proxied = {"X-Forwarded-For": "198.51.100.7", "X-Forwarded-User": "carol"}
    async with app.http() as http:
        anonymous = (await http.get("/api/auth")).json()
        assert anonymous["authenticated"] is False
        assert anonymous["methods"] == {"password": False, "oidc": [], "proxy": True}
        assert (await http.get("/api/sessions")).status_code == 401

        member = {**proxied, "X-Forwarded-Groups": "x, lab", "X-Forwarded-Name": "Carol"}
        status = (await http.get("/api/auth", headers=member)).json()
        assert status["authenticated"] is True and status["csrf_token"]
        assert "set-cookie" not in (await http.get("/api/auth", headers=member)).headers
        assert (await http.get("/api/sessions", headers=member)).status_code == 200
        # 改动类请求照样要令牌
        assert (await http.post("/api/auth/logout", headers=member)).status_code == 403
        with_token = {**member, "X-CSRF-Token": status["csrf_token"]}
        assert (await http.post("/api/auth/logout", headers=with_token)).status_code == 200

        # 不在允许的组里
        outsider = {**proxied, "X-Forwarded-Groups": "guests"}
        assert (await http.get("/api/sessions", headers=outsider)).status_code == 401

    # 代理不在受信任地址里：同样的身份头一律不认
    other = await start_app(
        {"server.proxy_auth.trusted_proxies": ["192.0.2.10"]}, data_dir=tmp_path / "other"
    )
    async with other.http() as http:
        headers = {"X-Forwarded-User": "carol"}
        assert (await http.get("/api/auth", headers=headers)).json()["authenticated"] is False
        assert (await http.get("/api/sessions", headers=headers)).status_code == 401
