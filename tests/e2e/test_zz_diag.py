"""临时诊断（不合入）：Windows 上带 --with-services 时 Ctrl+Break 能否停下。"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .conftest import App, _wait_started, app_config, dump_toml
from .meeting_client import MeetingClient, WatchingDevice

DIAG = Path(__file__).parent / "diag"


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("meeting", [False, True])
@pytest.mark.parametrize("services", [False, True])
async def test_diag_stop(inference, tmp_path, services, meeting):
    inference.reset()
    port = _port()
    cfg = app_config(inference, tmp_path / "data", port)
    args = ["serve"]
    if services:
        model = tmp_path / "emb.gguf"
        model.write_bytes(b"x")
        emb = cfg["embedding"]
        emb["base_url"] = f"http://127.0.0.1:{_port()}/v1"
        launcher = DIAG / ("fake-llama.cmd" if sys.platform == "win32" else "fake-llama.sh")
        emb["launch"].update(enabled=True, executable=str(launcher), model_path=str(model))
        args.append("--with-services")
    (tmp_path / "config.toml").write_text(dump_toml(cfg), encoding="utf-8")
    log = tmp_path / "app.log"
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith("_PROXY")}
    env.update(PYTHONUTF8="1", FAKE_PY=sys.executable, PYTHONASYNCIODEBUG="0")
    with log.open("wb") as out:
        proc = subprocess.Popen(  # noqa: ASYNC220
            [
                sys.executable,
                "-X",
                "dev",
                "-m",
                "agentic_meeting.cli",
                "--config",
                str(tmp_path / "config.toml"),
                *args,
            ],
            stdout=out,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=tmp_path,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    app = App(f"http://127.0.0.1:{port}", tmp_path / "data", log, proc)
    await _wait_started(app)
    async with app.http() as http:
        client = phone = None
        if meeting:
            client = MeetingClient(http)
            session = await client.connect()
            phone = WatchingDevice(http, session["id"], user_agent="diag")
            await phone.open()
        started = time.monotonic()
        rc = await app.shutdown(timeout_secs=40)
        took = time.monotonic() - started
        if phone:
            await phone.close()
        if client:
            await client.close()
    print(f"==== services={services} meeting={meeting} rc={rc} took={took:.1f}s ====")
    print(app.log_text()[-6000:])
    raise AssertionError(f"diag services={services} meeting={meeting} rc={rc} took={took:.1f}s")
