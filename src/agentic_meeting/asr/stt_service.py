"""识别服务（Pipecat 侧）：把 ``StreamingASR`` 后端接进管线。

行为规则见 docs/interfaces.md §3.4，写法见 docs/pipecat-notes.md §4（已对照 Pipecat 1.12.0 源码）：

* 音频帧：累加采样计数；没有在说话时只写进「预留缓冲」；说话中把音频送给后端。音频帧始终原样放行。
* 开始说话：先把预留缓冲里的音频一并送给后端，避免句首被切掉。
* 停止说话：让后端 ``flush()`` 收尾。
* 后端增量：翻译成临时转录帧和转录帧推向下游。

时间轴（architecture.md §3）：采样计数在基类可能提前返回的路径（被静音、重连、服务不可用）之前做，
计数永不中断；后端起不来或中途失败时，音频照常计数、照常下传，只是不再送给后端，
并在后台按退避间隔重启后端（architecture.md §9「识别服务请求失败」）。
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncGenerator

from loguru import logger
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import STTService
from pipecat.utils.time import time_now_iso8601

from agentic_meeting.asr.base import ASR_SAMPLE_RATE, StreamingASR
from agentic_meeting.types import ASRDelta

# 没收到 finalized 转录帧时，轮次结束判定最多再等这么久（interfaces.md §3.4 第 8 条）。
FINAL_TRANSCRIPT_WAIT_SECS = 0.5
_BYTES_PER_MS = ASR_SAMPLE_RATE * 2 // 1000


class StreamingASRService(STTService):
    """流式识别服务。每路音频流一个实例，后端通过参数注入。"""

    def __init__(
        self,
        *,
        backend: StreamingASR,
        preroll_ms: int,
        retry_base_secs: float = 1.0,
        retry_max_secs: float = 30.0,
        base_secs: float = 0.0,
        **kwargs,
    ):
        kwargs.setdefault("ttfs_p99_latency", FINAL_TRANSCRIPT_WAIT_SECS)
        kwargs.setdefault("settings", STTSettings(model=None, language=None))
        super().__init__(**kwargs)
        self._backend = backend
        self._preroll_bytes = max(0, preroll_ms) * _BYTES_PER_MS
        self._retry_base_secs = retry_base_secs
        self._retry_max_secs = retry_max_secs
        # 继续一场会议时，本次连接在会话时间轴上的起点（architecture.md §3.1）。后端只知道本次连接的音频，
        # 它给的时间在出口统一加上这个起点。
        self._base_secs = base_secs

        self._samples = 0  # 收到的全部采样数，会话时间轴 = 采样数 / 16000
        self._preroll = bytearray()
        self._speaking = False
        self._ready = False  # 后端已启动且可用
        self._consumer: asyncio.Task | None = None
        self._shut_down = False
        self._warned_format = False
        # 当前识别段的状态
        self._segment_stable = ""
        self._carry = ""  # 只有空白的定稿文字，并入下一条转录帧的开头

    @property
    def backend(self) -> StreamingASR:
        return self._backend

    # ---- 生命周期 ----

    async def start(self, frame: StartFrame):
        await super().start(frame)
        # 后端在后台启动：第一次预热可能要十几秒，不能让整条管线的 StartFrame 等着。
        self._consumer = self.create_task(self._run_backend(), name="asr_backend")

    async def stop(self, frame: EndFrame):
        await self._shutdown()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        await self._shutdown()
        await super().cancel(frame)

    async def cleanup(self):
        await self._shutdown()
        await super().cleanup()

    async def _shutdown(self) -> None:
        if self._shut_down:
            return
        self._shut_down = True
        self._ready = False
        task, self._consumer = self._consumer, None
        if task is not None:
            await self.cancel_task(task)
        await self._backend.close()

    # ---- 音频与语音活动事件 ----

    async def process_audio_frame(self, frame: InputAudioRawFrame, direction: FrameDirection):
        # 计数放在基类的静音 / 重连 / 不可用检查之前：时间轴不能因为这些状态停下。
        self._samples += frame.num_frames
        if (frame.sample_rate != ASR_SAMPLE_RATE or frame.num_channels != 1) and (
            not self._warned_format
        ):
            self._warned_format = True
            logger.error(
                f"识别服务收到 {frame.sample_rate} Hz、{frame.num_channels} 声道的音频，"
                f"要求 {ASR_SAMPLE_RATE} Hz 单声道；请检查 PipelineParams.audio_in_sample_rate"
            )
        await super().process_audio_frame(frame, direction)

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        if self._speaking:
            if self._ready:
                await self._safely(self._backend.push_audio(audio, self._end_secs()))
        else:
            self._remember(audio)
        yield None  # 识别结果由后台任务推送，这里不产出帧

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        # 基类已经负责把帧传下去（包括语音活动事件），这里不再手动传。
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            await self._on_speech_started()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            await self._on_speech_stopped()

    def _end_secs(self) -> float:
        return self._samples / ASR_SAMPLE_RATE

    def _remember(self, audio: bytes) -> None:
        """预留缓冲只保留最近 ``preroll_ms`` 的音频。"""
        self._preroll.extend(audio)
        excess = len(self._preroll) - self._preroll_bytes
        if excess > 0:
            del self._preroll[:excess]

    async def _on_speech_started(self) -> None:
        if self._speaking:
            return
        self._speaking = True
        buffered = bytes(self._preroll)
        self._preroll.clear()
        if buffered and self._ready:
            await self._safely(self._backend.push_audio(buffered, self._end_secs()))

    async def _on_speech_stopped(self) -> None:
        if not self._speaking:
            return
        try:
            if self._ready:
                # 等收尾的增量放进队列：保证它先于下一次开始说话处理，不会和新音频交错。
                await self._safely(self._backend.flush())
        finally:
            self._speaking = False
            self._preroll.clear()

    @staticmethod
    async def _safely(awaitable) -> None:
        """后端按约定不会抛异常；万一抛了也不能打断音频的传递（转录链路优先存活）。"""
        try:
            await awaitable
        except Exception:
            logger.exception("识别后端调用出错")

    # ---- 后端的增量 ----

    async def _run_backend(self) -> None:
        """启动后端并消费它的增量；后端失败后按退避间隔重启。"""
        delay = self._retry_base_secs
        recovering = False
        while True:
            try:
                await self._backend.start()
                self._ready = True
                delay = self._retry_base_secs
                if recovering:
                    recovering = False
                    logger.info("识别后端已恢复")
                    await self._notice("info", "识别服务已恢复")
                async for delta in self._backend.deltas():
                    try:
                        await self._handle_delta(delta)
                    except Exception:
                        logger.exception("处理识别增量时出错，已跳过这一条")
                return  # 迭代正常结束：后端已关闭
            except asyncio.CancelledError:
                raise
            except Exception as e:  # ASRBackendError，以及不该出现的其他错误：一律走恢复流程
                self._ready = False
                self._reset_segment()
                logger.warning(f"识别后端不可用：{e}；{delay:.1f} 秒后重试")
                if not recovering:
                    recovering = True
                    await self._notice("warn", "识别服务暂时不可用，正在重试")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._retry_max_secs)

    def _reset_segment(self) -> None:
        self._segment_stable = ""
        self._carry = ""

    async def _handle_delta(self, delta: ASRDelta) -> None:
        """增量 → 帧（规则见 interfaces.md §3.4）。"""
        if self._base_secs:
            delta = dataclasses.replace(
                delta, audio_end_secs=delta.audio_end_secs + self._base_secs
            )
        self._segment_stable += delta.stable_text

        # 1. 临时转录帧：本段已定稿文字 + 未定稿尾巴，供字幕与轮次判定使用。
        #    5. 收尾之后、下一次开始说话之前不再推（临时转录帧会把「转录已定稿」的状态重置）：
        #       收尾的增量本身除外，它的临时帧排在转录帧前面。
        interim = self._segment_stable + delta.unstable_text
        if interim.strip() and (delta.segment_end or self._speaking):
            await self._push_text(InterimTranscriptionFrame, interim, delta)

        # 2–4. 定稿文字推转录帧；只有空白的并入下一条非空增量的开头，绝不推空白文本。
        stable = delta.stable_text
        if stable.strip():
            text, self._carry = self._carry + stable, ""
            await self._push_text(TranscriptionFrame, text, delta, finalized=delta.segment_end)
        elif stable:
            self._carry += stable

        if delta.segment_end:
            self._reset_segment()

    async def _push_text(self, frame_cls, text: str, delta: ASRDelta, **kwargs) -> None:
        frame = frame_cls(
            text=text, user_id="", timestamp=time_now_iso8601(), result=delta, **kwargs
        )
        # 否则用户侧聚合器会在各条转录之间各加一个空格，一句中文会变成「这个问题 我们问 一下」。
        frame.includes_inter_frame_spaces = True
        await self.push_frame(frame)

    async def _notice(self, level: str, text: str) -> None:
        try:
            await self.push_frame(
                RTVIServerMessageFrame(data={"type": "notice", "level": level, "text": text})
            )
        except Exception:
            logger.exception("推送提示消息失败")
