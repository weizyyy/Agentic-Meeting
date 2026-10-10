"""命令行：check 核对配置缺项，services status 只看服务通不通。"""

from __future__ import annotations

import os
import subprocess
import sys

from .conftest import app_config, dump_toml


def run_cli(tmp_path, config: dict, *args: str, env: dict | None = None):
    path = tmp_path / "config.toml"
    path.write_text(dump_toml(config), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "agentic_meeting.cli", "--config", str(path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1", **(env or {})},
        cwd=tmp_path,
        timeout=120,
    )


def test_check_accepts_a_complete_configuration(tmp_path, inference):
    result = run_cli(tmp_path, app_config(inference, tmp_path / "data", 7860), "check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "配置就绪" in result.stdout


def test_check_lists_what_is_missing(tmp_path, inference):
    config = app_config(inference, tmp_path / "data", 7860)
    config["asr"]["launch"].update(enabled=True, model_path="models/missing-asr.gguf")
    config["server"].update(host="0.0.0.0", password_env="E2E_UNSET_PASSWORD")
    result = run_cli(tmp_path, config, "check", env={"E2E_UNSET_PASSWORD": ""})
    assert result.returncode == 1
    assert "asr.launch.model_path" in result.stdout
    assert "E2E_UNSET_PASSWORD" in result.stdout

    config["server"]["port"] = "not a port"
    broken = run_cli(tmp_path, config, "check")
    assert broken.returncode == 2 and "server.port" in broken.stdout


def test_services_status_probes_without_starting_anything(tmp_path, inference):
    inference.reset()
    inference.tts.down = True
    result = run_cli(tmp_path, app_config(inference, tmp_path / "data", 7860), "services", "status")
    lines = result.stdout.splitlines()
    assert any(inference.asr.origin.split("//")[1] in line and "连通" in line for line in lines)
    assert any(inference.tts.origin.split("//")[1] in line and "不通" in line for line in lines)
