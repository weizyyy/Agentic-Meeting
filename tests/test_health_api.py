"""健康 HTTP 契约、按需快照与生命周期；不连接真实推理服务。"""

from __future__ import annotations

import asyncio
from collections import Counter
from types import SimpleNamespace

import httpx
import pytest

from agentic_meeting.services.supervisor import ProbeTarget
from agentic_meeting.store.db import Store
from agentic_meeting.web import health_api
from agentic_meeting.web.app import create_app


class FakeStore:
    def __init__(self):
        self.calls = 0
        self.error = False
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def check_available(self):
        self.calls += 1
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error:
            raise ValueError("private storage detail")


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def assert_ready(body):
    assert set(body) == {
        "status",
        "lifecycle",
        "storage",
        "services",
        "service_snapshot_age_seconds",
    }
    assert set(body["storage"]) == {"status", "reason"}
    assert body["status"] in {"ok", "degraded", "not_ready"}
    assert body["lifecycle"] in {"starting", "running", "stopping"}
    assert (
        body["storage"]["reason"]
        in {
            "ok": {"checked"},
            "unavailable": {"timeout", "storage_error"},
            "unknown": {"not_started", "shutting_down"},
        }[body["storage"]["status"]]
    )
    age = body["service_snapshot_age_seconds"]
    assert age is None or age >= 0
    assert set(body["services"]) == {"asr", "realtime", "tts", "embedding", "agent"}
    reasons = {
        "ok": {"healthy"},
        "reachable": {"http_response"},
        "unavailable": {"timeout", "connection_failed", "http_error", "invalid_config"},
        "unknown": {"not_started", "shutting_down", "refresh_failed", "budget_exhausted"},
        "disabled": {"disabled"},
    }
    for name, value in body["services"].items():
        assert set(value) == {"enabled", "required", "status", "reason"}
        assert value["required"] == (name == "asr")
        assert value["reason"] in reasons[value["status"]]
        assert value["enabled"] == (value["status"] != "disabled")
    assert "private" not in str(body)


@pytest.fixture
def probes(monkeypatch):
    calls = Counter()
    statuses = {}
    gate = asyncio.Event()
    gate.set()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake(target, *, timeout_secs):
        assert timeout_secs == health_api.SERVICE_BUDGET_SECS
        calls[target.name] += 1
        entered.set()
        try:
            await gate.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        status, reason = statuses.get(target.name, ("ok", "healthy"))
        if status == "error":
            raise RuntimeError("private inference detail")
        if not target.enabled:
            status, reason = "disabled", "disabled"
        return {
            "enabled": target.enabled,
            "required": target.required,
            "status": status,
            "reason": reason,
        }

    monkeypatch.setattr(health_api, "probe_service", fake)
    return SimpleNamespace(
        calls=calls, statuses=statuses, gate=gate, entered=entered, cancelled=cancelled
    )


@pytest.fixture
def running(make_cfg, tmp_path):
    cfg = make_cfg()
    cfg.agent.enabled = False
    cfg.tts.enabled = True
    cfg.embedding.enabled = True
    app = create_app(cfg, static_dir=tmp_path / "no-build")
    store = FakeStore()
    app.state.health.start(store)
    return app, store


@pytest.mark.parametrize("built", [True, False])
async def test_health_routes_precede_static_without_startup(make_cfg, tmp_path, built, probes):
    root = tmp_path / "dist"
    if built:
        root.mkdir()
        (root / "index.html").write_text("client", encoding="utf-8")
    app = create_app(make_cfg(), static_dir=root)
    async with client_for(app) as client:
        health = await client.get("/healthz")
        ready = await client.get("/readyz")
    assert health.status_code == 200 and health.json() == {"status": "ok"}
    assert ready.status_code == 503
    assert ready.headers["content-type"] == "application/json"
    body = ready.json()
    assert_ready(body)
    assert body["lifecycle"] == "starting"
    assert body["storage"] == {"status": "unknown", "reason": "not_started"}
    assert not probes.calls


