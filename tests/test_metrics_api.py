"""指标 HTTP 接线、共享缓存、故障与停止竞态；全部使用本地临时数据和 fake。"""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace

import pytest
from test_health_api import FakeStore, assert_ready, client_for
from test_health_api import probes as probes

from agentic_meeting.pipeline.bot import AppResources
from agentic_meeting.pipeline.recorder import MeetingRecorder
from agentic_meeting.pipeline.session import SessionManager
from agentic_meeting.store.db import Store
from agentic_meeting.web import health_api
from agentic_meeting.web.app import create_app

SENTINEL = "private-metrics-sentinel"


class CountsStore(FakeStore):
    def __init__(self):
        super().__init__()
        self.counts = dict.fromkeys(health_api.TASK_STATUSES, 0)
        self.count_calls = 0
        self.count_error = False
        self.count_gate = asyncio.Event()
        self.count_gate.set()
        self.count_entered = asyncio.Event()
        self.count_cancelled = asyncio.Event()

    async def task_counts(self):
        self.count_calls += 1
        result = dict(self.counts)
        self.count_entered.set()
        try:
            await self.count_gate.wait()
        except asyncio.CancelledError:
            self.count_cancelled.set()
            raise
        if self.count_error:
            raise ValueError(SENTINEL)
        return result


@pytest.fixture
async def metrics_env(make_cfg, tmp_path):
    cfg = make_cfg()
    cfg.agent.enabled = False
    cfg.realtime.digest_provider = cfg.report.provider = "realtime_llm"
    app = create_app(cfg, static_dir=tmp_path / "no-build")
    store = CountsStore()
    resources = AppResources(
        cfg,
        store=store,
        sessions=SimpleNamespace(live=None),
        captions=SimpleNamespace(enabled=False, pending_count=0),
    )
    app.state.resources = resources
    app.state.health.start(store, screen_caption_enabled=False)
    now = [100.0]
    app.state.health.clock = lambda: now[0]
    yield SimpleNamespace(app=app, store=store, resources=resources, now=now, cfg=cfg)
    await app.state.health.close()


def assert_metrics(response):
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert set(body) == {
        "status",
        "lifecycle",
        "live_connections",
        "caption_lag_seconds",
        "caption_sample_age_seconds",
        "queues",
        "task_counts",
        "task_counts_age_seconds",
        "services",
        "service_snapshot_age_seconds",
    }
    assert body["status"] in {"ok", "partial"}
    assert_ready(
        {
            "status": "ok",
            "lifecycle": body["lifecycle"],
            "storage": {"status": "ok", "reason": "checked"},
            "services": body["services"],
            "service_snapshot_age_seconds": body["service_snapshot_age_seconds"],
        }
    )
    assert body["live_connections"] in (None, 0, 1)
    for name in ("caption_lag_seconds", "caption_sample_age_seconds", "task_counts_age_seconds"):
        value = body[name]
        assert value is None or (
            type(value) in (int, float) and math.isfinite(value) and value >= 0
        )
    assert set(body["queues"]) == {"transcript_retry", "screen_caption", "agent_tasks"}
    for name in ("transcript_retry", "agent_tasks"):
        value = body["queues"][name]
        assert value is None or (type(value) is int and value >= 0)
    screen = body["queues"]["screen_caption"]
    assert set(screen) == {"enabled", "depth"} and type(screen["enabled"]) is bool
    assert screen["depth"] in (None, 0, 1)
    if not screen["enabled"]:
        assert screen["depth"] == 0
    counts = body["task_counts"]
    if counts is None:
        assert body["queues"]["agent_tasks"] is None and body["task_counts_age_seconds"] is None
    else:
        assert set(counts) == set(health_api.TASK_STATUSES)
        assert all(type(value) is int and value >= 0 for value in counts.values())
        assert body["queues"]["agent_tasks"] == counts["queued"]
        assert body["task_counts_age_seconds"] is not None
    assert SENTINEL not in response.text
    return body


def live_with(recorder):
    return SimpleNamespace(
        worker=object(),
        recorder=recorder,
        stop_requested=False,
        done=asyncio.Event(),
        session_id=SENTINEL,
        connection_id=SENTINEL,
    )


