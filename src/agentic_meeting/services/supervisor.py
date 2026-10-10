"""本地推理服务的进程管理：按配置生成命令行、拉起子进程、健康检查、收集日志、优雅退出。

分三层，彼此独立，便于单独测试：

* :func:`build_specs` 把配置翻译成 :class:`ServiceSpec`（命令行、健康检查地址、日志路径……），
  除了定位程序之外不碰磁盘；
* :func:`build_probe_targets` 只提取五个 HTTP 探测目标，:func:`probe` 做一次只读探测；
* :class:`Supervisor` 负责子进程的生命周期。

命令行的生成规则见 docs/interfaces.md §9。
"""

from __future__ import annotations

import asyncio
import ctypes
import functools
import os
import signal
import ssl
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO
from urllib.parse import urlparse

import httpx
from loguru import logger

from agentic_meeting.config import (
    AppConfig,
    ASRProfile,
    is_loopback,
    load_asr_profile,
    secret,
)
from agentic_meeting.services.paths import find_executable

# 我们启动的服务一律只监听回环地址，对外只暴露应用端口（architecture.md §2）。
BIND_HOST = "127.0.0.1"
# 单次健康探测的超时。模型加载期间 llama-server 也会很快回 503，不会拖到这个时间。
PROBE_TIMEOUT_SECS = 5.0
# 本机地址的连接超时。本机的连接要么瞬间成功，要么没人监听；但 Windows 对没人监听的回环端口
# 要约 2 秒才报「连接被拒绝」（已实测 2.07 秒），启动阶段每个服务每轮都白等 2 秒。
LOOPBACK_CONNECT_TIMEOUT_SECS = 0.5
# 正常退出的宽限期，超过就强制结束。
STOP_GRACE_SECS = 5.0
# 启动失败时带进错误信息的日志行数。
LOG_TAIL_LINES = 30

# 识别和嵌入每次请求的内容都不同，llama-server 放在内存里的提示缓存（默认上限 8 GB）对它们没有用，
# 只会让进程的内存在头十几分钟里涨到上限（实测识别服务从约 4 GB 涨到约 12 GB）。关掉。
NO_PROMPT_CACHE = ("--cache-ram", "0")

Finder = Callable[[str, str], Path]

# 各服务用哪个程序。集中放在这里，换运行时时只改这一处。
_PROGRAM = {
    "realtime": "llama-server",
    "asr": "llama-server",
    "embedding": "llama-server",
    "tts": "tts-server",
}


# --------------------------------------------------------------------------- #
# 父进程没了，子进程跟着走
# --------------------------------------------------------------------------- #
#
# 正常退出时 Supervisor.stop() 会逐个结束子进程。但父进程被强杀或崩溃时来不及收尾，推理服务会继续占着端口和显存。
# 这里按平台请操作系统代劳：
#   Windows  把子进程放进一个作业对象（Job Object），作业对象设成「句柄一关就结束里面所有进程」；
#            句柄只有父进程持有，父进程一消失系统就会关掉它。
#   Linux    子进程启动时用 prctl(PR_SET_PDEATHSIG) 登记「父进程死了给我发 SIGTERM」。
#   macOS    没有对应的机制，强杀父进程后要自己结束残留的推理进程。

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PR_SET_PDEATHSIG = 1


