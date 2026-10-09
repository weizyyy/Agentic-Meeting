"""services/supervisor.py：命令行生成、健康探测、进程生命周期。

生命周期测试用 ``sys.executable`` 跑一个很小的假 HTTP 服务，真实地启动、探测、结束子进程，
不依赖任何推理程序或权重。
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from agentic_meeting.config import AppConfig, load_asr_profile
from agentic_meeting.services.supervisor import (
    ProbeResult,
    ServiceSpec,
    ServiceStartError,
    Supervisor,
    asr_template_path,
    build_specs,
    check_tts_voice,
    probe,
    write_asr_template,
)

CfgFactory = Callable[[], AppConfig]


def fake_find(name: str, override: str = "") -> Path:
    return Path(override) if override else Path("fake-bin") / name


def flag_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def by_name(specs: list[ServiceSpec]) -> dict[str, ServiceSpec]:
    return {spec.name: spec for spec in specs}


# --------------------------------------------------------------------------- #
# build_specs
# --------------------------------------------------------------------------- #


def test_default_config_yields_four_services_in_order(make_cfg: CfgFactory):
    specs = build_specs(make_cfg(), find=fake_find)
    assert [s.name for s in specs] == ["realtime", "asr", "tts", "embedding"]
    assert all(s.managed for s in specs)


def test_realtime_llama_server_command_line(make_cfg: CfgFactory):
    cfg = make_cfg()
    ls = cfg.realtime_llm.llama_server
    ls.launch.env = {"CUDA_VISIBLE_DEVICES": "0"}
    ls.launch.extra_args = ["--device", "CUDA0", "--flash-attn", "on"]
    spec = by_name(build_specs(cfg, find=fake_find))["realtime"]

    argv = spec.argv
    assert argv[0] == str(fake_find("llama-server"))
    assert flag_value(argv, "-m") == str(cfg.resolve("models/fake-llm.gguf"))
    assert flag_value(argv, "--mmproj") == str(cfg.resolve("models/fake-llm-mmproj.gguf"))
    assert flag_value(argv, "-a") == "realtime"
    assert flag_value(argv, "--host") == "127.0.0.1"
    assert flag_value(argv, "--port") == "8080"
    assert flag_value(argv, "-c") == "32768"
    assert flag_value(argv, "-np") == "2"
    assert flag_value(argv, "-ngl") == "all"
    assert "--jinja" in argv
    assert flag_value(argv, "--reasoning") == "off"
    assert argv[-4:] == ["--device", "CUDA0", "--flash-attn", "on"]  # extra_args 排在最后
    assert spec.env == {"CUDA_VISIBLE_DEVICES": "0"}
    assert spec.health_url == "http://127.0.0.1:8080/health"
    assert spec.log_path == cfg.resolve(cfg.session.data_dir) / "logs" / "realtime.log"
    assert spec.startup_timeout_secs == ls.launch.startup_timeout_secs
    assert spec.managed and not spec.any_status_ok and not spec.auth_env


def test_realtime_thinking_flag_and_mmproj_follow_config(make_cfg: CfgFactory):
    cfg = make_cfg()
    cfg.realtime_llm.llama_server.thinking = True
    assert (
        flag_value(by_name(build_specs(cfg, find=fake_find))["realtime"].argv, "--reasoning")
        == "on"
    )

    cfg = make_cfg()
    cfg.realtime_llm.llama_server.supports_vision = False  # 不识图就不必加载投影文件
    assert "--mmproj" not in by_name(build_specs(cfg, find=fake_find))["realtime"].argv


def test_asr_command_line_uses_template_file(make_cfg: CfgFactory):
    cfg = make_cfg()
    cfg.asr.launch.extra_args = ["--device", "CUDA1"]
    spec = by_name(build_specs(cfg, find=fake_find))["asr"]

    argv = spec.argv
    assert flag_value(argv, "-m") == str(cfg.resolve("models/fake-asr.gguf"))
    assert flag_value(argv, "--mmproj") == str(cfg.resolve("models/fake-asr-mmproj.gguf"))
    assert flag_value(argv, "--port") == "8081"
    assert flag_value(argv, "-c") == "8192"
    assert flag_value(argv, "-np") == "1"
    assert "--jinja" in argv
    assert flag_value(argv, "--chat-template-file") == str(asr_template_path(cfg))
    assert flag_value(argv, "--cache-ram") == "0"  # 识别用不上提示缓存，不关会多占 8 GB 内存
    assert argv[-2:] == ["--device", "CUDA1"]
    assert spec.health_url == "http://127.0.0.1:8081/health"


def test_embedding_command_line(make_cfg: CfgFactory):
    cfg = make_cfg()
    spec = by_name(build_specs(cfg, find=fake_find))["embedding"]
    argv = spec.argv
    assert flag_value(argv, "-m") == str(cfg.resolve("models/fake-embedding.gguf"))
    assert flag_value(argv, "-a") == "embedding"
    assert "--embedding" in argv
    assert flag_value(argv, "--cache-ram") == "0"
    assert flag_value(argv, "--port") == "8083"
    assert flag_value(argv, "-ngl") == "0"
    assert argv[-4:] == ["--pooling", "last", "--device", "none"]  # 模板里的 extra_args
    assert "-np" not in argv and "-c" not in argv


def test_tts_command_line(make_cfg: CfgFactory):
    cfg = make_cfg()
    cfg.tts.launch.extra_args = ["--max-batch", "2"]
    spec = by_name(build_specs(cfg, find=fake_find))["tts"]
    argv = spec.argv
    assert argv[0] == str(fake_find("tts-server"))
    assert flag_value(argv, "--model") == str(cfg.resolve("models/fake-tts.gguf"))
    assert flag_value(argv, "--codec") == str(cfg.resolve("models/fake-codec.gguf"))
    assert flag_value(argv, "--alias") == "tts"
    assert flag_value(argv, "--host") == "127.0.0.1"
    assert flag_value(argv, "--port") == "8082"  # base_url 末尾带 /v1，端口照样解析得出
    assert flag_value(argv, "--lang") == "Chinese"
    assert argv[-2:] == ["--max-batch", "2"]
    assert spec.health_url == "http://127.0.0.1:8082/health"


def test_ports_come_from_base_urls(make_cfg: CfgFactory):
    cfg = make_cfg()
    cfg.asr.base_url = "http://localhost:9101"
    cfg.embedding.base_url = "http://127.0.0.1:9102/v1"
    specs = by_name(build_specs(cfg, find=fake_find))
    assert flag_value(specs["asr"].argv, "--port") == "9101"
    assert specs["asr"].health_url == "http://127.0.0.1:9101/health"  # 我们绑的是 127.0.0.1
    assert flag_value(specs["embedding"].argv, "--port") == "9102"


def test_executable_override_is_passed_to_the_finder(make_cfg: CfgFactory):
    cfg = make_cfg()
    cfg.tts.launch.executable = "bin/custom-tts"
    calls: list[tuple[str, str]] = []

    def recording_find(name: str, override: str = "") -> Path:
        calls.append((name, override))
        return fake_find(name, override)

    build_specs(cfg, find=recording_find)
    assert ("tts-server", "bin/custom-tts") in calls
    assert ("llama-server", "") in calls


def test_disabled_services_are_not_generated(make_cfg: CfgFactory):
    cfg = make_cfg()
    cfg.tts.enabled = False
    cfg.embedding.enabled = False
    assert [s.name for s in build_specs(cfg, find=fake_find)] == ["realtime", "asr"]


def test_launch_disabled_gives_check_only_record(make_cfg: CfgFactory):
    cfg = make_cfg()
    cfg.asr.launch.enabled = False
    cfg.asr.base_url = "http://192.168.1.20:8081"  # 不受管的服务可以在别的机器上
    cfg.tts.launch.enabled = False

    def must_not_locate(name: str, override: str = "") -> Path:
        raise AssertionError(f"不受管的服务不应去定位程序：{name}")

    cfg.realtime_llm.llama_server.launch.enabled = False
    cfg.embedding.enabled = False
    specs = by_name(build_specs(cfg, find=must_not_locate))

    asr = specs["asr"]
    assert not asr.managed
    assert asr.argv == []
    assert asr.health_url == "http://192.168.1.20:8081/health"
    assert asr.startup_timeout_secs == 0  # 不等待，只检查一次
    # 提醒用户：自己启动识别服务时必须带上同样的模板文件（interfaces.md §9）。
    assert "--chat-template-file" in asr.note
    assert str(asr_template_path(cfg)) in asr.note

    assert not specs["tts"].managed and specs["tts"].health_url == "http://127.0.0.1:8082/health"
    assert not specs["realtime"].managed
    assert specs["realtime"].health_url == "http://127.0.0.1:8080/health"
    assert specs["realtime"].argv == []


def test_openai_api_realtime_is_a_connectivity_check_only(make_cfg: CfgFactory):
    cfg = make_cfg()
    cfg.realtime_llm.mode = "openai_api"
    cfg.realtime_llm.openai_api.base_url = "https://llm.example.com/v1/"
    cfg.realtime_llm.openai_api.model = "fake-model"
    cfg.realtime_llm.openai_api.api_key_env = "FAKE_REALTIME_KEY"
    # 另一节里的启动设置不应再起作用。
    cfg.realtime_llm.llama_server.launch.enabled = True

    realtime = by_name(build_specs(cfg, find=fake_find))["realtime"]
    assert not realtime.managed
    assert realtime.argv == []
    assert realtime.health_url == "https://llm.example.com/v1/models"
    assert realtime.any_status_ok  # 401、404 也说明地址是通的
    assert realtime.auth_env == "FAKE_REALTIME_KEY"  # 只记变量名，不记密钥值
    assert realtime.startup_timeout_secs == 0
    assert "外部接口" in realtime.note


def test_managed_service_must_listen_on_loopback_with_explicit_port(make_cfg: CfgFactory):
    cfg = make_cfg()
    cfg.asr.base_url = "http://192.168.1.20:8081"
    with pytest.raises(ValueError, match=r"asr\.base_url.*launch\.enabled = false"):
        build_specs(cfg, find=fake_find)

    cfg = make_cfg()
    cfg.tts.base_url = "http://127.0.0.1/v1"  # 没写端口，我们无从知道该绑哪个
    with pytest.raises(ValueError, match=r"tts\.base_url.*端口"):
        build_specs(cfg, find=fake_find)

    cfg = make_cfg()
    cfg.realtime_llm.llama_server.base_url = "http://10.0.0.5:8080/v1"
    with pytest.raises(ValueError, match="realtime_llm"):
        build_specs(cfg, find=fake_find)


# --------------------------------------------------------------------------- #
# 识别模板文件
# --------------------------------------------------------------------------- #


def test_write_asr_template_is_verbatim(make_cfg: CfgFactory):
    cfg = make_cfg()
    profile = load_asr_profile(cfg)
    path = write_asr_template(cfg, profile)

    assert path == cfg.resolve(cfg.session.data_dir) / "run" / "asr_chat_template.jinja"
    assert path == asr_template_path(cfg)
    # 按字节比较：Windows 上用文本模式写会把 \n 变成 \r\n，模板就不再逐字相同。
    assert path.read_bytes() == profile.chat_template.encode("utf-8")
    # 重复写入覆盖旧内容。
    path.write_text("stale", encoding="utf-8")
    assert write_asr_template(cfg, profile) == path
    assert path.read_bytes() == profile.chat_template.encode("utf-8")


def test_asr_template_path_does_not_touch_the_disk(make_cfg: CfgFactory):
    cfg = make_cfg()
    path = asr_template_path(cfg)
    assert not path.exists() and not path.parent.exists()


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #


def check_spec(url: str = "http://svc.test/health", **kw) -> ServiceSpec:
    defaults = dict(
        name="svc",
        argv=[],
        env={},
        health_url=url,
        log_path=Path("svc.log"),
        startup_timeout_secs=0.0,
        managed=False,
    )
    return ServiceSpec(**{**defaults, **kw})


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("status", "any_ok", "expected"),
    [
        (200, False, True),
        (503, False, False),  # llama-server 加载模型期间返回 503
        (401, False, False),
        (404, False, False),
        (200, True, True),
        (401, True, True),
        (404, True, True),
        (500, True, True),  # 有响应就说明地址是通的
    ],
)
async def test_probe_status_rules(status: int, any_ok: bool, expected: bool):
    async with mock_client(lambda request: httpx.Response(status)) as client:
        result = await probe(check_spec(any_status_ok=any_ok), client)
    assert isinstance(result, ProbeResult)
    assert result.ok is expected
    assert result.status_code == status
    assert str(status) in result.detail


async def test_probe_reports_connection_failure_without_raising():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("All connection attempts failed")

    async with mock_client(refuse) as client:
        result = await probe(check_spec(any_status_ok=True), client)
    assert not result.ok  # 连不上不能因为 any_status_ok 而算通
    assert result.status_code is None
    assert "无法连接" in result.detail


async def test_probe_reports_timeout():
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    async with mock_client(slow) as client:
        result = await probe(check_spec(), client)
    assert not result.ok and "超时" in result.detail


async def test_probe_sends_key_from_env_only_in_the_header(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FAKE_PROBE_KEY", "sk-test-secret")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401)

    async with mock_client(handler) as client:
        result = await probe(check_spec(auth_env="FAKE_PROBE_KEY", any_status_ok=True), client)
    assert seen[0].headers["Authorization"] == "Bearer sk-test-secret"
    assert "sk-test-secret" not in result.detail
    assert "sk-test-secret" not in repr(check_spec(auth_env="FAKE_PROBE_KEY"))


async def test_probe_without_key_sends_no_authorization(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("FAKE_PROBE_KEY", raising=False)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    async with mock_client(handler) as client:
        await probe(check_spec(auth_env="FAKE_PROBE_KEY"), client)
        await probe(check_spec(), client)
    assert all("Authorization" not in r.headers for r in seen)


# --------------------------------------------------------------------------- #
# check_tts_voice
# --------------------------------------------------------------------------- #


def voices_response(*names: str) -> httpx.Response:
    return httpx.Response(200, json={"voices": [{"name": n, "kind": "speaker"} for n in names]})


async def test_tts_voice_present_passes(make_cfg: CfgFactory):
    cfg = make_cfg()
    requested: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request)
        return voices_response("alice", "fake-voice")

    async with mock_client(handler) as client:
        await check_tts_voice(cfg, client)
    assert requested[0].url.path == "/v1/audio/voices"


async def test_tts_voice_missing_lists_the_available_ones(make_cfg: CfgFactory):
    cfg = make_cfg()
    async with mock_client(lambda r: voices_response("alice", "bob")) as client:
        with pytest.raises(ServiceStartError) as exc:
            await check_tts_voice(cfg, client)
    message = str(exc.value)
    assert "fake-voice" in message and "alice" in message and "bob" in message
    assert "tts" in message


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"data": []}),
        httpx.Response(200, json={"voices": "oops"}),
    ],
)
async def test_tts_voice_unverifiable_is_only_a_warning(make_cfg: CfgFactory, response):
    """别家的 OpenAI 兼容服务可能没有这个接口：无法核对不等于配置有错。"""
    async with mock_client(lambda r: response) as client:
        await check_tts_voice(make_cfg(), client)


async def test_tts_voice_request_failure_is_only_a_warning(make_cfg: CfgFactory):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with mock_client(refuse) as client:
        await check_tts_voice(make_cfg(), client)


async def test_tts_voice_check_sends_configured_key(make_cfg: CfgFactory, monkeypatch):
    monkeypatch.setenv("FAKE_TTS_KEY", "sk-tts")
    cfg = make_cfg()
    cfg.tts.api_key_env = "FAKE_TTS_KEY"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return voices_response("fake-voice")

    async with mock_client(handler) as client:
        await check_tts_voice(cfg, client)
    assert seen[0].headers["Authorization"] == "Bearer sk-tts"


# --------------------------------------------------------------------------- #
# 生命周期：真实子进程
# --------------------------------------------------------------------------- #

FAKE_SERVER = """
import http.server, os, sys, time

