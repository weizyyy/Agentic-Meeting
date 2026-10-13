"""一场会议从头到尾：发言变成字幕、叫名字提问、打字提问、断线继续、结束、导出、换设备接管、旁观的设备打字提问。"""

from __future__ import annotations

import io
import json
import zipfile

from .inference import Reply
from .waiting import until, until_sync


async def test_speech_is_transcribed_stored_and_exported(app, http, meeting, inference):
    client = meeting()
    session = await client.connect()
    assert session["resumed"] is False and session["base_secs"] == 0
    notice = await client.next_message("notice", level="info")
    assert notice["code"] == "recording.active" and notice["params"] == {}
    assert "正在转录" in notice["text"]  # 认不出代码的页面照样能显示

    inference.asr.say("我们先看一下上周的实验结果")
    await client.speak()
    utterance = await client.next_message("utterance")
    assert utterance["text"] == "我们先看一下上周的实验结果"
    assert utterance["source"] == "asr"
    # 说的过程中先有实时字幕，同一行定稿后才落库
    captions = [c for c in client.of_type("caption") if c["segment_id"] == utterance["segment_id"]]
    assert captions and captions[-1]["stable"] + captions[-1]["unstable"]

    stored = (await http.get("/api/utterances", params={"session_id": session["id"]})).json()
    assert [u["text"] for u in stored["items"]] == ["我们先看一下上周的实验结果"]
    current = (await http.get("/api/session")).json()
    assert current["id"] == session["id"] and current["state"] == "live"

    ended = await http.post(f"/api/sessions/{session['id']}/end")
    assert ended.status_code == 200
    closed = await client.next_message("session_closed")
    assert closed["reason"] == "ended"
    detail = (await http.get(f"/api/sessions/{session['id']}")).json()
    assert detail["state"] == "ended" and detail["utterance_count"] == 1

    markdown = await http.get(f"/api/export/{session['id']}.md")
    assert markdown.status_code == 200 and "我们先看一下上周的实验结果" in markdown.text
    exported = (await http.get(f"/api/export/{session['id']}.json")).json()
    assert [u["text"] for u in exported["utterances"]] == ["我们先看一下上周的实验结果"]
    archive = await http.get(f"/api/export/{session['id']}.zip")
    with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
        assert z.namelist()[:2] == ["transcript.md", "session.json"]
        assert "我们先看一下上周的实验结果" in z.read("transcript.md").decode("utf-8")


async def test_a_dropped_connection_can_be_resumed_on_the_same_timeline(
    app, http, meeting, inference
):
    first = meeting()
    session = await first.connect()
    inference.asr.say("第一段发言")
    await first.speak()
    before = await first.next_message("utterance")
    await first.close()

    async def interrupted():
        detail = (await http.get(f"/api/sessions/{session['id']}")).json()
        return detail["state"] == "interrupted"

    await until(interrupted, "会议变成已中断")

    second = meeting()
    resumed = await second.connect(session_id=session["id"])
    assert resumed["id"] == session["id"] and resumed["resumed"] is True
    assert resumed["base_secs"] >= before["t_end"]
    inference.asr.say("继续之后的第二段")
    await second.speak()
    after = await second.next_message("utterance")
    assert after["t_start"] > before["t_end"]
    # 继续之后实时模型带着之前的发言：重建的上下文里有第一段
    inference.llm.when("Jarvis", Reply(text="记得。"))
    second.type_text("Jarvis 刚才第一段说了什么")
    await until_sync(lambda: "记得" in second.bot_text(), "收到回答")
    request = inference.llm.requests[-1]
    assert "第一段发言" in json.dumps(request["messages"], ensure_ascii=False)

    texts = [u["text"] for u in (await http.get("/api/utterances")).json()["items"]]
    assert texts[:1] == ["第一段发言"] and "继续之后的第二段" in texts
    detail = (await http.get(f"/api/sessions/{session['id']}")).json()
    assert len(detail["connections"]) == 2

    unknown = await http.post(
        "/api/offer", json={"sdp": "x", "type": "offer", "requestData": {"session_id": "nope"}}
    )
    assert unknown.status_code == 404 and unknown.json()["code"] == "session.not_found"


