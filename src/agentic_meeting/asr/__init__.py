"""流式语音识别。

    base.py          StreamingASR 协议、ASRBackendError
    llama_server.py  后端：用标准 llama-server 的音频输入做「滚动窗口 + 前缀续写」流式识别
    stt_service.py   Pipecat STTService 子类，把 StreamingASR 接进管线

新增后端 = 新增一个实现 ``StreamingASR`` 的类，并在 :func:`build_asr_backend` 里按
``config.asr.backend`` 分派。
"""

from __future__ import annotations

import httpx

from agentic_meeting.asr.base import StreamingASR
from agentic_meeting.asr.llama_server import LlamaServerASR
from agentic_meeting.config import AppConfig, load_asr_profile


def asr_hotwords(cfg: AppConfig) -> list[str]:
    """识别用的热词：成员姓名和课题术语，加上助理的名字（让识别把它写对）。去重、保持顺序。

    唤醒词的别名不放进来：它们是识别听错时的写法，当成热词只会让识别更爱这么写。
    """
    seen: list[str] = []
    for word in [*cfg.session.hotwords, cfg.session.assistant_name]:
        if word and word not in seen:
            seen.append(word)
    return seen


def build_asr_backend(cfg: AppConfig, *, client: httpx.AsyncClient | None = None) -> StreamingASR:
    """按配置创建识别后端。一路音频流一个实例。"""
    if cfg.asr.backend == "llama_server":
        return LlamaServerASR(cfg.asr, load_asr_profile(cfg), asr_hotwords(cfg), client)
    raise ValueError(f"不支持的识别后端：{cfg.asr.backend}")  # pragma: no cover
