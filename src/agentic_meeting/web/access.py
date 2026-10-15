"""会议身份与授权；共享口令旧模式不进入此模块的认证流程。"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from agentic_meeting.config import AppConfig, secret
from agentic_meeting.store.db import Store
from agentic_meeting.web.auth import (
    COOKIE_NAME,
    CSRF_HEADER,
    MAX_LOGIN_BODY_BYTES,
    PUBLIC_PATHS,
    SAFE_METHODS,
    AuthGuard,
    LoginLimiter,
    _client_address,
    _read_body_limited,
    load_or_create_secret,
)
from agentic_meeting.web.errors import ApiError, error_response, invalid


@dataclass(frozen=True)
class Identity:
    digest: str
    principal: str
    cookie: str


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
        self.site = AuthGuard.from_config(cfg)
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
        self.checks = asyncio.Semaphore(4)
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
        guard = self.guards.get(principal)
        if guard is None or guard.verify(cookie) is None:
            return None
        return Identity(digest, principal, cookie)

    async def require(self, identity: Identity, session_id: str, action: str = "read") -> dict:
        if await self.authenticate(identity.cookie) is None:
            raise ApiError(401, "auth.required")
        membership = await self.store.membership(session_id, identity.principal)
        if membership is None:
            raise ApiError(404, "session.not_found")
        if action == "host" and membership["role"] != "host":
            raise ApiError(403, "access.denied")
        if (
            action == "question"
            and membership["role"] != "host"
            and not membership["allow_questions"]
        ):
            raise ApiError(403, "access.denied")
        return {"role": membership["role"], "allow_questions": bool(membership["allow_questions"])}

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

    def status(self, identity: Identity | None) -> dict:
        result: dict[str, Any] = {
            "enabled": True,
            "meeting_access": True,
            "site_password_required": self.site is not None,
            "authenticated": identity is not None,
            "csrf_token": None,
        }
        if identity is not None:
            user = self.users[identity.principal]
            guard = self.guards[identity.principal]
            result["principal"] = {
                "id": identity.principal,
                "display_name": user.display_name,
                "can_host": user.can_host,
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
    request: Request, session_id: str, action: str = "read", *, missing: str = "session.not_found"
) -> dict | None:
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
    return result


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
    live = res.sessions.live
    if live is not None and await res.store.membership(live.session.id, identity.principal):
        return await res.store.get_session(live.session.id)
    rows = await res.store.list_sessions(1, principal=identity.principal)
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
        response.set_cookie(
            COOKIE_NAME,
            cookie,
            max_age=int(guard.session_secs),
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        return response

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
            user = access.users.get(principal_id)
            if user is None or (role == "host" and not user.can_host):
                raise ApiError(400, "access.invalid_member")
            if not await access.store.set_member(session_id, principal_id, role, questions):
                raise ApiError(409, "access.last_host")
        await access.sweep()
        return {"items": await access.store.members(session_id)}

    app.add_middleware(AccessRequired, access=access)


class AccessRequired:
    def __init__(self, app: ASGIApp, access: MeetingAccess) -> None:
        self.app, self.access = app, access

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path, method = request.url.path, request.method
        if not (path == "/api" or path.startswith("/api/")) or (method, path) in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        identity = await self.access.authenticate(request.cookies.get(COOKIE_NAME))
        if identity is None:
            await error_response(401, "auth.required")(scope, receive, send)
            return
        guard = self.access.guards[identity.principal]
        if method not in SAFE_METHODS and not guard.csrf_ok(
            identity.cookie.split(".")[1], request.headers.get(CSRF_HEADER)
        ):
            await error_response(403, "auth.csrf")(scope, receive, send)
            return
        request.state.identity = identity
        started = False
        terminated = False

        async def guarded_send(message):
            nonlocal started, terminated
            if terminated:
                return
            session_ids = getattr(request.state, "authorized_sessions", ())
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

        await self.app(scope, receive, guarded_send)
