"""独立验收：真实会话接管到指标接口，以及新增采集路径的日志隐私。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest
from loguru import logger
from test_health_api import client_for
from test_health_api import probes as probes
from test_meeting_recorder import audio, capture_recorder, interim, messages
from test_metrics_api import assert_metrics
from test_metrics_api import metrics_env as metrics_env
from test_web_sessions import FakeWorker

from agentic_meeting.pipeline.clock import SessionClock
from agentic_meeting.services import supervisor
from agentic_meeting.types import ASRDelta, Utterance
from agentic_meeting.web.app import create_app


async def test_sampling_error_logs_no_exception_or_caption_content(monkeypatch):
    records = []
    sink = logger.add(records.append, diagnose=True)
    rec, output = capture_recorder(monkeypatch)
    rec._on_audio(audio(1000))

    def broken_clock():
        raise RuntimeError("FAKE-06-SECRET-KEY")

    monkeypatch.setattr("agentic_meeting.pipeline.recorder.monotonic", broken_clock)
    try:
        await rec._handle_delta(
            interim("FAKE-06-TRANSCRIPT", ASRDelta("", "FAKE-06-TRANSCRIPT", 0.5))
        )
    finally:
        logger.remove(sink)
    assert len(messages(output, "caption")) == 1
    assert rec.caption_lag_seconds is rec.caption_sample_age_seconds is None
    assert "FAKE-06-" not in "".join(records)
    assert all(record.record["exception"] is None for record in records)


async def test_real_session_takeover_resets_http_caption_sample(
    make_cfg, tmp_path, monkeypatch, probes
):
    cfg = make_cfg()
    cfg.agent.enabled = False
    app = create_app(cfg, static_dir=tmp_path / "no-build")
    now = [100.0]
    monkeypatch.setattr("agentic_meeting.pipeline.recorder.monotonic", lambda: now[0])
    async with app.router.lifespan_context(app), client_for(app) as client:
        manager = app.state.resources.sessions
        monkeypatch.setattr(manager, "_now", lambda: now[0])
        first = await manager.begin()
        store = app.state.resources.store
        await store.rename_session(first.session.id, "FAKE-06-TITLE")
        await store.ensure_speaker(first.session.id, 1, "FAKE-06-SPEAKER")
        await store.add_utterance(Utterance(first.session.id, 1, 0, 1, "FAKE-06-TRANSCRIPT"))
        task = await store.create_task(first.session.id, goal="FAKE-06-GOAL")
        await store.update_task(task.id, artifacts=[{"path": "/FAKE-06-ARTIFACT.txt"}])
        old, _ = capture_recorder(monkeypatch, clock=SessionClock(first.base_secs))
        worker = FakeWorker()
        await manager.register(first, worker, old)
        old._on_audio(audio(2000))
        await old._handle_delta(interim("虚构字幕", ASRDelta("", "虚构字幕", 1.25)))
        body = assert_metrics(await client.get("/metrics"))
        assert body["live_connections"] == 1 and body["caption_lag_seconds"] == 0.75
        now[0] += 10
        takeover = asyncio.create_task(manager.attach(first.session.id))
        try:
            await asyncio.wait_for(worker.cancelled.wait(), 2)
            gap = assert_metrics(await client.get("/metrics"))
            assert gap["live_connections"] == 0 and gap["caption_lag_seconds"] is None
            await manager.finish(first)
            second = await asyncio.wait_for(takeover, 2)
            assert second.base_secs == 10
            new, output = capture_recorder(monkeypatch, clock=SessionClock(second.base_secs))
            await manager.register(second, FakeWorker(), new)
            fresh = assert_metrics(await client.get("/metrics"))
            assert fresh["live_connections"] == 1 and fresh["caption_lag_seconds"] is None
            new._on_audio(audio(2000))
            await new._handle_delta(interim("接续字幕", ASRDelta("", "接续字幕", 11.5)))
            sampled = assert_metrics(await client.get("/metrics"))
            assert sampled["caption_lag_seconds"] == 0.5
            assert sampled["caption_sample_age_seconds"] == 0
            assert len(messages(output, "caption")) == 1
            await manager.finish(second)
            final = assert_metrics(await client.get("/metrics"))
            assert final["live_connections"] == 0 and final["caption_lag_seconds"] is None
            for snapshot in (body, gap, fresh, sampled, final):
                assert "FAKE-06-" not in str(snapshot)
            for path in ("/healthz", "/readyz"):
                assert "FAKE-06-" not in (await client.get(path)).text
        finally:
            if manager.live is not None:
                await manager.finish(manager.live)
            await asyncio.gather(takeover, return_exceptions=True)


@pytest.mark.parametrize("failure", [False, True])
async def test_anonymous_probe_responses_and_logs_exclude_sensitive_values(
    metrics_env, monkeypatch, failure
):
    env = metrics_env
    sentinel = "FAKE-06-PRIVATE-KEY"
    monkeypatch.setenv("FAKE_06_PROBE_KEY", sentinel)
    env.app.state.health.targets = [
        replace(target, auth_env="FAKE_06_PROBE_KEY") for target in env.app.state.health.targets
    ]
    records = []
    sink = logger.add(records.append, diagnose=True)
    calls = []

    def respond(request):
        assert request.headers["authorization"] == f"Bearer {sentinel}"
        calls.append(request)
        if failure:
            raise httpx.ConnectError(f"{sentinel} /private/meeting https://internal.test")
        return httpx.Response(200, text=sentinel)

    monkeypatch.setattr(
        supervisor,
        "_new_client",
        lambda url: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    try:
        async with client_for(env.app) as client:
            for path in ("/healthz", "/readyz", "/metrics"):
                response = await client.get(path)
                assert "authorization" not in response.request.headers
                assert "cookie" not in response.request.headers
                assert response.status_code == (503 if failure and path == "/readyz" else 200)
                assert sentinel not in response.text and "internal.test" not in response.text
            assert assert_metrics(await client.get("/metrics"))["status"] == "ok"
    finally:
        logger.remove(sink)
    assert len(calls) == sum(target.enabled for target in env.app.state.health.targets)
    assert sentinel not in "".join(records)
