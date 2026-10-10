"""agent 运行器。

纯函数（输入组装、结果解析、事件翻译）单独测；整体流程用真实的 Agents SDK + ``httpx.MockTransport`` 模拟远端模型的
chat completions（一次工具调用 + 一次最终回答）。沙箱用假的——真实的 Docker 沙箱在标了 ``gpu`` 的测试里。
"""

from __future__ import annotations

import asyncio
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from agents import function_tool
from openai import AsyncOpenAI

from agentic_meeting.agent import sandbox as sandbox_module
from agentic_meeting.agent.runner import (
    FALLBACK_BRIEF,
    AgentRunner,
    EventTranslator,
    build_input,
    describe_tool_call,
    describe_tool_output,
    mcp_headers,
    parse_result,
    transcript_lines,
)
from agentic_meeting.agent.sandbox import (
    SandboxUnavailable,
    SdkSandbox,
    open_sandbox,
    safe_artifact_name,
)
from agentic_meeting.agent.tasks import RunnerError, TaskFailed, TaskManager
from agentic_meeting.config import MCPServerConfig, SandboxConfig
from agentic_meeting.store.db import Store
from agentic_meeting.types import NamedUtterance, TaskResult, Utterance

# --------------------------------------------------------------------------- #
# 输入组装
# --------------------------------------------------------------------------- #


def test_transcript_lines_use_the_context_format():
    items = [NamedUtterance(Utterance("s", 1, 842.0, 845.0, "学习率是不是大了"), "王老师")]
    assert transcript_lines(items) == ["[00:14:02 王老师] 学习率是不是大了"]


def test_build_input_has_goal_transcript_and_images():
    (message,) = build_input(
        "  核实这篇论文的引用数  ",
        ["[00:14:02 王老师] 这篇引用过千了吧"],
        [("screen_001402_7.webp", "data:image/webp;base64,AAAA")],
        image_names=["screen_001402_7.webp"],
        can_run_code=True,
    )
    assert message["role"] == "user"
    text, image = message["content"]
    assert text["type"] == "input_text"
    assert "# 任务目标\n\n核实这篇论文的引用数" in text["text"]
    assert "# 相关的会议转录\n\n[00:14:02 王老师] 这篇引用过千了吧" in text["text"]
    assert "共 1 张" in text["text"] and "input/screen_001402_7.webp" in text["text"]
    assert "这次的限制" not in text["text"]
    assert image == {
        "type": "input_image",
        "image_url": "data:image/webp;base64,AAAA",
        "detail": "auto",
    }


def test_build_input_without_transcript_images_or_code():
    (message,) = build_input("算一下", [], [], can_run_code=False, notes=["这次没有代码执行环境。"])
    (text,) = message["content"]
    assert "（这段时间没有转录。）" in text["text"]
    assert "屏幕截图" not in text["text"]
    assert "# 这次的限制\n\n- 这次没有代码执行环境。" in text["text"]


def test_build_input_when_the_model_cannot_see():
    (message,) = build_input("看图", [], [], image_names=["a.webp", "b.webp"], can_run_code=True)
    (text,) = message["content"]  # 没有图片附件
    assert "共 2 张" in text["text"] and "你看不到图片本身" in text["text"]
    assert "input/a.webp、input/b.webp" in text["text"]


# --------------------------------------------------------------------------- #
# 结果解析
# --------------------------------------------------------------------------- #

GOOD = {
    "brief": "这篇论文目前被引 1243 次",
    "detail_md": "## 结论\n被引 1243 次。",
    "sources": ["https://example.org/a", " ", 5],
    "artifacts": ["plot.png", "../etc/passwd", "/workspace/out/fig.png"],
}


def test_parse_result_plain_json():
    result = parse_result(json.dumps(GOOD, ensure_ascii=False))
    assert result == TaskResult(
        brief="这篇论文目前被引 1243 次",
        detail_md="## 结论\n被引 1243 次。",
        sources=["https://example.org/a"],
        artifacts=["plot.png", "out/fig.png"],  # 不合规的文件名被去掉
    )