port = int(sys.argv[1])
warmup = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
print("FAKE_FLAG=" + os.environ.get("FAKE_FLAG", "<unset>"), flush=True)
print("HAS_PATH=" + str("PATH" in os.environ or "Path" in os.environ), flush=True)
started = time.monotonic()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        # 预热期内返回 503，像 llama-server 加载模型时那样。
        code = 200 if time.monotonic() - started >= warmup else 503
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def reachable(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return True
    except OSError:
        return False


def wait_reachable(port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not reachable(port):
        assert time.monotonic() < deadline, f"端口 {port} 上的假服务没有起来"
        time.sleep(0.2)


def wait_unreachable(port: int, timeout: float = 10.0) -> None:
    """等端口不再接受连接。进程对象结束后，内核回收它的套接字还有极短的延迟。"""
    deadline = time.monotonic() + timeout
    while reachable(port):
        assert time.monotonic() < deadline, f"端口 {port} 在进程结束后仍然可连：进程没有被结束？"
        time.sleep(0.2)


@pytest.fixture
def standalone_server(server_script: Path):
    """在 Supervisor 之外独立启动假服务（模拟用户自己启动的、或上次崩溃留下的进程）。"""
    procs: list[subprocess.Popen] = []

    def launch(port: int) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, str(server_script), str(port)])
        procs.append(proc)
        return proc

    yield launch
    for proc in procs:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
