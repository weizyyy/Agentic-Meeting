"""实时模型的工具：recall、get_digest、look_at_screen。

前半用假的 ``FunctionCallParams``（只要 ``app_resources``、``result_callback``、``context``）+ 真实的 SQLite 临时库；
最后用真实的管线和模拟的大模型走一遍「模型发起工具调用 → 工具执行 → 带着结果再生成」。
"""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from fakes import FakeASR
from pipecat.adapters.services.open_ai_adapter import OpenAILLMAdapter
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import SleepFrame, run_test
from test_bot import FakeTransport, completion_chunk, sse
from test_meeting_recorder import CallSignal, Hook

from agentic_meeting.pipeline import tools as tools_module
from agentic_meeting.pipeline.bot import (
    AppResources,
    build_parts,
    pipeline_processors,
    wire_assistant_recording,
)
from agentic_meeting.pipeline.services import build_realtime_llm
from agentic_meeting.pipeline.text_input import TextInputHandler
from agentic_meeting.pipeline.tools import (
    ITEM_MAX_CHARS,
    get_digest,
    look_at_screen,
    realtime_tools,
    recall,
    resolve_speaker,
    time_range,
)
from agentic_meeting.store.db import Store
from agentic_meeting.types import SPEAKER_ASSISTANT, Utterance


class Rig:
    """一场进行中的会议 + 假的工具调用参数。"""

    def __init__(self, cfg, store, session, tmp_path, now_secs=3600.0):
        self.cfg, self.store, self.session, self.tmp_path = cfg, store, session, tmp_path
        self.recorder = SimpleNamespace(elapsed_secs=now_secs)
        self.embed_calls: list[str] = []
        self.resources = SimpleNamespace(
            cfg=cfg,
            store=store,
            sessions=SimpleNamespace(live=SimpleNamespace(session=session, recorder=self.recorder)),
            embedder=None,
            frames=SimpleNamespace(path_of=lambda frame: tmp_path / frame.path),
        )
        self.results: list[dict] = []
        self.context = LLMContext()

    def params(self, tool_call_id="call_1"):
        async def result_callback(result, *, properties=None):
            self.results.append(result)

        return SimpleNamespace(
            app_resources=self.resources,
            result_callback=result_callback,
            context=self.context,
            tool_call_id=tool_call_id,
        )

    async def call(self, tool, **kwargs) -> dict:
        await tool(self.params(), **kwargs)
        return self.results[-1]

    async def say(self, text, t, speaker=1, source="asr"):
        u = Utterance(self.session.id, speaker, t, t + 2.0, text, source=source)
        await self.store.add_utterance(u)
        return u.id


@pytest.fixture
async def rig(make_cfg, tmp_path):
    cfg = make_cfg()
    store = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    session = await store.create_session(now=1000.0)
    yield Rig(cfg, store, session, tmp_path)
    await store.close()


# --------------------------------------------------------------------------- #
# 工具定义（给模型看的那部分）
# --------------------------------------------------------------------------- #


def test_tool_schemas_come_from_signatures_and_docstrings():
    context = LLMContext(tools=realtime_tools())
    params = OpenAILLMAdapter().get_llm_invocation_params(
        context, system_instruction="s", convert_developer_to_user=True
    )
    by_name = {t["function"]["name"]: t["function"] for t in params["tools"]}
    assert list(by_name) == ["recall", "get_digest", "look_at_screen"]  # 顺序固定，前缀缓存才稳定
    recall_schema = by_name["recall"]["parameters"]
    assert set(recall_schema["properties"]) == {
        "query",
        "speaker",
        "minutes_ago_from",
        "minutes_ago_to",
        "limit",
    }
    assert recall_schema["required"] == []  # 全部可选
    assert recall_schema["properties"]["minutes_ago_from"]["type"] == "number"
    assert recall_schema["properties"]["limit"]["type"] == "integer"
    assert all(p.get("description") for p in recall_schema["properties"].values())
    assert by_name["get_digest"]["parameters"]["properties"]["scope"]["type"] == "string"
    look = by_name["look_at_screen"]["parameters"]
    assert look["properties"]["frame_ids"]["type"] == "string" and look["required"] == []
    assert all(f["description"] for f in by_name.values())  # 描述是中文，来自文档字符串
    assert "params" not in json.dumps(params["tools"])


