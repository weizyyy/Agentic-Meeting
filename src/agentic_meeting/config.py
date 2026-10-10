"""配置加载与校验。

设计约束（见 docs/development.md）：

* 代码里不出现任何具体模型名或权重文件名。模型由用户在 config/config.toml 里填写，
  换模型只改配置、不改代码。
* 密钥不写进配置文件，只写「环境变量名」（``*_env`` 字段），运行时从环境或 .env 读取。
* 结构校验（字段类型、取值范围）在加载时完成；「文件是否存在、程序是否装好」属于
  就绪检查，由 :func:`check_ready` 单独给出，便于一次性列出所有缺项。

所有字段的含义与默认值的取舍见 docs/interfaces.md §1。
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "config.toml"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "config" / "config.example.toml"


class _Model(BaseModel):
    """所有配置段的基类：禁止未知字段，拼错键名时立即报错而不是静默忽略。"""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# 通用：受管子进程
# --------------------------------------------------------------------------- #


class LaunchSpec(_Model):
    """由本项目的进程管理器拉起的本地推理服务。

    ``enabled = false`` 表示服务已由用户自行启动（或在另一台机器上），本项目只按
    ``base_url`` 连接，不负责进程生命周期。
    """

    enabled: bool = True
    # 留空则到 runtimes/<runtime>/ 与 third_party/<runtime>/build/ 下自动查找。
    executable: str = ""
    # 透传给子进程的环境变量。多显卡分配靠它：{ CUDA_VISIBLE_DEVICES = "1" }。
    env: dict[str, str] = Field(default_factory=dict)
    # 追加在自动生成的命令行之后，原样传递。
    extra_args: list[str] = Field(default_factory=list)
    # 启动后等待健康检查通过的最长秒数（加载大模型较慢）。
    startup_timeout_secs: float = 180.0


class LlamaServerLaunch(LaunchSpec):
    """llama-server 的启动参数（实时 LLM / ASR / 嵌入 共用）。"""

    model_path: str = ""  # GGUF 权重，用户必填
    mmproj_path: str = ""  # 多模态投影（识图 / 音频输入时必填）
    ctx_size: int = Field(default=0, ge=0)  # 0 = 用模型自带上限
    parallel: int = Field(default=1, ge=1)
    gpu_layers: str = "all"  # 透传给 -ngl：数字 / "auto" / "all"；"0" 表示纯 CPU


# --------------------------------------------------------------------------- #
# 会话与服务端
# --------------------------------------------------------------------------- #


_ENGLISH_PHRASE = re.compile(r"[A-Za-z][A-Za-z0-9]*( [A-Za-z0-9]+)*")


def _english_phrase(value: str, label: str) -> str:
    value = " ".join(value.split())
    if not _ENGLISH_PHRASE.fullmatch(value):
        raise ValueError(
            f"{label} 必须是英文单词（字母开头，可含数字，多个词用空格分开），例如 Jarvis；"
            f"当前值是 {value!r}"
        )
    return value


# 汉字的基本区（U+4E00 到 U+9FFF）
_CJK_PHRASE = re.compile(f"[{chr(0x4E00)}-{chr(0x9FFF)}]{{2,}}")


def _wake_alias(value: str) -> str:
    """唤醒词的别名：英文词（规则同助理的名字），或者至少两个汉字。"""
    value = " ".join(value.split())
    if _ENGLISH_PHRASE.fullmatch(value) or _CJK_PHRASE.fullmatch(value):
        return value
    raise ValueError(
        "wake_aliases 里的每一项必须是英文单词（字母开头，可含数字，多个词用空格分开），"
        f"或者至少两个汉字（识别把名字听成的那几个字）；当前值是 {value!r}"
    )


class SessionConfig(_Model):
    data_dir: str = "data"
    # 助理的名字。同时是唤醒词，也会写进系统提示词。必须是英文单词（可含数字、可多个词）：
    # 唤醒靠在识别文字里找这个词，英文词在中文句子里边界清楚、不会和同音字混淆。
    assistant_name: str
    # 唤醒词的其他写法：识别有时把名字写成的另一种拼写（英文），或听成的汉字（至少两个字，如音译）。
    # assistant_name 会自动并入。别名只用来唤醒，不进系统提示词，也不作为识别的热词。
    wake_aliases: list[str] = Field(default_factory=list)
    # 传给 ASR 的热词/上下文提示：成员姓名、课题术语、英文缩写。
    hotwords: list[str] = Field(default_factory=list)
    # 预先登记的成员名单，仅用于界面里给说话人编号改名时做候选。
    members: list[str] = Field(default_factory=list)

    @field_validator("assistant_name")
    @classmethod
    def _name_is_english(cls, v: str) -> str:
        return _english_phrase(v, "assistant_name")

    @field_validator("wake_aliases")
    @classmethod
    def _aliases_are_words(cls, v: list[str]) -> list[str]:
        return [_wake_alias(item) for item in v]

    @property
    def wake_phrases(self) -> list[str]:
        seen: list[str] = []
        for phrase in [self.assistant_name, *self.wake_aliases]:
            if phrase and phrase not in seen:
                seen.append(phrase)
        return seen


class ServerConfig(_Model):
    host: str = "0.0.0.0"
    port: int = Field(default=7860, ge=1, le=65535)
    # 局域网访问必须 HTTPS（浏览器采集麦克风/屏幕要求安全上下文）。二者都留空 = 纯 HTTP，
    # 只能用 http://localhost 访问。
    tls_cert: str = ""
    tls_key: str = ""
    # WebRTC ICE 服务器。同一局域网内留空即可。
    ice_servers: list[str] = Field(default_factory=list)
    # 访问口令所在的环境变量名。留空 = 不需要登录（只在本机或可信网络里用）。见 docs/interfaces.md §5.7。
    password_env: str = ""
    # 登录一次的有效天数，到期需要重新输入口令。
    auth_session_days: float = Field(default=7.0, gt=0, le=365)


# --------------------------------------------------------------------------- #
# 本地推理服务
# --------------------------------------------------------------------------- #


class SamplingConfig(_Model):
    """留空（None）的字段不发送，由服务端/模型默认值决定。"""

    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    presence_penalty: float | None = None
    max_tokens: int | None = 512


class LLMEndpoint(_Model):
    """实时模型一种接入方式的完整设置。

    两种接入方式各存一整套（地址、模型名、是否识图、采样参数……），互不共用字段：
    这样可以把两边都填好，只改 ``realtime_llm.mode`` 一行就能切换。
    """

    base_url: str
    api_key_env: str = ""
    # 请求体里的 model 字段。
    model: str
    supports_vision: bool = True
    # 合并进请求体的额外字段（OpenAI SDK 不认识的参数都放这里）。
    extra_body: dict = Field(default_factory=dict)
    sampling: SamplingConfig = Field(default_factory=SamplingConfig)


class LlamaServerEndpoint(LLMEndpoint):
    """接入方式一：用 llama.cpp 的 llama-server 部署（本项目可代为启动）。"""

    base_url: str = "http://127.0.0.1:8080/v1"
    # 是否允许模型输出思考段。实时路径必须 false，否则首字延迟会成倍增加。
    # 由本项目启动服务时对应 --reasoning on|off。
    thinking: bool = False
    # 槽位分工：实时应答与缓存预热用一个，后台任务（画面摘要、滚动纪要）用另一个。
    realtime_slot: int = Field(default=0, ge=0)
    background_slot: int = Field(default=1, ge=0)
    launch: LlamaServerLaunch = Field(default_factory=lambda: LlamaServerLaunch(parallel=2))


class OpenAIAPIEndpoint(LLMEndpoint):
    """接入方式二：任意 OpenAI 兼容的 chat completions 接口（本机、局域网或云端均可）。

    本项目不管理它的进程，也不假设任何服务端特性。关闭思考、指定推理档位等做法因服务而异，
    一律通过 ``extra_body`` 由用户填写。
    """

    # 该接口是否认识 "developer" 角色。不确定就保持 false（会被转成 "user"）。
    supports_developer_role: bool = False
    # 是否做缓存预热。只有服务端支持前缀缓存、且预热请求不产生可观费用时才值得打开。
    cache_warm: bool = False


class RealtimeLLMConfig(_Model):
    """实时应答模型。``mode`` 选择接入方式，对应的小节必须填写；另一节可留着备用。

    业务代码不要自己判断 mode：地址、模型名等从 :attr:`active` 取，请求附加字段用
    :meth:`request_extra_body`，其余差异用下面的几个属性。
    """

    mode: Literal["llama_server", "openai_api"]
    llama_server: LlamaServerEndpoint | None = None
    openai_api: OpenAIAPIEndpoint | None = None

    @model_validator(mode="after")
    def _selected_section_present(self) -> RealtimeLLMConfig:
        if getattr(self, self.mode) is None:
            raise ValueError(
                f'realtime_llm.mode = "{self.mode}"，但没有填写 [realtime_llm.{self.mode}] 小节'
            )
        return self

    @property
    def active(self) -> LLMEndpoint:
        """当前生效的那一套设置。"""
        endpoint = self.llama_server if self.mode == "llama_server" else self.openai_api
        assert endpoint is not None  # 已由校验器保证
        return endpoint

    @property
    def managed(self) -> bool:
        """是否由本项目的进程管理器负责启动实时模型服务。"""
        return (
            self.mode == "llama_server"
            and self.llama_server is not None
            and self.llama_server.launch.enabled
        )

    @property
    def supports_developer_role(self) -> bool:
        """服务端是否认识 developer 角色；llama-server 加载的本地模型一般不认识。"""
        return self.mode == "openai_api" and self.active.supports_developer_role  # type: ignore[attr-defined]

    @property
    def cache_warm(self) -> bool:
        """是否做缓存预热。llama-server 始终做；通用接口按配置。"""
        if self.mode == "llama_server":
            return True
        return self.active.cache_warm  # type: ignore[attr-defined]

    def request_extra_body(self, *, background: bool = False) -> dict:
        """每次请求要并入 ``extra_body`` 的字段。

        - 用户在配置里写的 ``extra_body`` 原样带上。
        - ``sampling.top_k`` 不是 OpenAI SDK 的标准参数，也放这里。
        - llama_server 方式额外带槽位号和 ``cache_prompt``：实时应答与后台任务各用一个槽位，
          互不冲掉对方的前缀缓存。通用接口没有这些概念，不带。
        """
        endpoint = self.active
        body: dict = dict(endpoint.extra_body)
        if endpoint.sampling.top_k is not None:
            body.setdefault("top_k", endpoint.sampling.top_k)
        if self.mode == "llama_server":
            assert self.llama_server is not None
            slot = (
                self.llama_server.background_slot if background else self.llama_server.realtime_slot
            )
            body["id_slot"] = slot
            body["cache_prompt"] = True
        return body


class ASRProfile(_Model):
    """识别模型的提示词格式档案（模型相关，存放在 config/asr_profiles/*.toml）。"""

    chat_template: str
    assistant_prefix: str = ""
    text_marker: str = ""
    cut_markers: list[str] = Field(default_factory=list)
    hotwords_template: str = "{hotwords}"
    hotwords_joiner: str = " "


class ASRConfig(_Model):
    """流式语音识别。后端可插拔，新增后端见 asr/__init__.py。"""

    backend: Literal["llama_server"] = "llama_server"
    # 提示词格式档案的路径；换识别模型时换档案，不改代码。
    profile: str
    base_url: str = "http://127.0.0.1:8081"
    language: str = "Chinese"  # 模型提示用的语言名；填 "" 表示让模型自动判断
    chunk_ms: int = Field(default=320, ge=80, le=2000)  # 每步送入的新增音频时长
    window_secs: float = Field(default=16.0, ge=4.0, le=30.0)  # 滚动音频窗口上限
    window_drop_secs: float = Field(default=8.0, ge=1.0)  # 超限时一次丢弃的最早音频
    # 每一步末尾暂不定稿、留待下一步重新生成的 token 数（按模型自己的分词回退）。
    unfixed_tokens: int = Field(default=1, ge=0)
    # 单次请求生成 token 数的上限。流式的每一步按新增音频时长另算预算（远小于它），
    # 只有段落收尾时才会用到这个上限。
    max_new_tokens: int = Field(default=32, ge=1)
    # 句首回补：语音检测判定「开始说话」之前这么长的音频也一并送去识别（只取上一段说完之后的音频，不会重复）。
    # 要够长：叫名字常常是「名字，（停一下）要求」，名字很短，检测往往到后半句才触发；回补太短名字就被切掉，
    # 助理不应答（实测 500 毫秒时约四成叫不醒，1500 毫秒时 12 次里 11 次，见 docs/benchmarks.md）。
    preroll_ms: int = Field(default=1500, ge=0, le=5000)
    launch: LlamaServerLaunch = Field(default_factory=LlamaServerLaunch)


class SegmentationConfig(_Model):
    """说话人分段后处理阈值；0 表示用运行库默认值。"""

    onset: float = 0.0
    offset: float = 0.0
    pad_onset_secs: float = 0.0
    pad_offset_secs: float = 0.0
    min_gap_secs: float = 0.0
    min_duration_secs: float = 0.0


class DiarizationConfig(_Model):
    backend: Literal["nemo_ctypes", "none"] = "nemo_ctypes"
    # NeMo-Speech.cpp 的动态库（nemo_speech_asr_c）。留空则到 runtimes/nemo_speech/ 下查找。
    library_path: str = ""
    model_path: str = ""  # 说话人区分 GGUF，用户必填（backend != "none" 时）
    gpu: int = 0  # GPU 序号；-1 = CPU
    preset: str = ""  # 留空 = 模型自带的低延迟默认
    poll_interval_ms: int = Field(default=320, ge=80)
    segmentation: SegmentationConfig = Field(default_factory=SegmentationConfig)


class TTSLaunch(LaunchSpec):
    model_path: str = ""  # talker GGUF，用户必填
    codec_path: str = ""  # tokenizer/codec GGUF，用户必填
    default_language: str = "Chinese"


class TTSConfig(_Model):
    """OpenAI 兼容的 /v1/audio/speech 端点，要求支持 response_format=pcm 流式输出。"""

    enabled: bool = True
    base_url: str = "http://127.0.0.1:8082/v1"
    api_key_env: str = ""
    model: str  # 请求体里的 model 字段（tts-server 的 --alias）
    voice: str  # 音色名，取决于所选权重
    language: str = "Chinese"
    sample_rate: int = 24000
    launch: TTSLaunch = Field(default_factory=TTSLaunch)


class EmbeddingConfig(_Model):
    enabled: bool = True
    base_url: str = "http://127.0.0.1:8083/v1"
    model: str
    dimensions: int = Field(gt=0)  # 必须与模型输出维度一致，建库时写入向量表定义
    # 召回时加在查询前面的任务指令（有的嵌入模型推荐给查询加，给被检索的文本不加）；留空 = 不加。
    query_prefix: str = ""
    # 语义召回的相关度门槛（余弦相似度）：查询与发言的相似度低于它的不算命中。0 = 不设门槛。
    # 0.4 是按经验定的起始值；换嵌入模型、或给查询加了任务指令前缀之后，合适的值会变。
    min_similarity: float = Field(default=0.4, ge=0, le=1)
    launch: LlamaServerLaunch = Field(default_factory=lambda: LlamaServerLaunch(gpu_layers="0"))


# --------------------------------------------------------------------------- #
# 实时行为
# --------------------------------------------------------------------------- #


class AudioConfig(_Model):
    """入口收音增强（audio/gain.py）。麦克风电平偏低时把说话声拉到合适范围，见 docs/architecture.md §4。"""

    # 自动增益：把「较响音节」的 RMS 调到 target_dbfs。只增不减，上限 max_gain_db。
    auto_gain: bool = True
    # 起始增益；auto_gain 关闭时就是固定增益。
    gain_db: float = Field(default=0.0, ge=-20, le=60)
    max_gain_db: float = Field(default=30.0, ge=0, le=60)
    # 较响音节的目标电平。峰值有软限幅兜着。
    target_dbfs: float = Field(default=-16.0, ge=-50, le=-6)
    # 帧 RMS 低于它视为静音，不参与增益调整（也不会被越拉越大）。
    noise_floor_dbfs: float = Field(default=-70.0, ge=-100, le=-30)
    # 周期性输入电平日志的间隔（按音频时长计，0 = 关闭）。
    level_log_secs: float = Field(default=10.0, ge=0)


class TurnConfig(_Model):
    vad_stop_secs: float = Field(default=0.2, gt=0)
    # 语音检测的音量门限（Pipecat 的 VADParams.min_volume，0.6 ≈ −50 LUFS，每低 6 dB 约少 0.06）。
    # 麦克风电平偏低而又不想开自动增益时调低它；0 = 关闭这道门限，只看模型置信度。
    vad_min_volume: float = Field(default=0.6, ge=0, le=1)
    smart_turn: bool = True
    # 唤醒窗口：叫一次名字之后最多醒多少秒。single_activation 为 true 时助理一答完就回到待唤醒状态，
    # 所以它是「从叫名字到答完」的上限，也是能用声音打断朗读的时间（pipecat-notes.md §3.3）。
    wake_timeout_secs: float = Field(default=30.0, gt=0)
    single_activation: bool = True


class RealtimeConfig(_Model):
    # 送入实时模型的上下文 token 上限；超过后在空闲期压缩。
    context_budget_tokens: int = Field(default=24000, ge=2000)
    # 压缩后保留的最近原文时长。
    keep_recent_minutes: float = 10.0
    digest_interval_minutes: float = 5.0
    # 滚动纪要由谁写：实时模型的后台实例，或后台 agent 的那个远端模型（转录会发给它）。
    digest_provider: Literal["realtime_llm", "agent_llm"] = "realtime_llm"
    cache_warm_interval_secs: float = 30.0
    # 允许实时模型直接调用的 MCP 工具名（留空 = 实时路径不直连 MCP，全部走后台 agent）。
    direct_mcp_tools: list[str] = Field(default_factory=list)


class ScreenConfig(_Model):
    enabled: bool = True
    min_interval_secs: float = 2.0  # 画面变化触发的最小间隔
    heartbeat_secs: float = 60.0  # 画面不变时的兜底间隔
    max_side_px: int = 1920
    change_threshold: float = Field(default=0.04, ge=0, le=1)
    caption: bool = True  # 是否生成画面文字摘要
    caption_provider: Literal["realtime_llm", "agent_llm"] = "realtime_llm"


class TranscriptConfig(_Model):
    """发言怎么分条（diar/fusion.py 的规则 6）。语音检测按停顿切段，这里把同一个人挨得很近的几段并成一条。"""

    # 同一个人的两段之间停顿不超过这么久就并成一条；0 = 不并，一次停顿一条。
    merge_gap_secs: float = Field(default=2.0, ge=0, le=30)
    # 一条发言已经有这么多字、并且停在句末（。！？）时，下一段另起一条。
    merge_soft_chars: int = Field(default=40, ge=1)
    # 一条发言最多并到这么多字。
    merge_max_chars: int = Field(default=200, ge=1)


class ReportConfig(_Model):
    """会后报告。"""

    # 由谁写：实时模型的后台实例，或后台 agent 的那个远端模型（整场会议的转录会发给它）。
    provider: Literal["realtime_llm", "agent_llm"] = "realtime_llm"
    # 一次请求里最多放多少字的转录；超过就分段提要点、再合并。要给提示词和输出留余地，
    # 按所选模型的上下文长度调。
    max_input_chars: int = Field(default=8000, ge=500)


# --------------------------------------------------------------------------- #
# 后台 agent
# --------------------------------------------------------------------------- #


class MCPServerConfig(_Model):
    name: str
    url: str  # 流式传输 HTTP（streamable HTTP）端点
    # 认证头：{ 头名 = 环境变量名 }，值在运行时读取。
    headers_env: dict[str, str] = Field(default_factory=dict)
    timeout_secs: float = 30.0


class SandboxConfig(_Model):
    kind: Literal["docker", "local", "none"] = "docker"
    docker_image: str = ""  # kind = "docker" 时必填
    network: bool = False
    timeout_secs: float = 120.0


class AgentConfig(_Model):
    enabled: bool = True
    framework: Literal["openai_agents", "smolagents"] = "openai_agents"
    base_url: str  # 远端 LLM 的 OpenAI 兼容 chat completions 端点
    api_key_env: str
    model: str
    supports_vision: bool = True
    # 发给这个模型的每个请求都并入的字段（后台任务，以及它被拿来写会后报告、生成画面摘要时），写法因服务而异。
    # 一般用来指定思考的强度，例如 {"reasoning_effort": "medium"}。做任务要靠思考来规划和调用工具，不建议关掉。
    extra_body: dict = Field(default_factory=dict)
    # 交给这个模型的活（后台任务，以及由它写的会后报告、滚动纪要）要不要带上会议里的截图原图。
    # 关着时这些环节只看得到截图的文字摘要（任务在实时模型认为需要时带最近的几张）。打开后带全部截图：
    # 「无关画面」和画面没变的重复截图除外，一次请求最多 max_attached_frames 张（超过就均匀抽取）。
    # 图片很占上下文（一张 1920×1080 的图约 2000 token），按模型的上下文长度定上限。
    attach_frames: bool = False
    max_attached_frames: int = Field(default=40, ge=1, le=999)
    # 它直接生成文字时的输出上限（token）。会思考的模型思考也算在里面，给小了正文会是空的。
    generation_max_tokens: int = Field(default=16384, ge=256)
    max_turns: int = 30
    task_timeout_secs: float = 900.0
    max_concurrent_tasks: int = 2
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)


# --------------------------------------------------------------------------- #
# 顶层
# --------------------------------------------------------------------------- #


class AppConfig(_Model):
    session: SessionConfig
    server: ServerConfig = Field(default_factory=ServerConfig)
    realtime_llm: RealtimeLLMConfig
    asr: ASRConfig
    diarization: DiarizationConfig = Field(default_factory=DiarizationConfig)
    tts: TTSConfig
    embedding: EmbeddingConfig
    audio: AudioConfig = Field(default_factory=AudioConfig)
    turn: TurnConfig = Field(default_factory=TurnConfig)
    realtime: RealtimeConfig = Field(default_factory=RealtimeConfig)
    screen: ScreenConfig = Field(default_factory=ScreenConfig)
    transcript: TranscriptConfig = Field(default_factory=TranscriptConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    agent: AgentConfig

    def resolve(self, path: str) -> Path:
        """把配置里的相对路径解析为绝对路径（相对仓库根目录）。"""
        p = Path(path).expanduser()
        return p if p.is_absolute() else REPO_ROOT / p


ENV_FILE_PATH = REPO_ROOT / ".env"


def load_env_file(path: str | os.PathLike | None = None) -> bool:
    """把仓库根目录的 ``.env`` 读进进程环境（密钥放在那里）。已经在环境里的变量不覆盖。返回是否读到了文件。

    固定读仓库根目录的那一份，不看当前目录——从哪里启动都一样。
    """
    env_path = Path(path) if path is not None else ENV_FILE_PATH
    if not env_path.is_file():
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv 随 pipecat 安装；缺失时只是不读 .env
        return False
    load_dotenv(env_path, override=False)
    return True


def load_config(path: str | os.PathLike | None = None) -> AppConfig:
    """读取并校验配置。

    查找顺序：显式参数 → 环境变量 ``AGENTIC_MEETING_CONFIG`` → config/config.toml。
    不给路径（用默认位置的正式配置）时顺带读 ``.env``：不只是命令行入口，脚本和需要外部服务的测试也都从这里进来，
    密钥得在这里就位。显式给了路径的（单元测试读配置模板）不读，免得本机的密钥混进测试。
    """
    if path is None:
        load_env_file()
    cfg_path = Path(path or os.environ.get("AGENTIC_MEETING_CONFIG") or DEFAULT_CONFIG_PATH)
    if not cfg_path.is_file():
        raise FileNotFoundError(
            f"找不到配置文件 {cfg_path}。请复制 {EXAMPLE_CONFIG_PATH.name} 为 config.toml 并填写。"
        )
    with cfg_path.open("rb") as f:
        return AppConfig.model_validate(tomllib.load(f))


def load_asr_profile(cfg: AppConfig) -> ASRProfile:
    """读取 ``asr.profile`` 指向的提示词格式档案。"""
    path = cfg.resolve(cfg.asr.profile)
    if not path.is_file():
        raise FileNotFoundError(f"asr.profile 指向的档案不存在：{path}")
    with path.open("rb") as f:
        return ASRProfile.model_validate(tomllib.load(f))


MIN_PASSWORD_CHARS = 8


def secret(env_name: str) -> str | None:
    """按环境变量名取密钥；名字为空时返回 None。"""
    return os.environ.get(env_name) if env_name else None


def check_ready(cfg: AppConfig) -> list[str]:
    """就绪检查：返回人类可读的缺项列表，空列表表示可以启动。

    只检查「用户必须自己准备」的东西：权重文件、密钥环境变量、容器镜像名。
    推理程序是否装好由 scripts/runtimes.py status 负责。
    """
    problems: list[str] = []

    def need_file(label: str, value: str) -> None:
        if not value:
            problems.append(f"{label} 未填写")
        elif not cfg.resolve(value).is_file():
            problems.append(f"{label} 指向的文件不存在：{cfg.resolve(value)}")

    def need_env(label: str, env_name: str) -> None:
        if env_name and not os.environ.get(env_name):
            problems.append(f"{label} 需要环境变量 {env_name}，当前未设置")

    def need_value(label: str, value: str) -> None:
        if not value.strip():
            problems.append(f"{label} 未填写")

    rt = cfg.realtime_llm
    section = f"realtime_llm.{rt.mode}"
    need_value(f"{section}.base_url", rt.active.base_url)
    need_value(f"{section}.model", rt.active.model)
    need_env(f"{section}.api_key_env", rt.active.api_key_env)
    if rt.mode == "llama_server" and rt.llama_server is not None:
        if rt.llama_server.realtime_slot == rt.llama_server.background_slot:
            problems.append(f"{section}.realtime_slot 与 background_slot 不能相同")
        launch = rt.llama_server.launch
        if launch.enabled:
            need_file(f"{section}.launch.model_path", launch.model_path)
            if rt.llama_server.supports_vision:
                need_file(f"{section}.launch.mmproj_path", launch.mmproj_path)
            slots_needed = max(rt.llama_server.realtime_slot, rt.llama_server.background_slot) + 1
            if launch.parallel < slots_needed:
                problems.append(
                    f"{section}.launch.parallel = {launch.parallel}，"
                    f"但槽位设置需要至少 {slots_needed} 个并发槽位"
                )

    try:
        load_asr_profile(cfg)
    except (FileNotFoundError, ValueError) as e:
        problems.append(f"asr.profile 无法加载：{e}")
    if cfg.asr.launch.enabled:
        need_file("asr.launch.model_path", cfg.asr.launch.model_path)
        need_file("asr.launch.mmproj_path", cfg.asr.launch.mmproj_path)

    if cfg.diarization.backend != "none":
        need_file("diarization.model_path", cfg.diarization.model_path)

    if cfg.tts.enabled:
        need_value("tts.voice", cfg.tts.voice)
        if cfg.tts.launch.enabled:
            need_file("tts.launch.model_path", cfg.tts.launch.model_path)
            need_file("tts.launch.codec_path", cfg.tts.launch.codec_path)
        need_env("tts.api_key_env", cfg.tts.api_key_env)

    if cfg.embedding.enabled and cfg.embedding.launch.enabled:
        need_file("embedding.launch.model_path", cfg.embedding.launch.model_path)

    if cfg.agent.enabled:
        need_value("agent.base_url", cfg.agent.base_url)
        need_value("agent.model", cfg.agent.model)
        need_env("agent.api_key_env", cfg.agent.api_key_env)
        for server in cfg.agent.mcp_servers:
            need_value(f"agent.mcp_servers[{server.name}].url", server.url)
            for header, env_name in server.headers_env.items():
                need_env(f"agent.mcp_servers[{server.name}].headers_env.{header}", env_name)
        if cfg.agent.sandbox.kind == "docker" and not cfg.agent.sandbox.docker_image:
            problems.append("agent.sandbox.docker_image 未填写（sandbox.kind = docker）")

    for label, value in (
        ("server.tls_cert", cfg.server.tls_cert),
        ("server.tls_key", cfg.server.tls_key),
    ):
        if value and not cfg.resolve(value).is_file():
            problems.append(f"{label} 指向的文件不存在：{cfg.resolve(value)}")
    if bool(cfg.server.tls_cert) != bool(cfg.server.tls_key):
        problems.append("server.tls_cert 与 server.tls_key 必须同时填写或同时留空")
    need_env("server.password_env", cfg.server.password_env)
    password = secret(cfg.server.password_env)
    if password and len(password) < MIN_PASSWORD_CHARS:
        problems.append(f"server.password_env 指向的口令太短：至少需要 {MIN_PASSWORD_CHARS} 个字符")

    return problems


def is_loopback(url: str) -> bool:
    """地址的主机是否是本机回环地址（localhost / 127.x / ::1）。"""
    return is_loopback_host(urlparse(url).hostname or "")


def is_loopback_host(host: str) -> bool:
    """监听地址是否只在本机可达（localhost / 127.x / ::1）。"""
    host = host.strip().strip("[]").lower()
    return host in ("localhost", "::1") or host.startswith("127.")


def server_warnings(cfg: AppConfig) -> list[str]:
    """服务端自身的安全提醒，只在命令行打印、不发给浏览器（目前只有「没有访问口令却对外监听」）。"""
    if cfg.server.password_env or is_loopback_host(cfg.server.host):
        return []
    return [
        f"服务监听在 {cfg.server.host}，同一网络里的其他设备都能访问，但没有设置访问口令："
        "任何人都可以查看和删除会议记录。在可信网络之外使用时，请设置 server.password_env"
        "（见 docs/configuration.md）。"
    ]


def check_warnings(cfg: AppConfig) -> list[str]:
    """不妨碍启动、但用户应当知情的事项（目前只有数据外发）。"""
    warnings: list[str] = []
    rt = cfg.realtime_llm
    if not is_loopback(rt.active.base_url):
        host = urlparse(rt.active.base_url).hostname or rt.active.base_url
        what = "会议转录" + ("和屏幕截图" if rt.active.supports_vision else "")
        warnings.append(
            f"实时模型经由 {host} 接入：整场会议的{what}会持续发送到该地址。"
            "如需数据不出本机，请改用本机部署的服务。"
        )
    if cfg.screen.enabled and cfg.screen.caption and cfg.screen.caption_provider == "agent_llm":
        if cfg.agent.base_url and not is_loopback(cfg.agent.base_url):
            host = urlparse(cfg.agent.base_url).hostname or cfg.agent.base_url
            warnings.append(f"画面摘要交给后台模型生成：每张屏幕截图都会发送到 {host}。")
    if cfg.report.provider == "agent_llm":
        if cfg.agent.base_url and not is_loopback(cfg.agent.base_url):
            host = urlparse(cfg.agent.base_url).hostname or cfg.agent.base_url
            warnings.append(
                f"会后报告交给后台模型生成：生成报告时，整场会议的转录会发送到 {host}。"
            )
    return warnings