@pytest.mark.parametrize(
    "wrap",
    [
        lambda s: f"```json\n{s}\n```",
        lambda s: f"```\n{s}\n```",
        lambda s: f"好的，结果如下：\n{s}\n以上。",
        lambda s: f"\n\n  {s}  \n",
    ],
)
def test_parse_result_tolerates_fences_and_chatter(wrap):
    result = parse_result(wrap(json.dumps(GOOD, ensure_ascii=False)))
    assert result.brief == "这篇论文目前被引 1243 次" and result.sources == [
        "https://example.org/a"
    ]


@pytest.mark.parametrize(
    "text",
    [
        "我查了一下，大概一千多次。",
        '{"detail_md": "没有 brief"}',
        '{"brief": "   "}',
        '{"brief": 5}',
        "[1, 2, 3]",
        '{"brief": "没写完',
        "",
    ],
)
def test_parse_result_falls_back_to_raw_text(text):
    result = parse_result(text)
    assert result == TaskResult(brief=FALLBACK_BRIEF, detail_md=text.strip())


def test_parse_result_minimal_and_odd_inputs():
    assert parse_result('{"brief": "办好了\\n两行"}') == TaskResult(brief="办好了 两行")
    assert parse_result(None) == TaskResult(brief=FALLBACK_BRIEF, detail_md="")
    odd = parse_result('{"brief": "好", "detail_md": 5, "sources": "x", "artifacts": null}')
    assert odd == TaskResult(brief="好")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("plot.png", "plot.png"),
        (" out/fig 1.png ", "out/fig 1.png"),
        ("./plot.png", "plot.png"),
        ("/workspace/plot.png", "plot.png"),
        ("out\\fig.png", "out/fig.png"),
        ("../secret", None),
        ("a/../../b", None),
        ("/etc/passwd", None),
        ("C:/Windows/x", None),
        ("", None),
        ("   ", None),
    ],
)
def test_safe_artifact_name(name, expected):
    assert safe_artifact_name(name) == expected


# --------------------------------------------------------------------------- #
# 事件翻译
# --------------------------------------------------------------------------- #


def test_describe_tool_call():
    summary, payload = describe_tool_call(
        "web_search", '{"query": "对比学习 温度系数"}', server="search"
    )
    assert summary == "正在检索「对比学习 温度系数」"
    assert payload == {
        "tool": "web_search",
        "arguments": {"query": "对比学习 温度系数"},
        "server": "search",
    }
    assert describe_tool_call("fetch", {"url": "https://example.org/p"})[0] == (
        "正在打开「https://example.org/p」"
    )
    assert describe_tool_call("exec_command", '{"cmd": "python plot.py"}')[0] == "正在运行一段代码"
    assert describe_tool_call("apply_patch", "{}")[0] == "正在写文件"
    assert describe_tool_call("lookup", "不是 JSON")[0] == "正在调用工具 lookup"
    assert describe_tool_call("lookup", '["a"]')[1]["arguments"] == {}
    long = describe_tool_call("s", {"query": "很长" * 100})[0]
    assert len(long) < 80 and long.endswith("…」")
    assert "\n" not in describe_tool_call("s", {"query": "两行\n查询"})[0]


def test_describe_tool_output():
    assert describe_tool_output("web_search", "x" * 1200) == (
        "工具返回了结果（约 1200 字）",
        {"tool": "web_search", "chars": 1200},
    )
    assert describe_tool_output("web_search", "")[0] == "工具没有返回内容"
    assert describe_tool_output("web_search", "Error: timeout")[0] == "工具返回了错误"
    assert describe_tool_output("web_search", {"error": "not found"})[0] == "工具返回了错误"
    assert describe_tool_output("web_search", {"items": [1, 2]})[0].startswith("工具返回了结果")
    assert describe_tool_output("exec_command", "saved plot.png")[0] == "代码运行完成"
    assert describe_tool_output("exec_command", "Command timed out after 10.000 seconds.")[0] == (
        "代码运行超时"
    )


def run_item(name, item):
    return SimpleNamespace(type="run_item_stream_event", name=name, item=item)


