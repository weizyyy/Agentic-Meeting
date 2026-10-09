"""HTTP 应用（web/app.py）：对时、信令、静态站点。

用 ``httpx.ASGITransport`` 直接调应用，不监听端口。信令的两个路由用注入的假处理器和假 bot 验证：
真正的 WebRTC 协商不在这里测（scripts/soak.py 会走一遍）。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCPatchRequest

from agentic_meeting.web import app as web_app
from agentic_meeting.web.app import create_app


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def cfg(make_cfg):
    return make_cfg()


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    """一个假的 client/dist。"""
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(
        "<!doctype html><title>组会助理</title><div id=root></div>", "utf-8"
    )
    (root / "assets" / "app.js").write_text("console.log('ok')", "utf-8")
    return root


class FakeConnection:
    pass


class FakeHandler:
    """代替 SmallWebRTCRequestHandler：记录收到的请求，并像真的一样回调 bot。"""

    def __init__(self):
        self.requests = []
        self.patches: list[SmallWebRTCPatchRequest] = []
        self.closed = False
        self.connection = FakeConnection()

    async def handle_web_request(self, request, webrtc_connection_callback):
        self.requests.append(request)
        await webrtc_connection_callback(self.connection)
        return {"sdp": "answer-sdp", "type": "answer", "pc_id": "pc-1"}

    async def handle_patch_request(self, request):
        self.patches.append(request)

    async def close(self):
        self.closed = True


# --------------------------------------------------------------------------- #
# /api/time
# --------------------------------------------------------------------------- #


async def test_time_returns_the_server_clock(cfg, tmp_path):
    async with client_for(create_app(cfg, static_dir=tmp_path / "nope")) as client:
        response = await client.get("/api/time")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"server_time"}
    assert isinstance(body["server_time"], float)
    assert abs(body["server_time"] - time.time()) < 5  # Unix 秒（浮点）


async def test_time_keeps_sub_second_precision(cfg, tmp_path, monkeypatch):
    # 浏览器靠它估算与服务端的时钟偏移，截图时间轴要精确到毫秒级。
    monkeypatch.setattr(web_app, "time", SimpleNamespace(time=lambda: 1234.5678))
    async with client_for(create_app(cfg, static_dir=tmp_path / "nope")) as client:
        assert (await client.get("/api/time")).json() == {"server_time": 1234.5678}


# --------------------------------------------------------------------------- #
# 静态站点
# --------------------------------------------------------------------------- #


async def test_root_explains_how_to_build_the_client_when_dist_is_missing(cfg, tmp_path):
    async with client_for(create_app(cfg, static_dir=tmp_path / "nope")) as client:
        response = await client.get("/")
    assert response.status_code != 500
    assert "请先构建客户端" in response.text
    assert "npm run build" in response.text


async def test_root_serves_the_built_client(cfg, dist):
    async with client_for(create_app(cfg, static_dir=dist)) as client:
        index = await client.get("/")
        asset = await client.get("/assets/app.js")
        api = await client.get("/api/time")  # 站点挂在根路径，不能盖住 API
    assert index.status_code == 200 and "组会助理" in index.text
    assert asset.status_code == 200 and "console.log" in asset.text
    assert api.status_code == 200 and "server_time" in api.json()


@pytest.mark.parametrize("built", [True, False])
async def test_errors_are_json_with_a_chinese_message(cfg, dist, tmp_path, built):
    app = create_app(cfg, static_dir=dist if built else tmp_path / "nope")
    async with client_for(app) as client:
        response = await client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert set(response.json()) == {"error"} and response.json()["error"]


# --------------------------------------------------------------------------- #
# 信令
# --------------------------------------------------------------------------- #


async def test_offer_passes_the_connection_and_request_data_to_the_bot(cfg, tmp_path):
    handler, calls = FakeHandler(), []

    async def bot(connection, request_data, resources):
        calls.append((connection, request_data, resources))

    app = create_app(cfg, handler=handler, bot=bot, static_dir=tmp_path / "nope")
    async with app.router.lifespan_context(app), client_for(app) as client:
        response = await client.post(
            "/api/offer",
            json={
                "sdp": "offer-sdp",
                "type": "offer",
                "pc_id": None,
                "restart_pc": False,
                "requestData": {"note": "abc"},  # 浏览器端 SDK 用的是驼峰写法
            },
        )

    assert response.status_code == 200
    assert response.json() == {"sdp": "answer-sdp", "type": "answer", "pc_id": "pc-1"}
    assert handler.requests[0].sdp == "offer-sdp"
    ((connection, request_data, resources),) = calls
    assert connection is handler.connection
    assert request_data == {"note": "abc"}
    assert resources.cfg is cfg


async def test_offer_accepts_the_snake_case_request_data_too(cfg, tmp_path):
    handler, calls = FakeHandler(), []

    async def bot(connection, request_data, resources):
        calls.append(request_data)

    app = create_app(cfg, handler=handler, bot=bot, static_dir=tmp_path / "nope")
    async with app.router.lifespan_context(app), client_for(app) as client:
        await client.post(
            "/api/offer", json={"sdp": "s", "type": "offer", "request_data": {"k": 1}}
        )
    assert calls == [{"k": 1}]


@pytest.mark.parametrize(
    "payload",
    [b"not json", b"[]", b'{"type": "offer"}', b'{"sdp": "x", "type": "offer", "unknown": 1}'],
)
async def test_malformed_offer_is_rejected_with_a_json_error(cfg, tmp_path, payload):
    app = create_app(cfg, handler=FakeHandler(), static_dir=tmp_path / "nope")
    async with app.router.lifespan_context(app), client_for(app) as client:
        response = await client.post(
            "/api/offer", content=payload, headers={"content-type": "application/json"}
        )
    assert response.status_code == 400
    assert response.json()["error"]


async def test_patch_forwards_ice_candidates(cfg, tmp_path):
    handler = FakeHandler()
    app = create_app(cfg, handler=handler, static_dir=tmp_path / "nope")
    payload = {
        "pc_id": "pc-1",
        "candidates": [{"candidate": "candidate:1", "sdp_mid": "0", "sdp_mline_index": 0}],
    }
    async with app.router.lifespan_context(app), client_for(app) as client:
        response = await client.patch("/api/offer", json=payload)

    assert response.status_code == 200 and response.json() == {"status": "success"}
    (patch,) = handler.patches
    assert patch.pc_id == "pc-1" and patch.candidates[0].candidate == "candidate:1"


async def test_lifespan_creates_and_closes_the_default_handler(cfg, tmp_path, monkeypatch):
    created = []

    class Recording(FakeHandler):
        def __init__(self, ice_servers=None, **kwargs):
            super().__init__()
            created.append(ice_servers)

    monkeypatch.setattr(web_app, "SmallWebRTCRequestHandler", Recording)
    cfg.server.ice_servers = ["stun:stun.example.com:3478"]
    app = create_app(cfg, static_dir=tmp_path / "nope")
    async with app.router.lifespan_context(app):
        handler = app.state.handler
        assert isinstance(handler, Recording) and not handler.closed
    assert handler.closed  # 应用关闭时断开全部连接

    (ice_servers,) = created
    assert [server.urls for server in ice_servers] == ["stun:stun.example.com:3478"]


async def test_no_ice_servers_are_passed_when_none_are_configured(cfg, tmp_path, monkeypatch):
    created = []

    class Recording(FakeHandler):
        def __init__(self, ice_servers=None, **kwargs):
            super().__init__()
            created.append(ice_servers)

    monkeypatch.setattr(web_app, "SmallWebRTCRequestHandler", Recording)
    cfg.server.ice_servers = []
    app = create_app(cfg, static_dir=tmp_path / "nope")
    async with app.router.lifespan_context(app):
        pass
    assert created == [None]  # 同一局域网内留空即可
