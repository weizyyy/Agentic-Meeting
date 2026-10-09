"""流式语音识别后端的统一接口。

Pipecat 侧的 STT 服务（asr/stt_service.py，待实现）只依赖这个接口，不关心具体后端。
新增后端 = 新增一个实现本协议的类，并在 asr/__init__.py 的工厂函数里按
``config.asr.backend`` 分派。接口约定见 docs/interfaces.md §3。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from agentic_meeting.types import ASRDelta

ASR_SAMPLE_RATE = 16000  # 所有后端统一吃 16 kHz 单声道 s16le


class ASRBackendError(RuntimeError):
    """识别后端无法继续工作：预热失败，或连续多次请求失败。

    后端出现这种错误后会停止处理音频；调用方可以再调一次 :meth:`StreamingASR.start`
    让同一个实例重新开始（见 architecture.md §9「识别服务请求失败」）。
    """


class StreamingASR(Protocol):
    """一路音频流对应一个实例；方法都在事件循环线程里调用，不要求线程安全。"""

    async def start(self) -> None:
        """建立连接/加载资源。失败时抛 :class:`ASRBackendError`，由调用方决定是否重试。

        后端因连续失败而停止之后，再次调用本方法会清空内部状态并重新开始。
        """
        ...

    async def push_audio(self, pcm16: bytes, audio_end_secs: float) -> None:
        """送入一段 16 kHz 单声道 s16le 音频。

        ``audio_end_secs`` 是这段音频末尾在会话时间轴上的位置，由调用方按采样数计算后
        传入；后端只负责把它原样带回 :class:`ASRDelta`，不自己计时。
        本方法必须立即返回：识别在后台任务里进行，结果从 :meth:`deltas` 取。
        """
        ...

    def deltas(self) -> AsyncIterator[ASRDelta]:
        """按产生顺序异步迭代识别增量；:meth:`close` 之后迭代结束。

        后端无法继续工作时，迭代在取完已产生的增量之后抛出 :class:`ASRBackendError`。
        这是唯一的失败通道：``push_audio`` 和 ``flush`` 都不会因此抛异常。
        """
        ...

    async def flush(self) -> None:
        """段落收尾：把尚未定稿的尾巴定稿，清空音频窗口与前缀。

        在检测到停顿（VAD 判定说话结束）时调用。收尾产生的增量同样从 :meth:`deltas`
        发出，并带 ``segment_end=True``。没有待处理内容时也要发一条空的 segment_end 增量，
        让下游能可靠地结束当前发言。

        返回时收尾的增量已经放进 :meth:`deltas` 的队列（在途的请求会先等它结束）。
        """
        ...

    async def close(self) -> None:
        """释放资源。可重复调用。"""
        ...
