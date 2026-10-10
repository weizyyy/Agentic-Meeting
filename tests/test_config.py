"""配置加载的基线测试。接手实现时请保持这些测试通过。"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentic_meeting.config import (
    EXAMPLE_CONFIG_PATH,
    REPO_ROOT,
    AppConfig,
    check_ready,
    check_warnings,
    load_config,
    secret,
    server_warnings,
)


def example_dict() -> dict:
    with EXAMPLE_CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def test_example_config_is_structurally_valid():
    cfg = load_config(EXAMPLE_CONFIG_PATH)
    assert cfg.session.assistant_name
    assert cfg.realtime_llm.mode == "llama_server"
    assert cfg.realtime_llm.llama_server.thinking is False
    assert cfg.realtime_llm.llama_server.launch.parallel == 2
    # 模板里两种接入方式都写了，便于用户只改 mode 一行就切换。
    assert cfg.realtime_llm.openai_api is not None


def test_example_config_reports_everything_the_user_must_fill():
    problems = check_ready(load_config(EXAMPLE_CONFIG_PATH))
    joined = "\n".join(problems)
    # 没选中的接入方式不应被检查。
    assert "realtime_llm.openai_api" not in joined
    for field in (
        "realtime_llm.llama_server.launch.model_path",
        "realtime_llm.llama_server.launch.mmproj_path",
        "asr.launch.model_path",
        "asr.launch.mmproj_path",
        "diarization.model_path",
        "tts.voice",
        "tts.launch.model_path",
        "embedding.launch.model_path",
        "agent.base_url",
        "agent.model",
    ):
        assert field in joined, f"就绪检查漏报了 {field}"


# ---- 实时模型的两种接入方式 ----


def realtime_cfg(mode: str, **overrides) -> AppConfig:
    """以模板为底，切到指定接入方式并覆盖若干字段（键用双下划线表示层级）。"""
    data = example_dict()
    data["realtime_llm"]["mode"] = mode
    for dotted, value in overrides.items():
        node = data["realtime_llm"]
        *parents, leaf = dotted.split("__")
        for key in parents:
            node = node[key]
        node[leaf] = value
    return AppConfig.model_validate(data)


def remote_api_cfg(**overrides) -> AppConfig:
    return realtime_cfg(
        "openai_api",
        openai_api__base_url="https://llm.example.com/v1",
        openai_api__model="fake-model",
        **overrides,
    )


def test_realtime_mode_selects_active_endpoint():
    llama = realtime_cfg("llama_server").realtime_llm
    assert llama.active is llama.llama_server
    assert llama.managed is True
    assert llama.has_health_endpoint is True
    assert "has_health_endpoint" not in llama.model_dump()
    assert llama.cache_warm is True
    assert llama.supports_developer_role is False

    api = remote_api_cfg().realtime_llm
    assert api.active is api.openai_api
    assert api.active.model == "fake-model"
    assert api.managed is False  # 通用接口的进程不归本项目管
    assert api.has_health_endpoint is False
    assert api.cache_warm is False  # 默认不对通用接口做预热
    assert remote_api_cfg(openai_api__cache_warm=True).realtime_llm.cache_warm is True
    assert (
        remote_api_cfg(
            openai_api__supports_developer_role=True
        ).realtime_llm.supports_developer_role
        is True
    )


def test_realtime_llama_server_not_managed_when_launch_disabled():
    cfg = realtime_cfg("llama_server", llama_server__launch__enabled=False)
    assert cfg.realtime_llm.managed is False
    assert cfg.realtime_llm.has_health_endpoint is True
    assert "realtime_llm.llama_server.launch" not in "\n".join(check_ready(cfg))


def test_realtime_mode_requires_its_section():
    data = example_dict()
    data["realtime_llm"]["mode"] = "openai_api"
    del data["realtime_llm"]["openai_api"]
    with pytest.raises(ValidationError, match="openai_api"):
        AppConfig.model_validate(data)
    # 反过来：选了 llama_server 时可以不写 openai_api 小节。
    data = example_dict()
    del data["realtime_llm"]["openai_api"]
    assert AppConfig.model_validate(data).realtime_llm.openai_api is None


def test_realtime_request_extra_body_differs_by_mode():
    llama = realtime_cfg(
        "llama_server",
        llama_server__extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        llama_server__sampling={"top_k": 20},
    ).realtime_llm
    assert llama.request_extra_body() == {
        "chat_template_kwargs": {"enable_thinking": False},
        "top_k": 20,
        "id_slot": 0,
        "cache_prompt": True,
    }
    assert llama.request_extra_body(background=True)["id_slot"] == 1

    api = remote_api_cfg(openai_api__extra_body={"reasoning_effort": "none"}).realtime_llm
    # 通用接口不带任何 llama.cpp 专有字段，实时与后台请求也没有区别。
    assert api.request_extra_body() == {"reasoning_effort": "none"}
    assert api.request_extra_body(background=True) == {"reasoning_effort": "none"}


def test_openai_api_mode_readiness_and_egress_warning(monkeypatch):
    monkeypatch.delenv("REALTIME_LLM_API_KEY", raising=False)
    unfilled = "\n".join(check_ready(realtime_cfg("openai_api")))
    assert "realtime_llm.openai_api.base_url" in unfilled
    assert "realtime_llm.openai_api.model" in unfilled
    assert "realtime_llm.llama_server" not in unfilled

    remote = remote_api_cfg()
    problems = "\n".join(check_ready(remote))
    assert "REALTIME_LLM_API_KEY" in problems  # 密钥变量没设置要报出来
    assert "realtime_llm.openai_api.base_url" not in problems
    warnings = "\n".join(check_warnings(remote))
    assert "llm.example.com" in warnings
    assert "会议转录" in warnings

    local = realtime_cfg(
        "openai_api",
        openai_api__base_url="http://127.0.0.1:9000/v1",
        openai_api__model="fake-model",
        openai_api__api_key_env="",
    )
    assert check_warnings(local) == []
    assert check_warnings(realtime_cfg("llama_server")) == []


def test_llama_server_slots_must_fit_parallel():
    cfg = realtime_cfg("llama_server", llama_server__launch__parallel=1)
    assert "并发槽位" in "\n".join(check_ready(cfg))
    cfg = realtime_cfg("llama_server", llama_server__background_slot=0)
    assert "不能相同" in "\n".join(check_ready(cfg))


# ---- 其他 ----


def test_unknown_keys_are_rejected():
    data = example_dict()
    data["realtime_llm"]["modle"] = "typo"
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_wake_phrases_include_name_without_duplicates():
    data = example_dict()
    data["session"]["assistant_name"] = "Nova"
    data["session"]["wake_aliases"] = ["Nova", "Hey  Nova"]
    cfg = AppConfig.model_validate(data)
    assert cfg.session.wake_phrases == ["Nova", "Hey Nova"]  # 去重，多余的空白被规整


@pytest.mark.parametrize("name", ["小研", "", "  ", "Nova!", "9lives", "诺瓦Nova"])
def test_assistant_name_must_be_english(name):
    """唤醒靠在识别文字里找英文词（见 pipeline/wake.py），中文名字或带符号的名字一律拒绝。"""
    data = example_dict()
    data["session"]["assistant_name"] = name
    with pytest.raises(ValidationError, match="英文"):
        AppConfig.model_validate(data)


def test_wake_aliases_may_be_english_words_or_chinese_transliterations():
    """识别有时把名字写成近似的英文词，有时听成几个汉字；两种别名都收。"""
    data = example_dict()
    data["session"]["assistant_name"] = "Nova"
    data["session"]["wake_aliases"] = ["Novel", " 诺瓦 "]
    assert AppConfig.model_validate(data).session.wake_phrases == ["Nova", "Novel", "诺瓦"]


@pytest.mark.parametrize("alias", ["诺", "诺瓦Nova", "Nova!", "诺 瓦", ""])
def test_wake_aliases_reject_single_characters_and_mixed_spellings(alias):
    data = example_dict()
    data["session"]["wake_aliases"] = [alias]
    with pytest.raises(ValidationError, match="wake_aliases"):
        AppConfig.model_validate(data)


def test_wake_timeout_is_configurable_and_must_be_positive():
    """唤醒窗口同时决定助理说话时能用声音打断多久（pipecat-notes.md §3.3），用户要能自己调。"""
    assert load_config(EXAMPLE_CONFIG_PATH).turn.wake_timeout_secs == 30.0
    data = example_dict()
    data["turn"]["wake_timeout_secs"] = 45
    assert AppConfig.model_validate(data).turn.wake_timeout_secs == 45.0
    for bad in (0, -5):
        data["turn"]["wake_timeout_secs"] = bad
        with pytest.raises(ValidationError):
            AppConfig.model_validate(data)


def test_audio_section_has_documented_defaults_and_is_optional():
    cfg = load_config(EXAMPLE_CONFIG_PATH)
    a = cfg.audio
    assert (a.auto_gain, a.gain_db, a.max_gain_db) == (True, 0.0, 30.0)
    assert (a.target_dbfs, a.noise_floor_dbfs, a.level_log_secs) == (-16.0, -70.0, 10.0)
    assert cfg.turn.vad_min_volume == 0.6  # Pipecat 的默认值：行为不变，只是变成可配
    data = example_dict()
    data.pop("audio", None)  # 旧的配置文件没有这一段，也要能加载
    assert AppConfig.model_validate(data).audio == a


@pytest.mark.parametrize(
    ("section", "key", "bad"),
    [
        ("audio", "gain_db", 61),
        ("audio", "gain_db", -21),
        ("audio", "max_gain_db", -1),
        ("audio", "max_gain_db", 61),
        ("audio", "target_dbfs", 0),
        ("audio", "target_dbfs", -60),
        ("audio", "noise_floor_dbfs", -20),
        ("audio", "noise_floor_dbfs", -120),
        ("audio", "level_log_secs", -1),
        ("turn", "vad_min_volume", -0.1),
        ("turn", "vad_min_volume", 1.1),
    ],
)
def test_audio_and_vad_volume_values_are_range_checked(section, key, bad):
    data = example_dict()
    data.setdefault(section, {})[key] = bad
    with pytest.raises(ValidationError):
        AppConfig.model_validate(data)


def test_relative_paths_resolve_against_repo_root():
    cfg = load_config(EXAMPLE_CONFIG_PATH)
    assert cfg.resolve("models/x.gguf") == REPO_ROOT / "models" / "x.gguf"
    absolute = Path(REPO_ROOT.anchor) / "abs" / "x.gguf"
    assert cfg.resolve(str(absolute)) == absolute


def test_asr_profile_loads_and_matches_upstream_template():
    """档案里的对话模板必须与官方参考实现逐字一致（子模块未初始化时跳过比对）。"""
    import ast

    from agentic_meeting.config import load_asr_profile

    profile = load_asr_profile(load_config(EXAMPLE_CONFIG_PATH))
    assert profile.text_marker and profile.text_marker in profile.assistant_prefix
    assert "{language}" in profile.assistant_prefix

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


def test_no_model_names_hardcoded_in_source():
    """硬性规则：src/ 里不允许出现具体模型名或权重文件名。"""
    banned = ("ornith", "confucius", "nemotron", "qwen3", "sortformer", '.gguf"', ".gguf'")
    offenders = []
    for path in (REPO_ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        offenders += [f"{path.relative_to(REPO_ROOT)}: {word}" for word in banned if word in text]
    assert not offenders, "发现写死的模型名/权重名：\n" + "\n".join(offenders)


# --------------------------------------------------------------------------- #
# .env：密钥放在仓库根目录的 .env 里，凡是用本机正式配置的入口都要读到它
# --------------------------------------------------------------------------- #


def test_env_file_is_loaded_without_overriding_existing_variables(tmp_path, monkeypatch):
    from agentic_meeting.config import load_env_file

    env_file = tmp_path / ".env"
    env_file.write_text(
        '# 注释\nFAKE_FROM_FILE=from-file\nFAKE_ALREADY_SET=from-file\nFAKE_QUOTED="Bearer abc"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("FAKE_FROM_FILE", raising=False)
    monkeypatch.delenv("FAKE_QUOTED", raising=False)
    monkeypatch.setenv("FAKE_ALREADY_SET", "from-shell")
    assert load_env_file(env_file) is True
    assert secret("FAKE_FROM_FILE") == "from-file"
    assert secret("FAKE_QUOTED") == "Bearer abc"
    assert secret("FAKE_ALREADY_SET") == "from-shell"  # 终端里设的优先
    assert load_env_file(tmp_path / "nope.env") is False
    monkeypatch.delenv("FAKE_FROM_FILE")
    monkeypatch.delenv("FAKE_QUOTED")


def test_load_config_reads_env_file_only_for_the_real_configuration(tmp_path, monkeypatch):
    """实测时踩到的：.env 只有命令行入口读，需要外部服务的测试和脚本读不到密钥。"""
    import agentic_meeting.config as config_module

    env_file = tmp_path / ".env"
    env_file.write_text("FAKE_AGENT_KEY=sk-from-dotenv\n", encoding="utf-8")
    real_config = tmp_path / "config.toml"
    real_config.write_bytes(EXAMPLE_CONFIG_PATH.read_bytes())
    monkeypatch.setattr(config_module, "ENV_FILE_PATH", env_file)
    monkeypatch.setattr(config_module, "DEFAULT_CONFIG_PATH", real_config)
    monkeypatch.delenv("AGENTIC_MEETING_CONFIG", raising=False)
    monkeypatch.delenv("FAKE_AGENT_KEY", raising=False)

    load_config(EXAMPLE_CONFIG_PATH)  # 显式给路径（单元测试的用法）：不读 .env
    assert secret("FAKE_AGENT_KEY") is None
    load_config()  # 本机的正式配置：读
    assert secret("FAKE_AGENT_KEY") == "sk-from-dotenv"
    monkeypatch.delenv("FAKE_AGENT_KEY")


# ---- 会后报告 ----


def test_report_section_defaults_and_validation():
    cfg = load_config(EXAMPLE_CONFIG_PATH)
    assert (cfg.report.provider, cfg.report.max_input_chars) == ("realtime_llm", 8000)
    data = example_dict()
    del data["report"]  # 整段不写也可以
    assert AppConfig.model_validate(data).report.provider == "realtime_llm"
    for key, bad in (("provider", "someone_else"), ("max_input_chars", 10), ("unknown", 1)):
        data = example_dict()
        data["report"][key] = bad
        with pytest.raises(ValidationError):
            AppConfig.model_validate(data)


def test_report_by_the_remote_model_warns_that_the_transcript_leaves_the_machine():
    data = example_dict()
    data["report"]["provider"] = "agent_llm"
    data["agent"]["base_url"] = "https://agent.example.com/v1"
    warnings = "\n".join(check_warnings(AppConfig.model_validate(data)))
    assert "会后报告" in warnings and "agent.example.com" in warnings and "转录" in warnings
    data["agent"]["base_url"] = "http://127.0.0.1:9000/v1"  # 本机的不算外发
    assert "会后报告" not in "\n".join(check_warnings(AppConfig.model_validate(data)))
    data["report"]["provider"] = "realtime_llm"
    data["agent"]["base_url"] = "https://agent.example.com/v1"
    assert "会后报告" not in "\n".join(check_warnings(AppConfig.model_validate(data)))


# ---- 发言怎么分条 ----


def test_transcript_section_defaults_and_validation():
    cfg = load_config(EXAMPLE_CONFIG_PATH)
    t = cfg.transcript
    assert (t.merge_gap_secs, t.merge_soft_chars, t.merge_max_chars) == (2.0, 40, 200)
    data = example_dict()
    del data["transcript"]  # 整段不写也可以
    assert AppConfig.model_validate(data).transcript.merge_gap_secs == 2.0
    data = example_dict()
    data["transcript"]["merge_gap_secs"] = 0  # 0 = 不并
    assert AppConfig.model_validate(data).transcript.merge_gap_secs == 0
    for key, bad in (("merge_gap_secs", -1), ("merge_soft_chars", 0), ("merge_max_chars", 0)):
        data = example_dict()
        data["transcript"][key] = bad
        with pytest.raises(ValidationError):
            AppConfig.model_validate(data)


# ---- 访问口令 ----


def test_access_password_is_opt_in_and_read_from_the_environment(monkeypatch):
    cfg = load_config(EXAMPLE_CONFIG_PATH)
    assert (cfg.server.password_env, cfg.server.auth_session_days) == ("", 7)
    data = example_dict()
    data["server"]["password_env"] = "FAKE_MEETING_PASSWORD"
    configured = AppConfig.model_validate(data)
    monkeypatch.delenv("FAKE_MEETING_PASSWORD", raising=False)
    assert any("FAKE_MEETING_PASSWORD" in p for p in check_ready(configured))
    monkeypatch.setenv("FAKE_MEETING_PASSWORD", "short")
    assert any("太短" in p for p in check_ready(configured))
    monkeypatch.setenv("FAKE_MEETING_PASSWORD", "long enough")
    assert not any("password_env" in p for p in check_ready(configured))
    for bad in (0, -1, 400):
        data = example_dict()
        data["server"]["auth_session_days"] = bad
        with pytest.raises(ValidationError):
            AppConfig.model_validate(data)


@pytest.mark.parametrize(
    ("host", "password_env", "warned"),
    [
        ("0.0.0.0", "", True),
        ("192.168.1.20", "", True),
        ("0.0.0.0", "FAKE_MEETING_PASSWORD", False),
        ("127.0.0.1", "", False),
        ("localhost", "", False),
        ("::1", "", False),
    ],
)
def test_listening_beyond_this_machine_without_a_password_is_warned(host, password_env, warned):
    data = example_dict()
    data["server"].update(host=host, password_env=password_env)
    warnings = server_warnings(AppConfig.model_validate(data))
    assert bool(warnings) == warned
    if warned:
        assert host in warnings[0] and "password_env" in warnings[0]
    # 这条只在命令行打印，不混进发给浏览器的那组提醒
    assert not any("口令" in w for w in check_warnings(AppConfig.model_validate(data)))
