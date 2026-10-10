"""真实 HTTP/SQLite/WebRTC，只有推理输入由受控测试管线替代。"""

from __future__ import annotations

import asyncio
import socket
import tempfile
import time
import tomllib
from dataclasses import asdict
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from PIL import Image
from pipecat.frames.frames import Frame, InputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame
from pipecat.transports.base_transport import TransportParams
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
    return {"ended": ended.id, "interrupted": interrupted.id, "task": task.id, "frame": frame.id}


class AudioProbe(FrameProcessor):
    """计数真实接收的 PCM，证明麦克风轨道确实经过 WebRTC。"""

    def __init__(self, base_secs: float) -> None:
        super().__init__()
        self.elapsed_secs = base_secs
        self.audio_frames = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self.audio_frames += 1
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

    async def __call__(self, connection: Any, request: Any, resources: AppResources) -> None:
        manager = resources.sessions
        assert manager is not None
        wanted = request.get("session_id") if isinstance(request, dict) else None
        live = await manager.attach(wanted) if wanted else await manager.begin()
        self.segment = live.connection_id * 1000 + 1
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


def application(cfg: AppConfig, store: Store, seeds: dict[str, Any]) -> FastAPI:
    bot = ControlledBot()
    app = create_app(cfg, store=store, bot=bot)
    control = FastAPI()

    @control.get("/state")
    async def state() -> dict[str, Any]:
        live = app.state.resources.sessions.live
        return {
            **seeds,
            "live": live.session.id if live else None,
            "ready": bot.ready,
            "audio_frames": bot.probe.audio_frames if bot.probe else 0,
        }

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
