"""画面摘要与后台模型的接线：配置 → 由谁生成；应用里从上传到摘要的整条路；助理忙时让路。"""

from __future__ import annotations

import asyncio
import io
import time

import httpx
import pytest
from PIL import Image
from pipecat.frames.frames import LLMMessagesAppendFrame
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame

from agentic_meeting.pipeline.activity import AssistantActivity
from agentic_meeting.pipeline.bot import wire_activity
from agentic_meeting.pipeline.services import (
    build_agent_llm,
    build_realtime_llm,
    caption_provider,
)
from agentic_meeting.pipeline.session import SessionManager
from agentic_meeting.pipeline.text_input import TextInputHandler
from agentic_meeting.store.db import Store
from agentic_meeting.types import SPEAKER_TYPED, Utterance
from agentic_meeting.web.app import create_app


def image_bytes(color=(20, 20, 60)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (320, 180), color).save(buf, "WEBP")
    return buf.getvalue()


class FakeLLM:
    def __init__(self, reply="幻灯片：实验结果"):
        self.calls: list[dict] = []
        self.reply = reply

    async def run_inference(self, context, max_tokens=None, system_instruction=None):
        self.calls.append({"messages": list(context.get_messages()), "system": system_instruction})
        return self.reply


class FakeWorker:
    def __init__(self):
        self.frames = []

    async def queue_frame(self, frame):
        self.frames.append(frame)

    async def cancel(self):
        pass

    def messages(self, kind):
        return [
            f.data
            for f in self.frames
            if isinstance(f, RTVIServerMessageFrame) and f.data.get("type") == kind
        ]

    def context_lines(self):
        """经由会议记录器追加进实时模型上下文的行。"""
        return list(self.recorder.lines) if self.recorder is not None else []

    recorder = None


class FakeRecorder:
    elapsed_secs = 1.0

    def __init__(self):
        self.lines: list[str] = []

    async def append_context_line(self, line: str) -> None:
        self.lines.append(line)


# --------------------------------------------------------------------------- #
# 配置 → 由谁生成
# --------------------------------------------------------------------------- #


def test_caption_provider_follows_configuration(make_cfg):
    cfg = make_cfg()
    assert caption_provider(cfg) == "realtime_llm"
    cfg.realtime_llm.active.supports_vision = False
    assert caption_provider(cfg) is None  # 模型不识图
    cfg.screen.caption_provider = "agent_llm"
    assert caption_provider(cfg) == "agent_llm"
    cfg.agent.supports_vision = False
    assert caption_provider(cfg) is None

    for field in ("caption", "enabled"):
        cfg = make_cfg()
        setattr(cfg.screen, field, False)
        assert caption_provider(cfg) is None


def test_background_instance_drops_reply_token_cap_but_keeps_other_sampling(make_cfg):
    cfg = make_cfg()
    sampling = cfg.realtime_llm.active.sampling
    sampling.max_tokens, sampling.temperature = 300, 0.3
    realtime = build_realtime_llm(cfg, "系统提示词")
    background = build_realtime_llm(cfg, "", background=True)
    assert realtime._settings.max_tokens == 300
    assert background._settings.max_tokens != 300  # 口头应答的上限不带到后台请求里
    assert background._settings.temperature == 0.3
    assert background._settings.extra == {
        "extra_body": cfg.realtime_llm.request_extra_body(background=True)
    }


def test_build_agent_llm_uses_agent_section_and_needs_address_and_model(make_cfg, monkeypatch):
    cfg = make_cfg()
    cfg.agent.base_url, cfg.agent.model, cfg.agent.api_key_env = (
        "https://agent.example/v1",
        "fake-agent-model",
        "FAKE_AGENT_KEY",
    )
    monkeypatch.setenv("FAKE_AGENT_KEY", "sk-test")
    llm = build_agent_llm(cfg)
    assert llm is not None
    assert str(llm._client.base_url).rstrip("/") == "https://agent.example/v1"
    assert llm._client.api_key == "sk-test"
    assert llm._settings.model == "fake-agent-model"
    assert llm.supports_developer_role is False
    assert llm._settings.extra == {"extra_body": {}}  # 实时模型的附加字段不带过来
    cfg.agent.model = ""
    assert build_agent_llm(cfg) is None
    cfg.agent.model, cfg.agent.base_url = "fake-agent-model", ""
    assert build_agent_llm(cfg) is None


# --------------------------------------------------------------------------- #
# 应用里的整条路：上传 → 摘要 → 页面消息 + 上下文行
# --------------------------------------------------------------------------- #


