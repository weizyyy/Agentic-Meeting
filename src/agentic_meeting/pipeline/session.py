"""会话管理（docs/architecture.md §3.1）。

* **同一时刻只有一路活动连接**：语音识别固定单槽位，说话人区分是一条流，实时模型的上下文也只有一份。
  新连接到来时，先取消旧连接的管线、等它收尾（有超时），再启动新的；被顶替的会话顺手结束。
* **断开不等于结束**：连接断开（关页面、刷新、断网）只关闭这一路连接的记录，会话仍然「未结束」，也就是「已中断」；
  只有 ``end()``（用户点「结束会议」或在列表里结束）才写 ``ended_at``。
* **继续**（``attach``）：同一场会议再挂上一路连接。时间轴接着走（``base_secs`` 在连接建立那一刻
  对一次表），已结束的先重新打开。新建不再顺手结束别的会议——已中断的会议留着，随时可以继续。
* **说话人区分的流比连接活得长**（``diarizer_for``）：同一进程内继续时复用同一条流，说话人编号不变；
  换到别的会议、结束会议、应用退出时才释放。服务重启后只能新开一条，编号加上已有的最大编号作偏移。

``run_bot`` 的用法::

    live = await manager.begin()                   # 建会话、登记；顶替旧连接（继续则是 attach(session_id)）
    diarizer = await manager.diarizer_for(live, build, notice)
    ...                                            # 造管线
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

from agentic_meeting.diar.base import Diarizer, NullDiarizer
from agentic_meeting.diar.stream import SessionDiarizer
from agentic_meeting.store.db import Store
from agentic_meeting.types import Session

TAKEOVER_TIMEOUT_SECS = 10.0
SHUTDOWN_TIMEOUT_SECS = 3.0  # 应用退出时等活动连接收尾的时间
NOTIFY_GRACE_SECS = 0.2  # 发出 session_closed 之后，等这么久再取消管线，让消息有机会送达
RENUMBERED_NOTICE = "服务重启过，说话人编号可能与之前不同；认错的可以在说话人那里用「合并到…」收拾"


class SessionNotFound(LookupError):
    """要继续的会议不存在（可能刚被删掉）。"""


@dataclass(eq=False)
class LiveConnection:
    """当前活动的那一路连接。``worker`` / ``recorder`` 在管线造好后由 ``register`` 填上。"""

    session: Session
    connection_id: int
    connected_at: float
    worker: Any = None  # PipelineWorker：要用到 queue_frame / cancel
    recorder: Any = None  # MeetingRecorder：要用到 set_speaker_name / elapsed_secs
    gate: Any = None  # ModalityGate：工具要知道当前这次请求是文字还是语音
    base_secs: float = 0.0  # 本次连接在会话时间轴上的起点；新建的会议恒为 0
    resumed: bool = False  # 是继续一场已有的会议，不是新建
    stop_requested: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)


class SessionManager:
    def __init__(
        self,
        store: Store,
        *,
        now: Callable[[], float] = time.time,
        notify_grace_secs: float = NOTIFY_GRACE_SECS,
        takeover_timeout_secs: float = TAKEOVER_TIMEOUT_SECS,
    ) -> None:
        self._store = store
        self._now = now
        self._grace = notify_grace_secs
        self._timeout = takeover_timeout_secs
        self._live: LiveConnection | None = None
        self._stream: SessionDiarizer | None = None  # 当前这场会议的说话人区分流
        self._lock = asyncio.Lock()
        # 连接结束（断开或会议结束）之后调用，参数是会话编号。滚动纪要用它做收尾。出错只记日志。
        self.on_finished: list[Callable[[str], Awaitable[None] | None]] = []

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    @property
    def live(self) -> LiveConnection | None:
        return self._live

    def is_live(self, session_id: str) -> bool:
        return self._live is not None and self._live.session.id == session_id

    async def current_session(self) -> Session | None:
        """「当前会话」：活动连接所在的会话；没有就取最近一个未结束的；都没有返回 ``None``。"""
        if self._live is not None:
            return await self._store.get_session(self._live.session.id)
        return await self._store.latest_unended_session()

    async def push(self, data: dict) -> bool:
        """向活动连接的浏览器推一条数据通道消息；没有活动连接返回 ``False``。"""
        live = self._live
        if live is None or live.worker is None:
            return False
        try:
            await live.worker.queue_frame(RTVIServerMessageFrame(data=data))
        except Exception:
            logger.exception("向浏览器推送消息失败")
            return False
        return True

    async def push_to(self, session_id: str, data: dict) -> bool:
        """只在 ``session_id`` 这场会议正在进行时推消息（后台任务完成时，会议可能已经换了）。"""
        if not self.is_live(session_id):
            return False
        return await self.push(data)

    async def queue_frame_to(self, session_id: str, frame: Any) -> bool:
        """只在 ``session_id`` 这场会议正在进行时，向它的管线送一帧（比如往实时模型的上下文追加一行）。"""
        live = self._live
        if live is None or live.worker is None or live.session.id != session_id:
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
        live = self._live
        if live is None or live.session.id != session_id:
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

    async def wait_idle(self, wait_secs: float = 5.0) -> None:
        """应用关闭时：等活动连接把连接记录写完（有超时）。"""
        live = self._live
        if live is None:
            return
        try:
            await asyncio.wait_for(live.done.wait(), wait_secs)
        except TimeoutError:
            logger.warning("应用关闭时活动连接没有及时收尾")

    # ------------------------------------------------------------------ #
    # 连接的生命周期
    # ------------------------------------------------------------------ #

    async def begin(self) -> LiveConnection:
        """新连接、新会议：顶替旧连接，建新会话和连接记录，登记为当前连接。

        别的没结束的会议（已中断的）原样留着，之后还能继续。
        """
        async with self._lock:
            if self._live is not None:
                await self._stop(self._live, "taken_over")
            await self._drop_stream()
            now = self._now()
            session = await self._store.create_session(now=now)
            connection_id = await self._store.open_connection(
                session.id, connected_at=now, t_from=0.0
            )
            live = LiveConnection(session, connection_id, now)
            self._live = live
            return live

    async def attach(self, session_id: str) -> LiveConnection:
        """新连接、继续一场已有的会议：顶替旧连接（哪怕就是这场会议的），已结束的重新打开，登记为当前连接。

        ``base_secs`` = 此刻 − 会议的 0 点，在这里对一次表，之后只按采样数推进。它不会早于时间轴已经用到的地方
        （上一次连接的终点、最后一条发言的终点）：音频时钟和墙上时钟有细微出入，马上重连时不能让时间倒着走。
        会议不存在抛 ``SessionNotFound``。
        """
        async with self._lock:
            session = await self._store.get_session(session_id)
            if session is None:
                raise SessionNotFound(session_id)
            if self._live is not None:
                await self._stop(self._live, "taken_over")
            if self._stream is not None and self._stream.session_id != session_id:
                await self._drop_stream()
            if session.ended_at is not None:
                session = await self._store.reopen_session(session_id) or session
            now = self._now()
            base_secs = max(now - session.started_at, await self._store.timeline_end(session_id))
            connection_id = await self._store.open_connection(
                session_id, connected_at=now, t_from=base_secs
            )
            live = LiveConnection(session, connection_id, now, base_secs=base_secs, resumed=True)
            self._live = live
            return live

    async def diarizer_for(
        self,
        live: LiveConnection,
        build: Callable[[], Awaitable[Diarizer]],
        notice: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> Diarizer:
        """这路连接用的说话人区分。

        手里有这场会议的流、而且它没出过错：接着用（记下这次连接的接续点）。否则用 ``build`` 新开一条；
        继续一场已经有说话人的会议时，新流的编号加上已有的最大编号作偏移，并通过 ``notice`` 说明。
        ``build`` 给出的是 ``NullDiarizer``（没开、或启动失败已降级）时原样返回，不留着。
        """
        stream = self._stream
        if stream is not None and stream.session_id == live.session.id and not stream.failed:
            stream.begin_connection(live.base_secs)
            return stream
        await self._drop_stream()
        inner = await build()
        if isinstance(inner, NullDiarizer):
            return inner
        offset = 0
        if live.resumed:
            try:
                offset = await self._store.max_speaker_idx(live.session.id)
            except Exception:
                logger.exception("读取已有的说话人编号失败，新流的编号不加偏移")
            if offset > 0 and notice is not None:
                await notice("info", RENUMBERED_NOTICE)
        stream = SessionDiarizer(inner, session_id=live.session.id, speaker_offset=offset)
        stream.begin_connection(live.base_secs)
        self._stream = stream
        return stream

    async def merge_speakers(self, session_id: str, src: int, dst: int) -> dict | None:
        """把说话人 ``src`` 并入 ``dst``。两个说话人有一个不存在（或不能合并）返回 ``None``。

        改的是正在进行的会议时：说话人区分之后再输出 ``src`` 也算作 ``dst``，并通知页面。
        返回 ``{"from", "into", "display_name", "moved"}``。
        """
        moved = await self._store.merge_speakers(session_id, src, dst)
        if moved is None:
            return None
        name = await self._store.speaker_name(session_id, dst)
        if self._stream is not None and self._stream.session_id == session_id:
            self._stream.merge(src, dst)
        result = {"from": src, "into": dst, "display_name": name, "moved": moved}
        live = self._live
        if live is not None and live.session.id == session_id:
            if live.recorder is not None:
                live.recorder.set_speaker_name(src, name)  # 已经在路上的字幕也用合并后的名字
            await self.push(
                {"type": "speakers_merged", "from": src, "into": dst, "display_name": name}
            )
        return result

    async def forget(self, session_id: str) -> None:
        """这场会议被删掉了：手里要是还留着它的说话人区分流，释放掉。"""
        if self._stream is not None and self._stream.session_id == session_id:
            await self._drop_stream()

    async def shutdown(self, wait_secs: float = SHUTDOWN_TIMEOUT_SECS) -> None:
        """应用退出：告诉页面「服务正在停止」并停掉活动连接（最多等 ``wait_secs`` 秒），释放说话人区分流。"""
        async with self._lock:
            if self._live is not None:
                await self._stop(self._live, "server_stopping", wait_secs=wait_secs)
            await self._drop_stream()

    async def register(
        self, live: LiveConnection, worker: Any, recorder: Any, gate: Any = None
    ) -> None:
        """管线造好后登记它的 worker、记录器和模态闸门。登记之前就有人要求停止的，现在立刻取消。"""
        live.worker, live.recorder, live.gate = worker, recorder, gate
        if live.stop_requested:
            await worker.cancel()

    async def finish(self, live: LiveConnection) -> None:
        """连接结束（无论怎么结束）：关闭连接记录，释放位置。不写 ``ended_at``。"""
        try:
            elapsed = float(getattr(live.recorder, "elapsed_secs", 0.0) or 0.0)
            await self._store.close_connection(
                live.connection_id, disconnected_at=self._now(), t_to=elapsed
            )
        except Exception:
            logger.exception("关闭连接记录失败")
        finally:
            if self._live is live:
                self._live = None
            live.done.set()
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
            live = self._live
            if live is not None and live.session.id == session_id:
                await self._stop(live, "ended")
            if self._stream is not None and self._stream.session_id == session_id:
                await self._drop_stream()
            return await self._store.end_session(session_id, now=self._now())

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    async def _drop_stream(self) -> None:
        stream, self._stream = self._stream, None
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
            if self._live is live:
                self._live = None