def test_translator_pairs_outputs_with_their_calls():
    translator = EventTranslator()
    origin = SimpleNamespace(mcp_server_name="search")
    call = SimpleNamespace(
        raw_item=SimpleNamespace(name="web_search", call_id="c1", arguments='{"q": "LoRA"}'),
        tool_origin=origin,
    )
    code = SimpleNamespace(
        raw_item=SimpleNamespace(name="exec_command", call_id="c2", arguments="{}"),
        tool_origin=None,
    )
    assert translator.translate(run_item("tool_called", call)) == (
        "tool_call",
        "正在检索「LoRA」",
        {"tool": "web_search", "arguments": {"q": "LoRA"}, "server": "search"},
    )
    assert translator.translate(run_item("tool_called", code))[1] == "正在运行一段代码"
    out_code = SimpleNamespace(raw_item={"call_id": "c2"}, output="done")
    out_search = SimpleNamespace(raw_item={"call_id": "c1"}, output="五条结果")
    assert translator.translate(run_item("tool_output", out_code))[:2] == (
        "tool_result",
        "代码运行完成",
    )
    assert translator.translate(run_item("tool_output", out_search))[2]["tool"] == "web_search"
    unknown = SimpleNamespace(raw_item={"call_id": "zz"}, output="x")
    assert translator.translate(run_item("tool_output", unknown))[2]["tool"] == "工具"
    # 不值得报的事件
    assert translator.translate(run_item("message_output_created", SimpleNamespace())) is None
    assert translator.translate(SimpleNamespace(type="raw_response_event")) is None
    assert translator.translate(SimpleNamespace(type="agent_updated_stream_event")) is None


def test_mcp_headers_read_values_from_environment(monkeypatch):
    monkeypatch.setenv("FAKE_MCP_KEY", "secret-value")
    monkeypatch.delenv("FAKE_MISSING", raising=False)
    server = MCPServerConfig(
        name="search",
        url="https://mcp.example/mcp",
        headers_env={"Authorization": "FAKE_MCP_KEY", "X-Other": "FAKE_MISSING"},
    )
    assert mcp_headers(server) == {"Authorization": "secret-value"}  # 没设的变量不发这个头


# --------------------------------------------------------------------------- #
# 整体流程：真实的 Agents SDK + 模拟的远端模型
# --------------------------------------------------------------------------- #


def chunk(delta, finish=None):
    return {
        "id": "c",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def sse(*chunks):
    body = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks)
    return httpx.Response(
        200, content=body + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
    )


def tool_call(name, arguments, call_id="call_1"):
    call = {
        "index": 0,
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }
    return sse(chunk({"role": "assistant", "tool_calls": [call]}), chunk({}, "tool_calls"))


def answer(text):
    return sse(chunk({"role": "assistant", "content": text}), chunk({}, "stop"))


class Remote:
    """模拟的远端模型：按顺序给出预先排好的回答，记下每次请求。"""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.bodies: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def client(self) -> AsyncOpenAI:
        return AsyncOpenAI(
            base_url="http://agent.test/v1",
            api_key="k",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
        )


@function_tool
async def web_search(query: str) -> str:
    """检索网页。"""
    return f"关于「{query}」的 5 条结果"


class FakeSandbox:
    """假的沙箱：记下放进去的输入；取产物时从预先准备的文件里拿。"""

    def __init__(self, files=None):
        self.session = object()
        self.inputs: dict[str, bytes] = {}
        self.files = files or {}
        self.closed = False

    async def put_inputs(self, files):
        self.inputs.update(files)

    async def fetch(self, names, dest: Path, *, write=None):
        got = []
        for name in names:
            if name in self.files:
                (dest / name).write_bytes(self.files[name])
                got.append(name)
        return got


class Env:
    def __init__(self, cfg, store, session, tmp_path):
        self.cfg, self.store, self.session, self.tmp_path = cfg, store, session, tmp_path
        self.events: list[tuple[str, str, dict | None]] = []
        self.data_dir = Path(cfg.resolve(cfg.session.data_dir))

    async def on_event(self, kind, summary, payload=None):
        self.events.append((kind, summary, payload))

    async def task(self, goal="核实引用数", **kw):
        return await self.store.create_task(self.session.id, goal=goal, **kw)

    def runner(self, remote, **kw):
        kw.setdefault("mcp_servers", [])
        kw.setdefault("extra_tools", [web_search])
        return AgentRunner(
            self.cfg, self.store, system_prompt="你是研究助理", openai_client=remote.client(), **kw
        )

    def summaries(self):
        return [(kind, summary) for kind, summary, _ in self.events]


