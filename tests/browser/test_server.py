"""底座自检：隔离、服务替换和生产入口边界。"""

from pathlib import Path

import httpx

from agentic_meeting.store.db import Store
from agentic_meeting.web.app import create_app
from tests.browser.server import application, configuration, seed


async def test_seed_isolation_and_injected_services(tmp_path: Path, monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("浏览器底座不能建立真实推理服务")

    for name in ("build_realtime_llm", "build_agent_llm", "build_runner"):
        monkeypatch.setattr(f"agentic_meeting.web.app.{name}", forbidden)
    cfg = configuration(tmp_path)
    stores = [await Store.open(tmp_path / f"{i}.db", cfg.embedding.dimensions) for i in range(2)]
    try:
        seeds = await seed(stores[0], tmp_path)
        assert await stores[1].get_session(seeds["ended"]) is None
        app = application(cfg, stores[0], seeds)
        async with app.router.lifespan_context(app):
            resources = app.state.resources
            assert resources.embedder is resources.background is resources.tasks is None
            assert not resources.captions.enabled
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://test"
            ) as client:
                assert (await client.get("/healthz")).status_code == 200
                assert (await client.get("/__test/state")).json()["ended"] == seeds["ended"]
        production = create_app(cfg, store=stores[0])
        assert not any(
            getattr(route, "path", "").startswith("/__test") for route in production.routes
        )
    finally:
        for store in stores:
            await store.close()