# --------------------------------------------------------------------------- #
# 时间换算与说话人
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("older", "newer", "expected"),
    [
        (0, 0, (None, None)),  # 不限
        (10, 0, (3000.0, None)),  # 十分钟以内
        (35, 25, (1500.0, 2100.0)),  # 半小时前后那一段
        (25, 35, (1500.0, 2100.0)),  # 给反了
        (0, 20, (None, 2400.0)),  # 二十分钟以前的
        (600, 0, (0.0, None)),  # 比会议还长：从头算
        ("10", None, (3000.0, None)),  # 模型给了字符串 / 空值
        (-5, "abc", (None, None)),
        (float("nan"), 0, (None, None)),
    ],
)
def test_time_range(older, newer, expected):
    assert time_range(3600.0, older, newer) == expected


async def test_resolve_speaker(rig):
    await rig.say("a", 1.0, speaker=1)
    await rig.say("b", 2.0, speaker=2)
    await rig.say("c", 3.0, speaker=SPEAKER_ASSISTANT, source="assistant")
    await rig.store.rename_speaker(rig.session.id, 1, "王老师")
    sid = rig.session.id
    assert (await resolve_speaker(rig.store, sid, "王老师"))[0] == 1
    assert (await resolve_speaker(rig.store, sid, " 王老师 "))[0] == 1
    assert (await resolve_speaker(rig.store, sid, "王"))[0] == 1  # 唯一包含它的
    assert (await resolve_speaker(rig.store, sid, "说话人2"))[0] == 2  # 忽略空格
    assert (await resolve_speaker(rig.store, sid, "nova"))[0] == SPEAKER_ASSISTANT
    idx, names = await resolve_speaker(rig.store, sid, "李老师")
    assert idx is None and names == ["Nova", "王老师", "说话人 2"]
    await rig.store.rename_speaker(sid, 2, "王同学")
    assert (await resolve_speaker(rig.store, sid, "王"))[0] is None  # 有歧义
    assert (await resolve_speaker(rig.store, sid, "王同学"))[0] == 2  # 精确匹配优先


# --------------------------------------------------------------------------- #
# recall
# --------------------------------------------------------------------------- #


async def test_recall_by_keyword_returns_time_speaker_text(rig):
    await rig.say("学习率是不是设大了", 842.0)
    await rig.say("中午吃什么", 900.0, speaker=2)
    await rig.store.rename_speaker(rig.session.id, 1, "王老师")
    result = await rig.call(recall, query="学习率")
    assert result == {
        "items": [{"time": "00:14:02", "speaker": "王老师", "text": "学习率是不是设大了"}]
    }


async def test_recall_filters_by_speaker_and_time(rig):
    await rig.say("很早的一句", 60.0, speaker=1)
    await rig.say("十分钟内的一句", 3300.0, speaker=1)
    await rig.say("别人十分钟内说的", 3400.0, speaker=2)
    by_speaker = await rig.call(recall, speaker="说话人 1")
    assert [i["text"] for i in by_speaker["items"]] == ["很早的一句", "十分钟内的一句"]
    recent = await rig.call(recall, minutes_ago_from=10)
    assert [i["text"] for i in recent["items"]] == ["十分钟内的一句", "别人十分钟内说的"]
    both = await rig.call(recall, speaker="说话人 1", minutes_ago_from=10)
    assert [i["text"] for i in both["items"]] == ["十分钟内的一句"]
    earlier = await rig.call(recall, minutes_ago_from=0, minutes_ago_to=30)
    assert [i["text"] for i in earlier["items"]] == ["很早的一句"]


