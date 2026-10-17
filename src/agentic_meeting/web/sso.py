"""单点登录：OpenID Connect 登录与反向代理传来的身份头（docs/interfaces.md §5.7）。

两种方式都只负责「认出这个人」，认出来之后和访问口令一样签发 ``auth.py`` 的会话 Cookie（反向代理那种
不发 Cookie，每个请求都看身份头），其余接口不知道设备是怎么登录的。访问口令始终可以并用：身份提供方
离线时，口令照常能登录，已经登录的设备也不受影响（只有登录那一下要连身份提供方）。

OpenID Connect 走授权码流程 + PKCE（S256）+ nonce。登录进行中的状态只放在内存里：重启服务会让正在
跳转中的登录失败，用户重新点一次即可。ID 令牌只接受非对称签名，签名、issuer、受众、有效期和 nonce
全部校验通过才算数；令牌和声明都不落盘。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from loguru import logger
from starlette.types import ASGIApp, Receive, Scope, Send

from agentic_meeting.config import (
    AppConfig,
    OidcProviderConfig,
    ProxyAuthConfig,
    is_loopback_host,
    secret,
)
from agentic_meeting.web.errors import ApiError

# 记录 TCP 直连对端的 scope 键：uvicorn 按 X-Forwarded-For 改写 client 之前的地址（见 record_peer）
PEER_SCOPE_KEY = "agentic_meeting.peer"

BINDING_COOKIE = "am_sso"
BINDING_COOKIE_PATH = "/api/auth/oidc/"
PENDING_SECS = 600.0
MAX_PENDING = 1000
METADATA_SECS = 3600.0
JWKS_REFRESH_SECS = 30.0
HTTP_TIMEOUT_SECS = 10.0
CLOCK_LEEWAY_SECS = 60
MAX_NEXT_CHARS = 2000
MAX_HEADER_VALUE_CHARS = 256
# 只接受非对称签名：HMAC 用的是客户端密钥，公开客户端没有；"none" 更不行
ASYMMETRIC_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}
)


@dataclass(frozen=True)
class ExternalIdentity:
    """单点登录认出来的人。``source`` 是 ``oidc:<id>`` 或 ``proxy``；``subject`` 在来源内稳定不变。"""

    source: str
    subject: str
    username: str
    display_name: str
    groups: tuple[str, ...]


class LoginFailed(Exception):
    """登录没有完成。``code`` 是给页面看的 ``sso.*`` 代码，``detail`` 只进日志。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def allowed(identity: ExternalIdentity, users: list[str], groups: list[str]) -> bool:
    """两张名单都空 = 都允许；否则用户名在前者里，或者属于后者中的某个组。"""
    if not users and not groups:
        return True
    return identity.username in users or any(group in groups for group in identity.groups)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe_next(value: str | None) -> str:
    """登录后回到的页面：只能是本站的路径，否则回首页（防止被当成开放跳转）。# 片段去掉，
    失败原因要放在那里。"""
    value = value.split("#", 1)[0] if value else value
    if (
        not value
        or len(value) > MAX_NEXT_CHARS
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)
    ):
        return "/"
    return value


def _string_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    return ()


# --------------------------------------------------------------------------- #
# OpenID Connect
# --------------------------------------------------------------------------- #


@dataclass
class PendingLogin:
    provider: str
    verifier: str
    nonce: str
    redirect_uri: str
    binding: str  # 浏览器那份 am_sso Cookie 的 SHA-256
    next: str
    created: float


