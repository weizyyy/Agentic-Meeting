"""发给页面的提示（docs/interfaces.md §6.1.1）：代码加参数，页面按自己的界面语言翻译。

每条提示同时带一句中文 ``text``，给日志和还不认得代码的页面用。新代码先写进 interfaces.md 的表，
再加到这里的 ``NOTICES``，并在 ``client/src/i18n/serverNotices.ts`` 里配上中英文；
``tests/test_repository_rules.py`` 检查三处一致。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

Params = Mapping[str, str | int]

# llm.unavailable / tts.unavailable 的 reason
REASONS: dict[str, Callable[[Params], str]] = {
    "auth": lambda p: "密钥被拒绝，请检查配置里的密钥变量",
    "not_found": lambda p: "接口地址或模型名不对（404）",
    "rate_limited": lambda p: "被服务端限流了，稍后再试",
    "server_error": lambda p: f"服务端出错（{p['status']}）",
    "timeout": lambda p: "请求超时",
    "connection": lambda p: "连不上服务",
    "other": lambda p: str(p["detail"]),
}


def _reason(params: Params) -> str:
    return REASONS[str(params["reason"])](params)


# 代码 → 中文。带参数的从 params 里取，参数名与 interfaces.md §6.1.1 的表一致。
NOTICES: dict[str, Callable[[Params], str]] = {
    "recording.active": lambda p: "会议正在转录，发言和共享画面会保存在服务器上",
    "mic.low_level": lambda p: "麦克风音量过低，请调高系统的输入音量或靠近麦克风",
    "asr.unavailable": lambda p: "识别服务暂时不可用，正在重试",
    "asr.recovered": lambda p: "识别服务已恢复",
    "diar.unavailable": lambda p: (
        f"说话人区分不可用，所有发言将记为「未知」，不分说话人：{p['error']}"
    ),
    "diar.failed": lambda p: f"说话人区分出错，之后的发言将记为「未知」：{p['error']}",
    "speakers.renumbered": lambda p: (
        "服务重启过，说话人编号可能与之前不同；认错的可以在说话人那里用「合并到…」收拾"
    ),
    "llm.unavailable": lambda p: f"助理暂不可用：{_reason(p)}",
    "tts.unavailable": lambda p: f"语音不可用，助理这次只出文字：{_reason(p)}",
    "text.malformed": lambda p: "文字消息的格式不对，没有发送",
    "text.empty": lambda p: "消息是空的，没有发送",
    "text.too_long": lambda p: f"消息太长（最多 {p['max']} 字），没有发送",
    "text.queue_full": lambda p: f"助理正忙，排队已满（{p['max']} 条），这条消息没有发送",
    "text.queued": lambda p: f"助理正忙，这条消息排在第 {p['position']} 位，稍后回答",
}


@dataclass(frozen=True)
class Message:
    """一条提示：代码和参数。``text`` 是它的中文。"""

    code: str
    params: Params = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in NOTICES:
            raise ValueError(f"未登记的提示代码：{self.code}")

    @property
    def text(self) -> str:
        return NOTICES[self.code](self.params)

    def notice(self, level: str) -> dict[str, Any]:
        """数据通道里的 ``notice`` 消息（interfaces.md §6.1）。"""
        return {
            "type": "notice",
            "level": level,
            "code": self.code,
            "params": dict(self.params),
            "text": self.text,
        }


def message(code: str, **params: str | int) -> Message:
    return Message(code, params)


# 发提示的回调：``(level, message)``，level 是 ``"info"`` 或 ``"warn"``。
Notice = Callable[[str, Message], Awaitable[None]]