@pytest.mark.parametrize(
    "failed,storage_error,expected,http_status",
    [
        (None, False, "ok", 200),
        ("asr", False, "not_ready", 503),
        (None, True, "not_ready", 503),
        ("realtime", False, "degraded", 200),
        ("tts", False, "degraded", 200),
        ("embedding", False, "degraded", 200),
    ],
)
async def test_readiness_core_and_optional_failures(
    running, probes, failed, storage_error, expected, http_status
):
    app, store = running
    store.error = storage_error
    if failed:
        probes.statuses[failed] = "unavailable", "connection_failed"
    async with client_for(app) as client:
        response = await client.get("/readyz")
        assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert response.status_code == http_status
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert_ready(body)
    assert body["status"] == expected
    assert body["service_snapshot_age_seconds"] is not None
    assert store.calls == 1
    await app.state.health.close()


async def test_disabled_and_generic_reachable_do_not_degrade(make_cfg, tmp_path, probes):
    cfg = make_cfg()
    cfg.tts.enabled = cfg.embedding.enabled = cfg.agent.enabled = False
    probes.statuses["realtime"] = "reachable", "http_response"
    app = create_app(cfg, static_dir=tmp_path)
    app.state.health.start(FakeStore())
    async with client_for(app) as client:
        body = (await client.get("/readyz")).json()
    assert_ready(body)
    assert body["status"] == "ok"
    for name in ("tts", "embedding", "agent"):
        assert body["services"][name]["status"] == "disabled"
    await app.state.health.close()


async def test_health_never_probes_even_when_resources_fail(running, probes):
    app, store = running
    store.error = True
    probes.gate.clear()
    async with client_for(app) as client:
        assert (await client.get("/healthz")).status_code == 200
    assert store.calls == 0 and not probes.calls
    await app.state.health.close()


async def test_concurrent_requests_share_refresh_and_only_inflight_storage(running, probes):
    app, store = running
    probes.gate.clear()
    store.gate = asyncio.Event()
    async with client_for(app) as client:
        requests = [asyncio.create_task(client.get("/readyz")) for _ in range(6)]
        await asyncio.wait_for(probes.entered.wait(), 1)
        await asyncio.wait_for(store.entered.wait(), 1)
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/api/time")).status_code == 200
        probes.gate.set()
        store.gate.set()
        responses = await asyncio.wait_for(asyncio.gather(*requests), 1)
        assert all(r.status_code == 200 for r in responses)
        assert store.calls == 1
        await client.get("/readyz")
        assert store.calls == 2
    assert set(probes.calls.values()) == {1}
    await app.state.health.close()


