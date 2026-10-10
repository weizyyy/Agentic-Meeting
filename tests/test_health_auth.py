"""真实口令中间件与匿名监控端点的组合边界，不连接推理服务。"""

from __future__ import annotations

import pytest
from loguru import logger
from test_health_api import assert_ready, client_for
from test_health_api import probes as probes
from test_metrics_api import assert_metrics

from agentic_meeting.types import Utterance
from agentic_meeting.web import auth
from agentic_meeting.web.app import create_app

PASSWORD = "FAKE-AUTH-MONITOR-PASSWORD"
CONTENT = "FAKE-AUTH-MONITOR-CONTENT"


@pytest.fixture
def password_cfg(make_cfg, monkeypatch):
    cfg = make_cfg()
    cfg.server.password_env = "FAKE_MONITOR_PASSWORD_ENV"
    monkeypatch.setenv(cfg.server.password_env, PASSWORD)
    cfg.agent.enabled = cfg.screen.enabled = cfg.embedding.enabled = False
    cfg.tts.enabled = True
    return cfg


async def test_anonymous_snapshots_remain_safe_through_authenticated_app_lifecycle(
    password_cfg, tmp_path, probes, monkeypatch
):
    app = create_app(password_cfg, static_dir=tmp_path / "no-build")
    assert any(middleware.cls is auth.LoginRequired for middleware in app.user_middleware)
    now = [100.0]
    app.state.health.clock = lambda: now[0]
    logs = []
    sink = logger.add(logs.append, diagnose=True)

    async def snapshot(client, lifecycle, ready_status, metrics_status):
        for path in ("/healthz", "/readyz", "/metrics"):
            response = await client.get(path)
            assert "cookie" not in response.request.headers
            assert "authorization" not in response.request.headers
            assert "set-cookie" not in response.headers
            assert PASSWORD not in response.text and CONTENT not in response.text
            if path == "/healthz":
                assert response.status_code == 200 and response.json() == {"status": "ok"}
            elif path == "/readyz":
                body = response.json()
                assert_ready(body)
                assert body["lifecycle"] == lifecycle and body["status"] == ready_status
                assert response.status_code == (503 if ready_status == "not_ready" else 200)
            else:
                body = assert_metrics(response)
                assert body["lifecycle"] == lifecycle and body["status"] == metrics_status
        assert (await client.get("/api/time")).status_code == 401

    try:
        async with client_for(app) as client:
            await snapshot(client, "starting", "not_ready", "partial")
            assert not probes.calls
            async with app.router.lifespan_context(app):
                store = app.state.resources.store
                session = await store.create_session(CONTENT)
                await store.ensure_speaker(session.id, 1, CONTENT)
                await store.add_utterance(Utterance(session.id, 1, 0, 1, CONTENT))
                task = await store.create_task(session.id, goal=CONTENT)
                await store.update_task(task.id, artifacts=[{"path": f"/{CONTENT}.txt"}])
                await snapshot(client, "running", "ok", "ok")
                now[0] += 5
                probes.statuses["tts"] = "unavailable", "timeout"
                await snapshot(client, "running", "degraded", "ok")
                now[0] += 5
                probes.statuses["asr"] = "unavailable", "http_error"
                await snapshot(client, "running", "not_ready", "ok")

                async def storage_failure():
                    raise RuntimeError(f"{PASSWORD} {CONTENT}")

                with monkeypatch.context() as fault:
                    fault.setattr(store, "check_available", storage_failure)
                    fault.setattr(store, "task_counts", storage_failure)
                    await snapshot(client, "running", "not_ready", "partial")
            calls = dict(probes.calls)
            await snapshot(client, "stopping", "not_ready", "partial")
            assert dict(probes.calls) == calls
    finally:
        logger.remove(sink)
    assert PASSWORD not in "".join(logs) and CONTENT not in "".join(logs)


@pytest.mark.parametrize("built", [False, True])
async def test_monitoring_paths_do_not_unlock_business_routes_or_other_methods(
    password_cfg, tmp_path, probes, built
):
    root = tmp_path / "static"
    if built:
        root.mkdir()
        (root / "index.html").write_text("<title>虚构页面</title>", encoding="utf-8")
    app = create_app(password_cfg, static_dir=root)
    async with client_for(app) as client:
        for method, path in (
            ("GET", "/api"),
            ("GET", "/api/"),
            ("GET", "/api/time"),
            ("GET", "/api/sessions"),
            ("GET", "/api/utterances"),
            ("GET", "/api/frames"),
            ("GET", "/api/frames/1/image"),
            ("GET", "/api/tasks"),
            ("GET", "/api/tasks/fake.t1/artifacts/fake.txt"),
            ("GET", "/api/sessions/fake/report"),
            ("GET", "/api/export/fake.zip"),
            ("POST", "/api/offer"),
            ("PATCH", "/api/offer"),
            ("GET", "/api/healthz"),
            ("GET", "/api/readyz"),
            ("GET", "/api/metrics"),
            ("GET", "/api/healthz-extra"),
            ("GET", "/api/metrics/anything"),
        ):
            response = await client.request(method, path)
            assert response.status_code == 401, (method, path)
            assert response.json() == {"error": auth.NEED_LOGIN}
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            for path in ("/healthz", "/readyz", "/metrics"):
                response = await client.request(method, path)
                assert response.status_code == 405, (method, path)
                assert "lifecycle" not in response.text
        for path in ("/healthz-extra", "/readyz/api/time", "/metrics/api/sessions"):
            assert (await client.get(path)).status_code == 404
    assert not probes.calls


async def test_login_keeps_business_gets_working_without_changing_public_metrics(
    password_cfg, tmp_path, probes
):
    app = create_app(password_cfg, static_dir=tmp_path / "no-build")
    async with app.router.lifespan_context(app), client_for(app) as client:
        session = await app.state.resources.store.create_session(CONTENT)
        assert (await client.get(f"/api/sessions/{session.id}")).status_code == 401
        response = await client.post("/api/auth/login", json={"password": PASSWORD})
        assert response.status_code == 200 and response.json()["authenticated"]
        assert auth.COOKIE_NAME in client.cookies
        assert PASSWORD not in response.text and PASSWORD not in response.headers["set-cookie"]
        assert (await client.get("/api/time")).status_code == 200
        detail = await client.get(f"/api/sessions/{session.id}")
        assert detail.status_code == 200 and detail.json()["title"] == CONTENT
        assert assert_metrics(await client.get("/metrics"))["status"] == "ok"
        async with client_for(app) as anonymous:
            assert assert_metrics(await anonymous.get("/metrics"))["status"] == "ok"
            assert (await anonymous.get(f"/api/sessions/{session.id}")).status_code == 401
