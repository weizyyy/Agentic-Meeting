"""助理的能力：查会议记录、看共享屏幕、把活交给后台 agent。"""

from __future__ import annotations

import io
import json
import time

import pytest
from PIL import Image

from .inference import Reply
from .waiting import until_sync


async def test_assistant_recalls_what_was_said_earlier(app, http, meeting, inference):
    client = meeting()
    await client.connect()
    inference.asr.say("消融实验还需要补两组")
    await client.speak()
    await client.next_message("utterance")

    inference.llm.when("谁说过消融", Reply(tool="recall", arguments={"query": "消融实验"}))
    client.type_text("Jarvis 刚才谁说过消融实验")
    await until_sync(lambda: "已经处理" in client.bot_text(), "收到回答")

    tool_results = [m for m in inference.llm.requests[-1]["messages"] if m.get("role") == "tool"]
    assert tool_results, "模型应当拿到工具结果"
    result = json.loads(tool_results[-1]["content"])
    assert [item["text"] for item in result["items"]] == ["消融实验还需要补两组"]


async def test_shared_screen_is_summarized_and_shown_to_the_assistant(
    app, http, meeting, inference
):
    client = meeting()
    session = await client.connect()
    inference.llm.when("屏幕截图", Reply(text="一张折线图，横轴是训练步数。"), sticky=True)

    uploaded = await _upload_frame(http, "#3366cc")
    assert uploaded.status_code == 200, uploaded.text
    frame = uploaded.json()
    announced = await client.next_message("frame", id=frame["id"])
    assert announced["width"] == 320
    captioned = await client.next_message("frame_caption", id=frame["id"])
    assert "折线图" in captioned["caption"]

    listed = (await http.get("/api/frames", params={"session_id": session["id"]})).json()
    assert [(f["id"], f["caption_status"]) for f in listed["items"]] == [(frame["id"], "done")]
    image = await http.get(f"/api/frames/{frame['id']}/image")
    assert image.status_code == 200 and image.headers["content-type"].startswith("image/")

    # 画面摘要进了实时模型的上下文：下一次提问时模型能看到
    inference.llm.rules.clear()
    client.type_text("Jarvis 屏幕上是什么")
    await until_sync(lambda: client.bot_text(), "收到回答")
    context = json.dumps(inference.llm.requests[-1]["messages"], ensure_ascii=False)
    assert "折线图" in context

    not_an_image = await http.post(
        "/api/frames",
        data={"captured_at": str(time.time())},
        files={"image": ("x.jpg", b"not an image", "image/jpeg")},
    )
    assert not_an_image.status_code == 400 and not_an_image.json()["code"] == "frame.invalid"


async def test_screenshots_need_a_live_meeting(app, http):
    response = await _upload_frame(http, "#000000")
    assert response.status_code == 404 and response.json()["code"] == "frames.no_live_session"


@pytest.mark.parametrize("modality", ["text", "voice"])
async def test_delegated_task_runs_in_the_background_and_reports_back(
    app, http, meeting, inference, modality
):
    client = meeting()
    session = await client.connect()
    inference.asr.say("基线的学习率是千分之三")
    await client.speak()
    await client.next_message("utterance")

    inference.llm.when(
        "整理一下",
        Reply(
            tool="delegate_task", arguments={"goal": "整理基线的超参数", "minutes_of_context": 5}
        ),
    )
    inference.agent.when(
        "整理基线的超参数",
        Reply(
            text=json.dumps(
                {"brief": "学习率千分之三", "detail_md": "## 超参数\n- 学习率 0.003"},
                ensure_ascii=False,
            )
        ),
    )
    request = "Jarvis 把基线的超参数交给后台整理一下"
    if modality == "voice":
        inference.asr.say(request)
        await client.speak()
    else:
        client.type_text(request)

    queued = await client.next_message("task", status="queued")
    done = await client.next_message("task", status="succeeded", timeout_secs=60)
    assert queued["id"] == done["id"] and queued["modality"] == modality
    assert done["brief"] == "学习率千分之三" and done["modality"] == modality
    # 交给后台模型的输入里带着会议原文
    sent = json.dumps(inference.agent.requests[0]["messages"], ensure_ascii=False)
    assert "基线的学习率是千分之三" in sent and "整理基线的超参数" in sent

    listed = (await http.get("/api/tasks", params={"session_id": session["id"]})).json()
    assert [t["status"] for t in listed["items"]] == ["succeeded"]
    detail = (await http.get(f"/api/tasks/{done['id']}")).json()
    assert "学习率 0.003" in detail["detail_md"]
    assert detail["outbound"]["goal"] == "整理基线的超参数"
    assert detail["events"]
    # 结果回到实时模型，助理据此回答
    await until_sync(
        lambda: any(
            "[任务 t1 完成] 学习率千分之三" in json.dumps(r["messages"], ensure_ascii=False)
            for r in inference.llm.requests
        ),
        "任务结果交回实时模型",
    )
    await until_sync(lambda: "已经处理" in client.bot_text(), "收到任务汇报")
    if modality == "voice":
        await until_sync(lambda: client.bot_audio_frames > 10, "收到语音汇报")
    else:
        assert inference.tts.count() == 0


async def test_a_failing_task_is_reported_as_failed(app, http, meeting, inference):
    client = meeting()
    session = await client.connect()
    inference.agent.down = True
    inference.llm.when("查一下", Reply(tool="delegate_task", arguments={"goal": "查文献"}))
    client.type_text("Jarvis 帮我查一下文献")

    failed = await client.next_message("task", status="failed", timeout_secs=60)
    # 失败原因带代码和参数，页面按界面语言显示；error 是给实时模型的中文
    assert failed["error_code"] == "agent.http_error", failed
    assert failed["error_params"] == {"status": 503}
    assert failed["error"] == "远端模型返回了错误（503）"
    listed = (await http.get("/api/tasks", params={"session_id": session["id"]})).json()
    assert listed["items"][0]["error_code"] == "agent.http_error"
    assert (await client.next_message("utterance", source="text"))["text"]


async def _upload_frame(http, color: str):
    buffer = io.BytesIO()
    Image.new("RGB", (320, 180), color).save(buffer, format="JPEG")
    return await http.post(
        "/api/frames",
        data={"captured_at": str(time.time())},
        files={"image": ("screen.jpg", buffer.getvalue(), "image/jpeg")},
    )
