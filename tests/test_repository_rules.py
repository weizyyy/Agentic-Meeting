"""仓库的硬性规则，以及配置档案与上游、评测样例与工具表之间的对应关系。

这些不是功能测试（功能由 ``tests/e2e/`` 端到端地验证），而是守住几条一旦破坏、运行时不会立刻暴露的约定。
"""

from __future__ import annotations

import ast
import json
import re
import tomllib

import pytest

from agentic_meeting.config import EXAMPLE_CONFIG_PATH, REPO_ROOT, load_asr_profile, load_config
from agentic_meeting.messages import ERRORS, FAILURES, NOTICES, REASONS
from agentic_meeting.pipeline.tools import realtime_tools

MODEL_NAMES = ("ornith", "confucius", "nemotron", "qwen3", "sortformer")
WEIGHT_FILE_LITERALS = ('.gguf"', ".gguf'")  # 源码里写死的权重文件名


def test_no_model_names_in_source():
    """硬性规则（AGENTS.md 第 2 条）：源码、客户端、脚本和容器文件里不写具体模型名，源码里不写权重文件名。

    ``scripts/runtimes.py`` 要按扩展名认出权重文件，所以权重扩展名只在 ``src/`` 里查。
    """
    checked = [
        *((path, MODEL_NAMES + WEIGHT_FILE_LITERALS) for path in REPO_ROOT.glob("src/**/*.py")),
        *((path, MODEL_NAMES) for path in REPO_ROOT.glob("scripts/**/*.py")),
        *((path, MODEL_NAMES) for path in REPO_ROOT.glob("client/src/**/*.ts*")),
        *((path, MODEL_NAMES) for path in REPO_ROOT.glob("docker/**/*") if path.is_file()),
        (REPO_ROOT / "compose.yaml", MODEL_NAMES),
    ]
    offenders = []
    for path, banned in checked:
        text = path.read_text(encoding="utf-8").lower()
        offenders += [f"{path.relative_to(REPO_ROOT)}: {word}" for word in banned if word in text]
    assert not offenders, "发现写死的模型名/权重名：\n" + "\n".join(offenders)


def test_compose_files_use_the_locked_llama_cpp_build():
    """Compose 里 llama-server 的上游镜像与 runtimes.lock.toml 钉住的是同一个构建号。

    升级运行时（docs/runtimes.md §7）时两处要一起改，否则容器里跑的版本和本机安装的不一样，
    而这只有在某个接口真的变了时才会暴露。
    """
    with (REPO_ROOT / "runtimes.lock.toml").open("rb") as f:
        release_tag = tomllib.load(f)["llama_cpp"]["release_tag"]
    files = [REPO_ROOT / "compose.yaml", *sorted((REPO_ROOT / "docker").glob("compose.*.yaml"))]
    pattern = re.compile(r"llama\.cpp\}:\$\{\w+:-(server[\w-]*?)-(b\d+)\}")
    found = [
        (path.relative_to(REPO_ROOT).as_posix(), match.group(1), match.group(2))
        for path in files
        for match in pattern.finditer(path.read_text(encoding="utf-8"))
    ]
    assert {flavor for _, flavor, _ in found} == {"server", "server-cuda", "server-vulkan"}
    stale = [f"{name}: {flavor}-{tag}" for name, flavor, tag in found if tag != release_tag]
    assert not stale, f"与 runtimes.lock.toml 的 {release_tag} 不一致：" + "、".join(stale)


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


def _code_tables(path, heading: str) -> tuple[dict[str, dict[str, str]], set[str]]:
    """interfaces.md 里一节代码表：代码 → 参数及类型；另有 reason 表时一并给出它的取值。"""
    text = path.read_text(encoding="utf-8")
    section = text[text.index(f"### {heading} ") :]
    section = section[: section.index("\n### ", 1)]
    codes: dict[str, dict[str, str]] = {}
    reasons: set[str] = set()
    column = 0  # 参数在第几列（不算第一列），按表头里带 params 的那一列找
    for name, rest in re.findall(r"^\| `([a-z_.]+)` +\|([^\n]*)$", section, re.M):
        if name in ("code", "error_code", "reason"):
            headers = rest.split("|")
            column = next((i for i, h in enumerate(headers) if "params" in h), 0)
            continue
        if "." in name:
            params = rest.split("|")[column]
            codes[name] = dict(re.findall(r"`(\w+)`[:：] *(string|number)", params))
        else:
            reasons.add(name)
    return codes, reasons