class OidcProvider:
    """一个身份提供方：发现文档和签名公钥的缓存、授权跳转、换令牌和校验。``clock`` 可注入（测试用）。"""

    def __init__(
        self,
        cfg: OidcProviderConfig,
        client_secret: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.client_secret = client_secret
        self._clock = clock
        self._metadata: dict[str, Any] | None = None
        self._metadata_at = 0.0
        self._jwks: list[dict[str, Any]] = []
        self._jwks_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def source(self) -> str:
        return f"oidc:{self.cfg.id}"

    def _client(self) -> httpx.AsyncClient:
        # 每次登录新建一个：登录很少，不值得长期占着连接；读环境里的代理设置（机构网络常要）
        return httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECS, follow_redirects=False)

    def _check_endpoint(self, metadata: dict[str, Any], key: str, *, required: bool) -> None:
        url = metadata.get(key)
        if url is None and not required:
            return
        if not isinstance(url, str):
            raise LoginFailed("sso.failed", f"发现文档缺少 {key}")
        parsed = urlparse(url)
        loopback_issuer = is_loopback_host(urlparse(self.cfg.issuer).hostname or "")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback_issuer):
            raise LoginFailed("sso.failed", f"发现文档里的 {key} 不是 https 地址：{url}")

    async def metadata(self) -> dict[str, Any]:
        async with self._lock:
            if self._metadata is not None and self._clock() - self._metadata_at < METADATA_SECS:
                return self._metadata
            url = self.cfg.issuer.rstrip("/") + "/.well-known/openid-configuration"
            data = await self._get_json(url, "发现文档")
            if data.get("issuer") != self.cfg.issuer:
                raise LoginFailed(
                    "sso.failed",
                    f"发现文档的 issuer {data.get('issuer')!r} 与配置的 {self.cfg.issuer!r} 不一致",
                )
            for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                self._check_endpoint(data, key, required=True)
            self._check_endpoint(data, "userinfo_endpoint", required=False)
            self._metadata, self._metadata_at = data, self._clock()
            return data

    async def _get_json(self, url: str, what: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with self._client() as client:
                response = await client.get(url, **kwargs)
        except httpx.HTTPError as e:
            raise LoginFailed("sso.unavailable", f"读取{what}失败（{url}）：{e!r}") from e
        if response.status_code >= 500:
            raise LoginFailed("sso.unavailable", f"{what}返回 {response.status_code}（{url}）")
        if response.status_code != 200:
            raise LoginFailed("sso.failed", f"{what}返回 {response.status_code}（{url}）")
        try:
            data = response.json()
        except ValueError as e:
            raise LoginFailed("sso.failed", f"{what}不是 JSON（{url}）") from e
        if not isinstance(data, dict):
            raise LoginFailed("sso.failed", f"{what}不是 JSON 对象（{url}）")
        return data

    async def authorization_url(
        self, redirect_uri: str, next_path: str
    ) -> tuple[str, str, str, PendingLogin]:
        """返回 (跳转地址, state, 浏览器绑定值, 待完成的登录)。"""
        metadata = await self.metadata()
        state = secrets.token_urlsafe(32)
        binding = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        nonce = secrets.token_urlsafe(24)
        challenge = _b64(hashlib.sha256(verifier.encode("ascii")).digest())
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.cfg.client_id,
                "redirect_uri": redirect_uri,
                "scope": " ".join(self.cfg.scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        endpoint = metadata["authorization_endpoint"]
        url = f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"
        pending = PendingLogin(
            provider=self.cfg.id,
            verifier=verifier,
            nonce=nonce,
            redirect_uri=redirect_uri,
            binding=_digest(binding),
            next=next_path,
            created=self._clock(),
        )
        return url, state, binding, pending

    async def finish(self, pending: PendingLogin, code: str) -> ExternalIdentity:
        """用授权码换令牌，校验 ID 令牌，必要时从 userinfo 补全声明。"""
        metadata = await self.metadata()
        tokens = await self._exchange(metadata, pending, code)
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str):
            raise LoginFailed("sso.failed", "令牌响应里没有 id_token")
        claims = await self._verify(metadata, id_token, pending.nonce)
        access_token = tokens.get("access_token")
        missing = self.cfg.username_claim not in claims or self.cfg.groups_claim not in claims
        if missing and isinstance(access_token, str) and metadata.get("userinfo_endpoint"):
            info = await self._get_json(
                metadata["userinfo_endpoint"],
                "userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if info.get("sub") != claims["sub"]:
                raise LoginFailed("sso.failed", "userinfo 的 sub 与 ID 令牌不一致")
            claims = {**info, **claims}
        subject = str(claims["sub"])
        username = claims.get(self.cfg.username_claim)
        if not isinstance(username, str) or not username:
            username = subject
        name = claims.get("name")
        return ExternalIdentity(
            source=self.source,
            subject=subject,
            username=username,
            display_name=name if isinstance(name, str) and name else username,
            groups=_string_list(claims.get(self.cfg.groups_claim)),
        )

    async def _exchange(
        self, metadata: dict[str, Any], pending: PendingLogin, code: str
    ) -> dict[str, Any]:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": pending.redirect_uri,
            "code_verifier": pending.verifier,
        }
        auth: httpx.BasicAuth | None = None
        if self.client_secret:
            methods = metadata.get("token_endpoint_auth_methods_supported")
            post_only = (
                isinstance(methods, list)
                and "client_secret_post" in methods
                and "client_secret_basic" not in methods
            )
            if post_only:
                form.update(client_id=self.cfg.client_id, client_secret=self.client_secret)
            else:
                auth = httpx.BasicAuth(self.cfg.client_id, self.client_secret)
        else:
            form["client_id"] = self.cfg.client_id
        url = metadata["token_endpoint"]
        try:
            async with self._client() as client:
                response = await client.post(
                    url, data=form, auth=auth, headers={"Accept": "application/json"}
                )
        except httpx.HTTPError as e:
            raise LoginFailed("sso.unavailable", f"换取令牌失败（{url}）：{e!r}") from e
        if response.status_code >= 500:
            raise LoginFailed("sso.unavailable", f"令牌接口返回 {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code != 200 or not isinstance(data, dict):
            error = data.get("error") if isinstance(data, dict) else None
            raise LoginFailed("sso.failed", f"令牌接口返回 {response.status_code}：{error!r}")
        return data

    async def _signing_key(self, metadata: dict[str, Any], kid: str | None, alg: str) -> Any:
        for refresh in (False, True):
            if refresh or not self._jwks:
                if self._jwks and self._clock() - self._jwks_at < JWKS_REFRESH_SECS:
                    break  # 刚取过：未知的 kid 不让人反复打身份提供方
                data = await self._get_json(metadata["jwks_uri"], "签名公钥")
                keys = data.get("keys")
                self._jwks = (
                    [k for k in keys if isinstance(k, dict)] if isinstance(keys, list) else []
                )
                self._jwks_at = self._clock()
            for raw in self._jwks:
                if raw.get("use", "sig") != "sig" or (kid is not None and raw.get("kid") != kid):
                    continue
                if "alg" in raw and raw["alg"] != alg:
                    continue
                try:
                    return jwt.PyJWK(raw, algorithm=alg).key
                except jwt.PyJWKError:
                    continue
        raise LoginFailed("sso.failed", f"找不到签名公钥（kid={kid!r}，alg={alg}）")

    async def _verify(self, metadata: dict[str, Any], token: str, nonce: str) -> dict[str, Any]:
        supported = metadata.get("id_token_signing_alg_values_supported") or ["RS256"]
        algorithms = ASYMMETRIC_ALGORITHMS.intersection(
            supported if isinstance(supported, list) else []
        )
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as e:
            raise LoginFailed("sso.failed", f"ID 令牌格式不对：{e}") from e
        alg = header.get("alg")
        if alg not in algorithms:
            raise LoginFailed("sso.failed", f"ID 令牌的签名算法 {alg!r} 不被接受")
        kid = header.get("kid")
        key = await self._signing_key(metadata, kid if isinstance(kid, str) else None, alg)
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[alg],
                audience=self.cfg.client_id,
                issuer=self.cfg.issuer,
                leeway=CLOCK_LEEWAY_SECS,
                options={"require": ["iss", "aud", "exp", "iat", "sub"]},
            )
        except jwt.InvalidTokenError as e:
            raise LoginFailed("sso.failed", f"ID 令牌校验失败：{e}") from e
        audiences = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        azp = claims.get("azp")
        if (len(audiences) > 1 or azp is not None) and azp != self.cfg.client_id:
            raise LoginFailed("sso.failed", f"ID 令牌的 azp {azp!r} 不是本客户端")
        if not isinstance(claims.get("nonce"), str) or not hmac.compare_digest(
            claims["nonce"], nonce
        ):
            raise LoginFailed("sso.failed", "ID 令牌的 nonce 不对")
        if not isinstance(claims["sub"], str) or not claims["sub"]:
            raise LoginFailed("sso.failed", "ID 令牌的 sub 无效")
        return claims


class PendingLogins:
    """跳转中的登录：state → 待完成的登录。有上限、有有效期，每个 state 只能用一次。"""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._items: OrderedDict[str, PendingLogin] = OrderedDict()

    def _expire(self) -> None:
        cutoff = self._clock() - PENDING_SECS
        while self._items:
            state, pending = next(iter(self._items.items()))
            if pending.created > cutoff:
                break
            del self._items[state]

    def add(self, state: str, pending: PendingLogin) -> None:
        self._expire()
        while len(self._items) >= MAX_PENDING:
            self._items.popitem(last=False)
        self._items[state] = pending

    def take(self, state: str) -> PendingLogin | None:
        self._expire()
        return self._items.pop(state, None)


# --------------------------------------------------------------------------- #
# 反向代理的身份头
# --------------------------------------------------------------------------- #


def record_peer(app: ASGIApp) -> ASGIApp:
    """记下 TCP 直连对端，放在 uvicorn 的 ProxyHeadersMiddleware 外面用（见 ``web/app.make_server``）。

    那个中间件会按 X-Forwarded-For 把 ``scope["client"]`` 改成原始客户端；判断「是不是受信任的代理连过来的」
    要看改写之前的地址，否则任何人都能自己写一个 X-Forwarded-For 冒充。
    """

    async def wrapped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            scope[PEER_SCOPE_KEY] = scope.get("client")
        await app(scope, receive, send)

    return wrapped


def _header(scope: Scope, name: str) -> str | None:
    wanted = name.lower().encode("latin-1")
    for key, raw in scope.get("headers", []):
        if key == wanted:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
            text = text.strip()
            if not text or len(text) > MAX_HEADER_VALUE_CHARS:
                return None
            if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
                return None
            return text
    return None


class ProxyAuth:
    """只认受信任代理直接发来的身份头；别的地址带的同名头一律当没有。"""

    def __init__(self, cfg: ProxyAuthConfig) -> None:
        self.cfg = cfg
        self.networks = [
            ipaddress.ip_network(item.strip(), strict=False) for item in cfg.trusted_proxies
        ]

    def _trusted(self, scope: Scope) -> bool:
        peer = scope.get(PEER_SCOPE_KEY, scope.get("client"))
        if not peer:
            return False
        try:
            address = ipaddress.ip_address(str(peer[0]).split("%", 1)[0])
        except ValueError:
            return False
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        return any(address in network for network in self.networks)

    def identity(self, scope: Scope) -> ExternalIdentity | None:
        """这个请求是受信任代理认证过的人就返回身份，否则 None（包括名单不允许的人）。"""
        if not self._trusted(scope):
            return None
        username = _header(scope, self.cfg.user_header)
        if username is None:
            return None
        name = _header(scope, self.cfg.name_header) if self.cfg.name_header else None
        groups_text = _header(scope, self.cfg.groups_header) if self.cfg.groups_header else None
        groups = tuple(g.strip() for g in (groups_text or "").split(",") if g.strip())
        identity = ExternalIdentity("proxy", username, username, name or username, groups)
        if not allowed(identity, self.cfg.allowed_users, self.cfg.allowed_groups):
            return None
        return identity


def proxy_nonce(identity: ExternalIdentity) -> str:
    """反向代理登录没有 Cookie，CSRF 令牌按用户名派生。Cookie 的随机数不含冒号，二者不会撞上。"""
    return f"proxy:{identity.username}"


# --------------------------------------------------------------------------- #
# 接口
# --------------------------------------------------------------------------- #


class SingleSignOn:
    """全部单点登录方式。``from_config`` 在两种都没配时返回 None。"""

    def __init__(self, providers: list[OidcProvider], proxy: ProxyAuth | None, public_url: str):
        self.providers = {provider.cfg.id: provider for provider in providers}
        self.proxy = proxy
        self.public_url = public_url
        self.pending = PendingLogins()

    @classmethod
    def from_config(cls, cfg: AppConfig) -> SingleSignOn | None:
        providers = []
        for provider in cfg.server.oidc:
            client_secret = secret(provider.client_secret_env) if provider.client_secret_env else ""
            if provider.client_secret_env and not client_secret:
                raise RuntimeError(
                    f"server.oidc[{provider.id}].client_secret_env 指向的环境变量 "
                    f"{provider.client_secret_env} 未设置"
                )
            providers.append(OidcProvider(provider, client_secret or ""))
        proxy = ProxyAuth(cfg.server.proxy_auth) if cfg.server.proxy_auth.enabled else None
        if not providers and proxy is None:
            return None
        return cls(providers, proxy, cfg.server.public_url)

    def methods(self) -> dict[str, Any]:
        return {
            "oidc": [
                {"id": p.cfg.id, "display_name": p.cfg.display_name}
                for p in self.providers.values()
            ],
            "proxy": self.proxy is not None,
        }

    def redirect_uri(self, request: Request, provider: str) -> str:
        base = self.public_url or str(request.base_url).rstrip("/")
        return f"{base}/api/auth/oidc/{provider}/callback"


def _failure(next_path: str, code: str) -> RedirectResponse:
    return RedirectResponse(f"{next_path}#login_error={code}", status_code=303)


def register(
    app: FastAPI,
    sso: SingleSignOn,
    *,
    start_session: Callable[[Request, Response, ExternalIdentity], Awaitable[None]],
    client_address: Callable[[Request], str],
) -> None:
    """挂上 ``/api/auth/oidc/…``。``start_session`` 为认出来的人签发登录、把 Cookie 写进响应
    （由 ``auth.py`` 或会议权限模式的 ``access.py`` 提供）。"""

    def provider_or_404(provider_id: str) -> OidcProvider:
        provider = sso.providers.get(provider_id)
        if provider is None:
            raise ApiError(404, "auth.provider_not_found")
        return provider

    def binding_cookie(response: Response, request: Request, value: str | None) -> None:
        secure = request.url.scheme == "https"
        if value is None:
            response.delete_cookie(
                BINDING_COOKIE,
                path=BINDING_COOKIE_PATH,
                httponly=True,
                samesite="lax",
                secure=secure,
            )
            return
        # Lax：身份提供方跳回来是跨站的顶层 GET，Strict 的 Cookie 不会被带上
        response.set_cookie(
            BINDING_COOKIE,
            value,
            max_age=int(PENDING_SECS),
            path=BINDING_COOKIE_PATH,
            httponly=True,
            samesite="lax",
            secure=secure,
        )

    @app.get("/api/auth/oidc/{provider_id}/start")
    async def oidc_start(provider_id: str, request: Request) -> Response:
        provider = provider_or_404(provider_id)
        next_path = safe_next(request.query_params.get("next"))
        try:
            url, state, binding, pending = await provider.authorization_url(
                sso.redirect_uri(request, provider_id), next_path
            )
        except LoginFailed as e:
            logger.warning(f"单点登录（{provider_id}）无法开始：{e.detail}")
            return _failure(next_path, e.code)
        sso.pending.add(state, pending)
        response = RedirectResponse(url, status_code=302)
        binding_cookie(response, request, binding)
        return response

    @app.get("/api/auth/oidc/{provider_id}/callback")
    async def oidc_callback(provider_id: str, request: Request) -> Response:
        provider = provider_or_404(provider_id)
        params = request.query_params
        address = client_address(request)
        state = params.get("state") or ""
        pending = sso.pending.take(state) if state else None
        next_path = pending.next if pending is not None else "/"
        try:
            if pending is None or pending.provider != provider_id:
                raise LoginFailed("sso.expired", "state 未知、已用过或已过期")
            binding = request.cookies.get(BINDING_COOKIE)
            if not binding or not hmac.compare_digest(_digest(binding), pending.binding):
                raise LoginFailed("sso.expired", "浏览器绑定 Cookie 缺失或不匹配")
            error = params.get("error")
            if error:
                code = "sso.cancelled" if error == "access_denied" else "sso.failed"
                raise LoginFailed(code, f"身份提供方返回错误 {error!r}")
            code_param = params.get("code")
            if not code_param:
                raise LoginFailed("sso.failed", "回调里没有授权码")
            identity = await provider.finish(pending, code_param)
            if not allowed(identity, provider.cfg.allowed_users, provider.cfg.allowed_groups):
                raise LoginFailed("sso.denied", f"{identity.username} 不在允许登录的名单里")
        except LoginFailed as e:
            logger.warning(f"单点登录（{provider_id}）失败：{e.detail}；来自 {address}")
            response: Response = _failure(next_path, e.code)
            binding_cookie(response, request, None)
            return response
        response = RedirectResponse(next_path, status_code=303)
        binding_cookie(response, request, None)
        await start_session(request, response, identity)
        logger.info(f"单点登录成功：{identity.username}（{provider_id}）；来自 {address}")
        return response
