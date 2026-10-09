"""实时模型对比脚本（scripts/eval_realtime_model.py）用的样例集：格式对、期望的工具确实存在。"""

from __future__ import annotations

import json
from pathlib import Path

from agentic_meeting.pipeline.tools import realtime_tools

CASES_PATH = Path(__file__).parent / "data" / "realtime_eval.jsonl"


def load_cases() -> list[dict]:
    with CASES_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_every_case_is_well_formed_and_ids_are_unique():
    cases = load_cases()
    assert len({case["id"] for case in cases}) == len(cases) >= 10
    for case in cases:
        assert "{name}" in case["question"]
        assert all(line.startswith("[") for line in case["context"])
        assert "expect_tool" in case


def test_expected_tools_exist_and_every_tool_is_covered(make_cfg):
    cfg = make_cfg()
    cfg.agent.enabled = True
    names = {tool.__name__ for tool in realtime_tools(cfg)}
    expected = {case["expect_tool"] for case in load_cases()} - {None}
    assert expected == names
    assert sum(case["expect_tool"] is None for case in load_cases()) >= 3
