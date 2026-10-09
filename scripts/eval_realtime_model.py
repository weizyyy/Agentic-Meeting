"""实时模型对比：对当前配置的实时模型跑一组固定样例，看它答得快不快、工具选得对不对、话多不多。

用法::

    uv run python scripts/eval_realtime_model.py                 # 用配置里选中的接入方式
    uv run python scripts/eval_realtime_model.py --mode llama_server
    uv run python scripts/eval_realtime_model.py --repeat 3 --out data/eval/model-a.jsonl

样例在 ``tests/data/realtime_eval.jsonl``，每行一条：会议里最近的几句转录（``context``）、一句对助理说的话
（``question``，``{name}`` 换成助理的名字）、期望调用的工具（``expect_tool``，``null`` 表示不该调用工具）。
请求和正式运行时一样：同一份系统提示词、同一组工具、同样的采样参数和附加字段（``cfg.realtime_llm.active`` 与
``request_extra_body()``），所以两种接入方式都能测；``--mode`` 临时覆盖配置里的接入方式，方便放在一起比。

每条样例输出：首字延迟（第一个文字或第一个工具调用出现的时刻）、总耗时、选了哪个工具、回答的字数。
这里只看模型「第一步」的选择，不执行工具。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from pipecat.adapters.services.open_ai_adapter import OpenAILLMAdapter
from pipecat.processors.aggregators.llm_context import LLMContext

from agentic_meeting.config import AppConfig, load_config, secret
from agentic_meeting.pipeline.prompts import load_prompt
from agentic_meeting.pipeline.tools import realtime_tools

DEFAULT_CASES = Path(__file__).resolve().parent.parent / "tests" / "data" / "realtime_eval.jsonl"
SAMPLING_FIELDS = ("temperature", "top_p", "presence_penalty", "max_tokens")


def load_cases(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def system_prompt(cfg: AppConfig) -> str:
    task_section = load_prompt("realtime_tasks") if cfg.agent.enabled else ""
    return load_prompt(
        "realtime_system", assistant_name=cfg.session.assistant_name, task_section=task_section
    )


def request_for(cfg: AppConfig, case: dict[str, Any], tools: list[Any]) -> dict[str, Any]:
    """一条样例对应的请求参数（和正式运行时的请求同一个形状）。"""
    endpoint = cfg.realtime_llm.active
    question = case["question"].format(name=cfg.session.assistant_name)
    messages = [{"role": "system", "content": system_prompt(cfg)}]
    messages += [{"role": "user", "content": line} for line in case.get("context", [])]
    messages.append({"role": "user", "content": question})
    sampling = {
        name: value
        for name in SAMPLING_FIELDS
        if (value := getattr(endpoint.sampling, name)) is not None
    }
    return {
        "model": endpoint.model,
        "messages": messages,
        "tools": tools,
        "stream": True,
        "extra_body": cfg.realtime_llm.request_extra_body(),
        **sampling,
    }


async def run_case(client: AsyncOpenAI, request: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    first: float | None = None
    text: list[str] = []
    tool_names: list[str] = []
    stream = await client.chat.completions.create(**request)
    async for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            first = first or time.perf_counter() - started
            text.append(delta.content)
        for call in delta.tool_calls or []:
            first = first or time.perf_counter() - started
            if call.function and call.function.name:
                tool_names.append(call.function.name)
    return {
        "first_secs": round(first, 3) if first is not None else None,
        "total_secs": round(time.perf_counter() - started, 3),
        "tool": tool_names[0] if tool_names else None,
        "answer": "".join(text),
    }


def is_correct(case: dict[str, Any], result: dict[str, Any]) -> bool:
    return result["tool"] == case.get("expect_tool")


def print_summary(rows: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
    firsts = [r["first_secs"] for _, r in rows if r.get("first_secs") is not None]
    spoken = [len(r["answer"]) for c, r in rows if c.get("expect_tool") is None and r["answer"]]
    tool_rows = [r for c, r in rows if c.get("expect_tool")]
    plain_rows = [r for c, r in rows if not c.get("expect_tool")]
    print("\n===== 汇总 =====")
    if firsts:
        print(
            f"首字延迟：中位 {statistics.median(firsts):.2f} 秒，"
            f"最慢 {max(firsts):.2f} 秒（{len(firsts)} 次）"
        )
    print(f"该用工具时选对：{sum(r['correct'] for r in tool_rows)} / {len(tool_rows)}")
    print(f"不该用工具时没用：{sum(r['correct'] for r in plain_rows)} / {len(plain_rows)}")
    if spoken:
        print(f"直接回答的字数：中位 {statistics.median(spoken):.0f}，最长 {max(spoken)}")
    errors = sum("error" in r for _, r in rows)
    if errors:
        print(f"请求出错 {errors} 次")


async def run(args: argparse.Namespace, out: Any) -> int:
    cfg = load_config(args.config)
    if args.mode:
        if getattr(cfg.realtime_llm, args.mode) is None:
            print(f"配置里没有填写 [realtime_llm.{args.mode}] 小节，没法用这种接入方式")
            return 2
        cfg.realtime_llm.mode = args.mode
    endpoint = cfg.realtime_llm.active
    cases = load_cases(Path(args.cases))
    tools = OpenAILLMAdapter().to_provider_tools_format(LLMContext(tools=realtime_tools(cfg)).tools)
    client = AsyncOpenAI(
        base_url=endpoint.base_url, api_key=secret(endpoint.api_key_env) or "none", max_retries=0
    )
    print(f"接入方式 {cfg.realtime_llm.mode}，{len(cases)} 条样例 × {args.repeat} 遍\n")
    print(f"{'样例':<18}{'期望':<16}{'实际':<16}{'首字(秒)':<10}{'总(秒)':<8}{'字数':<6}回答")
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for _ in range(args.repeat):
        for case in cases:
            try:
                result = await run_case(client, request_for(cfg, case, tools))
            except Exception as e:  # 一条失败不影响其余的
                result = {"error": f"{type(e).__name__}: {e}", "tool": "(出错)", "answer": ""}
            result["correct"] = is_correct(case, result)
            rows.append((case, result))
            mark = "" if result["correct"] else "  ✗"
            print(
                f"{case['id']:<18}{case.get('expect_tool') or '-':<16}{result['tool'] or '-':<16}"
                f"{result.get('first_secs') or '-'!s:<10}{result.get('total_secs') or '-'!s:<8}"
                f"{len(result['answer']):<6}{result['answer'][:40]!r}{mark}"
            )
            if out is not None:
                out.write(json.dumps({"id": case["id"], **result}, ensure_ascii=False) + "\n")
    print_summary(rows)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="对当前配置的实时模型跑一组固定样例")
    parser.add_argument("--config", default=None, help="配置文件，默认 config/config.toml")
    parser.add_argument(
        "--mode", choices=["llama_server", "openai_api"], default=None, help="临时覆盖接入方式"
    )
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="样例文件")
    parser.add_argument("--repeat", type=int, default=1, help="每条样例跑几遍")
    parser.add_argument("--out", default=None, help="把每条结果追加写进这个 JSONL 文件")
    args = parser.parse_args()
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with Path(args.out).open("a", encoding="utf-8") as out:
            sys.exit(asyncio.run(run(args, out)))
    sys.exit(asyncio.run(run(args, None)))


if __name__ == "__main__":
    main()
