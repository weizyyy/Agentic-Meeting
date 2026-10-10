"""访问口令：登录、签名 Cookie 会话、CSRF 令牌、登录限速（docs/interfaces.md §5.7）。

需要显式开启：``server.password_env`` 留空时 :func:`register` 只挂一个报告「未启用」的 ``GET /api/auth``，
不加中间件，单机使用与以前完全一样。

会话不存服务端：Cookie 里带到期时刻和一个随机数，用签名密钥做 HMAC。签名密钥由数据目录里的随机密钥
（``auth_secret``，首次启动时生成）和口令共同派生，所以改口令或删掉那个文件都会让所有设备退出登录；
只拿到 Cookie 也没法离线猜口令。CSRF 令牌由同一密钥对会话随机数做 HMAC 得到，同样不用存。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from agentic_meeting.config import AppConfig, secret

COOKIE_NAME = "am_session"
CSRF_HEADER = "X-CSRF-Token"
SECRET_FILE = "auth_secret"
SECRET_BYTES = 32
# 口令拉伸参数（scrypt）：一次约几十毫秒、16 MB 内存。启动时算一次，每次登录尝试再算一次。
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
# 同时进行的口令比对上限：一大波并发登录不会占满线程池和内存
MAX_CONCURRENT_CHECKS = 4

# 登录限速：同一地址在窗口内最多失败这么多次
MAX_FAILURES = 5
FAILURE_WINDOW_SECS = 300.0
# 记着的地址数上限：超出时丢掉最久没失败过的，内存不会被大量来源地址撑大
MAX_TRACKED_ADDRESSES = 10_000

# 不需要登录的接口
PUBLIC_PATHS = frozenset({("GET", "/api/auth"), ("HEAD", "/api/auth"), ("POST", "/api/auth/login")})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

NEED_LOGIN = "请先登录"
BAD_CSRF = "请求缺少有效的安全令牌，请刷新页面后重试"
WRONG_PASSWORD = "口令不对"
TOO_MANY = "尝试次数过多，请稍后再试"
NOT_ENABLED = "没有启用访问口令"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def stretch_password(password: str, salt: bytes) -> bytes:
    """用 scrypt 拉伸口令。签名密钥和口令比对都从它出发，不直接对口令做快速哈希。"""
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )


def load_or_create_secret(data_dir: Path) -> bytes:
    """读数据目录里的随机密钥；没有就生成一个（仅属主可读）。文件内容是十六进制文本。"""
    path = data_dir / SECRET_FILE
    if not path.is_file():
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # 另一个进程刚写好，下面照常读
        else:
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(secrets.token_hex(SECRET_BYTES))
            logger.info(f"已生成登录会话密钥：{path}")
    text = path.read_text("ascii").strip()
    try:
        value = bytes.fromhex(text)
    except ValueError:
        value = b""
    if len(value) < 16:
        raise RuntimeError(f"{path} 内容无效：删除该文件后重启即可重新生成（所有设备需要重新登录）")
    return value


class LoginLimiter:
    """按客户端地址统计登录失败次数；窗口内失败过多时拒绝，直到最早一次失败移出窗口。"""

    def __init__(
        self,
        max_failures: int = MAX_FAILURES,
        window_secs: float = FAILURE_WINDOW_SECS,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_addresses: int = MAX_TRACKED_ADDRESSES,
    ) -> None:
        self.max_failures = max_failures
        self.window_secs = window_secs
        self._clock = clock
        self._max_addresses = max_addresses
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, address: str) -> deque[float] | None:
        times = self._failures.get(address)
        if times is None:
            return None
        cutoff = self._clock() - self.window_secs
        while times and times[0] <= cutoff:
            times.popleft()
        if not times:
            del self._failures[address]
            return None
        return times

    def retry_after(self, address: str) -> float:
        """还要等多少秒才能再试；0 表示现在就可以。"""
        times = self._recent(address)
        if times is None or len(times) < self.max_failures:
            return 0.0
        return max(0.0, times[0] + self.window_secs - self._clock())

    def failed(self, address: str) -> None:
        times = self._recent(address)
        if times is None:
            if len(self._failures) >= self._max_addresses:
                # 丢掉最近一次失败最早的那个地址
                oldest = min(self._failures, key=lambda a: self._failures[a][-1])
                del self._failures[oldest]
            times = self._failures[address] = deque()
        times.append(self._clock())

    def succeeded(self, address: str) -> None:
        self._failures.pop(address, None)


class AuthGuard:
    """口令校验与会话签发。``clock`` 可注入（测试用）。"""

    def __init__(
        self,
        password: str,
        server_secret: bytes,
        *,
        session_secs: float,
        clock: Callable[[], float] = time.time,
        limiter: LoginLimiter | None = None,
    ) -> None:
        if not password:
            raise ValueError("访问口令不能为空")
        self._salt = server_secret
        self._stretched = stretch_password(password, server_secret)
        self._key = hmac.new(self._stretched, b"agentic-meeting session", hashlib.sha256).digest()
        self.session_secs = session_secs
        self._clock = clock
        self.limiter = limiter or LoginLimiter()
        self._checks = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

    @classmethod
    def from_config(cls, cfg: AppConfig) -> AuthGuard | None:
        """按配置创建；没有启用口令返回 None。启用了却读不到口令时报错（宁可起不来，也不能不设防）。"""
        env_name = cfg.server.password_env
        if not env_name:
            return None
        password = secret(env_name)
        if not password:
            raise RuntimeError(f"server.password_env 指向的环境变量 {env_name} 未设置")
        server_secret = load_or_create_secret(cfg.resolve(cfg.session.data_dir))
        return cls(password, server_secret, session_secs=cfg.server.auth_session_days * 86400.0)

    def check_password(self, candidate: str) -> bool:
        """比对口令（阻塞几十毫秒；在事件循环里请用 :meth:`check_password_async`）。"""
        return hmac.compare_digest(stretch_password(candidate, self._salt), self._stretched)

    async def check_password_async(self, candidate: str) -> bool:
        async with self._checks:
            return await asyncio.to_thread(self.check_password, candidate)

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).digest())

    def issue(self) -> tuple[str, str]:
        """新会话：返回 (Cookie 值, CSRF 令牌)。"""
        expires = int(self._clock() + self.session_secs)
        nonce = secrets.token_urlsafe(18)
        payload = f"{expires}.{nonce}"
        return f"{payload}.{self._sign(payload)}", self.csrf_token(nonce)

    def csrf_token(self, nonce: str) -> str:
        return self._sign(f"csrf.{nonce}")

    def verify(self, cookie: str | None) -> str | None:
        """Cookie 有效时返回会话随机数，否则 None（格式不对、签名不对、已过期）。"""
        if not cookie:
            return None
        parts = cookie.split(".")
        if len(parts) != 3:
            return None
        expires_text, nonce, signature = parts
        if not (expires_text.isascii() and expires_text.isdigit()) or not nonce:
            return None
        payload = f"{expires_text}.{nonce}"
        try:
            expected = self._sign(payload)
        except UnicodeEncodeError:
            return None
        if not hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8")):
            return None
        if int(expires_text) <= self._clock():
            return None
        return nonce

    def csrf_ok(self, nonce: str, header: str | None) -> bool:
        if not header:
            return False
        return hmac.compare_digest(self.csrf_token(nonce).encode("ascii"), header.encode("utf-8"))


def _client_address(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _status(*, enabled: bool, csrf_token: str | None) -> dict[str, Any]:
    authenticated = csrf_token is not None or not enabled
    return {"enabled": enabled, "authenticated": authenticated, "csrf_token": csrf_token}


def register(app: FastAPI, guard: AuthGuard | None) -> None:
    """挂上 ``/api/auth`` 系列接口；启用了口令时再加上拦截 ``/api`` 的中间件。"""

    @app.get("/api/auth")
    async def auth_status(request: Request) -> dict[str, Any]:
        if guard is None:
            return _status(enabled=False, csrf_token=None)
        nonce = guard.verify(request.cookies.get(COOKIE_NAME))
        return _status(enabled=True, csrf_token=guard.csrf_token(nonce) if nonce else None)

    @app.post("/api/auth/login")
    async def login(request: Request) -> JSONResponse:
        if guard is None:
            raise StarletteHTTPException(404, NOT_ENABLED)
        address = _client_address(request)
        wait = guard.limiter.retry_after(address)
        if wait > 0:
            return JSONResponse(
                {"error": TOO_MANY}, status_code=429, headers={"Retry-After": str(int(wait) + 1)}
            )
        try:
            body = await request.json()
        except ValueError:
            body = None
        password = body.get("password") if isinstance(body, dict) else None
        if not isinstance(password, str):
            raise StarletteHTTPException(400, '请求体应为 {"password": "..."}')
        if not await guard.check_password_async(password):
            guard.limiter.failed(address)
            logger.warning(f"登录失败：{address}")
            return JSONResponse({"error": WRONG_PASSWORD}, status_code=401)
        guard.limiter.succeeded(address)
        cookie, csrf = guard.issue()
        response = JSONResponse(_status(enabled=True, csrf_token=csrf))
        response.set_cookie(
            COOKIE_NAME,
            cookie,
            max_age=int(guard.session_secs),
            path="/",
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        logger.info(f"登录成功：{address}")
        return response

    @app.post("/api/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        # 启用了口令时这个接口在中间件后面：只有已登录、带着令牌的页面才能退出（防止别的网站把人踢下线）
        response = JSONResponse(_status(enabled=guard is not None, csrf_token=None))
        response.delete_cookie(
            COOKIE_NAME,
            path="/",
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        return response

    if guard is None:
        return

    app.add_middleware(LoginRequired, guard=guard)


class LoginRequired:
    """拦住 ``/api`` 下除登录接口外的全部请求：没有有效 Cookie 回 401，改动性请求缺令牌回 403。

    写成纯 ASGI 中间件，不用 ``@app.middleware("http")``（Starlette 的 BaseHTTPMiddleware）：
    后者把下游连同响应后的后台任务一起包在自己的任务组里，而 ``POST /api/offer`` 的后台任务就是整场会议的
    bot，不能被一个 HTTP 请求的生命周期牵连。
    """

    def __init__(self, app: ASGIApp, guard: AuthGuard) -> None:
        self.app = app
        self.guard = guard

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)  # 只读请求头和 Cookie，不碰请求体
        path = request.url.path
        method = request.method
        if not (path == "/api" or path.startswith("/api/")) or (method, path) in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        nonce = self.guard.verify(request.cookies.get(COOKIE_NAME))
        if nonce is None:
            response = JSONResponse({"error": NEED_LOGIN}, status_code=401)
        elif method not in SAFE_METHODS and not self.guard.csrf_ok(
            nonce, request.headers.get(CSRF_HEADER)
        ):
            response = JSONResponse({"error": BAD_CSRF}, status_code=403)
        else:
            await self.app(scope, receive, send)
            return
        await response(scope, receive, send)
