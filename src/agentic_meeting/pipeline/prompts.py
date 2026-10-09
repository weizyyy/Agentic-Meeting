"""提示词加载：读 ``config/prompts/<名字>.md``，用 ``str.format_map`` 代入变量。

提示词是纯文本，用户可以直接改，改完重启生效（config/prompts/README.md）。正文里要出现字面的花括号时
写成两个：``{{`` 和 ``}}``。
"""

from __future__ import annotations

from agentic_meeting.config import REPO_ROOT

PROMPTS_DIR = REPO_ROOT / "config" / "prompts"


class PromptError(ValueError):
    """提示词的变量或格式有问题（缺变量、花括号没转义……）。缺文件则抛 ``FileNotFoundError``。"""


class _Variables(dict):
    def __init__(self, prompt: str, values: dict):
        super().__init__(values)
        self._prompt = prompt

    def __missing__(self, key: str):
        raise PromptError(f"提示词 {self._prompt}.md 里用到了变量 {{{key}}}，但调用时没有提供")


def load_prompt(name: str, /, **variables: object) -> str:
    """读取提示词 ``name`` 并代入变量。多传的变量被忽略，缺变量时报错并指出是哪个。"""
    if not name or name in {".", ".."} or any(sep in name for sep in "/\\"):
        raise PromptError(f"提示词名字不能为空，也不能带路径：{name!r}")
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"找不到提示词文件：{path}")
    text = path.read_text(encoding="utf-8")
    try:
        return text.format_map(_Variables(name, variables))
    except PromptError:
        raise
    except (ValueError, IndexError, KeyError, AttributeError) as e:
        raise PromptError(
            f"提示词 {name}.md 的格式有误（花括号要成对，字面花括号写成两个）：{e}"
        ) from e
