"""提示词加载（pipeline/prompts.py）。"""

from __future__ import annotations

import pytest

from agentic_meeting.pipeline import prompts
from agentic_meeting.pipeline.prompts import PromptError, load_prompt


@pytest.fixture
def prompts_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(prompts, "PROMPTS_DIR", tmp_path)
    return tmp_path


def write(directory, name: str, text: str) -> None:
    (directory / f"{name}.md").write_bytes(text.encode("utf-8"))


def test_variables_are_substituted(prompts_dir):
    write(prompts_dir, "greet", "你是「{assistant_name}」。\n纪要：{digest}\n")
    assert load_prompt("greet", assistant_name="Nova", digest="无") == "你是「Nova」。\n纪要：无\n"


def test_doubled_braces_stay_as_literal_braces(prompts_dir):
    write(prompts_dir, "json", '返回 {{"ok": true}}，名字 {name}')
    assert load_prompt("json", name="x") == '返回 {"ok": true}，名字 x'


def test_prompt_without_variables_loads_as_is(prompts_dir):
    write(prompts_dir, "plain", "没有变量。")
    assert load_prompt("plain") == "没有变量。"


def test_extra_variables_are_ignored(prompts_dir):
    write(prompts_dir, "one", "{a}")
    assert load_prompt("one", a="1", b="2") == "1"


def test_missing_variable_is_reported_by_name(prompts_dir):
    write(prompts_dir, "digest", "旧：{previous_digest}\n新：{new_transcript}")
    with pytest.raises(PromptError) as error:
        load_prompt("digest", previous_digest="无")
    message = str(error.value)
    assert "digest" in message and "new_transcript" in message


def test_missing_file_is_reported_with_its_path(prompts_dir):
    with pytest.raises(FileNotFoundError) as error:
        load_prompt("nope")
    assert "nope.md" in str(error.value)


def test_malformed_template_is_reported_with_the_prompt_name(prompts_dir):
    write(prompts_dir, "broken", "多了一个 { 花括号")
    with pytest.raises(PromptError) as error:
        load_prompt("broken")
    assert "broken" in str(error.value)


def test_positional_placeholder_is_reported_not_crashed(prompts_dir):
    write(prompts_dir, "positional", "这里写了 {} 而不是名字")
    with pytest.raises(PromptError):
        load_prompt("positional")


@pytest.mark.parametrize("name", ["../secret", "sub/dir", "a\\b", "", ".."])
def test_prompt_names_cannot_leave_the_prompts_directory(prompts_dir, name):
    with pytest.raises(PromptError):
        load_prompt(name)


def test_the_shipped_prompts_load_with_their_documented_variables():
    # config/prompts/README.md 里的变量表；改提示词时漏了变量或花括号没转义，这里会第一时间发现。
    assert "Nova" in load_prompt("realtime_system", assistant_name="Nova", task_section="")
    with_tasks = load_prompt(
        "realtime_system", assistant_name="Nova", task_section=load_prompt("realtime_tasks")
    )
    assert "delegate_task" in with_tasks
    assert "delegate_task" not in load_prompt(
        "realtime_system", assistant_name="Nova", task_section=""
    )
    digest = load_prompt("digest", previous_digest="旧纪要", new_transcript="新转录")
    assert "旧纪要" in digest and "新转录" in digest
    assert load_prompt("screen_caption")
    assert load_prompt("agent_system")