@pytest.fixture
async def env(make_cfg, tmp_path):
    cfg = make_cfg()
    cfg.agent.base_url, cfg.agent.model = "http://agent.test/v1", "fake-agent-model"
    cfg.agent.sandbox.kind = "none"
    cfg.agent.max_turns = 5
    store = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    session = await store.create_session(now=1000.0)
    yield Env(cfg, store, session, tmp_path)
    await store.close()


FINAL = json.dumps(
    {
        "brief": "这篇论文目前被引 1243 次",
        "detail_md": "## 结论\n1243 次",
        "sources": ["https://example.org/a"],
        "artifacts": [],
    },
    ensure_ascii=False,
)


async def test_tool_call_then_final_answer(env):
    await env.store.add_utterance(Utterance(env.session.id, 1, 100.0, 102.0, "这篇引用过千了吧"))
    await env.store.add_utterance(Utterance(env.session.id, 1, 900.0, 902.0, "范围之外的话"))
    task = await env.task(t_from=0.0, t_to=300.0)
    remote = Remote(tool_call("web_search", {"query": "论文 引用数"}), answer(FINAL))
    result = await env.runner(remote)(task, env.on_event)

    assert result == TaskResult(
        brief="这篇论文目前被引 1243 次",
        detail_md="## 结论\n1243 次",
        sources=["https://example.org/a"],
        artifacts=[],
    )
    assert env.summaries() == [
        ("tool_call", "正在检索「论文 引用数」"),
        ("tool_result", "工具返回了结果（约 17 字）"),
    ]
    first, second = remote.bodies
    assert first["model"] == "fake-agent-model" and first["stream"] is True
    assert first["messages"][0] == {"role": "system", "content": "你是研究助理"}
    user_text = first["messages"][1]["content"][0]["text"]
    assert "核实引用数" in user_text and "[00:01:40 说话人 1] 这篇引用过千了吧" in user_text
    assert "范围之外的话" not in user_text
    assert "没有代码执行环境" in user_text  # sandbox.kind = none：告诉 agent 这次不能跑代码
    assert [t["function"]["name"] for t in first["tools"]] == ["web_search"]
    assert [m["role"] for m in second["messages"]] == ["system", "user", "assistant", "tool"]
    assert second["messages"][-1]["content"] == "关于「论文 引用数」的 5 条结果"
    assert (env.data_dir / "sessions" / env.session.id / "tasks" / "t1").is_dir()


async def test_task_requests_carry_the_configured_extra_body(env):
    """配置里给后台模型的附加字段（一般是思考的强度）每次请求都带上；没配就不带。"""
    env.cfg.agent.extra_body = {"reasoning_effort": "medium"}
    remote = Remote(tool_call("web_search", {"query": "论文 引用数"}), answer(FINAL))
    await env.runner(remote)(await env.task(), env.on_event)
    assert [body.get("reasoning_effort") for body in remote.bodies] == ["medium", "medium"]

    env.cfg.agent.extra_body = {}
    plain = Remote(answer(FINAL))
    await env.runner(plain)(await env.task(), env.on_event)
    assert "reasoning_effort" not in plain.bodies[0]


async def test_screenshots_are_attached_and_copied_into_the_workdir(env):
    frame = await env.store.add_frame(env.session.id, t=842.0, width=16, height=9, suffix=".webp")
    path = env.data_dir / frame.path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"pixels")
    other_session = await env.store.create_session()
    foreign = await env.store.add_frame(other_session.id, t=1.0, width=1, height=1, suffix=".webp")
    task = await env.task(frame_ids=[frame.id, foreign.id, 9999])
    remote = Remote(answer(FINAL))
    await env.runner(remote)(task, env.on_event)

    name = f"screen_001402_{frame.id}.webp"
    workdir = env.data_dir / "sessions" / env.session.id / "tasks" / "t1"
    assert (workdir / "input" / name).read_bytes() == b"pixels"
    assert [p.name for p in (workdir / "input").iterdir()] == [name]  # 别的会议的截图不带
    content = remote.bodies[0]["messages"][1]["content"]
    images = [p for p in content if p["type"] == "image_url"]
    assert len(images) == 1 and images[0]["image_url"]["url"].startswith("data:image/webp;base64,")
    assert "共 1 张" in content[0]["text"]

    # 模型不能看图：不附图片，只说明有截图
    env.cfg.agent.supports_vision = False
    blind = Remote(answer(FINAL))
    await env.runner(blind)(await env.task(frame_ids=[frame.id]), env.on_event)
    content = blind.bodies[0]["messages"][1]["content"]
    text = content if isinstance(content, str) else content[0]["text"]
    assert "image_url" not in json.dumps(blind.bodies[0]) and "你看不到图片本身" in text


