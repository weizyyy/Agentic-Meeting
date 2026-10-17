"""假身份提供方：在测试进程里起两个本机 OpenID Connect 服务，代替机构的登录系统。

两个的风格不同，覆盖常见提供方之间的差别：

* ``keys``：RS256 签名，客户端用 HTTP Basic 认证，用户组直接写在 ID 令牌里。
* ``portal``：ES256 签名，只接受表单里的客户端密钥，用户名用 ``email`` 声明，用户组只在 userinfo 里给。

没有真的登录页面：``authorize`` 直接按测试安排好的 ``user`` 签发授权码并跳回应用（``deny`` 时回
``error=access_denied``）。换令牌时严格检查 PKCE、回调地址和客户端密钥。``tamper`` 让下一个 ID 令牌出一种错，
用来验证应用会拒收。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from .inference import FakeService

CLIENT_ID = "agentic-meeting-e2e"
CLIENT_SECRET = "e2e-client-secret-0123456789"


@dataclass
class Grant:
    client_id: str
    redirect_uri: str
    challenge: str
    nonce: str
    user: dict[str, Any]


@dataclass
class Style:
    algorithm: str
    auth_method: str  # client_secret_basic / client_secret_post
    groups_in_id_token: bool


class FakeIdentityProvider(FakeService):
    def __init__(self, name: str, style: Style) -> None:
        super().__init__(name)
        self.style = style
        if style.algorithm == "RS256":
            self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            self._other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            self._algo = jwt.algorithms.RSAAlgorithm
        else:
            self._key = ec.generate_private_key(ec.SECP256R1())
            self._other = ec.generate_private_key(ec.SECP256R1())
            self._algo = jwt.algorithms.ECAlgorithm
        self.kid = secrets.token_hex(4)
        # 测试安排：下一个登录的人、是否拒绝、ID 令牌出什么错
        self.user: dict[str, Any] = {}
        self.deny = False
        self.tamper = ""
        self.grants: dict[str, Grant] = {}
        self.tokens: dict[str, dict[str, Any]] = {}
        self.token_requests = 0
        app = self.app

        @app.get("/realm/.well-known/openid-configuration")
        async def discovery() -> dict[str, Any]:
            return {
                "issuer": self.issuer,
                "authorization_endpoint": f"{self.origin}/authorize",
                "token_endpoint": f"{self.origin}/token",
                "jwks_uri": f"{self.origin}/jwks",
                "userinfo_endpoint": f"{self.origin}/userinfo",
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": [style.algorithm],
                "token_endpoint_auth_methods_supported": [style.auth_method],
                "code_challenge_methods_supported": ["S256"],
            }

        @app.get("/jwks")
        async def jwks() -> dict[str, Any]:
            public = json.loads(self._algo.to_jwk(self._key.public_key()))
            return {"keys": [{**public, "kid": self.kid, "use": "sig", "alg": style.algorithm}]}

        @app.get("/authorize")
        async def authorize(request: Request) -> Response:
            q = request.query_params
            self.record({"authorize": dict(q)})
            assert q["response_type"] == "code" and q["client_id"] == CLIENT_ID
            assert q["code_challenge_method"] == "S256" and "openid" in q["scope"].split()
            if self.deny:
                query = {"error": "access_denied", "state": q["state"]}
            else:
                code = secrets.token_urlsafe(16)
                self.grants[code] = Grant(
                    q["client_id"], q["redirect_uri"], q["code_challenge"], q["nonce"], self.user
                )
                query = {"code": code, "state": q["state"]}
            return RedirectResponse(f"{q['redirect_uri']}?{urlencode(query)}", status_code=302)

        @app.post("/token")
        async def token(request: Request) -> Response:
            self.token_requests += 1
            form = await request.form()
            if not self._client_ok(request, form):
                return JSONResponse({"error": "invalid_client"}, status_code=401)
            grant = self.grants.pop(str(form.get("code", "")), None)
            verifier = str(form.get("code_verifier", ""))
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
            )
            if (
                grant is None
                or form.get("grant_type") != "authorization_code"
                or form.get("redirect_uri") != grant.redirect_uri
                or challenge != grant.challenge
            ):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            access = secrets.token_urlsafe(16)
            self.tokens[access] = grant.user
            return JSONResponse(
                {
                    "access_token": access,
                    "token_type": "Bearer",
                    "id_token": self._id_token(grant),
                }
            )

        @app.get("/userinfo")
        async def userinfo(request: Request) -> Response:
            auth = request.headers.get("authorization", "")
            user = self.tokens.get(auth.removeprefix("Bearer "))
            if user is None:
                return JSONResponse({"error": "invalid_token"}, status_code=401)
            return JSONResponse(user)

    @property
    def issuer(self) -> str:
        return f"{self.origin}/realm"

    def _client_ok(self, request: Request, form: Any) -> bool:
        if self.style.auth_method == "client_secret_basic":
            expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            return request.headers.get("authorization") == f"Basic {expected}"
        return form.get("client_id") == CLIENT_ID and form.get("client_secret") == CLIENT_SECRET

    def _id_token(self, grant: Grant) -> str:
        now = int(time.time())
        user = dict(grant.user)
        if not self.style.groups_in_id_token:
            user.pop("groups", None)
            user.pop("email", None)
        claims = {
            **user,
            "iss": self.issuer,
            "aud": grant.client_id,
            "iat": now,
            "exp": now + 300,
            "nonce": grant.nonce,
        }
        key = self._key
        tamper, self.tamper = self.tamper, ""
        if tamper == "audience":
            claims["aud"] = "someone-else"
        elif tamper == "nonce":
            claims["nonce"] = "replayed"
        elif tamper == "expired":
            claims["iat"], claims["exp"] = now - 3600, now - 1800
        elif tamper == "signature":
            key = self._other
        if tamper == "unsigned":
            return jwt.encode(claims, None, algorithm="none")
        return jwt.encode(claims, key, algorithm=self.style.algorithm, headers={"kid": self.kid})

    def reset(self) -> None:
        self.down = False
        self.user, self.deny, self.tamper = {}, False, ""
        self.grants.clear()
        self.tokens.clear()
        self.token_requests = 0
        with self._lock:
            self.requests.clear()


@dataclass
class IdentityProviders:
    """两个假身份提供方，跑在后台线程自己的事件循环里。"""

    keys: FakeIdentityProvider = field(
        default_factory=lambda: FakeIdentityProvider(
            "keys", Style("RS256", "client_secret_basic", groups_in_id_token=True)
        )
    )
    portal: FakeIdentityProvider = field(
        default_factory=lambda: FakeIdentityProvider(
            "portal", Style("ES256", "client_secret_post", groups_in_id_token=False)
        )
    )

    def __post_init__(self) -> None:
        self._servers: list[uvicorn.Server] = []
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()
        for provider in (self.keys, self.portal):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            provider.port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(provider.app, log_level="warning"))
            self._servers.append(server)
            asyncio.run_coroutine_threadsafe(server.serve(sockets=[sock]), self._loop)
        deadline = time.monotonic() + 10
        while not all(server.started for server in self._servers):
            if time.monotonic() > deadline:
                raise RuntimeError("假身份提供方没有启动")
            time.sleep(0.01)

    def stop(self) -> None:
        for server in self._servers:
            server.should_exit = True
        deadline = time.monotonic() + 5
        while any(server.started for server in self._servers) and time.monotonic() < deadline:
            time.sleep(0.01)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def reset(self) -> None:
        self.keys.reset()
        self.portal.reset()

    def config(self, provider: FakeIdentityProvider, **extra: Any) -> dict[str, Any]:
        """应用配置里 ``server.oidc`` 的一项。"""
        return {
            "id": provider.name,
            "display_name": f"{provider.name} 账号",
            "issuer": provider.issuer,
            "client_id": CLIENT_ID,
            "client_secret_env": "E2E_OIDC_SECRET",
            **extra,
        }
