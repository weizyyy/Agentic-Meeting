"""端到端测试的夹具。

每个测试起一个真实的应用进程（``agentic-meeting --config <临时配置> serve``，和用户启动的方式相同），
数据目录在临时目录里；推理服务换成 ``inference.py`` 的假服务。测试只通过 HTTP 和 WebRTC 与应用交互。
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
import tomllib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentic_meeting.config import EXAMPLE_CONFIG_PATH

from .inference import EMBEDDING_DIMENSIONS, Inference
from .meeting_client import SPEECH_WAV, MeetingClient

STARTUP_TIMEOUT_SECS = 60.0

if not SPEECH_WAV.is_file():
    pytest.skip(
        "缺少语音样本，先运行 git submodule update --init --depth 1 third_party/Confucius4-R2T2",
        allow_module_level=True,
    )


@pytest.fixture(scope="session")
def inference() -> Iterator[Inference]:
    fakes = Inference()
    fakes.start()
    try:
        yield fakes
    finally:
        fakes.stop()


@dataclass
class App:
    """一个运行中的应用进程。"""

    base_url: str
    data_dir: Path
    log: Path
    process: subprocess.Popen

    def http(self, **kwargs: Any) -> httpx.AsyncClient:
        # 不读环境里的代理设置：应用在本机
        return httpx.AsyncClient(base_url=self.base_url, trust_env=False, timeout=30, **kwargs)

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")

    def stop(self, timeout: float = 30) -> int:
        """像用户按 Ctrl+C 一样停下（Windows 上没有对子进程发 Ctrl+C 的简单办法，直接结束进程）。"""
        if self.process.poll() is None:
            if sys.platform == "win32":
                self.process.terminate()
            else:
                self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        return self.process.returncode

    async def shutdown(self, timeout_secs: float = 30) -> int:
        """``stop`` 放到线程里做：等进程退出的同时，测试这边的连接还能收消息。"""
        return await asyncio.to_thread(self.stop, timeout_secs)


StartApp = Callable[..., Awaitable[App]]


@pytest.fixture
async def start_app(inference: Inference, tmp_path: Path) -> AsyncIterator[StartApp]:
    """``await start_app(**改动)`` 起一个应用进程；改动是按点分路径覆盖配置的键，例如
    ``{"server.password_env": "E2E_PASSWORD"}``。测试结束时停掉全部进程。"""
    inference.reset()
    apps: list[App] = []
    count = 0

    async def start(
        overrides: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        data_dir: Path | None = None,
    ) -> App:
        nonlocal count
        count += 1
        root = tmp_path / f"app{count}"
        root.mkdir()
        data = data_dir or tmp_path / "data"
        port = _free_port()
        config = app_config(inference, data, port)
        for key, value in (overrides or {}).items():
            _set(config, key, value)
        config_path = root / "config.toml"
        config_path.write_text(dump_toml(config), encoding="utf-8")
        log = root / "app.log"
        process_env = {
            **{k: v for k, v in os.environ.items() if not k.upper().endswith("_PROXY")},
            "PYTHONUTF8": "1",
            **(env or {}),
        }
        with log.open("wb") as out:
            process = await asyncio.to_thread(
                subprocess.Popen,
                [
                    sys.executable,
                    "-m",
                    "agentic_meeting.cli",
                    "--config",
                    str(config_path),
                    "serve",
                ],
                stdout=out,
                stderr=subprocess.STDOUT,
                env=process_env,
                cwd=root,
            )
        app = App(f"http://127.0.0.1:{port}", data, log, process)
        apps.append(app)
        await _wait_started(app)
        return app

    try:
        yield start
    finally:
        for app in apps:
            app.stop()
            # pytest 只在测试失败时显示捕获的输出：失败时能看到应用的日志
            print(f"---- 应用日志 {app.log} ----\n{app.log_text()[-20000:]}")


@pytest.fixture
async def app(start_app: StartApp) -> App:
    return await start_app()


@pytest.fixture
async def http(app: App) -> AsyncIterator[httpx.AsyncClient]:
    async with app.http() as client:
        yield client


class Meetings:
    """``meetings()`` 造一个会议页面替身；``close_all()`` 全部断开。"""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self.clients: list[MeetingClient] = []

    def __call__(self) -> MeetingClient:
        client = MeetingClient(self._http)
        self.clients.append(client)
        return client

    async def close_all(self) -> None:
        for client in self.clients:
            await client.close()
        self.clients.clear()


@pytest.fixture
async def meeting_factory() -> AsyncIterator[Callable[[httpx.AsyncClient], Meetings]]:
    made: list[Meetings] = []

    def make(http: httpx.AsyncClient) -> Meetings:
        made.append(Meetings(http))
        return made[-1]

    try:
        yield make
    finally:
        for meetings in made:
            await meetings.close_all()


@pytest.fixture
async def meeting(
    http: httpx.AsyncClient, meeting_factory: Callable[[httpx.AsyncClient], Meetings]
) -> Meetings:
    return meeting_factory(http)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


def app_config(inference: Inference, data_dir: Path, port: int) -> dict[str, Any]:
    """以配置模板为底：推理服务都指向假服务、都不由应用启动；不需要任何密钥和权重文件。"""
    with EXAMPLE_CONFIG_PATH.open("rb") as f:
        cfg = tomllib.load(f)
    cfg["session"]["data_dir"] = str(data_dir)
    cfg["server"].update(host="127.0.0.1", port=port)
    llm = cfg["realtime_llm"]["llama_server"]
    llm.update(base_url=f"{inference.llm.origin}/v1", model="fake-model")
    llm["launch"]["enabled"] = False
    cfg["asr"]["base_url"] = inference.asr.origin
    cfg["asr"]["launch"]["enabled"] = False
    cfg["diarization"]["backend"] = "none"
    cfg["tts"].update(base_url=f"{inference.tts.origin}/v1", voice="fake-voice")
    cfg["tts"]["launch"]["enabled"] = False
    cfg["embedding"].update(
        base_url=f"{inference.embedding.origin}/v1", dimensions=EMBEDDING_DIMENSIONS
    )
    cfg["embedding"]["launch"]["enabled"] = False
    cfg["agent"].update(
        base_url=f"{inference.agent.origin}/v1",
        model="fake-agent",
        api_key_env="",
        mcp_servers=[],
    )
    cfg["agent"]["sandbox"]["kind"] = "none"
    return cfg


def _set(cfg: dict[str, Any], dotted: str, value: Any) -> None:
    *path, last = dotted.split(".")
    node = cfg
    for part in path:
        node = node[part]
    node[last] = value


def dump_toml(data: dict[str, Any]) -> str:
    """够用的 TOML 写出：表写成 [段]，空表和标量列表写成行内。"""
    lines: list[str] = []

    def value(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int | float):
            return repr(v)
        if isinstance(v, str):
            return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
        if isinstance(v, list):
            return "[" + ", ".join(value(x) for x in v) + "]"
        if isinstance(v, dict):
            return "{" + ", ".join(f"{k} = {value(x)}" for k, x in v.items()) + "}"
        raise TypeError(type(v))

    def section(prefix: str, table: dict[str, Any]) -> None:
        if prefix:
            lines.append(f"[{prefix}]")
        nested = {k: v for k, v in table.items() if isinstance(v, dict) and v}
        for k, v in table.items():
            if k not in nested:
                lines.append(f"{k} = {value(v)}")
        lines.append("")
        for k, v in nested.items():
            section(f"{prefix}.{k}" if prefix else k, v)

    section("", data)
    return "\n".join(lines)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_started(app: App) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECS
    async with app.http() as client:
        while True:
            if app.process.poll() is not None:
                raise AssertionError(f"应用启动失败：\n{app.log_text()[-5000:]}")
            try:
                if (await client.get("/healthz")).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"应用 {STARTUP_TIMEOUT_SECS} 秒内没有启动：\n{app.log_text()[-5000:]}"
                )
            await asyncio.sleep(0.1)