def server_script(tmp_path: Path) -> Path:
    path = tmp_path / "fake_server.py"
    path.write_text(FAKE_SERVER, encoding="utf-8")
    return path


def server_spec(
    script: Path, tmp_path: Path, port: int, *, name: str = "fake", warmup: float = 0.0, **kw
) -> ServiceSpec:
    defaults = dict(
        name=name,
        argv=[sys.executable, str(script), str(port), str(warmup)],
        env={},
        health_url=f"http://127.0.0.1:{port}/",
        log_path=tmp_path / "logs" / f"{name}.log",
        startup_timeout_secs=30.0,
        managed=True,
    )
    return ServiceSpec(**{**defaults, **kw})


def script_spec(tmp_path: Path, code: str, *, name: str = "fake", **kw) -> ServiceSpec:
    """一个执行给定 Python 代码的假服务；健康检查地址指向没人监听的端口。"""
    defaults = dict(
        name=name,
        argv=[sys.executable, "-c", code],
        env={},
        health_url=f"http://127.0.0.1:{free_port()}/",
        log_path=tmp_path / "logs" / f"{name}.log",
        startup_timeout_secs=30.0,
        managed=True,
    )
    return ServiceSpec(**{**defaults, **kw})


@pytest.fixture
async def make_supervisor():
    """创建 Supervisor，并保证测试结束（哪怕失败）时子进程都被结束。"""
    created: list[Supervisor] = []

    def factory(specs: list[ServiceSpec], **kw) -> Supervisor:
        supervisor = Supervisor(specs, poll_interval_secs=0.1, **kw)
        created.append(supervisor)
        return supervisor

    yield factory
    for supervisor in created:
        await supervisor.stop()


