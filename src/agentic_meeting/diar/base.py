"""说话人区分后端的统一接口。接口约定见 docs/interfaces.md §4。"""

from __future__ import annotations

from typing import Protocol

from agentic_meeting.types import SpeakerSegment


class Diarizer(Protocol):
    """一路音频流对应一个实例。

    底层推理是阻塞调用，且同一条流不允许并发访问：实现必须把所有底层调用串行地放到
    同一个工作线程里（例如单线程的 ThreadPoolExecutor），不能阻塞事件循环。
    """

    @property
    def max_speakers(self) -> int:
        """模型能区分的说话人上限。"""
        ...

    async def start(self) -> None: ...

    async def push_audio(self, pcm16: bytes) -> None:
        """送入 16 kHz 单声道 s16le 音频。必须按时间顺序、不丢不重地送入全部音频
        （包括静音），否则输出的时间轴会与会话时间轴错位。"""
        ...

    async def segments(self, since_secs: float = 0.0) -> list[SpeakerSegment]:
        """返回结束时间晚于 ``since_secs`` 的全部分段，按开始时间排序。

        流式模型会修订最近几秒的结论，所以调用方每次都应重新读取「最近一段」，
        而不是假设之前读到的分段不再变化。
        """
        ...

    async def close(self) -> None: ...


class NullDiarizer:
    """``diarization.backend = "none"`` 时使用：不做任何判定。"""

    max_speakers = 0

    async def start(self) -> None:
        return None

    async def push_audio(self, pcm16: bytes) -> None:
        return None

    async def segments(self, since_secs: float = 0.0) -> list[SpeakerSegment]:
        return []

    async def close(self) -> None:
        return None