async def test_recall_unknown_speaker_and_no_hits_explain_themselves(rig):
    await rig.say("一句话", 10.0)
    unknown = await rig.call(recall, speaker="李老师")
    assert unknown["items"] == [] and "李老师" in unknown["note"]
    assert unknown["speakers"] == ["说话人 1"]
    nothing = await rig.call(recall, query="不存在的词语")
    assert nothing == {"items": [], "note": "没有找到符合条件的发言"}


async def test_recall_limit_is_clamped_and_long_text_is_cut(rig):
    for i in range(40):
        await rig.say(f"第{i:02d}句", float(i))
    await rig.say("长" * (ITEM_MAX_CHARS + 100), 100.0)
    assert len((await rig.call(recall))["items"]) == 10  # 默认
    assert len((await rig.call(recall, limit=1000))["items"]) == 30
    assert len((await rig.call(recall, limit=0))["items"]) == 10  # 不合理的值按默认
    assert len((await rig.call(recall, limit="3"))["items"]) == 3
    last = (await rig.call(recall, limit=1))["items"][0]["text"]
    assert len(last) == ITEM_MAX_CHARS + 1 and last.endswith("…")


async def test_recall_uses_embedder_when_available(rig):
    await rig.say("把步长调小以后收敛慢了", 10.0)
    await rig.store.set_embeddings([(1, [1.0, 0.0, 0.0, 0.0])])

    class Embedder:
        max_distance = None  # 不设相关度门槛

        async def embed_query(self, text):
            rig.embed_calls.append(text)
            return [1.0, 0.0, 0.0, 0.0]

    rig.resources.embedder = Embedder()
    result = await rig.call(recall, query="  谁说过学习率要降低  ")
    assert rig.embed_calls == ["谁说过学习率要降低"]
    assert [i["text"] for i in result["items"]] == ["把步长调小以后收敛慢了"]


async def test_requests_to_the_assistant_are_not_meeting_content(rig):
    rig.cfg.session.assistant_name = "Nova"
    await rig.say("Nova 这个名字是上周定的", 100.0)  # 很早以前提到名字：算会议内容
    await rig.say("学习率调到了五乘十的负四次方", 3000.0)
    await rig.say("nova，刚才谁提到学习率", 3590.0)  # 正在对助理提的要求
    await rig.say("帮我查一下学习率", 3595.0, speaker=-2, source="text")  # 键入的要求
    await rig.say("学习率是王老师提的。", 3596.0, speaker=SPEAKER_ASSISTANT, source="assistant")
    result = await rig.call(recall, query="学习率")
    assert [i["text"] for i in result["items"]] == [
        "学习率调到了五乘十的负四次方",
        "学习率是王老师提的。",  # 助理自己说过的话算
    ]
    named = await rig.call(recall, query="Nova")
    assert [i["text"] for i in named["items"]] == ["Nova 这个名字是上周定的"]
    # 过滤之后仍然给够数
    one = await rig.call(recall, query="学习率", limit=1)
    assert [i["text"] for i in one["items"]] == ["学习率是王老师提的。"]

    since = await rig.call(get_digest)
    assert "nova，刚才谁提到学习率" not in [i["text"] for i in since["since_then"]]
    assert "帮我查一下学习率" not in [i["text"] for i in since["since_then"]]
    recent = await rig.call(get_digest, scope="recent")
    assert [i["text"] for i in recent["since_then"]] == ["学习率是王老师提的。"]


async def test_recall_only_searches_the_live_session(rig):
    other = await rig.store.create_session(now=500.0)
    await rig.store.add_utterance(Utterance(other.id, 1, 1.0, 2.0, "别的会议里的学习率"))
    assert (await rig.call(recall, query="学习率"))["items"] == []


# --------------------------------------------------------------------------- #
# get_digest
# --------------------------------------------------------------------------- #