async def test_start_wait_healthy_and_stop(make_supervisor, server_script, tmp_path):
    port = free_port()
    spec = server_spec(server_script, tmp_path, port)
    supervisor = make_supervisor([spec])
    assert supervisor.status()[0]["state"] == "not_started"

    async with asyncio.timeout(60):
        await supervisor.start()
        await supervisor.wait_healthy()

    status = supervisor.status()[0]
    assert status["name"] == "fake" and status["managed"] is True
    assert status["state"] == "running" and isinstance(status["pid"], int)
    assert status["health_url"] == spec.health_url
    assert reachable(port)
    assert (await supervisor.probe_all())["fake"].ok

    log = spec.log_path.read_text(encoding="utf-8")
    assert "启动 fake" in log  # 每次启动写一行分隔行

    await supervisor.stop()
    await asyncio.to_thread(wait_unreachable, port)  # 停止后端口释放
    assert supervisor.status()[0]["state"] == "not_started"
    await supervisor.stop()  # 可重复调用


async def test_waits_through_connection_refused_and_503(make_supervisor, server_script, tmp_path):
    """进程刚起来时端口还没监听（连接被拒绝），随后一段时间返回 503：这两种都是「还在启动」。"""
    port = free_port()
    spec = server_spec(server_script, tmp_path, port, warmup=1.5)
    supervisor = make_supervisor([spec])
    began = time.monotonic()
    async with asyncio.timeout(60):
        await supervisor.start()
        await supervisor.wait_healthy()
    assert time.monotonic() - began >= 1.4  # 确实等到了 503 结束
    assert (await supervisor.probe_all())["fake"].ok


