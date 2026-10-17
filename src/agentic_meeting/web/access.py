"""会议身份与授权；共享口令旧模式不进入此模块的认证流程。"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response
from loguru import logger
from starlette.types import ASGIApp, Receive, Scope, Send

from agentic_meeting.config import AppConfig, secret
from agentic_meeting.store.db import Store
from agentic_meeting.web import sso as sso_module
from agentic_meeting.web.auth import (
    COOKIE_NAME,
    CSRF_HEADER,
    MAX_LOGIN_BODY_BYTES,
    PUBLIC_GET_PREFIX,
    PUBLIC_PATHS,
    SAFE_METHODS,
    AuthGuard,
    LoginLimiter,
    _client_address,
    _read_body_limited,
    load_or_create_secret,
)
from agentic_meeting.web.errors import ApiError, error_response, invalid

# 同一个登录反复读同一场会议，这么久之内只记一条「查看」
VIEW_LOG_SECS = 1800
VIEW_LOG_MAX_KEYS = 10000


@dataclass(frozen=True)
class Identity:
    digest: str
    principal: str
    cookie: str
    guest: bool = False


@dataclass(frozen=True)
class ExternalPrincipal:
    """单点登录的主体：显示名和能否主持取自最近一次登录。"""

    display_name: str
    can_host: bool


@dataclass(eq=False)
class Lease:
    identity: Identity
    session_id: str
    action: str
    close: Callable[[], Awaitable[None]]
    live: Any = None


class MeetingAccess:
    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.users = {f"local:{user.id}": user for user in cfg.server.local_users}
        salt = load_or_create_secret(cfg.resolve(cfg.session.data_dir))
        # 只有设了站点口令才要它；单点登录让 AuthGuard.from_config 也返回不带口令的 guard
        self.site = AuthGuard.from_config(cfg) if cfg.server.password_env else None
        self.guards: dict[str, AuthGuard] = {}
        for principal, user in self.users.items():
            password = secret(user.password_env)
            if not password or len(password) < 8:
                raise RuntimeError(f"本地账号 {user.id} 的 {user.password_env} 未设置或不足8字符")
            # 站点口令轮换也使个人登录失效，不把秘密写入登录表。
            binding = principal + (secret(cfg.server.password_env) or "")
            user_salt = hmac.new(salt, binding.encode(), hashlib.sha256).digest()
            self.guards[principal] = AuthGuard(
                password, user_salt, session_secs=cfg.server.auth_session_days * 86400
            )
        self.limiter = LoginLimiter()
        # 串行检查共享预算，避免同地址并发校验超出失败次数上限。
        self.checks = asyncio.Semaphore(1)
        guest_salt = hmac.new(
            salt, b"guest" + (secret(cfg.server.password_env) or "").encode(), hashlib.sha256
        ).digest()
        self.guest_guard = AuthGuard(
            salt.hex(), guest_salt, session_secs=min(cfg.server.auth_session_days * 86400, 86400)
        )
        # 单点登录的主体共用一个签名密钥；哪个登录属于谁由 login_sessions 决定
        self.sso = sso_module.SingleSignOn.from_config(cfg)
        sso_salt = hmac.new(
            salt, b"sso" + (secret(cfg.server.password_env) or "").encode(), hashlib.sha256
        ).digest()
        self.sso_guard = AuthGuard(
            None, sso_salt, session_secs=cfg.server.auth_session_days * 86400
        )
        self.external: dict[str, ExternalPrincipal] = {}
        self.pruned_at = time.monotonic()
        self.viewed: dict[tuple[str, str], float] = {}
        self.store: Store
        self.leases: set[Lease] = set()
        self.monitor: asyncio.Task | None = None
        self.peers: dict[str, tuple[Identity, Any]] = {}
        self.offer_lock = asyncio.Lock()
        # 授权变更与只读回答的最后一次检查/写入/发布按此锁排序。
        self.lock = asyncio.Lock()

    async def start(self, store: Store) -> None:
        self.store = store
        await store.register_principals(
            [(key, user.display_name) for key, user in self.users.items()]
        )
        # 身份提供方从配置里删掉后，经它登录的人不再能登录，也不能再被授权
        providers = self.sso.providers if self.sso is not None else {}
        self.external = {
            row["id"]: ExternalPrincipal(row["display_name"], row["can_host"])
            for row in await store.external_principals()
            if row["id"].split(":")[1] in providers
        }
        await store.prune_shares()
        self.monitor = asyncio.create_task(self._monitor(), name="meeting-access")

    async def stop(self) -> None:
        if self.monitor is not None:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        for lease in list(self.leases):
            self.leases.discard(lease)
            await lease.close()

    async def authenticate(self, cookie: str | None) -> Identity | None:
        if not cookie:
            return None
        digest = hashlib.sha256(cookie.encode()).hexdigest()
        row = await self.store.get_login(digest)
        if row is None:
            return None
        principal = row["principal_id"]
        guest = await self.store.is_guest_login(digest, principal)
        identity = Identity(digest, principal, cookie, guest)
        guard = self._guard(identity)
        if guard is None or guard.verify(cookie) is None:
            return None
        return identity

    async def require(self, identity: Identity, session_id: str, action: str = "read") -> dict:
        if await self.authenticate(identity.cookie) is None:
            raise ApiError(401, "auth.required")
        membership = await self.store.membership(session_id, identity.principal)
        shares = await self.store.share_sources(session_id, identity.digest)
        if membership is None and not shares:
            raise ApiError(404, "session.not_found")
        host = membership is not None and membership["role"] == "host"
        questions = (
            host
            or bool(membership and membership["allow_questions"])
            or any(s["allow_questions"] for s in shares)
        )
        if (action == "host" and not host) or (action == "question" and not questions):
            raise ApiError(403, "access.denied")
        return {
            "role": "host" if host else "watcher",
            "allow_questions": questions,
            "share_expires_at": max(s["expires_at"] for s in shares)
            if membership is None
            else None,
            "sources": ([{"kind": "acl"}] if membership else [])
            + [{"kind": "share_" + s["kind"], "id": s["id"]} for s in shares],
        }

    def display_name(self, identity: Identity) -> str:
        if identity.guest:
            return "Guest"
        user = self.users.get(identity.principal) or self.external.get(identity.principal)
        return "Guest" if user is None else user.display_name

    async def audit(self, identity: Identity, session_id: str, action: str, **detail: Any) -> None:
        """记一条访问记录；写不进去只记日志，不影响请求本身。"""
        try:
            await self.store.log_access(
                session_id, identity.principal, self.display_name(identity), action, detail
            )
        except Exception:
            logger.exception(f"写会议 {session_id} 的访问记录失败")

    async def note_view(self, identity: Identity, session_id: str, access: dict) -> None:
        """读会议内容：同一个登录对同一场会议 ``VIEW_LOG_SECS`` 内只记一次。"""
        key, now = (identity.digest, session_id), time.monotonic()
        if now - self.viewed.get(key, -VIEW_LOG_SECS) < VIEW_LOG_SECS:
            return
        if len(self.viewed) >= VIEW_LOG_MAX_KEYS:
            cutoff = now - VIEW_LOG_SECS
            self.viewed = {k: t for k, t in self.viewed.items() if t > cutoff}
        self.viewed[key] = now
        await self.audit(
            identity, session_id, "meeting.view", role=access["role"], sources=access["sources"]
        )

    def _guard(self, identity: Identity) -> AuthGuard | None:
        if identity.guest:
            return self.guest_guard
        if identity.principal in self.external:
            return self.sso_guard
        return self.guards.get(identity.principal)

    def guard(self, identity: Identity) -> AuthGuard:
        guard = self._guard(identity)
        assert guard is not None
        return guard

    def principal_can_host(self, principal: str) -> bool | None:
        """配置的账号或登录过的单点登录主体能否主持；不认识的主体返回 None。"""
        user = self.users.get(principal) or self.external.get(principal)
        return None if user is None else user.can_host

    def can_host(self, identity: Identity) -> bool:
        return not identity.guest and bool(self.principal_can_host(identity.principal))

    async def sign_in_external(self, identity: sso_module.ExternalIdentity) -> tuple[str, Identity]:
        """单点登录认出人之后：记下主体，按配置判断能否主持，签发一个登录。返回 Cookie 和身份。"""
        assert self.sso is not None
        principal = f"{identity.source}:{identity.subject}"
        provider = self.sso.providers[identity.source.removeprefix("oidc:")]
        can_host = sso_module.allowed(identity, provider.cfg.host_users, provider.cfg.host_groups)
        # 两张名单都空时 allowed 放行所有人；主持人名单要显式给出
        can_host = can_host and bool(provider.cfg.host_users or provider.cfg.host_groups)
        await self.store.save_external_principal(principal, identity.display_name, can_host)
        self.external[principal] = ExternalPrincipal(identity.display_name, can_host)
        cookie, _ = self.sso_guard.issue()
        digest = hashlib.sha256(cookie.encode()).hexdigest()
        await self.store.save_login(digest, principal, float(cookie.split(".")[0]))
        return cookie, Identity(digest, principal, cookie)

    def track(
        self,
        identity: Identity,
        session_id: str,
        action: str,
        close: Callable[[], Awaitable[None]],
        *,
        live: Any = None,
    ) -> Lease:
        lease = Lease(identity, session_id, action, close, live)
        self.leases.add(lease)
        return lease

    async def sweep(self) -> None:
        for lease in list(self.leases):
            try:
                if lease.live is not None and (
                    lease.live.stop_requested or lease.live.done.is_set()
                ):
                    raise ApiError(409, "session.not_live")
                await self.require(lease.identity, lease.session_id, lease.action)
            except ApiError:
                if lease in self.leases:
                    self.leases.discard(lease)
                    await lease.close()

    async def _monitor(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            await self.sweep()
            if time.monotonic() - self.pruned_at >= 60:
                await self.store.prune_shares()
                self.pruned_at = time.monotonic()

    def status(self, identity: Identity | None) -> dict:
        result: dict[str, Any] = {
            "enabled": True,
            "meeting_access": True,
            "site_password_required": self.site is not None,
            "authenticated": identity is not None,
            "csrf_token": None,
        }
        if self.sso is not None:
            result["methods"] = {"password": True, **self.sso.methods(), "proxy": False}
        if identity is not None:
            guard = self.guard(identity)
            result["principal"] = {
                "id": identity.principal,
                "guest": identity.guest,
                "display_name": self.display_name(identity),
                "can_host": self.can_host(identity),
            }
            result["csrf_token"] = guard.csrf_token(identity.cookie.split(".")[1])
        return result

    async def peer(self, identity: Identity, pc_id: str) -> Any:
        found = self.peers.get(pc_id)
        if found is None or found[0].digest != identity.digest:
            raise ApiError(404, "session.not_found")
        live = found[1]
        await self.require(identity, live.session.id, "host")
        if live.stop_requested or live.done.is_set():
            raise ApiError(404, "session.not_found")
        return live


async def authorize(
    request: Request,
    session_id: str,
    action: str = "read",
    *,
    missing: str = "session.not_found",
    view: bool = True,
) -> dict | None:
    """会议权限模式下检查调用者对这场会议的权限。

    读内容（``action="read"``）时顺带记一条「查看」；列表之类不算查看的读取传 ``view=False``。
    """
    access = request.app.state.resources.access
    if access is None:
        return None
    try:
        result = await access.require(request.state.identity, session_id, action)
    except ApiError as exc:
        if exc.status_code == 404:
            raise ApiError(404, missing) from None
        raise
    if not hasattr(request.state, "authorized_sessions"):
        request.state.authorized_sessions = set()
    request.state.authorized_sessions.add(session_id)
    if view and action == "read":
        await access.note_view(request.state.identity, session_id, result)
    return result


async def audit(request: Request, session_id: str, action: str, **detail: Any) -> None:
    """会议权限模式下给这场会议记一条访问记录；旧模式什么也不做。"""
    access = request.app.state.resources.access
    if access is not None:
        await access.audit(request.state.identity, session_id, action, **detail)


def host_write(function):
    """短 HTTP 管理操作与撤权排序；持续问答和媒体另有可撤销租约。"""

    @functools.wraps(function)
    async def wrapped(*args, **kwargs):
        request = kwargs.get("request") or kwargs.get("http_request")
        access = request.app.state.resources.access
        if access is None:
            return await function(*args, **kwargs)
        async with access.lock:
            return await function(*args, **kwargs)

    return wrapped


async def current_session(request: Request):
    res = request.app.state.resources
    if res.access is None:
        return await res.sessions.current_session()
    identity = request.state.identity
    # 同时进行好几场时，取这个登录能看的、最近连上的那一场
    for live in reversed(res.sessions.connections):
        try:
            await res.access.require(identity, live.session.id)
        except ApiError as exc:
            if exc.status_code != 404:
                raise
        else:
            return await res.store.get_session(live.session.id)
    rows = await res.store.list_sessions(
        1, principal=identity.principal, login_digest=identity.digest
    )
    return rows[0].session if rows else None


def register(app: FastAPI, access: MeetingAccess) -> None:
    @app.get("/api/auth")
    async def status(request: Request) -> dict:
        return access.status(await access.authenticate(request.cookies.get(COOKIE_NAME)))

    @app.post("/api/auth/login")
    async def login(request: Request) -> JSONResponse:
        address = _client_address(request)
        wait = access.limiter.retry_after(address)
        if wait > 0:
            return error_response(429, "auth.too_many", headers={"Retry-After": str(int(wait) + 1)})
        raw = await _read_body_limited(request, MAX_LOGIN_BODY_BYTES)
        if raw is None:
            raise ApiError(413, "request.too_large")
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise invalid("需要JSON登录信息") from exc
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("password"), str)
            or not isinstance(body.get("user_id"), str)
        ):
            raise invalid("需要 user_id 和 password")
        principal = f"local:{body['user_id']}"
        guard = access.guards.get(principal)
        async with access.checks:
            wait = access.limiter.retry_after(address)
            if wait > 0:
                return error_response(
                    429, "auth.too_many", headers={"Retry-After": str(int(wait) + 1)}
                )
            # 未知账号也执行相同代价的比对，不让结果或耗时暴露账号列表。
            checked = guard or next(iter(access.guards.values()))
            valid = await checked.check_password_async(body["password"])
            if access.site is not None:
                site_password = body.get("site_password", "")
                site_valid = isinstance(
                    site_password, str
                ) and await access.site.check_password_async(site_password)
                valid = valid and site_valid
            if guard is None or not valid:
                access.limiter.failed(address)
                raise ApiError(401, "auth.wrong_password")
            access.limiter.succeeded(address)
        cookie, _ = guard.issue()
        digest = hashlib.sha256(cookie.encode()).hexdigest()
        await access.store.save_login(digest, principal, float(cookie.split(".")[0]))
        response = JSONResponse(access.status(Identity(digest, principal, cookie)))
        set_cookie(request, response, cookie, guard)
        return response

    def set_cookie(request: Request, response: Response, cookie: str, guard: AuthGuard) -> None:
        response.set_cookie(
            COOKIE_NAME,
            cookie,
            max_age=int(guard.session_secs),
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )

    async def start_sso_session(
        request: Request, response: Response, identity: sso_module.ExternalIdentity
    ) -> None:
        cookie, _ = await access.sign_in_external(identity)
        set_cookie(request, response, cookie, access.sso_guard)

    if access.sso is not None:
        sso_module.register(
            app, access.sso, start_session=start_sso_session, client_address=_client_address
        )

    @app.post("/api/auth/logout")
    @app.post("/api/auth/logout-all")
    async def logout(request: Request) -> JSONResponse:
        identity = request.state.identity
        async with access.lock:
            await access.store.revoke_login(
                identity.digest,
                principal=identity.principal if request.url.path.endswith("logout-all") else None,
            )
        await access.sweep()
        response = JSONResponse(access.status(None))
        response.delete_cookie(
            COOKIE_NAME, httponly=True, samesite="strict", secure=request.url.scheme == "https"
        )
        return response

    @app.get("/api/sessions/{session_id}/members")
    async def members(request: Request, session_id: str) -> dict:
        await authorize(request, session_id, "host")
        return {"items": await access.store.members(session_id)}

    @app.put("/api/sessions/{session_id}/members/{principal_id}")
    @app.delete("/api/sessions/{session_id}/members/{principal_id}")
    async def member(request: Request, session_id: str, principal_id: str) -> dict:
        role, questions = None, False
        if request.method == "PUT":
            raw = await _read_body_limited(request, MAX_LOGIN_BODY_BYTES)
            if raw is None:
                raise ApiError(413, "request.too_large")
            try:
                body = json.loads(raw)
            except ValueError as exc:
                raise invalid("需要JSON成员信息") from exc
            if not isinstance(body, dict):
                raise invalid("需要JSON成员信息")
            role, questions = body.get("role"), body.get("allow_questions", False)
            if role not in ("host", "watcher") or not isinstance(questions, bool):
                raise invalid("role 必须是 host/watcher，allow_questions 必须是布尔值")
        async with access.lock:
            await authorize(request, session_id, "host")
            can_host = access.principal_can_host(principal_id)
            # 移除不要求账号还在配置里：账号删掉之后，主持人仍要能把它从名单上去掉
            if role is not None and (can_host is None or (role == "host" and not can_host)):
                raise ApiError(400, "access.invalid_member")
            if not await access.store.set_member(session_id, principal_id, role, questions):
                raise ApiError(409, "access.last_host")
        if role is None:
            await access.audit(
                request.state.identity, session_id, "member.remove", principal_id=principal_id
            )
        else:
            await access.audit(
                request.state.identity,
                session_id,
                "member.set",
                principal_id=principal_id,
                role=role,
                allow_questions=questions,
            )
        await access.sweep()
        return {"items": await access.store.members(session_id)}

    @app.get("/api/sessions/{session_id}/access-log")
    async def access_log(
        request: Request,
        session_id: str,
        limit: int = Query(100, ge=1, le=500),
        before_id: int | None = None,
    ) -> dict:
        await authorize(request, session_id, "host", view=False)
        return {
            "items": await access.store.access_log(session_id, limit=limit, before_id=before_id)
        }

    from agentic_meeting.web.shares import register as register_shares

    register_shares(app, access)
    app.add_middleware(AccessRequired, access=access)


class AccessRequired:
    def __init__(self, app: ASGIApp, access: MeetingAccess) -> None:
        self.app, self.access = app, access

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        original_send = send

        async def headers_send(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                headers = [(k, v) for k, v in headers if k.lower() != b"referrer-policy"]
                headers.append((b"referrer-policy", b"no-referrer"))
                if scope["path"].startswith("/api/shares") or "/shares" in scope["path"]:
                    headers = [(k, v) for k, v in headers if k.lower() != b"cache-control"]
                    headers.append((b"cache-control", b"private, no-store"))
                message = {**message, "headers": headers}
            await original_send(message)

        send = headers_send
        request = Request(scope)
        path, method = request.url.path, request.method
        if (
            not (path == "/api" or path.startswith("/api/"))
            or (method, path) in PUBLIC_PATHS
            or (method, path) == ("POST", "/api/shares/exchange")
            or (method == "GET" and path.startswith(PUBLIC_GET_PREFIX))
        ):
            await self.app(scope, receive, send)
            return
        identity = await self.access.authenticate(request.cookies.get(COOKIE_NAME))
        if identity is None:
            await error_response(401, "auth.required")(scope, receive, send)
            return
        guard = self.access.guard(identity)
        if method not in SAFE_METHODS and not guard.csrf_ok(
            identity.cookie.split(".")[1], request.headers.get(CSRF_HEADER)
        ):
            await error_response(403, "auth.csrf")(scope, receive, send)
            return
        request.state.identity = identity
        started = False
        terminated = False
        revoked = False
        leases: dict[str, Lease] = {}
        task = asyncio.current_task()

        async def close() -> None:
            nonlocal revoked
            revoked = True
            task.cancel()

        async def guarded_send(message):
            nonlocal started, terminated
            if terminated:
                return
            session_ids = getattr(request.state, "authorized_sessions", ())
            if not path.endswith("/events"):
                for sid in session_ids:
                    if sid not in leases:
                        leases[sid] = self.access.track(identity, sid, "read", close)
            if session_ids and message["type"] in (
                "http.response.start",
                "http.response.body",
            ):
                try:
                    for session_id in session_ids:
                        await self.access.require(identity, session_id)
                except ApiError as exc:
                    terminated = True
                    if started:
                        await send({"type": "http.response.body", "body": b"", "more_body": False})
                    else:
                        await error_response(exc.status_code, exc.message.code)(
                            scope, receive, send
                        )
                    return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, guarded_send)
        except asyncio.CancelledError:
            if not revoked:
                raise
            async with asyncio.timeout(0.25):
                if started:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                else:
                    await error_response(404, "session.not_found")(scope, receive, send)
        finally:
            for lease in leases.values():
                self.access.leases.discard(lease)
