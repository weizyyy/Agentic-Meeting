"""声纹档案：主持人征得本人同意后录入的人（docs/interfaces.md §2.8、§5.14）。

一份档案有名字和最多 ``MAX_SAMPLES`` 个样本；一个样本是某场会议里这个人的声纹。
一位说话人在一场会议里的声纹，是他名下各条发言的声纹片段按时长加权平均、再归一化的结果。
向量只在服务端用来计算，从不出现在接口、导出、模型上下文或日志里。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

MAX_SAMPLES = 8  # 每份档案保留最近这么多场会议的样本


@dataclass(frozen=True, slots=True)
class VoiceProfile:
    id: int
    name: str
    model_sha256: str
    dimensions: int
    samples: int  # 样本个数
    secs: float  # 各样本的语音时长之和
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class Voiceprint:
    """一位说话人在一场会议里的声纹。"""

    vector: list[float]
    secs: float
    model_sha256: str


class VoiceProfileInUse(Exception):
    """这份档案已经关联到同一场会议的另一位说话人。"""


class VoiceModelChanged(Exception):
    """档案是用另一个声纹模型录入的，不能和现在的声纹放在一起。"""


def voiceprint(clips: Sequence[tuple[bytes, float, str]]) -> Voiceprint | None:
    """``(vector, secs, model_sha256)`` 若干个片段 → 声纹。

    片段可能来自不同的模型（中途换过模型）：只用语音最多的那个模型的片段，不同模型的向量不能混在一起。
    没有片段时返回 None。
    """
    totals: dict[str, float] = {}
    for _, secs, model in clips:
        totals[model] = totals.get(model, 0.0) + secs
    if not totals:
        return None
    model = max(sorted(totals), key=lambda m: totals[m])
    chosen = [(np.frombuffer(v, dtype="<f4"), s) for v, s, m in clips if m == model]
    weights = np.array([s for _, s in chosen], dtype=np.float64)
    vectors = np.stack([v.astype(np.float64) for v, _ in chosen])
    mean = (vectors * weights[:, None]).sum(axis=0)
    norm = float(np.linalg.norm(mean))
    if not np.isfinite(norm) or norm == 0.0:
        return None
    return Voiceprint((mean / norm).tolist(), float(weights.sum()), model)