async def test_get_digest_all_returns_latest_digest_and_later_lines(rig):
    first = await rig.say("纪要里已经有的", 100.0)
    await rig.store.add_digest(
        rig.session.id, t_from=0.0, t_to=102.0, text="旧版", last_utterance_id=first
    )
    await rig.store.add_digest(
        rig.session.id, t_from=102.0, t_to=2530.0, text="一、讨论了学习率", last_utterance_id=first
    )
    await rig.say("纪要之后说的", 2600.0, speaker=2)
    result = await rig.call(get_digest)
    assert result == {
        "digest": "一、讨论了学习率",
        "covers_until": "00:42:10",
        "since_then": [{"time": "00:43:20", "speaker": "说话人 2", "text": "纪要之后说的"}],
    }
    assert await rig.call(get_digest, scope="ALL ") == result


async def test_get_digest_before_any_digest_exists(rig):
    empty = await rig.call(get_digest, scope="all")
    assert empty["digest"] == "" and empty["since_then"] == [] and "刚开始" in empty["note"]
    await rig.say("第一句", 5.0)
    result = await rig.call(get_digest)
    assert result["digest"] == "" and result["covers_until"] == ""
    assert [i["text"] for i in result["since_then"]] == ["第一句"]
    assert "还没有生成纪要" in result["note"]


async def test_get_digest_caps_lines_after_digest_to_the_most_recent(rig):
    for i in range(tools_module.SINCE_DIGEST_MAX_ITEMS + 5):
        await rig.say(f"第{i:02d}句", float(i))
    result = await rig.call(get_digest)
    texts = [i["text"] for i in result["since_then"]]
    assert len(texts) == tools_module.SINCE_DIGEST_MAX_ITEMS
    assert texts[0] == "第05句" and texts[-1].startswith("第44")


async def test_get_digest_recent_returns_only_the_last_interval_verbatim(rig):
    rig.cfg.realtime.digest_interval_minutes = 5.0
    await rig.store.add_digest(
        rig.session.id, t_from=0.0, t_to=10.0, text="整场纪要", last_utterance_id=0
    )
    await rig.say("六分钟前", 3600.0 - 360)
    await rig.say("四分钟前", 3600.0 - 240)
    result = await rig.call(get_digest, scope="recent")
    assert result == {
        "digest": "",
        "covers_until": "",
        "since_then": [{"time": "00:56:00", "speaker": "说话人 1", "text": "四分钟前"}],
    }
    rig.recorder.elapsed_secs = 90000.0
    quiet = await rig.call(get_digest, scope="recent")
    assert quiet["since_then"] == [] and "没有发言" in quiet["note"]


# --------------------------------------------------------------------------- #
# look_at_screen
# --------------------------------------------------------------------------- #


async def add_frame(rig, t, caption=None, content=b"\x89img"):
    frame = await rig.store.add_frame(rig.session.id, t=t, width=16, height=9, suffix=".webp")
    path = rig.tmp_path / frame.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if caption is not None:
        await rig.store.set_frame_caption(frame.id, status="done", caption=caption)
    return frame


async def test_look_at_screen_without_frames_says_so(rig):
    result = await rig.call(look_at_screen)
    assert set(result) == {"note"} and "没有屏幕截图" in result["note"]
    assert rig.context.get_messages() == []


async def test_look_at_screen_adds_latest_image_to_context(rig):
    await add_frame(rig, 100.0, "旧的一页", b"old")
    await add_frame(rig, 3570.0, "幻灯片：消融实验结果表", b"newest")
    result = await rig.call(look_at_screen)
    assert result == {
        "time": "00:59:30",
        "seconds_ago": 30,
        "caption": "幻灯片：消融实验结果表",
        "earlier": [{"id": 1, "time": "00:01:40", "caption": "旧的一页"}],
        "note": "最新的截图已经附在后面；要看更早的某几张，带上 frame_ids 再调用一次",
    }
    (message,) = rig.context.get_messages()
    assert message["role"] == "user"
    text = next(p["text"] for p in message["content"] if p["type"] == "text")
    assert text == "[画面 00:59:30] 屏幕截图"
    image = next(p for p in message["content"] if p["type"] == "image_url")
    assert image["image_url"]["url"] == (
        "data:image/webp;base64," + base64.b64encode(b"newest").decode()
    )