async def test_sandbox_gets_inputs_and_artifacts_are_fetched_back(env):
    env.cfg.agent.sandbox.kind = "docker"
    frame = await env.store.add_frame(env.session.id, t=5.0, width=16, height=9, suffix=".webp")
    path = env.data_dir / frame.path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"pixels")
    box = FakeSandbox(files={"plot.png": b"\x89PNG"})
    opened: list[SandboxConfig] = []

    @asynccontextmanager
    async def opener(cfg):
        opened.append(cfg)
        yield box
        box.closed = True

    final = json.dumps(
        {
            "brief": "画好了",
            "detail_md": "",
            "sources": [],
            "artifacts": ["plot.png", "missing.png"],
        }
    )
    seen: dict = {}

    class Recording(AgentRunner):
        pass

    remote = Remote(answer(final))
    runner = env.runner(remote, sandbox_opener=opener)
    # 用假的沙箱会话时 SDK 的 SandboxAgent 跑不起来，这里只验证运行器自己的部分：输入、取回、清理
    import agents.sandbox as sdk_sandbox

    original = sdk_sandbox.SandboxAgent

    def fake_sandbox_agent(**kwargs):
        from agents import Agent

        seen["sandbox_agent"] = True
        seen["capabilities"] = kwargs.pop("capabilities")
        return Agent(**kwargs)

    sdk_sandbox.SandboxAgent = fake_sandbox_agent
    original_config = sdk_sandbox.SandboxRunConfig
    sdk_sandbox.SandboxRunConfig = lambda session: seen.setdefault("session", session) and None
    try:
        result = await runner(await env.task(frame_ids=[frame.id]), env.on_event)
    finally:
        sdk_sandbox.SandboxAgent = original
        sdk_sandbox.SandboxRunConfig = original_config

    assert seen["sandbox_agent"] and seen["session"] is box.session
    # 只给 Shell：SDK 默认带的 apply_patch 走不了 chat completions（见下面的回归测试）
    assert [type(c).__name__ for c in seen["capabilities"]] == ["Shell"]
    assert opened == [env.cfg.agent.sandbox] and box.closed
    assert list(box.inputs) == [f"screen_000005_{frame.id}.webp"]
    assert result.artifacts == ["plot.png"]  # 取不回来的不算产物
    workdir = env.data_dir / "sessions" / env.session.id / "tasks" / "t1"
    assert (workdir / "plot.png").read_bytes() == b"\x89PNG"
    text = remote.bodies[0]["messages"][1]["content"][0]["text"]
    assert "这次的限制" not in text and "原件在工作目录的 input/" in text


async def test_unavailable_sandbox_degrades_to_search_only(env):
    env.cfg.agent.sandbox.kind = "docker"

    @asynccontextmanager
    async def opener(cfg):
        raise SandboxUnavailable("连不上 Docker（没有安装，或者没有启动）")
        yield

    final = json.dumps({"brief": "查到了", "artifacts": ["plot.png"]})
    remote = Remote(answer(final))
    result = await env.runner(remote, sandbox_opener=opener)(await env.task(), env.on_event)
    assert result.brief == "查到了" and result.artifacts == []  # 没有沙箱就没有产物
    assert env.summaries() == [
        ("note", "连不上 Docker（没有安装，或者没有启动），这次只能检索和读图")
    ]
    text = remote.bodies[0]["messages"][1]["content"]
    text = text if isinstance(text, str) else text[0]["text"]
    assert "连不上 Docker" in text and "不能运行代码" in text


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (httpx.ConnectError("refused"), "连不上远端模型"),
        (
            httpx.Response(401, json={"error": {"message": "bad key"}}),
            "远端模型不接受这个密钥（环境变量 FAKE_AGENT_KEY）",
        ),
        (httpx.Response(500, json={"error": {"message": "boom"}}), "远端模型返回了错误（500）"),
    ],
)
async def test_remote_model_failures_become_runner_errors(env, reply, reason, monkeypatch):
    env.cfg.agent.api_key_env = "FAKE_AGENT_KEY"
    monkeypatch.setenv("FAKE_AGENT_KEY", "sk-wrong")
    with pytest.raises(RunnerError) as e:
        await env.runner(Remote(reply))(await env.task(), env.on_event)
    assert str(e.value) == reason