class App:
    def __init__(self, cfg, tmp_path, **inject):
        self.cfg, self.tmp_path, self.inject = cfg, tmp_path, inject

    async def __aenter__(self):
        self.store = await Store.open(self.tmp_path / "meetings.db", 4, assistant_name="Nova")
        self.app = create_app(
            self.cfg, store=self.store, static_dir=self.tmp_path / "nope", **self.inject
        )
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )
        self.resources = self.app.state.resources
        self.manager: SessionManager = self.resources.sessions
        return self

    async def __aexit__(self, *exc):
        if self.manager.live is not None:
            await self.manager.finish(self.manager.live)
        await self.client.aclose()
        await self._lifespan.__aexit__(*exc)
        await self.store.close()

    async def go_live(self):
        live = await self.manager.begin()
        worker = FakeWorker()
        worker.recorder = FakeRecorder()
        await self.manager.register(live, worker, worker.recorder)
        return live, worker

    async def upload(self, data=None):
        r = await self.client.post(
            "/api/frames",
            data={"captured_at": str(time.time())},
            files={"image": ("f.webp", data or image_bytes(), "image/webp")},
        )
        assert r.status_code == 200, r.text
        return r.json()["id"]

    async def caption_status(self, frame_id, want, tries=400):
        for _ in range(tries):
            frame = await self.store.get_frame(frame_id)
            if frame.caption_status == want:
                return frame
            await asyncio.sleep(0.005)
        raise AssertionError(f"截图 {frame_id} 的摘要状态一直不是 {want}")


async def test_uploaded_frame_gets_caption_page_message_and_context_line(make_cfg, tmp_path):
    llm = FakeLLM()
    async with App(make_cfg(), tmp_path, background_llm=llm) as app:
        _, worker = await app.go_live()
        frame_id = await app.upload()
        frame = await app.caption_status(frame_id, "done")
        assert frame.caption == "幻灯片：实验结果"
        assert worker.messages("frame_caption") == [
            {"type": "frame_caption", "id": frame_id, "caption": "幻灯片：实验结果"}
        ]
        (line,) = worker.context_lines()  # 经过会议记录器追加（只追加，不触发应答）
        assert line.startswith("[画面 00:00:0") and line.endswith("] 幻灯片：实验结果")
        # 提示词来自 config/prompts/screen_caption.md
        assert "屏幕截图" in llm.calls[0]["system"]
        # 后台模型登记在要让路的名单里
        assert app.resources.background in app.resources.background_models
        assert app.resources.captions.enabled


async def test_no_caption_when_model_cannot_see(make_cfg, tmp_path):
    cfg = make_cfg()
    cfg.realtime_llm.active.supports_vision = False
    llm = FakeLLM()
    async with App(cfg, tmp_path, background_llm=llm) as app:
        _, worker = await app.go_live()
        frame_id = await app.upload()
        await app.caption_status(frame_id, "skipped")
        assert llm.calls == [] and worker.context_lines() == []
        assert not app.resources.captions.enabled
        assert app.resources.background is not None  # 滚动纪要仍然可以用它


async def test_caption_by_agent_model_when_configured(make_cfg, tmp_path):
    cfg = make_cfg()
    cfg.screen.caption_provider = "agent_llm"
    local, remote = FakeLLM("本机的"), FakeLLM("远端的")
    async with App(cfg, tmp_path, background_llm=local, agent_llm=remote) as app:
        await app.go_live()
        frame = await app.caption_status(await app.upload(), "done")
        assert frame.caption == "远端的"
        assert local.calls == [] and len(remote.calls) == 1
        assert len(app.resources.background_models) == 2  # 两个都要在助理应答时让路


async def test_without_any_background_model_frames_are_skipped(make_cfg, tmp_path):
    async with App(make_cfg(), tmp_path) as app:
        await app.go_live()
        await app.caption_status(await app.upload(), "skipped")
        assert app.resources.background is None and app.resources.background_models == []


async def test_caption_finishing_after_the_meeting_changed_is_not_pushed_to_the_new_one(
    make_cfg, tmp_path
):
    gate = asyncio.Event()

    class SlowLLM(FakeLLM):
        async def run_inference(self, context, max_tokens=None, system_instruction=None):
            await gate.wait()
            return "上一场会议的画面"

    async with App(make_cfg(), tmp_path, background_llm=SlowLLM()) as app:
        old_live, _ = await app.go_live()
        frame_id = await app.upload()
        await app.manager.finish(old_live)
        _, new_worker = await app.go_live()
        gate.set()
        frame = await app.caption_status(frame_id, "done")
        assert frame.session_id == old_live.session.id
        assert new_worker.messages("frame_caption") == [] and new_worker.context_lines() == []