def _page_params(file: str, const: str) -> dict[str, dict[str, str]]:
    """页面目录里 ``const = { "代码": { 参数: "类型" }, ... }`` 那张表。"""
    page = (REPO_ROOT / "client" / "src" / "i18n" / file).read_text(encoding="utf-8")
    block = page[page.index(f"{const} = {{") :]
    block = block[: block.index("} as const satisfies")]
    return {
        code: dict(re.findall(r"(\w+): \"(string|number)\"", params))
        for code, params in re.findall(r"^ +\"([a-z_.]+)\": \{([^}]*)\},$", block, re.M)
    }


def _render_all(templates, codes: dict[str, dict[str, str]]) -> None:
    """服务端的每条中文都能用文档里的参数生成出来（参数名对得上）。"""
    for code, params in codes.items():
        sample = {name: 1 if kind == "number" else "x" for name, kind in params.items()}
        if "reason" in params:
            sample.update(reason="other", detail="x")
        assert templates[code](sample), code


DOCS = (REPO_ROOT / "docs" / "interfaces.md", REPO_ROOT / "docs" / "zh-CN" / "interfaces.md")


def test_notice_codes_agree_between_docs_server_and_page():
    """服务端提示的代码（interfaces.md §6.1.1）：文档、服务端和页面的表一致。

    页面认不出代码时退回服务端的中文，端到端测试看不出来少了一条翻译；
    很多代码只在服务出错时才发，也不是每一条都有场景覆盖。
    """
    page_codes = _page_params("serverNotices.ts", "NOTICE_PARAMS")
    page = (REPO_ROOT / "client" / "src" / "i18n" / "serverNotices.ts").read_text(encoding="utf-8")
    reasons_block = page[page.index("NOTICE_REASONS = [") :]
    page_reasons = set(re.findall(r"\"(\w+)\"", reasons_block[: reasons_block.index("]")]))

    for doc in DOCS:
        doc_codes, doc_reasons = _code_tables(doc, "6.1.1")
        assert doc_codes == page_codes, f"{doc.relative_to(REPO_ROOT)} 与页面的提示表不一致"
        assert doc_reasons == page_reasons == set(REASONS), doc.relative_to(REPO_ROOT)
    assert set(NOTICES) == set(page_codes)
    _render_all(NOTICES, page_codes)


def test_error_codes_agree_between_docs_server_and_page():
    """HTTP 接口的错误代码（interfaces.md §5.9）：文档、服务端和页面的表一致。理由同上。"""
    page_codes = _page_params("serverErrors.ts", "ERROR_PARAMS")
    for doc in DOCS:
        doc_codes, _ = _code_tables(doc, "5.9")
        assert doc_codes == page_codes, f"{doc.relative_to(REPO_ROOT)} 与页面的错误表不一致"
    assert set(ERRORS) == set(page_codes)
    _render_all(ERRORS, page_codes)


def test_failure_codes_agree_between_docs_server_and_page():
    """任务和报告失败原因的代码（interfaces.md §5.10）：文档、服务端和页面的表一致。理由同上。"""
    page_codes = _page_params("serverFailures.ts", "FAILURE_PARAMS")
    for doc in DOCS:
        doc_codes, _ = _code_tables(doc, "5.10")
        assert doc_codes == page_codes, f"{doc.relative_to(REPO_ROOT)} 与页面的失败原因表不一致"
    assert set(FAILURES) == set(page_codes)
    _render_all(FAILURES, page_codes)