async def test_all_five_metric_groups_with_nonzero_values_and_safe_whitelist(metrics_env, probes):
    env = metrics_env
    env.store.counts = dict(zip(health_api.TASK_STATUSES, range(1, 6), strict=True))
    env.store.counts[SENTINEL] = 999
    env.resources.sessions.live = live_with(
        SimpleNamespace(
            caption_lag_seconds=0.75,
            caption_sample_age_seconds=2.5,
            unsaved_utterance_count=3,
            transcript=SENTINEL,
            model=SENTINEL,
            api_key=SENTINEL,
        )
    )
    env.resources.captions = SimpleNamespace(enabled=True, pending_count=1, prompt=SENTINEL)
    probes.statuses["realtime"] = "reachable", "http_response"
    async with client_for(env.app) as client:
        body = assert_metrics(await client.get("/metrics?url=https://private-metrics-sentinel/"))
    assert body["status"] == "ok" and body["live_connections"] == 1
    assert (body["caption_lag_seconds"], body["caption_sample_age_seconds"]) == (0.75, 2.5)
    assert body["queues"] == {
        "transcript_retry": 3,
        "screen_caption": {"enabled": True, "depth": 1},
        "agent_tasks": 1,
    }
    assert body["task_counts"] == dict(zip(health_api.TASK_STATUSES, range(1, 6), strict=True))
    assert body["task_counts_age_seconds"] == body["service_snapshot_age_seconds"] == 0
    assert env.store.count_calls == 1 and env.store.calls == 0


@pytest.mark.parametrize(
    "stage", ["none", "no_worker", "no_recorder", "takeover", "done", "registered"]
)
async def test_live_registration_and_takeover_do_not_use_inactive_caption_samples(
    metrics_env, probes, stage
):
    env = metrics_env
    live = live_with(
        SimpleNamespace(
            caption_lag_seconds=1, caption_sample_age_seconds=2, unsaved_utterance_count=3
        )
    )
    if stage == "no_worker":
        live.worker = None
    elif stage == "no_recorder":
        live.recorder = None
    elif stage == "takeover":
        live.stop_requested = True
    elif stage == "done":
        live.done.set()
    env.resources.sessions.live = None if stage == "none" else live
    async with client_for(env.app) as client:
        body = assert_metrics(await client.get("/metrics"))
    active = stage == "registered"
    assert body["status"] == ("partial" if stage == "no_recorder" else "ok")
    assert body["live_connections"] == int(active)
    assert body["caption_lag_seconds"] == (1 if active else None)
    assert body["caption_sample_age_seconds"] == (2 if active else None)
    assert body["queues"]["transcript_retry"] == (
        None if stage == "no_recorder" else 3 if active else 0
    )


async def test_new_recorder_without_caption_is_normal_and_disconnect_clears_sample(
    metrics_env, probes
):
    env = metrics_env
    env.resources.sessions.live = live_with(MeetingRecorder())
    async with client_for(env.app) as client:
        body = assert_metrics(await client.get("/metrics"))
        assert body["status"] == "ok" and body["live_connections"] == 1
        assert body["caption_lag_seconds"] is body["caption_sample_age_seconds"] is None
        env.resources.sessions.live = live_with(
            SimpleNamespace(
                caption_lag_seconds=0.4, caption_sample_age_seconds=5, unsaved_utterance_count=0
            )
        )
        assert assert_metrics(await client.get("/metrics"))["caption_lag_seconds"] == 0.4
        env.resources.sessions.live = None
        assert assert_metrics(await client.get("/metrics"))["caption_lag_seconds"] is None


@pytest.mark.parametrize("built", [True, False])
@pytest.mark.parametrize("caption_enabled", [True, False])
async def test_prestartup_and_stopping_metrics_are_safe_without_any_io(
    make_cfg, tmp_path, probes, built, caption_enabled
):
    cfg = make_cfg()
    cfg.screen.enabled = caption_enabled
    if built:
        (tmp_path / "index.html").write_text("client", encoding="utf-8")
    app = create_app(cfg, static_dir=tmp_path)
    async with client_for(app) as client:
        for lifecycle in ("starting", "stopping"):
            if lifecycle == "stopping":
                await app.state.health.close()
            body = assert_metrics(await client.get("/metrics"))
            assert body["status"] == "partial" and body["lifecycle"] == lifecycle
            assert body["live_connections"] is body["queues"]["transcript_retry"] is None
            assert body["task_counts"] is None
            assert body["queues"]["screen_caption"] == {
                "enabled": caption_enabled,
                "depth": None if caption_enabled else 0,
            }
    assert not probes.calls