async def test_unauthorized_explains_a_missing_key_variable(env, monkeypatch):
    """实测时遇到的 401：最常见的原因是密钥的环境变量在这个终端里根本没设。"""
    unauthorized = httpx.Response(401, json={"error": "Unauthorized"})
    env.cfg.agent.api_key_env = "FAKE_AGENT_KEY"
    monkeypatch.delenv("FAKE_AGENT_KEY", raising=False)
    with pytest.raises(RunnerError) as e:
        await env.runner(Remote(unauthorized))(await env.task(), env.on_event)
    assert "环境变量 FAKE_AGENT_KEY 是空的" in str(e.value)
    env.cfg.agent.api_key_env = ""
    with pytest.raises(RunnerError) as e:
        await env.runner(Remote(unauthorized))(await env.task(), env.on_event)
    assert "agent.api_key_env" in str(e.value)


def test_sandbox_tools_can_all_be_sent_over_chat_completions():
    """回归：SDK 默认的沙箱能力里有 apply_patch，它不是函数工具，chat completions 发不出去。"""
    from agents.models.chatcmpl_converter import Converter
    from agents.sandbox.capabilities.capabilities import Capabilities

    from agentic_meeting.agent.sandbox import sandbox_capabilities

    class Session:
        def supports_pty(self):
            return True

    def names(capabilities):
        out = []
        for capability in capabilities:
            capability.bind(Session())
            for tool in capability.tools():
                Converter.tool_to_openai(tool)  # 转不了会抛 UserError
                out.append(tool.name)
        return out

    assert names(sandbox_capabilities()) == ["exec_command", "write_stdin"]
    with pytest.raises(Exception, match="not supported with the ChatCompletions API"):
        names(Capabilities.default())  # SDK 的默认确实不行——哪天它行了，这里会提醒我们可以放开


def test_root_cause_unwraps_exception_groups():
    from agentic_meeting.agent.runner import root_cause

    inner = httpx.ConnectError("All connection attempts failed")
    group = ExceptionGroup("unhandled errors in a TaskGroup", [ExceptionGroup("x", [inner])])
    assert root_cause(group) == "ConnectError: All connection attempts failed"
    response = httpx.Response(401, request=httpx.Request("POST", "http://x"))
    status = httpx.HTTPStatusError("401", request=response.request, response=response)
    assert root_cause(ExceptionGroup("g", [status])) == "服务器返回 401"
    assert root_cause(TimeoutError()) == "TimeoutError"
    assert root_cause(ValueError("很长" * 200)).endswith("…")


async def test_too_many_turns_is_reported(env):
    env.cfg.agent.max_turns = 2
    remote = Remote(
        *[tool_call("web_search", {"query": f"第{i}次"}, f"call_{i}") for i in range(5)]
    )
    with pytest.raises(RunnerError) as e:
        await env.runner(remote)(await env.task(), env.on_event)
    assert "超过 2 步" in str(e.value)


async def test_unparseable_final_answer_falls_back(env):
    remote = Remote(answer("我查了一下，大概一千多次。"))
    result = await env.runner(remote)(await env.task(), env.on_event)
    assert result == TaskResult(brief=FALLBACK_BRIEF, detail_md="我查了一下，大概一千多次。")


async def test_configuration_problems_are_explained(env):
    env.cfg.agent.enabled = False
    with pytest.raises(RunnerError, match="已在配置里关闭"):
        await env.runner(Remote())(await env.task(), env.on_event)
    env.cfg.agent.enabled = True
    env.cfg.agent.base_url = ""
    unconfigured = AgentRunner(env.cfg, env.store, system_prompt="x")
    with pytest.raises(RunnerError, match="还没有配置"):
        await unconfigured(await env.task(), env.on_event)


