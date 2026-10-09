"""说话人区分与「文字—说话人」融合。

base.py         Diarizer 协议与 NullDiarizer
nemo_ctypes.py  后端：用 ctypes 调 NeMo-Speech.cpp 动态库的流式说话人区分 C 接口
fusion.py       把 ASR 增量按时间归属到说话人，并切分成一条条发言（纯逻辑）
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from loguru import logger

from agentic_meeting.config import AppConfig
from agentic_meeting.diar.base import Diarizer, NullDiarizer
from agentic_meeting.diar.nemo_ctypes import DiarBinding, NemoDiarizer

NoticeCallback = Callable[[str, str], Awaitable[None]]

__all__ = ["Diarizer", "NullDiarizer", "NemoDiarizer", "build_diarizer"]


async def build_diarizer(
    cfg: AppConfig,
    *,
    on_notice: NoticeCallback | None = None,
    binding_factory: Callable[[], DiarBinding] | None = None,
) -> Diarizer:
    """按配置造出说话人区分并启动它。

    ``backend = "none"`` 返回 ``NullDiarizer``。加载动态库或模型失败时**降级**：记录错误、通过 ``on_notice``
    （参数为 ``(level, text)``）告诉用户，返回 ``NullDiarizer``——所有发言记为「未知」，其余照常
    （architecture.md §9，转录链路优先存活）。
    """
    if cfg.diarization.backend == "none":
        return NullDiarizer()
    diarizer = NemoDiarizer(cfg, binding_factory=binding_factory)
    try:
        await diarizer.start()
    except Exception as e:
        logger.error(f"说话人区分启动失败，已降级为不分说话人：{e}")
        await diarizer.close()
        if on_notice is not None:
            try:
                await on_notice(
                    "warn", f"说话人区分不可用，所有发言将记为「未知」，不分说话人：{e}"
                )
            except Exception:
                logger.exception("发送说话人区分降级提示失败")
        return NullDiarizer()
    logger.info(f"说话人区分已启动（最多 {diarizer.max_speakers} 个说话人）")
    return diarizer
