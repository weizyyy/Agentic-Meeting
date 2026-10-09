"""agent 运行器：把一个后台任务跑完（docs/interfaces.md §8.2、§8.3，docs/agents-sdk-notes.md）。

任务管理器（``agent/tasks.py``）调用 ``AgentRunner.__call__(task, on_event)``，这里做的事：

1. **组装输入**：任务目标 + 委托时那段时间的会议转录 + 截图（模型能看图时作为图片附上；原件也放进工作目录）。
2. **接上工具**：配置里的每个 MCP 服务器一个连接（连不上的跳过并说明）；代码执行沙箱（开不起来就退回到只能检索和读图）。
3. **驱动 agent 循环**（OpenAI Agents SDK 的 ``Runner.run_streamed``），把流式事件翻译成一句中文的进度。
4. **解析最终回答**为 ``TaskResult``，把产物文件从沙箱取回任务目录。

可以直接告诉用户的失败原因用 ``RunnerError`` 抛出（远端模型连不上、密钥无效、步骤太多……）。

输入组装、结果解析、事件翻译都是纯函数，单独可测。SDK 的对象在函数里才导入：没装 ``agent`` extra 时
应用照样能启动，只是委托任务会失败并说明原因。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from pathlib import Path
from typing import Any

from loguru import logger

from agentic_meeting.agent.sandbox import (
    INPUT_DIR,
    SandboxHandle,
    SandboxUnavailable,
    open_sandbox,
    safe_artifact_name,
    sandbox_capabilities,
)
from agentic_meeting.agent.tasks import OnEvent, RunnerError, task_dir
from agentic_meeting.config import AppConfig, MCPServerConfig, SandboxConfig, secret
from agentic_meeting.pipeline.clock import context_line, format_hms
from agentic_meeting.screen.ingest import media_type_of
from agentic_meeting.store.db import Store
from agentic_meeting.types import NamedUtterance, TaskRecord, TaskResult

AGENT_NAME = "研究助理"
MAX_TRANSCRIPT_LINES = 400
FALLBACK_BRIEF = "任务已完成，详情见任务面板"
SUMMARY_MAX_CHARS = 60  # 进度里引用的查询词、工具名最多这么长
QUERY_KEYS = ("query", "q", "keywords", "keyword", "question", "search", "text", "prompt", "url")
SANDBOX_TOOL_SUMMARIES = {
    "exec_command": ("正在运行一段代码", "代码运行完成"),
    "write_stdin": ("正在向运行中的程序输入", "输入完成"),
    "apply_patch": ("正在写文件", "文件已写好"),
    "view_image": ("正在查看图片", "图片看完了"),
}

SandboxOpener = Callable[[SandboxConfig], AbstractAsyncContextManager[SandboxHandle | None]]


# --------------------------------------------------------------------------- #
# 输入组装
# --------------------------------------------------------------------------- #


def transcript_lines(utterances: Sequence[NamedUtterance]) -> list[str]:
    """转录片段，格式同实时模型上下文里的行：``[时:分:秒 说话人] 文字``。"""
    return [context_line(n.utterance.t_start, n.speaker_name, n.utterance.text) for n in utterances]


def build_input(
    goal: str,
    lines: Sequence[str],
    images: Sequence[tuple[str, str]],
    *,
    image_names: Sequence[str] = (),
    can_run_code: bool,
    notes: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """给 agent 的首条用户消息（interfaces.md §8.2）：目标、相关转录、截图。

    ``images`` 是 ``(文件名, data URL)``，作为图片附在消息里；``image_names`` 是放进工作目录 ``input/`` 的截图文件名
    （模型不能看图时只有文件、没有附图）。``notes`` 是要让 agent 知道的限制（比如这次不能运行代码）。
    """
    parts = [f"# 任务目标\n\n{goal.strip()}"]
    if lines:
        parts.append("# 相关的会议转录\n\n" + "\n".join(lines))
    else:
        parts.append("# 相关的会议转录\n\n（这段时间没有转录。）")
    if image_names:
        listed = "、".join(f"{INPUT_DIR}/{name}" for name in image_names)
        attached = "已附在这条消息里，" if images else "你看不到图片本身，但"
        where = f"原件在工作目录的 {listed}。" if can_run_code else ""
        parts.append(
            f"# 屏幕截图\n\n共 {len(image_names)} 张，按时间先后排列，{attached}{where}".rstrip(
                "，"
            )
        )
    if notes:
        parts.append("# 这次的限制\n\n" + "\n".join(f"- {note}" for note in notes))
    content: list[dict[str, Any]] = [{"type": "input_text", "text": "\n\n".join(parts)}]
    for _name, url in images:
        content.append({"type": "input_image", "image_url": url, "detail": "auto"})
    return [{"role": "user", "content": content}]


# --------------------------------------------------------------------------- #
# 结果解析
# --------------------------------------------------------------------------- #

_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n(.*)\n```\s*$", re.DOTALL)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def parse_result(text: Any) -> TaskResult:
    """agent 的最终回答 → ``TaskResult``。

    要求是一个 JSON 对象（提示词里规定）。模型有时会用代码块包起来，或在前后加一句话：先去掉代码块标记，
    再取第一个 ``{`` 到最后一个 ``}`` 之间的部分。仍然解析不了、或者没有 ``brief`` 时，
    把原始回答整体作为 ``detail_md``，``brief`` 用一句固定的话。
    """
    raw = text if isinstance(text, str) else ("" if text is None else str(text))
    candidate = raw.strip()
    fenced = _FENCE.match(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    start, end = candidate.find("{"), candidate.rfind("}")
    data: Any = None
    if start != -1 and end > start:
        try:
            # strict=False：模型常把详细结果里的换行直接写进字符串（没有转义），照样认
            data = json.loads(candidate[start : end + 1], strict=False)
        except ValueError:
            data = None
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("brief"), str)
        or not data["brief"].strip()
    ):
        return TaskResult(brief=FALLBACK_BRIEF, detail_md=raw.strip())
    detail = data.get("detail_md")
    artifacts = [n for a in _string_list(data.get("artifacts")) if (n := safe_artifact_name(a))]
    return TaskResult(
        brief=" ".join(data["brief"].split()),
        detail_md=detail.strip() if isinstance(detail, str) else "",
        sources=_string_list(data.get("sources")),
        artifacts=artifacts,
    )


# --------------------------------------------------------------------------- #
# 事件翻译（interfaces.md §8.3）
# --------------------------------------------------------------------------- #


def _clip(text: str) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= SUMMARY_MAX_CHARS else one_line[:SUMMARY_MAX_CHARS] + "…"


def _arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def describe_tool_call(name: str, arguments: Any, *, server: str | None = None) -> tuple[str, dict]:
    """工具调用 → ``(一句中文, 细节)``。``server`` 是 MCP 服务器的名字（不是 MCP 工具时为 ``None``）。"""
    args = _arguments(arguments)
    payload: dict[str, Any] = {"tool": name, "arguments": args}
    if server:
        payload["server"] = server
    if name in SANDBOX_TOOL_SUMMARIES:
        return SANDBOX_TOOL_SUMMARIES[name][0], payload
    for key in QUERY_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            verb = "正在打开" if key == "url" else "正在检索"
            return f"{verb}「{_clip(value)}」", payload
    return f"正在调用工具 {_clip(name)}", payload


def describe_tool_output(name: str, output: Any) -> tuple[str, dict]:
    """工具返回 → ``(一句中文, 细节)``。只说个大概，不把结果原文念出来。"""
    text = (
        output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
    )
    payload = {"tool": name, "chars": len(text)}
    if name in SANDBOX_TOOL_SUMMARIES:
        if "timed out" in text:
            return "代码运行超时", payload
        return SANDBOX_TOOL_SUMMARIES[name][1], payload
    if not text.strip():
        return "工具没有返回内容", payload
    lowered = text[:200].lower()
    if lowered.startswith(("error", "an error occurred")) or '"error"' in lowered:
        return "工具返回了错误", payload
    return f"工具返回了结果（约 {len(text)} 字）", payload


class EventTranslator:
    """把 SDK 的流式事件翻译成进度事件。记着每个调用编号对应的工具名，返回时才知道是谁的结果。"""

    def __init__(self) -> None:
        self._names: dict[str, str] = {}

    def translate(self, event: Any) -> tuple[str, str, dict] | None:
        """返回 ``(kind, summary, payload)``；不值得报的事件返回 ``None``。"""
        if getattr(event, "type", None) != "run_item_stream_event":
            return None
        item = event.item
        raw = getattr(item, "raw_item", None)
        if event.name == "tool_called":
            name = _field(raw, "name") or "工具"
            call_id = _field(raw, "call_id") or _field(raw, "id")
            if call_id:
                self._names[str(call_id)] = name
            origin = getattr(item, "tool_origin", None)
            summary, payload = describe_tool_call(
                name, _field(raw, "arguments"), server=getattr(origin, "mcp_server_name", None)
            )
            return "tool_call", summary, payload
        if event.name == "tool_output":
            call_id = _field(raw, "call_id")
            name = self._names.get(str(call_id), "工具")
            summary, payload = describe_tool_output(name, getattr(item, "output", None))
            return "tool_result", summary, payload
        return None


def _field(raw: Any, name: str) -> Any:
    if isinstance(raw, dict):
        return raw.get(name)
    return getattr(raw, name, None)


# --------------------------------------------------------------------------- #
# 运行器
# --------------------------------------------------------------------------- #


def mcp_headers(server: MCPServerConfig) -> dict[str, str]:
    """认证头：配置里写的是 ``头名 → 环境变量名``，值在这里读；没设的环境变量不发这个头。"""
    headers = {}
    for header, env_name in server.headers_env.items():
        value = secret(env_name)
        if value:
            headers[header] = value
    return headers


class AgentRunner:
    def __init__(
        self,
        cfg: AppConfig,
        store: Store,
        *,
        system_prompt: str,
        openai_client: Any = None,
        mcp_servers: Sequence[Any] | None = None,
        extra_tools: Sequence[Any] = (),
        sandbox_opener: SandboxOpener = open_sandbox,
    ) -> None:
        """``openai_client``、``mcp_servers``、``extra_tools``、``sandbox_opener`` 可注入（测试用）。

        不注入 ``mcp_servers`` 时按配置为每个任务新建连接；注入的列表原样交给 agent，由调用方负责连接和清理。
        """
        self._cfg = cfg
        self._store = store
        self._prompt = system_prompt
        self._client = openai_client
        self._injected_mcp = mcp_servers
        self._extra_tools = list(extra_tools)
        self._open_sandbox = sandbox_opener
        self._data_dir = cfg.resolve(cfg.session.data_dir)

    async def __call__(self, task: TaskRecord, on_event: OnEvent) -> TaskResult:
        agent_cfg = self._cfg.agent
        if not agent_cfg.enabled:
            raise RunnerError("后台任务功能已在配置里关闭")
        if self._client is None and not (agent_cfg.base_url and agent_cfg.model):
            raise RunnerError(
                "后台 agent 还没有配置（config.toml 的 [agent] 里要填模型地址和名字）"
            )
        try:
            from agents import Agent, ModelSettings, OpenAIChatCompletionsModel, RunConfig, Runner
            from agents.exceptions import AgentsException, MaxTurnsExceeded
        except ImportError as e:
            raise RunnerError("没有安装后台 agent 的依赖（uv sync --extra agent）") from e
        import openai

        workdir = task_dir(self._data_dir, task)
        await asyncio.to_thread(workdir.mkdir, parents=True, exist_ok=True)
        files = await self._screenshots(task)
        if files:
            await asyncio.to_thread(_write_inputs, workdir / INPUT_DIR, files)
        utterances = await self._store.recall(
            task.session_id, t_from=task.t_from, t_to=task.t_to, limit=MAX_TRANSCRIPT_LINES
        )

        async with AsyncExitStack() as stack:
            notes: list[str] = []
            sandbox = await self._enter_sandbox(stack, on_event, notes)
            if sandbox is not None:
                await sandbox.put_inputs(files)
            servers = await self._enter_mcp(stack, on_event, notes)

            images = (
                [(name, _data_url(name, data)) for name, data in files.items()]
                if agent_cfg.supports_vision
                else []
            )
            agent_input = build_input(
                task.goal,
                transcript_lines(utterances),
                images,
                image_names=list(files),
                can_run_code=sandbox is not None,
                notes=notes,
            )
            model = OpenAIChatCompletionsModel(
                model=agent_cfg.model or "agent", openai_client=self._client or self._new_client()
            )
            common: dict[str, Any] = {
                "name": AGENT_NAME,
                # 配置里给这个模型的附加字段（一般是思考的强度），每次请求都带上
                "model_settings": ModelSettings(extra_body=dict(agent_cfg.extra_body) or None),
                "instructions": self._prompt,
                "model": model,
                "mcp_servers": servers,
                "tools": list(self._extra_tools),
            }
            run_config_kwargs: dict[str, Any] = {"tracing_disabled": True}
            if sandbox is not None:
                from agents.sandbox import SandboxAgent, SandboxRunConfig

                agent: Any = SandboxAgent(**common, capabilities=sandbox_capabilities())
                run_config_kwargs["sandbox"] = SandboxRunConfig(session=sandbox.session)
            else:
                agent = Agent(**common)

            result = Runner.run_streamed(
                agent,
                agent_input,
                max_turns=agent_cfg.max_turns,
                run_config=RunConfig(**run_config_kwargs),
            )
            translator = EventTranslator()
            try:
                async for event in result.stream_events():
                    translated = translator.translate(event)
                    if translated is not None:
                        await on_event(*translated)
            except asyncio.CancelledError:
                result.cancel()  # 任务被取消或超时：让 SDK 把在途的模型请求和工具调用停掉
                raise
            except MaxTurnsExceeded as e:
                raise RunnerError(f"步骤太多（超过 {agent_cfg.max_turns} 步）还没有做完") from e
            except openai.AuthenticationError as e:
                raise RunnerError(self._key_problem()) from e
            except openai.APIConnectionError as e:
                raise RunnerError("连不上远端模型") from e
            except openai.APIStatusError as e:
                raise RunnerError(f"远端模型返回了错误（{e.status_code}）") from e
            except AgentsException as e:
                logger.warning(f"agent 运行出错：{type(e).__name__}: {e}")
                raise RunnerError(f"agent 运行出错（{type(e).__name__}）") from e

            parsed = parse_result(result.final_output)
            if parsed.artifacts and sandbox is not None:
                parsed.artifacts = await sandbox.fetch(parsed.artifacts, workdir)
            else:
                parsed.artifacts = []  # 没有代码执行环境就不可能有产物文件
            return parsed

    # ---- 内部 ----

    def _key_problem(self) -> str:
        """远端模型说没有权限（401）时给用户的说明：先看是不是环境变量根本没设。"""
        env_name = self._cfg.agent.api_key_env
        if not env_name:
            return "远端模型要求密钥，但配置里没有写密钥的环境变量名（agent.api_key_env）"
        if not secret(env_name):
            return f"远端模型的密钥没有设置（环境变量 {env_name} 是空的；设好之后要重新启动）"
        return f"远端模型不接受这个密钥（环境变量 {env_name}）"

    def _new_client(self) -> Any:
        from openai import AsyncOpenAI

        agent_cfg = self._cfg.agent
        return AsyncOpenAI(
            base_url=agent_cfg.base_url, api_key=secret(agent_cfg.api_key_env) or "none"
        )

    async def _screenshots(self, task: TaskRecord) -> dict[str, bytes]:
        """任务带的截图：``{文件名: 内容}``，按编号顺序。读不到的跳过。"""
        files: dict[str, bytes] = {}
        root = self._data_dir.resolve()
        for frame_id in task.frame_ids:
            frame = await self._store.get_frame(frame_id)
            if frame is None or frame.session_id != task.session_id:
                continue
            path = (root / frame.path).resolve()
            if not path.is_relative_to(root):
                continue
            try:
                data = await asyncio.to_thread(path.read_bytes)
            except OSError:
                logger.warning(f"任务 {task.label} 的截图读不到：{frame.path}")
                continue
            files[f"screen_{format_hms(frame.t).replace(':', '')}_{frame_id}{path.suffix}"] = data
        return files

    async def _enter_sandbox(
        self, stack: AsyncExitStack, on_event: OnEvent, notes: list[str]
    ) -> SandboxHandle | None:
        if self._cfg.agent.sandbox.kind == "none":
            notes.append(
                "这次没有代码执行环境：不能运行代码、不能生成文件。需要计算或画图的部分请如实说明做不了。"
            )
            return None
        try:
            return await stack.enter_async_context(self._open_sandbox(self._cfg.agent.sandbox))
        except SandboxUnavailable as e:
            await on_event("note", f"{e}，这次只能检索和读图", None)
            notes.append(
                f"这次没有代码执行环境（{e}）：不能运行代码、不能生成文件。需要计算或画图的部分请如实说明做不了。"
            )
            return None

    async def _enter_mcp(
        self, stack: AsyncExitStack, on_event: OnEvent, notes: list[str]
    ) -> list[Any]:
        if self._injected_mcp is not None:
            return list(self._injected_mcp)
        if not self._cfg.agent.mcp_servers:
            return []
        from agents.mcp import MCPServerStreamableHttp

        servers: list[Any] = []
        for server_cfg in self._cfg.agent.mcp_servers:
            server = MCPServerStreamableHttp(
                params={
                    "url": server_cfg.url,
                    "headers": mcp_headers(server_cfg),
                    "timeout": server_cfg.timeout_secs,
                },
                name=server_cfg.name,
                client_session_timeout_seconds=server_cfg.timeout_secs,
                cache_tools_list=True,
            )
            try:
                await server.connect()
            except Exception as e:
                reason = root_cause(e)
                logger.warning(f"MCP 服务器 {server_cfg.name}（{server_cfg.url}）连不上：{reason}")
                await on_event(
                    "note", f"检索服务 {server_cfg.name} 连不上（{reason}），这次不用它", None
                )
                notes.append(f"检索服务 {server_cfg.name} 这次不可用。")
                try:
                    await server.cleanup()
                except Exception:
                    pass
                continue
            stack.push_async_callback(_cleanup_mcp, server)
            servers.append(server)
        return servers


def root_cause(error: BaseException) -> str:
    """异常的简短说明。异步库常把真正的原因包在 ``ExceptionGroup`` 里，这里一直剥到最里面那个。"""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status is not None:
        return f"服务器返回 {status}"
    text = " ".join(str(error).split())
    if len(text) > 120:
        text = text[:120] + "…"
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


async def _cleanup_mcp(server: Any) -> None:
    try:
        await server.cleanup()
    except Exception as e:
        logger.warning(f"断开 MCP 服务器时出错：{type(e).__name__}: {e}")


def _write_inputs(directory: Path, files: dict[str, bytes]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (directory / name).write_bytes(data)


def _data_url(name: str, data: bytes) -> str:
    return f"data:{media_type_of(name)};base64,{base64.b64encode(data).decode('ascii')}"


def build_runner(
    cfg: AppConfig, store: Store, system_prompt: str
) -> Callable[..., Awaitable[TaskResult]]:
    """按配置创建运行器（给 ``TaskManager`` 用）。"""
    return AgentRunner(cfg, store, system_prompt=system_prompt)