async def test_look_at_screen_places_image_after_the_call_record(rig, monkeypatch):
    monkeypatch.setattr(tools_module, "IN_PROGRESS_WAIT_SECS", 1.0)
    await add_frame(rig, 10.0)

    async def aggregator_writes_record_later():
        await asyncio.sleep(0.05)
        rig.context.add_message({"role": "assistant", "tool_calls": [{"id": "call_7"}]})
        rig.context.add_message(
            {"role": "tool", "content": "IN_PROGRESS", "tool_call_id": "call_7"}
        )

    writer = asyncio.create_task(aggregator_writes_record_later())
    await look_at_screen(rig.params("call_7"))
    await writer
    assert [m["role"] for m in rig.context.get_messages()] == ["assistant", "tool", "user"]


async def test_look_at_screen_does_not_wait_forever_for_the_call_record(rig, monkeypatch):
    monkeypatch.setattr(tools_module, "IN_PROGRESS_WAIT_SECS", 0.05)
    await add_frame(rig, 10.0)
    await asyncio.wait_for(look_at_screen(rig.params("never")), 1.0)
    assert (
        len(rig.context.get_messages()) == 1 and rig.results[-1]["note"] == "最新的截图已经附在后面"
    )


async def test_look_at_screen_without_vision_returns_caption_only(rig):
    rig.cfg.realtime_llm.active.supports_vision = False
    await add_frame(rig, 10.0, "幻灯片：背景介绍")
    result = await rig.call(look_at_screen)
    assert result["caption"] == "幻灯片：背景介绍" and "不能看图" in result["note"]
    assert rig.context.get_messages() == []
    await add_frame(rig, 20.0)  # 还没有摘要
    result = await rig.call(look_at_screen)
    assert result["caption"] == "" and "不能看图" in result["note"]


async def test_look_at_screen_survives_a_missing_file_and_hides_irrelevant_caption(rig):
    frame = await add_frame(rig, 10.0, "无关画面")
    (rig.tmp_path / frame.path).unlink()
    result = await rig.call(look_at_screen)
    assert result["caption"] == "" and "读不到" in result["note"]
    assert rig.context.get_messages() == []


# --------------------------------------------------------------------------- #
# 外壳：没有会议、内部出错
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tool", [recall, get_digest, look_at_screen])
async def test_tools_explain_when_there_is_no_meeting(rig, tool):
    rig.resources.sessions.live = None
    assert await rig.call(tool) == {"note": "现在没有进行中的会议"}
    params = rig.params()
    params.app_resources = None
    await tool(params)
    assert rig.results[-1] == {"note": "现在没有进行中的会议"}


@pytest.mark.parametrize("tool", [recall, get_digest, look_at_screen])
async def test_tool_errors_become_an_error_result_not_an_exception(rig, tool):
    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("数据库坏了")

    rig.resources.store = Broken()
    result = await rig.call(tool)
    assert result == {"error": "这个工具暂时用不了"}


# --------------------------------------------------------------------------- #
# 真实的管线：模型发起工具调用 → 工具执行 → 带着结果再生成
# --------------------------------------------------------------------------- #


def tool_call_chunk(name: str, arguments: dict) -> dict:
    call = {
        "index": 0,
        "id": "call_abc",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }
    return completion_chunk({"role": "assistant", "tool_calls": [call]})