async def test_calling_the_assistant_by_name_gets_a_spoken_answer(app, http, meeting, inference):
    client = meeting()
    session = await client.connect()
    inference.asr.say("Jarvis，上周定的下一步是什么")
    inference.llm.when("上周定的下一步", Reply(text="下一步是补消融实验。"))
    await client.speak()

    await client.next_message("assistant_state", state="listening")
    await until_sync(lambda: "补消融实验" in client.bot_text(), "收到回答文字")
    await until_sync(lambda: client.bot_audio_frames > 10, "收到回答的声音")
    assert inference.tts.requests and "补消融实验" in inference.tts.requests[0]["input"]

    async def answer_stored():
        items = (await http.get("/api/utterances", params={"session_id": session["id"]})).json()
        return [u for u in items["items"] if u["speaker_idx"] == -1]

    stored = await until(answer_stored, "回答记进字幕")
    assert stored[0]["text"] == "下一步是补消融实验。"


async def test_typed_question_is_answered_in_text_only(app, http, meeting, inference):
    client = meeting()
    await client.connect()
    inference.llm.when("实验用了几张卡", Reply(text="用了八张卡。"))
    client.type_text("实验用了几张卡")

    typed = await client.next_message("utterance", source="text")
    assert typed["speaker_idx"] == -2 and typed["text"] == "实验用了几张卡"
    await until_sync(lambda: "八张卡" in client.bot_text(), "收到回答文字")
    assert inference.tts.count() == 0  # 打字问的只用文字回答

    client.type_text("   ")
    warning = await client.next_message("notice", level="warn")
    assert warning["code"] == "text.empty" and warning["text"]


async def test_a_second_device_takes_over_the_meeting(app, http, meeting, inference):
    first = meeting()
    session = await first.connect()
    second = meeting()
    taken = await second.connect(session_id=session["id"])
    closed = await first.next_message("session_closed")
    assert closed["reason"] == "taken_over"
    assert taken["id"] == session["id"]

    inference.asr.say("换到第二台设备上说话")
    await second.speak()
    assert (await second.next_message("utterance"))["text"] == "换到第二台设备上说话"


async def test_a_device_following_the_meeting_can_ask_by_typing(app, http, meeting, inference):
    """手机只读旁观时打字提问：不接管会议，回答只出文字，记进字幕。"""
    client = meeting()
    session = await client.connect()
    inference.llm.when("报告什么时候交", Reply(text="下周五之前交。"))

    sent = await http.post(f"/api/sessions/{session['id']}/text", json={"text": "报告什么时候交"})
    assert sent.status_code == 200, sent.text
    assert sent.json() == {"accepted": True, "notice": None}
    # 会议页面照常看到这条提问和文字回答，连接没有被接管
    typed = await client.next_message("utterance", source="text")
    assert typed["text"] == "报告什么时候交"
    await until_sync(lambda: "下周五" in client.bot_text(), "收到回答文字")
    assert inference.tts.count() == 0

    async def answer_stored():
        items = (
            await http.get("/api/utterances", params={"session_id": session["id"], "tail": 10})
        ).json()["items"]
        return [u for u in items if u["text"] == "下周五之前交。"] or None

    await until(answer_stored, "回答记进字幕，旁观的设备轮询读到")

    # 不合格的提问由响应告诉提问的设备
    empty = await http.post(f"/api/sessions/{session['id']}/text", json={"text": "  "})
    assert empty.json()["accepted"] is False
    assert empty.json()["notice"]["code"] == "text.empty"

    await http.post(f"/api/sessions/{session['id']}/end")
    ended = await http.post(f"/api/sessions/{session['id']}/text", json={"text": "还在吗"})
    assert ended.status_code == 409
    assert ended.json()["code"] == "session.not_live"
