"""会话时间轴的换算（docs/architecture.md §3）。

会话时间 = 本次连接的起点 ``base_secs`` + 已收到的采样数 / 采样率。新建的会话 ``base_secs`` 恒为 0；
继续一场已有的会议时才设成非零。记录器、助理说话时段、说话人区分的时间换算都通过它，
不要在各处直接写 ``samples / 16000``。
"""

from __future__ import annotations

from dataclasses import dataclass

from agentic_meeting.asr.base import ASR_SAMPLE_RATE


@dataclass(frozen=True, slots=True)
class SessionClock:
    base_secs: float = 0.0
    sample_rate: int = ASR_SAMPLE_RATE

    def now(self, samples: int) -> float:
        return self.base_secs + samples / self.sample_rate


def format_hms(secs: float) -> str:
    """``00:12:05``：会话时间轴上的时:分:秒（召回工具和上下文里的行都用这个格式）。"""
    total = max(0, int(secs))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def context_line(t_secs: float, speaker_name: str, text: str) -> str:
    """追加进实时模型上下文的一行：``[00:12:05 王老师] 文本``（architecture.md §4）。"""
    return f"[{format_hms(t_secs)} {speaker_name}] {text}"