async def test_lifespan_uses_actual_caption_worker_and_counts_without_agent(
    make_cfg, tmp_path, probes
):
    cfg = make_cfg()
    cfg.agent.enabled = False
    store = await Store.open(tmp_path / "historical.db", 4)
    try:
        historical = await store.create_session(SENTINEL)
        for status in health_api.TASK_STATUSES:
            task = await store.create_task(historical.id, goal=SENTINEL)
            await store.update_task(task.id, status=status, detail_md=SENTINEL, error=SENTINEL)
        await store.end_session(historical.id)
        current = await store.create_session(SENTINEL)
        task = await store.create_task(current.id, goal=SENTINEL)
        await store.update_task(task.id, status="succeeded")
        app = create_app(cfg, store=store, static_dir=tmp_path / "no-build")
        async with app.router.lifespan_context(app), client_for(app) as client:
            body = assert_metrics(await client.get("/metrics"))
            assert body["status"] == "ok" and body["live_connections"] == 0
            assert body["queues"]["screen_caption"] == {"enabled": False, "depth": 0}
            # agent 关闭也恢复旧未完成行；gauges 仍来自全库真实历史状态。
            assert body["task_counts"] == {
                "queued": 0,
                "running": 0,
                "succeeded": 2,
                "failed": 3,
                "cancelled": 1,
            }
            assert body["services"]["agent"]["status"] == "disabled"
            assert isinstance(app.state.resources.sessions, SessionManager)
            await store.delete_session(historical.id)
            now = app.state.health.clock() + 5
            app.state.health.clock = lambda: now
            body = assert_metrics(await client.get("/metrics"))
            assert body["task_counts"] == {
                "queued": 0,
                "running": 0,
                "succeeded": 1,
                "failed": 0,
                "cancelled": 0,
            }
        async with client_for(app) as client:
            body = assert_metrics(await client.get("/metrics"))
            assert body["status"] == "partial" and body["lifecycle"] == "stopping"
            assert body["queues"]["screen_caption"] == {"enabled": False, "depth": 0}
    finally:
        await store.close()


async def test_counts_cache_ttl_failure_and_recovery(metrics_env, probes):
    env = metrics_env
    async with client_for(env.app) as client:
        assert assert_metrics(await client.get("/metrics"))["status"] == "ok"
        env.now[0] += 4
        env.store.counts["queued"] = 7
        body = assert_metrics(await client.get("/metrics"))
        assert body["task_counts_age_seconds"] == 4 and body["queues"]["agent_tasks"] == 0
        assert env.store.count_calls == 1
        env.now[0] += 1
        body = assert_metrics(await client.get("/metrics"))
        assert body["queues"]["agent_tasks"] == 7 and body["task_counts_age_seconds"] == 0
        env.now[0] += 5
        env.store.count_error = True
        body = assert_metrics(await client.get("/metrics"))
        assert body["status"] == "partial" and body["task_counts"] is None
        env.store.count_error = False
        env.store.counts["queued"] = 8
        body = assert_metrics(await client.get("/metrics"))
        assert body["status"] == "ok" and body["queues"]["agent_tasks"] == 8
    assert env.store.count_calls == 4


async def test_ready_and_metrics_share_service_refresh_and_inflight_counts(metrics_env, probes):
    env = metrics_env
    probes.gate.clear()
    env.store.count_gate.clear()
    async with client_for(env.app) as client:
        requests = [asyncio.create_task(client.get("/metrics")) for _ in range(6)]
        ready = asyncio.create_task(client.get("/readyz"))
        await asyncio.wait_for(probes.entered.wait(), 1)
        await asyncio.wait_for(env.store.count_entered.wait(), 1)
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        assert (await client.get("/api/time")).status_code == 200
        probes.gate.set()
        env.store.count_gate.set()
        responses = await asyncio.wait_for(asyncio.gather(*requests), 1)
        assert all(assert_metrics(response)["status"] == "ok" for response in responses)
        assert (await ready).status_code == 200
    assert env.store.count_calls == 1 and set(probes.calls.values()) == {1}


