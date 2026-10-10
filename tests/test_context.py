"""上下文管理：长度估算、从数据库重建、压缩、预热、互斥。"""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext

import httpx
import pytest
from pipecat.frames.frames import LLMMessagesAppendFrame, LLMMessagesUpdateFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import SleepFrame, run_test
from test_bot import completion_chunk, sse
from test_meeting_recorder import (
    GAP,
    CallSignal,
    FakeStore,
    Hook,
    final,
    one_utterance_frames,
)
from waiting import wait_until

from agentic_meeting.pipeline.activity import AssistantActivity
from agentic_meeting.pipeline.background import Preempted
from agentic_meeting.pipeline.bot import build_context_manager, build_parts, wire_context_manager
from agentic_meeting.pipeline.context import (
    DIGEST_PREFIX,
    IMAGE_TOKENS,
    MESSAGE_OVERHEAD_TOKENS,
    NO_DIGEST_NOTE,
    ContextManager,
    build_context_messages,
    estimate_message,
    estimate_messages,
    estimate_tokens,
)
from agentic_meeting.pipeline.recorder import MeetingRecorder
from agentic_meeting.pipeline.services import build_realtime_llm
from agentic_meeting.pipeline.tools import realtime_tools
from agentic_meeting.store.db import Store
from agentic_meeting.types import SPEAKER_ASSISTANT, SPEAKER_TYPED, ASRDelta, Utterance

# --------------------------------------------------------------------------- #
# 估算
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "tokens"),
    [
        ("", 0),
        ("学习率", 3),  # 中文一字一个
        ("abcd", 1),  # 英文四个字符一个
        ("abcde", 2),  # 向上取整
        ("baseline 的学习率", 4 + 3),  # "baseline " 9 个字符 → 3，再加 4 个汉字
        ("，。！？", 4),  # 全角标点按一个算
        ("ひらがな한글", 6),
    ],
)
def test_estimate_tokens(text, tokens):
    assert estimate_tokens(text) == tokens


def test_estimate_message_counts_text_images_and_tool_calls():
    o = MESSAGE_OVERHEAD_TOKENS
    assert estimate_message({"role": "user", "content": "学习率"}) == o + 3
    image = {
        "role": "user",
        "content": [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/webp;base64," + "A" * 100000}},
        ],
    }
    assert estimate_message(image) == o + 2 + IMAGE_TOKENS  # 不数 base64 的长度
    call = {
        "role": "assistant",
        "tool_calls": [{"function": {"name": "recall", "arguments": '{"query": "学习率"}'}}],
    }
    assert estimate_message(call) == o + estimate_tokens("recall") + estimate_tokens(
        '{"query": "学习率"}'
    )
    assert estimate_message("奇怪的东西") == o
    assert estimate_messages([{"role": "user", "content": "abcd"}] * 3) == 3 * (o + 1)


# --------------------------------------------------------------------------- #
# 从数据库重建
# --------------------------------------------------------------------------- #


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    yield s
    await s.close()


async def say(store, sid, text, t, speaker=1, source="asr"):
    u = Utterance(sid, speaker, t, t + 2.0, text, source=source)
    await store.add_utterance(u)
    return u.id


async def caption(store, sid, t, text):
    f = await store.add_frame(sid, t=t, width=1, height=1, suffix=".webp")
    await store.set_frame_caption(f.id, status="done", caption=text)


async def test_rebuild_is_digest_plus_recent_lines_in_time_order(store):
    sid = (await store.create_session()).id
    old = await say(store, sid, "很早的讨论", 60.0)
    await say(store, sid, "十分钟内：学习率是不是大了", 3100.0)
    await say(store, sid, "我可以解释。", 3110.0, speaker=SPEAKER_ASSISTANT, source="assistant")
    await say(store, sid, "帮我查一下", 3120.0, speaker=SPEAKER_TYPED, source="text")
    await caption(store, sid, 30.0, "很早的画面")
    await caption(store, sid, 3105.0, "幻灯片：结果表")
    await caption(store, sid, 3115.0, "幻灯片：结果表")  # 和上一张一样
    await caption(store, sid, 3118.0, "无关画面")
    await store.add_digest(sid, t_from=0, t_to=3000.0, text="一、讨论了基线", last_utterance_id=old)

    messages = await build_context_messages(store, sid, 3600.0, keep_recent_secs=600.0)
    assert messages == [
        {"role": "user", "content": f"{DIGEST_PREFIX}\n一、讨论了基线"},
        {"role": "user", "content": "[00:51:40 说话人 1] 十分钟内：学习率是不是大了"},
        {"role": "user", "content": "[画面 00:51:45] 幻灯片：结果表"},
        {"role": "assistant", "content": "我可以解释。"},
        {"role": "user", "content": "[00:52:00 文字输入] 帮我查一下"},
    ]


