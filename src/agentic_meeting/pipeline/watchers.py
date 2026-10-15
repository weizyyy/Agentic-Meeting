"""旁观一场会议的设备（docs/interfaces.md §5.1「Following a meeting live」、§6.1 ``watchers``）。

别的设备不持有连接、只看会议时，开一条服务端推送的事件流（``GET /api/sessions/{id}/events``）。
会议页面收到的数据通道消息里，字幕、说话人、截图、任务这几类原样再推给旁观的设备；提示、助理状态这些只给会议页面。

* **怎么拿到消息**：管线里推给浏览器的消息有好几个出处（会议记录器、识别服务、从管线外 ``queue_frame``），
  所以不在每个出处各发一遍，而是给管线挂一个观察者（``WatcherObserver``），每条 ``RTVIServerMessageFrame``
  第一次被推出时抄一份给 ``WatcherHub``。
* **谁在旁观**：每条事件流登记一个旁观者，设备说明由 ``User-Agent`` 得出（``device_label``），只存在内存里。
  名单变了，会话管理器把 ``watchers`` 消息推给会议页面。
* **会议状态**：连接就绪、断开、会议结束时由会话管理器调 ``state``，旁观的设备据此刷新会议信息。
* **跟不上的设备**：每条流最多攒 ``MAX_PENDING`` 条没发出去的消息，再多就断开它；浏览器的 ``EventSource``
  会自己重连，重连后页面按最后一条字幕的编号补齐。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame

# 原样转给旁观设备的消息类型；其余（提示、助理状态、session、session_closed、watchers）只给会议页面
FORWARDED_TYPES = frozenset(
    {
        "caption",
        "utterance",
        "utterance_update",
        "speaker",
        "speakers_merged",
        "frame",
        "frame_caption",
        "task",
        "task_event",
    }
)
MAX_PENDING = 500
PING_SECS = 15.0

# 按顺序匹配：先认更具体的（iPad 的 UA 里也有 Mac、安卓的 UA 里也有 Linux，Edge 的 UA 里也有 Chrome）
_PLATFORMS = (
    (re.compile(r"iPhone"), "iPhone"),
    (re.compile(r"iPad"), "iPad"),
    (re.compile(r"Android"), "Android"),
    (re.compile(r"CrOS"), "ChromeOS"),
    (re.compile(r"Windows"), "Windows"),
    (re.compile(r"Macintosh|Mac OS X"), "macOS"),
    (re.compile(r"Linux"), "Linux"),
)
_BROWSERS = (
    (re.compile(r"Edg(e|A|iOS)?/"), "Edge"),
    (re.compile(r"OPR/|Opera"), "Opera"),
    (re.compile(r"Firefox/|FxiOS/"), "Firefox"),
    (re.compile(r"Chrome/|CriOS/|Chromium/"), "Chrome"),
    (re.compile(r"Safari/"), "Safari"),
)


def device_label(user_agent: str) -> str:
    """``iPhone · Safari`` 这样的设备说明；认不出来的部分略去，都认不出来是空字符串。"""

    def first(table: tuple[tuple[re.Pattern[str], str], ...]) -> str | None:
        return next((name for pattern, name in table if pattern.search(user_agent)), None)

    return " · ".join(p for p in (first(_PLATFORMS), first(_BROWSERS)) if p)


@dataclass(eq=False)
class Watcher:
    """一条打开着的事件流。``queue`` 里放的是要推的消息，``None`` 表示流该结束了。"""

    id: int
    session_id: str
    device: str
    since: float
    queue: asyncio.Queue[dict[str, Any] | None] = field(default_factory=asyncio.Queue)
    closed: bool = False

    def item(self) -> dict[str, Any]:
        return {"id": self.id, "device": self.device, "since": self.since}

    async def next(self) -> dict[str, Any] | None:
        return await self.queue.get()


class WatcherHub:
    """各场会议的旁观者名单和他们的消息队列。全部方法都是同步的，可以在已被取消的请求的收尾里调用。"""

    def __init__(self, *, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._watchers: dict[str, list[Watcher]] = {}
        self._next_id = 1
        self._tasks: set[asyncio.Task[None]] = set()
        # 名单变了之后调用，参数是会话编号；会话管理器用它把 watchers 消息推给会议页面。出错只记日志。
        self.on_change: Callable[[str], Awaitable[None]] | None = None

    def items(self, session_id: str) -> list[dict[str, Any]]:
        return [w.item() for w in self._watchers.get(session_id, [])]

    def subscribe(self, session_id: str, device: str, state: str) -> Watcher:
        """登记一个旁观者；它收到的第一条消息是会议此刻的状态。"""
        watcher = Watcher(self._next_id, session_id, device, self._now())
        self._next_id += 1
        watcher.queue.put_nowait({"type": "state", "state": state})
        self._watchers.setdefault(session_id, []).append(watcher)
        self._changed(session_id)
        return watcher

    def unsubscribe(self, watcher: Watcher) -> None:
        """事件流结束了（设备断开、被踢掉、应用退出）：从名单里去掉。重复调用没有影响。"""
        watcher.closed = True
        group = self._watchers.get(watcher.session_id)
        if group is None or watcher not in group:
            return
        group.remove(watcher)
        if not group:
            del self._watchers[watcher.session_id]
        self._changed(watcher.session_id)

    def publish(self, session_id: str, data: dict[str, Any]) -> None:
        """会议页面收到的一条消息：属于转发的类型就抄给这场会议的每个旁观者。"""
        if data.get("type") in FORWARDED_TYPES:
            self._send(session_id, data)

    def state(self, session_id: str, state: str) -> None:
        """会议状态变了：``live`` / ``interrupted`` / ``ended``。"""
        self._send(session_id, {"type": "state", "state": state})

    def close_session(self, session_id: str) -> None:
        """这场会议被删掉了：结束它所有的事件流。"""
        for watcher in list(self._watchers.get(session_id, [])):
            self._close(watcher)

    def close_all(self) -> None:
        """应用要退出了：结束全部事件流。它们是不会自己结束的请求，不结束的话 uvicorn 会一直等。"""
        for group in list(self._watchers.values()):
            for watcher in list(group):
                self._close(watcher)

    def _send(self, session_id: str, data: dict[str, Any]) -> None:
        for watcher in list(self._watchers.get(session_id, [])):
            if watcher.queue.qsize() >= MAX_PENDING:
                logger.warning(f"旁观设备 {watcher.id} 跟不上推送，断开它，等它重连后补齐")
                self._close(watcher)
            else:
                watcher.queue.put_nowait(data)

    def _close(self, watcher: Watcher) -> None:
        while not watcher.queue.empty():  # 没发出去的不再发：被踢掉的设备重连后自己补齐
            watcher.queue.get_nowait()
        watcher.queue.put_nowait(None)
        self.unsubscribe(watcher)

    def _changed(self, session_id: str) -> None:
        callback = self.on_change
        if callback is None:
            return

        async def run() -> None:
            try:
                await callback(session_id)
            except Exception:
                logger.exception("通知会议页面旁观名单变化时出错")

        try:
            task = asyncio.get_running_loop().create_task(run())
        except RuntimeError:
            return  # 没有事件循环（不会发生在请求里）
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class WatcherObserver(BaseObserver):
    """挂在一场会议的管线上：每条推给浏览器的消息第一次被推出时抄一份给 ``WatcherHub``。"""

    def __init__(self, hub: WatcherHub, session_id: str) -> None:
        super().__init__(observe_every_push=False)
        self._hub = hub
        self._session_id = session_id

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        if isinstance(frame, RTVIServerMessageFrame) and isinstance(frame.data, dict):
            self._hub.publish(self._session_id, frame.data)
