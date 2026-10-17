"""会议中比对声纹，给主持人提示「这位说话人可能是某某」（docs/interfaces.md §5.14、§6.1）。

一位说话人（编号 ≥ 1、还没关联档案）的单人语音够 ``min_match_secs`` 秒之后：

1. 和每份可比的档案（同一个模型、本场还没关联）打分：他在这场会议的声纹和档案各样本的余弦相似度取最高；
2. 最像的档案（不算主持人忽略过的）分数不低于 ``match_threshold``、并且比第二像的高出 ``match_margin``，才提示；
3. 按分数从高到低分配，一份档案最多提示给一位说话人。

提示只是提示：主持人确认之前什么都不改。向量和分数的原值都不离开服务端。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from loguru import logger

from agentic_meeting.config import SpeakerMemoryConfig
from agentic_meeting.voice.profiles import VoiceModelChanged, VoiceProfile, voiceprint

MATCH_INTERVAL_SECS = 5.0  # 会议进行中两次比对之间至少隔这么久


@dataclass(frozen=True, slots=True)
class ProfileSamples:
    id: int
    name: str
    model_sha256: str
    vectors: list[bytes]  # 各样本，float32 小端、单位长度


@dataclass(frozen=True, slots=True)
class MatchInputs:
    # 还没关联档案的说话人（编号 ≥ 1）→ 他名下的声纹片段 (vector, secs, model_sha256)
    clips: dict[int, list[tuple[bytes, float, str]]]
    linked_profiles: set[int]  # 本场已经关联了的档案
    dismissed: set[tuple[int, int]]  # 主持人忽略过的 (说话人, 档案)
    profiles: list[ProfileSamples]


@dataclass(frozen=True, slots=True)
class Suggestion:
    speaker_idx: int
    profile_id: int
    name: str
    score: float

    def json(self) -> dict:
        return {
            "speaker_idx": self.speaker_idx,
            "profile_id": self.profile_id,
            "name": self.name,
            "score": round(self.score, 2),
        }


def suggest(
    inputs: MatchInputs, *, min_secs: float, threshold: float, margin: float
) -> list[Suggestion]:
    """按上面的规则算出这场会议当前的提示，按说话人编号排序。"""
    candidates: list[Suggestion] = []
    for idx, clips in inputs.clips.items():
        voice = voiceprint(clips)
        if voice is None or voice.secs < min_secs:
            continue
        mine = np.asarray(voice.vector, dtype=np.float64)
        scored: list[tuple[float, ProfileSamples]] = []
        for profile in inputs.profiles:
            if profile.model_sha256 != voice.model_sha256 or profile.id in inputs.linked_profiles:
                continue
            samples = [np.frombuffer(v, dtype="<f4") for v in profile.vectors]
            samples = [v for v in samples if v.size == mine.size]
            if not samples or (idx, profile.id) in inputs.dismissed:
                continue
            scored.append((max(float(np.dot(v, mine)) for v in samples), profile))
        if not scored:
            continue
        scored.sort(key=lambda item: (-item[0], item[1].id))
        best, profile = scored[0]
        second = scored[1][0] if len(scored) > 1 else -1.0
        if best >= threshold and best - second >= margin:
            candidates.append(Suggestion(idx, profile.id, profile.name, best))
    chosen: list[Suggestion] = []
    used: set[int] = set()
    for candidate in sorted(candidates, key=lambda c: (-c.score, c.speaker_idx)):
        if candidate.profile_id not in used:
            used.add(candidate.profile_id)
            chosen.append(candidate)
    return sorted(chosen, key=lambda c: c.speaker_idx)


class MatchStore(Protocol):
    async def voice_match_inputs(self, session_id: str) -> MatchInputs: ...

    async def voice_links(self, session_id: str) -> dict[int, VoiceProfile]: ...

    async def voice_sample_profiles(self, session_id: str) -> set[int]: ...

    async def speaker_voice_clips(
        self, session_id: str, idx: int
    ) -> list[tuple[bytes, float, str]]: ...

    async def enroll_voice(
        self,
        session_id: str,
        idx: int,
        *,
        profile_id: int | None,
        name: str,
        vector: list[float],
        secs: float,
        model_sha256: str,
        now: float | None = None,
    ) -> VoiceProfile: ...


async def session_suggestions(
    store: MatchStore, session_id: str, cfg: SpeakerMemoryConfig
) -> list[Suggestion]:
    inputs = await store.voice_match_inputs(session_id)
    return await asyncio.to_thread(
        suggest,
        inputs,
        min_secs=cfg.min_match_secs,
        threshold=cfg.match_threshold,
        margin=cfg.match_margin,
    )


async def refresh_samples(store: MatchStore, session_id: str) -> None:
    """连接结束时：档案里已经有这场会议的样本的（录入过的，或确认提示时加了样本的），
    用整场的声纹替换那个样本。"""
    profiles = await store.voice_sample_profiles(session_id)
    for idx, profile in (await store.voice_links(session_id)).items():
        if profile.id not in profiles:
            continue
        voice = await asyncio.to_thread(
            voiceprint, await store.speaker_voice_clips(session_id, idx)
        )
        if voice is None:
            continue
        try:
            await store.enroll_voice(
                session_id,
                idx,
                profile_id=profile.id,
                name=profile.name,
                vector=voice.vector,
                secs=voice.secs,
                model_sha256=voice.model_sha256,
            )
        except (LookupError, VoiceModelChanged):
            continue


class VoiceMatcher:
    """一场进行中的会议里的比对：有新片段或说话人有变化时（``poke``）重新比，最多每 5 秒一次；
    提示变了就告诉录音的页面。"""

    def __init__(
        self,
        *,
        store: MatchStore,
        session_id: str,
        cfg: SpeakerMemoryConfig,
        send: Callable[[dict], Awaitable[None]],
    ) -> None:
        self._store = store
        self._session_id = session_id
        self._cfg = cfg
        self._send = send
        self._wake = asyncio.Event()
        self._sent: dict[int, Suggestion] = {}

    def poke(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self.evaluate()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("比对声纹失败，跳过这一轮（转录不受影响）")
            await asyncio.sleep(MATCH_INTERVAL_SECS)

    async def evaluate(self) -> None:
        found = {
            s.speaker_idx: s
            for s in await session_suggestions(self._store, self._session_id, self._cfg)
        }
        for idx in sorted(set(self._sent) - set(found)):
            del self._sent[idx]
            await self._send({"type": "speaker_suggestion_cleared", "speaker_idx": idx})
        for idx, suggestion in found.items():
            before = self._sent.get(idx)
            if before is None or (before.profile_id, before.name) != (
                suggestion.profile_id,
                suggestion.name,
            ):
                self._sent[idx] = suggestion
                await self._send({"type": "speaker_suggestion", **suggestion.json()})
