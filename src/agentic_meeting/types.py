"""跨模块共享的数据类型。

这些类型是模块之间的契约：ASR、说话人区分、融合、存储、界面消息都只通过它们交换数据。
改动字段前先看 docs/interfaces.md，并同步更新那里的说明。

时间一律用「会话时间轴」上的秒（float）：0 = 本次会议的音频第一帧。会话时间轴由服务端
按收到的音频采样数推进（见 docs/architecture.md §3），不使用各模块自己的墙上时钟。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# 说话人编号约定。说话人区分模型输出 1 起始的编号（按首次出现的顺序）。
SPEAKER_UNKNOWN = 0  # 尚未判定，或说话人区分已关闭
SPEAKER_ASSISTANT = -1  # 助理自己的发言
SPEAKER_TYPED = -2  # 在浏览器输入框里键入的文字（没有声音，也就没有说话人）


@dataclass(frozen=True, slots=True)
class ASRDelta:
    """流式识别的一次增量输出。

    语义是「只追加」：所有 ``stable_text`` 按到达顺序拼接就是定稿全文，永不回改。
    ``unstable_text`` 是当前尚未定稿的尾巴，仅用于实时字幕显示，下一次输出会整体替换它。
    """

    stable_text: str
    unstable_text: str
    # 产生本次输出时，已送入识别器的音频在会话时间轴上的终点（秒）。
    audio_end_secs: float
    # True 表示这是一次段落收尾（停顿触发的 flush）：尾巴已全部定稿，识别窗口已清空。
    segment_end: bool = False


@dataclass(frozen=True, slots=True)
class SpeakerSegment:
    """说话人区分给出的一段「谁在说话」。同一时刻可以有多段重叠（多人同时说话）。"""

    start_secs: float
    end_secs: float
    speaker: int  # 1 起始


@dataclass(slots=True)
class Utterance:
    """落库的一条发言（转录的最小展示/检索单元）。"""

    session_id: str
    speaker_idx: int
    t_start: float
    t_end: float
    text: str
    source: str = "asr"  # "asr" | "assistant" | "text"
    addressed_to_assistant: bool = False
    id: int | None = None


@dataclass(slots=True)
class Session:
    """一场会议。状态（进行中 / 已中断 / 已结束）由 ``ended_at`` 与应用内存里有没有活动连接合起来推出。"""

    id: str
    started_at: float  # 会话时间轴 0 点对应的 Unix 时间
    title: str = ""
    ended_at: float | None = None
    last_active_at: float = 0.0


@dataclass(frozen=True, slots=True)
class SpeakerInfo:
    idx: int
    display_name: str


@dataclass(slots=True)
class Connection:
    """一次浏览器连接覆盖的区间。``t_from`` / ``t_to`` 是会话时间轴上的秒。"""

    id: int
    session_id: str
    connected_at: float
    t_from: float
    disconnected_at: float | None = None
    t_to: float | None = None


@dataclass(frozen=True, slots=True)
class NamedUtterance:
    """发言加上说话人当前的显示名（改名后立即变化）。"""

    utterance: Utterance
    speaker_name: str


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """会议列表里的一行。"""

    session: Session
    duration_secs: float  # 各次连接实际时长之和，不含中断的空档
    utterance_count: int
    speakers: list[str]  # 说过话的人的显示名（不含助理和键入的文字）
    preview: list[tuple[str, str]]  # 最后两条发言：(说话人显示名, 文字)
    has_open_connection: bool  # 有没关闭的连接记录：正在进行，或服务崩溃过


@dataclass(slots=True)
class ScreenFrame:
    """一张屏幕截图的元数据；图片本体存为文件。"""

    session_id: str
    t: float
    path: str  # 相对 data_dir
    width: int
    height: int
    caption: str | None = None
    id: int | None = None
    caption_status: str = "pending"  # "pending" | "done" | "failed" | "skipped"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class TaskRecord:
    """任务表里的一行。``id`` 全库唯一（``<会话 id>.t<序号>``），``label`` 是会议内的短编号（``t3``）。"""

    id: str
    session_id: str
    goal: str
    requested_by: int = 0  # 说话人编号
    requested_t: float = 0.0
    status: str = "queued"
    brief: str | None = None
    detail_md: str | None = None
    sources: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)  # 任务目录下的文件名
    error: str | None = None
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    announced: bool = False
    modality: str = "voice"  # 委托当时的应答模态："voice" | "text"
    t_from: float = 0.0  # 带给 agent 的转录时间范围
    t_to: float = 0.0
    frame_ids: list[int] = field(default_factory=list)  # 带给 agent 的截图

    @property
    def label(self) -> str:
        return self.id.rsplit(".", 1)[-1]

    @property
    def finished(self) -> bool:
        return self.status in ("succeeded", "failed", "cancelled")


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """任务的一条进度。``summary`` 是一句能直接念给用户听的中文。"""

    id: int
    task_id: str
    at: float
    kind: str  # "status" | "step" | "tool_call" | "tool_result" | "note"
    summary: str
    payload: dict | None = None


@dataclass(slots=True)
class TaskResult:
    """后台 agent 的交付物。``brief`` 给实时模型口播，``detail_md`` 给界面展示。"""

    brief: str
    detail_md: str = ""
    sources: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)  # 相对 data_dir 的文件路径


@dataclass(frozen=True, slots=True)
class Digest:
    """一份滚动纪要。``text`` 是累积的（覆盖从会议开头到 ``t_to``）；``t_from`` / ``t_to`` 只记这一次新纳入的那一段。"""

    id: int
    session_id: str
    t_from: float
    t_to: float
    text: str
    created_at: float
    last_utterance_id: int = 0  # 已经纳入到哪条发言为止


@dataclass(frozen=True, slots=True)
class Report:
    """一份会后报告（``reports`` 表的一行）。同一场会议可以有多份，页面显示最近一份。"""

    id: int
    session_id: str
    created_at: float
    status: str  # "running" | "done" | "failed"
    provider: str = ""  # 由谁生成："realtime_llm" | "agent_llm"
    text_md: str = ""
    error: str | None = None