# --------------------------------------------------------------------------- #
# SessionManager 的两个定向推送
# --------------------------------------------------------------------------- #


async def test_append_context_goes_through_the_recorder_of_the_named_live_session(tmp_path):
    store = await Store.open(tmp_path / "m.db", 4, assistant_name="Nova")
    manager = SessionManager(store, notify_grace_secs=0.0)
    assert await manager.append_context("nobody", "[画面 00:00:01] x") is False
    live = await manager.begin()
    assert await manager.append_context(live.session.id, "x") is False  # 管线还没登记
    recorder = FakeRecorder()
    await manager.register(live, FakeWorker(), recorder)
    assert await manager.append_context(live.session.id, "[画面 00:00:01] 一页") is True
    assert await manager.append_context("other", "[画面 00:00:02] 别的会议") is False
    assert recorder.lines == ["[画面 00:00:01] 一页"]

    class Broken(FakeRecorder):
        async def append_context_line(self, line):
            raise RuntimeError("管线已经停了")

    live.recorder = Broken()
    assert await manager.append_context(live.session.id, "x") is False
    live.recorder = object()  # 没有这个方法的记录器（旧的测试替身）
    assert await manager.append_context(live.session.id, "x") is False
    await manager.finish(live)
    await store.close()


async def test_push_to_and_queue_frame_to_only_reach_the_named_live_session(tmp_path):
    store = await Store.open(tmp_path / "m.db", 4, assistant_name="Nova")
    manager = SessionManager(store, notify_grace_secs=0.0)
    frame = LLMMessagesAppendFrame(messages=[{"role": "user", "content": "x"}], run_llm=False)
    assert await manager.push_to("nobody", {"type": "notice"}) is False
    assert await manager.queue_frame_to("nobody", frame) is False
    live = await manager.begin()
    assert await manager.queue_frame_to(live.session.id, frame) is False  # 管线还没登记
    worker = FakeWorker()
    await manager.register(live, worker, FakeRecorder())
    assert await manager.push_to(live.session.id, {"type": "notice", "text": "a"}) is True
    assert await manager.queue_frame_to(live.session.id, frame) is True
    assert await manager.push_to("other", {"type": "notice", "text": "b"}) is False
    assert await manager.queue_frame_to("other", frame) is False
    assert worker.messages("notice") == [{"type": "notice", "text": "a"}]
    assert worker.frames[-1] is frame

    class Broken(FakeWorker):
        async def queue_frame(self, frame):
            raise RuntimeError("管线已经停了")

    live.worker = Broken()
    assert await manager.queue_frame_to(live.session.id, frame) is False
    await manager.finish(live)
    await store.close()


# --------------------------------------------------------------------------- #
# 助理忙闲的接线
# --------------------------------------------------------------------------- #


class Emitter:
    def __init__(self):
        self.handlers: dict[str, list] = {}

    def event_handler(self, name):
        def decorator(fn):
            self.handlers.setdefault(name, []).append(fn)
            return fn

        return decorator

    async def fire(self, name, *args):
        for fn in self.handlers.get(name, []):
            await fn(self, *args)


class SpeakingRecorder:
    def __init__(self):
        self.on_bot_speaking_changed = None


async def test_wire_activity_follows_wake_generation_and_speech_and_keeps_previous_callback():
    activity = AssistantActivity(idle_delay_secs=0)
    seen: list[bool] = []
    activity.subscribe(seen.append)
    wake, aggregator, recorder = Emitter(), Emitter(), SpeakingRecorder()
    earlier: list[bool] = []

    async def previous(speaking):
        earlier.append(speaking)

    recorder.on_bot_speaking_changed = previous
    wire_activity(activity, wake, aggregator, recorder)

    await wake.fire("on_wake_phrase_detected", "nova")
    assert seen == [True]
    await aggregator.fire("on_assistant_turn_started")
    await recorder.on_bot_speaking_changed(True)
    await aggregator.fire("on_assistant_turn_stopped", object())
    assert seen == [True]
    await recorder.on_bot_speaking_changed(False)
    assert seen == [True, False]
    assert earlier == [True, False]  # 原来的回调（文字入口）照样收到

    await wake.fire("on_wake_phrase_detected", "nova")
    await wake.fire("on_wake_phrase_timeout")
    assert seen == [True, False, True, False]

    # 之前没有回调时也能接
    bare = SpeakingRecorder()
    wire_activity(AssistantActivity(), Emitter(), Emitter(), bare)
    await bare.on_bot_speaking_changed(True)