async def test_cancelled_request_does_not_cancel_shared_refresh(running, probes):
    app, _ = running
    probes.gate.clear()
    async with client_for(app) as client:
        first = asyncio.create_task(client.get("/readyz"))
        await asyncio.wait_for(probes.entered.wait(), 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not probes.cancelled.is_set()
        second = asyncio.create_task(client.get("/readyz"))
        probes.gate.set()
        assert (await asyncio.wait_for(second, 1)).status_code == 200
    assert set(probes.calls.values()) == {1}
    await app.state.health.close()


async def test_ttl_expiry_failure_never_reuses_old_success_and_recovers(running, probes):
    app, _ = running
    now = [10.0]
    app.state.health.clock = lambda: now[0]
    async with client_for(app) as client:
        assert (await client.get("/readyz")).json()["status"] == "ok"
        now[0] += 4.0
        assert (await client.get("/readyz")).json()["service_snapshot_age_seconds"] == 4
        assert probes.calls["asr"] == 1
        now[0] += 1.0
        probes.statuses["asr"] = "error", "ignored"
        failed = (await client.get("/readyz")).json()
        assert failed["status"] == "not_ready"
        assert failed["services"]["asr"]["reason"] == "refresh_failed"
        assert failed["services"]["realtime"]["status"] == "ok"
        assert failed["service_snapshot_age_seconds"] is None
        await client.get("/readyz")
        assert probes.calls["asr"] == 2
        now[0] += 5
        probes.statuses.clear()
        assert (await client.get("/readyz")).status_code == 200
    await app.state.health.close()


async def test_round_budget_keeps_completed_targets(running, monkeypatch):
    app, _ = running
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def probe(target, **kwargs):
        if target.name == "asr":
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return {
            "enabled": target.enabled,
            "required": target.required,
            "status": "ok" if target.enabled else "disabled",
            "reason": "healthy" if target.enabled else "disabled",
        }

    monkeypatch.setattr(health_api, "probe_service", probe)
    monkeypatch.setattr(health_api, "SERVICE_BUDGET_SECS", 0.03)
    async with client_for(app) as client:
        response = await asyncio.wait_for(client.get("/readyz"), 1)
    assert entered.is_set() and cancelled.is_set()
    body = response.json()
    assert_ready(body)
    assert response.status_code == 503
    assert body["services"]["asr"]["reason"] == "budget_exhausted"
    assert body["services"]["realtime"]["status"] == "ok"
    assert body["service_snapshot_age_seconds"] is None
    await app.state.health.close()


async def test_storage_timeout_and_request_budget_are_safe(running, probes, monkeypatch):
    app, store = running
    store.gate = asyncio.Event()
    monkeypatch.setattr(health_api, "STORAGE_BUDGET_SECS", 0.03)
    async with client_for(app) as client:
        response = await asyncio.wait_for(client.get("/readyz"), 1)
        assert response.json()["storage"] == {"status": "unavailable", "reason": "timeout"}
        store.gate.set()
        probes.gate.clear()
        app.state.health._sampled_at = None
        monkeypatch.setattr(health_api, "REQUEST_BUDGET_SECS", 0.03)
        response = await asyncio.wait_for(client.get("/readyz"), 1)
        assert response.status_code == 503
        assert_ready(response.json())
        assert not app.state.health._service_task.done()
        probes.gate.set()
        await asyncio.wait_for(app.state.health._service_task, 1)
        assert (await client.get("/readyz")).status_code == 200
    await app.state.health.close()


async def test_app_instances_do_not_share_cache(make_cfg, tmp_path, probes):
    apps = [create_app(make_cfg(), static_dir=tmp_path) for _ in range(2)]
    for app in apps:
        app.state.health.start(FakeStore())
        async with client_for(app) as client:
            assert (await client.get("/readyz")).status_code == 200
    assert probes.calls["asr"] == 2
    assert apps[0].state.health._services is not apps[1].state.health._services
    for app in apps:
        await app.state.health.close()


async def test_lifespan_stops_collection_before_store_and_business_cleanup(
    make_cfg, tmp_path, probes, monkeypatch
):
    cfg = make_cfg()
    cfg.agent.enabled = False
    probes.gate.clear()
    app = create_app(cfg, static_dir=tmp_path)
    original_close = Store.close
    closed = asyncio.Event()

    async def close(store):
        assert app.state.health.lifecycle == "stopping"
        assert app.state.health._service_task is None
        assert app.state.health._storage_task is None
        closed.set()
        await original_close(store)

    monkeypatch.setattr(Store, "close", close)
    async with app.router.lifespan_context(app), client_for(app) as client:
        assert app.state.health.lifecycle == "running"
        refresh = asyncio.create_task(app.state.health.service_snapshot())
        await asyncio.wait_for(probes.entered.wait(), 1)
        assert (await client.get("/healthz")).status_code == 200
    assert closed.is_set() and probes.cancelled.is_set()
    with pytest.raises(asyncio.CancelledError):
        await refresh
    calls = probes.calls.copy()
    async with client_for(app) as client:
        response = await client.get("/readyz")
        assert (await client.get("/healthz")).status_code == 200
    assert response.status_code == 503
    assert_ready(response.json())
    assert response.json()["storage"] == {"status": "unknown", "reason": "shutting_down"}
    assert response.json()["services"]["asr"]["reason"] == "shutting_down"
    assert probes.calls == calls


async def test_probes_close_owned_http_clients_before_shutdown(running, monkeypatch):
    app, _ = running
    app.state.health.targets = [
        ProbeTarget(name, name == "asr", name == "asr", "http://127.0.0.1/health")
        for name in ("asr", "realtime", "tts", "embedding", "agent")
    ]
    from agentic_meeting.services import supervisor

    clients = []
    entered = asyncio.Event()

    async def response(request):
        entered.set()
        await asyncio.Event().wait()

    def new_client(url):
        client = httpx.AsyncClient(transport=httpx.MockTransport(response))
        clients.append(client)
        return client

    monkeypatch.setattr(supervisor, "_new_client", new_client)
    refresh = asyncio.create_task(app.state.health.service_snapshot())
    await asyncio.wait_for(entered.wait(), 1)
    await app.state.health.close()
    with pytest.raises(asyncio.CancelledError):
        await refresh
    assert clients and all(client.is_closed for client in clients)


async def test_shutdown_of_shared_refresh_returns_stopping_for_pending_http(running, probes):
    app, store = running
    probes.gate.clear()
    store.gate = asyncio.Event()
    async with client_for(app) as client:
        request = asyncio.create_task(client.get("/readyz"))
        await asyncio.wait_for(probes.entered.wait(), 1)
        await asyncio.wait_for(store.entered.wait(), 1)
        await app.state.health.close()
        response = await asyncio.wait_for(request, 1)
    assert response.status_code == 503
    assert_ready(response.json())
    assert response.json()["lifecycle"] == "stopping"
    assert response.json()["storage"] == {"status": "unknown", "reason": "shutting_down"}
    assert response.json()["services"]["asr"]["reason"] == "shutting_down"


async def test_unexpected_round_failure_is_cached_and_then_recovers(running, probes, monkeypatch):
    app, _ = running
    now = [0.0]
    health = app.state.health
    health.clock = lambda: now[0]
    refresh = health._refresh_services
    calls = 0

    async def broken():
        nonlocal calls
        calls += 1
        raise RuntimeError("private shared refresh failure")

    monkeypatch.setattr(health, "_refresh_services", broken)
    async with client_for(app) as client:
        for _ in range(2):
            response = await client.get("/readyz")
            assert response.status_code == 503
            assert_ready(response.json())
            assert response.json()["services"]["asr"]["reason"] == "refresh_failed"
        assert calls == 1
        now[0] += health_api.SERVICE_TTL_SECS
        monkeypatch.setattr(health, "_refresh_services", refresh)
        assert (await client.get("/readyz")).status_code == 200
    await health.close()


async def test_optional_unknown_is_degraded_and_age_is_unknown(running, probes):
    app, _ = running
    probes.statuses["realtime"] = "error", "ignored"
    async with client_for(app) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    assert_ready(response.json())
    assert response.json()["status"] == "degraded"
    assert response.json()["services"]["realtime"]["reason"] == "refresh_failed"
    assert response.json()["service_snapshot_age_seconds"] is None
    await app.state.health.close()


async def test_no_runtime_discovery_or_weights_needed(make_cfg, tmp_path, probes, monkeypatch):
    from agentic_meeting.services import supervisor

    def forbidden(*args, **kwargs):
        raise AssertionError("health must not build launch specs or find executables")

    monkeypatch.setattr(supervisor, "build_specs", forbidden)
    monkeypatch.setattr(supervisor, "find_executable", forbidden)
    cfg = make_cfg()
    assert not cfg.resolve(cfg.asr.launch.model_path).exists()
    app = create_app(cfg, static_dir=tmp_path / "no-build")
    app.state.health.start(FakeStore())
    async with client_for(app) as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/readyz")).status_code == 200
    await app.state.health.close()
