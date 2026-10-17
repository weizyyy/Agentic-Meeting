"""发给页面的提示和错误：代码加参数，页面按自己的界面语言翻译。

提示见 docs/interfaces.md §6.1.1，HTTP 接口的错误见 §5.9，任务和报告的失败原因见 §5.10。每条同时带一句中文
（提示的 ``text``、错误和失败原因的 ``error``），给日志、接口调用者、实时模型和还不认得代码的页面用。
新代码先写进 interfaces.md 的表，再加到这里的 ``NOTICES``、``ERRORS`` 或 ``FAILURES``，并在
``client/src/i18n/`` 对应的 ``serverNotices.ts``、``serverErrors.ts`` 或 ``serverFailures.ts`` 里配上中英文；
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
    "voice.active": lambda p: (
        "说话人记忆已开启：服务器会在本机计算发言人的声纹，用来认出记住过的人"
    ),
    "voice.unavailable": lambda p: "声纹服务暂时不可用，暂停采集声纹和提示，转录不受影响",
    "voice.recovered": lambda p: "声纹服务已恢复",
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


# HTTP 接口的错误：代码 → 中文（interfaces.md §5.9）。
ERRORS: dict[str, Callable[[Params], str]] = {
    "share.invalid": lambda p: "分享无效或已到期／撤销",
    "share.limit": lambda p: "本会议最多8个有效链接",
    "access.denied": lambda p: "无权执行此操作",
    "access.invalid_member": lambda p: "成员不存在或不能主持会议",
    "access.last_host": lambda p: "会议必须保留一位主持人",
    "access.busy": lambda p: "录音服务正忙",
    "text.context_too_large": lambda p: "问题和提示超出上下文预算",
    "text.interrupted": lambda p: "只读问答被实时应答打断，请稍后重新提问",
    "text.busy": lambda p: "只读问答队列已满或已有问题处理中",
    "text.timeout": lambda p: "只读问答等待超时",
    "text.failed": lambda p: "只读问答没有完成",
    "request.invalid": lambda p: f"请求格式有误：{p['detail']}",
    "request.too_large": lambda p: "请求体太大",
    "request.failed": lambda p: f"请求失败（{p['status']}）",
    "route.not_found": lambda p: "没有这个接口或页面",
    "auth.required": lambda p: "请先登录",
    "auth.csrf": lambda p: "请求缺少有效的安全令牌，请刷新页面后重试",
    "auth.wrong_password": lambda p: "口令不对",
    "auth.too_many": lambda p: "尝试次数过多，请稍后再试",
    "auth.not_enabled": lambda p: "没有启用访问口令",
    "auth.provider_not_found": lambda p: "没有这个登录方式",
    "sso.unavailable": lambda p: "暂时连不上身份提供方，请稍后再试或改用其他登录方式",
    "sso.cancelled": lambda p: "登录已取消",
    "sso.expired": lambda p: "登录请求已过期，请重新登录",
    "sso.denied": lambda p: "这个账号没有被允许登录",
    "sso.failed": lambda p: "单点登录没有完成，请重试",
    "storage.not_ready": lambda p: "存储尚未就绪",
    "cleanup.not_ready": lambda p: "清理尚未就绪",
    "session.not_found": lambda p: "找不到这场会议",
    "session.none": lambda p: "现在没有会议",
    "session.busy": lambda p: "会议正在处理或等待删除，请稍后重试",
    "session.not_live": lambda p: "这场会议现在没有在进行，消息没有发送",
    "session.capacity": lambda p: (
        f"已经有 {p['max']} 场会议在同时进行，请等其中一场结束或断开后再开始"
    ),
    "session.delete_incomplete": lambda p: "删除未完成，会议待删除，请稍后重试",
    "session.title_too_long": lambda p: f"标题太长（最多 {p['max']} 字）",
    "speaker.not_found": lambda p: "这场会议里没有这个说话人",
    "speaker.name_too_long": lambda p: f"名字太长（最多 {p['max']} 字）",
    "speaker.not_mergeable": lambda p: (
        "只能合并说话人区分给出的说话人（助理、文字输入、「未知」不能合并）"
    ),
    "speaker.merge_self": lambda p: "不能把说话人合并到自己",
    "utterances.too_many": lambda p: f"一次最多改 {p['max']} 条发言",
    "voice.consent_required": lambda p: "需要确认本人已同意保存声纹",
    "voice.not_enrollable": lambda p: "只能为说话人区分给出的说话人或手动新建的说话人录入声纹",
    "voice.too_short": lambda p: (
        f"这位说话人的单人语音还不够：已有 {p['have']} 秒，需要 {p['need']} 秒"
    ),
    "voice.profile_not_found": lambda p: "找不到这份声纹档案",
    "voice.profile_in_use": lambda p: "这份声纹档案已经关联到本场的另一位说话人",
    "voice.model_changed": lambda p: "这份声纹档案是用另一个声纹模型录入的，请新建一份",
    "frames.disabled": lambda p: "截图功能已在配置里关闭",
    "frames.no_live_session": lambda p: "现在没有进行中的会议，截图没有保存",
    "frame.not_found": lambda p: "找不到这张截图",
    "frame.bad_format": lambda p: "只接受 WebP 或 JPEG 格式的截图",
    "frame.too_wide": lambda p: f"截图的长边不能超过 {p['max']} 像素",
    "frame.invalid": lambda p: "上传的文件不是有效的图片",
    "frame.too_large": lambda p: f"截图太大（最多 {p['max_mb']} MB）",
    "frame.empty": lambda p: "上传的文件是空的",
    "frame.clock_skew": lambda p: (
        f"截图的采集时间与服务端时间相差超过 {p['max']} 秒，请刷新页面重新对时"
    ),
    "frame.save_failed": lambda p: "截图保存失败（磁盘不可写？）",
    "task.not_found": lambda p: "找不到这个任务",
    "tasks.disabled": lambda p: "后台任务功能没有开启",
    "artifact.not_found": lambda p: "找不到这个产物文件",
    "report.session_live": lambda p: "会议正在进行，结束或断开之后才能生成报告",
    "report.no_model": lambda p: "没有可用的模型，生成不了报告",
    "report.busy": lambda p: "这场会议已经有一份报告正在生成",
    "report.not_found": lambda p: "这场会议还没有报告",
    "report.not_ready": lambda p: "这场会议还没有生成好的报告",
}

# 任务和报告的失败原因：代码 → 中文（interfaces.md §5.10）。
FAILURES: dict[str, Callable[[Params], str]] = {
    "task.cancelled": lambda p: "已取消",
    "task.restarted": lambda p: "服务重启，任务没有做完",
    "task.shutdown": lambda p: "服务停止，任务没有做完",
    "task.timeout": lambda p: f"超时（超过 {p['minutes']} 分钟没有做完）",
    "task.internal": lambda p: f"内部错误（{p['type']}）",
    "agent.disabled": lambda p: "后台任务功能已在配置里关闭",
    "agent.not_configured": lambda p: (
        "后台 agent 还没有配置（config.toml 的 [agent] 里要填模型地址和名字）"
    ),
    "agent.not_installed": lambda p: "没有安装后台 agent 的依赖（uv sync --extra agent）",
    "agent.stopping": lambda p: "服务正在停止",
    "agent.too_many_steps": lambda p: f"步骤太多（超过 {p['max']} 步）还没有做完",
    "agent.key_env_missing": lambda p: (
        "远端模型要求密钥，但配置里没有写密钥的环境变量名（agent.api_key_env）"
    ),
    "agent.key_unset": lambda p: (
        f"远端模型的密钥没有设置（环境变量 {p['env']} 是空的；设好之后要重新启动）"
    ),
    "agent.key_rejected": lambda p: f"远端模型不接受这个密钥（环境变量 {p['env']}）",
    "agent.unreachable": lambda p: "连不上远端模型",
    "agent.http_error": lambda p: f"远端模型返回了错误（{p['status']}）",
    "agent.error": lambda p: f"agent 运行出错（{p['type']}）",
    "report.interrupted": lambda p: "服务停止了，这份报告没有生成完",
    "report.timeout": lambda p: "生成超时",
    "report.failed": lambda p: f"{p['detail']}",
}

_TEMPLATES = {**NOTICES, **ERRORS, **FAILURES}


@dataclass(frozen=True)
class Message:
    """一条提示或错误：代码和参数。``text`` 是它的中文。"""

    code: str
    params: Params = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in _TEMPLATES:
            raise ValueError(f"未登记的代码：{self.code}")

    @property
    def text(self) -> str:
        return _TEMPLATES[self.code](self.params)

    def notice(self, level: str) -> dict[str, Any]:
        """数据通道里的 ``notice`` 消息（interfaces.md §6.1）。"""
        return {
            "type": "notice",
            "level": level,
            "code": self.code,
            "params": dict(self.params),
            "text": self.text,
        }

    def error(self) -> dict[str, Any]:
        """HTTP 接口的错误响应体（interfaces.md §5.9）。"""
        return {"error": self.text, "code": self.code, "params": dict(self.params)}


def message(code: str, **params: str | int) -> Message:
    return Message(code, params)


# 发提示的回调：``(level, message)``，level 是 ``"info"`` 或 ``"warn"``。
Notice = Callable[[str, Message], Awaitable[None]]
