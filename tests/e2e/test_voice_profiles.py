"""声纹档案：主持人征得本人同意后记住一位说话人的声音，可以取消关联、改名、删除。"""

from __future__ import annotations

import pytest

from .meeting_client import WatchingDevice
from .test_speaker_memory import LINES, voice_secs
from .waiting import until


def speaker_config(inference, **extra):
    return {
        "speaker_memory.backend": "sherpa_onnx",
        "speaker_memory.base_url": inference.speaker.origin,
        "speaker_memory.launch.enabled": False,
        **extra,
    }


async def record(http, client, inference) -> str:
    """开一场会，说几句话，等声纹片段采集到；返回会议编号。"""
    session = await client.connect()
    for line in LINES:
        inference.asr.say(line)
    await client.speak(seconds=7)
    await client.next_message("utterance")

    async def collected():
        secs = await voice_secs(http, session["id"])
        return secs if sum(secs.values()) > 0 else None

    await until(collected, "采集到声纹片段")
    return session["id"]


async def speakers(http, sid: str) -> dict[int, dict]:
    items = (await http.get("/api/speakers", params={"session_id": sid})).json()["items"]
    return {item["idx"]: item for item in items}


async def test_a_host_remembers_a_voice_and_can_forget_it(start_app, meeting_factory, inference):
    app = await start_app(speaker_config(inference, **{"speaker_memory.min_enroll_secs": 1}))
    async with app.http() as http:
        client = meeting_factory(http)()
        sid = await record(http, client, inference)
        assert (await http.get("/api/voice-profiles")).json() == {
            "items": [],
            "min_enroll_secs": 1.0,
        }
        # 测试里没有说话人区分：先把发言归给一位新说话人，再记住他的声音
        ids = [
            u["id"]
            for u in (await http.get("/api/utterances", params={"session_id": sid})).json()["items"]
        ]
        moved = await http.post(
            "/api/utterances/speaker",
            json={"session_id": sid, "ids": ids, "new_speaker": "林老师"},
        )
        idx = moved.json()["speaker"]["idx"]

        refused = await http.post(
            f"/api/speakers/{idx}/voice-profile", json={"session_id": sid, "name": "林晓"}
        )
        assert refused.status_code == 400
        assert refused.json()["code"] == "voice.consent_required"
        unknown = await http.post(
            "/api/speakers/0/voice-profile", json={"session_id": sid, "consent": True}
        )
        assert unknown.json()["code"] == "voice.not_enrollable"

        enrolled = await http.post(
            f"/api/speakers/{idx}/voice-profile",
            json={"session_id": sid, "name": "林晓", "consent": True},
        )
        assert enrolled.status_code == 200, enrolled.text
        profile = enrolled.json()["voice_profile"]
        assert profile["name"] == "林晓" and profile["samples"] == 1 and profile["secs"] > 0
        assert "vector" not in str(enrolled.json())
        # 说话人改成档案里的名字，页面收到改名消息
        await client.next_message("speaker", display_name="林晓")
        linked = (await speakers(http, sid))[idx]
        assert linked["display_name"] == "林晓"
        assert linked["voice_profile"] == {"id": profile["id"], "name": "林晓"}

        # 取消关联：档案还在
        unlinked = await http.delete(
            f"/api/speakers/{idx}/voice-profile", params={"session_id": sid}
        )
        assert unlinked.status_code == 200
        assert (await speakers(http, sid))[idx]["voice_profile"] is None
        assert len((await http.get("/api/voice-profiles")).json()["items"]) == 1

        # 再录进同一份档案：同一场会议的样本被替换，不会多出一个
        again = await http.post(
            f"/api/speakers/{idx}/voice-profile",
            json={"session_id": sid, "profile_id": profile["id"], "consent": True},
        )
        assert again.status_code == 200 and again.json()["voice_profile"]["samples"] == 1

        renamed = await http.patch(
            f"/api/voice-profiles/{profile['id']}", json={"name": "林晓老师"}
        )
        assert renamed.json()["name"] == "林晓老师"

        # 删除档案：样本、关联和这位说话人在会议里的声纹片段都删掉，发言保留
        deleted = await http.delete(f"/api/voice-profiles/{profile['id']}")
        assert deleted.status_code == 200 and deleted.json()["clips"] > 0
        assert (await http.get("/api/voice-profiles")).json()["items"] == []
        after = (await speakers(http, sid))[idx]
        assert after["voice_profile"] is None and after["voice_secs"] == 0
        remaining = (await http.get("/api/utterances", params={"session_id": sid})).json()
        assert len(remaining["items"]) == len(ids)
        assert (await http.delete(f"/api/voice-profiles/{profile['id']}")).status_code == 404
        await client.close()


