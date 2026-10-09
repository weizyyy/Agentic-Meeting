"""长会议压力测试：不经过浏览器，把一段录音按真实速度送进正在运行的应用，定时叫助理并记录各项指标。

用法（应用和推理服务要先跑起来，例如 ``uv run agentic-meeting serve --with-services``）::

    uv run python scripts/soak.py --audio 录音.mp3 --minutes 120

这个脚本就是一个没有界面的客户端：和浏览器一样走 ``POST /api/offer`` 建立 WebRTC 连接，音频轨里放录音
（放完从头循环），数据通道里收字幕、发文字提问。因此它压到的是完整的应用——收音增强、语音检测、识别、
说话人区分、落库、唤醒、实时模型、语音合成、截图以外的全部链路。

每隔一段时间做两件事并记一行：

* **叫一次助理**：把事先用语音合成服务合成好的一句「<助理名字>，……」插进音频轨（录音暂停，等助理答完再继续），
  记录从这句话播完到助理第一个字、第一声的耗时；
* **打字问一次**：从数据通道发一条文字提问，记录到第一个字的耗时。

同时记录：应用进程和各推理服务进程的内存、各显卡的显存、字幕落后多少（发言定稿时，已送出的音频比这条发言的
结束时间多出多少秒）。结果逐行写进 ``--out`` 指定的 JSONL 文件，结束时打印汇总。

所有模型、地址、名字都取自配置，这里不写任何模型名。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fractions
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import av
import httpx
import numpy as np
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from pipecat.processors.frameworks.rtvi import models as rtvi

from agentic_meeting.config import AppConfig, load_config

TRACK_RATE = 48000  # WebRTC 音频轨的采样率
FRAME_SAMPLES = 960  # 20 毫秒一帧
FRAME_SECS = FRAME_SAMPLES / TRACK_RATE
SILENCE = np.zeros(FRAME_SAMPLES, dtype=np.int16)
# 叫助理之后最多等这么久；超过就记为没有应答，继续放录音。
ANSWER_TIMEOUT_SECS = 45.0
# 助理答完之后再静这么久才继续放录音，免得录音里的人声被当成对助理的追问。
AFTER_ANSWER_SECS = 1.5
# 插播之前先静这么久，让叫助理的那句话自成一段。
CLIP_LEAD_SECS = 0.8
SERVICE_PROCESS_NAMES = ("llama-server", "tts-server")

DEFAULT_SPOKEN = [
    "{name}，请用一句话说说刚才大家在讨论什么。",
    "{name}，你觉得刚才的讨论里最重要的一点是什么？",
    "{name}，帮我回忆一下最开始讲了什么。",
]
DEFAULT_TYPED = [
    "用一句话概括最近五分钟的讨论。",
    "到现在为止一共有几位发言人？",
    "刚才有没有人提到需要会后跟进的事情？",
]


# ---------------------------------------------------------------------------
# 音频
# ---------------------------------------------------------------------------


def decode_frames(path: Path, start_secs: float = 0.0) -> Iterator[np.ndarray]:
    """把音频文件解码成 48 kHz 单声道、每帧 20 毫秒的 int16 数组；边读边解，不整段放进内存。"""
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        if start_secs > 0:
            container.seek(int(start_secs / stream.time_base), stream=stream)
        resampler = av.AudioResampler(format="s16", layout="mono", rate=TRACK_RATE)
        pending = np.zeros(0, dtype=np.int16)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                pending = np.concatenate([pending, out.to_ndarray().reshape(-1)])
            while len(pending) >= FRAME_SAMPLES:
                yield pending[:FRAME_SAMPLES]
                pending = pending[FRAME_SAMPLES:]


def to_track_frames(pcm: np.ndarray, rate: int) -> list[np.ndarray]:
    """把一段单声道 int16 音频重采样到音频轨的采样率，切成 20 毫秒的帧（末尾补零）。"""
    if rate != TRACK_RATE:
        n = int(len(pcm) * TRACK_RATE / rate)
        pcm = np.interp(
            np.linspace(0, len(pcm), n, endpoint=False), np.arange(len(pcm)), pcm
        ).astype(np.int16)
    pad = (-len(pcm)) % FRAME_SAMPLES
    pcm = np.concatenate([pcm, np.zeros(pad, dtype=np.int16)])
    return [pcm[i : i + FRAME_SAMPLES] for i in range(0, len(pcm), FRAME_SAMPLES)]


def synthesize(cfg: AppConfig, text: str) -> np.ndarray:
    """向配置里的语音合成服务要一句话的音频（int16 单声道，采样率是 ``tts.sample_rate``）。"""
    body = {
        "model": cfg.tts.model,
        "input": text,
        "voice": cfg.tts.voice,
        "language": cfg.tts.language,
        "response_format": "pcm",
    }
    url = cfg.tts.base_url.rstrip("/") + "/audio/speech"
    response = httpx.post(url, json=body, timeout=300)
    response.raise_for_status()
    raw = response.content
    return np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int16)


class MeetingTrack(MediaStreamTrack):
    """按真实速度出帧的音频轨：平时放录音（放完从头再来），被要求时插播一段话，之后静音到放行为止。"""

    kind = "audio"

    def __init__(self, path: Path, start_secs: float = 0.0) -> None:
        super().__init__()
        self._path = path
        self._frames = decode_frames(path, start_secs)
        self._clip: list[np.ndarray] = []
        self._hold = False
        self._started: float | None = None
        self._count = 0
        self.loops = 0
        self.clip_done: asyncio.Event = asyncio.Event()

    @property
    def sent_secs(self) -> float:
        """已经送出去多少秒的音频（含插播和静音）；等于这次连接在服务端时间轴上走到了哪里。"""
        return self._count * FRAME_SECS

    def play_clip(self, frames: list[np.ndarray]) -> None:
        """插播一段话；播完后保持静音，直到调用 ``resume``。"""
        self.clip_done.clear()
        # 先静一小会儿：紧接着录音里的人声播出去，会被识别成那个人这句话的一部分
        self._clip = [SILENCE] * int(CLIP_LEAD_SECS / FRAME_SECS) + list(frames)
        self._hold = True

    def resume(self) -> None:
        self._hold = False

    def _next_samples(self) -> np.ndarray:
        if self._clip:
            samples = self._clip.pop(0)
            if not self._clip:
                self.clip_done.set()
            return samples
        if self._hold:
            return SILENCE
        try:
            return next(self._frames)
        except StopIteration:
            self.loops += 1
            self._frames = decode_frames(self._path)
            return next(self._frames, SILENCE)

    async def recv(self) -> av.AudioFrame:
        if self._started is None:
            self._started = time.perf_counter()
        wait = self._started + self._count * FRAME_SECS - time.perf_counter()
        if wait > 0:
            await asyncio.sleep(wait)
        samples = self._next_samples()
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = TRACK_RATE
        frame.pts = self._count * FRAME_SAMPLES
        frame.time_base = fractions.Fraction(1, TRACK_RATE)
        self._count += 1
        return frame


# ---------------------------------------------------------------------------
# 进程与显卡
# ---------------------------------------------------------------------------


def _run(command: list[str]) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def listening_pid(port: int) -> int | None:
    """监听这个端口的进程号（应用进程）；查不到返回 None。"""
    if platform.system() == "Windows":
        for line in _run(["netstat", "-ano", "-p", "TCP"]).splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{port}"):
                return int(parts[4])
        return None
    out = _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"]).split()
    return int(out[0]) if out else None


def process_memory_mb() -> dict[int, tuple[str, float]]:
    """所有进程的 {进程号: (名字, 常驻内存 MB)}。"""
    result: dict[int, tuple[str, float]] = {}
    if platform.system() == "Windows":
        for line in _run(["tasklist", "/FO", "CSV", "/NH"]).splitlines():
            cells = [c.strip('"') for c in line.split('","')]
            if len(cells) >= 5:
                digits = re.sub(r"\D", "", cells[4])
                with contextlib.suppress(ValueError):
                    result[int(cells[1])] = (cells[0], int(digits or 0) / 1024)
        return result
    for line in _run(["ps", "-eo", "pid=,rss=,comm="]).splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            result[int(parts[0])] = (os.path.basename(parts[2]), int(parts[1]) / 1024)
    return result


def gpu_memory_mb() -> list[int]:
    """各显卡已用显存（MB）；没有 nvidia-smi 时返回空列表。"""
    out = _run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"])
    return [int(x) for x in out.split() if x.strip().isdigit()]


def resource_snapshot(app_pid: int | None) -> dict[str, Any]:
    processes = process_memory_mb()
    services: dict[str, float] = {}
    for name, mb in processes.values():
        stem = name.lower().removesuffix(".exe")
        if stem in SERVICE_PROCESS_NAMES:
            services[stem] = round(services.get(stem, 0.0) + mb, 1)
    app = processes.get(app_pid) if app_pid is not None else None
    return {
        "app_rss_mb": round(app[1], 1) if app else None,
        "services_rss_mb": services,
        "gpu_used_mb": gpu_memory_mb(),
    }


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


@dataclass
class Stats:
    """跑的过程中攒下来的数字。"""

    utterance_ids: set[int] = field(default_factory=set)  # 并进已有发言的片段不重复计数
    assistant_lines: int = 0  # 助理说了几次话（应当等于提问的次数：没人叫它时不该开口）
    speakers: set[int] = field(default_factory=set)
    lags: list[float] = field(default_factory=list)  # 最近一个报告周期内的字幕落后
    all_lags: list[float] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    spoken: list[dict[str, Any]] = field(default_factory=list)
    typed: list[dict[str, Any]] = field(default_factory=list)


class SoakClient:
    """一个没有界面的会议客户端。"""

    def __init__(
        self, base_url: str, track: MeetingTrack, session_id: str | None, log: Any
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.track = track
        self.session_id = session_id
        self.log = log
        self.stats = Stats()
        self.pc = RTCPeerConnection()
        self.channel = self.pc.createDataChannel("chat", ordered=True)
        self.base_secs = 0.0
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()
        # 一次提问的计时：发出之后等第一个字、第一声、说完
        self._first_text: asyncio.Event = asyncio.Event()
        self._first_audio: asyncio.Event = asyncio.Event()
        self._answered: asyncio.Event = asyncio.Event()
        self._answer_text: list[str] = []
        self._tasks: list[asyncio.Task] = []

    async def connect(self) -> None:
        self.pc.addTrack(self.track)

        @self.pc.on("track")
        def _on_track(track: MediaStreamTrack) -> None:
            if track.kind == "audio":
                self._tasks.append(asyncio.create_task(self._drain(track)))

        @self.pc.on("connectionstatechange")
        async def _on_state() -> None:
            if self.pc.connectionState in ("failed", "closed"):
                self.closed.set()

        @self.channel.on("open")
        def _on_open() -> None:
            self._tasks.append(asyncio.create_task(self._keepalive()))
            self._send_rtvi(
                "client-ready",
                {"version": rtvi.PROTOCOL_VERSION, "about": {"library": "soak-script"}},
            )

        @self.channel.on("message")
        def _on_message(message: Any) -> None:
            if isinstance(message, str):
                with contextlib.suppress(ValueError):
                    self._handle(json.loads(message))

        await self.pc.setLocalDescription(await self.pc.createOffer())
        gathered = asyncio.Event()

        @self.pc.on("icegatheringstatechange")
        def _on_gathering() -> None:
            if self.pc.iceGatheringState == "complete":
                gathered.set()

        if self.pc.iceGatheringState != "complete":
            await asyncio.wait_for(gathered.wait(), 30)
        offer = self.pc.localDescription
        body: dict[str, Any] = {"sdp": offer.sdp, "type": offer.type}
        if self.session_id:
            body["requestData"] = {"session_id": self.session_id}
        # 应用常用自签或 mkcert 的证书，这里只连用户指定的地址，不校验证书
        async with httpx.AsyncClient(verify=False, timeout=30) as http:
            response = await http.post(f"{self.base_url}/api/offer", json=body)
            response.raise_for_status()
            answer = response.json()
        await self.pc.setRemoteDescription(
            RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
        )

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self.pc.close()

    # ---- 收 ----

    async def _drain(self, track: MediaStreamTrack) -> None:
        """把助理的声音收下来（不收会在内存里越积越多），顺便记下第一声出现的时刻。"""
        with contextlib.suppress(Exception):
            while True:
                frame = await track.recv()
                if not self._first_audio.is_set():
                    samples = frame.to_ndarray()
                    if samples.size and int(np.abs(samples).max()) > 500:
                        self._first_audio.set()

    async def _keepalive(self) -> None:
        # 服务端靠这个判断连接还在不在（pipecat 的 SmallWebRTCConnection.is_connected）
        while self.channel.readyState == "open":
            self.channel.send(f"ping: {int(time.time() * 1000)}")
            await asyncio.sleep(1.0)

    def _send_rtvi(self, kind: str, data: dict[str, Any]) -> None:
        message = {"label": rtvi.MESSAGE_LABEL, "type": kind, "id": uuid.uuid4().hex[:8]}
        self.channel.send(json.dumps({**message, "data": data}, ensure_ascii=False))

    def _handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        data = message.get("data") or {}
        if kind == "bot-ready":
            self.ready.set()
        elif kind == "bot-llm-text":
            self._answer_text.append(str(data.get("text", "")))
            self._first_text.set()
        elif kind == "bot-stopped-speaking":
            self._answered.set()
        elif kind == "server-message":
            self._handle_app(data)
        elif kind == "error":
            self.stats.notices.append(f"error: {data}")

    def _handle_app(self, data: dict[str, Any]) -> None:
        kind = data.get("type")
        if kind == "session":
            self.session_id = data.get("id")
            self.base_secs = float(data.get("base_secs") or 0.0)
            self.log(f"会话 {self.session_id}（{'继续' if data.get('resumed') else '新建'}）")
        elif kind == "utterance" and data.get("source") == "asr":
            if data.get("id") is not None:
                self.stats.utterance_ids.add(int(data["id"]))
            self.stats.speakers.add(int(data.get("speaker_idx") or 0))
            lag = self.base_secs + self.track.sent_secs - float(data.get("t_end") or 0.0)
            self.stats.lags.append(lag)
            self.stats.all_lags.append(lag)
        elif kind == "utterance" and data.get("source") == "assistant":
            self.stats.assistant_lines += 1
        elif kind == "notice":
            text = f"{data.get('level')}: {data.get('text')}"
            self.stats.notices.append(text)
            self.log(f"提示 {text}")

    # ---- 发 ----

    def _reset_answer(self) -> None:
        self._first_text.clear()
        self._first_audio.clear()
        self._answered.clear()
        self._answer_text = []

    async def ask_spoken(self, text: str, frames: list[np.ndarray]) -> dict[str, Any]:
        """插播一句叫助理的话，量到第一个字、第一声的耗时。"""
        self._reset_answer()
        self.track.play_clip(frames)
        await self.track.clip_done.wait()
        asked = time.perf_counter()
        record: dict[str, Any] = {"kind": "spoken", "at": self.track.sent_secs, "question": text}
        try:
            await asyncio.wait_for(self._first_text.wait(), ANSWER_TIMEOUT_SECS)
            record["first_text_secs"] = round(time.perf_counter() - asked, 3)
            await asyncio.wait_for(self._first_audio.wait(), ANSWER_TIMEOUT_SECS)
            record["first_audio_secs"] = round(time.perf_counter() - asked, 3)
            await asyncio.wait_for(self._answered.wait(), ANSWER_TIMEOUT_SECS)
            await asyncio.sleep(AFTER_ANSWER_SECS)
        except TimeoutError:
            record["timeout"] = True
        record["answer"] = "".join(self._answer_text)
        self.track.resume()
        self.stats.spoken.append(record)
        return record

    async def ask_typed(self, text: str) -> dict[str, Any]:
        """发一条文字提问，量到第一个字的耗时。"""
        self._reset_answer()
        asked = time.perf_counter()
        self._send_rtvi("client-message", {"t": "text_input", "d": {"text": text}})
        record: dict[str, Any] = {"kind": "typed", "at": self.track.sent_secs, "question": text}
        try:
            await asyncio.wait_for(self._first_text.wait(), ANSWER_TIMEOUT_SECS)
            record["first_text_secs"] = round(time.perf_counter() - asked, 3)
            await asyncio.sleep(8.0)  # 等它把话说完（文字回答不朗读，没有「说完」的事件可等）
        except TimeoutError:
            record["timeout"] = True
        record["answer"] = "".join(self._answer_text)
        self.stats.typed.append(record)
        return record


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def default_url(cfg: AppConfig) -> str:
    scheme = "https" if cfg.server.tls_cert else "http"
    return f"{scheme}://localhost:{cfg.server.port}"


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * q))]


def summarize(stats: Stats, snapshots: list[dict[str, Any]]) -> list[str]:
    lines = [
        f"发言 {len(stats.utterance_ids)} 条，说话人 {len(stats.speakers - {0, -1, -2})} 位；"
        f"助理开口 {stats.assistant_lines} 次（提问 {len(stats.spoken) + len(stats.typed)} 次）"
    ]
    if stats.all_lags:
        lines.append(
            f"字幕落后（定稿时）：中位 {statistics.median(stats.all_lags):.2f} 秒，"
            f"95% {percentile(stats.all_lags, 0.95):.2f} 秒，最大 {max(stats.all_lags):.2f} 秒"
        )
    for label, records, key in (
        ("叫名字 → 第一个字", stats.spoken, "first_text_secs"),
        ("叫名字 → 第一声", stats.spoken, "first_audio_secs"),
        ("打字 → 第一个字", stats.typed, "first_text_secs"),
    ):
        values = [r[key] for r in records if key in r]
        missed = len(records) - len(values)
        if records:
            body = (
                f"中位 {statistics.median(values):.2f} 秒，最大 {max(values):.2f} 秒"
                if values
                else "没有成功的"
            )
            lines.append(f"{label}：{len(records)} 次，{body}，没应答 {missed} 次")
    if len(snapshots) >= 2:
        first, last = snapshots[0], snapshots[-1]
        if first.get("app_rss_mb") and last.get("app_rss_mb"):
            lines.append(f"应用进程内存：{first['app_rss_mb']} → {last['app_rss_mb']} MB")
        for name in first.get("services_rss_mb", {}):
            before, after = first["services_rss_mb"][name], last["services_rss_mb"].get(name)
            lines.append(f"{name} 进程内存合计：{before} → {after} MB")
        lines.append(f"显存（各卡，MB）：{first['gpu_used_mb']} → {last['gpu_used_mb']}")
    if stats.notices:
        lines.append(f"页面提示 {len(stats.notices)} 条，前几条：{stats.notices[:5]}")
    return lines


async def run(args: argparse.Namespace, out: Any) -> int:
    cfg = load_config(args.config)
    base_url = args.url or default_url(cfg)
    began = time.perf_counter()

    def log(text: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)

    def write(record: dict[str, Any]) -> None:
        record["elapsed_secs"] = round(time.perf_counter() - began, 1)
        out.write(json.dumps(record, ensure_ascii=False) + "\n")
        out.flush()

    name = cfg.session.assistant_name
    clips: list[tuple[str, list[np.ndarray]]] = []
    if args.wake_minutes > 0:
        if not cfg.tts.enabled:
            log("语音合成没有开，不做叫名字的测试")
        else:
            for template in args.ask_spoken or DEFAULT_SPOKEN:
                text = template.format(name=name)
                pcm = await asyncio.to_thread(synthesize, cfg, text)
                clips.append((text, to_track_frames(pcm, cfg.tts.sample_rate)))
            log(f"已合成 {len(clips)} 句叫助理的话")

    track = MeetingTrack(Path(args.audio), args.start_minutes * 60.0)
    client = SoakClient(base_url, track, args.session_id, log)
    await client.connect()
    try:
        await asyncio.wait_for(client.ready.wait(), 60)
    except TimeoutError:
        log("60 秒内没有等到服务端就绪")
        await client.close()
        return 1
    log(f"已连接 {base_url}，开始放录音")
    app_pid = args.server_pid or listening_pid(cfg.server.port)
    snapshots: list[dict[str, Any]] = []
    deadline = began + args.minutes * 60.0
    next_report = began
    next_wake = began + args.first_ask_minutes * 60.0
    next_typed = next_wake + args.typed_offset_minutes * 60.0
    asked = 0
    try:
        while time.perf_counter() < deadline and not client.closed.is_set():
            now = time.perf_counter()
            if now >= next_report:
                snapshot = await asyncio.to_thread(resource_snapshot, app_pid)
                lags = client.stats.lags
                snapshot.update(
                    kind="resources",
                    audio_secs=round(track.sent_secs, 1),
                    loops=track.loops,
                    utterances=len(client.stats.utterance_ids),
                    lag_median_secs=round(statistics.median(lags), 2) if lags else None,
                    lag_max_secs=round(max(lags), 2) if lags else None,
                )
                client.stats.lags = []
                snapshots.append(snapshot)
                write(snapshot)
                log(
                    f"音频 {track.sent_secs / 60:.1f} 分，发言 {snapshot['utterances']} 条，"
                    f"字幕落后中位 {snapshot['lag_median_secs']} 秒，应用内存 "
                    f"{snapshot['app_rss_mb']} MB，显存 {snapshot['gpu_used_mb']}"
                )
                next_report += args.report_minutes * 60.0
            if clips and args.wake_minutes > 0 and now >= next_wake:
                text, frames = clips[asked % len(clips)]
                record = await client.ask_spoken(text, frames)
                write(record)
                log(f"叫名字：{json.dumps(record, ensure_ascii=False)}")
                next_wake += args.wake_minutes * 60.0
                asked += 1
            if args.typed_minutes > 0 and now >= next_typed:
                questions = args.ask_typed or DEFAULT_TYPED
                text = questions[len(client.stats.typed) % len(questions)]
                record = await client.ask_typed(text)
                write(record)
                log(f"打字：{json.dumps(record, ensure_ascii=False)}")
                next_typed += args.typed_minutes * 60.0
            await asyncio.sleep(0.5)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log("收到中断，收尾")
    finally:
        await asyncio.sleep(3.0)  # 让最后几条发言定稿
        await client.close()
    if client.closed.is_set() and time.perf_counter() < deadline:
        log("连接中途断开了")
    snapshots.append(await asyncio.to_thread(resource_snapshot, app_pid))
    summary = summarize(client.stats, snapshots)
    write({"kind": "summary", "session_id": client.session_id, "lines": summary})
    print("\n===== 汇总 =====")
    for line in summary:
        print(line)
    print(f"会话编号：{client.session_id}；逐条记录在 {args.out}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="长会议压力测试（不经过浏览器）")
    parser.add_argument("--audio", required=True, help="要循环播放的录音（ffmpeg 能读的格式都行）")
    parser.add_argument("--minutes", type=float, default=120.0, help="跑多久，默认 120 分钟")
    parser.add_argument("--config", default=None, help="配置文件，默认 config/config.toml")
    parser.add_argument("--url", default=None, help="应用地址，默认按配置里的端口连 localhost")
    parser.add_argument("--session-id", default=None, help="继续这场会议，而不是新建")
    parser.add_argument("--start-minutes", type=float, default=0.0, help="从录音的第几分钟开始放")
    parser.add_argument("--report-minutes", type=float, default=10.0, help="每隔多久记一次资源")
    parser.add_argument(
        "--wake-minutes", type=float, default=10.0, help="每隔多久叫一次助理；0 = 不叫"
    )
    parser.add_argument(
        "--typed-minutes", type=float, default=10.0, help="每隔多久打字问一次；0 = 不问"
    )
    parser.add_argument("--first-ask-minutes", type=float, default=2.0, help="第一次提问在第几分钟")
    parser.add_argument(
        "--typed-offset-minutes", type=float, default=3.0, help="打字提问比叫名字晚几分钟"
    )
    parser.add_argument(
        "--ask-spoken",
        action="append",
        help="叫助理时说的话，可重复，轮流使用；要带 {name}（助理的名字）。不给用内置的几句",
    )
    parser.add_argument(
        "--ask-typed", action="append", help="打字问的话，可重复，轮流使用。不给用内置的几句"
    )
    parser.add_argument("--server-pid", type=int, default=None, help="应用的进程号，默认按端口查")
    parser.add_argument("--out", default="data/soak/soak.jsonl", help="逐条记录写到哪里")
    args = parser.parse_args()
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as out:
        sys.exit(asyncio.run(run(args, out)))


if __name__ == "__main__":
    main()
