"""声纹档案与录入的 HTTP 接口（docs/interfaces.md §5.14）。

只在 ``speaker_memory.backend`` 不是 ``"none"`` 时注册；关闭时这些路径都不存在。
响应里只有名字、时长和日期，从不包含向量。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, Request

from agentic_meeting.config import AppConfig
from agentic_meeting.types import Session
from agentic_meeting.voice.profiles import (
    VoiceModelChanged,
    VoiceProfile,
    VoiceProfileInUse,
    voiceprint,
)
from agentic_meeting.web.access import audit, authorize, host_write
from agentic_meeting.web.access import current_session as accessible_current
from agentic_meeting.web.errors import ApiError, invalid
from agentic_meeting.web.sessions_api import MAX_SPEAKER_NAME_CHARS, _json_body


def profile_json(profile: VoiceProfile) -> dict[str, Any]:
    return {
        "id": profile.id,
        "name": profile.name,
        "samples": profile.samples,
        "secs": round(profile.secs, 1),
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
    }


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise invalid("name 必须是非空的字符串")
    name = value.strip()
    if len(name) > MAX_SPEAKER_NAME_CHARS:
        raise ApiError(400, "speaker.name_too_long", max=MAX_SPEAKER_NAME_CHARS)
    return name


def register(app: FastAPI, cfg: AppConfig) -> None:
    if cfg.speaker_memory.backend == "none":
        return
    min_enroll_secs = cfg.speaker_memory.min_enroll_secs

    def parts(request: Request) -> tuple[Any, Any]:
        res = request.app.state.resources
        if res.store is None or res.sessions is None:
            raise ApiError(503, "storage.not_ready")
        return res.store, res.sessions

    def require_account(request: Request) -> None:
        """档案是全部署共用的：会议权限模式下，通过分享进来的访客不能看、不能改。"""
        access = request.app.state.resources.access
        if access is not None and request.state.identity.guest:
            raise ApiError(403, "access.denied")

    async def host_session(request: Request, given: Any) -> Session:
        store, _ = parts(request)
        if given is not None and not isinstance(given, str):
            raise invalid("session_id 必须是字符串")
        session = await store.get_session(given) if given else await accessible_current(request)
        if session is None:
            raise ApiError(404, "session.not_found" if given else "session.none")
        await authorize(request, session.id, "host")
        await store.require_available(session.id)
        return session

    async def profile_or_404(store: Any, profile_id: int) -> VoiceProfile:
        profile = await store.get_voice_profile(profile_id)
        if profile is None:
            raise ApiError(404, "voice.profile_not_found")
        return profile

    # ---- 档案 ----

    @app.get("/api/voice-profiles")
    async def list_profiles(request: Request) -> dict[str, Any]:
        require_account(request)
        store, _ = parts(request)
        return {
            "items": [profile_json(p) for p in await store.list_voice_profiles()],
            "min_enroll_secs": min_enroll_secs,
        }

    @app.patch("/api/voice-profiles/{profile_id}")
    async def rename_profile(request: Request, profile_id: int) -> dict[str, Any]:
        require_account(request)
        store, _ = parts(request)
        name = _name((await _json_body(request)).get("name"))
        if not await store.rename_voice_profile(profile_id, name):
            raise ApiError(404, "voice.profile_not_found")
        return profile_json(await profile_or_404(store, profile_id))

    @app.delete("/api/voice-profiles/{profile_id}")
    async def delete_profile(request: Request, profile_id: int) -> dict[str, Any]:
        require_account(request)
        store, _ = parts(request)
        clips = await store.delete_voice_profile(profile_id)
        if clips is None:
            raise ApiError(404, "voice.profile_not_found")
        return {"id": profile_id, "clips": clips}

    # ---- 录入与关联 ----

    @app.post("/api/speakers/{idx}/voice-profile")
    @host_write
    async def enroll(request: Request, idx: int) -> dict[str, Any]:
        store, manager = parts(request)
        body = await _json_body(request)
        session = await host_session(request, body.get("session_id"))
        if body.get("consent") is not True:
            raise ApiError(400, "voice.consent_required")
        profile_id = body.get("profile_id")
        if profile_id is not None and (
            not isinstance(profile_id, int) or isinstance(profile_id, bool)
        ):
            raise invalid("profile_id 必须是整数")
        if idx <= 0:
            raise ApiError(400, "voice.not_enrollable")
        speakers = {s.idx: s for s in await store.list_speakers(session.id)}
        if idx not in speakers:
            raise ApiError(404, "speaker.not_found")
        if profile_id is None:
            name = _name(body.get("name", speakers[idx].display_name))
        else:
            name = (await profile_or_404(store, profile_id)).name
        clips = await store.speaker_voice_clips(session.id, idx)
        voice = await asyncio.to_thread(voiceprint, clips)
        have = voice.secs if voice is not None else 0.0
        if voice is None or have < min_enroll_secs:
            raise ApiError(
                400, "voice.too_short", have=round(have, 1), need=round(min_enroll_secs, 1)
            )
        try:
            profile = await store.enroll_voice(
                session.id,
                idx,
                profile_id=profile_id,
                name=name,
                vector=voice.vector,
                secs=voice.secs,
                model_sha256=voice.model_sha256,
            )
        except LookupError as e:
            raise ApiError(404, "voice.profile_not_found") from e
        except VoiceProfileInUse as e:
            raise ApiError(409, "voice.profile_in_use") from e
        except VoiceModelChanged as e:
            raise ApiError(409, "voice.model_changed") from e
        if speakers[idx].display_name != profile.name:
            await manager.rename_speaker(session.id, idx, profile.name)
        await audit(request, session.id, "voice.enroll", speaker_idx=idx, profile_id=profile.id)
        return {
            "speaker": {"idx": idx, "display_name": profile.name},
            "voice_profile": profile_json(profile),
        }

    @app.delete("/api/speakers/{idx}/voice-profile")
    @host_write
    async def unlink(request: Request, idx: int, session_id: str | None = None) -> dict[str, Any]:
        store, _ = parts(request)
        session = await host_session(request, session_id)
        if await store.unlink_voice(session.id, idx):
            await audit(request, session.id, "voice.unlink", speaker_idx=idx)
        return {"idx": idx, "voice_profile": None}
