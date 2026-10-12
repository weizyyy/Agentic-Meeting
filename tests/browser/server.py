"""真实 HTTP/SQLite/WebRTC，只有推理输入由受控测试管线替代。"""

from __future__ import annotations

import asyncio
import socket
import tempfile
import time
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from PIL import Image
from pipecat.frames.frames import Frame, InputAudioRawFrame, OutputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner
from starlette.routing import Mount

from agentic_meeting.agent.tasks import task_dir
from agentic_meeting.config import EXAMPLE_CONFIG_PATH, AppConfig
from agentic_meeting.pipeline.bot import AppResources, session_message
from agentic_meeting.store.db import Store
from agentic_meeting.types import Utterance
from agentic_meeting.web.app import create_app


def configuration(root: Path) -> AppConfig:
    """只读模板；不读取用户配置、环境文件或启动模型。"""
    data = tomllib.loads(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
    data["session"]["data_dir"] = str(root)
    data["session"]["members"] = ["林晓", "周远"]
    cfg = AppConfig.model_validate(data)
    cfg.agent.enabled = cfg.embedding.enabled = cfg.tts.enabled = False
    cfg.screen.caption = False
    cfg.server.ice_servers = []
    cfg.server.password_env = ""
    return cfg


async def seed(store: Store, root: Path) -> dict[str, Any]:
    """每个测试独占数据库，两个会议的内容刻意不同。"""
    now = time.time()
    ended = await store.create_session("星河项目复盘", now=now - 300)
    interrupted = await store.create_session("月面计划讨论", now=now - 60)
    for session, texts in (
        (ended, ["星河测试第一条发言", "星河测试第二条发言"]),
        (interrupted, ["月面计划保留的历史字幕"]),
    ):
        for idx, text in enumerate(texts, 1):
            await store.add_utterance(Utterance(session.id, idx, idx, idx + 1, text))
            await store.rename_speaker(session.id, idx, f"测试同学{idx}")
    await store.end_session(ended.id, now=now - 200)
    frame = await store.add_frame(
        ended.id, t=3, width=320, height=180, suffix=".png", caption_status="skipped"
    )
    path = root / frame.path
    path.parent.mkdir(parents=True)
    Image.new("RGB", (320, 180), "#234567").save(path)
    task = await store.create_task(ended.id, goal="整理星河测试结论", requested_t=4)
    await store.update_task(
        task.id,
        status="succeeded",
        brief="已整理虚构结论",
        detail_md="## 星河测试结果\n只包含虚构会议内容。",
        artifacts=["result.txt"],
        finished_at=now,
    )
    directory = task_dir(root, task)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "result.txt").write_text("星河测试产物\n", encoding="utf-8")
    # 再交办两个目标和结论都较长的任务，看板放不下时要能看出各区域怎么分高度。
    for goal, brief in (
        (
            "逐条核对星河测试里提到的四组虚构数据各自的采集日期和来源，"
            "每组用一两句话说明结论，最后汇总哪些数据需要重新采集，结果用中文回答。",
            "四组虚构数据里有三组是上周采集的，第四组更早，来源都已补全；"
            "建议重新采集第四组，其余可以直接用于下一轮星河测试。",
        ),
        (
            "比较星河测试前后两轮虚构结果的差异，列出变化最大的三项指标并说明可能的原因，"
            "结果用中文回答。",
            "变化最大的是虚构指标甲、乙、丙，主要原因是第二轮换了采集设备；"
            "其余指标的差异都在误差范围内，不需要额外处理。",
        ),
    ):
        extra = await store.create_task(ended.id, goal=goal, requested_t=5)
        await store.update_task(extra.id, status="succeeded", brief=brief, finished_at=now)
    return {"ended": ended.id, "interrupted": interrupted.id, "task": task.id, "frame": frame.id}


class AudioProbe(FrameProcessor):
    """计数真实接收的 PCM，证明麦克风轨道确实经过 WebRTC。"""

    def __init__(self, base_secs: float) -> None:
        super().__init__()
        self.elapsed_secs = base_secs
        self.audio_frames = 0
        self.non_silent_frames = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self.audio_frames += 1
            samples = np.frombuffer(frame.audio, dtype="<i2").astype(np.float64)
            if len(samples) and np.sqrt(np.mean(samples**2)) >= 300:
                self.non_silent_frames += 1
            self.elapsed_secs += len(frame.audio) / (2 * frame.num_channels * frame.sample_rate)
        await self.push_frame(frame, direction)

    def set_speaker_name(self, _idx: int, _name: str) -> None:
        """名字由真实 Store 和 SessionManager 管理。"""


class ControlledBot:
    """保留 Pipecat 管线、RTVI、连接生命周期；不建立 ASR/LLM/TTS。"""

    def __init__(self) -> None:
        self.ready = 0
        self.segment = 0
        self.probe: AudioProbe | None = None
        self.connection: SmallWebRTCConnection | None = None
        self.offers_enabled = True
        self.offer_sessions: list[str | None] = []

    async def __call__(
        self, connection: SmallWebRTCConnection, request: Any, resources: AppResources
    ) -> None:
        manager = resources.sessions
        assert manager is not None
        wanted = request.get("session_id") if isinstance(request, dict) else None
        live = await manager.attach(wanted) if wanted else await manager.begin()
        self.segment = live.connection_id * 1000 + 1
        self.connection = connection
        try:
            transport = SmallWebRTCTransport(
                webrtc_connection=connection,
                params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
            )
            self.probe = probe = AudioProbe(live.base_secs)
            worker = PipelineWorker(
                Pipeline([transport.input(), probe, transport.output()]),
                params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
                idle_timeout_secs=None,
            )

            @worker.rtvi.event_handler("on_client_ready")
            async def ready(_rtvi: Any) -> None:
                self.ready += 1
                await worker.queue_frame(
                    RTVIServerMessageFrame(
                        data=session_message(live.session, live.resumed, live.base_secs)
                    )
                )

            @transport.event_handler("on_client_disconnected")
            async def disconnected(_transport: Any, _connection: Any) -> None:
                await worker.cancel()

            await manager.register(live, worker, probe)
            runner = WorkerRunner(handle_sigint=False)
            await runner.add_workers(worker)
            await runner.run()
        finally:
            await manager.finish(live)
            if self.connection is connection:
                self.connection = None