async def run_typed_request(cfg, store, session, tmp_path, tool_name, arguments, question):
    """走一遍：键入一个问题 → 模型第一次回答是工具调用 → 第二次回答是文字。返回两次请求的请求体。"""
    bodies: list[dict] = []

    def llm_handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            body = sse(
                tool_call_chunk(tool_name, arguments), completion_chunk({}, finish="tool_calls")
            )
        else:
            body = sse(
                completion_chunk({"role": "assistant", "content": "查到了。"}),
                completion_chunk({}, finish="stop"),
            )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    cfg.session.assistant_name = "Nova"
    cfg.tts.enabled = False
    cfg.turn.smart_turn = False
    llm = build_realtime_llm(
        cfg, "你是助理", http_client=httpx.AsyncClient(transport=httpx.MockTransport(llm_handler))
    )
    parts = build_parts(cfg, asr_backend=FakeASR(), llm=llm, store=store, session_id=session.id)
    wire_assistant_recording(parts)
    resources = AppResources(
        cfg,
        store=store,
        sessions=SimpleNamespace(live=SimpleNamespace(session=session, recorder=parts.recorder)),
        frames=SimpleNamespace(path_of=lambda frame: tmp_path / frame.path),
    )
    hook = Hook()

    async def notice(level, text):
        pass

    handler = TextInputHandler(
        recorder=parts.recorder, push=hook.push_frame, notice=notice, tts_enabled=False
    )

    async def attach_resources():
        # run_test 自己建 PipelineWorker，没法传 app_resources：管线起来之后补上
        parts.llm.pipeline_worker._app_resources = resources

    frames = [
        SleepFrame(sleep=0.1),
        CallSignal(attach_resources),
        CallSignal(lambda: handler.handle(question)),
        SleepFrame(sleep=1.5),
    ]
    await run_test(
        Pipeline([hook, *pipeline_processors(FakeTransport(), parts)]),
        frames_to_send=frames,
        pipeline_params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=24000),
    )
    await handler.close()
    return bodies


async def test_pipeline_offers_tools_and_feeds_the_recall_result_back(make_cfg, tmp_path):
    cfg = make_cfg()
    store = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    try:
        session = await store.create_session(now=1000.0)
        await store.add_utterance(Utterance(session.id, 1, 5.0, 7.0, "学习率是不是设大了"))
        bodies = await run_typed_request(
            cfg, store, session, tmp_path, "recall", {"query": "学习率"}, "刚才谁提到学习率"
        )
        assert len(bodies) == 2
        assert [t["function"]["name"] for t in bodies[0]["tools"]] == [
            "recall",
            "get_digest",
            "look_at_screen",
            "delegate_task",  # 后台任务开着时多三个任务工具
            "task_status",
            "cancel_task",
        ]
        assert bodies[1]["tools"] == bodies[0]["tools"]  # 两次请求的工具定义逐字一致
        roles = [m["role"] for m in bodies[1]["messages"]]
        assert roles[-2:] == ["assistant", "tool"]
        assert bodies[1]["messages"][-2]["tool_calls"][0]["function"]["name"] == "recall"
        result = json.loads(bodies[1]["messages"][-1]["content"])
        assert result == {
            "items": [{"time": "00:00:05", "speaker": "说话人 1", "text": "学习率是不是设大了"}]
        }
        # 助理最终的文字回答落库了
        said = [n.utterance.text for n in await store.list_utterances(session.id, tail=5)]
        assert said[-1] == "查到了。"
    finally:
        await store.close()


async def test_pipeline_look_at_screen_sends_the_image_right_after_the_tool_result(
    make_cfg, tmp_path
):
    cfg = make_cfg()
    store = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    try:
        session = await store.create_session(now=1000.0)
        frame = await store.add_frame(session.id, t=3.0, width=16, height=9, suffix=".webp")
        path = tmp_path / frame.path
        path.parent.mkdir(parents=True)
        path.write_bytes(b"pixels")
        bodies = await run_typed_request(
            cfg, store, session, tmp_path, "look_at_screen", {}, "看一下屏幕上的图"
        )
        assert len(bodies) == 2
        messages = bodies[1]["messages"]
        assert [m["role"] for m in messages[-3:]] == ["assistant", "tool", "user"]
        assert json.loads(messages[-2]["content"])["note"] == "最新的截图已经附在后面"
        image = next(p for p in messages[-1]["content"] if p["type"] == "image_url")
        assert image["image_url"]["url"].endswith(base64.b64encode(b"pixels").decode())
    finally:
        await store.close()


