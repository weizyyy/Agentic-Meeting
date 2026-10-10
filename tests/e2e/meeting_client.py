"""一个用 aiortc 写的会议页面替身：和浏览器一样走 ``POST /api/offer`` 建 WebRTC 连接，
麦克风轨道送真实的语音录音，数据通道上说 RTVI 协议（client-ready、client-message），
收集服务端推来的消息和助理的声音。
"""

from __future__ import annotations

import asyncio
import contextlib
import fractions
import json
import time
import uuid
import wave
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from av import AudioFrame

REPO_ROOT = Path(__file__).resolve().parents[2]
# 一段真实的中文朗读（16 kHz 单声道，约 6.7 秒），来自 CI 同样会检出的子模块。
SPEECH_WAV = REPO_ROOT / "third_party" / "Confucius4-R2T2" / "resources" / "test.wav"

SAMPLE_RATE = 48000
FRAME_SAMPLES = 960  # 20 毫秒


def load_speech() -> np.ndarray:
    """录音，重采样到 48 kHz（WebRTC 的 Opus 用这个采样率）。"""
    with wave.open(str(SPEECH_WAV)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    return np.repeat(pcm, 3)


class Microphone(MediaStreamTrack):
    """按实时节奏出 20 毫秒的帧；排了录音就放录音，否则是静音。"""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._queue = np.zeros(0, dtype="<i2")
        self._start: float | None = None
        self._pts = 0
        self.played = asyncio.Event()

    def play(self, pcm: np.ndarray) -> None:
        self.played.clear()
        self._queue = np.concatenate([self._queue, pcm.astype("<i2")])

    async def recv(self) -> AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        if self._start is None:
            self._start = time.time()
        else:
            self._pts += FRAME_SAMPLES
            await asyncio.sleep(max(0.0, self._start + self._pts / SAMPLE_RATE - time.time()))
        chunk = self._queue[:FRAME_SAMPLES]
        self._queue = self._queue[FRAME_SAMPLES:]
        if len(chunk) < FRAME_SAMPLES:
            if len(chunk) or not len(self._queue):
                self.played.set()
            chunk = np.concatenate([chunk, np.zeros(FRAME_SAMPLES - len(chunk), dtype="<i2")])
        frame = AudioFrame(format="s16", layout="mono", samples=FRAME_SAMPLES)
        frame.planes[0].update(chunk.tobytes())
        frame.pts = self._pts
        frame.sample_rate = SAMPLE_RATE
        frame.time_base = fractions.Fraction(1, SAMPLE_RATE)
        return frame


class MeetingClient:
    """一路会议连接。``messages`` 是收到的全部服务端消息（RTVI ``server-message`` 的 ``data``）。"""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self.pc = RTCPeerConnection()
        self.mic = Microphone()
        self.messages: list[dict[str, Any]] = []
        self.rtvi: list[dict[str, Any]] = []  # 其他 RTVI 消息（bot-ready、bot-llm-text 等）
        self.bot_audio_frames = 0
        self._changed = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._channel = self.pc.createDataChannel("rtvi")

        @self._channel.on("message")
        def on_message(raw: str | bytes) -> None:
            if isinstance(raw, bytes) or raw.startswith("ping"):
                return
            data = json.loads(raw)
            if data.get("type") == "server-message":
                self.messages.append(data["data"])
            else:
                self.rtvi.append(data)
            self._changed.set()

        @self.pc.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            if track.kind == "audio":
                self._tasks.append(asyncio.create_task(self._listen(track)))

    async def connect(self, session_id: str | None = None) -> dict[str, Any]:
        """协商、等数据通道打开、发 client-ready，返回服务端的 ``session`` 消息。"""
        self.pc.addTransceiver(self.mic, direction="sendrecv")
        self.pc.addTransceiver("video", direction="recvonly")
        await self.pc.setLocalDescription(await self.pc.createOffer())
        request: dict[str, Any] = {
            "sdp": self.pc.localDescription.sdp,
            "type": self.pc.localDescription.type,
        }
        if session_id is not None:
            request["requestData"] = {"session_id": session_id}
        response = await self._http.post("/api/offer", json=request)
        response.raise_for_status()
        answer = response.json()
        await self.pc.setRemoteDescription(RTCSessionDescription(answer["sdp"], answer["type"]))
        await self._wait(lambda: self._channel.readyState == "open", "数据通道打开")
        self._channel_send("client-ready", {"version": "1.0.0", "about": {"library": "e2e-test"}})
        self._ping = asyncio.create_task(self._keepalive())
        return await self.next_message("session")

    async def close(self) -> None:
        if self.pc.connectionState == "closed":
            return
        for task in [*self._tasks, getattr(self, "_ping", None)]:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, MediaStreamError):
                    await task
        await self.pc.close()

    # ---- 动作 ----

    async def speak(self, seconds: float = 2.0, timeout_secs: float = 30) -> None:
        """放录音的前 ``seconds`` 秒（默认的 2 秒正好是一句话），再放 1 秒静音，等它放完。"""
        speech = load_speech()[: int(seconds * SAMPLE_RATE)]
        self.mic.play(np.concatenate([speech, np.zeros(SAMPLE_RATE, dtype="<i2")]))
        await asyncio.wait_for(self.mic.played.wait(), timeout_secs)

    def type_text(self, text: str) -> None:
        self.send_client_message("text_input", {"text": text})

    def send_client_message(self, kind: str, data: Any) -> None:
        self._channel_send("client-message", {"t": kind, "d": data})

    # ---- 等待 ----

    async def next_message(
        self, kind: str, timeout_secs: float = 30, **fields: Any
    ) -> dict[str, Any]:
        """等第一条类型为 ``kind``、并且字段都等于 ``fields`` 的消息（已收到的也算）。"""

        def match() -> dict[str, Any] | None:
            for message in self.messages:
                if message.get("type") == kind and all(
                    message.get(k) == v for k, v in fields.items()
                ):
                    return message
            return None

        return await self._wait(match, f"收到 {kind} 消息 {fields or ''}", timeout_secs)

    async def wait_for(self, predicate: Any, description: str, timeout_secs: float = 30) -> Any:
        return await self._wait(predicate, description, timeout_secs)

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.messages if m.get("type") == kind]

    def bot_text(self) -> str:
        return "".join(
            m.get("data", {}).get("text", "") for m in self.rtvi if m.get("type") == "bot-llm-text"
        )

    # ---- 内部 ----

    async def _wait(self, predicate: Any, description: str, timeout_secs: float = 30) -> Any:
        deadline = time.monotonic() + timeout_secs
        while True:
            value = predicate()
            if value:
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    f"{description}：{timeout_secs} 秒内没有等到。已收到：{self.messages[-10:]}"
                )
            self._changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), min(remaining, 0.1))

    def _channel_send(self, kind: str, data: Any) -> None:
        message = {"label": "rtvi-ai", "type": kind, "id": uuid.uuid4().hex[:8], "data": data}
        self._channel.send(json.dumps(message))

    async def _keepalive(self) -> None:
        # 浏览器端 SDK 也这么做：服务端靠它判断连接还活着
        while True:
            if self._channel.readyState == "open":
                self._channel.send(f"ping: {int(time.time() * 1000)}")
            await asyncio.sleep(1)

    async def _listen(self, track: MediaStreamTrack) -> None:
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError:
                return
            pcm = frame.to_ndarray()
            if np.abs(pcm).max() > 1000:
                self.bot_audio_frames += 1
                self._changed.set()
