# OpenAI Agents SDK 集成说明

[English](../agents-sdk-notes.md) · **简体中文**

本文说明后台 agent（`src/agentic_meeting/agent/`）对 OpenAI Agents SDK **0.23.1** 的用法。文中描述的行为均已对照
已安装的源码核实，并由 `tests/test_agent_runner.py` 覆盖（用 `httpx.MockTransport` 模拟远端模型）。
另有两条标记为 `gpu` 的测试，使用真实的远端模型、MCP 服务和 Docker 沙箱。

安装：`uv sync --extra agent`（`openai-agents[docker]`）。未安装时应用仍可启动，委托的任务会失败并说明原因
（SDK 的对象都在函数内部导入）。

## 1. 基本用法

```python
from agents import Agent, ModelSettings, OpenAIChatCompletionsModel, RunConfig, Runner
from openai import AsyncOpenAI

model = OpenAIChatCompletionsModel(
    model=cfg.agent.model,
    openai_client=AsyncOpenAI(base_url=cfg.agent.base_url, api_key=...),
)
agent = Agent(
    name="研究助理",
    instructions=system_prompt,
    model=model,
    model_settings=ModelSettings(extra_body=cfg.agent.extra_body or None),
    mcp_servers=[...],
    tools=[...],
)
result = Runner.run_streamed(
    agent, agent_input, max_turns=cfg.agent.max_turns, run_config=RunConfig(tracing_disabled=True)
)
async for event in result.stream_events():
    ...
answer = result.final_output          # 未设置 output_type 时为字符串
```

- **追踪。** SDK 默认会把追踪数据发送到 OpenAI。每次运行都传入 `RunConfig(tracing_disabled=True)`，
  这样不必修改全局状态。
- **chat completions。** 显式选用 `OpenAIChatCompletionsModel`。SDK 的默认值是 Responses API，
  多数 OpenAI 兼容的服务并不支持。请求体是标准的 `model / stream / messages / tools`，`stream` 为真。
- `instructions` 成为第一条 `system` 消息。
- `ModelSettings.extra_body` 会并入每个请求；`agent.extra_body` 通过它传递，通常用于指定思考强度。
- 用尽 `max_turns` 时抛出 `agents.exceptions.MaxTurnsExceeded`。
- 取消：`result.cancel()`（默认 `mode="immediate"`）停止在途的模型请求和工具调用。外层任务被取消时需要显式调用。
- 远端模型的错误以 `openai` 包的异常形式抛出：`APIConnectionError`、`AuthenticationError`、
  带 `status_code` 的 `APIStatusError`。SDK 自身的错误继承自 `agents.exceptions.AgentsException`。
  遇到 401 时，运行器会区分「密钥被拒绝」和「环境变量根本没有设置」两种情况。
- **工具调用编号必须唯一。** 同一次运行中重复的 `call_id` 会引发 `ModelBehaviorError`。

## 2. 输入中的图片

输入是 Responses 风格的条目列表，由 SDK 转换为 chat completions 的格式：

```python
[{"role": "user", "content": [
    {"type": "input_text", "text": "..."},
    {"type": "input_image", "image_url": "data:image/webp;base64,...", "detail": "auto"},
]}]
```

发出的请求中对应为 `{"type": "text", …}` 和
`{"type": "image_url", "image_url": {"url": …, "detail": "auto"}}`。

## 3. 流式事件

`result.stream_events()` 产出三类事件，由 `event.type` 区分：

| `type`                       | 含义                                  | 用途           |
| ---------------------------- | ------------------------------------- | -------------- |
| `raw_response_event`         | 模型的原始流式片段                    | 不使用         |
| `agent_updated_stream_event` | 当前 agent 发生变化（开始时也有一次） | 不使用         |
| `run_item_stream_event`      | 一个完整的条目，见 `event.name`       | 转换为进度事件 |

用到的两种 `run_item_stream_event`：

- `tool_called` —— `event.item` 是 `ToolCallItem`。`item.raw_item` 包含 `name`、`arguments`（JSON 字符串）
  和 `call_id`；`item.tool_origin.mcp_server_name` 非空时表示 MCP 工具。
- `tool_output` —— `event.item` 是 `ToolCallOutputItem`。`item.output` 是返回值；`item.raw_item` 是字典，
  其中有 `call_id` 但**没有工具名**，因此 `EventTranslator` 自行维护「调用编号 → 工具名」的映射。