async def test_env_is_merged_into_the_process_environment(make_supervisor, server_script, tmp_path):
    port = free_port()
    spec = server_spec(server_script, tmp_path, port, env={"FAKE_FLAG": "hello"})
    supervisor = make_supervisor([spec])
    async with asyncio.timeout(60):
        await supervisor.start()
        await supervisor.wait_healthy()
    log = spec.log_path.read_text(encoding="utf-8")
    assert "FAKE_FLAG=hello" in log  # launch.env 带上了
    assert "HAS_PATH=True" in log  # 同时保留了原有的环境变量


async def test_process_that_exits_fails_fast_with_log_tail(make_supervisor, tmp_path):
    code = "import sys; print('boom: cannot load model', flush=True); sys.exit(3)"
    spec = script_spec(tmp_path, code, startup_timeout_secs=60.0)
    supervisor = make_supervisor([spec])
    began = time.monotonic()
    async with asyncio.timeout(30):
        await supervisor.start()
        with pytest.raises(ServiceStartError) as exc:
            await supervisor.wait_healthy()
    assert time.monotonic() - began < 20  # 进程已退出就立刻失败，不等 60 秒超时
    message = str(exc.value)
    assert "fake" in message and "退出码 3" in message
    assert "boom: cannot load model" in message
    assert supervisor.status()[0]["state"] == "exited"
    assert supervisor.status()[0]["returncode"] == 3


