"""把 Pipecat 异步工具的协议消息改写成模型好读的中文行（docs/pipecat-notes.md §6）。

``delegate_task`` 是「不随打断取消」的工具：先回报已受理，任务做完再回报结果。Pipecat 为这类工具往上下文里写三种消息
（开始时的占位、中间结果、最终结果），内容是一段 JSON：一大段英文说明 + 把结果再编码一次的字符串。
原样发给模型有两个问题（端到端测试里看到的）：

* 外层 JSON 用的是默认编码，结果里的中文全变成 ``\\u8fd9\\u7bc7`` 这样的转义，本地的小模型未必读得顺，token 也多几倍；
* 那段英文说明要求「先回答用户刚说的话，再把结果附在同一次回复的末尾，不要单独成一次回复」，
  而我们的流程是任务结果回来时专门触发一次生成、用一两句话简报。

所以在**发请求之前**把它们改写成 architecture.md §6.1 里约定的那种行：``[任务 t2 完成] 结论``。
上下文里存的仍是 Pipecat 的原样消息（它自己还要靠这些消息判断调用的状态），只改发出去的那一份；
正式请求和预热请求走同一段代码，所以两者仍然逐字一致。
"""

from __future__ import annotations

import json
from typing import Any

from pipecat.processors.aggregators import async_tool_messages

STARTED_TEXT = "后台任务已经开始，结果稍后送达。不要重复调用，也不要猜结果。"
CANCELLED_TEXT = (
    "[任务没有完成] 这次调用被取消或超时了，没有结果。对方还在等的话，如实告诉他没办成。"
)


def _result_line(kind: str, raw: str) -> str:
    """中间 / 最终结果 → 一行中文。认得出是任务工具的结果就用任务的格式，否则原样给出（中文不转义）。"""
    if raw.startswith("CANCELLED"):
        return CANCELLED_TEXT
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("task_id"), str):
        label, status = data["task_id"], data.get("status")
        if status == "accepted":
            return f"[任务 {label} 已受理] 后台正在处理，做完后你会收到结果。"
        if status == "succeeded":
            return f"[任务 {label} 完成] {data.get('brief') or '已完成，详情见任务面板'}"
        if status == "cancelled":
            return f"[任务 {label} 已取消] {data.get('reason') or ''}".rstrip()
        if status == "failed":
            return f"[任务 {label} 失败] {data.get('reason') or '没有做完'}"
    text = json.dumps(data, ensure_ascii=False) if data is not None else raw
    return f"[工具结果] {text}" if kind == "final" else f"[工具进度] {text}"


def localize_async_tool_messages(messages: list[Any]) -> list[Any]:
    """返回一份新的消息列表：异步工具的协议消息换成中文行，其余原样（同一个对象）。"""
    out: list[Any] = []
    for message in messages:
        rewritten = message
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            role = message.get("role")
            # developer 角色可能已经被转成 user（本地模型不认识 developer）；按原来的角色去认
            probe = {**message, "role": "developer"} if role == "user" else message
            payload = (
                async_tool_messages.parse_message(probe)
                if role
                in (
                    "tool",
                    "developer",
                    "user",
                )
                else None
            )
            if payload is not None:
                if payload.kind == "started":
                    content = STARTED_TEXT
                else:
                    content = _result_line(payload.kind, payload.result or "")
                rewritten = {**message, "content": content}
        out.append(rewritten)
    return out
