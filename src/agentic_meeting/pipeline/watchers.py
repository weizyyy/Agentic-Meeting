"""单进程按会事件历史、临时状态和实际旁听连接；不镜像全部 RTVI 提示。"""

from __future__ import annotations

import asyncio
import copy
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

EVENT_LIMIT = 256
# 回答文字是累计全文，逐个 token 发会很快挤满历史和队列；中途最多这么久发一次，结束时总会发全文。
ANSWER_TEXT_SECS = 0.25

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
    """一条真实订阅；名单、队列及释放使用同一个对象。"""

    id: int
    session_id: str
    device: str
    since: float
    source: SessionEvents
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(EVENT_LIMIT))
    overflow: bool = False

    def item(self) -> dict[str, Any]:
        return {"id": self.id, "device": self.device, "since": self.since}


class SessionEvents:
    def __init__(self, session_id: str, epoch: str) -> None:
        self.session_id = session_id
        self.epoch = epoch
        self.seq = 0
        self.history: deque[dict] = deque(maxlen=EVENT_LIMIT)
        self.subscribers: set[Watcher] = set()
        self.transient: dict[str, Any] = {
            "captions": {},
            "answer": None,
            "assistant_state": "idle",
            "speaking": False,
        }

    def envelope(self, data: dict, *, seq: int | None = None) -> dict:
        return {
            "session_id": self.session_id,
            "epoch": self.epoch,
            "seq": self.seq if seq is None else seq,
            "data": data,
        }

    def publish(self, data: dict) -> None:
        data = copy.deepcopy(data)
        kind = data["type"]
        if kind == "caption":
            if data["stable"] or data["unstable"]:
                self.transient["captions"][data["segment_id"]] = dict(data)
            else:
                self.transient["captions"].pop(data["segment_id"], None)
        elif kind == "utterance":
            self.transient["captions"].pop(data.get("segment_id"), None)
        elif kind in {"speaker", "speakers_merged"}:
            idx = data["idx"] if kind == "speaker" else data["from"]
            for caption in self.transient["captions"].values():
                if caption["speaker_idx"] == idx:
                    caption["speaker_name"] = data["display_name"]
                    if kind == "speakers_merged":
                        caption["speaker_idx"] = data["into"]
        elif kind == "assistant_answer":
            self.transient["answer"] = data
            self.transient["assistant_state"] = (
                "speaking"
                if self.transient["speaking"]
                else ("thinking" if data["phase"] in {"started", "text"} else "idle")
            )
        elif kind == "assistant_state":
            self.transient["assistant_state"] = data["state"]
        elif kind == "assistant_speaking":
            self.transient["speaking"] = data["speaking"]
            self.transient["assistant_state"] = "speaking" if data["speaking"] else "idle"
        elif kind == "session_state" and data["state"] != "live":
            self.transient.update(captions={}, speaking=False, assistant_state="idle")
            answer = self.transient["answer"]
            if answer and answer["phase"] in {"started", "text"}:
                self.transient["answer"] = {**answer, "phase": "interrupted"}
        self.seq += 1
        event = self.envelope(data)
        self.history.append(event)
        for subscriber in self.subscribers:
            if subscriber.overflow:
                continue
            try:
                subscriber.queue.put_nowait(event)
            except asyncio.QueueFull:
                subscriber.overflow = True

    def subscribe(
        self, cursor: str | None, subscriber: Watcher
    ) -> tuple[Watcher, bool, list[dict]]:
        """校验游标、选重放、登记订阅之间没有 await，生产者不能插入。"""
        self.subscribers.add(subscriber)
        valid = False
        replay: list[dict] = []
        if cursor:
            epoch, separator, number = cursor.rpartition(":")
            if (
                separator
                and epoch == self.epoch
                and len(number) <= 20
                and number.isascii()
                and number.isdigit()
            ):
                seq = int(number)
                oldest = self.history[0]["seq"] - 1 if self.history else self.seq
                valid = oldest <= seq <= self.seq
                if valid:
                    replay = [event for event in self.history if event["seq"] > seq]
        return subscriber, valid, replay

    def snapshot_transient(self) -> dict:
        state = copy.deepcopy(self.transient)
        state["captions"] = list(state["captions"].values())
        return state


