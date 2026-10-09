"""cli.py 的 ``services up`` / ``services status``。

用真实的本机端口和 ``python -m http.server`` 当假服务；不需要推理程序或权重。
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from agentic_meeting import cli
from agentic_meeting.config import EXAMPLE_CONFIG_PATH, AppConfig, load_config
from agentic_meeting.services.supervisor import ServiceSpec

CfgFactory = Callable[[], AppConfig]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def reachable(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return True
    except OSError:
        return False


def wait_unreachable(*ports: int, timeout: float = 10.0) -> None:
    """等这些端口都不再接受连接（进程结束后内核回收套接字有极短的延迟）。"""
    deadline = time.monotonic() + timeout
    while any(reachable(port) for port in ports):
        assert time.monotonic() < deadline, f"端口 {ports} 在进程结束后仍然可连：进程没有被结束？"
        time.sleep(0.2)


def args(config: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(config=config)


def use_config(monkeypatch: pytest.MonkeyPatch, cfg: AppConfig) -> None:
    monkeypatch.setattr(cli, "load_config", lambda path=None: cfg)


def http_server_argv(directory: Path, port: int) -> list[str]:
    return [
        sys.executable, "-m", "http.server", str(port),
        "--bind", "127.0.0.1", "--directory", str(directory),
    ]  # fmt: skip


def http_spec(directory: Path, port: int, name: str = "fake") -> ServiceSpec:
    return ServiceSpec(
        name=name,
        argv=http_server_argv(directory, port),
        env={},
        health_url=f"http://127.0.0.1:{port}/",
        log_path=directory / "logs" / f"{name}.log",
        startup_timeout_secs=30.0,
        managed=True,
    )


@pytest.fixture
def unreachable_cfg(make_cfg: CfgFactory) -> AppConfig:
    """四个服务都指向没人监听的端口。"""
    cfg = make_cfg()
    cfg.realtime_llm.llama_server.base_url = f"http://127.0.0.1:{free_port()}/v1"
    cfg.asr.base_url = f"http://127.0.0.1:{free_port()}"
    cfg.tts.base_url = f"http://127.0.0.1:{free_port()}/v1"
    cfg.embedding.base_url = f"http://127.0.0.1:{free_port()}/v1"
    return cfg


# --------------------------------------------------------------------------- #
# services status
# --------------------------------------------------------------------------- #


def test_status_when_nothing_is_reachable(unreachable_cfg, monkeypatch, capsys):
    use_config(monkeypatch, unreachable_cfg)
    began = time.monotonic()
    code = cli.cmd_services_status(args())
    out = capsys.readouterr().out

    assert code == 1
    for name in ("realtime", "asr", "tts", "embedding"):
        assert name in out
    assert out.count("[不通]") == 4  # 表里每个服务一行
    assert "[就绪]" not in out and "[连通]" not in out
    assert time.monotonic() - began < 10  # 并发探测，不是 4 次累加


def test_status_does_not_need_programs_or_filled_in_models(monkeypatch, capsys):
    """status 只需要地址：权重没填、程序没装也能运行。"""
    cfg = load_config(EXAMPLE_CONFIG_PATH)  # 模板里什么权重都没填
    cfg.realtime_llm.llama_server.base_url = f"http://127.0.0.1:{free_port()}/v1"
    cfg.asr.base_url = f"http://127.0.0.1:{free_port()}"
    cfg.tts.base_url = f"http://127.0.0.1:{free_port()}/v1"
    cfg.embedding.base_url = f"http://127.0.0.1:{free_port()}/v1"

    def not_installed(name: str, override: str = "") -> Path:
        raise FileNotFoundError(name)

    monkeypatch.setattr(cli, "find_executable", not_installed)
    use_config(monkeypatch, cfg)
    assert cli.cmd_services_status(args()) == 1
    assert "asr" in capsys.readouterr().out


def test_status_all_reachable_and_external_api(make_cfg, monkeypatch, capsys, tmp_path):
    """识别服务不受管 + 实时模型走通用接口：全部可达时退出码为 0，并给出对应的提示。"""
    (tmp_path / "health").write_text("ok")  # GET /health -> 200；GET /v1/models -> 404
    port = free_port()
    server = subprocess.Popen(http_server_argv(tmp_path, port))
    try:
        deadline = time.monotonic() + 20
        while not reachable(port):
            assert time.monotonic() < deadline, "假服务没有起来"
            time.sleep(0.2)

        cfg = make_cfg()
        cfg.tts.enabled = False
        cfg.embedding.enabled = False
        cfg.asr.launch.enabled = False
        cfg.asr.base_url = f"http://127.0.0.1:{port}"
        cfg.realtime_llm.mode = "openai_api"
        cfg.realtime_llm.openai_api.base_url = f"http://127.0.0.1:{port}/v1"
        cfg.realtime_llm.openai_api.model = "fake-model"
        cfg.realtime_llm.openai_api.api_key_env = ""
        use_config(monkeypatch, cfg)

        code = cli.cmd_services_status(args())
        out = capsys.readouterr().out
    finally:
        server.terminate()
        server.wait(timeout=10)

    assert code == 0
    assert "外部接口" in out and "连通" in out  # /models 返回 404 也算连通
    assert "--chat-template-file" in out  # 识别服务不受管时的提醒
    assert "不通" not in out


def test_status_reports_unreadable_config(tmp_path, capsys):
    code = cli.cmd_services_status(args(str(tmp_path / "nope.toml")))
    assert code == 2
    assert "nope.toml" in capsys.readouterr().out


def test_status_reports_invalid_service_address(make_cfg, monkeypatch, capsys):
    cfg = make_cfg()
    cfg.asr.base_url = "http://192.168.1.20:8081"  # 受管服务只能监听本机
    use_config(monkeypatch, cfg)
    assert cli.cmd_services_status(args()) == 2
    assert "launch.enabled = false" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# services up
# --------------------------------------------------------------------------- #


def test_up_refuses_to_start_with_missing_items(monkeypatch, capsys):
    cfg = load_config(EXAMPLE_CONFIG_PATH)  # 模板里有大量必填项没填

    class MustNotBeCreated:
        def __init__(self, *a, **kw):
            raise AssertionError("有缺项时不应该创建 Supervisor，更不该启动任何进程")

    monkeypatch.setattr(cli, "Supervisor", MustNotBeCreated)
    use_config(monkeypatch, cfg)
    code = cli.cmd_services_up(args())
    out = capsys.readouterr().out

    assert code == 1
    assert "asr.launch.model_path" in out
    assert "tts.voice" in out
    assert "未就绪" in out


def test_up_prints_data_egress_warnings_first(make_cfg, monkeypatch, capsys):
    cfg = make_cfg()
    cfg.realtime_llm.mode = "openai_api"
    cfg.realtime_llm.openai_api.base_url = "https://llm.example.com/v1"
    cfg.realtime_llm.openai_api.model = "fake-model"
    cfg.realtime_llm.openai_api.api_key_env = ""
    use_config(monkeypatch, cfg)
    monkeypatch.setattr(cli, "check_ready", lambda cfg: ["故意留一个缺项"])
    assert cli.cmd_services_up(args()) == 1
    out = capsys.readouterr().out
    assert out.index("llm.example.com") < out.index("故意留一个缺项")


async def test_up_failure_cleans_up_and_shows_log(make_cfg, monkeypatch, capsys, tmp_path):
    good_port = free_port()
    good = http_spec(tmp_path, good_port, name="good")
    bad = http_spec(tmp_path, free_port(), name="bad")
    bad.argv = [
        sys.executable,
        "-c",
        "print('boom: cannot load model', flush=True); raise SystemExit(7)",
    ]

    code = await cli._run_up(make_cfg(), [good, bad])
    out = capsys.readouterr().out

    assert code == 1
    assert "启动失败" in out and "boom: cannot load model" in out
    # 失败时先启动的 good 也要被结束，不留残留进程。
    await asyncio.to_thread(wait_unreachable, good_port)


async def test_up_stops_everything_when_cancelled_like_ctrl_c(make_cfg, capsys, tmp_path):
    cfg = make_cfg()
    cfg.tts.enabled = False  # 假服务没有音色接口，本测试不关心音色核对
    port_a, port_b = free_port(), free_port()
    specs = [http_spec(tmp_path, port_a, name="a"), http_spec(tmp_path, port_b, name="b")]

    task = asyncio.create_task(cli._run_up(cfg, specs))
    collected: list[str] = []

    def wait_until_ready() -> None:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            collected.append(capsys.readouterr().out)
            if "Ctrl+C" in "".join(collected):
                return
            time.sleep(0.2)
        raise AssertionError("up 没有打印就绪提示：\n" + "".join(collected))

    await asyncio.to_thread(wait_until_ready)
    assert not task.done()  # 就绪后一直挂着
    assert reachable(port_a) and reachable(port_b)
    out = "".join(collected)
    assert out.count("[就绪]") == 2

    task.cancel()  # asyncio.run 收到 Ctrl+C 时做的就是取消主任务
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.to_thread(wait_unreachable, port_a, port_b)


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #


def test_services_requires_a_subcommand(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["agentic-meeting", "services"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2


def test_main_wires_services_status(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        ["agentic-meeting", "--config", str(tmp_path / "nope.toml"), "services", "status"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "nope.toml" in capsys.readouterr().out
