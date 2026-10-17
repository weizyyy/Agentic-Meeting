"""跨会议记住说话人的基础：开启后，会议里每位说话人的单人语音切成片段、在本机算出声纹，随发言保存；
可以单独清除；关闭时什么都不算。"""

from __future__ import annotations

import pytest

from .meeting_client import MeetingClient
from .test_meeting_access import PASSWORD, USERS, login
from .waiting import until

LINES = ["这周把对照组的数据补齐", "下周再讨论模型的结果", "样本量还要再加一些", "先这样安排"]


@pytest.fixture
async def app(start_app, inference):
    return await start_app(
        {
            "speaker_memory.backend": "sherpa_onnx",
            "speaker_memory.base_url": inference.speaker.origin,
            "speaker_memory.launch.enabled": False,
        }
    )


async def voice_secs(http, session_id: str) -> dict[int, float]:
    items = (await http.get("/api/speakers", params={"session_id": session_id})).json()["items"]
    return {item["idx"]: item["voice_secs"] for item in items}


async def test_voice_clips_follow_the_captions_and_can_be_erased(http, meeting, inference):
    client = meeting()
    session = await client.connect()
    for line in LINES:
        inference.asr.say(line)
    await client.speak(seconds=7)
    await client.next_message("utterance")

    async def collected():
        secs = await voice_secs(http, session["id"])
        return secs if sum(secs.values()) > 0 else None

    secs = await until(collected, "采集到声纹片段")
    assert set(secs) == {0}  # 测试里没有说话人区分，发言都记在「未知」名下
    # 送去算声纹的都是 1 到 6 秒的语音
    assert inference.speaker.seconds()
    assert all(1.0 <= s <= 6.0 + 0.01 for s in inference.speaker.seconds())

    # 把发言改给一位新说话人，声纹跟着发言走
    utterances = (await http.get("/api/utterances", params={"session_id": session["id"]})).json()
    ids = [u["id"] for u in utterances["items"]]
    moved = await http.post(
        "/api/utterances/speaker",
        json={"session_id": session["id"], "ids": ids, "new_speaker": "林老师"},
    )
    new_idx = moved.json()["speaker"]["idx"]
    after = await voice_secs(http, session["id"])
    assert after[new_idx] == pytest.approx(sum(secs.values()), abs=0.2) and after[0] == 0

    # 声纹服务停了，字幕照常
    inference.speaker.down = True
    inference.asr.say("服务停了也照样转录")
    await client.speak()
    await client.next_message("utterance", text="服务停了也照样转录")

    erased = await http.delete(f"/api/sessions/{session['id']}/voice")
    assert erased.status_code == 200 and erased.json()["clips"] > 0
    assert all(s == 0 for s in (await voice_secs(http, session["id"])).values())
    remaining = (await http.get("/api/utterances", params={"session_id": session["id"]})).json()
    assert len(remaining["items"]) == len(ids) + 1


async def test_without_a_voice_model_nothing_is_computed(start_app, meeting_factory, inference):
    app = await start_app()
    async with app.http() as http:
        client = meeting_factory(http)()
        session = await client.connect()
        inference.asr.say("没有开启声纹")
        await client.speak()
        await client.next_message("utterance")
        await http.post(f"/api/sessions/{session['id']}/end")
        await client.next_message("session_closed")
        items = (await http.get("/api/speakers", params={"session_id": session["id"]})).json()
        assert items["items"] and all("voice_secs" not in item for item in items["items"])
    assert inference.speaker.count() == 0


async def test_only_a_host_can_erase_voice_clips(start_app, inference):
    app = await start_app(
        {
            "server.meeting_access": True,
            "server.local_users": USERS,
            "speaker_memory.backend": "sherpa_onnx",
            "speaker_memory.base_url": inference.speaker.origin,
            "speaker_memory.launch.enabled": False,
        },
        env={"E2E_LOCAL_PASSWORD": PASSWORD},
    )
    async with app.http() as host, app.http() as viewer:
        await login(host, "alpha")
        await login(viewer, "viewer")
        client = MeetingClient(host)
        try:
            sid = (await client.connect())["id"]
            for line in LINES:
                inference.asr.say(line)
            await client.speak(seconds=7)
            await client.next_message("utterance")

            async def collected():
                secs = await voice_secs(host, sid)
                return secs if sum(secs.values()) > 0 else None

            await until(collected, "采集到声纹片段")
        finally:
            await client.close()
        grant = {"role": "watcher"}
        assert (
            await host.put(f"/api/sessions/{sid}/members/local:viewer", json=grant)
        ).status_code == 200
        # 旁观者能看到时长，但不能清除
        assert sum((await voice_secs(viewer, sid)).values()) > 0
        assert (await viewer.delete(f"/api/sessions/{sid}/voice")).status_code == 403
        # 也不能录入声纹档案；档案列表登录了的账号都能看
        enroll = {"session_id": sid, "consent": True}
        assert (await viewer.post("/api/speakers/1/voice-profile", json=enroll)).status_code == 403
        assert (await viewer.get("/api/voice-profiles")).status_code == 200
        assert sum((await voice_secs(host, sid)).values()) > 0

        erased = await host.delete(f"/api/sessions/{sid}/voice")
        assert erased.status_code == 200 and erased.json()["clips"] > 0
        log = (await host.get(f"/api/sessions/{sid}/access-log")).json()["items"]
        entry = next(e for e in log if e["action"] == "voice.erase")
        assert entry["principal_id"] == "local:alpha"
        assert entry["detail"] == {"clips": erased.json()["clips"]}