async def test_rebuild_leaves_no_gap_between_digest_and_recent_window(store):
    """纪要只覆盖到第 20 分钟，而「最近 10 分钟」从第 50 分钟算起：中间那段原文要保留。"""
    sid = (await store.create_session()).id
    covered = await say(store, sid, "纪要里有的", 600.0)
    await say(store, sid, "纪要之后、十分钟以前", 1800.0)
    await say(store, sid, "十分钟以内", 3500.0)
    await caption(store, sid, 1500.0, "纪要之后的画面")
    await store.add_digest(sid, t_from=0, t_to=1200.0, text="纪要", last_utterance_id=covered)
    messages = await build_context_messages(store, sid, 3600.0, keep_recent_secs=600.0)
    assert [m["content"] for m in messages[1:]] == [
        "[画面 00:25:00] 纪要之后的画面",
        "[00:30:00 说话人 1] 纪要之后、十分钟以前",
        "[00:58:20 说话人 1] 十分钟以内",
    ]


async def test_rebuild_keeps_lines_the_digest_covers_if_they_are_recent(store):
    sid = (await store.create_session()).id
    covered = await say(store, sid, "纪要已经纳入、但还在最近十分钟里", 3500.0)
    await store.add_digest(sid, t_from=0, t_to=3502.0, text="纪要", last_utterance_id=covered)
    messages = await build_context_messages(store, sid, 3600.0, keep_recent_secs=600.0)
    assert len(messages) == 2 and "还在最近十分钟里" in messages[1]["content"]


async def test_rebuild_without_digest(store):
    sid = (await store.create_session()).id
    await say(store, sid, "很早", 10.0)
    await say(store, sid, "最近", 3500.0)
    dropped = await build_context_messages(store, sid, 3600.0, keep_recent_secs=600.0)
    assert dropped == [
        {"role": "user", "content": f"{DIGEST_PREFIX}\n{NO_DIGEST_NOTE}"},
        {"role": "user", "content": "[00:58:20 说话人 1] 最近"},
    ]
    # 会议还没有「最近 N 分钟」长：什么都没丢，不需要那条说明
    everything = await build_context_messages(store, sid, 300.0, keep_recent_secs=600.0)
    assert [m["content"] for m in everything] == [
        "[00:00:10 说话人 1] 很早",
        "[00:58:20 说话人 1] 最近",
    ]


async def test_rebuild_only_reads_its_own_session(store):
    sid = (await store.create_session()).id
    other = (await store.create_session()).id
    await say(store, other, "别的会议", 10.0)
    await caption(store, other, 5.0, "别的会议的画面")
    await store.add_digest(other, t_from=0, t_to=5, text="别的纪要", last_utterance_id=0)
    assert await build_context_messages(store, sid, 100.0, keep_recent_secs=600.0) == []


# --------------------------------------------------------------------------- #
# ContextManager（假的模型、记录器、忙闲状态）
# --------------------------------------------------------------------------- #


class FakeLLM:
    def __init__(self, static="系统提示词"):
        self.static = static
        self.warmed: list[list] = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.started = asyncio.Event()
        self.error: Exception | None = None

    def static_prompt_tokens_text(self, context):
        return self.static

    async def warm_cache(self, context):
        self.started.set()
        await self.gate.wait()
        if self.error is not None:
            raise self.error
        self.warmed.append(list(context.get_messages()))


class FakeRecorder:
    def __init__(self, context, elapsed=3600.0):
        self.context = context
        self.elapsed_secs = elapsed
        self.rebuilds: list[list] = []

    async def rebuild_context(self, build):
        messages = await build()
        self.rebuilds.append(messages)
        self.context.set_messages(messages)  # 真实运行时是用户侧聚合器收到替换帧后做这件事
        return len(messages)


class FakeActivity:
    busy = False
    responding = False


