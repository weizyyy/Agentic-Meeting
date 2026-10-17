"""会话管理（docs/architecture.md §3.1、§3.2）。

* **同时进行的会议有上限**（``session.max_live_meetings``，默认 1）：每场进行中的会议有自己的连接、管线、
  会议记录器和说话人区分流，实时模型各用一个槽位（``LiveConnection.lane``）。上限是 1 时，新连接顶替正在进行的
  那一场（和以前一样）；上限大于 1 时，满了就拒绝新会议（``MeetingsFull``），不去顶替别人的会议。
  同一场会议换设备继续，始终是顶替这场会议自己的旧连接：先取消旧连接的管线、等它收尾（有超时），再启动新的。
* **断开不等于结束**：连接断开（关页面、刷新、断网）只关闭这一路连接的记录，会话仍然「未结束」，也就是「已中断」；
  只有 ``end()``（用户点「结束会议」或在列表里结束）才写 ``ended_at``。
* **继续**（``attach``）：同一场会议再挂上一路连接。时间轴接着走（``base_secs`` 在连接建立那一刻
  对一次表），已结束的先重新打开。新建不再顺手结束别的会议——已中断的会议留着，随时可以继续。
* **旁观**（``watchers``）：别的设备只看不连时开事件流（``pipeline/watchers.py``）。会议状态变了（连接就绪、断开、
  结束）在这里告诉它们；同一场会议换了设备接管，只发一次「进行中」，中间不发「已中断」。
* **说话人区分的流比连接活得长**（``diarizer_for``）：同一进程内继续时复用同一条流，说话人编号不变。
  每条流占一份模型，所以手里最多留 ``max_live_meetings`` 条：要开新流而位置满了，先释放没有连接的会议的流；
  结束会议、删除会议、应用退出时也释放。服务重启后只能新开一条，编号加上已有的最大编号作偏移。

``run_bot`` 的用法::

    live = await manager.begin()                   # 建会话、登记；位置不够时顶替或拒绝（继续则是 attach(session_id)）
    diarizer = await manager.diarizer_for(live, build, notice)
    ...                                            # 造管线，实时模型用 live.lane 对应的槽位
    await manager.register(live, worker, recorder)
    try:
        await runner.run()
    finally:
        await manager.finish(live)                 # 关闭连接记录，释放位置
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame

from agentic_meeting.config import RetentionConfig
from agentic_meeting.diar.base import Diarizer, NullDiarizer
from agentic_meeting.diar.stream import SessionDiarizer
from agentic_meeting.messages import Message, Notice, message
from agentic_meeting.pipeline.watchers import WatcherHub
from agentic_meeting.store.db import CleanupPlan, SessionBusy, Store
from agentic_meeting.store.work import drain_io
from agentic_meeting.types import Session

TAKEOVER_TIMEOUT_SECS = 10.0
SHUTDOWN_TIMEOUT_SECS = 3.0  # 应用退出时等活动连接收尾的时间
NOTIFY_GRACE_SECS = 0.2  # 发出 session_closed 之后，等这么久再取消管线，让消息有机会送达


class SessionNotFound(LookupError):
    """要继续的会议不存在（可能刚被删掉）。"""


@dataclass(eq=False)
class LiveConnection:
    """一路活动的连接。``worker`` / ``recorder`` 在管线造好后由 ``register`` 填上。"""

    session: Session
    connection_id: int
    connected_at: float
    lane: int = 0  # 第几个会议位置（0 起）：实时模型用对应的槽位（config.request_extra_body）
    worker: Any = None  # PipelineWorker：要用到 queue_frame / cancel
    recorder: Any = None  # MeetingRecorder：要用到 set_speaker_name / elapsed_secs
    gate: Any = None  # ModalityGate：工具要知道当前这次请求是文字还是语音
    text_input: Any = None  # TextInputHandler：旁观的设备打字提问时用
    base_secs: float = 0.0  # 本次连接在会话时间轴上的起点；新建的会议恒为 0
    resumed: bool = False  # 是继续一场已有的会议，不是新建
    stop_requested: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    owner: asyncio.Task | None = None
    finishing: bool = False
    identity: Any = None
    tool_tasks: set[asyncio.Task] = field(default_factory=set)
    quiet: bool = False  # 由会话管理器停掉的：收尾时不再告诉旁观的设备「已中断」，停它的地方自己说


class MeetingsFull(RuntimeError):
    """同时进行的会议已经到了上限（``session.max_live_meetings``），新会议不能开始。"""

    def __init__(self, limit: int) -> None:
        super().__init__(f"同时进行的会议已经有 {limit} 场")
        self.limit = limit


class SessionManager:
    def __init__(
        self,
        store: Store,
        *,
        max_live: int = 1,
        now: Callable[[], float] = time.time,
        notify_grace_secs: float = NOTIFY_GRACE_SECS,
        takeover_timeout_secs: float = TAKEOVER_TIMEOUT_SECS,
    ) -> None:
        self._store = store
        self._max = max(1, max_live)
        self._now = now
        self._grace = notify_grace_secs
        self._timeout = takeover_timeout_secs
        self._live: dict[str, LiveConnection] = {}  # 会议编号 → 它正在用的那一路连接
        self._streams: dict[str, SessionDiarizer] = {}  # 会议编号 → 它的说话人区分流
        self._lock = asyncio.Lock()
        self._unfinished: set[LiveConnection] = set()
        # 连接结束（断开或会议结束）之后调用，参数是会话编号。滚动纪要用它做收尾。出错只记日志。
        self.on_finished: list[Callable[[str], Awaitable[None] | None]] = []
        self.watchers = WatcherHub(now=now)
        self.watchers.on_change = self._watchers_changed

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    @property
    def max_live(self) -> int:
        return self._max

    @property
    def connections(self) -> list[LiveConnection]:
        """全部活动的连接，先连上的在前。"""
        return sorted(self._live.values(), key=lambda live: live.connected_at)

    def live_session_ids(self) -> list[str]:
        """正在进行的会议，先连上的在前。"""
        return [live.session.id for live in self.connections]

    def live_for(self, session_id: str) -> LiveConnection | None:
        return self._live.get(session_id)

    def is_live(self, session_id: str) -> bool:
        return session_id in self._live

    def has_room(self, session_id: str | None = None) -> bool:
        """这时候来一路连接能不能开始：继续一场正在进行的会议（换设备）总可以；位置没满可以；
        上限是 1 时顶替正在进行的那一场，也可以。只是预检，真正的判定在 ``begin`` / ``attach`` 里。"""
        if session_id is not None and session_id in self._live:
            return True
        return self._max == 1 or len(self._live) < self._max

    async def claim_cleanup(
        self, session_id: str, policy: RetentionConfig, now: float, *, manual: bool = False
    ) -> CleanupPlan | None:
        """与挂接同锁的最后判定；这里只领取所有权，不等待文件或模型。"""
        async with self._lock:
            if any(live.session.id == session_id for live in self._unfinished):
                raise SessionBusy("会议连接尚未收尾")
            return await self._store.claim_cleanup(session_id, policy, now, manual=manual)

    async def update_session(
        self, session_id: str, *, title: str | None = None, keep: bool | None = None
    ) -> Session | None:
        async with self._lock:
            updated = await self._store.update_session(session_id, title=title, keep=keep)
            if updated is not None:
                self.publish(
                    session_id,
                    {"type": "session_updated", "title": updated.title, "keep": updated.keep},
                )
            return updated

    async def current_session(self) -> Session | None:
        """「当前会话」：有进行中的会议就取最近连上的那一场；没有就取最近一个未结束的；都没有返回 ``None``。

        同时进行好几场时这只是个默认值，页面和接口应当带上明确的会议编号。
        """
        connections = self.connections
        if connections:
            return await self._store.get_session(connections[-1].session.id)
        return await self._store.latest_unended_session()

    def publish(self, session_id: str, data: dict) -> None:
        self.watchers.publish(session_id, data)

    async def _watchers_changed(self, session_id: str) -> None:
        """旁观名单变了：告诉正在进行的这场会议的页面（interfaces.md §6.1 ``watchers``）。"""
        await self.push_to(session_id, self.watchers_message(session_id))

    def watchers_message(self, session_id: str) -> dict:
        return {"type": "watchers", "items": self.watchers.items(session_id)}

    async def push_to(self, session_id: str, data: dict) -> bool:
        """向 ``session_id`` 这场会议的页面推一条数据通道消息，也抄给旁观它的设备。

        它没有在进行（或管线还没登记好）返回 ``False``（后台任务完成时，会议可能已经结束了）。
        """
        self.publish(session_id, data)
        live = self._live.get(session_id)
        if live is None or live.worker is None:
            return False
        try:
            await live.worker.queue_frame(RTVIServerMessageFrame(data=data))
        except Exception:
            logger.exception("向浏览器推送消息失败")
            return False
        return True

    async def queue_frame_to(self, session_id: str, frame: Any) -> bool:
        """只在 ``session_id`` 这场会议正在进行时，向它的管线送一帧（比如往实时模型的上下文追加一行）。"""
        live = self._live.get(session_id)
        if live is None or live.worker is None:
            return False
        try:
            await live.worker.queue_frame(frame)
        except Exception:
            logger.exception("向管线送帧失败")
            return False
        return True

    async def append_context(self, session_id: str, line: str) -> bool:
        """只在 ``session_id`` 这场会议正在进行时，往它的实时模型上下文追加一行（不触发应答）。

        经过会议记录器（``append_context_line``），和发言的追加、上下文的重建排在同一个顺序里。
        """
        live = self._live.get(session_id)
        if live is None:
            return False
        append = getattr(live.recorder, "append_context_line", None)
        if append is None:
            return False
        try:
            await append(line)
        except Exception:
            logger.exception("往实时模型的上下文追加一行失败")
            return False
        return True

    async def submit_text(self, session_id: str, text: str) -> list[tuple[str, Message]] | None:
        """旁观的设备打字提问（interfaces.md §5.1）：交给这场会议的文字入口，返回处理结果的提示。

        这场会议没有活动连接、或管线还没登记好时返回 ``None``。
        """
        live = self._live.get(session_id)
        if live is None or live.text_input is None:
            return None
        notices: list[tuple[str, Message]] = []

        async def collect(level: str, msg: Message) -> None:
            notices.append((level, msg))

        await live.text_input.handle(text, notice=collect)
        return notices

    async def wait_idle(self, wait_secs: float = 5.0) -> None:
        """应用关闭时：等活动连接把连接记录写完（有超时）。"""
        pending = [live.done.wait() for live in self._live.values()]
        if not pending:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*pending), wait_secs)
        except TimeoutError:
            logger.warning("应用关闭时活动连接没有及时收尾")

    async def drain(self) -> None:
        """关闭传输后等待真实管线和已开始的收尾，不让旧连接继续使用已关闭的库。"""
        pending = [live for live in self._unfinished if live.owner is not None or live.finishing]
        if pending:
            await asyncio.gather(*(live.finished.wait() for live in pending))

    # ------------------------------------------------------------------ #
    # 连接的生命周期
    # ------------------------------------------------------------------ #

    async def begin(self, *, owner: str | None = None) -> LiveConnection:
        """新连接、新会议：建新会话和连接记录，登记为活动连接。

        位置满了：上限是 1 时顶替正在进行的那一场；否则抛 ``MeetingsFull``。
        别的没结束的会议（已中断的）原样留着，之后还能继续。
        """
        async with self._lock:
            await self._make_room(None)
            now = self._now()
            session = await self._store.create_session(now=now, owner=owner)
            connection_id = await self._store.open_connection(
                session.id, connected_at=now, t_from=0.0
            )
            live = LiveConnection(session, connection_id, now, lane=self._free_lane())
            self._unfinished.add(live)
            self._live[session.id] = live
            return live

    async def attach(self, session_id: str) -> LiveConnection:
        """新连接、继续一场已有的会议：顶替这场会议自己的旧连接（如果有），已结束的重新打开，登记为活动连接。

        这场会议没有在进行而位置满了：上限是 1 时顶替正在进行的那一场；否则抛 ``MeetingsFull``。
        ``base_secs`` = 此刻 − 会议的 0 点，在这里对一次表，之后只按采样数推进。它不会早于时间轴已经用到的地方
        （上一次连接的终点、最后一条发言的终点）：音频时钟和墙上时钟有细微出入，马上重连时不能让时间倒着走。
        会议不存在抛 ``SessionNotFound``。
        """
        async with self._lock:
            session = await self._store.get_session(session_id)
            if session is None:
                raise SessionNotFound(session_id)
            await self._store.require_available(session_id)
            await self._make_room(session_id)
            if session.ended_at is not None:
                session = await self._store.reopen_session(session_id) or session
            now = self._now()
            base_secs = max(now - session.started_at, await self._store.timeline_end(session_id))
            connection_id = await self._store.open_connection(
                session_id, connected_at=now, t_from=base_secs
            )
            live = LiveConnection(
                session,
                connection_id,
                now,
                lane=self._free_lane(),
                base_secs=base_secs,
                resumed=True,
            )
            self._unfinished.add(live)
            self._live[session_id] = live
            return live

    async def _make_room(self, session_id: str | None) -> None:
        """在锁里调用：给一路要开始的连接腾位置。

        继续的会议正在别的设备上进行：停掉那一路（换设备）。位置满了：上限是 1 时顶替正在进行的那一场，
        否则抛 ``MeetingsFull``。
        """
        own = self._live.get(session_id) if session_id is not None else None
        if own is not None:
            await self._stop(own, "taken_over")
            return
        if len(self._live) < self._max:
            return
        if self._max > 1:
            raise MeetingsFull(self._max)
        for old in list(self._live.values()):
            await self._stop(old, "taken_over")
            self.watchers.state(old.session.id, "interrupted")

    def displaced_by(self, session_id: str | None) -> list[LiveConnection]:
        """一路要开始的连接（继续 ``session_id``，``None`` 是新会议）会停掉哪些进行中的连接（不改动什么）。

        和 ``_make_room`` 的规则一致；位置满了而上限大于 1 时返回空表，那种情况会被拒绝而不是顶替。
        """
        own = self._live.get(session_id) if session_id is not None else None
        if own is not None:
            return [own]
        if self._max == 1:
            return list(self._live.values())
        return []

    def _free_lane(self) -> int:
        used = {live.lane for live in self._live.values()}
        return next((lane for lane in range(self._max) if lane not in used), 0)

    async def diarizer_for(
        self,
        live: LiveConnection,
        build: Callable[[], Awaitable[Diarizer]],
        notice: Notice | None = None,
    ) -> Diarizer:
        """这路连接用的说话人区分。

        手里有这场会议的流、而且它没出过错：接着用（记下这次连接的接续点）。否则用 ``build`` 新开一条；
        继续一场已经有说话人的会议时，新流的编号加上已有的最大编号作偏移，并通过 ``notice`` 说明。
        开新流之前，手里的流已经有 ``max_live`` 条的话，先释放没有连接的会议的流（每条流占一份模型）。
        ``build`` 给出的是 ``NullDiarizer``（没开、或启动失败已降级）时原样返回，不留着。
        """
        session_id = live.session.id
        stream = self._streams.get(session_id)
        if stream is not None and not stream.failed:
            stream.begin_connection(live.base_secs)
            return stream
        await self._drop_stream(session_id)
        idle = [sid for sid in self._streams if sid not in self._live]
        while idle and len(self._streams) >= self._max:
            await self._drop_stream(idle.pop(0))
        inner = await build()
        if isinstance(inner, NullDiarizer):
            return inner
        offset = 0
        if live.resumed:
            try:
                offset = await self._store.max_speaker_idx(session_id)
            except Exception:
                logger.exception("读取已有的说话人编号失败，新流的编号不加偏移")
            if offset > 0 and notice is not None:
                await notice("info", message("speakers.renumbered"))
        stream = SessionDiarizer(inner, session_id=session_id, speaker_offset=offset)
        stream.begin_connection(live.base_secs)
        self._streams[session_id] = stream
        return stream

    async def rename_speaker(self, session_id: str, idx: int, name: str) -> bool:
        """改说话人的显示名；说话人不存在返回 ``False``。

        改的是正在进行的会议时，之后的字幕和上下文行也用新名字；页面收到一条 ``speaker`` 消息。
        """
        if not await self._store.rename_speaker(session_id, idx, name):
            return False
        live = self._live.get(session_id)
        if live is not None and live.recorder is not None:
            live.recorder.set_speaker_name(idx, name)
        await self.push_to(session_id, {"type": "speaker", "idx": idx, "display_name": name})
        return True

    def voice_changed(self, session_id: str | None = None) -> None:
        """说话人或声纹档案变了：让进行中的会议重新比对声纹（``None`` = 所有进行中的会议，档案是共用的）。"""
        lives = self._live.values() if session_id is None else [self._live.get(session_id)]
        for live in lives:
            if live is not None and live.recorder is not None:
                live.recorder.voice_changed()

    async def merge_speakers(self, session_id: str, src: int, dst: int) -> dict | None:
        """把说话人 ``src`` 并入 ``dst``。两个说话人有一个不存在（或不能合并）返回 ``None``。

        改的是正在进行的会议时：说话人区分之后再输出 ``src`` 也算作 ``dst``，并通知页面。
        返回 ``{"from", "into", "display_name", "moved"}``。
        """
        moved = await self._store.merge_speakers(session_id, src, dst)
        if moved is None:
            return None
        name = await self._store.speaker_name(session_id, dst)
        stream = self._streams.get(session_id)
        if stream is not None:
            stream.merge(src, dst)
        result = {"from": src, "into": dst, "display_name": name, "moved": moved}
        live = self._live.get(session_id)
        if live is not None:
            if live.recorder is not None:
                live.recorder.set_speaker_name(src, name)  # 已经在路上的字幕也用合并后的名字
        await self.push_to(
            session_id, {"type": "speakers_merged", "from": src, "into": dst, "display_name": name}
        )
        return result

    async def forget(self, session_id: str) -> None:
        """这场会议被删掉了：结束旁观它的事件流；手里要是还留着它的说话人区分流，释放掉。"""
        self.watchers.close_session(session_id)
        await self._drop_stream(session_id)

    async def shutdown(self, wait_secs: float = SHUTDOWN_TIMEOUT_SECS) -> None:
        """应用退出：告诉页面「服务正在停止」并停掉全部活动连接（最多等 ``wait_secs`` 秒），释放说话人区分流。"""
        self.watchers.close_all()
        async with self._lock:
            await asyncio.gather(
                *(
                    self._stop(live, "server_stopping", wait_secs=wait_secs)
                    for live in list(self._live.values())
                )
            )
            for session_id in list(self._streams):
                await self._drop_stream(session_id)

    async def register(
        self,
        live: LiveConnection,
        worker: Any,
        recorder: Any,
        gate: Any = None,
        text_input: Any = None,
    ) -> None:
        """管线造好后登记它的 worker、记录器、模态闸门和文字入口。登记之前就有人要求停止的，现在立刻取消。"""
        live.worker, live.recorder, live.gate = worker, recorder, gate
        live.text_input = text_input
        if live.stop_requested:
            await worker.cancel()
        else:
            self.watchers.state(live.session.id, "live")

    async def finish(self, live: LiveConnection) -> None:
        """连接结束（无论怎么结束）：关闭连接记录，释放位置。不写 ``ended_at``。"""
        live.stop_requested = True
        for pending in list(live.tool_tasks):
            pending.cancel()
        live.finishing = True
        await drain_io(self._finish(live))

    async def _finish(self, live: LiveConnection) -> None:
        try:
            await self._finish_connection(live)
        finally:
            self._unfinished.discard(live)
            live.finished.set()

    async def _finish_connection(self, live: LiveConnection) -> None:
        try:
            elapsed = float(getattr(live.recorder, "elapsed_secs", 0.0) or 0.0)
            await self._store.close_connection(
                live.connection_id, disconnected_at=self._now(), t_to=elapsed
            )
        except Exception:
            logger.exception("关闭连接记录失败")
        finally:
            self._release(live)
            live.done.set()
        if not live.quiet:
            self.watchers.state(live.session.id, "interrupted")
        for hook in list(self.on_finished):
            try:
                result = hook(live.session.id)
                if result is not None:
                    await result
            except Exception:
                logger.exception("连接结束后的收尾钩子出错")

    async def end(self, session_id: str) -> Session | None:
        """结束会议：写 ``ended_at``；它正在进行的话先停掉连接。会话不存在返回 ``None``。"""
        async with self._lock:
            live = self._live.get(session_id)
            if live is not None:
                await self._stop(live, "ended")
            await self._drop_stream(session_id)
            ended = await self._store.end_session(session_id, now=self._now())
            if ended is not None:
                self.watchers.state(session_id, "ended")
            return ended

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _release(self, live: LiveConnection) -> None:
        """把这路连接从活动连接里去掉（它还是这场会议的活动连接的话）。"""
        if self._live.get(live.session.id) is live:
            del self._live[live.session.id]

    async def _drop_stream(self, session_id: str) -> None:
        stream = self._streams.pop(session_id, None)
        if stream is not None:
            try:
                await stream.shutdown()
            except Exception:
                logger.exception("释放说话人区分流时出错")

    async def _stop(
        self, live: LiveConnection, reason: str, wait_secs: float | None = None
    ) -> None:
        """告诉页面原因、取消管线、等它收尾（超时就不等了）。"""
        timeout = self._timeout if wait_secs is None else wait_secs
        live.stop_requested = True
        for pending in list(live.tool_tasks):
            pending.cancel()
        live.quiet = True
        if live.worker is not None:
            try:
                await live.worker.queue_frame(
                    RTVIServerMessageFrame(data={"type": "session_closed", "reason": reason})
                )
                if self._grace > 0:
                    await asyncio.sleep(self._grace)
                await live.worker.cancel()
            except Exception:
                logger.exception("停止旧连接时出错")
        try:
            await asyncio.wait_for(live.done.wait(), timeout)
        except TimeoutError:
            logger.warning(f"旧连接在 {timeout:.0f} 秒内没有收尾，不再等它")
            self._release(live)
