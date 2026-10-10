"""转录优先存活：模型、语音合成、嵌入、后台 agent 出问题时照常出字幕；识别服务恢复后字幕接着出；
应用重启后会议还在、可以继续；健康检查如实反映这些状态。"""

from __future__ import annotations

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
        return body if body["status"] == "degraded" else None

    ready = await until(degraded, "就绪检查报告降级")
    assert ready["services"]["asr"]["status"] == "ok"
    assert ready["services"]["tts"]["status"] == "unavailable"


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
    assert (await http.get("/readyz")).status_code == 200


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
    reason="已知问题只在 Ctrl+C（SIGINT）下确认过；Windows 上停应用发的是 Ctrl+Break",
)
@pytest.mark.xfail(
    strict=True,
    reason="已知问题：会议进行中按 Ctrl+C，uvicorn 停在关闭 HTTP 服务这一步不退出，要再按一次 Ctrl+C",
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
    ready = (await http.get("/readyz")).json()
    assert ready["status"] == "ok" and ready["lifecycle"] == "running"
    assert set(ready["services"]) == {"asr", "realtime", "tts", "embedding", "agent"}
    metrics = (await http.get("/metrics")).json()
    assert metrics["live_connections"] == 0 and metrics["status"] in ("ok", "partial")

    getattr(inference, service).down = True
    name = {"asr": "asr", "llm": "realtime"}[service]

    async def reported():
        response = await http.get("/readyz")
        body = response.json()
        return (response.status_code, body) if body["services"][name]["status"] != "ok" else None

    status, body = await until(reported, "健康检查发现服务不通")
    if service == "asr":  # 识别是核心功能：不通就是没就绪
        assert status == 503 and body["status"] == "not_ready"
    else:
        assert status == 200 and body["status"] == "degraded"
