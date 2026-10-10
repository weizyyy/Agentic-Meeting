"""会议记录器（docs/architecture.md §4）。

它放在识别服务之后、用户侧聚合器**之前**：Pipecat 的唤醒策略在未唤醒时会清空转录文本，记录器要先于它拿到
每一条转录。它做这些事：

* 数音频采样，维护会话时间轴（``SessionClock``）；把全部音频（含静音）按顺序、分批送给说话人区分——放在后台任务里，
  不等结果，不阻塞音频帧；
* 每个识别增量：查最近的说话人分段 → ``TranscriptAssembler`` 归属并切分 → 字幕消息（``caption``）、
  发言定稿（落库、``utterance`` 消息、向实时模型的上下文追加一行 ``[时:分:秒 说话人] 文本``）；
* 发言落库后 5 秒内每秒重新核对一次说话人，变了就更新数据库并发 ``utterance_update``；
* 助理说话的起止（``BotStarted/StoppedSpeakingFrame``，方向是向上游）告诉 assembler，丢弃回声；
  助理自己的话、浏览器里键入的文字也由它落库（``record_assistant`` / ``record_typed``）。

**转录链路优先存活**：任何一环出错（存储、说话人区分、字幕）都只记日志，帧照常放行；落库失败的发言进重试队列，
下一次写入时按原顺序补写。

顺序要点：``TranscriptionFrame`` 是先处理、后放行的——先追加上下文行，再把触发用户轮次的转录帧放行，
这样模型作答时上下文里已经有这句话的完整版本；临时转录帧（只给界面用）则先放行、后处理。
"""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any, Protocol

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    LLMMessagesAppendFrame,
    LLMMessagesUpdateFrame,
    StartFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame

from agentic_meeting.asr.base import ASR_SAMPLE_RATE
from agentic_meeting.diar.base import Diarizer, NullDiarizer
from agentic_meeting.diar.fusion import (
    MERGE_MAX_CHARS,
    MERGE_SOFT_CHARS,
    CaptionUpdate,
    TranscriptAssembler,
    UtteranceFinal,
    join_fragments,
    should_merge,
)
from agentic_meeting.pipeline.clock import SessionClock, context_line
from agentic_meeting.store.db import default_speaker_name
from agentic_meeting.types import (
    SPEAKER_ASSISTANT,
    SPEAKER_TYPED,
    SPEAKER_UNKNOWN,
    ASRDelta,
    SpeakerSegment,
    Utterance,
)

UNKNOWN_SPEAKER_NAME = "未知"

SEGMENT_LOOKBACK_SECS = 15.0  # 每次只取最近这么久的说话人分段
DIAR_BATCH_MS = 160  # 送给说话人区分的音频攒够这么久再发一次
DIAR_BACKLOG_WARN_SECS = 5.0  # 说话人区分落后这么多音频时告警
RECHECK_INTERVAL_SECS = 1.0
RECHECK_ATTEMPTS = 5  # 落库后 5 秒内每秒核对一次（interfaces.md §4.3 第 5 条）
DRAIN_TIMEOUT_SECS = 5.0
_BYTES_PER_SEC = ASR_SAMPLE_RATE * 2


class RecorderStore(Protocol):
    """记录器用到的存储接口（``store.db.Store`` 满足它；测试里换成假的）。"""

    async def add_utterance(self, utterance: Utterance) -> int: ...

    async def speaker_name(self, session_id: str, idx: int) -> str: ...

    async def update_utterance_speaker(
        self, utterance_id: int, speaker_idx: int, *, session_id: str, write_token: str
    ) -> bool: ...