class _KillOnCloseJob:
    """Windows 的作业对象：这个进程一退出（不管怎么退出的），放进来的进程都会被系统结束。"""

    def __init__(self) -> None:
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("a", "b", "c", "d", "e", "f")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k32 = k32
        self._job = k32.CreateJobObjectW(None, None)
        if not self._job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
            self._job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def add(self, pid: int) -> None:
        handle = self._k32.OpenProcess(_PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self._k32.AssignProcessToJobObject(self._job, handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._k32.CloseHandle(handle)


_job: _KillOnCloseJob | None = None


def tie_to_parent(pid: int) -> bool:
    """让进程 ``pid`` 在本进程消失时被系统结束（Windows）。办不到只记日志，不影响启动。"""
    global _job
    if sys.platform != "win32":
        return False
    try:
        if _job is None:
            _job = _KillOnCloseJob()
        _job.add(pid)
        return True
    except OSError as e:
        logger.warning("没能把进程 {} 绑到本进程上，本进程被强杀时它会留下来：{}", pid, e)
        return False


def _die_with_parent() -> None:  # pragma: no cover - 在子进程里、exec 之前执行
    """Linux：父进程死了就给自己发 SIGTERM。办不到（比如不是 glibc）就算了，不能因此让服务起不来。"""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:  # noqa: S110 - 这里是 fork 之后、exec 之前，不能记日志
        pass


class ServiceStartError(RuntimeError):
    """服务没能启动或没能就绪。信息是写给用户看的中文说明，必要时带日志末尾。"""


# --------------------------------------------------------------------------- #
# 规格
# --------------------------------------------------------------------------- #


@dataclass
class ServiceSpec:
    """一个服务的启动与检查规格。

    ``managed = False`` 的记录只做健康检查：``argv`` 为空，``startup_timeout_secs`` 固定为 0
    （只检查一次、不等待）。密钥不放在这里，``auth_env`` 只是它所在的环境变量名。
    """

    name: str
    argv: list[str]
    # 只存 launch.env 里用户写的覆盖项；启动时才与进程环境合并。
    env: dict[str, str]
    health_url: str
    log_path: Path
    startup_timeout_secs: float
    managed: bool
    auth_env: str = ""
    # 为真时收到任何 HTTP 响应都算连通（不同服务对 /models 的支持不一，401、404 也说明地址是通的）。
    any_status_ok: bool = False
    note: str = ""


def asr_template_path(cfg: AppConfig) -> Path:
    """识别服务的对话模板文件路径（只算路径，不碰磁盘）。"""
    return cfg.resolve(cfg.session.data_dir) / "run" / "asr_chat_template.jinja"


def write_asr_template(cfg: AppConfig, profile: ASRProfile) -> Path:
    """把识别格式档案里的 ``chat_template`` 逐字写成文件，返回路径。

    当前锁定的 llama-server 只认启动时指定的模板、不读请求体里的模板（interfaces.md §3.3）。
    必须按字节原样写出：用文本模式会在 Windows 上把换行变成 ``\\r\\n``，模板就变了。
    """
    path = asr_template_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(profile.chat_template.encode("utf-8"))
    return path


def _origin(base_url: str) -> str:
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _bind_port(base_url: str, field_name: str) -> int:
    """受管服务要绑定的端口，取自 ``base_url``；同时核对它指向本机。"""
    if not is_loopback(base_url):
        raise ValueError(
            f"{field_name} = {base_url!r} 不是本机地址，但这个服务由本项目启动、只监听 {BIND_HOST}。"
            "请改成本机地址，或设 launch.enabled = false 改为连接你自己启动的服务。"
        )
    port = urlparse(base_url).port
    if port is None:
        raise ValueError(f"{field_name} = {base_url!r} 没有写端口，无法确定要让服务监听哪个端口。")
    return port


def build_specs(cfg: AppConfig, *, find: Finder = find_executable) -> list[ServiceSpec]:
    """按配置生成各服务的规格，顺序固定为 realtime、asr、tts、embedding。

    ``find`` 用来定位程序，默认是 :func:`find_executable`；只在服务确实要由我们启动时才调用。
    被整体关闭的服务不生成；``launch.enabled = false`` 的生成一条只检查不启动的记录。
    """
    log_dir = cfg.resolve(cfg.session.data_dir) / "logs"

    def path(value: str) -> str:
        return str(cfg.resolve(value))

    def local(
        name: str,
        field_name: str,
        base_url: str,
        launch,
        make_argv: Callable[[str, int], list[str]],
        *,
        unmanaged_note: str = "",
    ) -> ServiceSpec:
        """llama-server / tts-server 一类、健康检查在 ``/health`` 的服务。"""
        if not launch.enabled:
            return ServiceSpec(
                name=name,
                argv=[],
                env={},
                health_url=f"{_origin(base_url)}/health",
                log_path=log_dir / f"{name}.log",
                startup_timeout_secs=0.0,
                managed=False,
                note=unmanaged_note,
            )
        port = _bind_port(base_url, field_name)
        exe = str(find(_PROGRAM[name], launch.executable))
        return ServiceSpec(
            name=name,
            argv=make_argv(exe, port),
            env=dict(launch.env),
            health_url=f"http://{BIND_HOST}:{port}/health",
            log_path=log_dir / f"{name}.log",
            startup_timeout_secs=launch.startup_timeout_secs,
            managed=True,
        )

    specs: list[ServiceSpec] = []

    # ---- 实时模型 ----
    rt = cfg.realtime_llm
    if rt.mode == "llama_server":
        assert rt.llama_server is not None  # 由配置校验保证
        ls = rt.llama_server
        launch = ls.launch

        def realtime_argv(exe: str, port: int) -> list[str]:
            argv = [exe, "-m", path(launch.model_path)]
            if ls.supports_vision and launch.mmproj_path:
                argv += ["--mmproj", path(launch.mmproj_path)]
            argv += [
                "-a", ls.model,
                "--host", BIND_HOST, "--port", str(port),
                "-c", str(launch.ctx_size), "-np", str(launch.parallel), "-ngl", launch.gpu_layers,
                "--jinja", "--reasoning", "on" if ls.thinking else "off",
                *launch.extra_args,
            ]  # fmt: skip
            return argv

        specs.append(
            local(
                "realtime", "realtime_llm.llama_server.base_url", ls.base_url, launch, realtime_argv
            )
        )
    else:
        api = rt.active
        specs.append(
            ServiceSpec(
                name="realtime",
                argv=[],
                env={},
                health_url=api.base_url.rstrip("/") + "/models",
                log_path=log_dir / "realtime.log",
                startup_timeout_secs=0.0,
                managed=False,
                auth_env=api.api_key_env,
                any_status_ok=True,
                note="外部接口，不由本项目启动",
            )
        )

    # ---- 语音识别 ----
    asr = cfg.asr
    template = asr_template_path(cfg)

    def asr_argv(exe: str, port: int) -> list[str]:
        return [
            exe, "-m", path(asr.launch.model_path), "--mmproj", path(asr.launch.mmproj_path),
            "--host", BIND_HOST, "--port", str(port),
            "-c", str(asr.launch.ctx_size), "-np", "1", "-ngl", asr.launch.gpu_layers,
            "--jinja", "--chat-template-file", str(template),
            *NO_PROMPT_CACHE,
            *asr.launch.extra_args,
        ]  # fmt: skip

    specs.append(
        local(
            "asr",
            "asr.base_url",
            asr.base_url,
            asr.launch,
            asr_argv,
            unmanaged_note=(
                "识别服务由你自行启动：启动命令必须带上 "
                f"--chat-template-file {template}（内容取自识别格式档案，services up 会生成该文件）"
            ),
        )
    )

    # ---- 语音合成 ----
    if cfg.tts.enabled:
        tts = cfg.tts

        def tts_argv(exe: str, port: int) -> list[str]:
            return [
                exe, "--model", path(tts.launch.model_path), "--codec", path(tts.launch.codec_path),
                "--alias", tts.model, "--host", BIND_HOST, "--port", str(port),
                "--lang", tts.launch.default_language,
                *tts.launch.extra_args,
            ]  # fmt: skip

        specs.append(local("tts", "tts.base_url", tts.base_url, tts.launch, tts_argv))

    # ---- 嵌入 ----
    if cfg.embedding.enabled:
        emb = cfg.embedding

        def embedding_argv(exe: str, port: int) -> list[str]:
            return [
                exe, "-m", path(emb.launch.model_path), "-a", emb.model, "--embedding",
                "--host", BIND_HOST, "--port", str(port), "-ngl", emb.launch.gpu_layers,
                *NO_PROMPT_CACHE,
                *emb.launch.extra_args,
            ]  # fmt: skip

        specs.append(
            local("embedding", "embedding.base_url", emb.base_url, emb.launch, embedding_argv)
        )

    return specs


# --------------------------------------------------------------------------- #
# 健康探测
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProbeTarget:
    """只读探测目标；空地址表示启用但配置无效，禁用项不访问网络。不能直接公开序列化。"""

    name: str
    enabled: bool
    required: bool = False
    health_url: str = ""
    auth_env: str = ""
    any_status_ok: bool = False


def _valid_probe_url(url: str) -> bool:
    try:
        parsed = httpx.URL(url)
        return (
            not any(char.isspace() for char in url)
            and parsed.scheme in {"http", "https"}
            and bool(parsed.host)
            and (parsed.port is None or 0 < parsed.port < 65536)
            and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
        )
    except httpx.InvalidURL:
        return False


def build_probe_targets(cfg: AppConfig) -> list[ProbeTarget]:
    """按 §5.8 构建五个目标，不定位程序、不读取模型或模板、不生成启动规格。"""
    from agentic_meeting.pipeline.services import caption_provider

    def target(
        name: str,
        enabled: bool,
        base_url: str,
        *,
        auth_env: str = "",
        generic: bool = False,
        configured: bool = True,
    ) -> ProbeTarget:
        url = ""
        if enabled and configured and _valid_probe_url(base_url):
            url = base_url.rstrip("/") + "/models" if generic else _origin(base_url) + "/health"
        return ProbeTarget(name, enabled, name == "asr", url, auth_env, generic)

    rt = cfg.realtime_llm.active
    agent_enabled = (
        cfg.agent.enabled
        or caption_provider(cfg) == "agent_llm"
        or cfg.realtime.digest_provider == "agent_llm"
        or cfg.report.provider == "agent_llm"
    )
    return [
        target("asr", True, cfg.asr.base_url),
        target(
            "realtime", True, rt.base_url,
            auth_env=rt.api_key_env, generic=not cfg.realtime_llm.has_health_endpoint,
        ),
        target("tts", cfg.tts.enabled, cfg.tts.base_url, auth_env=cfg.tts.api_key_env),
        target("embedding", cfg.embedding.enabled, cfg.embedding.base_url),
        target(
            "agent", agent_enabled, cfg.agent.base_url,
            auth_env=cfg.agent.api_key_env, generic=True, configured=bool(cfg.agent.model.strip()),
        ),
    ]  # fmt: skip


@dataclass
class ProbeResult:
    ok: bool
    detail: str
    status_code: int | None = None
    # detail 留给 CLI；HTTP 层只使用白名单原因码。
    reason: str = ""


@functools.cache
def _ssl_context(trust_env: bool) -> ssl.SSLContext:
    # 默认每个 client 都会重新加载整份 CA 证书（本机实测每次约 30 毫秒），而且是在事件循环里同步做的。
    # /readyz 刷新一轮要建五个，事件循环因此卡住；慢机器上超过 0.5 秒，同时进行的存储检查就会超时，
    # 就绪检查短暂报 not_ready。证书在进程内只加载一次，与 httpx 自己建立时的参数相同。
    return httpx.create_ssl_context(trust_env=trust_env)


def _new_client(url: str) -> httpx.AsyncClient:
    # 本机地址不走系统代理：设了 HTTP_PROXY 的机器上，经代理访问 127.0.0.1 会误判成不通。
    # 远端地址照系统设置走代理，与应用之后实际访问它的方式一致。
    trust_env = not is_loopback(url)
    return httpx.AsyncClient(
        timeout=_timeout_for(url), trust_env=trust_env, verify=_ssl_context(trust_env)
    )


def _timeout_for(url: str, timeout_secs: float = PROBE_TIMEOUT_SECS) -> httpx.Timeout:
    connect = min(LOOPBACK_CONNECT_TIMEOUT_SECS, timeout_secs) if is_loopback(url) else timeout_secs
    return httpx.Timeout(timeout_secs, connect=connect)


async def probe(
    spec: ServiceSpec | ProbeTarget,
    client: httpx.AsyncClient | None = None,
    *,
    timeout_secs: float = PROBE_TIMEOUT_SECS,
) -> ProbeResult:
    """对 ``spec.health_url`` 发一次 GET。连接失败、超时返回 ``ok=False`` 并写明原因，不抛异常。"""
    if not _valid_probe_url(spec.health_url):
        return ProbeResult(False, "探测地址配置无效", reason="invalid_config")
    headers: dict[str, str] = {}
    key = secret(spec.auth_env)
    if key:
        headers["Authorization"] = f"Bearer {key}"

    owned = client is None
    if client is None:
        client = _new_client(spec.health_url)
    try:
        async with asyncio.timeout(timeout_secs):
            async with client.stream(
                "GET",
                spec.health_url,
                headers=headers,
                timeout=_timeout_for(spec.health_url, timeout_secs),
                follow_redirects=False,
            ) as response:
                code = response.status_code
    except httpx.ConnectTimeout:
        return ProbeResult(False, "无法连接：端口没有响应", reason="timeout")
    except (httpx.TimeoutException, TimeoutError):
        return ProbeResult(False, f"超时（{timeout_secs:g} 秒内没有响应）", reason="timeout")
    except httpx.HTTPError as e:
        # 仅供 CLI 使用，公开接口绝不能序列化 detail 或异常信息。
        return ProbeResult(
            False, f"无法连接：{e}" if str(e) else "无法连接", reason="connection_failed"
        )
    finally:
        if owned:
            await client.aclose()

    if code == 200:
        return ProbeResult(
            True, "HTTP 200", code, "http_response" if spec.any_status_ok else "healthy"
        )
    if spec.any_status_ok:
        return ProbeResult(True, f"HTTP {code}（有响应，地址是通的）", code, "http_response")
    hint = "（服务还在加载）" if code == 503 else ""
    return ProbeResult(False, f"HTTP {code}{hint}", code, "http_error")


async def probe_service(
    target: ProbeTarget,
    client: httpx.AsyncClient | None = None,
    *,
    timeout_secs: float = PROBE_TIMEOUT_SECS,
) -> dict[str, bool | str]:
    """返回契约的四个安全字段；只关闭自建 client，取消继续传播给调用者。"""
    if not target.enabled:
        status, reason = "disabled", "disabled"
    else:
        result = await probe(target, client, timeout_secs=timeout_secs)
        status = ("reachable" if target.any_status_ok else "ok") if result.ok else "unavailable"
        reason = result.reason
    return {
        "enabled": target.enabled,
        "required": target.required,
        "status": status,
        "reason": reason,
    }


async def check_tts_voice(cfg: AppConfig, client: httpx.AsyncClient | None = None) -> None:
    """语音合成就绪后核对 ``tts.voice`` 是否真的存在（interfaces.md §9 末尾）。

    音色不存在时服务只会在每次合成时回 502，配置错了很难看出来，所以启动时就查。
    只有「确认音色不在列表里」才抛异常；服务没有这个接口或查询失败时只记警告。
    """
    url = cfg.tts.base_url.rstrip("/") + "/audio/voices"
    headers: dict[str, str] = {}
    key = secret(cfg.tts.api_key_env)
    if key:
        headers["Authorization"] = f"Bearer {key}"

    owned = client is None
    if client is None:
        client = _new_client(url)
    try:
        try:
            response = await client.get(url, headers=headers, timeout=_timeout_for(url))
        except httpx.HTTPError as e:
            logger.warning("无法查询语音合成的音色列表（{}），跳过音色核对", type(e).__name__)
            return
    finally:
        if owned:
            await client.aclose()

    try:
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}")
        names = [str(item["name"]) for item in response.json()["voices"]]
    except (ValueError, KeyError, TypeError) as e:
        logger.warning("语音合成服务没有给出可用的音色列表（{}），跳过音色核对", e)
        return

    if cfg.tts.voice not in names:
        available = "、".join(names) if names else "（列表为空）"
        raise ServiceStartError(
            f"[tts] voice = {cfg.tts.voice!r} 不在语音合成服务提供的音色里。可用音色：{available}。"
            "请修改 config/config.toml 里 [tts] 的 voice。"
        )


# --------------------------------------------------------------------------- #
# 进程管理
# --------------------------------------------------------------------------- #


@dataclass
class _Child:
    """一个服务在 Supervisor 里的运行记录。``reused`` 表示它早已在运行、不是我们启动的。"""

    spec: ServiceSpec
    proc: asyncio.subprocess.Process | None = None
    log_file: IO[bytes] | None = None
    log_offset: int = 0
    reused: bool = False


class Supervisor:
    """管理一组服务的生命周期。

    ``cfg`` 只用来在启动前写出识别服务要用的模板文件（有名为 ``asr`` 的记录时），不给就不写。
    ``client`` 用于测试时注入带 ``MockTransport`` 的客户端，平时留空，由本类自己按需创建。
    """

    def __init__(
        self,
        specs: list[ServiceSpec],
        *,
        cfg: AppConfig | None = None,
        client: httpx.AsyncClient | None = None,
        poll_interval_secs: float = 0.5,
        stop_grace_secs: float = STOP_GRACE_SECS,
    ) -> None:
        names = [spec.name for spec in specs]
        if len(set(names)) != len(names):
            raise ValueError(f"服务名重复：{names}")
        self.specs = specs
        self.cfg = cfg
        self.client = client
        self.poll_interval_secs = poll_interval_secs
        self.stop_grace_secs = stop_grace_secs
        self._children: dict[str, _Child] = {}
        # 本类自己创建的客户端，按「是否读取系统代理设置」各一个，stop() 时关闭。
        self._owned_clients: dict[bool, httpx.AsyncClient] = {}

    # ---- 内部 ----

    def _client_for(self, spec: ServiceSpec) -> httpx.AsyncClient:
        if self.client is not None:
            return self.client
        trust_env = not is_loopback(spec.health_url)
        if trust_env not in self._owned_clients:
            self._owned_clients[trust_env] = _new_client(spec.health_url)
        return self._owned_clients[trust_env]

    def _prepare_runtime_files(self) -> None:
        if self.cfg is not None and any(spec.name == "asr" for spec in self.specs):
            write_asr_template(self.cfg, load_asr_profile(self.cfg))

    async def _launch(self, spec: ServiceSpec) -> _Child:
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)
        offset = spec.log_path.stat().st_size if spec.log_path.exists() else 0
        log_file = spec.log_path.open("ab")
        header = f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 启动 {spec.name}：{' '.join(spec.argv)} =====\n"
        log_file.write(header.encode("utf-8"))
        log_file.flush()

        extra = {}
        if sys.platform == "win32":
            # 让终端里的 Ctrl+C 不直接打到子进程，收尾统一经过 stop()。
            extra["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        elif sys.platform.startswith("linux"):
            extra["preexec_fn"] = _die_with_parent
        try:
            proc = await asyncio.create_subprocess_exec(
                *spec.argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, **spec.env},
                **extra,
            )
        except OSError as e:
            log_file.close()
            raise ServiceStartError(
                f"无法启动 {spec.name}：{e}（命令：{' '.join(spec.argv)}）"
            ) from e
        tie_to_parent(proc.pid)
        logger.info("已启动 {}（pid {}），日志 {}", spec.name, proc.pid, spec.log_path)
        return _Child(spec, proc=proc, log_file=log_file, log_offset=offset)

    @staticmethod
    def _log_tail(child: _Child) -> str:
        """本次运行写入日志的最后若干行（不含以前运行留下的内容）。"""
        try:
            with child.spec.log_path.open("rb") as f:
                f.seek(child.log_offset)
                text = f.read().decode("utf-8", errors="replace")
        except OSError:
            return "（读不到日志）"
        lines = text.splitlines()[-LOG_TAIL_LINES:]
        return "\n".join(lines) or "（日志是空的）"

    def _failure(self, child: _Child, reason: str) -> ServiceStartError:
        return ServiceStartError(
            f"{child.spec.name} {reason}。日志 {child.spec.log_path} 的最后 {LOG_TAIL_LINES} 行：\n"
            f"{self._log_tail(child)}"
        )

    async def _wait_one(self, child: _Child) -> None:
        spec, proc = child.spec, child.proc
        assert proc is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + spec.startup_timeout_secs
        while True:
            if proc.returncode is not None:
                raise self._failure(child, f"启动后进程退出了（退出码 {proc.returncode}）")
            result = await probe(spec, self._client_for(spec))
            if result.ok:
                if proc.returncode is not None:  # 端口是别的进程应的
                    raise self._failure(child, f"进程已退出（退出码 {proc.returncode}）")
                logger.info("{} 已就绪", spec.name)
                return
            if loop.time() >= deadline:
                raise self._failure(
                    child,
                    f"在 {spec.startup_timeout_secs:g} 秒内未就绪（最后一次检查：{result.detail}）",
                )
            # 连接被拒绝（还在启动）和 503（还在加载模型）都继续等；进程退出由上面的检查立即发现。
            await asyncio.sleep(self.poll_interval_secs)

    def _terminate(self, child: _Child) -> None:
        if child.proc is not None and child.proc.returncode is None:
            try:
                child.proc.terminate()
            except ProcessLookupError:
                pass

    async def _reap(self, child: _Child) -> None:
        proc = child.proc
        assert proc is not None
        try:
            await asyncio.wait_for(proc.wait(), self.stop_grace_secs)
        except TimeoutError:
            logger.warning(
                "{} 在 {:g} 秒内没有退出，强制结束", child.spec.name, self.stop_grace_secs
            )
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

    # ---- 公共接口 ----

    async def start(self) -> None:
        """启动所有受管服务。要么全部启动，要么一个不留：中途失败会先停掉已启动的。"""
        try:
            self._prepare_runtime_files()
            todo = [s for s in self.specs if s.managed and s.name not in self._children]
            # 并发探测：没人监听的端口在 Windows 上也要等一会儿才有结果，逐个探会累加。
            existing = await asyncio.gather(*(probe(s, self._client_for(s)) for s in todo))
            for spec, found in zip(todo, existing, strict=True):
                if found.ok:
                    logger.warning(
                        "{} 已经在运行（{}），不重复启动，退出时也不会停止它",
                        spec.name,
                        spec.health_url,
                    )
                    self._children[spec.name] = _Child(spec, reused=True)
                else:
                    self._children[spec.name] = await self._launch(spec)
        except BaseException:
            await self.stop()
            raise

    async def wait_healthy(self) -> None:
        """等本次启动的服务全部就绪。不受管的服务不在此列（不等待、不因不通而抛异常）。"""
        tasks = [
            asyncio.create_task(self._wait_one(child))
            for child in self._children.values()
            if child.proc is not None
        ]
        try:
            for finished in asyncio.as_completed(tasks):
                await finished  # 第一个失败的直接抛出
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def probe_all(self) -> dict[str, ProbeResult]:
        """探测全部服务（含不受管的），用来打印状态表。"""
        results = await asyncio.gather(
            *(probe(spec, self._client_for(spec)) for spec in self.specs)
        )
        return {spec.name: result for spec, result in zip(self.specs, results, strict=True)}

    async def stop(self) -> None:
        """先终止，宽限期过后强杀；复用的服务不动。可重复调用。"""
        children = list(self._children.values())
        self._children.clear()
        owned = [child for child in reversed(children) if child.proc is not None]
        for child in owned:
            self._terminate(child)
        await asyncio.gather(*(self._reap(child) for child in owned))
        for child in children:
            if child.log_file is not None:
                child.log_file.close()
        clients, self._owned_clients = list(self._owned_clients.values()), {}
        for client in clients:
            await client.aclose()

    def status(self) -> list[dict]:
        """各服务的进程状态（不做网络请求）。

        ``state``：``running`` / ``exited`` / ``reused``（复用已在运行的）/
        ``external``（不受管）/ ``not_started``。
        """
        rows: list[dict] = []
        for spec in self.specs:
            child = self._children.get(spec.name)
            proc = child.proc if child else None
            if not spec.managed:
                state = "external"
            elif child is None:
                state = "not_started"
            elif child.reused:
                state = "reused"
            else:
                assert proc is not None
                state = "running" if proc.returncode is None else "exited"
            rows.append(
                {
                    "name": spec.name,
                    "managed": spec.managed,
                    "state": state,
                    "pid": proc.pid if proc is not None and proc.returncode is None else None,
                    "returncode": proc.returncode if proc is not None else None,
                    "health_url": spec.health_url,
                    "log_path": str(spec.log_path),
                }
            )
        return rows
