"""只读运维探测：固定目标、安全结果、请求预算与资源所有权。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agentic_meeting.services import supervisor
from agentic_meeting.services.supervisor import ProbeTarget, build_probe_targets, probe_service


def targets(cfg):
    return {target.name: target for target in build_probe_targets(cfg)}


def test_build_targets_without_launch_files_or_discovery(make_cfg, monkeypatch):
    cfg = make_cfg()

    def forbidden(*args, **kwargs):
        pytest.fail("只读目标构建不得调用启动或文件读取路径")

    for name in ("build_specs", "find_executable", "asr_template_path", "load_asr_profile"):
        monkeypatch.setattr(supervisor, name, forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    cfg.asr.launch.model_path = cfg.asr.launch.mmproj_path = ""
    cfg.realtime_llm.active.launch.model_path = ""
    built = targets(cfg)
    assert list(built) == ["asr", "realtime", "tts", "embedding", "agent"]
    assert built["asr"].required and all(not t.required for n, t in built.items() if n != "asr")
    assert built["asr"].health_url == "http://127.0.0.1:8081/health"
    assert built["realtime"].health_url == "http://127.0.0.1:8080/health"
    assert built["tts"].health_url == "http://127.0.0.1:8082/health"
    assert built["embedding"].health_url == "http://127.0.0.1:8083/health"
    assert not cfg.resolve(cfg.session.data_dir).exists()


@pytest.mark.parametrize("managed", [False, True])
def test_external_and_local_launch_keep_services_enabled(make_cfg, managed):
    cfg = make_cfg()
    cfg.asr.launch.enabled = managed
    cfg.realtime_llm.active.launch.enabled = managed
    cfg.tts.launch.enabled = managed
    cfg.embedding.launch.enabled = managed
    built = targets(cfg)
    assert all(built[name].enabled for name in ("asr", "realtime", "tts", "embedding"))
    assert all(not built[name].any_status_ok for name in ("asr", "realtime", "tts", "embedding"))


def test_remote_targets_and_selected_realtime_endpoint(make_cfg):
    cfg = make_cfg()
    cfg.asr.base_url = "https://asr.test/api/v1"
    cfg.realtime_llm.mode = "openai_api"
    cfg.realtime_llm.active.base_url = "https://realtime.test/api/v1/"
    cfg.realtime_llm.active.api_key_env = "FAKE_REALTIME_KEY"
    cfg.tts.api_key_env = "FAKE_TTS_KEY"
    cfg.agent.base_url = "https://agent.test/v1/"
    cfg.agent.model = "fake-model"
    cfg.agent.enabled = True
    built = targets(cfg)
    assert built["asr"].health_url == "https://asr.test/health"
    assert built["realtime"].health_url == "https://realtime.test/api/v1/models"
    assert built["realtime"].any_status_ok
    assert built["realtime"].auth_env == "FAKE_REALTIME_KEY"
    assert built["tts"].auth_env == "FAKE_TTS_KEY"
    assert built["agent"].health_url == "https://agent.test/v1/models"
    assert built["agent"].any_status_ok


@pytest.mark.parametrize("has_health_endpoint", [False, True])
def test_target_consumes_config_health_semantics(make_cfg, monkeypatch, has_health_endpoint):
    cfg = make_cfg()
    cfg.realtime_llm.mode = "openai_api" if has_health_endpoint else "llama_server"
    cfg.realtime_llm.active.base_url = "https://realtime.test/api/v1"
    # 配置语义改变时，探测器无需知道具体端点类或接入方式。
    monkeypatch.setattr(
        type(cfg.realtime_llm), "has_health_endpoint", property(lambda _: has_health_endpoint)
    )
    target = targets(cfg)["realtime"]
    expected = "/health" if has_health_endpoint else "/api/v1/models"
    assert target.health_url == "https://realtime.test" + expected
    assert target.any_status_ok is (not has_health_endpoint)


@pytest.mark.parametrize(
    ("mode", "managed", "base_url", "path"),
    [
        ("llama_server", True, "http://127.0.0.1:8080/api/v1/", "/health"),
        ("llama_server", False, "https://realtime.test/api/v1/", "/health"),
        ("openai_api", False, "https://realtime.test/api/v1/", "/api/v1/models"),
        ("openai_api", False, "http://127.0.0.1:9000/api/v1/", "/api/v1/models"),
    ],
)
@pytest.mark.parametrize("status_code", [200, 401, 403, 404, 503])
async def test_realtime_probe_protocol_and_selected_auth(
    make_cfg, monkeypatch, mode, managed, base_url, path, status_code
):
    cfg = make_cfg()
    cfg.realtime_llm.mode = mode
    cfg.realtime_llm.llama_server.launch.enabled = managed
    cfg.realtime_llm.active.base_url = base_url
    cfg.realtime_llm.active.api_key_env = "FAKE_SELECTED_KEY"
    monkeypatch.setenv("FAKE_SELECTED_KEY", "fake-selected-token")
    target = targets(cfg)["realtime"]
    assert target.any_status_ok is (not cfg.realtime_llm.has_health_endpoint)

    def respond(request):
        assert request.url.path == path
        assert str(request.url) == target.health_url
        assert request.headers["Authorization"] == "Bearer fake-selected-token"
        return httpx.Response(status_code)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await probe_service(target, client)
    if mode == "openai_api":
        assert (result["status"], result["reason"]) == ("reachable", "http_response")
    elif status_code == 200:
        assert (result["status"], result["reason"]) == ("ok", "healthy")
    else:
        assert (result["status"], result["reason"]) == ("unavailable", "http_error")


@pytest.mark.parametrize("provider", ["none", "tasks", "caption", "digest", "report"])
def test_agent_selected_by_every_provider(make_cfg, provider):
    cfg = make_cfg()
    cfg.agent.enabled = provider == "tasks"
    cfg.screen.caption_provider = "agent_llm" if provider == "caption" else "realtime_llm"
    cfg.realtime.digest_provider = "agent_llm" if provider == "digest" else "realtime_llm"
    cfg.report.provider = "agent_llm" if provider == "report" else "realtime_llm"
    assert targets(cfg)["agent"].enabled is (provider != "none")


@pytest.mark.parametrize("disabled_field", ["screen", "caption", "vision"])
def test_inactive_agent_caption_provider_does_not_enable_agent(make_cfg, disabled_field):
    cfg = make_cfg()
    cfg.agent.enabled = False
    cfg.realtime.digest_provider = cfg.report.provider = "realtime_llm"
    cfg.screen.caption_provider = "agent_llm"
    if disabled_field == "screen":
        cfg.screen.enabled = False
    elif disabled_field == "caption":
        cfg.screen.caption = False
    else:
        cfg.agent.supports_vision = False
    assert not targets(cfg)["agent"].enabled


@pytest.mark.parametrize("missing", ["model", "base_url"])
async def test_selected_agent_missing_config_is_unavailable(make_cfg, missing):
    cfg = make_cfg()
    cfg.agent.enabled = False
    cfg.report.provider = "agent_llm"
    setattr(cfg.agent, missing, "")
    target = targets(cfg)["agent"]
    assert await probe_service(target) == {
        "enabled": True,
        "required": False,
        "status": "unavailable",
        "reason": "invalid_config",
    }


@pytest.mark.parametrize(
    "url",
    [
        "",
        "relative/v1",
        "ftp://service.test",
        "http://",
        "http://service.test:bad",
        "http://service.test:70000",
        "http://service.test:0",
        "http://[bad-ip]",
        "https://user:pass@service.test",
        "https://service.test/?key=fake",
        "https://service.test/#part",
        "https://service.test/space here",
    ],
)
async def test_invalid_targets_do_not_make_requests(make_cfg, url):
    cfg = make_cfg()
    cfg.asr.base_url = url
    target = targets(cfg)["asr"]

    def forbidden(request):
        pytest.fail("无效配置不能退回默认目标或发请求")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        result = await probe_service(target, client)
    assert result == {
        "enabled": True,
        "required": True,
        "status": "unavailable",
        "reason": "invalid_config",
    }


async def test_disabled_services_keep_metadata_and_never_probe(make_cfg):
    cfg = make_cfg()
    cfg.tts.enabled = cfg.embedding.enabled = cfg.agent.enabled = False
    cfg.realtime.digest_provider = cfg.report.provider = "realtime_llm"
    cfg.screen.caption_provider = "realtime_llm"

    def forbidden(request):
        pytest.fail("禁用服务不得发请求")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        for name in ("tts", "embedding", "agent"):
            assert await probe_service(targets(cfg)[name], client) == {
                "enabled": False,
                "required": False,
                "status": "disabled",
                "reason": "disabled",
            }


@pytest.mark.parametrize("generic", [False, True])
@pytest.mark.parametrize("code", [200, 401, 403, 404, 503])
async def test_safe_status_rules(generic, code):
    target = ProbeTarget(
        "agent", True, health_url="https://private.test/models", any_status_ok=generic
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(code))
    ) as c:
        result = await probe_service(target, c)
    status = "reachable" if generic else "ok" if code == 200 else "unavailable"
    reason = "http_response" if generic else "healthy" if code == 200 else "http_error"
    assert result == {"enabled": True, "required": False, "status": status, "reason": reason}
    assert "private.test" not in json.dumps(result)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpx.ConnectError, "connection_failed"),
        (httpx.ConnectTimeout, "timeout"),
        (httpx.ReadTimeout, "timeout"),
    ],
)
async def test_errors_are_mapped_without_private_details(error, reason):
    def fail(request):
        raise error("private URL and secret and upstream body")

    target = ProbeTarget("asr", True, True, "https://private.test/health")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        result = await probe_service(target, client)
    assert result == {"enabled": True, "required": True, "status": "unavailable", "reason": reason}
    assert "private" not in json.dumps(result)


async def test_probe_does_not_follow_redirects_or_read_response_body(monkeypatch):
    monkeypatch.setenv("FAKE_PROBE_KEY", "fake-probe-key")
    seen = []

    class UnreadBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("探测只读状态，不读取上游正文")
            yield b""

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://other.test/"}, stream=UnreadBody())

    target = ProbeTarget(
        "agent",
        True,
        health_url="https://private.test/models",
        auth_env="FAKE_PROBE_KEY",
        any_status_ok=True,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as c:
        result = await probe_service(target, c)
        assert not c.is_closed
    assert len(seen) == 1 and seen[0].method == "GET"
    assert seen[0].headers["Authorization"] == "Bearer fake-probe-key"
    assert "fake-probe-key" not in json.dumps(result)
    assert result["status"] == "reachable"


async def test_explicit_budget_cancels_wait_and_keeps_injected_client_open():
    cancelled = asyncio.Event()

    async def slow(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    target = ProbeTarget("asr", True, True, "http://127.0.0.1/health")
    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        result = await probe_service(target, client, timeout_secs=0.01)
        assert cancelled.is_set() and not client.is_closed
    assert result["status"] == "unavailable" and result["reason"] == "timeout"
    assert supervisor.PROBE_TIMEOUT_SECS == 5.0


async def test_request_cancellation_propagates_and_closes_owned_client(monkeypatch):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    monkeypatch.setattr(supervisor, "_new_client", lambda url: client)
    task = asyncio.create_task(
        probe_service(ProbeTarget("asr", True, True, "https://svc.test/health"))
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and client.is_closed


async def test_budget_timeout_closes_owned_client_and_stops_transport(monkeypatch):
    stopped = asyncio.Event()

    async def slow(request):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    monkeypatch.setattr(supervisor, "_new_client", lambda url: client)
    result = await probe_service(
        ProbeTarget("asr", True, True, "https://svc.test/health"), timeout_secs=0.01
    )
    assert result["reason"] == "timeout" and stopped.is_set() and client.is_closed


@pytest.mark.parametrize(
    "url,trust_env",
    [
        ("http://127.0.0.1/health", False),
        ("http://localhost/health", False),
        ("http://[::1]/health", False),
        ("https://remote.test/models", True),
    ],
)
async def test_owned_client_uses_existing_proxy_policy(monkeypatch, url, trust_env):
    real_client = httpx.AsyncClient
    settings = []

    def capture(**kwargs):
        settings.append(kwargs)
        return real_client(transport=httpx.MockTransport(lambda _: httpx.Response(200)), **kwargs)

    monkeypatch.setattr(supervisor.httpx, "AsyncClient", capture)
    assert (await probe_service(ProbeTarget("asr", True, True, url)))["status"] == "ok"
    assert settings[0]["trust_env"] is trust_env


async def test_default_and_injected_timeout_keep_cli_compatibility():
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200)

    target = ProbeTarget("asr", True, True, "http://127.0.0.1/health")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await supervisor.probe(target, client)
        await probe_service(target, client, timeout_secs=0.2)
    assert seen[0] == {"connect": 0.5, "read": 5.0, "write": 5.0, "pool": 5.0}
    assert seen[1] == {"connect": 0.2, "read": 0.2, "write": 0.2, "pool": 0.2}