class MeetingRecorder(FrameProcessor):
    def __init__(
        self,
        *,
        store: RecorderStore | None = None,
        diarizer: Diarizer | None = None,
        assembler: TranscriptAssembler | None = None,
        session_id: str = "",
        clock: SessionClock | None = None,
        assistant_name: str = "助理",
        diar_batch_ms: int = DIAR_BATCH_MS,
        recheck_interval_secs: float = RECHECK_INTERVAL_SECS,
        recheck_attempts: int = RECHECK_ATTEMPTS,
        merge_gap_secs: float = 0.0,
        merge_soft_chars: int = MERGE_SOFT_CHARS,
        merge_max_chars: int = MERGE_MAX_CHARS,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._store = store
        self._diarizer = diarizer
        self._assembler = assembler or TranscriptAssembler()
        self._session_id = session_id
        self._clock = clock or SessionClock()
        self._assistant_name = assistant_name
        self._recheck_interval = recheck_interval_secs
        self._recheck_attempts = recheck_attempts
        # 相邻片段并成一条（diar/fusion.py 规则 6）。0 = 不并；正式运行时来自配置的 [transcript] 段。
        self._merge_gap = merge_gap_secs
        self._merge_soft = merge_soft_chars
        self._merge_max = merge_max_chars
        self._last_asr: Utterance | None = (
            None  # 最近落库的那条识别发言；中间隔了助理的话或键入的文字就清掉
        )

        self._samples = 0  # 与识别服务看到的是同一串音频帧
        self._last_delta: ASRDelta | None = None
        self._caption_lag: float | None = None
        self._caption_sampled_at: float | None = None
        self._names: dict[int, str] = {}
        self._unsaved: list[Utterance] = []  # 落库失败、等下一次补写的发言（保持原顺序）
        self._bot_started_at: float | None = None
        # 助理开始 / 停止说话时调用（文字入口要等助理说完再交出排队的消息）；参数是「是否正在说话」。
        self.on_bot_speaking_changed: Callable[[bool], Awaitable[None]] | None = None
        self._assistant_turn_started_at: float | None = None
        self._tasks: set[asyncio.Task] = set()
        self._finished = False
        # 「现在没有人在说话」：任务完成后的口头简报要等它
        self._quiet = asyncio.Event()
        self._quiet.set()
        # 最近一条定稿发言的说话人（委托任务时记作交办人）
        self.last_speaker_idx: int = SPEAKER_UNKNOWN
        # 「往上下文追加一行」和「整体重建上下文」互斥：重建时从数据库读到的内容，和这之后才追加的行不能交错，
        # 否则刚落库的发言可能既不在重建的结果里、又被重建盖掉。
        self._context_lock = asyncio.Lock()

        # 说话人区分的喂入
        self._diar_batch_bytes = max(1, diar_batch_ms) * _BYTES_PER_SEC // 1000
        self._audio_buf = bytearray()
        self._audio_queue: asyncio.Queue[bytes | None] | None = None
        self._audio_task: asyncio.Task | None = None
        self._queued_bytes = 0
        self._diar_failed = False
        self._warned_backlog = False
        self._warned_segments = False

    # ------------------------------------------------------------------ #
    # Pipecat 入口
    # ------------------------------------------------------------------ #

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if direction == FrameDirection.UPSTREAM:
            await self.push_frame(frame, direction)
            self._observe_upstream(frame)
            return

        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            self._begin()
        elif isinstance(frame, EndFrame):
            await self._finish()
            await self.push_frame(frame, direction)
        elif isinstance(frame, CancelFrame):
            await self._abort()
            await self.push_frame(frame, direction)
        elif isinstance(frame, TranscriptionFrame):
            # 先处理（追加上下文行）、后放行，见模块说明。
            await self._guard(self._handle_delta(frame))
            await self.push_frame(frame, direction)
        else:
            await self.push_frame(frame, direction)  # 其余的先放行，再做自己的事
            if isinstance(frame, InputAudioRawFrame):
                self._on_audio(frame)
            elif isinstance(frame, VADUserStartedSpeakingFrame):
                self._quiet.clear()
                self._on_speech_started(frame)
            elif isinstance(frame, VADUserStoppedSpeakingFrame):
                self._quiet.set()
            elif isinstance(frame, InterimTranscriptionFrame):
                await self._guard(self._handle_delta(frame))

    async def cleanup(self):
        await self._abort()
        await super().cleanup()

    # ------------------------------------------------------------------ #
    # 外部接口
    # ------------------------------------------------------------------ #

    @property
    def elapsed_secs(self) -> float:
        """已经收到的音频在会话时间轴上走到了哪里（连接结束时写进连接记录的 ``t_to``）。"""
        return self._now()

    @property
    def caption_lag_seconds(self) -> float | None:
        """最近一次成功输出非空识别字幕时的音频积压估计；无有效样本为 None。"""
        return self._caption_lag

    @property
    def caption_sample_age_seconds(self) -> float | None:
        """有效字幕样本的单调时钟年龄；静音期间不重算积压。"""
        if self._caption_sampled_at is None:
            return None
        return max(0.0, monotonic() - self._caption_sampled_at)

    @property
    def unsaved_utterance_count(self) -> int:
        """等待下次落库重试的发言数。"""
        return len(self._unsaved)

    def set_speaker_name(self, idx: int, name: str) -> None:
        """说话人改名后调用，之后的字幕和上下文行用新名字。"""
        self._names[idx] = name

    def assistant_turn_started(self) -> None:
        self._assistant_turn_started_at = self._now()

    async def record_assistant(self, text: str) -> Utterance | None:
        """助理这一轮说的话落库（``source="assistant"``）并通知界面。"""
        text = text.strip()
        if not text:
            return None
        now = self._now()
        started = self._assistant_turn_started_at
        self._assistant_turn_started_at = None
        self._last_asr = None  # 助理插了话：后面的发言另起一条
        utterance = Utterance(
            self._session_id,
            SPEAKER_ASSISTANT,
            min(started if started is not None else now, now),
            now,
            text,
            source="assistant",
        )
        await self._persist_and_announce(utterance, segment_id=None)
        return utterance

    async def record_typed(self, text: str) -> Utterance | None:
        """浏览器输入框里键入的文字：落库（``source="text"``，说话人 −2，算作对助理说的）并通知界面。

        不追加上下文——调用方（文字入口 ``text_input.py``）自己决定怎么把这一行交给模型，行文本用 ``context_text``。
        """
        text = text.strip()
        if not text:
            return None
        now = self._now()
        self._last_asr = None
        utterance = Utterance(
            self._session_id,
            SPEAKER_TYPED,
            now,
            now,
            text,
            source="text",
            addressed_to_assistant=True,
        )
        await self._persist_and_announce(utterance, segment_id=None)
        return utterance

    @property
    def someone_speaking(self) -> bool:
        return not self._quiet.is_set()

    async def wait_quiet(self, max_wait_secs: float) -> bool:
        """等到没有人在说话，最多等 ``max_wait_secs`` 秒。返回是否真的等到了（超时返回 ``False``）。"""
        try:
            await asyncio.wait_for(self._quiet.wait(), max_wait_secs)
        except TimeoutError:
            return False
        return True

    async def append_context_line(self, line: str) -> None:
        """向实时模型的上下文追加一行（不触发应答）。画面摘要走这里，和发言的追加排在同一个顺序里。"""
        async with self._context_lock:
            await self.push_frame(
                LLMMessagesAppendFrame(messages=[{"role": "user", "content": line}], run_llm=False)
            )

    async def rebuild_context(self, build: Callable[[], Awaitable[list[dict[str, Any]]]]) -> int:
        """整体替换实时模型的上下文（压缩用）。``build`` 从数据库生成新的消息列表。

        在锁里「读数据库 → 推替换帧」：这期间落库的发言要等替换帧发出去之后才追加，所以不会丢，也不会重复。
        返回新上下文的消息条数。
        """
        async with self._context_lock:
            messages = await build()
            await self.push_frame(LLMMessagesUpdateFrame(messages=messages, run_llm=False))
            return len(messages)

    async def context_text(self, utterance: Utterance) -> str:
        """这条发言在实时模型上下文里的样子：``[00:12:05 王老师] 文本``。"""
        return context_line(
            utterance.t_start, await self._display_name(utterance.speaker_idx), utterance.text
        )

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def _begin(self) -> None:
        if self._diarizer is None or isinstance(self._diarizer, NullDiarizer):
            return
        if self._audio_task is None:
            self._audio_queue = asyncio.Queue()
            self._audio_task = self.create_task(self._feed_diarizer(), name="diarizer_feed")

    async def _finish(self) -> None:
        """EndFrame：把没送完的音频送完，补写没落库的发言，停掉后台任务。"""
        if self._finished:
            return
        self._finished = True
        if self._audio_task is not None and self._audio_queue is not None:
            self._flush_audio_buffer()
            self._audio_queue.put_nowait(None)
            task, self._audio_task = self._audio_task, None
            try:
                await asyncio.wait_for(task, DRAIN_TIMEOUT_SECS)
            except (TimeoutError, asyncio.CancelledError, Exception):
                logger.warning("说话人区分没能在结束前处理完剩余的音频")
        await self._guard(self._write_unsaved())
        await self._cancel_tasks()

    async def _abort(self) -> None:
        self._finished = True
        task, self._audio_task = self._audio_task, None
        if task is not None:
            await self.cancel_task(task)
        await self._cancel_tasks()

    async def _cancel_tasks(self) -> None:
        for task in list(self._tasks):
            await self.cancel_task(task)
        self._tasks.clear()

    def _spawn(self, coro: Awaitable[None], name: str) -> None:
        task = self.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ #
    # 时间与名字
    # ------------------------------------------------------------------ #

    def _now(self) -> float:
        return self._clock.now(self._samples)

    async def _display_name(self, idx: int) -> str:
        cached = self._names.get(idx)
        if cached is not None:
            return cached
        name = default_speaker_name(idx, self._assistant_name)
        if idx == SPEAKER_UNKNOWN:
            name = UNKNOWN_SPEAKER_NAME
        if self._store is not None and self._session_id:
            try:
                name = await self._store.speaker_name(self._session_id, idx)
            except Exception:
                logger.exception("读取说话人显示名失败，用默认名")
        self._names[idx] = name
        return name

    # ------------------------------------------------------------------ #
    # 容错与消息
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _guard(coro: Awaitable[Any]) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("会议记录器处理失败，已跳过（转录链路不受影响）")

    async def _send(self, data: dict) -> None:
        await self.push_frame(RTVIServerMessageFrame(data=data))

    async def _notice(self, level: str, text: str) -> None:
        await self._send({"type": "notice", "level": level, "text": text})

    # ------------------------------------------------------------------ #
    # 音频与说话人区分
    # ------------------------------------------------------------------ #

    def _on_audio(self, frame: InputAudioRawFrame) -> None:
        self._samples += frame.num_frames
        if self._audio_queue is None or self._diar_failed:
            return
        if frame.sample_rate != ASR_SAMPLE_RATE or frame.num_channels != 1:
            return  # 格式不对的音频识别服务已经报过错，这里不再重复
        self._audio_buf += frame.audio
        if len(self._audio_buf) >= self._diar_batch_bytes:
            self._flush_audio_buffer()

    def _flush_audio_buffer(self) -> None:
        if not self._audio_buf or self._audio_queue is None:
            return
        chunk = bytes(self._audio_buf)
        self._audio_buf.clear()
        self._queued_bytes += len(chunk)
        self._audio_queue.put_nowait(chunk)
        if (
            self._queued_bytes / _BYTES_PER_SEC > DIAR_BACKLOG_WARN_SECS
            and not self._warned_backlog
        ):
            self._warned_backlog = True
            logger.warning(
                f"说话人区分落后了超过 {DIAR_BACKLOG_WARN_SECS:.0f} 秒的音频，说话人标签会明显延迟"
            )

    async def _feed_diarizer(self) -> None:
        assert self._audio_queue is not None and self._diarizer is not None
        while True:
            chunk = await self._audio_queue.get()
            if chunk is None:
                return
            self._queued_bytes -= len(chunk)
            if self._diar_failed:
                continue  # 已经降级：丢掉，免得积压
            try:
                await self._diarizer.push_audio(chunk)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._diar_failed = True
                logger.exception("说话人区分出错，之后的发言不再区分说话人")
                await self._guard(
                    self._notice("warn", f"说话人区分出错，之后的发言将记为「未知」：{e}")
                )

    async def _segments(self, since_secs: float) -> list[SpeakerSegment]:
        if self._diarizer is None or self._diar_failed or isinstance(self._diarizer, NullDiarizer):
            return []
        try:
            return list(await self._diarizer.segments(max(0.0, since_secs)))
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._warned_segments:
                self._warned_segments = True
                logger.exception("读取说话人分段失败，这一批发言暂记为未知")
            return []

    # ------------------------------------------------------------------ #
    # 帧事件
    # ------------------------------------------------------------------ #

    def _on_speech_started(self, frame: VADUserStartedSpeakingFrame) -> None:
        # VAD 是说话 start_secs 之后才确认的，这一段从确认时刻往前推。
        self._assembler.on_speech_started(max(0.0, self._now() - frame.start_secs))

    def _observe_upstream(self, frame: Frame) -> None:
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_started_at = self._now()
            self._assembler.on_bot_speaking(self._bot_started_at, None)
            self._notify_bot_speaking(True)
        elif isinstance(frame, BotStoppedSpeakingFrame) and self._bot_started_at is not None:
            self._assembler.on_bot_speaking(self._bot_started_at, self._now())
            self._bot_started_at = None
            self._notify_bot_speaking(False)

    def _notify_bot_speaking(self, speaking: bool) -> None:
        callback = self.on_bot_speaking_changed
        if callback is not None:
            self._spawn(self._guard(callback(speaking)), "bot_speaking_callback")

    async def _handle_delta(self, frame: InterimTranscriptionFrame | TranscriptionFrame) -> None:
        delta = frame.result if isinstance(frame.result, ASRDelta) else None
        if delta is None or delta is self._last_delta:
            return  # 不是我们的识别服务发的，或这个增量已经通过它的临时转录帧处理过了
        self._last_delta = delta
        segments = await self._segments(self._now() - SEGMENT_LOOKBACK_SECS)
        sampled = False
        for event in self._assembler.on_delta(delta, segments):
            if isinstance(event, CaptionUpdate):
                await self._send(await self._caption_message(event))
                if not sampled and (event.stable + event.unstable).strip():
                    sampled = True
                    self._sample_caption(delta)
            else:
                await self._on_final(event)

    def _sample_caption(self, delta: ASRDelta) -> None:
        """每个增量仅尝试一次；拒绝异常时间轴而保留旧样本，不影响字幕和定稿。"""
        try:
            elapsed = self.elapsed_secs
            raw_lag = elapsed - delta.audio_end_secs
            if (
                math.isfinite(elapsed)
                and math.isfinite(delta.audio_end_secs)
                and math.isfinite(raw_lag)
                and raw_lag >= -1 / ASR_SAMPLE_RATE
            ):
                sampled_at = monotonic()
                self._caption_lag = max(0.0, raw_lag)
                self._caption_sampled_at = sampled_at
        except Exception:
            logger.warning("字幕指标采样失败，保留上次样本")

    async def _caption_message(self, c: CaptionUpdate) -> dict:
        return {
            "type": "caption",
            "segment_id": c.segment_id,
            "speaker_idx": c.speaker_idx,
            "speaker_name": await self._display_name(c.speaker_idx),
            "t_start": c.t_start,
            "stable": c.stable,
            "unstable": c.unstable,
        }

    # ------------------------------------------------------------------ #
    # 发言定稿
    # ------------------------------------------------------------------ #

    async def _on_final(self, final: UtteranceFinal) -> None:
        utterance = Utterance(
            self._session_id,
            final.speaker_idx,
            final.t_start,
            final.t_end,
            final.text,
            source="asr",
        )
        self.last_speaker_idx = final.speaker_idx
        async with self._context_lock:  # 落库和追加上下文行是一个整体，见 _context_lock
            merged = await self._merge_into_previous(final)
            if merged is not None:
                utterance = merged
            else:
                await self._persist_and_announce(utterance, segment_id=final.segment_id)
                self._last_asr = utterance
            # 先追加上下文行，后面紧跟着放行触发用户轮次的那条转录帧。并进上一条的片段也照样追加自己这一行：
            # 上下文里已有的行不回改，模型看到的是按时间排的几行，内容不缺。
            line = context_line(
                final.t_start, await self._display_name(utterance.speaker_idx), final.text
            )
            await self.push_frame(
                LLMMessagesAppendFrame(messages=[{"role": "user", "content": line}], run_llm=False)
            )
        if utterance.id is not None:
            self._spawn(self._recheck(utterance), f"recheck_{utterance.id}")

    async def _merge_into_previous(self, final: UtteranceFinal) -> Utterance | None:
        """新定稿的片段并进上一条发言（规则见 ``should_merge``）。并了返回那条发言，没并返回 ``None``。

        并的时候：数据库里那一行的文字和结束时间更新；给页面发一条 ``utterance``，``id`` 是原来那条的、
        ``segment_id`` 是这个片段的——页面据此把灰色的实时字幕行收掉，把原来那行的文字换成并好的。
        """
        prev = self._last_asr
        if prev is None or prev.id is None or self._store is None:
            return None
        extend = getattr(self._store, "extend_utterance", None)
        if extend is None or not should_merge(
            prev_speaker=prev.speaker_idx,
            prev_text=prev.text,
            prev_end=prev.t_end,
            speaker=final.speaker_idx,
            t_start=final.t_start,
            gap_secs=self._merge_gap,
            soft_chars=self._merge_soft,
            max_chars=self._merge_max,
        ):
            return None
        text = join_fragments(prev.text, final.text)
        t_end = max(prev.t_end, final.t_end)
        try:
            next_token = uuid.uuid4().hex
            if not await extend(
                prev.id,
                text=text,
                t_end=t_end,
                session_id=prev.session_id,
                write_token=prev.write_token,
                next_token=next_token,
            ):
                return None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("把片段并进上一条发言失败，改为另存一条")
            return None
        prev.text, prev.t_end, prev.write_token = text, t_end, next_token
        if prev.speaker_idx == SPEAKER_UNKNOWN and final.speaker_idx != SPEAKER_UNKNOWN:
            # 上一段太短，当时说话人还没有结论；这一段有了
            try:
                if await self._store.update_utterance_speaker(
                    prev.id,
                    final.speaker_idx,
                    session_id=prev.session_id,
                    write_token=prev.write_token,
                ):
                    prev.speaker_idx = final.speaker_idx
            except Exception:
                logger.exception("补上发言的说话人失败")
        await self._send(
            {
                "type": "utterance",
                "id": prev.id,
                "segment_id": final.segment_id,
                "speaker_idx": prev.speaker_idx,
                "speaker_name": await self._display_name(prev.speaker_idx),
                "t_start": prev.t_start,
                "t_end": prev.t_end,
                "text": prev.text,
                "source": prev.source,
            }
        )
        return prev

    async def _persist_and_announce(self, utterance: Utterance, *, segment_id: int | None) -> None:
        await self._guard(self._write(utterance))
        await self._send(
            {
                "type": "utterance",
                "id": utterance.id,
                "segment_id": segment_id,
                "speaker_idx": utterance.speaker_idx,
                "speaker_name": await self._display_name(utterance.speaker_idx),
                "t_start": utterance.t_start,
                "t_end": utterance.t_end,
                "text": utterance.text,
                "source": utterance.source,
            }
        )

    async def _write(self, utterance: Utterance) -> None:
        """落库；失败的（连同之前失败的）留在重试队列里，下一次写入时按原顺序补写。"""
        if self._store is None:
            return
        queue = [*self._unsaved, utterance]
        self._unsaved = []
        for position, item in enumerate(queue):
            try:
                await self._store.add_utterance(item)
            except asyncio.CancelledError:
                self._unsaved = queue[position:]
                raise
            except Exception:
                logger.exception("发言落库失败，已放进重试队列，稍后补写")
                self._unsaved = queue[position:]
                return

    async def _write_unsaved(self) -> None:
        if self._unsaved:
            queue, self._unsaved = self._unsaved, []
            for position, item in enumerate(queue):
                try:
                    await self._store.add_utterance(item)  # type: ignore[union-attr]
                except Exception:
                    logger.exception(f"结束时仍有 {len(queue) - position} 条发言没能落库")
                    self._unsaved = queue[position:]
                    return

    # ------------------------------------------------------------------ #
    # 事后更正
    # ------------------------------------------------------------------ #

    async def _recheck(self, utterance: Utterance) -> None:
        """落库后每隔 ``recheck_interval`` 秒重新归属一次，最多 ``recheck_attempts`` 次。

        每次都按这条发言**当时**的时间范围算：后面的片段并进来之后，范围跟着变长。
        """
        for _ in range(self._recheck_attempts):
            await asyncio.sleep(self._recheck_interval)
            token = utterance.write_token
            current = UtteranceFinal(
                0, utterance.speaker_idx, utterance.t_start, utterance.t_end, utterance.text
            )
            segments = await self._segments(current.t_start - 1.0)
            new_idx = self._assembler.recheck(current, segments)
            if new_idx is None or utterance.id is None or self._store is None:
                continue
            try:
                updated = await self._store.update_utterance_speaker(
                    utterance.id,
                    new_idx,
                    session_id=utterance.session_id,
                    write_token=token,
                )
            except Exception:
                logger.exception("更正发言的说话人失败")
                continue
            if not updated:
                return
            utterance.speaker_idx = new_idx
            await self._guard(
                self._send(
                    {
                        "type": "utterance_update",
                        "id": utterance.id,
                        "speaker_idx": new_idx,
                        "speaker_name": await self._display_name(new_idx),
                    }
                )
            )
