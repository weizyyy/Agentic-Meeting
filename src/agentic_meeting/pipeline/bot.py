"""管线组装：把识别、记录、唤醒与轮次判定、实时模型、语音合成接成一条管线并运行。

处理器顺序见 docs/architecture.md §4，Pipecat 接口见 docs/pipecat-notes.md §1–§5。

* ``build_parts`` 按配置造出各个处理器（外部依赖可注入，测试里换成假的）；
* ``pipeline_processors`` 把它们和传输的输入输出按固定顺序排好；
* ``run_bot`` 为一次浏览器连接建传输、组管线、运行到连接断开。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.observers.loggers.metrics_log_observer import MetricsLogObserver
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMAssistantAggregator,
    LLMContextAggregatorPair,
    LLMUserAggregator,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_start import TranscriptionUserTurnStartStrategy, VADUserTurnStartStrategy
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from agentic_meeting.asr import build_asr_backend
from agentic_meeting.asr.base import ASR_SAMPLE_RATE, StreamingASR
from agentic_meeting.asr.stt_service import StreamingASRService
from agentic_meeting.audio.gain import InputGainFilter
from agentic_meeting.config import AppConfig
from agentic_meeting.diar import build_diarizer
from agentic_meeting.diar.base import Diarizer
from agentic_meeting.diar.fusion import TranscriptAssembler
from agentic_meeting.pipeline.activity import AssistantActivity
from agentic_meeting.pipeline.background import BackgroundModel
from agentic_meeting.pipeline.clock import SessionClock
from agentic_meeting.pipeline.context import ContextManager, build_context_messages
from agentic_meeting.pipeline.errors import ErrorNotifier
from agentic_meeting.pipeline.modality import ModalityGate
from agentic_meeting.pipeline.prompts import load_prompt
from agentic_meeting.pipeline.recorder import MeetingRecorder, RecorderStore
from agentic_meeting.pipeline.services import (
    LocalTTSService,
    RealtimeLLMService,
    build_realtime_llm,
    build_tts,
)
from agentic_meeting.pipeline.session import LiveConnection, SessionManager, SessionNotFound
from agentic_meeting.pipeline.text_input import TextInputHandler, extract_text
from agentic_meeting.pipeline.tools import realtime_tools
from agentic_meeting.pipeline.wake import WakeWordUserTurnStartStrategy
from agentic_meeting.screen.ingest import FrameIngestor
from agentic_meeting.store.db import SessionBusy, Store
from agentic_meeting.store.embeddings import EmbeddingClient
from agentic_meeting.store.retention import RetentionWorker
from agentic_meeting.store.work import drain_io
from agentic_meeting.types import Session

# 不用轮次结束模型时，停顿多久算说完（pipecat-notes.md §3.2）。
SPEECH_TIMEOUT_SECS = 0.6
# 字幕行编号（segment_id）每次连接占一段：连接记录的编号 × 这个数 + 1 起。页面断线重连后还留着上一次连接的字幕行，
# 编号要是每次都从 1 起，新连接的字幕会被当成早已定稿的旧行而不显示。
SEGMENT_IDS_PER_CONNECTION = 1_000_000


@dataclass
class AppResources:
    """整个应用共享的资源：配置、存储、会话管理、截图接收、后台模型、任务管理器等。

    ``store`` / ``sessions`` / ``frames`` 由 ``web/app.py`` 的 lifespan 创建；不给时 ``run_bot`` 不落库、不管理会话（测试用）。
    """

    cfg: AppConfig
    store: Store | None = None
    sessions: SessionManager | None = None
    frames: FrameIngestor | None = None  # 截图接收
    embedder: EmbeddingClient | None = None  # 查询的嵌入；没有就只按关键词召回
    # 后台模型的入口。background 给滚动纪要用，也可能同时是画面摘要用的那个；
    # background_models 是要在助理应答时暂停的全部入口。
    background: BackgroundModel | None = None
    digests: Any = None  # 滚动纪要：DigestWorker
    tasks: Any = None  # 后台任务：TaskManager；没有就不能委托
    reports: Any = None  # 会后报告：ReportWorker
    background_models: list[BackgroundModel] = field(default_factory=list)
    retention: RetentionWorker | None = None
    captions: Any = None  # 画面摘要：要用到 submit(IngestedFrame)


@dataclass
class BotParts:
    """一次连接用到的全部处理器。"""

    vad_analyzer: SileroVADAnalyzer
    vad: VADProcessor
    asr: StreamingASRService
    recorder: MeetingRecorder
    wake: WakeWordUserTurnStartStrategy
    user_aggregator: LLMUserAggregator
    llm: RealtimeLLMService
    tts: LocalTTSService | None
    assistant_aggregator: LLMAssistantAggregator
    context: LLMContext  # 实时模型的上下文（两个聚合器共用的那一份）
    gate: ModalityGate  # 模态闸门（紧挨在实时模型之前）


def build_parts(
    cfg: AppConfig,
    *,
    asr_backend: StreamingASR | None = None,
    llm: RealtimeLLMService | None = None,
    tts: LocalTTSService | None = None,
    store: RecorderStore | None = None,
    session_id: str = "",
    diarizer: Diarizer | None = None,
    clock: SessionClock | None = None,
    messages: list[dict[str, Any]] | None = None,
    first_segment_id: int = 1,
) -> BotParts:
    """按配置造出各个处理器。``asr_backend``、``llm``、``tts``、``store``、``diarizer`` 可注入，测试时换成假的。

    ``store`` / ``session_id`` / ``diarizer`` 交给会议记录器（落库、说话人归属）；都不给时记录器只发字幕。
    继续一场会议时：``clock`` 带着本次连接在会话时间轴上的起点，记录器和识别服务都用它；
    ``messages`` 是从数据库重建的上下文，实时模型一开始就带着它；``first_segment_id`` 是字幕行编号的起点
    （每次连接各占一段，页面上留着的旧字幕行才不会和新连接的撞号）。
    """
    vad_analyzer = SileroVADAnalyzer(
        params=VADParams(stop_secs=cfg.turn.vad_stop_secs, min_volume=cfg.turn.vad_min_volume)
    )

    # 唤醒策略放在 start 列表的第一个：它在未唤醒时返回 STOP，后面的策略就不会执行。
    wake = WakeWordUserTurnStartStrategy(
        phrases=cfg.session.wake_phrases,
        single_activation=cfg.turn.single_activation,
        timeout=cfg.turn.wake_timeout_secs,
    )
    stop_strategy = (
        TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())
        if cfg.turn.smart_turn
        else SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=SPEECH_TIMEOUT_SECS)
    )
    # 放进 tools 的直接函数会自动注册到模型服务上（pipecat-notes.md §6）
    context = LLMContext(messages=messages or None, tools=realtime_tools(cfg))
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                start=[wake, VADUserTurnStartStrategy(), TranscriptionUserTurnStartStrategy()],
                stop=[stop_strategy],
            )
        ),
    )

    # 任务相关的工具说明单独一份：后台任务关掉时不给这三个工具，提示词里也就不提它们
    task_section = load_prompt("realtime_tasks") if cfg.agent.enabled else ""
    system_prompt = load_prompt(
        "realtime_system", assistant_name=cfg.session.assistant_name, task_section=task_section
    )
    if tts is None and cfg.tts.enabled:
        tts = build_tts(cfg)
    return BotParts(
        vad_analyzer=vad_analyzer,
        vad=VADProcessor(vad_analyzer=vad_analyzer),
        asr=StreamingASRService(
            backend=asr_backend or build_asr_backend(cfg),
            preroll_ms=cfg.asr.preroll_ms,
            base_secs=clock.base_secs if clock is not None else 0.0,
        ),
        recorder=MeetingRecorder(
            store=store,
            diarizer=diarizer,
            assembler=TranscriptAssembler(first_segment_id=first_segment_id),
            session_id=session_id,
            clock=clock,
            assistant_name=cfg.session.assistant_name,
            merge_gap_secs=cfg.transcript.merge_gap_secs,
            merge_soft_chars=cfg.transcript.merge_soft_chars,
            merge_max_chars=cfg.transcript.merge_max_chars,
        ),
        wake=wake,
        user_aggregator=aggregators.user(),
        llm=llm or build_realtime_llm(cfg, system_prompt),
        tts=tts if cfg.tts.enabled else None,  # 语音合成关闭时管线里不放这个服务
        assistant_aggregator=aggregators.assistant(),
        context=context,
        gate=ModalityGate(),
    )


def build_context_manager(
    cfg: AppConfig,
    parts: BotParts,
    activity: AssistantActivity,
    *,
    store: Store | None,
    session_id: str,
    digests: Any = None,
) -> ContextManager:
    """上下文管理（压缩与预热，pipeline/context.py）。要不要预热只看 ``cfg.realtime_llm.cache_warm``。"""
    return ContextManager(
        llm=parts.llm,
        context=parts.context,
        store=store,
        session_id=session_id,
        recorder=parts.recorder,
        activity=activity,
        budget_tokens=cfg.realtime.context_budget_tokens,
        keep_recent_secs=cfg.realtime.keep_recent_minutes * 60.0,
        cache_warm=cfg.realtime_llm.cache_warm,
        warm_interval_secs=cfg.realtime.cache_warm_interval_secs,
        digests=digests,
    )


def build_input_filter(
    cfg: AppConfig, on_low_level: Callable[[], Awaitable[None]] | None = None
) -> InputGainFilter | None:
    """入口收音增强（architecture.md §4）。自动增益关着、固定增益也是 0 时什么都不做，就不挂。"""
    audio = cfg.audio
    if not audio.auto_gain and audio.gain_db == 0.0:
        return None
    return InputGainFilter.from_config(audio, on_low_level)


def transport_params(cfg: AppConfig, input_filter: InputGainFilter | None) -> TransportParams:
    """传输参数。收音增强挂在 ``audio_in_filter`` 上：先滤波、再推帧，语音检测等看到的是增益后的音频。"""
    return TransportParams(
        audio_in_enabled=True, audio_out_enabled=True, audio_in_filter=input_filter
    )


def pipeline_processors(transport: Any, parts: BotParts) -> list[FrameProcessor]:
    """按 architecture.md §4 的顺序排好处理器。

    * 语音活动检测在识别服务之前：识别服务要靠开始 / 停止说话事件决定何时送音频。
    * 会议记录器在用户侧聚合器之前：唤醒策略在未唤醒时会清空转录文本，记录器要先拿到。
    * 模态闸门紧挨在实时模型之前：每个新请求进入模型时设定这次是只出文字还是照常朗读（pipeline/modality.py）。
    """
    processors: list[FrameProcessor] = [
        transport.input(),
        parts.vad,
        parts.asr,
        parts.recorder,
        parts.user_aggregator,
        parts.gate,
        parts.llm,
    ]
    if parts.tts is not None:
        processors.append(parts.tts)
    processors += [transport.output(), parts.assistant_aggregator]
    return processors


def wire_wake_events(
    wake: WakeWordUserTurnStartStrategy, send: Callable[[dict], Awaitable[None]]
) -> None:
    """被叫到名字时向浏览器发 ``assistant_state: listening``（interfaces.md §6.1）。"""

    @wake.event_handler("on_wake_phrase_detected")
    async def _on_wake_phrase(strategy: Any, phrase: str) -> None:
        logger.info(f"检测到唤醒词：{phrase}")
        await send({"type": "assistant_state", "state": "listening"})


def wire_assistant_recording(parts: BotParts) -> None:
    """助理这一轮说的话落库为 ``source="assistant"`` 的发言（助理的话已由助理侧聚合器写进上下文，不重复追加）。"""
    aggregator, recorder = parts.assistant_aggregator, parts.recorder

    @aggregator.event_handler("on_assistant_turn_started")
    async def _turn_started(_aggregator: Any) -> None:
        recorder.assistant_turn_started()

    @aggregator.event_handler("on_assistant_turn_stopped")
    async def _turn_stopped(_aggregator: Any, message: Any) -> None:
        await recorder.record_assistant(message.content)


def wire_text_input(
    rtvi: Any,
    assistant_aggregator: Any,
    recorder: MeetingRecorder,
    handler: TextInputHandler,
    notice: Callable[[str, str], Awaitable[None]],
) -> None:
    """把浏览器的 ``text_input`` 消息接到文字入口，并让它知道助理什么时候忙、什么时候闲。

    忙 = 助理正在生成回答（助理侧聚合器的一轮开始到结束）或正在朗读（记录器收到的 ``Bot*SpeakingFrame``）。
    """

    @assistant_aggregator.event_handler("on_assistant_turn_started")
    async def _turn_started(_aggregator: Any) -> None:
        await handler.set_generating(True)

    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def _turn_stopped(_aggregator: Any, _message: Any) -> None:
        await handler.set_generating(False)

    recorder.on_bot_speaking_changed = handler.set_speaking

    @rtvi.event_handler("on_client_message")
    async def _client_message(_rtvi: Any, message: Any) -> None:
        if message.type != "text_input":
            return
        text = extract_text(message.data)
        if text is None:
            await notice("warn", "文字消息的格式不对，没有发送")
            return
        await handler.handle(text)


def wire_context_manager(manager: ContextManager, wake: Any) -> None:
    """检测到唤醒词的那一刻预热上下文缓存（用户还要说几秒才说完，正好利用这段时间）。"""

    @wake.event_handler("on_wake_phrase_detected")
    async def _wake_detected(_strategy: Any, _phrase: str) -> None:
        manager.on_wake()


def wire_tool_activity(
    llm: Any, activity: AssistantActivity, text_input: TextInputHandler | None = None
) -> None:
    """模型发起工具调用时通知「忙闲」和文字入口：工具执行期间（两次生成之间）仍然算忙。"""

    @llm.event_handler("on_function_calls_started")
    async def _function_calls_started(_llm: Any, _calls: Any) -> None:
        await activity.tools_started()
        if text_input is not None:
            await text_input.tools_started()


def wire_activity(
    activity: AssistantActivity,
    wake: Any,
    assistant_aggregator: Any,
    recorder: MeetingRecorder,
) -> None:
    """把「助理忙不忙」接到各处的事件上（pipeline/activity.py）。

    要在 ``wire_text_input`` **之后**调用：记录器的朗读回调只有一个位置，这里把原来的那个包起来一起通知。
    文字请求那一路由 ``TextInputHandler(on_dispatch=activity.request_sent)`` 通知。
    """

    @wake.event_handler("on_wake_phrase_detected")
    async def _wake_detected(_strategy: Any, _phrase: str) -> None:
        await activity.wake_detected()

    @wake.event_handler("on_wake_phrase_timeout")
    async def _wake_timeout(_strategy: Any) -> None:
        await activity.wake_expired()

    @assistant_aggregator.event_handler("on_assistant_turn_started")
    async def _turn_started(_aggregator: Any) -> None:
        await activity.set_generating(True)

    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def _turn_stopped(_aggregator: Any, _message: Any) -> None:
        await activity.set_generating(False)

    previous = recorder.on_bot_speaking_changed

    async def _speaking_changed(speaking: bool) -> None:
        if previous is not None:
            await previous(speaking)
        await activity.set_speaking(speaking)

    recorder.on_bot_speaking_changed = _speaking_changed


def wire_wake_sleep(activity: AssistantActivity, wake: Any, user_aggregator: Any) -> None:
    """「每次都要叫名字」时，助理答完就回到待唤醒状态，不等唤醒窗口过期。

    否则叫过一次名字之后的整个窗口里（``turn.wake_timeout_secs``），会上其他人接着说的话都会被当成对助理说的，
    助理会不请自来地接话。窗口本身仍然有用：它是「叫了名字到助理答完」这段时间的上限，也是能用声音打断朗读的时间。

    有人正在说话时不睡：那是在打断助理或接着把话说完（「Nova，」停一下再说要求），等这一轮答完再睡。
    """
    user_speaking = False

    @user_aggregator.event_handler("on_user_turn_started")
    async def _turn_started(_aggregator: Any, *_args: Any) -> None:
        nonlocal user_speaking
        user_speaking = True

    @user_aggregator.event_handler("on_user_turn_stopped")
    async def _turn_stopped(_aggregator: Any, *_args: Any) -> None:
        nonlocal user_speaking
        user_speaking = False

    def _on_activity(busy: bool) -> None:
        if not busy and not user_speaking and wake.awake:
            logger.debug("助理答完了，回到待唤醒状态")
            wake.sleep()

    activity.subscribe(_on_activity)


def wire_screen_state(rtvi: Any) -> None:
    """浏览器的 ``screen_state`` 消息（屏幕共享开始 / 停止）：只记日志（interfaces.md §6.2）。"""

    @rtvi.event_handler("on_client_message")
    async def _client_message(_rtvi: Any, message: Any) -> None:
        if message.type != "screen_state":
            return
        data = message.data
        sharing = isinstance(data, dict) and data.get("sharing") is True
        logger.info("屏幕共享已开始" if sharing else "屏幕共享已停止")


def _latency_observer() -> UserBotLatencyObserver:
    """每次应答结束后把「用户说完 → 助理开口」的耗时拆分写进日志（调优时看首字、首音延迟）。"""
    observer = UserBotLatencyObserver()

    @observer.event_handler("on_latency_breakdown")
    async def _log(_observer: Any, breakdown: Any) -> None:
        lines = breakdown.turn_contribution_lines()
        if lines:
            logger.info("应答延迟拆分：\n  " + "\n  ".join(lines))

    return observer


def requested_session_id(request_data: Any) -> str | None:
    """连接参数里要继续的会议编号（interfaces.md §5.2）；没带就是新建。"""
    if isinstance(request_data, dict):
        value = request_data.get("session_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def resumed_context(
    cfg: AppConfig, store: Store | None, live: LiveConnection | None
) -> list[dict[str, Any]] | None:
    """继续一场会议时实时模型一开始带着的上下文：最新纪要 + 最近若干分钟的原文（和压缩是同一段代码）。

    读不出来不拦着连接：从空的上下文开始，模型还可以用 recall 去找。
    """
    if store is None or live is None or not live.resumed:
        return None
    try:
        return await build_context_messages(
            store,
            live.session.id,
            live.base_secs,
            keep_recent_secs=cfg.realtime.keep_recent_minutes * 60.0,
        )
    except Exception:
        logger.exception("继续会议时重建上下文失败，从空的上下文开始")
        return None


async def run_bot(
    connection: SmallWebRTCConnection, request_data: Any, resources: AppResources
) -> None:
    """为一次浏览器连接组装管线并运行到连接断开。

    有会话管理（``resources.sessions``）时：先建会话（连接参数里带了 ``session_id`` 就是继续那一场）、顶替旧连接，
    管线里落库；连接断开只关闭这一路连接的记录，不结束会议（architecture.md §3.1）。
    """
    cfg = resources.cfg
    manager = resources.sessions
    live: LiveConnection | None = None
    if manager is not None:
        session_id = requested_session_id(request_data)
        try:
            live = await (manager.attach(session_id) if session_id else manager.begin())
        except (SessionNotFound, SessionBusy):
            # 协商之前校验过，走到这里说明会议在这一瞬间被删掉了
            logger.warning("要继续的会议已经不存在，断开这次连接")
            await connection.disconnect()
            return
    if live is not None:
        live.owner = asyncio.current_task()
    diarizer: Diarizer | None = None
    text_input: TextInputHandler | None = None
    context_manager: ContextManager | None = None
    activity = AssistantActivity()
    for model in resources.background_models:
        # 助理应答时后台模型让路（画面摘要、滚动纪要），应答完再恢复
        activity.subscribe(lambda busy, m=model: m.pause() if busy else m.resume())
    try:
        pending_notices: list[tuple[str, str]] = []  # 管线造好、页面就绪之前产生的提示

        async def collect_notice(level: str, text: str) -> None:
            pending_notices.append((level, text))

        async def new_diarizer() -> Diarizer:
            return await build_diarizer(cfg, on_notice=collect_notice)

        if manager is not None and live is not None:
            # 说话人区分的流归会话管理器：同一进程内继续同一场会议时接着用，说话人编号不变
            diarizer = await manager.diarizer_for(live, new_diarizer, collect_notice)
        else:
            diarizer = await new_diarizer()

        async def on_low_level() -> None:
            await send(
                {
                    "type": "notice",
                    "level": "warn",
                    "text": "麦克风音量过低，请调高系统的输入音量或靠近麦克风",
                }
            )

        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=transport_params(cfg, build_input_filter(cfg, on_low_level)),
        )
        parts = build_parts(
            cfg,
            store=resources.store,
            session_id=live.session.id if live is not None else "",
            diarizer=diarizer,
            clock=SessionClock(live.base_secs) if live is not None else None,
            messages=await resumed_context(cfg, resources.store, live),
            first_segment_id=(
                live.connection_id * SEGMENT_IDS_PER_CONNECTION + 1 if live is not None else 1
            ),
        )
        worker = PipelineWorker(
            Pipeline(pipeline_processors(transport, parts)),
            params=PipelineParams(
                audio_in_sample_rate=ASR_SAMPLE_RATE,
                audio_out_sample_rate=cfg.tts.sample_rate,
                enable_metrics=True,
            ),
            observers=[MetricsLogObserver(), _latency_observer()],
            app_resources=resources,
            idle_timeout_secs=None,  # 会议里长时间没人对助理说话是常态，必须关掉空闲超时
        )

        async def send(data: dict) -> None:
            await worker.queue_frame(RTVIServerMessageFrame(data=data))

        wire_wake_events(parts.wake, send)
        wire_assistant_recording(parts)

        async def notice(level: str, text: str) -> None:
            await send({"type": "notice", "level": level, "text": text})

        text_input = TextInputHandler(
            recorder=parts.recorder,
            push=worker.queue_frame,
            notice=notice,
            tts_enabled=parts.tts is not None,
            on_dispatch=activity.request_sent,
        )
        wire_text_input(worker.rtvi, parts.assistant_aggregator, parts.recorder, text_input, notice)
        wire_activity(activity, parts.wake, parts.assistant_aggregator, parts.recorder)
        wire_tool_activity(parts.llm, activity, text_input)
        if cfg.turn.single_activation:
            wire_wake_sleep(activity, parts.wake, parts.user_aggregator)
        context_manager = build_context_manager(
            cfg,
            parts,
            activity,
            store=resources.store,
            session_id=live.session.id if live is not None else "",
            digests=resources.digests,
        )
        wire_context_manager(context_manager, parts.wake)
        wire_screen_state(worker.rtvi)
        errors = ErrorNotifier(notice, llm=parts.llm, tts=parts.tts)

        @worker.event_handler("on_pipeline_error")
        async def _on_pipeline_error(_worker: Any, frame: Any) -> None:
            # 实时模型 / 语音合成出错：换成一句看得懂的提示（architecture.md §9）
            await errors.on_error(frame)

        ready_sent = False

        @worker.rtvi.event_handler("on_client_ready")
        async def _on_client_ready(_rtvi: Any) -> None:
            # 页面就绪之后才发：这之前发出的数据通道消息可能丢。页面自己也会用 HTTP 拉当前会话，这里只是更及时。
            nonlocal ready_sent
            if ready_sent:
                return
            ready_sent = True
            if live is not None:
                if live.stop_requested or live.done.is_set() or manager.live is not live:
                    return
                await send_session_ready(resources, live, send)
            for level, text in pending_notices:
                await send({"type": "notice", "level": level, "text": text})
            pending_notices.clear()

        @transport.event_handler("on_client_disconnected")
        async def _on_client_disconnected(_transport: Any, _connection: Any) -> None:
            logger.info("浏览器已断开，结束本次连接")
            await worker.cancel()

        if manager is not None and live is not None:
            await manager.register(live, worker, parts.recorder, parts.gate)
        runner = WorkerRunner(handle_sigint=False)  # 嵌在 FastAPI 里，不要让它接管信号
        await runner.add_workers(worker)
        context_manager.start()
        if live is not None and live.resumed:
            context_manager.warm_soon("继续会议")  # 重建出来的上下文先算好，第一次应答就不慢
        await runner.run()
    finally:

        async def close() -> None:
            if context_manager is not None:
                await context_manager.stop()
            await activity.close()  # 同时让后台模型恢复：不能因为连接断在应答中途就一直停着
            if text_input is not None:
                await text_input.close()
            if diarizer is not None:
                await diarizer.close()  # 会话管理器持有的流在这里不会真的关掉（diar/stream.py）
            if manager is not None and live is not None:
                await manager.finish(live)

        await drain_io(close())


async def send_session_ready(
    resources: AppResources,
    live: LiveConnection,
    send: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    """真正就绪时读当前 keep，再发送会话及可选的保存提示。"""
    current = (
        await resources.store.get_session(live.session.id)
        if resources.store is not None
        else live.session
    )
    if current is None or current.deletion_pending:
        return
    await send(session_message(current, live.resumed, live.base_secs))
    if resources.cfg.session.recording_notice:
        await send(
            {
                "type": "notice",
                "level": "info",
                "text": "会议正在转录，发言和共享画面会保存在服务器上",
            }
        )


def session_message(session: Session, resumed: bool = False, base_secs: float = 0.0) -> dict:
    """连接建立后发给页面的 ``session`` 消息（interfaces.md §6.1）。"""
    return {
        "type": "session",
        "id": session.id,
        "title": session.title,
        "keep": session.keep,
        "started_at": session.started_at,
        "resumed": resumed,
        "base_secs": base_secs,
        "state": "live",
    }