# --------------------------------------------------------------------------- #
# look_at_screen：回看之前的截图
# --------------------------------------------------------------------------- #


async def test_look_at_screen_lists_earlier_frames_for_the_model_to_choose_from(rig):
    await add_frame(rig, 60.0, "幻灯片：数据集划分", b"a")
    await add_frame(rig, 120.0, "幻灯片：数据集划分", b"a2")  # 画面没变的兜底截图：不重复列
    await add_frame(rig, 180.0, "无关画面", b"desktop")
    await add_frame(rig, 240.0, None, b"no-caption")  # 没有摘要的，模型没法据此判断，不列
    await add_frame(rig, 300.0, "幻灯片：消融实验结果表", b"b")
    await add_frame(rig, 3570.0, "幻灯片：结论", b"latest")
    result = await rig.call(look_at_screen)
    assert result["caption"] == "幻灯片：结论"
    assert result["earlier"] == [
        {"id": 1, "time": "00:01:00", "caption": "幻灯片：数据集划分"},
        {"id": 5, "time": "00:05:00", "caption": "幻灯片：消融实验结果表"},
    ]
    assert len(rig.context.get_messages()) == 1  # 只附了最新的一张


async def test_look_at_screen_with_frame_ids_attaches_those_earlier_frames(rig):
    first = await add_frame(rig, 60.0, "幻灯片：数据集划分", b"a")
    second = await add_frame(rig, 300.0, "幻灯片：消融实验结果表", b"b")
    await add_frame(rig, 3570.0, "幻灯片：结论", b"latest")
    result = await rig.call(look_at_screen, frame_ids=f"{second.id}, {first.id}")
    assert result == {
        "frames": [
            {"id": first.id, "time": "00:01:00", "caption": "幻灯片：数据集划分"},
            {"id": second.id, "time": "00:05:00", "caption": "幻灯片：消融实验结果表"},
        ],
        "note": "2 张截图已经附在后面，看过再回答",
    }
    messages = rig.context.get_messages()
    urls = [
        p["image_url"]["url"] for m in messages for p in m["content"] if p["type"] == "image_url"
    ]
    assert urls == [
        "data:image/webp;base64," + base64.b64encode(data).decode() for data in (b"a", b"b")
    ]
    texts = [p["text"] for m in messages for p in m["content"] if p["type"] == "text"]
    assert texts[0] == f"[画面 00:01:00] 之前的屏幕截图（编号 {first.id}）"


async def test_look_at_screen_looks_back_at_no_more_than_three_and_only_this_meeting(rig):
    frames = [await add_frame(rig, 60.0 * i, f"第 {i} 页", bytes([i])) for i in range(1, 6)]
    ids = ",".join(str(f.id) for f in frames)
    result = await rig.call(look_at_screen, frame_ids=ids)
    assert len(result["frames"]) == 3 and "一次最多看 3 张" in result["note"]
    assert len(rig.context.get_messages()) == 3

    result = await rig.call(look_at_screen, frame_ids="9999, abc")
    assert set(result) == {"note"} and "没有这些编号的截图" in result["note"]


async def test_look_at_screen_without_vision_ignores_frame_ids(rig):
    frame = await add_frame(rig, 60.0, "幻灯片：数据集划分", b"a")
    await add_frame(rig, 3570.0, "幻灯片：结论", b"latest")
    rig.cfg.realtime_llm.active.supports_vision = False
    result = await rig.call(look_at_screen, frame_ids=str(frame.id))
    assert result["caption"] == "幻灯片：结论" and "不能看图" in result["note"]
    assert rig.context.get_messages() == []