async def test_cancelling_the_task_stops_the_run(env):
    started = asyncio.Event()

    @function_tool
    async def slow_search(query: str) -> str:
        """很慢的检索。"""
        started.set()
        await asyncio.sleep(30)
        return "不会走到这里"

    remote = Remote(tool_call("slow_search", {"query": "x"}), answer(FINAL))
    runner = env.runner(remote, extra_tools=[slow_search])
    running = asyncio.create_task(runner(await env.task(), env.on_event))
    await asyncio.wait_for(started.wait(), 5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 5)
    assert len(remote.bodies) == 1  # 没有再去请求模型


async def test_runner_under_the_task_manager(env):
    """和任务管理器接起来：进度落库、结果入库、失败原因如实记录。"""
    messages: list[dict] = []

    async def notify(_sid, data):
        messages.append(data)

    remote = Remote(
        tool_call("web_search", {"query": "引用数"}), answer(FINAL), httpx.ConnectError("refused")
    )
    manager = TaskManager(
        store=env.store, runner=env.runner(remote), notify=notify, max_concurrent=1, timeout_secs=30
    )
    ok = await manager.submit(session_id=env.session.id, goal="核实引用数")
    assert (await manager.wait(ok.id)).brief == "这篇论文目前被引 1243 次"
    events = [e.summary for e in await env.store.list_task_events(ok.id)]
    assert events == ["开始处理", "正在检索「引用数」", "工具返回了结果（约 14 字）", "已完成"]
    bad = await manager.submit(session_id=env.session.id, goal="再查一个")
    with pytest.raises(TaskFailed) as e:
        await manager.wait(bad.id)
    assert e.value.reason == "连不上远端模型"
    await manager.close()


# --------------------------------------------------------------------------- #
# 沙箱模块（不起真的容器）
# --------------------------------------------------------------------------- #


class FakeSession:
    def __init__(self, files=None):
        self.files: dict[str, bytes] = dict(files or {})
        self.dirs: list[str] = []
        self.calls: list[str] = []

    async def start(self):
        self.calls.append("start")

    async def aclose(self):
        self.calls.append("aclose")

    async def mkdir(self, path, *, parents=False):
        self.dirs.append(Path(path).as_posix())

    async def write(self, path, data):
        self.files[Path(path).as_posix()] = data.read()

    async def read(self, path):
        key = Path(path).as_posix()
        if key not in self.files:
            raise FileNotFoundError(key)
        return io.BytesIO(self.files[key])


async def test_sdk_sandbox_puts_inputs_and_fetches_artifacts(tmp_path, monkeypatch):
    session = FakeSession({"plot.png": b"png", "out/data.csv": b"a,b", "big.bin": b"x" * 50})
    box = SdkSandbox(session)
    await box.put_inputs({})
    assert session.dirs == []  # 没有输入就不建目录
    await box.put_inputs({"screen_1.webp": b"img"})
    assert session.dirs == ["input"] and session.files["input/screen_1.webp"] == b"img"

    monkeypatch.setattr(sandbox_module, "MAX_ARTIFACT_BYTES", 10)
    got = await box.fetch(
        ["plot.png", "/workspace/out/data.csv", "missing.png", "../x", "big.bin"], tmp_path
    )
    assert got == ["plot.png", "out/data.csv"]
    assert (tmp_path / "plot.png").read_bytes() == b"png"
    assert (tmp_path / "out" / "data.csv").read_bytes() == b"a,b"
    assert not (tmp_path / "big.bin").exists()


async def test_open_sandbox_none_and_misconfiguration():
    async with open_sandbox(SandboxConfig(kind="none")) as box:
        assert box is None
    with pytest.raises(SandboxUnavailable, match="没有配置沙箱镜像"):
        async with open_sandbox(SandboxConfig(kind="docker", docker_image="")):
            pass


async def test_open_sandbox_creates_starts_and_always_cleans_up(monkeypatch):
    session = FakeSession()
    log: list[str] = []

    class FakeClient:
        async def create(self, *, options):
            log.append(f"create:{options}")
            return session

        async def delete(self, s):
            assert s is session
            log.append("delete")

    async def fake_client_and_options(cfg):
        return FakeClient(), "opts"

    monkeypatch.setattr(sandbox_module, "_client_and_options", fake_client_and_options)
    with pytest.raises(RuntimeError):
        async with open_sandbox(SandboxConfig(kind="docker", docker_image="fake-image")) as box:
            assert box.session is session and session.calls == ["start"]
            raise RuntimeError("任务出错")
    assert session.calls == ["start", "aclose"] and log == ["create:opts", "delete"]

    # 启动失败 → SandboxUnavailable，不是别的异常
    class Broken(FakeClient):
        async def create(self, *, options):
            raise OSError("image not found")

    async def broken_client(cfg):
        return Broken(), "opts"

    monkeypatch.setattr(sandbox_module, "_client_and_options", broken_client)
    with pytest.raises(SandboxUnavailable, match="启动失败"):
        async with open_sandbox(SandboxConfig(kind="docker", docker_image="fake-image")):
            pass