@pytest.mark.parametrize("storage_timeout", [False, True])
async def test_ready_storage_failure_invalidates_hot_counts(
    metrics_env, probes, monkeypatch, storage_timeout
):
    env = metrics_env
    env.store.counts["queued"] = 4
    async with client_for(env.app) as client:
        assert assert_metrics(await client.get("/metrics"))["queues"]["agent_tasks"] == 4
        if storage_timeout:
            env.store.gate = asyncio.Event()
            monkeypatch.setattr(health_api, "STORAGE_BUDGET_SECS", 0.01)
        else:
            env.store.error = True
        ready = await asyncio.wait_for(client.get("/readyz"), 1)
        assert ready.status_code == 503
        assert env.app.state.health.current_task_counts() == (None, None)
        env.store.count_error = True
        assert assert_metrics(await client.get("/metrics"))["task_counts"] is None
        env.store.count_error = False
        env.store.counts["queued"] = 5
        assert assert_metrics(await client.get("/metrics"))["queues"]["agent_tasks"] == 5


async def test_storage_failure_discards_prior_inflight_count_success(metrics_env, probes):
    env = metrics_env
    env.store.counts["queued"] = 9
    env.store.count_gate.clear()
    async with client_for(env.app) as client:
        metrics = asyncio.create_task(client.get("/metrics"))
        await asyncio.wait_for(env.store.count_entered.wait(), 1)
        env.store.error = True
        assert (await client.get("/readyz")).status_code == 503
        env.store.count_gate.set()
        body = assert_metrics(await asyncio.wait_for(metrics, 1))
        assert body["task_counts"] is None and body["status"] == "partial"
        env.store.counts["queued"] = 10
        assert assert_metrics(await client.get("/metrics"))["queues"]["agent_tasks"] == 10


async def test_storage_failure_during_service_wait_cannot_publish_local_old_counts(
    metrics_env, probes
):
    env = metrics_env
    env.store.counts["queued"] = 9
    async with client_for(env.app) as client:
        await client.get("/metrics")
        probes.entered.clear()
        probes.gate.clear()
        env.app.state.health._sampled_at = None
        metrics = asyncio.create_task(client.get("/metrics"))
        await asyncio.wait_for(probes.entered.wait(), 1)
        env.store.error = True
        storage = await env.app.state.health.storage_status()
        assert storage["status"] == "unavailable"
        probes.gate.set()
        body = assert_metrics(await asyncio.wait_for(metrics, 1))
        assert body["task_counts"] is None and body["queues"]["agent_tasks"] is None


@pytest.mark.parametrize("failure", ["observed", "unknown"])
async def test_service_observation_failure_differs_from_incomplete_metrics(
    metrics_env, probes, failure
):
    env = metrics_env
    probes.statuses["asr"] = (
        ("unavailable", "http_error") if failure == "observed" else ("error", "ignored")
    )
    async with client_for(env.app) as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/readyz")).status_code == 503
        body = assert_metrics(await client.get("/metrics"))
    assert body["status"] == ("ok" if failure == "observed" else "partial")
    assert body["task_counts"] is not None


async def test_count_and_request_timeouts_preserve_other_fields_and_recover(
    metrics_env, probes, monkeypatch
):
    env = metrics_env
    env.store.count_gate.clear()
    monkeypatch.setattr(health_api, "TASK_BUDGET_SECS", 0.01)
    async with client_for(env.app) as client:
        body = assert_metrics(await asyncio.wait_for(client.get("/metrics"), 1))
        assert body["status"] == "partial" and body["task_counts"] is None
        assert body["live_connections"] == 0 and body["services"]["asr"]["status"] == "ok"
        assert env.store.count_cancelled.is_set()
        env.store.count_gate.set()
        assert assert_metrics(await client.get("/metrics"))["status"] == "ok"
        probes.gate.clear()
        env.app.state.health._sampled_at = None
        monkeypatch.setattr(health_api, "REQUEST_BUDGET_SECS", 0.01)
        body = assert_metrics(await asyncio.wait_for(client.get("/metrics"), 1))
        assert body["status"] == "partial" and body["task_counts"] is not None
        assert not env.app.state.health._service_task.done()
        probes.gate.set()
        await asyncio.wait_for(env.app.state.health._service_task, 1)
        assert assert_metrics(await client.get("/metrics"))["status"] == "ok"


