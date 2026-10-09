"""cli.py 的 ``serve``：先核对配置，可选地拉起推理服务，再启动 HTTP 应用，退出时停掉自己拉起的服务。

Supervisor 和 uvicorn 服务器都换成假的，只验证编排顺序；真实的 uvicorn 另有一条本机回环上的测试。
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys

import httpx
import pytest

from agentic_meeting import cli
from agentic_meeting.config import EXAMPLE_CONFIG_PATH, load_config
from agentic_meeting.services.supervisor import ProbeResult, ServiceSpec, ServiceStartError
from agentic_meeting.web import app as web_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def args(*, with_services: bool = False, config: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(config=config, with_services=with_services)


class Recorder:
    def __init__(self):
        self.events: list[str] = []


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def fake_supervisor(recorder, monkeypatch):
    class FakeSupervisor:
        fail_start = False

        def __init__(self, specs, *, cfg=None, **kwargs):
            self.specs = specs
            recorder.events.append("supervisor:create")

        async def start(self):
            recorder.events.append("supervisor:start")
            if FakeSupervisor.fail_start:
                raise ServiceStartError("asr 启动失败：模型文件损坏")

        async def wait_healthy(self):
            recorder.events.append("supervisor:wait_healthy")

        async def probe_all(self):
            recorder.events.append("supervisor:probe_all")
            return {s.name: ProbeResult(True, "") for s in self.specs}

        def status(self):
            return [
                {"name": s.name, "state": "running", "pid": 1, "returncode": None}
                for s in self.specs
            ]

        async def stop(self):
            recorder.events.append("supervisor:stop")

    monkeypatch.setattr(cli, "Supervisor", FakeSupervisor)
    return FakeSupervisor


@pytest.fixture
def fake_server(recorder, monkeypatch):
    class FakeServer:
        block = False

        async def serve(self):
            recorder.events.append("server:serve")
            if FakeServer.block:
                await asyncio.Event().wait()

    monkeypatch.setattr(web_app, "make_server", lambda cfg, app=None: FakeServer())
    return FakeServer


@pytest.fixture
def ready_cfg(make_cfg, monkeypatch):
    cfg = make_cfg()
    cfg.tts.enabled = False  # 假 Supervisor 没有音色接口，本组测试不关心音色核对
    monkeypatch.setattr(cli, "load_config", lambda path=None: cfg)
    monkeypatch.setattr(cli, "check_ready", lambda cfg: [])
    fake = [ServiceSpec("asr", [], {}, "http://127.0.0.1:1/health", cfg.resolve("x"), 1.0, True)]
    monkeypatch.setattr(cli, "build_specs", lambda cfg, **kwargs: fake)
    return cfg


# --------------------------------------------------------------------------- #
# 启动前的核对
# --------------------------------------------------------------------------- #


def test_serve_refuses_to_start_with_missing_items(monkeypatch, capsys, recorder, fake_server):
    monkeypatch.setattr(cli, "load_config", lambda path=None: load_config(EXAMPLE_CONFIG_PATH))
    assert cli.cmd_serve(args()) == 1
    out = capsys.readouterr().out
    assert "asr.launch.model_path" in out and "未就绪" in out
    assert recorder.events == []  # 没有启动任何东西


def test_serve_reports_unreadable_config(tmp_path, capsys):
    assert cli.cmd_serve(args(config=str(tmp_path / "nope.toml"))) == 2
    assert "nope.toml" in capsys.readouterr().out


def test_serve_prints_the_data_egress_warning_before_the_missing_items(
    make_cfg, monkeypatch, capsys
):
    cfg = make_cfg()
    cfg.realtime_llm.mode = "openai_api"
    cfg.realtime_llm.openai_api.base_url = "https://llm.example.com/v1"
    cfg.realtime_llm.openai_api.model = "fake-model"
    cfg.realtime_llm.openai_api.api_key_env = ""
    monkeypatch.setattr(cli, "load_config", lambda path=None: cfg)
    monkeypatch.setattr(cli, "check_ready", lambda cfg: ["故意留一个缺项"])
    assert cli.cmd_serve(args()) == 1
    out = capsys.readouterr().out
    assert out.index("llm.example.com") < out.index("故意留一个缺项")


# --------------------------------------------------------------------------- #
# 编排顺序
# --------------------------------------------------------------------------- #


def test_serve_with_services_starts_them_first_and_stops_them_last(
    ready_cfg, recorder, fake_supervisor, fake_server, capsys
):
    assert cli.cmd_serve(args(with_services=True)) == 0
    assert recorder.events == [
        "supervisor:create",
        "supervisor:start",
        "supervisor:wait_healthy",
        "supervisor:probe_all",
        "server:serve",
        "supervisor:stop",
    ]
    assert f"http://localhost:{ready_cfg.server.port}" in capsys.readouterr().out


def test_serve_shows_https_when_certificates_are_configured(
    ready_cfg, fake_supervisor, fake_server, capsys
):
    ready_cfg.server.tls_cert, ready_cfg.server.tls_key = "cert.pem", "key.pem"
    cli.cmd_serve(args(with_services=True))
    assert "https://localhost" in capsys.readouterr().out


def test_serve_does_not_start_the_server_when_a_service_fails_to_start(
    ready_cfg, recorder, fake_supervisor, fake_server, capsys
):
    fake_supervisor.fail_start = True
    assert cli.cmd_serve(args(with_services=True)) == 1
    out = capsys.readouterr().out
    assert "启动失败" in out and "模型文件损坏" in out
    assert "server:serve" not in recorder.events
    assert recorder.events[-1] == "supervisor:stop"  # 已启动的那些要被停掉


async def test_serve_stops_the_services_when_cancelled_like_ctrl_c(
    ready_cfg, recorder, fake_supervisor, fake_server
):
    fake_server.block = True
    specs = cli.build_specs(ready_cfg)
    task = asyncio.create_task(cli._run_serve(ready_cfg, specs, with_services=True))
    for _ in range(100):
        if "server:serve" in recorder.events:
            break
        await asyncio.sleep(0.02)
    assert not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recorder.events[-1] == "supervisor:stop"


def test_serve_without_services_only_warns_about_unreachable_ones(
    make_cfg, monkeypatch, recorder, fake_server, capsys
):
    cfg = make_cfg()
    cfg.realtime_llm.llama_server.base_url = f"http://127.0.0.1:{free_port()}/v1"
    cfg.asr.base_url = f"http://127.0.0.1:{free_port()}"
    cfg.tts.base_url = f"http://127.0.0.1:{free_port()}/v1"
    cfg.embedding.base_url = f"http://127.0.0.1:{free_port()}/v1"
    monkeypatch.setattr(cli, "load_config", lambda path=None: cfg)
    monkeypatch.setattr(cli, "check_ready", lambda cfg: [])

    assert cli.cmd_serve(args(with_services=False)) == 0  # 不拦着启动
    out = capsys.readouterr().out
    for name in ("realtime", "asr", "tts", "embedding"):
        assert name in out
    assert "当前不通" in out and "--with-services" in out
    assert recorder.events == ["server:serve"]  # 没有创建 Supervisor 去启动任何进程


# --------------------------------------------------------------------------- #
# 参数解析、真实的 uvicorn
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("flag", [[], ["--with-services"]])
def test_main_wires_serve_and_its_flag(monkeypatch, flag):
    seen = []
    monkeypatch.setattr(cli, "cmd_serve", lambda a: seen.append(a.with_services) or 0)
    monkeypatch.setattr(sys, "argv", ["agentic-meeting", "serve", *flag])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert seen == [bool(flag)]


async def test_make_server_serves_the_app_over_a_real_socket(make_cfg, tmp_path):
    cfg = make_cfg()
    cfg.server.host, cfg.server.port = "127.0.0.1", free_port()
    server = web_app.make_server(cfg, web_app.create_app(cfg, static_dir=tmp_path / "nope"))
    server.config.log_level = "warning"
    task = asyncio.create_task(server.serve())
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            for _ in range(100):
                try:
                    response = await client.get(f"http://127.0.0.1:{cfg.server.port}/api/time")
                    break
                except httpx.TransportError:
                    await asyncio.sleep(0.05)
            else:
                pytest.fail("服务器没有起来")
        assert response.status_code == 200 and "server_time" in response.json()
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


def test_make_server_enables_https_when_certificates_are_configured(make_cfg, tmp_path):
    cfg = make_cfg()
    cfg.server.tls_cert, cfg.server.tls_key = "certs/cert.pem", "certs/key.pem"
    config = web_app.make_server(cfg, web_app.create_app(cfg, static_dir=tmp_path)).config
    assert config.ssl_certfile == str(cfg.resolve("certs/cert.pem"))
    assert config.ssl_keyfile == str(cfg.resolve("certs/key.pem"))


def test_make_server_is_plain_http_without_certificates(make_cfg, tmp_path):
    cfg = make_cfg()
    config = web_app.make_server(cfg, web_app.create_app(cfg, static_dir=tmp_path)).config
    assert config.ssl_certfile is None and config.ssl_keyfile is None
    assert (config.host, config.port) == (cfg.server.host, cfg.server.port)
