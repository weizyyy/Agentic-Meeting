"""转录优先存活：模型、语音合成、嵌入、后台 agent 出问题时照常出字幕；识别服务恢复后字幕接着出；
应用重启后会议还在、可以继续；健康检查如实反映这些状态。"""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from .waiting import until


async def test_transcription_survives_failing_assistant_services(app, http, meeting, inference):
    for service in (inference.llm, inference.tts, inference.embedding, inference.agent):
        service.down = True
    client = meeting()
    session = await client.connect()

    inference.asr.say("Jarvis，现在几点了")
    await client.speak()
    await client.next_message("utterance")
    await client.wait_for(
        lambda: any("助理暂不可用" in n["text"] for n in client.of_type("notice")),
        "提示助理暂不可用",
    )

    inference.asr.say("模型挂了字幕还在继续")
    await client.speak()
    assert (await client.next_message("utterance", text="模型挂了字幕还在继续"))["id"]
    client.type_text("打字也能记下来")
    await client.next_message("utterance", source="text")

    stored = (await http.get("/api/utterances", params={"session_id": session["id"]})).json()
    assert [u["text"] for u in stored["items"]] == [
        "Jarvis，现在几点了",
        "模型挂了字幕还在继续",
        "打字也能记下来",
    ]

    async def degraded():
        body = (await http.get("/readyz")).json()
        tts = body["services"]["tts"]["status"]  # 慢机器上探测超时会短暂报 unknown
        return body if body["status"] == "degraded" and tts == "unavailable" else None

    ready = await until(degraded, "就绪检查报告降级")
    assert ready["services"]["asr"]["status"] == "ok"


async def test_captions_resume_after_the_recognition_service_comes_back(
    app, http, meeting, inference
):
    client = meeting()
    await client.connect()
    inference.asr.down = True

    async def not_ready():
        response = await http.get("/readyz")
        return response.status_code == 503 and response.json()["status"] == "not_ready"

    await until(not_ready, "识别服务不通时就绪检查失败")
    await client.speak()  # 这段话识别不了，但连接和会议照常

    inference.asr.down = False
    assert (await http.get("/healthz")).json() == {"status": "ok"}

    async def recovered():
        inference.asr.say("识别服务回来了")
        await client.speak()
        return [u for u in client.of_type("utterance") if u["text"] == "识别服务回来了"]

    await until(recovered, "识别恢复后出字幕", timeout_secs=90)

    async def ready():  # 就绪检查的服务状态有几秒缓存，刚恢复时可能还是旧结果
        return (await http.get("/readyz")).status_code == 200

    await until(ready, "识别恢复后就绪检查通过")


async def test_meetings_survive_a_restart(start_app, inference, meeting_factory):
    first = await start_app()
    async with first.http() as http:
        meetings = meeting_factory(http)
        client = meetings()
        session = await client.connect()
        inference.asr.say("重启之前说的话")
        await client.speak()
        await client.next_message("utterance")
        await meetings.close_all()
    await first.shutdown()

    second = await start_app(data_dir=first.data_dir)
    async with second.http() as http:
        listed = (await http.get("/api/sessions")).json()["items"]
        assert [(s["id"], s["state"]) for s in listed] == [(session["id"], "interrupted")]
        client = meeting_factory(http)()
        resumed = await client.connect(session_id=session["id"])
        assert resumed["resumed"] is True
        inference.asr.say("重启之后接着说")
        await client.speak()
        await client.next_message("utterance")
        texts = [u["text"] for u in (await http.get("/api/utterances")).json()["items"]]
        assert texts == ["重启之前说的话", "重启之后接着说"]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows 上停应用发的是 Ctrl+Break，不是 Ctrl+C",
)
async def test_ctrl_c_during_a_meeting_stops_the_server(start_app, meeting_factory):
    """没有一并拉起模型服务时，按一次 Ctrl+C 应当直接停下；页面按原有逻辑发现连接断了，不需要额外通知。"""
    app = await start_app()
    async with app.http() as http:
        client = meeting_factory(http)()
        await client.connect()
        started = time.monotonic()
        await app.shutdown(timeout_secs=20)  # 超时会被强行结束
        assert time.monotonic() - started < 10, "按一次 Ctrl+C 没有停下"


@pytest.mark.parametrize("service", ["asr", "llm"])
async def test_health_endpoints_describe_the_services(app, http, inference, service):
    assert (await http.get("/healthz")).json() == {"status": "ok"}

    async def all_ok():  # 刚启动时服务还没探测完，先报 not_ready
        body = (await http.get("/readyz")).json()
        return body if body["status"] == "ok" else None

    ready = await until(all_ok, "启动后就绪检查通过")
    assert ready["lifecycle"] == "running"
    assert set(ready["services"]) == {"asr", "realtime", "tts", "embedding", "agent"}
    metrics = (await http.get("/metrics")).json()
    assert metrics["live_connections"] == 0 and metrics["status"] in ("ok", "partial")

    getattr(inference, service).down = True
    name = {"asr": "asr", "llm": "realtime"}[service]

    async def reported():
        # 等一次确实探测到这个服务不通、其余照常的结果；慢机器（Windows CI）上探测偶尔超出预算，
        # 会短暂报 unknown。存储一直可用，检查不能因为服务探测而超时
        response = await http.get("/readyz")
        body = response.json()
        assert body["storage"] == {"status": "ok", "reason": "checked"}, body
        services = body["services"]
        others_ok = all(
            s["status"] in ("ok", "reachable") for n, s in services.items() if n != name
        )
        if services[name]["status"] == "unavailable" and others_ok:
            return response.status_code, body
        return None

    status, body = await until(reported, "健康检查发现服务不通")
    if service == "asr":  # 识别是核心功能：不通就是没就绪
        assert (status, body["status"]) == (503, "not_ready"), body
    else:
        assert (status, body["status"]) == (200, "degraded"), body


async def test_readiness_storage_check_is_not_disturbed_by_service_probes(app, http):
    """服务探测每 5 秒刷新一次；刷新时存储检查照常在 0.5 秒内完成，就绪检查不会短暂报 not_ready。"""

    async def probed():
        body = (await http.get("/readyz")).json()
        return body["service_snapshot_age_seconds"] is not None

    await until(probed, "启动后完成第一次服务探测")
    refreshes, last_age = 0, None
    deadline = time.monotonic() + 30
    while refreshes < 3:  # 跨过三次刷新
        assert time.monotonic() < deadline, f"30 秒内只看到 {refreshes} 次服务探测刷新"
        body = (await http.get("/readyz")).json()
        assert body["storage"] == {"status": "ok", "reason": "checked"}, body
        age = body["service_snapshot_age_seconds"]
        if age is not None:
            if last_age is not None and age < last_age:
                refreshes += 1
            last_age = age
        await asyncio.sleep(0.05)