class WatcherHub:
    """唯一旁听入口：每会一个有界事件源，实际订阅同时构成名单。"""

    def __init__(self, *, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self.epoch = uuid.uuid4().hex
        self.sources: dict[str, SessionEvents] = {}
        self._next_id = 1
        self._tasks: set[asyncio.Task[None]] = set()
        self.on_change: Callable[[str], Awaitable[None]] | None = None

    def for_session(self, session_id: str) -> SessionEvents:
        if session_id not in self.sources:
            self.sources[session_id] = SessionEvents(session_id, f"{self.epoch}.{session_id}")
        return self.sources[session_id]

    def items(self, session_id: str) -> list[dict[str, Any]]:
        source = self.sources.get(session_id)
        return (
            sorted((w.item() for w in source.subscribers), key=lambda w: w["id"]) if source else []
        )

    def subscribe(
        self, session_id: str, device: str, cursor: str | None
    ) -> tuple[Watcher, bool, list[dict]]:
        source = self.for_session(session_id)
        watcher = Watcher(self._next_id, session_id, device, self._now(), source)
        self._next_id += 1
        result = source.subscribe(cursor, watcher)
        self._changed(session_id)
        return result

    def unsubscribe(self, watcher: Watcher) -> None:
        if watcher in watcher.source.subscribers:
            watcher.source.subscribers.remove(watcher)
            self._changed(watcher.session_id)

    def publish(self, session_id: str, data: dict[str, Any]) -> None:
        if data["type"] not in {"watchers", "session", "session_closed"}:
            self.for_session(session_id).publish(data)

    def state(self, session_id: str, state: str) -> None:
        self.publish(session_id, {"type": "session_state", "state": state})

    def close_session(self, session_id: str) -> None:
        self.publish(session_id, {"type": "session_deleted"})
        self.sources.pop(session_id, None)

    def close_all(self) -> None:
        for source in self.sources.values():
            for watcher in list(source.subscribers):
                self.close_watcher(watcher)

    def close_watcher(self, watcher: Watcher) -> None:
        """只撤销这条订阅，未发送内容不再交付。"""
        while not watcher.queue.empty():
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
    def __init__(self, hub: WatcherHub, session_id: str) -> None:
        super().__init__(observe_every_push=False)
        self._publish = lambda data: hub.publish(session_id, data)
        self._response_id = ""
        self._text = ""
        self._answering = False
        self._text_sent_at = 0.0

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        phase = None
        if isinstance(frame, LLMFullResponseStartFrame):
            self._response_id = uuid.uuid4().hex
            self._text = ""
            self._text_sent_at = 0.0
            self._answering = True
            phase = "started"
        elif isinstance(frame, LLMTextFrame) and self._answering:
            self._text += frame.text
            now = time.monotonic()
            if now - self._text_sent_at >= ANSWER_TEXT_SECS:
                self._text_sent_at = now
                phase = "text"
        elif isinstance(frame, LLMFullResponseEndFrame) and self._answering:
            self._answering = False
            phase = "ended"
        elif isinstance(frame, InterruptionFrame) and self._answering:
            self._answering = False
            phase = "interrupted"
        elif isinstance(frame, (BotStartedSpeakingFrame, BotStoppedSpeakingFrame)):
            self._publish(
                {
                    "type": "assistant_speaking",
                    "speaking": isinstance(frame, BotStartedSpeakingFrame),
                }
            )
        if phase:
            self._publish(
                {
                    "type": "assistant_answer",
                    "response_id": self._response_id,
                    "phase": phase,
                    "text": self._text,
                }
            )