class FakeDigests:
    def __init__(self, store=None, sid=None):
        self.calls = 0
        self.error: BaseException | None = None
        self.on_call = None

    async def catch_up(self, session_id):
        self.calls += 1
        if self.on_call is not None:
            await self.on_call()
        if self.error is not None:
            raise self.error


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def manager_for(store, sid, context, **kw):
    llm = kw.pop("llm", None) or FakeLLM()
    recorder = kw.pop("recorder", None) or FakeRecorder(context)
    activity = kw.pop("activity", None) or FakeActivity()
    kw.setdefault("budget_tokens", 2000)
    kw.setdefault("keep_recent_secs", 600.0)
    kw.setdefault("cache_warm", True)
    kw.setdefault("warm_interval_secs", 30.0)
    manager = ContextManager(
        llm=llm,
        context=context,
        store=store,
        session_id=sid,
        recorder=recorder,
        activity=activity,
        **kw,
    )
    return manager, llm, recorder, activity


def filled_context(chars=5000) -> LLMContext:
    context = LLMContext()
    for i in range(chars // 50):
        context.add_message({"role": "user", "content": f"[00:00:{i % 60:02d} 甲] " + "讨" * 40})
    return context


async def long_meeting(store):
    """一场说了很多话的会议：纪要覆盖到第 50 分钟，最近十分钟有几句。"""
    sid = (await store.create_session()).id
    last = 0
    for i in range(40):
        last = await say(store, sid, "很早的讨论" * 10, float(i * 60))
    await store.add_digest(
        sid, t_from=0, t_to=3000.0, text="一、此前讨论了很多", last_utterance_id=last
    )
    await say(store, sid, "最近的一句", 3500.0)
    return sid


def test_estimate_includes_system_prompt_and_tools():
    context = LLMContext([{"role": "user", "content": "学习率"}])
    manager, llm, _, _ = manager_for(None, "s", context, llm=FakeLLM(static="系统" * 50))
    assert manager.estimate() == 100 + MESSAGE_OVERHEAD_TOKENS + 3
    llm.static = "变了也不重算"
    context.add_message({"role": "user", "content": "abcd"})
    assert manager.estimate() == 100 + 2 * MESSAGE_OVERHEAD_TOKENS + 3 + 1


async def test_compacts_when_over_budget_and_idle_then_warms(store):
    sid = await long_meeting(store)
    context = filled_context()
    manager, llm, recorder, _ = manager_for(store, sid, context)
    assert manager.estimate() > 2000
    assert await manager.maybe_compact() is True
    (rebuilt,) = recorder.rebuilds
    assert rebuilt[0]["content"].startswith(DIGEST_PREFIX)
    assert [m["content"] for m in rebuilt[1:]] == ["[00:58:20 说话人 1] 最近的一句"]
    assert manager.estimate() < 2000
    # 压缩之后立刻预热，预热的正是新的上下文
    assert llm.warmed == [rebuilt]


async def test_no_compaction_under_budget_or_while_busy(store):
    sid = await long_meeting(store)
    small = LLMContext([{"role": "user", "content": "短"}])
    manager, _, recorder, _ = manager_for(store, sid, small)
    assert await manager.maybe_compact() is False

    manager, llm, recorder, activity = manager_for(store, sid, filled_context())
    activity.busy = True
    assert await manager.maybe_compact() is False
    assert recorder.rebuilds == [] and llm.warmed == []
    activity.busy = False
    assert await manager.maybe_compact() is True


async def test_compaction_without_cache_warm_still_happens_but_never_warms(store):
    sid = await long_meeting(store)
    manager, llm, recorder, _ = manager_for(store, sid, filled_context(), cache_warm=False)
    assert await manager.maybe_compact() is True
    assert len(recorder.rebuilds) == 1 and llm.warmed == []
    manager.on_wake()
    await manager._warm_if_due()
    assert await manager.warm() is False
    assert llm.warmed == [] and manager._spawned == set()


async def test_compaction_refreshes_digest_first_and_yields_if_assistant_got_busy(store):
    sid = await long_meeting(store)
    digests = FakeDigests()
    manager, _, recorder, activity = manager_for(store, sid, filled_context(), digests=digests)
    assert await manager.maybe_compact() is True
    assert digests.calls == 1

    # 补纪要的工夫助理被叫到了：这次不压
    digests = FakeDigests()

    async def become_busy():
        activity2.busy = True

    manager, _, recorder, activity2 = manager_for(store, sid, filled_context(), digests=digests)
    digests.on_call = become_busy
    assert await manager.maybe_compact() is False
    assert recorder.rebuilds == []
    # 让路不算一次压缩：助理闲下来马上可以再试，不用等冷却
    activity2.busy = False
    digests.on_call = None
    assert await manager.maybe_compact() is True


@pytest.mark.parametrize("error", [Preempted(), RuntimeError("后台模型挂了")])
async def test_digest_failure_does_not_block_compaction(store, error):
    sid = await long_meeting(store)
    digests = FakeDigests()
    digests.error = error
    manager, _, recorder, _ = manager_for(store, sid, filled_context(), digests=digests)
    assert await manager.maybe_compact() is True
    assert len(recorder.rebuilds) == 1


async def test_cooldown_between_compactions(store):
    sid = await long_meeting(store)
    clock = Clock()
    context = filled_context()
    manager, _, recorder, _ = manager_for(
        store, sid, context, now=clock, compact_cooldown_secs=60.0
    )
    assert await manager.maybe_compact() is True
    for m in filled_context().get_messages():
        context.add_message(m)  # 又涨上去了
    clock.t += 59
    assert await manager.maybe_compact() is False
    clock.t += 2
    assert await manager.maybe_compact() is True
    assert len(recorder.rebuilds) == 2


async def test_recent_window_shrinks_until_it_fits(store):
    sid = (await store.create_session()).id
    for i in range(60):  # 最近十分钟每十秒一句长话：光原文就超预算
        await say(store, sid, "很长的一句话" * 20, 3000.0 + i * 10)
    manager, _, recorder, _ = manager_for(store, sid, filled_context(), budget_tokens=2000)
    assert await manager.maybe_compact() is True
    (rebuilt,) = recorder.rebuilds
    assert rebuilt[0]["content"].endswith(NO_DIGEST_NOTE)
    kept = rebuilt[1:]
    assert 0 < len(kept) < 60  # 保留时长被减半了若干次
    assert kept[-1]["content"].startswith("[00:59:50")  # 留下的是最近的
    assert estimate_messages(rebuilt) <= 2000 * 0.8 or len(kept) <= 7  # 放得下，或已经缩到一分钟


async def test_compaction_failure_is_contained_and_cools_down(store):
    sid = await long_meeting(store)
    clock = Clock()

    class BrokenRecorder(FakeRecorder):
        async def rebuild_context(self, build):
            raise RuntimeError("管线已经停了")

    context = filled_context()
    manager, llm, _, _ = manager_for(
        store, sid, context, recorder=BrokenRecorder(context), now=clock
    )
    assert await manager.maybe_compact() is False
    assert llm.warmed == []
    assert await manager.maybe_compact() is False  # 冷却中，不会每五秒重试一次
    assert manager._compacting is False


async def test_no_store_means_no_compaction():
    manager, _, recorder, _ = manager_for(None, "", filled_context())
    assert await manager.maybe_compact() is False


# ---- 预热 ----


async def test_warm_skips_when_responding_empty_or_already_in_flight():
    context = LLMContext()
    manager, llm, _, activity = manager_for(None, "", context)
    assert await manager.warm() is False  # 上下文是空的
    context.add_message({"role": "user", "content": "[00:00:01 甲] 开始吧"})
    activity.responding = True
    assert await manager.warm() is False  # 正在生成或朗读
    activity.responding = False

    llm.gate.clear()
    first = asyncio.create_task(manager.warm())
    await llm.started.wait()
    assert await manager.warm() is False  # 已有一次在途
    llm.gate.set()
    assert await first is True
    assert len(llm.warmed) == 1


async def test_wake_warms_even_though_assistant_is_awaiting_the_request():
    """被叫到名字后助理算「忙」（后台模型让路），但这时正是预热的时机。"""
    context = LLMContext([{"role": "user", "content": "[00:00:01 甲] 开始吧"}])
    manager, llm, _, activity = manager_for(None, "", context)
    activity.busy = True  # 等着作答
    try:
        manager.on_wake()
        await wait_until(lambda: not manager._spawned, description="唤醒预热收尾")
        assert len(llm.warmed) == 1
    finally:
        await manager.stop()


async def test_warm_failure_is_logged_not_raised():
    context = LLMContext([{"role": "user", "content": "x"}])
    manager, llm, _, _ = manager_for(None, "", context)
    llm.error = RuntimeError("连接被拒绝")
    assert await manager.warm() is False
    llm.error = None
    assert await manager.warm() is True


async def test_periodic_warm_only_when_due_and_context_changed():
    clock = Clock()
    context = LLMContext([{"role": "user", "content": "[00:00:01 甲] 开始吧"}])
    manager, llm, _, activity = manager_for(None, "", context, now=clock, warm_interval_secs=30.0)
    await manager._warm_if_due()
    assert len(llm.warmed) == 1  # 第一次
    clock.t += 10
    context.add_message({"role": "user", "content": "[00:00:09 乙] 好"})
    await manager._warm_if_due()
    assert len(llm.warmed) == 1  # 还没到间隔
    clock.t += 25
    await manager._warm_if_due()
    assert len(llm.warmed) == 2  # 到了，而且有新内容
    clock.t += 40
    await manager._warm_if_due()
    assert len(llm.warmed) == 2  # 到了，但上下文没变：不用再发
    clock.t += 40
    context.add_message({"role": "user", "content": "[00:01:00 甲] 继续"})
    activity.responding = True
    await manager._warm_if_due()
    assert len(llm.warmed) == 2  # 助理正在回答
    activity.responding = False
    await manager._warm_if_due()
    assert len(llm.warmed) == 3


async def test_loop_runs_checks_and_stop_cancels_everything(store):
    sid = await long_meeting(store)
    manager, llm, recorder, _ = manager_for(store, sid, filled_context(), check_interval_secs=0.01)
    manager.start()
    manager.start()
    try:
        await wait_until(lambda: recorder.rebuilds and llm.warmed, description="循环压缩并预热")
        llm.gate.clear()
        manager.on_wake()
    finally:
        await manager.stop()
    await manager.stop()
    assert len(recorder.rebuilds) == 1  # 冷却期内不会反复压缩
    assert manager._task is None


# --------------------------------------------------------------------------- #
# 预热请求与正式请求：前缀逐字一致（真实的模型服务对象）
# --------------------------------------------------------------------------- #


async def test_warm_request_matches_the_real_request_except_stream_and_token_cap(make_cfg):
    cfg = make_cfg()
    cfg.realtime_llm.mode = "llama_server"
    ep = cfg.realtime_llm.llama_server
    ep.model, ep.sampling.max_tokens, ep.sampling.temperature = "fake-local-llm", 256, 0.6
    bodies: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("stream"):
            text = sse(
                completion_chunk({"role": "assistant", "content": "好"}),
                completion_chunk({}, finish="stop"),
            )
            return httpx.Response(200, content=text, headers={"content-type": "text/event-stream"})
        message = {"role": "assistant", "content": "好"}
        return httpx.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 0,
                "model": "fake",
                "choices": [{"index": 0, "finish_reason": "length", "message": message}],
            },
        )

    llm = build_realtime_llm(
        cfg, "你是组会助理", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))
    )
    context = LLMContext(
        [
            {"role": "user", "content": "[00:00:05 王老师] 学习率是不是大了"},
            {"role": "assistant", "content": "可以再降一半。"},
            {"role": "developer", "content": "后台任务的结果"},  # 会被转成 user
            {"role": "user", "content": "[画面 00:00:30] 幻灯片：结果表"},
        ],
        tools=realtime_tools(),
    )
    await llm.warm_cache(context)
    stream = await llm.get_chat_completions(context)
    async for _ in stream:
        pass

    warm, real = bodies
    assert warm["messages"] == real["messages"]
    assert warm["tools"] == real["tools"] and len(warm["tools"]) == 3
    assert warm["messages"][0] == {"role": "system", "content": "你是组会助理"}
    assert (warm["stream"], warm["max_tokens"]) == (False, 1)
    assert (real["stream"], real["max_tokens"]) == (True, 256)
    assert "stream_options" not in warm and "max_completion_tokens" not in warm
    different = {"stream", "stream_options", "max_tokens"}
    assert {k: v for k, v in warm.items() if k not in different} == {
        k: v for k, v in real.items() if k not in different
    }
    # 同一个槽位、同样开着前缀缓存
    assert warm["id_slot"] == real["id_slot"] and warm["cache_prompt"] is True

    static = llm.static_prompt_tokens_text(context)
    assert static.startswith("你是组会助理") and '"recall"' in static


