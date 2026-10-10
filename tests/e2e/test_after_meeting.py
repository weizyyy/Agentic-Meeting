"""会后：报告、保留、删除，以及按保留期限自动清理。"""

from __future__ import annotations

import io
import sqlite3
import time

from PIL import Image

from .waiting import until

THREE_DAYS = 3 * 86400


async def _meeting_with_content(http, meeting, inference, line: str) -> dict:
    """开一场会：说一句话、共享一张屏幕，然后结束。返回会议的 ``session`` 消息。"""
    client = meeting()
    session = await client.connect()
    inference.asr.say(line)
    await client.speak()
    await client.next_message("utterance")
    buffer = io.BytesIO()
    Image.new("RGB", (320, 180), "#228844").save(buffer, format="JPEG")
    frame = await http.post(
        "/api/frames",
        data={"captured_at": str(time.time())},
        files={"image": ("screen.jpg", buffer.getvalue(), "image/jpeg")},
    )
    assert frame.status_code == 200, frame.text
    assert (await http.post(f"/api/sessions/{session['id']}/end")).status_code == 200
    await client.close()
    return session


async def test_report_is_generated_after_the_meeting(app, http, meeting, inference):
    from .inference import Reply

    inference.llm.when("这周把消融实验做完", Reply(text="## 结论\n- 这周做完消融实验"), sticky=True)
    client = meeting()
    session = await client.connect()
    inference.asr.say("这周把消融实验做完")
    await client.speak()
    await client.next_message("utterance")
    live = await http.post(f"/api/sessions/{session['id']}/report")
    assert live.status_code == 409  # 会议还在开

    await http.post(f"/api/sessions/{session['id']}/end")
    started = await http.post(f"/api/sessions/{session['id']}/report")
    assert started.status_code == 202, started.text

    async def finished():
        report = (await http.get(f"/api/sessions/{session['id']}/report")).json()
        return report if report["status"] != "running" else None

    report = await until(finished, "报告生成完", timeout_secs=60)
    assert report["status"] == "done", report
    assert "这周做完消融实验" in report["text_md"]
    download = await http.get(f"/api/sessions/{session['id']}/report.md")
    assert download.status_code == 200 and "这周做完消融实验" in download.text
    exported = (await http.get(f"/api/export/{session['id']}.json")).json()
    assert "这周做完消融实验" in exported["report"]["text_md"]


async def test_deleting_a_meeting_removes_its_records_and_files(app, http, meeting, inference):
    kept = await _meeting_with_content(http, meeting, inference, "另一场会议的发言")
    session = await _meeting_with_content(http, meeting, inference, "要删除的会议里的发言")
    folder = app.data_dir / "sessions" / session["id"]
    assert any(folder.rglob("*.jpg"))

    # 刚结束的会议还在收尾（滚动纪要、画面摘要）时删除返回 409，页面会让用户再试
    async def delete():
        response = await http.delete(f"/api/sessions/{session['id']}")
        assert response.status_code in (200, 409), response.text
        return response.status_code == 200

    await until(delete, "删除会议")
    assert (await http.get(f"/api/sessions/{session['id']}")).status_code == 404
    assert (await http.get(f"/api/export/{session['id']}.md")).status_code == 404
    assert not folder.exists()
    assert (await http.delete(f"/api/sessions/{session['id']}")).status_code == 404

    # 别的会议不受影响
    remaining = (await http.get("/api/sessions")).json()["items"]
    assert [s["id"] for s in remaining] == [kept["id"]]
    texts = [
        u["text"] for u in (await http.get(f"/api/export/{kept['id']}.json")).json()["utterances"]
    ]
    assert texts == ["另一场会议的发言"]


async def test_expired_content_is_cleaned_up_unless_the_meeting_is_kept(
    start_app, inference, meeting_factory
):
    first = await start_app()
    async with first.http() as http:
        meeting = meeting_factory(http)
        old = await _meeting_with_content(http, meeting, inference, "三天前的发言")
        kept = await _meeting_with_content(http, meeting, inference, "要一直留着的发言")
        response = await http.patch(f"/api/sessions/{kept['id']}", json={"keep": True})
        assert response.status_code == 200 and response.json()["keep"] is True
        await meeting.close_all()
    await first.shutdown()

    # 把两场会议都挪到三天前，再以「字幕和截图保留一天」重新启动
    with sqlite3.connect(first.data_dir / "meetings.db") as db:
        db.execute(
            "UPDATE sessions SET started_at = started_at - ?, last_active_at = last_active_at - ?,"
            " ended_at = ended_at - ?",
            (THREE_DAYS, THREE_DAYS, THREE_DAYS),
        )
    second = await start_app(
        {"retention.transcript_days": 1, "retention.screenshots_days": 1},
        data_dir=first.data_dir,
    )
    async with second.http() as http:

        async def cleaned():
            detail = (await http.get(f"/api/sessions/{old['id']}")).json()
            return detail["utterance_count"] == 0

        await until(cleaned, "过期的字幕被清理")
        assert (await http.get("/api/frames", params={"session_id": old["id"]})).json()[
            "items"
        ] == []
        assert not list((second.data_dir / "sessions" / old["id"]).rglob("*.jpg"))
        # 会议本身和标题等元数据还在
        assert (await http.get(f"/api/sessions/{old['id']}")).status_code == 200

        kept_detail = (await http.get(f"/api/sessions/{kept['id']}")).json()
        assert kept_detail["utterance_count"] == 1
        frames = (await http.get("/api/frames", params={"session_id": kept["id"]})).json()
        assert len(frames["items"]) == 1
