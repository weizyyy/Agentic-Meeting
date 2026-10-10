# OpenAI Agents SDK integration notes

**English** · [简体中文](zh-CN/agents-sdk-notes.md)

How the background agent (`src/agentic_meeting/agent/`) uses the OpenAI Agents SDK **0.23.1**. The
behavior described here was checked against the installed source. `tests/e2e/test_assistant.py`
runs delegated tasks end to end against a simulated OpenAI-compatible endpoint, and two tests in
`tests/test_real_services.py` marked `gpu` exercise a real remote model, an MCP server and the
sandbox.

Install with `uv sync --extra agent` (`openai-agents[docker]`). Without it the application still
starts; delegated tasks fail with an explanation, because SDK objects are imported inside functions.

## 1. Minimal shape

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
answer = result.final_output          # a string when no output_type is set
```

- **Tracing.** The SDK sends traces to OpenAI by default. Every run passes
  `RunConfig(tracing_disabled=True)`, which avoids changing global state.
- **Chat completions.** `OpenAIChatCompletionsModel` is selected explicitly. The SDK's default is the
  Responses API, which most OpenAI-compatible servers do not implement. The request body is the
  standard `model / stream / messages / tools` with `stream` set.
- `instructions` becomes the first `system` message.
- `ModelSettings.extra_body` is merged into every request; `agent.extra_body` uses it, typically for
  the reasoning effort.
- Exhausting `max_turns` raises `agents.exceptions.MaxTurnsExceeded`.
- Cancellation: `result.cancel()` (default `mode="immediate"`) stops the in-flight model request and
  tool calls. It has to be called explicitly when the surrounding task is cancelled.
- Errors from the remote model surface as `openai` exceptions — `APIConnectionError`,
  `AuthenticationError`, `APIStatusError` with `status_code`. The SDK's own errors derive from
  `agents.exceptions.AgentsException`. On a 401 the runner distinguishes a rejected key from an
  environment variable that was never set.
- **Tool call ids must be unique** within a run; a repeated `call_id` raises `ModelBehaviorError`.

## 2. Images in the input

The input is a list of Responses-style items, which the SDK converts to the chat completions format:

```python
[{"role": "user", "content": [
    {"type": "input_text", "text": "..."},
    {"type": "input_image", "image_url": "data:image/webp;base64,...", "detail": "auto"},
]}]
```

The request then contains `{"type": "text", …}` and
`{"type": "image_url", "image_url": {"url": …, "detail": "auto"}}`.

## 3. Stream events

`result.stream_events()` yields three kinds of events, distinguished by `event.type`:

| `type`                       | Meaning                                           | Use                             |
| ---------------------------- | ------------------------------------------------- | ------------------------------- |
| `raw_response_event`         | Raw streaming chunk from the model                | Ignored                         |
| `agent_updated_stream_event` | The active agent changed (also once at the start) | Ignored                         |
| `run_item_stream_event`      | A complete item; see `event.name`                 | Translated into progress events |

Two `run_item_stream_event` names are used:

- `tool_called` — `event.item` is a `ToolCallItem`. `item.raw_item` has `name`, `arguments` (a JSON
  string) and `call_id`; a non-empty `item.tool_origin.mcp_server_name` identifies an MCP tool.
- `tool_output` — `event.item` is a `ToolCallOutputItem`. `item.output` is the return value and
  `item.raw_item` is a dictionary with `call_id` but **no tool name**, so `EventTranslator` keeps
  its own map from call id to tool name.

Other items (`message_output_created`, `reasoning_item_created`, `mcp_list_tools`, …) are not
reported to the user.

Local tools are `async def` functions decorated with `@function_tool`; the signature and docstring
form the tool definition.

## 4. MCP

```python
from agents.mcp import MCPServerStreamableHttp

server = MCPServerStreamableHttp(
    params={"url": …, "headers": {…}, "timeout": seconds},
    name="search",
    client_session_timeout_seconds=seconds,     # the default of 5 s is too short for search tools
    cache_tools_list=True,
)
await server.connect()
…                                               # Agent(mcp_servers=[server])
await server.cleanup()
```

- A failed connection usually raises an `ExceptionGroup` that hides the cause behind "unhandled
  errors in a TaskGroup". `root_cause()` unwraps it to the innermost exception for the log and the
  progress event.
- Servers are connected one by one with `connect()` rather than `async with`, so that **one
  unreachable server does not affect the others**: it is skipped, a progress note is recorded and
  the agent is told in its input.
- Header values are read from environment variables; a header whose variable is unset is not sent.
- Connections are created per task and cleaned up when the task ends.

## 5. Sandbox

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
await session.aclose()          # the session's own cleanup
await client.delete(session)    # removes the container; aclose() does not
```

- **The workspace lives inside the sandbox** (`Manifest.root`, `/workspace` by default); host
  directories are not mounted. `Manifest` entries are not used. After the session starts,
  screenshots are written to `input/` with `session.mkdir(path, parents=True)` and
  `session.write(path, io.BytesIO(data))`, and artifacts are read back with `session.read(path)`.
  Paths may be relative to the workspace root.
- `SandboxAgent.capabilities` defaults to `[Filesystem(), Shell(), Compaction()]`, which **cannot be
  used with chat completions**: the `apply_patch` tool of `Filesystem` is a free-form `CustomTool`
  supported only by the Responses API, and the request fails before it is sent
  (`UserError: Hosted tools are not supported with the ChatCompletions API`). The project passes
  `[Shell()]` only (`sandbox_capabilities()` in `agent/sandbox.py`). The tools are `exec_command`
  and, when the session supports a pseudo-terminal, `write_stdin`; files are written and inspected
  through the shell. A test converts each tool to the chat completions format and asserts that the
  SDK default still cannot be converted, so that a future SDK change is noticed.
- A session passed with `session=` is owned by the caller; the runner does not close it.
- `DockerSandboxClientOptions.network_mode` is either `"none"` or unset;
  `agent.sandbox.network = false` maps to `"none"`.
- **The timeout of a single command is chosen by the model** through the `yield_time_ms` argument
  of `exec_command`. The SDK has no global limit, and `agent.sandbox.timeout_secs` is currently not
  wired up; `agent.task_timeout_secs` bounds the whole task.
- `UnixLocalSandboxClient` raises `ImportError` on import under Windows. The platform is checked
  first and the user is told to use `docker` or `none`.
- A sandbox that cannot be started — Docker missing or stopped, no image configured, start failure —
  does not fail the task. The runner falls back to a plain `Agent`, records a progress note and
  tells the agent that it cannot run code (architecture.md §9).

The `gpu` tests (`uv run pytest -m gpu tests/test_real_services.py -s`) verify that the container
starts, that `exec_command` runs Python, that artifacts are retrieved and that the container is
removed afterwards.