async def test_error_shows_only_this_runs_last_30_lines(make_supervisor, tmp_path):
    code = "\n".join(
        [
            "import sys",
            "for i in range(100): print(f'line-{i:03d}', flush=True)",
            "sys.exit(1)",
        ]
    )
    spec = script_spec(tmp_path, code)
    spec.log_path.parent.mkdir(parents=True)
    spec.log_path.write_text("OLD-RUN-LINE\n", encoding="utf-8")  # 以前运行留下的内容
    supervisor = make_supervisor([spec])
    async with asyncio.timeout(30):
        await supervisor.start()
        with pytest.raises(ServiceStartError) as exc:
            await supervisor.wait_healthy()
    message = str(exc.value)
    assert "line-099" in message and "line-070" in message
    assert "line-069" not in message and "line-000" not in message  # 只取最后 30 行
    assert "OLD-RUN-LINE" not in message
    assert "OLD-RUN-LINE" in spec.log_path.read_text(encoding="utf-8")  # 日志是追加写入的


async def test_timeout_reports_and_stop_kills_the_process(make_supervisor, tmp_path):
    spec = script_spec(tmp_path, "import time; time.sleep(120)", startup_timeout_secs=0.6)
    supervisor = make_supervisor([spec])
    async with asyncio.timeout(30):
        await supervisor.start()
        with pytest.raises(ServiceStartError, match="未就绪"):
            await supervisor.wait_healthy()
        pid_state = supervisor.status()[0]
        assert pid_state["state"] == "running"
        await supervisor.stop()
    assert supervisor.status()[0]["state"] == "not_started"


async def test_unlaunchable_executable_stops_what_was_already_started(
    make_supervisor, server_script, tmp_path
):
    port = free_port()
    good = server_spec(server_script, tmp_path, port, name="good")
    bad = script_spec(tmp_path, "pass", name="bad")
    bad.argv = ["definitely-not-an-existing-program-xyz"]
    supervisor = make_supervisor([good, bad])
    async with asyncio.timeout(60):
        with pytest.raises(ServiceStartError, match="bad"):
            await supervisor.start()
    # start() 要么全成功要么全不留：失败时先启动的 good 也已被结束。
    await asyncio.to_thread(wait_unreachable, port)
    assert {s["state"] for s in supervisor.status()} == {"not_started"}


async def test_already_running_service_is_reused_and_left_alone(
    make_supervisor, server_script, standalone_server, tmp_path
):
    port = free_port()
    spec = server_spec(server_script, tmp_path, port)
    standalone = standalone_server(port)
    await asyncio.to_thread(wait_reachable, port)

    supervisor = make_supervisor([spec])
    async with asyncio.timeout(30):
        await supervisor.start()
        await supervisor.wait_healthy()
    status = supervisor.status()[0]
    assert status["state"] == "reused" and status["pid"] is None
    assert not spec.log_path.exists()  # 没有启动新进程，也就没有新日志

    await supervisor.stop()
    assert standalone.poll() is None  # 别人的进程不能被我们结束
    assert reachable(port)


