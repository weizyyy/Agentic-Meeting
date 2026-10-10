"""命令行入口：``uv run agentic-meeting <子命令>``。

子命令：
    check              校验配置并列出缺项（权重文件、密钥、镜像名）。
    services up        按配置拉起本地推理服务，等就绪，挂着直到 Ctrl+C，再全部停掉。
    services status    只做健康检查并打印，不启动任何东西。
    serve              启动 Pipecat 应用与网页客户端；--with-services 时先拉起本地推理服务。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import unicodedata
from pathlib import Path

from pydantic import ValidationError

from agentic_meeting.config import (
    AppConfig,
    check_ready,
    check_warnings,
    load_config,
    load_env_file,
    server_warnings,
)
from agentic_meeting.services.paths import find_executable
from agentic_meeting.services.supervisor import (
    ProbeResult,
    ServiceSpec,
    ServiceStartError,
    Supervisor,
    build_specs,
    check_tts_voice,
)


def _load_dotenv() -> None:
    """命令行入口：不管有没有用 --config 指定配置，都读仓库根目录的 .env。"""
    load_env_file()


def _load_cfg(config_path: str | None) -> AppConfig | None:
    """读配置；读不出来时把原因打印出来并返回 None（调用方以退出码 2 结束）。"""
    try:
        return load_config(config_path)
    except FileNotFoundError as e:
        print(e)
    except ValidationError as e:
        print("配置文件结构有误：")
        for err in e.errors():
            loc = ".".join(str(part) for part in err["loc"])
            print(f"  {loc}: {err['msg']}")
    return None


def cmd_check(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args.config)
    if cfg is None:
        return 2

    endpoint = cfg.realtime_llm.active
    print(
        f"实时模型接入方式：{cfg.realtime_llm.mode}（{endpoint.base_url}，模型 {endpoint.model or '未填'}）"
    )
    for warning in [*server_warnings(cfg), *check_warnings(cfg)]:
        print(f"提醒：{warning}")

    problems = check_ready(cfg)
    if problems:
        print(f"配置结构正确，但还有 {len(problems)} 项未就绪：")
        for p in problems:
            print(f"  - {p}")
        print("\n推理程序是否装好请另行运行：python scripts/runtimes.py status")
        return 1
    print("配置就绪。")
    return 0


# --------------------------------------------------------------------------- #
# services
# --------------------------------------------------------------------------- #


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _find_or_bare(name: str, override: str = "") -> Path:
    """``status`` 只需要地址、不启动进程：程序没装好时用裸名字占位，不报错。"""
    try:
        return find_executable(name, override)
    except FileNotFoundError:
        return Path(name)


def _format_table(
    specs: list[ServiceSpec],
    results: dict[str, ProbeResult],
    rows: list[dict] | None = None,
) -> list[str]:
    """状态表。``rows`` 是 ``Supervisor.status()`` 的结果；``status`` 子命令没有进程可看，传 None。"""
    by_name = {row["name"]: row for row in rows or []}
    lines = [f"  {_pad('服务', 11)}{_pad('状态', 9)}{_pad('进程', 24)}地址"]
    notes: list[str] = []
    for spec in specs:
        result = results[spec.name]
        row = by_name.get(spec.name)
        state = ("就绪" if spec.managed else "连通") if result.ok else "不通"
        if row is None:
            who = "受管" if spec.managed else "不受管"
        elif row["state"] == "reused":
            who = "复用已在运行的服务"
        elif row["state"] == "running":
            who = f"受管，pid {row['pid']}"
        elif row["state"] == "exited":
            who = f"受管，已退出（{row['returncode']}）"
        else:
            who = "不受管"
        line = f"  {_pad(spec.name, 11)}{_pad(f'[{state}]', 9)}{_pad(who, 24)}{spec.health_url}"
        if not result.ok:
            line += f"（{result.detail}）"
        lines.append(line)
        if spec.note:
            notes.append(f"    └ {spec.name}：{spec.note}")
    return lines + notes


async def _probe_all(specs: list[ServiceSpec]) -> dict[str, ProbeResult]:
    supervisor = Supervisor(specs)
    try:
        return await supervisor.probe_all()
    finally:
        await supervisor.stop()  # 关掉探测时创建的客户端


def cmd_services_status(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args.config)
    if cfg is None:
        return 2
    try:
        specs = build_specs(cfg, find=_find_or_bare)
    except ValueError as e:
        print(e)
        return 2

    results = asyncio.run(_probe_all(specs))
    print("\n".join(_format_table(specs, results)))
    down = [name for name, result in results.items() if not result.ok]
    if down:
        print(f"\n不通：{'、'.join(down)}。")
    return 1 if down else 0


async def _bring_up(
    cfg: AppConfig, specs: list[ServiceSpec], supervisor: Supervisor
) -> dict[str, ProbeResult]:
    """启动、等就绪、核对音色、打印状态表。失败抛 ``ServiceStartError``（调用方负责停掉已启动的）。"""
    await supervisor.start()
    await supervisor.wait_healthy()
    results = await supervisor.probe_all()
    if cfg.tts.enabled and "tts" in results and results["tts"].ok:
        await check_tts_voice(cfg)

    print("\n".join(_format_table(specs, results, supervisor.status())))
    down = [name for name, result in results.items() if not result.ok]
    if down:
        print(f"\n警告：{'、'.join(down)} 不通，相关功能会降级（见 docs/architecture.md §9）。")
    return results


async def _run_up(cfg: AppConfig, specs: list[ServiceSpec]) -> int:
    """启动、等就绪、打印状态表，然后一直挂着。

    被取消（asyncio.run 收到 Ctrl+C 时的做法）时，``finally`` 里把全部服务停掉，
    CancelledError 照常向上传，不吞掉。
    """
    supervisor = Supervisor(specs, cfg=cfg)
    try:
        try:
            await _bring_up(cfg, specs, supervisor)
        except ServiceStartError as e:
            print(f"\n启动失败：{e}")
            return 1
        print("\n全部就绪。按 Ctrl+C 停止所有服务。")
        await asyncio.Event().wait()
        return 0  # 永远不会走到：上一行只会被取消打断
    finally:
        await supervisor.stop()


def cmd_services_up(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args.config)
    if cfg is None:
        return 2
    if not _check_before_start(cfg):
        return 1

    try:
        specs = build_specs(cfg)
    except FileNotFoundError as e:
        print(e)
        return 1
    except ValueError as e:
        print(e)
        return 2

    try:
        return asyncio.run(_run_up(cfg, specs))
    except KeyboardInterrupt:
        print("\n已停止全部服务。")
        return 0


def _check_before_start(cfg: AppConfig) -> bool:
    """打印数据外发提醒，核对缺项。有缺项返回 False（调用方以退出码 1 结束）。"""
    for warning in check_warnings(cfg):
        print(f"提醒：{warning}")
    problems = check_ready(cfg)
    if problems:
        print(f"还有 {len(problems)} 项未就绪，先补齐再启动：")
        for problem in problems:
            print(f"  - {problem}")
        print("\n推理程序是否装好请另行运行：python scripts/runtimes.py status")
        return False
    return True


async def _run_serve(cfg: AppConfig, specs: list[ServiceSpec], *, with_services: bool) -> int:
    """（可选地先拉起推理服务，）再启动 HTTP 应用，直到被中断；退出时停掉自己拉起的服务。"""
    from agentic_meeting.web.app import make_server  # 延迟导入：check / services 不需要 Pipecat

    supervisor = Supervisor(specs, cfg=cfg) if with_services else None
    try:
        if supervisor is not None:
            try:
                await _bring_up(cfg, specs, supervisor)
            except ServiceStartError as e:
                print(f"\n启动失败：{e}")
                return 1
        else:
            # 没有让本命令拉起服务：只看一眼它们是否可达，不通就提醒，不拦着启动。
            results = await _probe_all(specs)
            down = [name for name, result in results.items() if not result.ok]
            if down:
                print(
                    f"提醒：{'、'.join(down)} 当前不通，相关功能会降级；"
                    "可以另开终端运行 services up，或下次加 --with-services。"
                )

        scheme = "https" if cfg.server.tls_cert else "http"
        print(f"\n服务地址：{scheme}://localhost:{cfg.server.port}　按 Ctrl+C 停止。")
        await make_server(cfg).serve()
        return 0
    finally:
        if supervisor is not None:
            await supervisor.stop()


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args.config)
    if cfg is None:
        return 2
    for warning in server_warnings(cfg):
        print(f"提醒：{warning}")
    if not _check_before_start(cfg):
        return 1

    try:
        specs = build_specs(cfg) if args.with_services else build_specs(cfg, find=_find_or_bare)
    except FileNotFoundError as e:
        print(e)
        return 1
    except ValueError as e:
        print(e)
        return 2

    try:
        return asyncio.run(_run_serve(cfg, specs, with_services=args.with_services))
    except KeyboardInterrupt:
        print("\n已停止。")
        return 0


def main() -> None:
    # 输出被重定向到文件或管道时，Windows 默认用本地代码页；两个流统一成 UTF-8，避免中文乱码。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    _load_dotenv()

    parser = argparse.ArgumentParser(prog="agentic-meeting")
    parser.add_argument("--config", help="配置文件路径（默认 config/config.toml）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="校验配置并列出缺项").set_defaults(func=cmd_check)

    services = sub.add_parser("services", help="管理本地推理服务")
    services_sub = services.add_subparsers(dest="services_command", required=True)
    services_sub.add_parser(
        "up", help="拉起配置里由本项目启动的服务，等就绪，Ctrl+C 时全部停止"
    ).set_defaults(func=cmd_services_up)
    services_sub.add_parser("status", help="检查各服务是否可达（不启动任何东西）").set_defaults(
        func=cmd_services_status
    )

    serve = sub.add_parser("serve", help="启动应用与网页客户端")
    serve.add_argument(
        "--with-services",
        action="store_true",
        help="先拉起配置里由本项目启动的推理服务，Ctrl+C 时一并停止",
    )
    serve.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