async def test_enrollment_needs_enough_speech(start_app, meeting_factory, inference):
    app = await start_app(speaker_config(inference, **{"speaker_memory.min_enroll_secs": 600}))
    async with app.http() as http:
        client = meeting_factory(http)()
        sid = await record(http, client, inference)
        ids = [
            u["id"]
            for u in (await http.get("/api/utterances", params={"session_id": sid})).json()["items"]
        ]
        moved = await http.post(
            "/api/utterances/speaker",
            json={"session_id": sid, "ids": ids, "new_speaker": "林老师"},
        )
        idx = moved.json()["speaker"]["idx"]
        short = await http.post(
            f"/api/speakers/{idx}/voice-profile", json={"session_id": sid, "consent": True}
        )
        assert short.status_code == 400
        body = short.json()
        assert body["code"] == "voice.too_short"
        assert 0 < body["params"]["have"] < 600 and body["params"]["need"] == 600
        assert (await http.get("/api/voice-profiles")).json()["items"] == []
        await client.close()


@pytest.mark.parametrize("path", ["/api/voice-profiles", "/api/speaker-suggestions"])
async def test_voice_profiles_do_not_exist_when_the_feature_is_off(start_app, path):
    app = await start_app()
    async with app.http() as http:
        response = await http.get(path)
        assert response.status_code == 404


async def assign_all(http, sid: str, name: str) -> int:
    """测试里没有说话人区分：把这场会议的发言都归给一位新说话人，返回他的编号。"""
    ids = [
        u["id"]
        for u in (await http.get("/api/utterances", params={"session_id": sid})).json()["items"]
    ]
    moved = await http.post(
        "/api/utterances/speaker", json={"session_id": sid, "ids": ids, "new_speaker": name}
    )
    return moved.json()["speaker"]["idx"]


async def test_a_remembered_voice_is_suggested_in_a_later_meeting(
    start_app, meeting_factory, inference
):
    app = await start_app(
        speaker_config(
            inference,
            **{"speaker_memory.min_enroll_secs": 1, "speaker_memory.min_match_secs": 1},
        )
    )
    async with app.http() as http:
        meetings = meeting_factory(http)
        first = meetings()
        sid = await record(http, first, inference)
        idx = await assign_all(http, sid, "林老师")
        enrolled = await http.post(
            f"/api/speakers/{idx}/voice-profile",
            json={"session_id": sid, "name": "林晓", "consent": True},
        )
        profile = enrolled.json()["voice_profile"]
        await first.close()

        # 下一场会议：同一个人又说了话，主持人的页面收到提示，旁观的设备收不到
        second = meetings()
        sid2 = await record(http, second, inference)
        assert sid2 != sid
        watcher = WatchingDevice(http, sid2, "Mozilla/5.0 (iPhone)")
        assert await watcher.open() == 200
        idx2 = await assign_all(http, sid2, "说话人 甲")
        hint = await second.next_message("speaker_suggestion", speaker_idx=idx2)
        assert hint["profile_id"] == profile["id"] and hint["name"] == "林晓"
        assert 0 < hint["score"] <= 1 and "vector" not in str(hint)
        listed = (await http.get("/api/speaker-suggestions", params={"session_id": sid2})).json()
        assert [(s["speaker_idx"], s["profile_id"]) for s in listed["items"]] == [
            (idx2, profile["id"])
        ]

        # 忽略：这场会议里不再提
        target = {"session_id": sid2, "speaker_idx": idx2, "profile_id": profile["id"]}
        assert (await http.post("/api/speaker-suggestions/dismiss", json=target)).status_code == 200
        await second.next_message("speaker_suggestion_cleared", speaker_idx=idx2)
        listed = (await http.get("/api/speaker-suggestions", params={"session_id": sid2})).json()
        assert listed["items"] == []

        # 主持人还是认定是他：确认之后说话人改名、关联档案，这场会议的声纹加进档案
        accepted = await http.post("/api/speaker-suggestions/accept", json=target)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["voice_profile"]["samples"] == 2
        await second.next_message("speaker", idx=idx2, display_name="林晓")
        linked = (await speakers(http, sid2))[idx2]
        assert linked["voice_profile"] == {"id": profile["id"], "name": "林晓"}
        assert not [m for m in watcher.messages if m.get("type", "").startswith("speaker_sugg")]
        await watcher.close()
        await second.close()

        # 会议结束后照样能查：已经关联了，就没有提示了
        listed = (await http.get("/api/speaker-suggestions", params={"session_id": sid2})).json()
        assert listed["items"] == []
        profiles = (await http.get("/api/voice-profiles")).json()["items"]
        assert [(p["name"], p["samples"]) for p in profiles] == [("林晓", 2)]