# --------------------------------------------------------------------------- #
# 接线
# --------------------------------------------------------------------------- #


def test_build_context_manager_takes_everything_from_configuration(make_cfg):
    cfg = make_cfg()
    cfg.realtime.context_budget_tokens = 9000
    cfg.realtime.keep_recent_minutes = 7.5
    cfg.realtime.cache_warm_interval_secs = 12.0
    parts = build_parts(cfg, asr_backend=object())
    manager = build_context_manager(
        cfg, parts, AssistantActivity(), store=None, session_id="s1", digests="digests"
    )
    assert manager._budget == 9000 and manager._keep_recent == 450.0
    assert manager._warm_interval == 12.0
    assert manager._cache_warm is cfg.realtime_llm.cache_warm
    assert manager._context is parts.context and manager._llm is parts.llm
    assert manager._recorder is parts.recorder and manager._digests == "digests"
    # 估算能直接用真实的服务对象：系统提示词 + 三个工具的定义
    assert manager.estimate() > 100


@pytest.mark.parametrize("mode", ["llama_server", "openai_api"])
def test_cache_warm_follows_the_config_property_not_the_access_mode(make_cfg, mode):
    cfg = make_cfg()
    cfg.realtime_llm.mode = mode
    parts = build_parts(cfg, asr_backend=object())
    manager = build_context_manager(cfg, parts, AssistantActivity(), store=None, session_id="")
    assert manager._cache_warm == cfg.realtime_llm.cache_warm