class TypedRecorder:
    async def record_typed(self, text):
        return Utterance("s", SPEAKER_TYPED, 1.0, 1.0, text, source="text")

    async def context_text(self, utterance):
        return f"[00:00:01 文字输入] {utterance.text}"


@pytest.mark.parametrize("broken", [False, True])
async def test_text_request_notifies_before_the_request_is_pushed(broken):
    order: list[str] = []

    async def push(frame):
        order.append(type(frame).__name__)

    async def notice(level, text):
        order.append(f"notice:{text}")

    async def on_dispatch():
        order.append("dispatch")
        if broken:
            raise RuntimeError("boom")

    handler = TextInputHandler(
        recorder=TypedRecorder(),
        push=push,
        notice=notice,
        tts_enabled=False,
        on_dispatch=on_dispatch,
    )
    await handler.handle("查一下")
    await handler.close()
    assert order == ["dispatch", "LLMMessagesAppendFrame"]  # 通知出错也照样把请求交出去


# --------------------------------------------------------------------------- #
# 后台请求实际发出去的样子（真实的模型服务对象 + MockTransport）
# --------------------------------------------------------------------------- #


async def test_background_request_body(make_cfg):
    import json

    from pipecat.processors.aggregators.llm_context import LLMContext

    from agentic_meeting.pipeline.background import BackgroundModel

    cfg = make_cfg()
    cfg.realtime_llm.mode = "llama_server"
    ep = cfg.realtime_llm.llama_server
    ep.model, ep.realtime_slot, ep.background_slot = "fake-local-llm", 0, 1
    ep.sampling.max_tokens, ep.sampling.temperature = 256, 0.7
    bodies: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        message = {"role": "assistant", "content": " 幻灯片：结果表 "}
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "created": 0,
                "model": "fake",
                "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
            },
        )

    llm = build_realtime_llm(
        cfg,
        "",
        background=True,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    model = BackgroundModel(llm)
    image = LLMContext.create_image_url_message(url="data:image/webp;base64,AAAA", text="看图")
    assert await model.run([image], system="描述截图", max_tokens=200) == "幻灯片：结果表"

    (body,) = bodies
    assert body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": "描述截图"}
    assert body["messages"][1]["content"][-1]["image_url"]["url"] == "data:image/webp;base64,AAAA"
    # 输出上限写在 max_completion_tokens 里（pipecat-notes.md §5）；配置里给口头应答的 max_tokens 不带
    assert body["max_completion_tokens"] == 200 and "max_tokens" not in body
    assert body["temperature"] == 0.7
    assert (body["id_slot"], body["cache_prompt"]) == (1, True)  # 后台槽位

    # 没有系统提示词时不发空的 system 消息
    await model.run([{"role": "user", "content": "整理纪要"}], max_tokens=50)
    assert [m["role"] for m in bodies[1]["messages"]] == ["user"]


def test_agent_llm_carries_the_agent_extra_body(make_cfg):
    """直接用后台模型生成（会后报告、画面摘要）时带上 agent.extra_body，一般用来指定思考的强度。"""
    cfg = make_cfg()
    cfg.agent.base_url, cfg.agent.model = "https://agent.example/v1", "fake-agent-model"
    cfg.agent.extra_body = {"reasoning_effort": "medium"}
    llm = build_agent_llm(cfg)
    assert llm is not None
    assert llm._settings.extra == {"extra_body": {"reasoning_effort": "medium"}}


async def test_generation_by_the_agent_model_gets_the_wide_output_budget(make_cfg, tmp_path):
    """后台 agent 的模型会先思考，思考也占输出额度：由它写报告、生成画面摘要时用 agent.generation_max_tokens。"""
    cfg = make_cfg()
    cfg.agent.generation_max_tokens = 20000
    async with App(
        cfg, tmp_path, background_llm=FakeLLM("本机的"), agent_llm=FakeLLM("远端的")
    ) as app:
        assert app.resources.captions._max_tokens < 20000  # 默认由实时模型生成，上限不变
        assert app.resources.reports._max_tokens < 20000
    cfg.screen.caption_provider = cfg.report.provider = "agent_llm"
    async with App(
        cfg, tmp_path, background_llm=FakeLLM("本机的"), agent_llm=FakeLLM("远端的")
    ) as app:
        assert app.resources.captions._max_tokens == 20000
        assert app.resources.reports._max_tokens == 20000
        assert app.resources.reports._section_max_tokens == 20000
