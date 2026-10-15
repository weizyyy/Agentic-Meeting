"""会议凭据只在此边界输入/一次性输出；授权租约保留精确登录来源。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentic_meeting.web.access import Identity, MeetingAccess, authorize
from agentic_meeting.web.auth import (
    COOKIE_NAME,
    CSRF_HEADER,
    MAX_LOGIN_BODY_BYTES,
    _client_address,
    _read_body_limited,
    stretch_password,
)
from agentic_meeting.web.errors import ApiError, error_response, invalid

HEADERS = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}


async def body(request: Request, allowed: set[str]) -> dict:
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise invalid("需要JSON")
    raw = await _read_body_limited(request, MAX_LOGIN_BODY_BYTES)
    if raw is None:
        raise ApiError(413, "request.too_large")
    try:
        value = json.loads(raw)
    except ValueError:
        raise invalid("需要JSON对象") from None
    if not isinstance(value, dict) or value.keys() - allowed:
        raise invalid("分享字段无效")
    return value


def options(value: dict) -> tuple[int, bool]:
    ttl, questions = value.get("expires_in", 86400), value.get("allow_questions", False)
    if type(ttl) is not int or not 1 <= ttl <= 604800 or type(questions) is not bool:
        raise invalid("分享期限或提问选项无效")
    return ttl, questions


def metadata(row: dict) -> dict:
    return {key: row[key] for key in ("id", "kind", "expires_at", "allow_questions")}


def outcome(action: str, row: dict, principal: str) -> dict:
    # S06接入点仅返回已确认的非秘密来源，不记录凭据或未确认的目标。
    return {
        "name": "share." + action,
        "result": "success",
        "at": time.time(),
        "principal": principal,
        "session_id": row["session_id"],
        "source_id": row["id"],
        "source": "share_" + row["kind"],
    }


def register(app: FastAPI, access: MeetingAccess) -> None:
    dummy_salt = secrets.token_bytes(16)

    @app.get("/api/sessions/{session_id}/shares")
    async def listing(request: Request, session_id: str) -> dict:
        await authorize(request, session_id, "host")
        await access.store.require_available(session_id)
        return {"items": await access.store.shares(session_id)}

    async def create(
        request: Request, session_id: str, kind: str, old: str | None = None
    ) -> JSONResponse:
        await authorize(request, session_id, "host")
        value = await body(
            request,
            {"password", "expires_in", "allow_questions"}
            if kind == "password"
            else {"expires_in", "allow_questions"},
        )
        ttl, questions = options(value)
        token, salt = None, None
        if kind == "password":
            password = value.get("password")
            if not isinstance(password, str) or not 12 <= len(password) <= 128:
                raise invalid("会议口令需12至128字符")
            salt = secrets.token_bytes(16).hex()
            async with access.checks:
                verifier = (
                    await asyncio.to_thread(stretch_password, password, bytes.fromhex(salt))
                ).hex()
        else:
            token = secrets.token_urlsafe(32)
            verifier = hashlib.sha256(token.encode()).hexdigest()
        row = {
            "id": uuid.uuid4().hex,
            "session_id": session_id,
            "kind": kind,
            "credential_hash": verifier,
            "salt": salt,
            "created_at": time.time(),
            "expires_at": time.time() + ttl,
            "allow_questions": questions,
        }
        async with access.lock:
            await authorize(request, session_id, "host")
            await access.store.prune_shares()
            previous_password = kind == "password" and any(
                s["kind"] == "password" for s in await access.store.shares(session_id)
            )
            try:
                changed = await access.store.put_share(row, replace_id=old)
            except ValueError as exc:
                if str(exc) != "share.limit":
                    raise
                raise ApiError(409, "share.limit") from None
            if not changed:
                raise ApiError(404, "share.invalid")
        await access.sweep()
        result = {
            "share": metadata(row),
            "action": outcome(
                "rotate" if old or previous_password else "create",
                row,
                request.state.identity.principal,
            ),
        }
        if token is not None:
            result["token"] = token
        return JSONResponse(
            result, status_code=201 if kind == "link" and old is None else 200, headers=HEADERS
        )

    @app.put("/api/sessions/{session_id}/shares/password")
    async def password(request: Request, session_id: str) -> JSONResponse:
        return await create(request, session_id, "password")

    @app.post("/api/sessions/{session_id}/shares/links")
    async def link(request: Request, session_id: str) -> JSONResponse:
        return await create(request, session_id, "link")

    @app.post("/api/sessions/{session_id}/shares/{share_id}/rotate")
    async def rotate(request: Request, session_id: str, share_id: str) -> JSONResponse:
        return await create(request, session_id, "link", share_id)

    @app.delete("/api/sessions/{session_id}/shares/{share_id}")
    async def revoke(request: Request, session_id: str, share_id: str) -> dict:
        async with access.lock:
            await authorize(request, session_id, "host")
            await access.store.prune_shares()
            await access.store.revoke_share(session_id, share_id)
        await access.sweep()
        return {
            "revoked": True,
            "action": {
                "name": "share.revoke",
                "result": "success",
                "source_id": share_id,
                "session_id": session_id,
            },
        }

    @app.post("/api/shares/exchange")
    async def exchange(request: Request) -> JSONResponse:
        if request.headers.get("origin") != f"{request.url.scheme}://{request.url.netloc}":
            raise ApiError(403, "auth.csrf")
        cookie = request.cookies.get(COOKIE_NAME)
        identity = await access.authenticate(cookie)
        if cookie and identity is None:
            response = error_response(401, "auth.required", headers=HEADERS)
            response.delete_cookie(
                COOKIE_NAME, httponly=True, samesite="strict", secure=request.url.scheme == "https"
            )
            return response
        if identity is not None and not access.guard(identity).csrf_ok(
            identity.cookie.split(".")[1], request.headers.get(CSRF_HEADER)
        ):
            raise ApiError(403, "auth.csrf")
        value = await body(
            request, {"session_id", "kind", "share_id", "token", "password", "site_password"}
        )
        sid, kind = value.get("session_id"), value.get("kind")
        if (
            not isinstance(sid, str)
            or not re.fullmatch(r"[a-f0-9]{32}", sid)
            or kind not in ("password", "link")
        ):
            raise invalid("会议或分享类型无效")
        if "site_password" in value and not isinstance(value["site_password"], str):
            raise invalid("站点口令无效")
        if kind == "link":
            source, candidate = value.get("share_id"), value.get("token")
            if (
                not isinstance(source, str)
                or not re.fullmatch(r"[a-f0-9]{32}", source)
                or not isinstance(candidate, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", candidate)
                or "password" in value
            ):
                raise invalid("链接字段无效")
        else:
            source, candidate = None, value.get("password")
            if (
                not isinstance(candidate, str)
                or not 1 <= len(candidate) <= 128
                or {"share_id", "token"} & value.keys()
            ):
                raise invalid("口令字段无效")
        address = _client_address(request)
        async with access.checks:
            wait = access.limiter.retry_after(address)
            if wait > 0:
                return error_response(
                    429, "auth.too_many", headers={**HEADERS, "Retry-After": str(int(wait) + 1)}
                )
            row = await access.store.get_share(sid, kind, source)
            site_ok = access.site is None or await access.site.check_password_async(
                value.get("site_password", "")
            )
            if kind == "password":
                verifier = await asyncio.to_thread(
                    stretch_password, candidate, bytes.fromhex(row["salt"]) if row else dummy_salt
                )
                valid = hmac.compare_digest(
                    verifier.hex(), row["credential_hash"] if row else "0" * 64
                )
            else:
                valid = hmac.compare_digest(
                    hashlib.sha256(candidate.encode()).hexdigest(),
                    row["credential_hash"] if row else "0" * 64,
                )
            if row is None or not site_ok or not valid:
                access.limiter.failed(address)
                raise ApiError(404, "share.invalid")
            async with access.lock:
                guest = None
                if identity is None:
                    guest_cookie, _ = access.guest_guard.issue()
                    identity = Identity(
                        hashlib.sha256(guest_cookie.encode()).hexdigest(),
                        "guest:" + uuid.uuid4().hex,
                        guest_cookie,
                        True,
                    )
                    guest = (identity.principal, float(guest_cookie.split(".")[0]))
                elif await access.authenticate(identity.cookie) is None:
                    raise ApiError(401, "auth.required")
                expires = await access.store.exchange_share(row, identity.digest, guest=guest)
                if expires is None:
                    access.limiter.failed(address)
                    raise ApiError(404, "share.invalid")
        result = {
            "auth": access.status(identity),
            "session_id": sid,
            "access": {
                "role": "watcher",
                "allow_questions": bool(row["allow_questions"]),
                "expires_at": expires,
            },
            "action": outcome("exchange", row, identity.principal),
        }
        response = JSONResponse(result, headers=HEADERS)
        if guest is not None:
            response.set_cookie(
                COOKIE_NAME,
                identity.cookie,
                max_age=int(access.guest_guard.session_secs),
                httponly=True,
                samesite="strict",
                secure=request.url.scheme == "https",
            )
        return response
