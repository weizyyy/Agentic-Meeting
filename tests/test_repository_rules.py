"""仓库的硬性规则，以及配置档案与上游、评测样例与工具表之间的对应关系。

这些不是功能测试（功能由 ``tests/e2e/`` 端到端地验证），而是守住几条一旦破坏、运行时不会立刻暴露的约定。
"""

from __future__ import annotations

import ast
import json

import pytest

from agentic_meeting.config import EXAMPLE_CONFIG_PATH, REPO_ROOT, load_asr_profile, load_config
from agentic_meeting.pipeline.tools import realtime_tools

MODEL_NAMES = ("ornith", "confucius", "nemotron", "qwen3", "sortformer")
WEIGHT_FILE_LITERALS = ('.gguf"', ".gguf'")  # 源码里写死的权重文件名


def test_no_model_names_in_source():
    """硬性规则（AGENTS.md 第 2 条）：源码、客户端和脚本里不写具体模型名，源码里不写权重文件名。

    ``scripts/runtimes.py`` 要按扩展名认出权重文件，所以权重扩展名只在 ``src/`` 里查。
    """
    offenders = []
    for folder, patterns, banned in (
        ("src", ["*.py"], MODEL_NAMES + WEIGHT_FILE_LITERALS),
        ("scripts", ["*.py"], MODEL_NAMES),
        ("client/src", ["*.ts", "*.tsx"], MODEL_NAMES),
    ):
        for pattern in patterns:
            for path in (REPO_ROOT / folder).rglob(pattern):
                text = path.read_text(encoding="utf-8").lower()
                offenders += [
                    f"{path.relative_to(REPO_ROOT)}: {word}" for word in banned if word in text
                ]
    assert not offenders, "发现写死的模型名/权重名：\n" + "\n".join(offenders)


def test_asr_profile_matches_the_upstream_chat_template():
    """识别档案里的对话模板必须与官方参考实现逐字一致，否则识别会悄悄变差。"""
    profile = load_asr_profile(load_config(EXAMPLE_CONFIG_PATH))
    assert profile.text_marker and profile.text_marker in profile.assistant_prefix
    upstream = REPO_ROOT / "third_party/Confucius4-R2T2/r2t2_llama/llama_native_backend.py"
    if not upstream.is_file():
        pytest.skip("子模块 third_party/Confucius4-R2T2 未初始化")
    tree = ast.parse(upstream.read_text(encoding="utf-8"))
    template = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and getattr(node.targets[0], "id", "") == "_ASR_CHAT_TEMPLATE"
    )
    assert profile.chat_template == template


def test_realtime_eval_cases_cover_exactly_the_realtime_tools():
    """``scripts/eval_realtime_model.py`` 的样例：格式对，期望的工具都存在，每个工具都有样例。"""
    with (REPO_ROOT / "tests" / "data" / "realtime_eval.jsonl").open(encoding="utf-8") as f:
        cases = [json.loads(line) for line in f if line.strip()]
    assert len({case["id"] for case in cases}) == len(cases) >= 10
    for case in cases:
        assert "{name}" in case["question"]
        assert all(line.startswith("[") for line in case["context"])
    cfg = load_config(EXAMPLE_CONFIG_PATH)
    cfg.agent.enabled = True
    names = {tool.__name__ for tool in realtime_tools(cfg)}
    assert {case["expect_tool"] for case in cases} - {None} == names
    assert sum(case["expect_tool"] is None for case in cases) >= 3
