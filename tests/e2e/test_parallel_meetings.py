"""同一台机器上同时开几场会：各场互不顶替，字幕、截图、助理的回答各归各的，实时模型各用一个槽位；
位置满了拒绝新会议并说明原因，同一场会议换设备照样可以；结束一场就空出位置。"""

from __future__ import annotations

import io
import json
import time

from PIL import Image

from .inference import Reply
from .waiting import until, until_sync

PARALLEL = {
    "session.max_live_meetings": 2,
    "realtime_llm.llama_server.background_slot": 2,
    "realtime_llm.llama_server.launch.parallel": 3,
}


def _screenshot() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (320, 180), "#336699").save(buffer, format="JPEG")
    return buffer.getvalue()


def _slot_of(inference, question: str) -> int:
    """问了 ``question`` 的那次实时模型请求用的槽位。"""
    asked = [r for r in inference.llm.requests if question in json.dumps(r, ensure_ascii=False)]
    assert asked, f"实时模型没有收到「{question}」"
    return asked[-1]["id_slot"]


async def test_two_meetings_run_side_by_side(start_app, meeting_factory, inference):
    app = await start_app(PARALLEL)
    async with app.http() as http:
        meetings = meeting_factory(http)
        first = meetings()
        a = await first.connect()
        second = meetings()
        b = await second.connect()
        assert a["id"] != b["id"]

        listed = {s["id"]: s["state"] for s in (await http.get("/api/sessions")).json()["items"]}
        assert listed[a["id"]] == listed[b["id"]] == "live"
        metrics = (await http.get("/metrics")).json()
        assert metrics["live_connections"] == 2

        # 发言只进自己那一场
        inference.asr.say("第一场先看实验结果")
        await first.speak()
        assert (await first.next_message("utterance"))["text"] == "第一场先看实验结果"
        inference.asr.say("第二场讨论论文提纲")
        await second.speak()
        assert (await second.next_message("utterance"))["text"] == "第二场讨论论文提纲"

        async def texts(session_id: str) -> list[str]:
            items = (await http.get("/api/utterances", params={"session_id": session_id})).json()
            return [u["text"] for u in items["items"] if u["source"] == "asr"]

        assert await texts(a["id"]) == ["第一场先看实验结果"]
        assert await texts(b["id"]) == ["第二场讨论论文提纲"]
        assert all(u["text"] != "第二场讨论论文提纲" for u in first.of_type("utterance"))

        # 两场各自向助理提问：回答只到提问的那一场，实时模型各用一个槽位
        inference.llm.when("第二场的问题", Reply(text="这是给第二场的回答。"))
        second.type_text("第二场的问题")
        await until_sync(lambda: "给第二场" in second.bot_text(), "第二场收到回答")
        inference.llm.when("第一场的问题", Reply(text="这是给第一场的回答。"))
        first.type_text("第一场的问题")
        await until_sync(lambda: "给第一场" in first.bot_text(), "第一场收到回答")
        assert "给第二场" not in first.bot_text() and "给第一场" not in second.bot_text()
        assert {_slot_of(inference, "第一场的问题"), _slot_of(inference, "第二场的问题")} == {0, 1}

        # 截图带上会议编号，归到那一场；同时有两场时不带编号就不知道是哪一场
        uploaded = await http.post(
            "/api/frames",
            data={"captured_at": str(time.time()), "session_id": b["id"]},
            files={"image": ("screen.jpg", _screenshot(), "image/jpeg")},
        )
        assert uploaded.status_code == 200, uploaded.text
        await second.next_message("frame")
        frames_b = (await http.get("/api/frames", params={"session_id": b["id"]})).json()
        frames_a = (await http.get("/api/frames", params={"session_id": a["id"]})).json()
        assert len(frames_b["items"]) == 1 and frames_a["items"] == []
        unaddressed = await http.post(
            "/api/frames",
            data={"captured_at": str(time.time())},
            files={"image": ("screen.jpg", _screenshot(), "image/jpeg")},
        )
        assert unaddressed.status_code == 400

        # 位置满了：新会议在协商之前就被拒绝，带着原因
        refused = await http.post("/api/offer", json={"sdp": "x", "type": "offer"})
        assert refused.status_code == 409
        assert refused.json()["code"] == "session.capacity"
        assert refused.json()["params"] == {"max": 2}

        # 同一场会议换设备继续照样可以，只顶替这一场自己的旧连接
        replacement = meetings()
        again = await replacement.connect(session_id=a["id"])
        assert again["id"] == a["id"]
        assert (await first.next_message("session_closed"))["reason"] == "taken_over"
        assert not second.of_type("session_closed")

        # 结束一场就空出位置，可以开新会议
        assert (await http.post(f"/api/sessions/{b['id']}/end")).status_code == 200
        assert (await second.next_message("session_closed"))["reason"] == "ended"
        third = meetings()
        c = await third.connect()
        assert c["id"] not in (a["id"], b["id"])

        async def live_ids():
            items = (await http.get("/api/sessions")).json()["items"]
            ids = {s["id"] for s in items if s["state"] == "live"}
            return ids if ids == {a["id"], c["id"]} else None

        await until(live_ids, "进行中的是第一场和新开的一场")