async def test_local_sandbox_is_refused_on_windows(monkeypatch):
    monkeypatch.setattr(sandbox_module.sys, "platform", "win32")
    with pytest.raises(SandboxUnavailable, match="不支持 Windows"):
        async with open_sandbox(SandboxConfig(kind="local")):
            pass


# --------------------------------------------------------------------------- #
# 真实的远端模型、MCP 和沙箱（需要外部服务，默认不跑：uv run pytest -m gpu tests/test_agent_runner.py）
# --------------------------------------------------------------------------- #


async def _real_run(goal: str, tmp_path, *, sandbox_kind: str | None = None):
    from agentic_meeting.config import load_config
    from agentic_meeting.pipeline.prompts import load_prompt

    cfg = load_config()
    if not (cfg.agent.enabled and cfg.agent.base_url and cfg.agent.model):
        pytest.skip("config.toml 的 [agent] 还没有配置")
    cfg.session.data_dir = str(tmp_path / "data")
    if sandbox_kind is not None:
        cfg.agent.sandbox.kind = sandbox_kind
    store = await Store.open(tmp_path / "meetings.db", cfg.embedding.dimensions)
    events: list[tuple[str, str]] = []

    async def on_event(kind, summary, payload=None):
        events.append((kind, summary))
        print(f"  [{kind}] {summary}")

    try:
        session = await store.create_session()
        task = await store.create_task(session.id, goal=goal)
        runner = AgentRunner(cfg, store, system_prompt=load_prompt("agent_system"))
        result = await asyncio.wait_for(runner(task, on_event), cfg.agent.task_timeout_secs)
    finally:
        await store.close()
    print(f"\nbrief: {result.brief}\nsources: {result.sources}\nartifacts: {result.artifacts}")
    print(result.detail_md[:800])
    return cfg, result, events


@pytest.mark.gpu
async def test_real_remote_model_answers_a_simple_task(tmp_path):
    """远端模型能按要求的 JSON 格式交卷（不带沙箱；配置了 MCP 的话可能会去检索）。"""
    _cfg, result, _events = await _real_run(
        "用一句话说明什么是对比学习里的温度系数，以及调大它一般有什么影响。",
        tmp_path,
        sandbox_kind="none",
    )
    assert result.brief and result.brief != FALLBACK_BRIEF  # 是按格式回答的，不是兜底


@pytest.mark.gpu
async def test_real_sandbox_runs_code_and_returns_an_artifact(tmp_path):
    """按配置的沙箱跑一段代码、画一张图，产物能取回任务目录。"""
    cfg, result, events = await _real_run(
        "用 Python 画出 y = x 的平方在 x 从 -3 到 3 之间的曲线，保存成 plot.png。", tmp_path
    )
    if cfg.agent.sandbox.kind == "none":
        pytest.skip("config.toml 里 agent.sandbox.kind = none")
    assert any(kind == "tool_call" and "代码" in summary for kind, summary in events)
    assert "plot.png" in result.artifacts
    workdir = Path(cfg.resolve(cfg.session.data_dir)) / "sessions"
    assert list(workdir.rglob("plot.png"))


def test_parse_result_accepts_raw_newlines_inside_strings():
    """实测遇到的：模型把详细结果里的换行直接写进了 JSON 字符串，严格解析会失败，结果整段原样进了任务面板。"""
    newline = chr(10)
    detail = newline.join(["一、结论", "", "二、数据", "昆山 115"])
    text = '{"brief": "数字读出来了", "detail_md": "' + detail + '", "sources": []}'
    with pytest.raises(ValueError):
        json.loads(text)  # 严格的 JSON 解析不认字符串里没转义的换行
    assert parse_result(text) == TaskResult(brief="数字读出来了", detail_md=detail)