其他条目（`message_output_created`、`reasoning_item_created`、`mcp_list_tools` 等）不向使用者报告。

本地工具是用 `@function_tool` 装饰的 `async def` 函数，其签名和文档字符串构成工具定义。

## 4. MCP

```python
from agents.mcp import MCPServerStreamableHttp

server = MCPServerStreamableHttp(
    params={"url": …, "headers": {…}, "timeout": seconds},
    name="search",
    client_session_timeout_seconds=seconds,     # 默认的 5 秒对检索工具来说太短
    cache_tools_list=True,
)
await server.connect()
…                                               # Agent(mcp_servers=[server])
await server.cleanup()
```

- 连接失败时抛出的通常是 `ExceptionGroup`，真正的原因被包在「unhandled errors in a TaskGroup」之内。
  `root_cause()` 会逐层展开到最内层的异常，用于日志和进度事件。
- 各个服务逐一调用 `connect()` 连接，而不使用 `async with`，这样**某个服务不可达不会影响其他服务**：
  跳过该服务，记录一条进度提示，并在输入中告知 agent。
- 请求头的值从环境变量读取；变量未设置时不发送该请求头。
- 连接按任务创建，任务结束时清理。

## 5. 沙箱

```python
from agents.sandbox import SandboxAgent, SandboxRunConfig
from agents.sandbox.sandboxes.docker import DockerSandboxClient, DockerSandboxClientOptions

client = DockerSandboxClient(docker.from_env())
session = await client.create(options=DockerSandboxClientOptions(image=…, network_mode="none"))
await session.start()
…
agent = SandboxAgent(name=…, instructions=…, model=…, mcp_servers=…)
Runner.run_streamed(agent, agent_input, run_config=RunConfig(sandbox=SandboxRunConfig(session=session), …))
…
await session.aclose()          # 会话自身的清理
await client.delete(session)    # 删除容器；aclose() 不会删除
```

- **工作区位于沙箱内部**（`Manifest.root`，默认为 `/workspace`），不挂载宿主机目录。本项目不使用 `Manifest` 的条目：
  会话启动后，用 `session.mkdir(path, parents=True)` 和 `session.write(path, io.BytesIO(data))` 把截图写入
  `input/`，任务结束后用 `session.read(path)` 取回产物。路径可以是相对于工作区根目录的相对路径。
- `SandboxAgent.capabilities` 默认为 `[Filesystem(), Shell(), Compaction()]`，**无法用于 chat completions**：
  `Filesystem` 的 `apply_patch` 工具是自由格式的 `CustomTool`，仅 Responses API 支持，请求在发出之前就会失败
  （`UserError: Hosted tools are not supported with the ChatCompletions API`）。本项目只传入 `[Shell()]`
  （`agent/sandbox.py` 中的 `sandbox_capabilities()`）。可用的工具是 `exec_command`，以及会话支持伪终端时的
  `write_stdin`；文件的写入和查看通过 shell 完成。有一条测试把每个工具转换为 chat completions 的格式，
  并断言 SDK 的默认配置仍然无法转换，以便在 SDK 行为变化时及时发现。
- 通过 `session=` 传入的会话由调用方负责其生命周期，运行器不会关闭它。
- `DockerSandboxClientOptions.network_mode` 只能是 `"none"` 或不设置；`agent.sandbox.network = false`
  对应 `"none"`。
- **单条命令的超时由模型指定**（`exec_command` 的 `yield_time_ms` 参数）。SDK 没有全局上限，
  `agent.sandbox.timeout_secs` 目前尚未接入；整个任务的时限由 `agent.task_timeout_secs` 控制。
- `UnixLocalSandboxClient` 在 Windows 上导入时即抛出 `ImportError`。运行器会先检查平台，
  并提示改用 `docker` 或 `none`。
- 沙箱无法启动（未安装或未运行 Docker、未配置镜像、启动失败）不会导致任务失败：运行器退回普通的 `Agent`，
  记录一条进度提示，并告知 agent 本次无法运行代码（architecture.md §9）。

标记为 `gpu` 的测试（`uv run pytest -m gpu tests/test_agent_runner.py -s`）验证容器能够启动、
`exec_command` 能够运行 Python、产物能够取回，以及任务结束后容器被删除。
