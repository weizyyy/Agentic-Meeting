"""任务工具与播报：delegate_task、task_status、cancel_task。

前半用假的工具调用参数 + 真实的任务管理器（假运行器）+ 真实的 SQLite 临时库；
最后用真实的管线走一遍「委托 → 口头确认 → 任务做完 → 简报」，分别验证文字委托不出声、语音委托朗读。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from fakes import FakeASR
from pipecat.adapters.services.open_ai_adapter import OpenAILLMAdapter
from pipecat.frames.frames import (
    InputAudioRawFrame,
    LLMConfigureOutputFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import SleepFrame, run_test
from test_bot import PCM, FakeTransport, completion_chunk, sse
from test_meeting_recorder import CallSignal, Hook

from agentic_meeting.agent.tasks import RunnerError, TaskManager
from agentic_meeting.pipeline import tools as tools_module
from agentic_meeting.pipeline.bot import (
    AppResources,
    build_parts,
    pipeline_processors,
    wire_assistant_recording,
    wire_wake_events,
)
from agentic_meeting.pipeline.services import build_realtime_llm, build_tts
from agentic_meeting.pipeline.text_input import TextInputHandler
from agentic_meeting.pipeline.tools import (
    cancel_task,
    delegate_tool,
    realtime_tools,
    task_status,
)
from agentic_meeting.store.db import Store
from agentic_meeting.types import SPEAKER_TYPED, ASRDelta, TaskResult


class GatedRunner:
    """假的运行器：放行才结束；可以让它失败。"""

    def __init__(self):
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.error: Exception | None = None
        self.tasks = []

    async def __call__(self, task, on_event):
        self.tasks.append(task)
        self.started.set()
        await on_event("tool_call", "正在检索「引用数」", None)
        await self.gate.wait()
        if self.error is not None:
            raise self.error
        return TaskResult(brief="这篇论文被引 1243 次", detail_md="## 详情")


class FakeLLMQueue:
    def __init__(self, log):
        self.log = log

    async def queue_frame(self, frame, direction=None):
        self.log.append(("llm_frame", frame))


class Rig:
    def __init__(self, cfg, store, session, *, now_secs=3600.0):
        self.cfg, self.store, self.session = cfg, store, session
        self.runner = GatedRunner()
        self.pushed: list[dict] = []
        self.log: list[tuple] = []  # 按先后：模型队列里的帧、工具的回报
        self.quiet = asyncio.Event()
        self.quiet.set()
        self.recorder = SimpleNamespace(
            elapsed_secs=now_secs, last_speaker_idx=3, wait_quiet=self.wait_quiet
        )
        self.gate = SimpleNamespace(modality="voice")
        self.manager = TaskManager(
            store=store, runner=self.runner, notify=self.notify, max_concurrent=2, timeout_secs=30
        )
        self.resources = SimpleNamespace(
            cfg=cfg,
            store=store,
            sessions=SimpleNamespace(
                live=SimpleNamespace(session=session, recorder=self.recorder, gate=self.gate)
            ),
            embedder=None,
            frames=None,
            tasks=self.manager,
        )
        self.context = LLMContext()
        self.delegate = delegate_tool(cfg.agent.task_timeout_secs)
        self.quiet_waits: list[float] = []

    async def notify(self, _sid, data):
        self.pushed.append(data)

    async def wait_quiet(self, max_wait_secs):
        self.quiet_waits.append(max_wait_secs)
        try:
            await asyncio.wait_for(self.quiet.wait(), max_wait_secs)
        except TimeoutError:
            return False
        return True

    def params(self):
        async def result_callback(result, *, properties=None):
            self.log.append(("result", result, properties))
            if properties is not None and properties.on_context_updated is not None:
                await properties.on_context_updated()  # 真实运行时由助理侧聚合器在写完上下文后调用

        return SimpleNamespace(
            app_resources=self.resources,
            result_callback=result_callback,
            context=self.context,
            tool_call_id="call_1",
            llm=FakeLLMQueue(self.log),
        )

    def results(self) -> list[dict]:
        return [entry[1] for entry in self.log if entry[0] == "result"]

    def kinds(self) -> list:
        """日志的形状：配置帧记成 skip_tts 的值，回报记成它的 status。"""
        out = []
        for entry in self.log:
            if entry[0] == "llm_frame":
                assert isinstance(entry[1], LLMConfigureOutputFrame)
                out.append(entry[1].skip_tts)
            else:
                out.append(entry[1].get("status", "error"))
        return out


async def settle(times=20):
    for _ in range(times):
        await asyncio.sleep(0.002)


@pytest.fixture
async def rig(make_cfg, tmp_path):
    cfg = make_cfg()
    store = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    session = await store.create_session(now=1000.0)
    r = Rig(cfg, store, session)
    yield r
    await r.manager.close()
    await store.close()


# --------------------------------------------------------------------------- #
# 工具列表与定义
# --------------------------------------------------------------------------- #


def tool_names(tools) -> list[str]:
    params = OpenAILLMAdapter().get_llm_invocation_params(
        LLMContext(tools=tools), system_instruction="s", convert_developer_to_user=True
    )
    return [t["function"]["name"] for t in params["tools"]]


def test_task_tools_are_offered_only_when_the_agent_is_enabled(make_cfg):
    cfg = make_cfg()
    assert tool_names(realtime_tools(cfg)) == [
        "recall",
        "get_digest",
        "look_at_screen",
        "delegate_task",
        "task_status",
        "cancel_task",
    ]
    cfg.agent.enabled = False
    assert tool_names(realtime_tools(cfg)) == ["recall", "get_digest", "look_at_screen"]
    assert tool_names(realtime_tools()) == ["recall", "get_digest", "look_at_screen"]


def test_delegate_task_schema_and_call_options(make_cfg):
    cfg = make_cfg()
    cfg.agent.task_timeout_secs = 900.0
    tool = delegate_tool(cfg.agent.task_timeout_secs)
    params = OpenAILLMAdapter().get_llm_invocation_params(
        LLMContext(tools=[tool]), system_instruction="s", convert_developer_to_user=True
    )
    schema = params["tools"][0]["function"]
    assert schema["name"] == "delegate_task" and schema["description"]
    assert schema["parameters"]["required"] == ["goal"]
    properties = schema["parameters"]["properties"]
    assert (properties["goal"]["type"], properties["include_screen"]["type"]) == (
        "string",
        "boolean",
    )
    assert properties["minutes_of_context"]["type"] == "number"
    # 不随打断取消；调用超时比任务超时长，留出等「没人说话」和收尾的时间
    assert tool._pipecat_cancel_on_interruption is False
    assert tool._pipecat_timeout_secs > 900.0 + tools_module.QUIET_WAIT_SECS


def test_prompt_mentions_task_tools_only_when_enabled(make_cfg):
    cfg = make_cfg()
    enabled = build_parts(cfg, asr_backend=object()).llm._settings.system_instruction
    assert "delegate_task" in enabled and "task_status" in enabled
    assert "ASYNC TOOLS" not in enabled  # Pipecat 自动附加的那段英文被去掉了，用我们自己的说明
    cfg.agent.enabled = False
    disabled = build_parts(cfg, asr_backend=object()).llm._settings.system_instruction
    assert "delegate_task" not in disabled and "recall" in disabled


# --------------------------------------------------------------------------- #
# delegate_task
# --------------------------------------------------------------------------- #


async def test_delegate_acknowledges_first_then_reports_the_result(rig):
    frame_ids = []
    for t in (3000.0, 3400.0, 3450.0, 3500.0, 3590.0):
        frame = await rig.store.add_frame(rig.session.id, t=t, width=1, height=1, suffix=".webp")
        frame_ids.append(frame.id)
    call = asyncio.create_task(
        rig.delegate(
            rig.params(), goal="  核实这篇论文的引用数  ", minutes_of_context=5, include_screen=True
        )
    )
    await rig.runner.started.wait()
    await settle()
    (accepted,) = rig.log
    assert accepted[1] == {"task_id": "t1", "status": "accepted"}
    assert accepted[2].is_final is False  # 中间结果：调用还没结束

    task = rig.runner.tasks[0]
    assert task.goal == "核实这篇论文的引用数"
    assert (task.t_from, task.t_to) == (3300.0, 3600.0)  # 最近 5 分钟
    assert task.frame_ids == frame_ids[-3:]  # 范围内最近 3 张
    assert (task.modality, task.requested_by, task.requested_t) == ("voice", 3, 3600.0)

    rig.runner.gate.set()
    await call
    assert rig.kinds() == ["accepted", False, "succeeded"]  # 语音委托：朗读
    assert rig.results()[-1] == {
        "task_id": "t1",
        "status": "succeeded",
        "brief": "这篇论文被引 1243 次",
    }
    assert rig.log[-1][2].is_final is True
    assert (await rig.store.get_task(task.id)).announced is True
    assert rig.quiet_waits == [tools_module.QUIET_WAIT_SECS]


async def test_delegate_argument_handling(rig):
    await rig.store.add_frame(rig.session.id, t=10.0, width=1, height=1, suffix=".webp")
    rig.runner.gate.set()
    await rig.delegate(rig.params(), goal="查一下")  # 默认：5 分钟、不带截图
    await rig.delegate(rig.params(), goal="查一下", minutes_of_context="abc", include_screen="true")
    await rig.delegate(rig.params(), goal="查一下", minutes_of_context=100000)
    await rig.delegate(rig.params(), goal="长" * 5000)
    first, second, third, fourth = rig.runner.tasks
    assert (first.t_from, first.frame_ids) == (3300.0, [])
    assert second.t_from == 3300.0  # 不合理的值按默认
    assert len(second.frame_ids) == 1  # 这段时间没有新截图：带上最新的一张
    assert third.t_from == 0.0  # 上限 60 分钟，且不早于会议开头
    assert len(fourth.goal) == tools_module.MAX_GOAL_CHARS

    for bad in ("", "   ", None):
        before = len(rig.runner.tasks)
        await rig.delegate(rig.params(), goal=bad)
        assert "error" in rig.results()[-1] and len(rig.runner.tasks) == before


async def test_failed_and_cancelled_tasks_are_reported_truthfully(rig):
    rig.runner.error = RunnerError("连不上远端模型")
    rig.runner.gate.set()
    await rig.delegate(rig.params(), goal="查一下")
    assert rig.results()[-1] == {"task_id": "t1", "status": "failed", "reason": "连不上远端模型"}

    rig.runner.error = None
    rig.runner.gate.clear()
    rig.runner.started.clear()
    call = asyncio.create_task(rig.delegate(rig.params(), goal="再查一个"))
    await rig.runner.started.wait()
    await settle()
    await cancel_task(rig.params(), task_id="t2")
    await call
    final = next(r for r in rig.results() if r.get("task_id") == "t2" and "reason" in r)
    assert final == {"task_id": "t2", "status": "cancelled", "reason": "已取消"}
    assert {"task_id": "t2", "status": "cancelled"} in rig.results()  # cancel_task 自己的回报


async def test_voice_announcement_waits_until_nobody_is_speaking(rig):
    rig.quiet.clear()  # 有人正在说话
    rig.runner.gate.set()
    call = asyncio.create_task(rig.delegate(rig.params(), goal="查一下"))
    await settle(60)
    assert rig.kinds() == ["accepted"]  # 任务已经做完，但结果还压着
    assert (await rig.store.find_task(rig.session.id, "t1")).status == "succeeded"
    rig.quiet.set()  # 说完了
    await call
    assert rig.kinds() == ["accepted", False, "succeeded"]


async def test_voice_announcement_is_forced_after_the_maximum_wait(rig, monkeypatch):
    monkeypatch.setattr(tools_module, "QUIET_WAIT_SECS", 0.05)
    rig.quiet.clear()
    rig.runner.gate.set()
    await asyncio.wait_for(rig.delegate(rig.params(), goal="查一下"), 2.0)
    assert rig.kinds() == ["accepted", False, "succeeded"] and rig.quiet_waits == [0.05]


async def test_text_delegation_is_announced_in_text_and_does_not_wait(rig):
    rig.gate.modality = "text"
    rig.quiet.clear()  # 有人在说话也不等：文字播报不出声
    rig.runner.gate.set()
    await asyncio.wait_for(rig.delegate(rig.params(), goal="查一下"), 2.0)
    # 交回结果之前先把模型设成只出文字
    assert rig.kinds() == ["accepted", True, "succeeded"]
    assert rig.quiet_waits == []
    task = rig.runner.tasks[0]
    assert (task.modality, task.requested_by) == ("text", SPEAKER_TYPED)


async def test_delegate_without_task_manager_or_meeting_explains(rig):
    rig.resources.tasks = None
    await rig.delegate(rig.params(), goal="查一下")
    assert rig.results()[-1] == {"note": "后台任务功能没有开启"}
    rig.resources.sessions.live = None
    await rig.delegate(rig.params(), goal="查一下")
    assert rig.results()[-1] == {"note": "现在没有进行中的会议"}


# --------------------------------------------------------------------------- #
# task_status、cancel_task
# --------------------------------------------------------------------------- #


async def test_task_status_and_cancel_by_label_or_latest(rig):
    await task_status(rig.params())
    assert rig.results()[-1] == {"note": "这场会议里还没有交办过任务"}
    await cancel_task(rig.params())
    assert rig.results()[-1] == {"note": "这场会议里还没有交办过任务"}

    call = asyncio.create_task(rig.delegate(rig.params(), goal="核实引用数"))
    await rig.runner.started.wait()
    await settle()
    await task_status(rig.params())
    status = rig.results()[-1]
    assert (status["task_id"], status["status"], status["goal"]) == ("t1", "running", "核实引用数")
    assert status["recent_steps"] == ["开始处理", "正在检索「引用数」"]
    await task_status(rig.params(), task_id=" T1 ")
    assert rig.results()[-1]["task_id"] == "t1"
    await task_status(rig.params(), task_id="t9")
    assert rig.results()[-1] == {"note": "没有编号是 t9 的任务"}
    await cancel_task(rig.params(), task_id="t9")
    assert rig.results()[-1] == {"note": "没有编号是 t9 的任务"}

    await cancel_task(rig.params())  # 不给编号：最近的一个
    assert rig.results()[-1] == {"task_id": "t1", "status": "cancelled"}
    await call
    await cancel_task(rig.params(), task_id="t1")
    assert rig.results()[-1] == {
        "task_id": "t1",
        "status": "cancelled",
        "note": "这个任务已经结束了，不用取消",
    }
    await task_status(rig.params())
    assert rig.results()[-1]["reason"] == "已取消"


# --------------------------------------------------------------------------- #
# 真实的管线：委托 → 口头确认 → 任务做完 → 简报
# --------------------------------------------------------------------------- #


def tool_call_chunk(name: str, arguments: dict) -> dict:
    call = {
        "index": 0,
        "id": "call_abc",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }
    return completion_chunk({"role": "assistant", "tool_calls": [call]})


async def run_delegation(make_cfg, tmp_path, *, typed: bool):
    cfg = make_cfg()
    cfg.session.assistant_name = "Nova"
    cfg.session.wake_aliases = []
    cfg.turn.smart_turn = False
    cfg.turn.wake_timeout_secs = 5.0
    cfg.turn.single_activation = True
    llm_bodies: list[dict] = []
    tts_inputs: list[str] = []

    def llm_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        llm_bodies.append(body)
        n = len(llm_bodies)
        if n == 1:
            text = sse(
                tool_call_chunk("delegate_task", {"goal": "核实这篇论文的引用数"}),
                completion_chunk({}, finish="tool_calls"),
            )
        else:
            answer = "好的，我去查。" if n == 2 else "查到了，被引一千二百多次。"
            text = sse(
                completion_chunk({"role": "assistant", "content": answer}),
                completion_chunk({}, finish="stop"),
            )
        return httpx.Response(200, content=text, headers={"content-type": "text/event-stream"})

    def tts_handler(request: httpx.Request) -> httpx.Response:
        tts_inputs.append(json.loads(request.content)["input"])
        return httpx.Response(200, content=PCM, headers={"content-type": "audio/pcm"})

    store = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    runner = GatedRunner()
    notified: list[dict] = []

    async def notify(_sid, data):
        notified.append(data)

    manager = TaskManager(
        store=store, runner=runner, notify=notify, max_concurrent=1, timeout_secs=30
    )
    try:
        session = await store.create_session(now=1000.0)
        asr = FakeASR()
        asr.on_push = {
            1: [ASRDelta("Nova，", "帮我", 0.1)],
            2: [ASRDelta("帮我查一下引用数", "", 0.2)],
        }
        asr.on_flush = [[ASRDelta("。", "", 0.2, segment_end=True)]]
        llm = build_realtime_llm(
            cfg,
            "你是助理",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(llm_handler)),
        )
        tts = build_tts(
            cfg,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(tts_handler)),
            stop_frame_timeout_s=0.3,
        )
        parts = build_parts(
            cfg, asr_backend=asr, llm=llm, tts=tts, store=store, session_id=session.id
        )
        wire_assistant_recording(parts)
        wire_wake_events(parts.wake, lambda data: asyncio.sleep(0))
        resources = AppResources(
            cfg,
            store=store,
            sessions=SimpleNamespace(
                live=SimpleNamespace(session=session, recorder=parts.recorder, gate=parts.gate)
            ),
            tasks=manager,
        )
        hook = Hook()

        async def notice(level, text):
            pass

        handler = TextInputHandler(
            recorder=parts.recorder, push=hook.push_frame, notice=notice, tts_enabled=True
        )

        async def attach_resources():
            parts.llm.pipeline_worker._app_resources = resources

        async def release_task():
            await asyncio.wait_for(runner.started.wait(), 5)
            runner.gate.set()

        chunk = np.zeros(16 * 100, dtype="<i2").tobytes()

        def audio() -> InputAudioRawFrame:
            return InputAudioRawFrame(audio=chunk, sample_rate=16000, num_channels=1)

        if typed:
            request = [CallSignal(lambda: handler.handle("帮我查一下这篇论文的引用数"))]
        else:
            request = [
                VADUserStartedSpeakingFrame(),
                audio(),
                SleepFrame(sleep=0.05),
                audio(),
                SleepFrame(sleep=0.05),
                VADUserStoppedSpeakingFrame(),
            ]
        frames = [
            SleepFrame(sleep=0.1),
            CallSignal(attach_resources),
            *request,
            SleepFrame(sleep=2.5),  # 工具调用 + 口头确认
            CallSignal(release_task),  # 后台任务做完
            SleepFrame(sleep=3.0),  # 简报
        ]
        await run_test(
            Pipeline([hook, *pipeline_processors(FakeTransport(), parts)]),
            frames_to_send=frames,
            pipeline_params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
        )
        await handler.close()
        said = [
            n.utterance.text
            for n in await store.list_utterances(session.id, tail=10)
            if n.utterance.source == "assistant"
        ]
        task = await store.find_task(session.id, "t1")
        return llm_bodies, tts_inputs, said, task, runner
    finally:
        await manager.close()
        await store.close()


async def test_typed_delegation_is_acknowledged_and_briefed_in_text_only(make_cfg, tmp_path):
    bodies, tts_inputs, said, task, runner = await run_delegation(make_cfg, tmp_path, typed=True)
    assert len(bodies) == 3  # 发起委托、口头确认、任务做完后的简报
    assert tts_inputs == []  # 全程不出声，包括任务完成后的简报
    assert said == ["好的，我去查。", "查到了，被引一千二百多次。"]
    assert (task.status, task.modality, task.announced) == ("succeeded", "text", True)
    assert runner.tasks[0].goal == "核实这篇论文的引用数"
    # 第二次请求里有「已受理」，第三次请求里有任务的结果——都是改写过的中文行，不是 Pipecat 的协议 JSON
    second = [m["content"] for m in bodies[1]["messages"] if isinstance(m.get("content"), str)]
    assert "[任务 t1 已受理] 后台正在处理，做完后你会收到结果。" in second
    assert "后台任务已经开始，结果稍后送达。不要重复调用，也不要猜结果。" in second
    third = [m["content"] for m in bodies[2]["messages"] if isinstance(m.get("content"), str)]
    assert third[-1] == "[任务 t1 完成] 这篇论文被引 1243 次"
    everything = json.dumps(bodies, ensure_ascii=False)
    assert "async_tool" not in everything and r"\u" not in everything  # 中文没有被转义
    # 这三次请求都没有 developer 角色（本地模型不认识，已转成 user）
    assert all(m["role"] != "developer" for body in bodies for m in body["messages"])


async def test_spoken_delegation_is_acknowledged_and_briefed_aloud(make_cfg, tmp_path):
    bodies, tts_inputs, said, task, _runner = await run_delegation(make_cfg, tmp_path, typed=False)
    assert len(bodies) == 3
    spoken = "".join(tts_inputs)
    assert "我去查" in spoken and "查到了" in spoken  # 确认和简报都朗读
    assert said == ["好的，我去查。", "查到了，被引一千二百多次。"]
    assert (task.status, task.modality, task.announced) == ("succeeded", "voice", True)


# --------------------------------------------------------------------------- #
# 异步工具的协议消息 → 中文行
# --------------------------------------------------------------------------- #


def test_async_tool_messages_are_rewritten_for_the_model():
    from pipecat.processors.aggregators import async_tool_messages as atm

    from agentic_meeting.pipeline.async_tools import (
        CANCELLED_TEXT,
        STARTED_TEXT,
        localize_async_tool_messages,
    )

    def final(result: dict) -> dict:
        return atm.build_final_result_message("c1", json.dumps(result, ensure_ascii=False))

    plain = {"role": "user", "content": "[00:00:05 王老师] 学习率是不是大了"}
    looks_like_json = {"role": "user", "content": '{"type": "别的东西"}'}
    image = {"role": "user", "content": [{"type": "text", "text": "图"}]}
    accepted = atm.build_intermediate_result_message(
        "c1", json.dumps({"task_id": "t1", "status": "accepted"})
    )
    done_as_user = {**final({"task_id": "t2", "status": "succeeded", "brief": "被引 1243 次"})}
    done_as_user["role"] = "user"  # 本地模型不认识 developer，已经被转成 user
    messages = [
        plain,
        looks_like_json,
        image,
        atm.build_started_message("c1"),
        accepted,
        done_as_user,
        final({"task_id": "t3", "status": "failed", "reason": "连不上远端模型"}),
        final({"task_id": "t4", "status": "cancelled", "reason": "已取消"}),
        final({"items": ["别的异步工具"]}),
        atm.build_cancelled_message("c9"),
    ]
    out = localize_async_tool_messages(messages)
    assert out[0] is plain and out[1] is looks_like_json and out[2] is image  # 别的消息原样
    assert out[3] == {"role": "tool", "content": STARTED_TEXT, "tool_call_id": "c1"}
    assert [m["content"] for m in out[4:]] == [
        "[任务 t1 已受理] 后台正在处理，做完后你会收到结果。",
        "[任务 t2 完成] 被引 1243 次",
        "[任务 t3 失败] 连不上远端模型",
        "[任务 t4 已取消] 已取消",
        '[工具结果] {"items": ["别的异步工具"]}',
        CANCELLED_TEXT,
    ]
    assert [m["role"] for m in out[4:]] == ["developer", "user"] + ["developer"] * 4  # 角色不动
    assert messages[5]["content"] != out[5]["content"]  # 不改原来的对象（上下文里存的还是原样）


def test_warm_and_real_requests_still_match_with_async_tool_messages(make_cfg):
    from pipecat.processors.aggregators import async_tool_messages as atm

    cfg = make_cfg()
    llm = build_realtime_llm(cfg, "你是助理")
    context = LLMContext(
        [
            {"role": "user", "content": "帮我查一下"},
            atm.build_final_result_message(
                "c1", json.dumps({"task_id": "t1", "status": "succeeded", "brief": "查到了"})
            ),
        ],
        tools=realtime_tools(cfg),
    )
    params = llm.request_params(context)
    assert params["messages"][-1]["content"] == "[任务 t1 完成] 查到了"
    assert llm.request_params(context)["messages"] == params["messages"]
    # 上下文里存的仍是 Pipecat 的原样消息
    assert "async_tool" in context.get_messages()[-1]["content"]


async def test_with_attach_frames_a_task_gets_every_relevant_frame_so_far(rig):
    """agent.attach_frames 打开：不看 include_screen，到现在为止的截图都带上（无关画面、没变的重复截图除外）。"""
    rig.cfg.agent.attach_frames = True
    rig.cfg.agent.max_attached_frames = 10
    kept = []
    for t, caption in (
        (100.0, "幻灯片：第一页"),
        (160.0, "幻灯片：第一页"),
        (900.0, "无关画面"),
        (3500.0, "幻灯片：第二页"),
    ):
        frame = await rig.store.add_frame(rig.session.id, t=t, width=1, height=1, suffix=".webp")
        await rig.store.set_frame_caption(frame.id, status="done", caption=caption)
        if caption != "无关画面" and t != 160.0:
            kept.append(frame.id)
    call = asyncio.create_task(rig.delegate(rig.params(), goal="核实一下", minutes_of_context=5))
    await rig.runner.started.wait()
    assert rig.runner.tasks[0].frame_ids == kept
    rig.runner.gate.set()
    await call
