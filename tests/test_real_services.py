"""接真实的推理服务和模型权重的检查，默认不跑：``uv run pytest -m gpu``。

用的是本机的 ``config/config.toml``。说话人区分需要用环境变量 ``AGENTIC_MEETING_TEST_WAV`` 指定一段多人对话的录音。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from agentic_meeting.agent.runner import FALLBACK_BRIEF, AgentRunner
from agentic_meeting.diar.nemo_ctypes import NemoDiarizer
from agentic_meeting.store.db import Store

# --------------------------------------------------------------------------- #
# 真实模型（pytest -m gpu 运行；用环境变量 AGENTIC_MEETING_TEST_WAV 指定一段多人对话的录音）
# --------------------------------------------------------------------------- #

MEETING_WAV = Path(os.environ.get("AGENTIC_MEETING_TEST_WAV", ""))
HAS_MEETING_WAV = MEETING_WAV.is_file()


@pytest.mark.gpu
async def test_real_model_finds_at_least_two_speakers(tmp_path):
    from agentic_meeting.audio.diagnose import load_pcm16k
    from agentic_meeting.config import load_config

    cfg = load_config()
    if cfg.diarization.backend == "none" or not cfg.diarization.model_path:
        pytest.skip("config.toml 里没有配置说话人区分模型")
    if not HAS_MEETING_WAV:
        pytest.skip("没有用 AGENTIC_MEETING_TEST_WAV 指定录音")
    audio = load_pcm16k(MEETING_WAV)
    d = NemoDiarizer(cfg)
    await d.start()
    try:
        step = 16000 * 2 // 3  # 约 0.3 秒一块
        for i in range(0, len(audio), step):
            await d.push_audio(audio[i : i + step])
        await d.finish()
        segments = await d.segments()
    finally:
        await d.close()
    assert len({s.speaker for s in segments}) >= 2


# --------------------------------------------------------------------------- #
# 真实的远端模型、MCP 和沙箱（需要外部服务，默认不跑：uv run pytest -m gpu tests/test_agent_runner.py）
# --------------------------------------------------------------------------- #


async def _real_run(goal: str, tmp_path, *, sandbox_kind: str | None = None):
    from agentic_meeting.config import load_config
    from agentic_meeting.pipeline.prompts import load_prompt

    cfg = load_config()
    if not (cfg.agent.enabled and cfg.agent.base_url and cfg.agent.model):
        pytest.skip("config.toml 的 [agent] 还没有配置")
    cfg.session.data_dir = str(tmp_path / "data")
    if sandbox_kind is not None:
        cfg.agent.sandbox.kind = sandbox_kind
    store = await Store.open(tmp_path / "meetings.db", cfg.embedding.dimensions)
    events: list[tuple[str, str]] = []

    async def on_event(kind, summary, payload=None):
        events.append((kind, summary))
        print(f"  [{kind}] {summary}")

    try:
        session = await store.create_session()
        task = await store.create_task(session.id, goal=goal)
        runner = AgentRunner(cfg, store, system_prompt=load_prompt("agent_system"))
        result = await asyncio.wait_for(runner(task, on_event), cfg.agent.task_timeout_secs)
    finally:
        await store.close()
    print(f"\nbrief: {result.brief}\nsources: {result.sources}\nartifacts: {result.artifacts}")
    print(result.detail_md[:800])
    return cfg, result, events


@pytest.mark.gpu
async def test_real_remote_model_answers_a_simple_task(tmp_path):
    """远端模型能按要求的 JSON 格式交卷（不带沙箱；配置了 MCP 的话可能会去检索）。"""
    _cfg, result, _events = await _real_run(
        "用一句话说明什么是对比学习里的温度系数，以及调大它一般有什么影响。",
        tmp_path,
        sandbox_kind="none",
    )
    assert result.brief and result.brief != FALLBACK_BRIEF  # 是按格式回答的，不是兜底


@pytest.mark.gpu
async def test_real_sandbox_runs_code_and_returns_an_artifact(tmp_path):
    """按配置的沙箱跑一段代码、画一张图，产物能取回任务目录。"""
    cfg, result, events = await _real_run(
        "用 Python 画出 y = x 的平方在 x 从 -3 到 3 之间的曲线，保存成 plot.png。", tmp_path
    )
    if cfg.agent.sandbox.kind == "none":
        pytest.skip("config.toml 里 agent.sandbox.kind = none")
    assert any(kind == "tool_call" and "代码" in summary for kind, summary in events)
    assert "plot.png" in result.artifacts
    workdir = Path(cfg.resolve(cfg.session.data_dir)) / "sessions"
    assert list(workdir.rglob("plot.png"))
