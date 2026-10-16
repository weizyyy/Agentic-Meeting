"""真实 HTTP/WebRTC 应用的旁听事件、恢复与慢消费者场景。"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading

import httpx
import pytest

from agentic_meeting.config import AppConfig
from agentic_meeting.store.db import Store
from agentic_meeting.types import TaskResult, Utterance
from agentic_meeting.web.app import create_app, make_server

from . import inference as fake_protocol
from .conftest import app_config
from .inference import Reply
from .meeting_client import MeetingClient
from .test_assistant import _upload_frame
from .waiting import until, until_sync


class Watcher:
    def __init__(self, http, session_id, cursor=None):
        self.http, self.session_id, self.cursor = http, session_id, cursor
        self.events = []
        self.task = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self.read())
        return self

    async def __aexit__(self, *_):
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass

    async def read(self):
        while True:
            params = {"after": self.cursor} if self.cursor else {}
            async with self.http.stream(
                "GET", f"/api/sessions/{self.session_id}/events", params=params
            ) as response:
                response.raise_for_status()
                assert response.headers["content-type"].startswith("text/event-stream")
                kind, identifier = None, None
                async for line in response.aiter_lines():
                    if line.startswith("event: "):
                        kind = line[7:]
                    elif line.startswith("id: "):
                        identifier = line[4:]
                    elif line.startswith("data: "):
                        payload = json.loads(line[6:])
                        self.events.append((kind, payload))
                        if kind == "reset":
                            self.cursor = None
                        elif identifier:
                            self.cursor = identifier
                        if kind == "meeting" and payload["data"]["type"] == "session_deleted":
                            return

    async def find(self, kind="meeting", **fields):
        def found():
            if self.task.done():
                self.task.result()
            for event_kind, event in self.events:
                data = event["data"] if event_kind in {"meeting", "snapshot"} else event
                if event_kind == kind and all(data.get(k) == v for k, v in fields.items()):
                    return event
            return None

        return await until_sync(found, f"SSE {kind} {fields}", timeout_secs=60)


async def test_typed_notices_stay_with_rtc_or_http_requester(http, meeting, inference, monkeypatch):
    recorder = meeting()
    sid = (await recorder.connect())["id"]
    entered, release = threading.Event(), threading.Event()
    stream = fake_protocol._stream

    async def held_stream(reply):
        if reply.text == "受控排队回答":
            entered.set()
            assert await asyncio.to_thread(release.wait, 20), "测试必须释放假模型回答"
        async for chunk in stream(reply):
            yield chunk

    # 只暂停外部假模型的 HTTP 响应；队列、RTC 校验与 SSE 均走真实应用。
    monkeypatch.setattr(fake_protocol, "_stream", held_stream)
    try:
        async with Watcher(http, sid) as first, Watcher(http, sid) as second:
            await first.find("snapshot")
            await second.find("snapshot")
            for text, code in ((" ", "text.empty"), ("x" * 2001, "text.too_long")):
                recorder.type_text(text)
                await recorder.next_message("notice", code=code)
            recorder.send_client_message("text_input", {"unexpected": "field"})
            await recorder.next_message("notice", code="text.malformed")

            inference.llm.when("受控排队请求", Reply(text="受控排队回答"))
            recorder.type_text("受控排队请求")
            await until_sync(entered.is_set, "真实模型请求到达暂停边界")
            recorder.type_text("RTC 排队1")
            await recorder.next_message("notice", code="text.queued", params={"position": 1})
            response = await http.post(f"/api/sessions/{sid}/text", json={"text": "HTTP 排队2"})
            assert response.status_code == 200
            assert response.json()["accepted"] is True
            assert response.json()["notice"]["code"] == "text.queued"
            assert response.json()["notice"]["params"] == {"position": 2}
            for position in range(3, 6):
                recorder.type_text(f"RTC 排队{position}")
                await recorder.next_message(
                    "notice", code="text.queued", params={"position": position}
                )
            recorder.type_text("RTC 队满拒绝")
            await recorder.next_message("notice", code="text.queue_full", params={"max": 5})
            private_before_http = len(
                [n for n in recorder.of_type("notice") if n["code"].startswith("text.")]
            )
            assert private_before_http == 8  # 三个校验、四个 RTC 排队和一个队满提示。
            for text, code in (
                (" ", "text.empty"),
                ("x" * 2001, "text.too_long"),
                ("HTTP 队满拒绝", "text.queue_full"),
            ):
                response = await http.post(f"/api/sessions/{sid}/text", json={"text": text})
                assert response.status_code == 200
                assert response.json()["accepted"] is False
                assert response.json()["notice"]["code"] == code
            malformed = await http.post(f"/api/sessions/{sid}/text", json={"unexpected": "field"})
            assert malformed.status_code == 400

            # 主持已收到 RTC 提示；持久事件作有序屏障，不靠睡眠证明没有旁听广播。
            changed = await http.patch(f"/api/sessions/{sid}", json={"title": "请求者通知屏障"})
            assert changed.status_code == 200
            for watcher in (first, second):
                await watcher.find(type="session_updated", title="请求者通知屏障")
                assert not any(
                    kind == "meeting" and event["data"].get("code", "").startswith("text.")
                    for kind, event in watcher.events
                )
                await watcher.find(type="utterance", text="HTTP 排队2")
            assert private_before_http == len(
                [n for n in recorder.of_type("notice") if n["code"].startswith("text.")]
            )
            release.set()
    finally:
        release.set()
        assert (await http.post(f"/api/sessions/{sid}/end")).status_code == 200
        await recorder.close()


async def test_two_watchers_receive_all_sources_without_taking_recording(
    app, http, meeting, inference
):
    recorder = meeting()
    session = await recorder.connect()
    sid = session["id"]
    async with Watcher(http, sid) as first, Watcher(http, sid) as second:
        await first.find("snapshot")
        await second.find("snapshot")
        inference.asr.say("Jarvis，请讲一下合成实验的结论")
        inference.llm.when("合成实验", Reply(text="实验结果可以复现。"))
        await recorder.speak()
        utterance = await recorder.next_message("utterance", source="asr")
        for watcher in (first, second):
            await watcher.find(type="caption")
            await watcher.find(type="utterance", id=utterance["id"])
            await watcher.find(type="assistant_state", state="listening")
            await watcher.find(type="assistant_answer", phase="text")
            await watcher.find(type="assistant_answer", phase="ended", text="实验结果可以复现。")
            await watcher.find(type="assistant_speaking", speaking=True)
            await watcher.find(type="assistant_speaking", speaking=False)

        changed = await http.post(
            "/api/utterances/speaker",
            json={"session_id": sid, "ids": [utterance["id"]], "new_speaker": "成员甲"},
        )
        assert changed.status_code == 200, changed.text
        idx = changed.json()["speaker"]["idx"]
        renamed = await http.put(
            f"/api/speakers/{idx}", json={"session_id": sid, "display_name": "新名字"}
        )
        assert renamed.status_code == 200
        inference.llm.when("整理参数", Reply(tool="delegate_task", arguments={"goal": "整理参数"}))
        inference.agent.when(
            "整理参数",
            Reply(
                text=json.dumps(
                    {"brief": "参数整理完成", "detail_md": "合成参数"}, ensure_ascii=False
                )
            ),
        )
        sent = await http.post(f"/api/sessions/{sid}/text", json={"text": "整理参数"})
        assert sent.status_code == 200
        inference.llm.when("屏幕截图", Reply(text="合成折线图。"), sticky=True)
        uploaded = await _upload_frame(http, "#3366cc")
        assert uploaded.status_code == 200
        for watcher in (first, second):
            await watcher.find(type="utterance_update", id=utterance["id"])
            await watcher.find(type="speaker", idx=idx, display_name="新名字")
            await watcher.find(type="task", status="queued")
            progress = await watcher.find(type="task_event")
            assert isinstance(progress["data"]["id"], int)
            await watcher.find(type="task", status="succeeded")
            await watcher.find(type="frame", id=uploaded.json()["id"])
            await watcher.find(type="frame_caption", id=uploaded.json()["id"])
            await watcher.find(
                type="frame_caption_status", id=uploaded.json()["id"], caption_status="done"
            )
        inference.llm.down = True
        failed_frame = await _upload_frame(http, "#ffffff")
        assert failed_frame.status_code == 200
        for watcher in (first, second):
            await watcher.find(
                type="frame_caption_status", id=failed_frame.json()["id"], caption_status="failed"
            )
        inference.llm.down = False
        assert not recorder.of_type("frame_caption_status")
        inference.asr.say("另一位成员的发言")
        await recorder.speak()
        other_utterance = await recorder.next_message("utterance", text="另一位成员的发言")
        other_speaker = await http.post(
            "/api/utterances/speaker",
            json={"session_id": sid, "ids": [other_utterance["id"]], "new_speaker": "成员乙"},
        )
        into = other_speaker.json()["speaker"]["idx"]
        merged = await http.post(
            f"/api/speakers/{idx}/merge", json={"session_id": sid, "into": into}
        )
        assert merged.status_code == 200, merged.text
        inference.agent.down = True
        inference.llm.when("失败任务", Reply(tool="delegate_task", arguments={"goal": "失败任务"}))
        await http.post(f"/api/sessions/{sid}/text", json={"text": "失败任务"})
        for watcher in (first, second):
            await watcher.find(type="speakers_merged", into=into)
            await watcher.find(type="task", status="failed")
        inference.agent.down = False
        detail = (await http.get(f"/api/sessions/{sid}")).json()
        assert detail["state"] == "live" and len(detail["connections"]) == 1
        assert not recorder.of_type("session_closed")
        assert app.log_text().count("POST /api/offer ") == 1
        ended = await http.post(f"/api/sessions/{sid}/end")
        assert ended.status_code == 200
        for watcher in (first, second):
            await watcher.find(type="session_state", state="ended")
            events = [e for kind, e in watcher.events if kind == "meeting"]
            assert all(e["session_id"] == sid for e in events)
            sequences = [e["seq"] for e in events]
            assert sequences == sorted(set(sequences))
        common = {e["seq"]: e for kind, e in first.events if kind == "meeting"}
        assert all(
            common[e["seq"]] == e
            for kind, e in second.events
            if kind == "meeting" and e["seq"] in common
        )


async def test_replay_history_expiry_restart_and_terminal_delete(start_app, inference):
    app = await start_app()
    async with app.http() as http:
        recorder = MeetingClient(http)
        try:
            sid = (await recorder.connect())["id"]
            inference.asr.say("这条历史发言需要改名字")
            await recorder.speak()
            utterance = await recorder.next_message("utterance")
            changed = await http.post(
                "/api/utterances/speaker",
                json={"session_id": sid, "ids": [utterance["id"]], "new_speaker": "旧名字"},
            )
            idx = changed.json()["speaker"]["idx"]
            async with Watcher(http, sid) as watcher:
                await watcher.find("snapshot")
                cursor = watcher.cursor
            await recorder.close()
            response = await http.put(
                f"/api/speakers/{idx}", json={"session_id": sid, "display_name": "断网后名字"}
            )
            assert response.status_code == 200
            async with Watcher(http, sid, cursor) as replay:
                event = await replay.find(type="speaker", display_name="断网后名字")
                assert not any(kind == "snapshot" for kind, _ in replay.events)
                next_cursor = f"{event['epoch']}:{event['seq']}"
            # 相同游标只读后续，不把该条事件追加第二遍。
            async with Watcher(http, sid, next_cursor) as repeated:
                await http.patch(f"/api/sessions/{sid}", json={"title": "重连标题"})
                await repeated.find(type="session_updated", title="重连标题")
                assert not any(
                    e.get("data", {}).get("type") == "speaker" for _, e in repeated.events
                )
            for i in range(260):
                result = await http.patch(f"/api/sessions/{sid}", json={"title": f"标题{i}"})
                assert result.status_code == 200
            for unavailable in (
                cursor,
                "invalid",
                f"{event['epoch']}:999999999",
                f"{event['epoch']}:" + "9" * 5000,
            ):
                async with Watcher(http, sid, unavailable) as expired:
                    await expired.find("reset", reason="cursor_unavailable")
                    snapshot = (await expired.find("snapshot"))["data"]
                    assert snapshot["session"]["title"] == "标题259"
                    assert snapshot["utterances"][0]["speaker_name"] == "断网后名字"
        finally:
            await recorder.close()
    await asyncio.to_thread(app.stop)
    restarted = await start_app(data_dir=app.data_dir)
    async with restarted.http() as http:
        async with Watcher(http, sid, cursor) as watcher:
            await watcher.find("reset", reason="cursor_unavailable")
            snapshot = await watcher.find("snapshot")
            assert snapshot["epoch"] != event["epoch"]
            assert snapshot["data"]["utterances"][0]["speaker_name"] == "断网后名字"
            deleted = await http.delete(f"/api/sessions/{sid}")
            assert deleted.status_code == 200
            await watcher.find(type="session_deleted")
            await asyncio.wait_for(watcher.task, 5)
        assert (await http.get(f"/api/sessions/{sid}/events")).status_code == 404


@pytest.fixture
async def controlled_app(inference, tmp_path):
    """真实应用加可暂停的存储/任务边界，确定性制造快照竞态与迟到结果。"""
    inference.reset()
    task_started, task_release = asyncio.Event(), asyncio.Event()

    async def runner(_task, on_event):
        task_started.set()
        await task_release.wait()
        await on_event("status", "旧会议任务完成", None)
        return TaskResult(brief="旧会议结果", detail_md="合成结果")

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    cfg = AppConfig.model_validate(app_config(inference, tmp_path / "data", port))
    application = create_app(cfg, task_runner=runner)
    server = make_server(cfg, app=application)
    running = asyncio.create_task(server.serve(sockets=[sock]))
    await until_sync(lambda: server.started, "应用启动")

    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30
        ) as http:
            yield application, http, task_started, task_release
    finally:
        task_release.set()
        server.should_exit = True
        await asyncio.wait_for(running, 25)
        sock.close()


async def test_snapshot_race_slow_queue_cleanup_and_late_task_routing(controlled_app, inference):
    application, http, task_started, task_release = controlled_app
    first, second = MeetingClient(http), MeetingClient(http)
    try:
        sid = (await first.connect())["id"]
        resources = application.state.resources
        store = resources.store
        original = store.all_utterances
        entered, release = asyncio.Event(), asyncio.Event()

        async def paused(session_id):
            rows = await original(session_id)
            entered.set()
            await release.wait()
            return rows

        store.all_utterances = paused
        async with Watcher(http, sid) as watcher:
            await asyncio.wait_for(entered.wait(), 10)
            inference.asr.say("快照期间的新字幕")
            await first.speak()
            utterance = await first.next_message("utterance")
            inference.llm.when("快照回答", Reply(text="快照期间的回答全文。"))
            await http.post(f"/api/sessions/{sid}/text", json={"text": "快照回答"})
            await until_sync(lambda: "回答全文" in first.bot_text(), "快照期间回答完成")
            inference.llm.when("旧任务", Reply(tool="delegate_task", arguments={"goal": "旧任务"}))
            await http.post(f"/api/sessions/{sid}/text", json={"text": "旧任务"})
            await asyncio.wait_for(task_started.wait(), 20)
            release.set()
            snapshot = await watcher.find("snapshot")
            final = await watcher.find(type="utterance", id=utterance["id"])
            answer = await watcher.find(type="assistant_answer", phase="ended")
            assert final["seq"] > snapshot["seq"] and answer["seq"] > snapshot["seq"]
            assert answer["data"]["text"] == "快照期间的回答全文。"
            captions = [
                e
                for kind, e in watcher.events
                if kind == "meeting" and e["data"]["type"] == "caption"
            ]
            assert captions and all(e["seq"] > snapshot["seq"] for e in captions)
            # 按契约重建：快照中旧的行加重放 upsert，不重复追加；定稿删除临时 segment。
            rows = {u["id"]: u for u in snapshot["data"]["utterances"]}
            pending = {c["segment_id"]: c for c in snapshot["data"]["transient"]["captions"]}
            for kind, event in watcher.events:
                if kind != "meeting":
                    continue
                data = event["data"]
                if data["type"] == "caption":
                    pending[data["segment_id"]] = data
                elif data["type"] == "utterance":
                    rows[data["id"]] = data
                    pending.pop(data["segment_id"], None)
            assert utterance["id"] in rows and not pending
            task = snapshot["data"]["tasks"][0]
            steps = {event["id"]: event for event in task["events"]}
            for kind, event in watcher.events:
                if kind == "meeting" and event["data"]["type"] == "task_event":
                    steps[event["data"]["id"]] = event["data"]
            detail = (await http.get(f"/api/tasks/{task['id']}")).json()
            assert set(steps) == {event["id"] for event in detail["events"]}
        store.all_utterances = original
        source = resources.sessions.watchers.for_session(sid)
        await until_sync(lambda: not source.subscribers, "断开释放订阅")

        # 快照消费者被存储边界暂停时产生超过队列上限的真实 HTTP 变更。
        entered.clear()
        release.clear()
        store.all_utterances = paused
        async with Watcher(http, sid) as slow:
            await asyncio.wait_for(entered.wait(), 10)
            for i in range(260):
                result = await http.patch(f"/api/sessions/{sid}", json={"title": f"慢端{i}"})
                assert result.status_code == 200
            assert any(s.overflow for s in source.subscribers)
            release.set()
            await slow.find("reset", reason="slow_consumer")
            await until_sync(
                lambda: any(
                    kind == "snapshot" and event["data"]["session"]["title"] == "慢端259"
                    for kind, event in slow.events
                ),
                "慢端 reset 后取得新快照",
            )
        store.all_utterances = original
        inference.asr.say("慢端断开之后录音继续")
        await first.speak()
        assert (await first.next_message("utterance", text="慢端断开之后录音继续"))["id"]
        assert not first.of_type("session_closed")
        detail = (await http.get(f"/api/sessions/{sid}")).json()
        assert detail["state"] == "live" and len(detail["connections"]) == 1

        other = (await second.connect())["id"]
        async with Watcher(http, sid) as old, Watcher(http, other, watcher.cursor) as current:
            await old.find("snapshot")
            await current.find("reset", reason="cursor_unavailable")
            await current.find("snapshot")
            task_release.set()
            complete = await old.find(type="task", status="succeeded")
            assert complete["session_id"] == sid
            await http.patch(f"/api/sessions/{other}", json={"title": "新会议"})
            await current.find(type="session_updated", title="新会议")
            assert all(e["session_id"] == other for kind, e in current.events if kind == "meeting")
            assert not any(
                e["data"].get("brief") == "旧会议结果"
                for kind, e in current.events
                if kind == "meeting"
            )
    finally:
        await application.state.resources.sessions.shutdown()
        await first.close()
        await second.close()


async def test_event_header_heartbeat_and_cursor_resume(app, http, meeting):
    recorder = meeting()
    sid = (await recorder.connect())["id"]
    await recorder.close()

    async def interrupted():
        return (await http.get(f"/api/sessions/{sid}")).json()["state"] == "interrupted"

    await until(interrupted, "录音已断开")
    async with Watcher(http, sid) as first:
        await first.find("snapshot")
        cursor = first.cursor
    async with http.stream(
        "GET",
        f"/api/sessions/{sid}/events",
        params={"after": "invalid"},
        headers={"Last-Event-ID": cursor},
    ) as response:
        assert response.status_code == 200
        changed = await http.patch(f"/api/sessions/{sid}", json={"title": "请求头优先"})
        assert changed.status_code == 200
        heartbeat, received = False, None
        async with asyncio.timeout(20):
            async for line in response.aiter_lines():
                assert line not in {"event: reset", "event: snapshot"}
                if line == ": heartbeat":
                    heartbeat = True
                elif line.startswith("data: "):
                    received = json.loads(line[6:])
                    assert received["data"]["type"] == "session_updated"
                if heartbeat and received is not None:
                    break
    assert received["data"]["title"] == "请求头优先"
    cursor = f"{received['epoch']}:{received['seq']}"
    async with Watcher(http, sid, cursor) as resumed:
        await http.patch(f"/api/sessions/{sid}", json={"keep": True})
        await resumed.find(type="session_updated", keep=True)
        assert not any(kind == "snapshot" for kind, _ in resumed.events)


async def test_a_watching_device_stays_listed_while_its_stream_is_healthy(http, meeting):
    """事件流不定期重建：主持人看到的旁观设备和「几点起」一直不变。"""
    recorder = meeting()
    sid = (await recorder.connect())["id"]
    async with Watcher(http, sid) as watcher:
        await watcher.find("snapshot")
        listed = await recorder.wait_for(
            lambda: next((m for m in recorder.of_type("watchers") if len(m["items"]) == 1), None),
            "主持人看到一台旁观设备",
        )
        await asyncio.sleep(20)  # 超过单次发送期限，健康的流仍不断开
        assert [kind for kind, _ in watcher.events].count("snapshot") == 1
        assert recorder.of_type("watchers")[-1]["items"] == listed["items"]
        await http.patch(f"/api/sessions/{sid}", json={"title": "一直在看"})
        await watcher.find(type="session_updated", title="一直在看")
    await recorder.close()


async def test_disabled_captioning_emits_skipped_only_to_observers(start_app):
    app = await start_app({"screen.caption": False})
    async with app.http() as http:
        recorder = MeetingClient(http)
        try:
            sid = (await recorder.connect())["id"]
            async with Watcher(http, sid) as watcher:
                await watcher.find("snapshot")
                uploaded = await _upload_frame(http, "#6633cc")
                assert uploaded.status_code == 200
                await watcher.find(
                    type="frame_caption_status", id=uploaded.json()["id"], caption_status="skipped"
                )
                frames = (await http.get("/api/frames", params={"session_id": sid})).json()
                assert frames["items"][0]["caption_status"] == "skipped"
                assert not recorder.of_type("frame_caption_status")
            # 场景断言已完成；先结束媒体所有者，再收尾合成客户端。
            assert (await http.post(f"/api/sessions/{sid}/end")).status_code == 200
        finally:
            await recorder.close()


async def test_retracted_asr_captions_are_removed_from_new_snapshots(
    start_app, inference, monkeypatch
):
    app = await start_app({"asr.unfixed_tokens": 100})
    answer = inference.asr._answer

    def retract_final(body):
        response = answer(body)
        if body.get("max_tokens") == 32:
            response["choices"][0]["message"]["content"] = body["messages"][-1]["content"]
        return response

    # 仅脚本化外部假 ASR HTTP 的最终响应；真实识别、融合器及 recorder 产生撤回。
    monkeypatch.setattr(inference.asr, "_answer", retract_final)
    async with app.http() as http:
        recorder = MeetingClient(http)
        try:
            sid = (await recorder.connect())["id"]
            async with Watcher(http, sid) as watcher:
                await watcher.find("snapshot")
                for index in range(3):
                    inference.asr.say("暂时识别内容")
                    await recorder.speak()

                    def cleared():
                        return [
                            event["data"]
                            for kind, event in watcher.events
                            if kind == "meeting"
                            and event["data"]["type"] == "caption"
                            and not event["data"]["stable"]
                            and not event["data"]["unstable"]
                        ]

                    await until_sync(
                        lambda index=index: len(cleared()) >= index + 1,
                        "ASR 撤回临时字幕",
                        timeout_secs=5,
                    )
                    segment = cleared()[-1]["segment_id"]
                    assert any(
                        kind == "meeting"
                        and event["data"].get("segment_id") == segment
                        and event["data"].get("unstable")
                        for kind, event in watcher.events
                    )
                    async with Watcher(http, sid) as fresh:
                        snapshot = (await fresh.find("snapshot"))["data"]
                        assert snapshot["transient"]["captions"] == []
                        assert snapshot["utterances"] == []
                assert len({caption["segment_id"] for caption in cleared()}) == 3
                monkeypatch.setattr(inference.asr, "_answer", answer)
                inference.asr.say("正常稳定终稿")
                await recorder.speak()
                utterance = await recorder.next_message("utterance", text="正常稳定终稿")
                await watcher.find(type="utterance", id=utterance["id"])
                async with Watcher(http, sid) as fresh:
                    snapshot = (await fresh.find("snapshot"))["data"]
                    assert snapshot["transient"]["captions"] == []
                    assert [u["text"] for u in snapshot["utterances"]] == ["正常稳定终稿"]
            # 三轮撤回及恢复均已验证；不把合成媒体的同时关闭竞态混入断言。
            assert (await http.post(f"/api/sessions/{sid}/end")).status_code == 200
        finally:
            await recorder.close()


@pytest.mark.parametrize("revocation", [False, True])
async def test_real_tcp_backpressure_bounds_request_and_server_exit(
    inference, tmp_path, caplog, monkeypatch, revocation
):
    """公开 socket 缓冲 + 连续真实 HTTP 事件，不能用暂停生成器代替发送背压。"""
    inference.reset()
    store = await Store.open(tmp_path / "meetings.db", 64, assistant_name="Jarvis")
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    config = app_config(inference, tmp_path / "data", port)
    if revocation:
        from .test_meeting_access import PASSWORD, USERS, login

        monkeypatch.setenv("E2E_LOCAL_PASSWORD", PASSWORD)
        config["server"].update(meeting_access=True, local_users=USERS)
    cfg = AppConfig.model_validate(config)
    application = create_app(cfg, store=store)
    clock = asyncio.get_running_loop().time
    network = {"sends": []}

    async def observed(scope, receive, send):
        if scope["type"] != "http" or (b"user-agent", b"slow-socket") not in scope["headers"]:
            return await application(scope, receive, send)
        network["started"] = clock()

        async def measured_send(message):
            started = network["sending_since"] = clock()
            try:
                await send(message)
            finally:
                network["sends"].append(clock() - started)
                network["sending_since"] = None
                if clock() - started > 1:
                    network["blocked_since"] = started

        try:
            await application(scope, receive, measured_send)
        finally:
            network["finished"] = clock()

    # ASGI 测量包装保留真实应用的公开 state，供 MeetingServer 关闭订阅。
    observed.state = application.state
    server = make_server(cfg, app=observed)
    logger = logging.getLogger("uvicorn.error")
    logger.addHandler(caplog.handler)
    running = asyncio.create_task(server.serve(sockets=[listener]))
    writer = recorder = other_recorder = other_watcher = None
    try:
        await until_sync(lambda: server.started, "网络测试应用启动")
        async with (
            httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30
            ) as http,
            httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30
            ) as other,
            httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30
            ) as viewer,
        ):
            if revocation:
                await login(http, "alpha")
                await login(other, "beta")
                await login(viewer, "viewer")
                other_recorder = MeetingClient(other)
                other_sid = (await other_recorder.connect())["id"]
                await other_recorder.close()

                async def interrupted():
                    return (await other.get(f"/api/sessions/{other_sid}")).json()[
                        "state"
                    ] == "interrupted"

                await until(interrupted, "另一会录音已断开")
                other_watcher = Watcher(other, other_sid)
                await other_watcher.__aenter__()
                await other_watcher.find("snapshot")
            recorder = MeetingClient(http)
            sid = (await recorder.connect())["id"]
            cookie = ""
            if revocation:
                assert (
                    await http.put(
                        f"/api/sessions/{sid}/members/local:viewer", json={"role": "watcher"}
                    )
                ).status_code == 200
                cookie = (
                    "Cookie: "
                    + "; ".join(f"{key}={value}" for key, value in viewer.cookies.items())
                    + "\r\n"
                )
            sessions = application.state.resources.sessions
            live = sessions.live_for(sid)
            # 合成持久历史经正常快照读取；没有向生产事件源直接灌数据。
            for index in range(64):
                await store.add_utterance(Utterance(sid, 0, index, index + 0.1, "s" * 131072))
            raw = socket.socket()
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            raw.setblocking(False)
            await asyncio.get_running_loop().sock_connect(raw, ("127.0.0.1", port))
            reader, writer = await asyncio.open_connection(sock=raw, limit=1024)
            writer.write(
                f"GET /api/sessions/{sid}/events HTTP/1.1\r\nHost: localhost\r\nUser-Agent: slow-socket\r\n{cookie}\r\n".encode()
            )
            await writer.drain()
            assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
            await until_sync(lambda: len(network["sends"]) >= 2, "快照提交发送")

            async def produce_changes():
                network["producer_started"] = clock()
                for index in range(400):
                    response = await http.put(
                        "/api/speakers/0",
                        json={"session_id": sid, "display_name": f"合成{index}:" + "s" * 40},
                    )
                    assert response.status_code == 200, response.text
                    network["http_changes"] = index + 1
                network["producer_finished"] = clock()

            captured = asyncio.Event()
            revoked_at = 0.0

            async def observe_backpressure():
                nonlocal revoked_at, other_watcher
                while not (
                    network.get("sending_since") is not None
                    and clock() - network["sending_since"] > 1
                ):
                    assert network.get("finished") is None, network
                    await asyncio.sleep(0.1)
                network["backpressure_observed"] = clock()
                assert network.get("finished") is None, network
                captured.set()
                inference.asr.say("发送阻塞期间录音继续")
                await recorder.speak()
                await recorder.next_message("utterance", text="发送阻塞期间录音继续")
                source = sessions.watchers.for_session(sid)
                assert source.subscribers
                revoked_at = clock()
                if revocation:
                    assert (
                        await http.delete(f"/api/sessions/{sid}/members/local:viewer")
                    ).status_code == 200
                await until_sync(
                    lambda: network.get("finished") is not None and not source.subscribers,
                    "发送期限释放 ASGI 请求及订阅",
                    timeout_secs=1 if revocation else 18,
                )
                if revocation:
                    assert network["finished"] - revoked_at < 1
                    assert (await viewer.get(f"/api/export/{sid}.json")).status_code == 404
                    assert (
                        await other.patch(
                            f"/api/sessions/{other_sid}", json={"title": "另一会仍可读"}
                        )
                    ).status_code == 200
                    await other_watcher.find(type="session_updated", title="另一会仍可读")
                    assert not other_watcher.task.done()
                    await other_watcher.__aexit__()
                    other_watcher = None
                    inference.asr.say("撤权后主持录音继续")
                    await recorder.speak()
                    await recorder.next_message("utterance", text="撤权后主持录音继续")
                else:
                    # 从阻塞的那次发送开始算：之前发快照用了多久取决于机器快慢，不该计入发送期限
                    assert 14 <= network["finished"] - network["blocked_since"] < 17, network
                assert max(network["sends"]) > 1
                assert sessions.live_for(sid) is live
                assert not reader.at_eof()

            # 实时验证不等生产结束；原10秒尾部预算仍从400次写全部完成后起算。
            async with asyncio.TaskGroup() as tasks:
                producer = tasks.create_task(produce_changes())
                tasks.create_task(observe_backpressure())
                await producer
                await until_sync(captured.is_set, "真实 TCP 发送背压", timeout_secs=10)

            request_seconds = network["finished"] - network["started"]
            stopped = clock()
            server.should_exit = True
            await asyncio.wait_for(asyncio.shield(running), 10)
            stop_seconds = clock() - stopped
            assert stop_seconds < 8
            # 公开 ASGI 无逐连接 abort：serve 退出后，恢复读取才能排空 TCP 缓存。
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1048576)
            received = 0
            async with asyncio.timeout(15):
                while chunk := await reader.read(1048576):
                    received += len(chunk)
            assert reader.at_eof() and received > 8 * 1024 * 1024
            assert "TimeoutError" in caplog.text  # 故障注入的可见错误，不吞掉。
            print(
                json.dumps(
                    {
                        "request_seconds": request_seconds,
                        "backpressure_observed_seconds": network["backpressure_observed"]
                        - network["started"],
                        "producer_seconds": network["producer_finished"] - network["started"],
                        "producer_started_seconds": network["producer_started"]
                        - network["started"],
                        "http_changes": network["http_changes"],
                        "subscribers": 0,
                        "server_stop_seconds": stop_seconds,
                        "tcp_eof_after_resume": True,
                        "received_bytes": received,
                        "recording_continued": True,
                        "revocation": revocation,
                        "revocation_seconds": network["finished"] - revoked_at
                        if revocation
                        else None,
                    }
                )
            )
    finally:
        print(json.dumps({"tcp_network": network}))
        if other_watcher is not None:
            await other_watcher.__aexit__()
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        if recorder is not None:
            await recorder.close()
        if other_recorder is not None:
            await other_recorder.close()
        server.should_exit = True
        await asyncio.wait_for(asyncio.shield(running), 25)
        listener.close()
        await store.close()
        logger.removeHandler(caplog.handler)