def application(cfg: AppConfig, store: Store, seeds: dict[str, Any]) -> FastAPI:
    bot = ControlledBot()
    app = create_app(cfg, store=store, bot=bot)
    control = FastAPI()

    @app.middleware("http")
    async def offers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method == "POST" and request.url.path == "/api/offer":
            try:
                body = await request.json()
            except ValueError:
                return await call_next(request)
            data = body.get("requestData", {}) if isinstance(body, dict) else {}
            wanted = data.get("session_id") if isinstance(data, dict) else None
            # 对当前在用 peer 的重新协商（同一个 pc_id、不重建）不新建连接、不选会议，不记：
            # 服务端连上后会要求重新协商，页面这时可能还不知道自己的会议编号，记下来就成了一条「另开一场」的假记录。
            live_pc = bot.connection.pc_id if bot.connection else None
            renegotiation = (
                isinstance(body, dict)
                and live_pc is not None
                and body.get("pc_id") == live_pc
                and not body.get("restart_pc")
            )
            if not renegotiation:
                bot.offer_sessions.append(wanted if isinstance(wanted, str) else None)
            if not bot.offers_enabled:
                return JSONResponse({"detail": "测试协商暂不可用"}, status_code=503)
        return await call_next(request)

    @control.get("/state")
    async def state() -> dict[str, Any]:
        live = app.state.resources.sessions.live
        return {
            **seeds,
            "live": live.session.id if live else None,
            "ready": bot.ready,
            "audio_frames": bot.probe.audio_frames if bot.probe else 0,
            "non_silent_frames": bot.probe.non_silent_frames if bot.probe else 0,
            "peer_id": bot.connection.pc_id if bot.connection else None,
            "peer_state": bot.connection.pc.connectionState if bot.connection else None,
            "offer_sessions": bot.offer_sessions,
        }

    @control.post("/offers")
    async def set_offers(enabled: bool = Body(embed=True)) -> dict[str, bool]:
        bot.offers_enabled = enabled
        return {"enabled": enabled}

    @control.post("/disconnect")
    async def disconnect() -> dict[str, str]:
        connection = bot.connection
        live = app.state.resources.sessions.live
        if connection is None or live is None:
            raise HTTPException(409, "没有活动测试连接")
        # 关闭真实 peer，不发送 session_closed 或 peerLeft，模拟意外传输中断。
        await asyncio.wait_for(connection.pc.close(), timeout=5)
        await asyncio.wait_for(live.finished.wait(), timeout=15)
        return {"peer_id": connection.pc_id, "state": connection.pc.connectionState}

    @control.post("/tone")
    async def tone() -> dict[str, int]:
        live = app.state.resources.sessions.live
        if live is None or live.worker is None or bot.connection is None:
            raise HTTPException(409, "没有活动测试连接")
        t = np.arange(24000) / 24000
        pcm = (np.sin(2 * np.pi * 440 * t) * 1600).astype("<i2").tobytes()
        await live.worker.queue_frame(OutputAudioRawFrame(pcm, 24000, 1))
        return {"samples": 24000, "sample_rate": 24000}

    @control.post("/emit")
    async def emit(body: dict[str, Any]) -> dict[str, Any]:
        resources = app.state.resources
        live = resources.sessions.live
        if live is None:
            raise HTTPException(409, "没有活动测试连接")
        text = str(body.get("text", "受控实时字幕"))
        t = max(live.base_secs, bot.probe.elapsed_secs)
        segment = bot.segment
        message = {
            "segment_id": segment,
            "speaker_idx": 1,
            "speaker_name": await store.speaker_name(live.session.id, 1),
            "t_start": t,
        }
        if not body.get("final", True):
            await resources.sessions.push(
                {"type": "caption", **message, "stable": text, "unstable": ""}
            )
            return {"segment_id": segment}
        bot.segment += 1
        utterance = Utterance(live.session.id, 1, t, t + 0.1, text)
        await store.add_utterance(utterance)
        await resources.sessions.push(
            {
                "type": "utterance",
                **message,
                "id": utterance.id,
                "t_end": t + 0.1,
                "text": text,
                "source": "asr",
            }
        )
        return asdict(utterance)

    # 放在生产静态站点之前；生产 create_app 从未挂载这些路由。
    app.router.routes.insert(0, Mount("/__test", app=control))
    return app


class ReadyServer(uvicorn.Server):
    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        if self.started:
            assert sockets
            print(f"E2E_READY http://127.0.0.1:{sockets[0].getsockname()[1]}", flush=True)


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="agentic-meeting-e2e-") as temp:
        root = Path(temp)
        cfg = configuration(root)
        store = await Store.open(root / "meetings.db", cfg.embedding.dimensions)
        try:
            seeds = await seed(store, root)
            app = application(cfg, store, seeds)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                server = ReadyServer(
                    uvicorn.Config(app, log_level="info", timeout_graceful_shutdown=10)
                )
                await server.serve(sockets=[sock])
        finally:
            await store.close()


if __name__ == "__main__":
    asyncio.run(main())