async def test_unmanaged_services_are_checked_once_and_never_awaited(
    make_supervisor, server_script, standalone_server, tmp_path
):
    up_port = free_port()
    up = server_spec(server_script, tmp_path, up_port, name="up", managed=False, argv=[])
    down = check_spec(f"http://127.0.0.1:{free_port()}/", name="down", startup_timeout_secs=0.0)
    standalone_server(up_port)
    await asyncio.to_thread(wait_reachable, up_port)

    supervisor = make_supervisor([up, down])
    began = time.monotonic()
    async with asyncio.timeout(30):
        await supervisor.start()
        await supervisor.wait_healthy()  # 不通的不受管服务不会让它抛异常
    assert time.monotonic() - began < 10  # 也不会为它等待
    assert [s["state"] for s in supervisor.status()] == ["external", "external"]
    results = await supervisor.probe_all()
    assert results["up"].ok and not results["down"].ok


async def test_loopback_probe_ignores_system_proxy(
    make_supervisor, server_script, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """设了 HTTP_PROXY 的机器上，经代理访问 127.0.0.1 会被判成「不通」；本机探测必须绕开它。"""
    for var in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")  # 9 端口没人监听
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)

    port = free_port()
    spec = server_spec(server_script, tmp_path, port)
    supervisor = make_supervisor([spec])
    async with asyncio.timeout(60):
        await supervisor.start()
        await supervisor.wait_healthy()
        assert (await probe(spec)).ok  # 不传 client 时同样如此


async def test_start_writes_asr_template_when_cfg_given(
    make_supervisor, make_cfg: CfgFactory, server_script, tmp_path
):
    cfg = make_cfg()
    port = free_port()
    asr = server_spec(server_script, tmp_path, port, name="asr")
    supervisor = make_supervisor([asr], cfg=cfg)
    assert not asr_template_path(cfg).exists()
    async with asyncio.timeout(60):
        await supervisor.start()
        await supervisor.wait_healthy()
    assert asr_template_path(cfg).read_bytes() == load_asr_profile(cfg).chat_template.encode()


async def test_start_without_asr_does_not_write_template(
    make_supervisor, make_cfg: CfgFactory, server_script, tmp_path
):
    cfg = make_cfg()
    other = server_spec(server_script, tmp_path, free_port(), name="tts")
    supervisor = make_supervisor([other], cfg=cfg)
    async with asyncio.timeout(60):
        await supervisor.start()
        await supervisor.wait_healthy()
    assert not asr_template_path(cfg).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 的终止信号本身就是强制结束")
async def test_stop_kills_a_process_that_ignores_terminate(make_supervisor, tmp_path):
    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(120)\n"
    )
    spec = script_spec(tmp_path, code)
    supervisor = make_supervisor([spec], stop_grace_secs=0.5)
    async with asyncio.timeout(30):
        await supervisor.start()
        await asyncio.sleep(1.0)  # 等它装好信号处理
        began = time.monotonic()
        await supervisor.stop()
    assert time.monotonic() - began < 10


@pytest.mark.skipif(sys.platform != "win32", reason="作业对象只有 Windows 有")
def test_a_child_dies_with_its_parent_even_when_the_parent_is_killed(tmp_path):
    """父进程被强杀（来不及收尾）时，绑在它身上的子进程由系统结束，不留孤儿。"""
    import subprocess
    import time

    pid_file = tmp_path / "child.pid"
    parent_code = (
        "import subprocess, sys, time\n"
        "from agentic_meeting.services.supervisor import tie_to_parent\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "assert tie_to_parent(child.pid)\n"
        f"open(r'{pid_file}', 'w').write(str(child.pid))\n"
        "time.sleep(120)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_code])
    try:
        deadline = time.monotonic() + 30
        while not (pid_file.exists() and pid_file.read_text()) and time.monotonic() < deadline:
            time.sleep(0.1)
        child_pid = int(pid_file.read_text())
        assert _alive(child_pid)
        parent.kill()  # TerminateProcess：父进程没有任何收尾的机会
        parent.wait()
        deadline = time.monotonic() + 10
        while _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(child_pid)
    finally:
        parent.kill()


def _alive(pid: int) -> bool:
    import subprocess

    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True
    ).stdout
    return f'"{pid}"' in out