async def test_wake_event_triggers_a_warm_up():
    class Emitter:
        def event_handler(self, name):
            def decorator(fn):
                self.name, self.fn = name, fn
                return fn

            return decorator

    context = LLMContext([{"role": "user", "content": "[00:00:01 甲] 开始吧"}])
    manager, llm, _, _ = manager_for(None, "", context)
    wake = Emitter()
    wire_context_manager(manager, wake)
    assert wake.name == "on_wake_phrase_detected"
    try:
        await wake.fn(wake, "nova")
        await wait_until(lambda: not manager._spawned, description="唤醒事件预热收尾")
        assert len(llm.warmed) == 1
    finally:
        await manager.stop()


# --------------------------------------------------------------------------- #
# 记录器：追加与重建的顺序
# --------------------------------------------------------------------------- #


def context_frames(frames) -> list:
    return [f for f in frames if isinstance(f, (LLMMessagesAppendFrame, LLMMessagesUpdateFrame))]


async def test_recorder_rebuild_pushes_an_update_frame_that_does_not_run_the_llm():
    rec = MeetingRecorder(store=FakeStore(), session_id="s1")

    async def rebuild():
        async def build():
            return [{"role": "user", "content": "【此前会议纪要】\n纪要"}]

        assert await rec.rebuild_context(build) == 1

    async def screen():
        await rec.append_context_line("[画面 00:00:01] 一页")

    down, _ = await run_test(
        Pipeline([Hook(), rec]),
        frames_to_send=[CallSignal(rebuild), CallSignal(screen), SleepFrame(sleep=0.05)],
    )
    update, append = context_frames(down)
    assert isinstance(update, LLMMessagesUpdateFrame) and update.run_llm is False
    assert update.messages == [{"role": "user", "content": "【此前会议纪要】\n纪要"}]
    assert isinstance(append, LLMMessagesAppendFrame) and append.run_llm is False
    assert append.messages == [{"role": "user", "content": "[画面 00:00:01] 一页"}]


