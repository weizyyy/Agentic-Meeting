"""测试公共夹具。"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from agentic_meeting.config import EXAMPLE_CONFIG_PATH, AppConfig


@pytest.fixture
def make_cfg(tmp_path: Path) -> Callable[[], AppConfig]:
    """以配置模板为底、填上虚构的模型路径的配置工厂。

    每次调用返回一份新的、可随意修改的配置；``session.data_dir`` 指向 ``tmp_path`` 下，
    测试写出的日志与运行时文件不会落进仓库的 ``data/``。
    """

    def factory() -> AppConfig:
        with EXAMPLE_CONFIG_PATH.open("rb") as f:
            data = tomllib.load(f)
        data["session"]["data_dir"] = str(tmp_path / "data")
        data["realtime_llm"]["llama_server"]["launch"].update(
            model_path="models/fake-llm.gguf", mmproj_path="models/fake-llm-mmproj.gguf"
        )
        data["asr"]["launch"].update(
            model_path="models/fake-asr.gguf", mmproj_path="models/fake-asr-mmproj.gguf"
        )
        data["tts"]["voice"] = "fake-voice"
        data["tts"]["launch"].update(
            model_path="models/fake-tts.gguf", codec_path="models/fake-codec.gguf"
        )
        data["embedding"]["launch"]["model_path"] = "models/fake-embedding.gguf"
        return AppConfig.model_validate(data)

    return factory