async def test_client_cancel_preserves_shared_count_query(metrics_env, probes):
    env = metrics_env
    env.store.count_gate.clear()
    async with client_for(env.app) as client:
        first = asyncio.create_task(client.get("/metrics"))
        await asyncio.wait_for(env.store.count_entered.wait(), 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not env.store.count_cancelled.is_set()
        second = asyncio.create_task(client.get("/metrics"))
        env.store.count_gate.set()
        assert assert_metrics(await asyncio.wait_for(second, 1))["status"] == "ok"
    assert env.store.count_calls == 1


async def test_shutdown_cancels_count_before_store_close_and_pending_http_is_safe(
    make_cfg, tmp_path, probes, monkeypatch
):
    cfg = make_cfg()
    cfg.agent.enabled = False
    app = create_app(cfg, static_dir=tmp_path / "no-build")
    entered, stopped, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_close = Store.close

    async def slow_counts(store):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert not closed.is_set()
            stopped.set()

    async def close(store):
        assert stopped.is_set() and app.state.health.lifecycle == "stopping"
        assert app.state.health._counts_task is None
        closed.set()
        await original_close(store)

    monkeypatch.setattr(Store, "task_counts", slow_counts)
    monkeypatch.setattr(Store, "close", close)
    async with client_for(app) as client:
        async with app.router.lifespan_context(app):
            request = asyncio.create_task(client.get("/metrics"))
            await asyncio.wait_for(entered.wait(), 1)
        body = assert_metrics(await asyncio.wait_for(request, 1))
        assert body["status"] == "partial" and body["lifecycle"] == "stopping"
        assert body["live_connections"] is body["task_counts"] is None
        assert closed.is_set()


@pytest.mark.parametrize(
    "field,bad",
    [
        ("caption_lag_seconds", float("nan")),
        ("caption_sample_age_seconds", float("inf")),
        ("caption_lag_seconds", -1.0),
        ("caption_lag_seconds", SENTINEL),
        ("unsaved_utterance_count", -1),
        ("unsaved_utterance_count", True),
    ],
)
async def test_invalid_recorder_numbers_return_safe_partial_group(metrics_env, probes, field, bad):
    recorder = SimpleNamespace(
        caption_lag_seconds=0.5, caption_sample_age_seconds=1, unsaved_utterance_count=3
    )
    setattr(recorder, field, bad)
    metrics_env.resources.sessions.live = live_with(recorder)
    async with client_for(metrics_env.app) as client:
        body = assert_metrics(await client.get("/metrics"))
    assert body["status"] == "partial" and body["live_connections"] == 1
    if field.startswith("caption"):
        assert body["caption_lag_seconds"] is body["caption_sample_age_seconds"] is None
        assert body["queues"]["transcript_retry"] == 3
    else:
        assert body["queues"]["transcript_retry"] is None
        assert body["caption_lag_seconds"] == 0.5


@pytest.mark.parametrize("bad", [-1, True, float("nan"), SENTINEL])
async def test_invalid_task_count_never_serializes_partial_group(metrics_env, probes, bad):
    metrics_env.store.counts["queued"] = bad
    async with client_for(metrics_env.app) as client:
        body = assert_metrics(await client.get("/metrics"))
    assert body["status"] == "partial" and body["task_counts"] is None


async def test_missing_runtime_resources_is_partial_but_retains_other_snapshots(
    metrics_env, probes
):
    del metrics_env.app.state.resources
    async with client_for(metrics_env.app) as client:
        body = assert_metrics(await client.get("/metrics"))
    assert body["status"] == "partial" and body["live_connections"] is None
    assert body["queues"]["transcript_retry"] is None and body["task_counts"] is not None
    assert body["services"]["asr"]["status"] == "ok"