@pytest.mark.parametrize("signal_finalizing", [True, False])
async def test_lines_finalized_during_a_rebuild_are_appended_after_the_update_frame(
    monkeypatch, signal_finalizing
):
    """重建读完数据库之后才落库的发言，必须排在替换帧后面——否则它会被替换掉，从上下文里消失。"""
    store = FakeStore()
    rec = MeetingRecorder(store=store, session_id="s1")
    reading = asyncio.Event()
    finalizing = asyncio.Event()
    release = asyncio.Event()
    on_final = rec._on_final

    async def entered_final(final):
        if signal_finalizing:
            finalizing.set()
        await on_final(final)

    monkeypatch.setattr(rec, "_on_final", entered_final)
    tasks: list[asyncio.Task] = []
    callback_errors: list[Exception] = []

    async def slow_build():
        snapshot = [u.text for u in store.utterances]  # 读数据库：这时还没有「你好」
        reading.set()
        await release.wait()
        return [{"role": "user", "content": f"重建时数据库里有：{snapshot}"}]

    async def start_rebuild():
        tasks.append(asyncio.create_task(rec.rebuild_context(slow_build)))
        await reading.wait()

    async def finish_rebuild():
        try:
            try:
                await asyncio.wait_for(finalizing.wait(), 2.0)  # 发言确实进入了重建锁的竞争
            finally:
                release.set()  # 管线会捕获回调异常；超时也必须先释放重建锁
            await tasks[0]
        except Exception as error:
            callback_errors.append(error)
            raise

    try:
        async with asyncio.timeout(5.0):
            with nullcontext() if signal_finalizing else pytest.raises(TimeoutError):
                down, _ = await run_test(
                    Pipeline([Hook(), rec]),
                    frames_to_send=[
                        CallSignal(start_rebuild),
                        *one_utterance_frames("你好"),
                        CallSignal(finish_rebuild),
                        SleepFrame(sleep=0.1),
                    ],
                )
                if callback_errors:
                    raise callback_errors[0]  # 不能只让 Pipecat 记日志而把测试判为成功
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if not signal_finalizing:
        assert len(callback_errors) == 1 and isinstance(callback_errors[0], TimeoutError)
    kinds = [type(f).__name__ for f in context_frames(down)]
    assert kinds == ["LLMMessagesUpdateFrame", "LLMMessagesAppendFrame"]
    update, append = context_frames(down)
    assert "你好" not in update.messages[0]["content"]
    assert append.messages[0]["content"].endswith("你好")
    assert [u.text for u in store.utterances] == ["你好"]


