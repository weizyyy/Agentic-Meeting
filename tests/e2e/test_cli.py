"""命令行：check 核对配置缺项，services status 只看服务通不通；还有标定声纹门限的脚本。"""

from __future__ import annotations

import os
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

from .conftest import app_config, dump_toml
from .meeting_client import SPEECH_WAV

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


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


def test_voice_features_stay_on_this_machine(tmp_path, inference):
    config = app_config(inference, tmp_path / "data", 7860)
    config["speaker_memory"]["backend"] = "sherpa_onnx"
    missing = run_cli(tmp_path, config, "check")
    assert missing.returncode == 1 and "speaker_memory.launch.model_path" in missing.stdout

    config["speaker_memory"]["base_url"] = "http://192.0.2.10:8084"
    remote = run_cli(tmp_path, config, "check")
    assert remote.returncode == 2 and "speaker_memory.base_url" in remote.stdout


def test_services_status_probes_without_starting_anything(tmp_path, inference):
    inference.reset()
    inference.tts.down = True
    result = run_cli(tmp_path, app_config(inference, tmp_path / "data", 7860), "services", "status")
    lines = result.stdout.splitlines()
    assert any(inference.asr.origin.split("//")[1] in line and "连通" in line for line in lines)
    assert any(inference.tts.origin.split("//")[1] in line and "不通" in line for line in lines)


def test_check_estimates_gpu_memory_from_weight_files(tmp_path, inference):
    config = app_config(inference, tmp_path / "data", 7860)
    models = tmp_path / "models"
    models.mkdir()

    def weights(name: str, mb: int) -> str:
        path = models / name
        path.write_bytes(b"\0" * mb * 1024**2)
        return str(path)

    config["asr"]["launch"].update(
        enabled=True,
        model_path=weights("fake-asr.gguf", 3),
        mmproj_path=weights("fake-asr-mmproj.gguf", 1),
    )
    config["diarization"].update(backend="nemo_ctypes", model_path=weights("fake-diar.gguf", 2))
    config["tts"]["launch"].update(enabled=True, model_path=weights("fake-tts.gguf", 5))
    # 嵌入默认 -ngl 0，留在 CPU 上，不计入。
    config["embedding"]["launch"].update(enabled=True, model_path=weights("fake-emb.gguf", 7))
    result = run_cli(tmp_path, config, "check")
    lines = result.stdout.splitlines()
    estimate = lines[lines.index("显存估算（只算模型权重文件，实际占用更高）：") :]

    def row(label: str) -> str:
        return next(line.split()[1] for line in estimate if line.split()[:1] == [label])

    assert row("语音识别") == "4"
    assert row("说话人区分") == "2"
    # 语音合成还缺 codec 文件：不计入合计，check 照常把缺项列出来。
    assert "权重文件未就绪" in next(line for line in estimate if "语音合成" in line)
    assert "tts.launch.codec_path" in result.stdout
    assert row("合计") == "6"
    assert not any("向量嵌入" in line for line in estimate)

    config["asr"]["launch"]["extra_args"] = ["--device", "none"]
    config["diarization"]["gpu"] = -1
    config["tts"]["enabled"] = False
    cpu_only = run_cli(tmp_path, config, "check")
    assert "显存估算：没有由本项目拉起、使用 GPU 的推理服务。" in cpu_only.stdout


def test_speaker_threshold_calibration_reports_scores(tmp_path, inference):
    """管理员拿几个人的录音和声纹服务，看同一人、不同人的分数和推荐门限。"""
    inference.reset()
    with wave.open(str(SPEECH_WAV)) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    half = len(pcm) // 2
    # 林晓两段，周明一段：周明没有档案，只用来看陌生人会不会被认错
    files = {"林晓/周会-1.wav": pcm, "林晓/周会-2.wav": pcm[:half], "周明/周会-1.wav": pcm[::-1]}
    for name, data in files.items():
        path = tmp_path / "voices" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(data.tobytes())
    config = app_config(inference, tmp_path / "data", 7860)
    config["speaker_memory"].update(
        backend="sherpa_onnx", base_url=inference.speaker.origin, launch={"enabled": False}
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(dump_toml(config), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "calibrate_speaker_threshold.py"),
            str(tmp_path / "voices"),
            "--config",
            str(config_path),
            "--lengths",
            "3,0",
            "--out",
            str(tmp_path / "result.json"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
        cwd=tmp_path,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 人、3 个文件" in result.stdout
    rows = [line for line in result.stdout.splitlines() if line.startswith("| 前 3 秒")]
    assert len(rows) == 1 and "| 整个文件 |" in result.stdout
    assert (tmp_path / "result.json").exists()
    # 送去算声纹的都是 1 到 6 秒的语音
    assert inference.speaker.seconds()
    assert all(1.0 <= s <= 6.0 + 0.01 for s in inference.speaker.seconds())

    inference.speaker.down = True
    down = subprocess.run(
        [sys.executable, str(SCRIPTS / "calibrate_speaker_threshold.py"), str(tmp_path / "voices")]
        + ["--config", str(config_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
        cwd=tmp_path,
        timeout=120,
    )
    assert down.returncode == 1 and "声纹服务不可用" in down.stderr