async def test_a_line_stored_before_the_rebuild_reads_is_not_appended_twice():
    store = FakeStore()
    rec = MeetingRecorder(store=store, session_id="s1")

    async def rebuild():
        async def build():
            return [{"role": "user", "content": u.text} for u in store.utterances]

        await rec.rebuild_context(build)

    d = ASRDelta("第二句", "", 2.0, segment_end=True)
    down, _ = await run_test(
        Pipeline([Hook(), rec]),
        frames_to_send=[
            *one_utterance_frames("第一句"),
            GAP,
            CallSignal(rebuild),
            GAP,
            final("第二句", d),
            SleepFrame(sleep=0.05),
        ],
    )
    kinds = [type(f).__name__ for f in context_frames(down)]
    assert kinds == ["LLMMessagesAppendFrame", "LLMMessagesUpdateFrame", "LLMMessagesAppendFrame"]
    assert context_frames(down)[1].messages == [{"role": "user", "content": "第一句"}]


async def test_rebuild_includes_recently_finished_tasks(store):
    session = await store.create_session(now=1000.0)
    sid = session.id
    await say(store, sid, "最近的一句", 3500.0)
    old = await store.create_task(sid, goal="很早做完的")
    await store.update_task(old.id, status="succeeded", brief="早就查到了", finished_at=1000.0 + 60)
    done = await store.create_task(sid, goal="刚做完的")
    await store.update_task(
        done.id, status="succeeded", brief="被引 1243 次", finished_at=1000.0 + 3510
    )
    failed = await store.create_task(sid, goal="失败的")
    await store.update_task(
        failed.id, status="failed", error="连不上远端模型", finished_at=1000.0 + 3520
    )
    cancelled = await store.create_task(sid, goal="取消的")
    await store.update_task(
        cancelled.id, status="cancelled", error="已取消", finished_at=1000.0 + 3530
    )
    await store.create_task(sid, goal="还在做的")  # 没做完的不写：做完时结果会自己送到

    messages = await build_context_messages(store, sid, 3600.0, keep_recent_secs=600.0)
    assert [m["content"] for m in messages[1:]] == [
        "[00:58:20 说话人 1] 最近的一句",
        "[任务 t2 完成] 被引 1243 次",
        "[任务 t3 失败] 连不上远端模型",
        "[任务 t4 已取消] 已取消",
    ]
