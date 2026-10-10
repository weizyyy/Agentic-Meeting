"""compose.yaml 在容器里调用的两件小事（docs/containers.md）。

    python scripts/container.py healthcheck                 # 应用的 /healthz 有应答时退出码为 0
    python scripts/container.py asr-template <输出文件>     # 写出识别服务启动时要用的对话模板

``healthcheck`` 只看进程是否在应答（``/healthz``，不需要登录）。启用了 TLS 时不校验证书：
证书签发给局域网地址或域名，这里访问的是本机。

``asr-template`` 把 ``asr.profile`` 里的 ``chat_template`` 逐字写成文件，内容与 ``services up``
写的 ``data/run/asr_chat_template.jinja`` 相同（interfaces.md §9）。识别服务的容器用
``--chat-template-file`` 读它。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx

from agentic_meeting.config import AppConfig, load_asr_profile, load_config, load_env_file

TIMEOUT_SECS = 5.0


def healthcheck(cfg: AppConfig) -> bool:
    host = cfg.server.host
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    scheme = "https" if cfg.server.tls_cert else "http"
    url = f"{scheme}://{host}:{cfg.server.port}/healthz"
    try:
        response = httpx.get(url, timeout=TIMEOUT_SECS, verify=False, trust_env=False)
    except httpx.HTTPError as e:
        print(f"{url} 无应答：{type(e).__name__}")
        return False
    if response.status_code != 200:
        print(f"{url} 返回 {response.status_code}")
        return False
    return True


def write_asr_template(cfg: AppConfig, out: Path) -> None:
    profile = load_asr_profile(cfg)
    out.parent.mkdir(parents=True, exist_ok=True)
    # 按字节写：模板里的换行不能被改动
    out.write_bytes(profile.chat_template.encode("utf-8"))
    print(f"已写出识别服务的对话模板：{out}（取自 {cfg.asr.profile}）")


def main() -> None:
    parser = argparse.ArgumentParser(description="容器里用的健康检查与准备步骤")
    parser.add_argument("--config", help="配置文件路径（默认 config/config.toml）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("healthcheck", help="应用的 /healthz 有应答时退出码为 0")
    template = sub.add_parser("asr-template", help="写出识别服务的对话模板")
    template.add_argument("out", type=Path, help="输出文件")
    args = parser.parse_args()

    load_env_file()
    cfg = load_config(args.config)
    if args.command == "healthcheck":
        sys.exit(0 if healthcheck(cfg) else 1)
    write_asr_template(cfg, args.out)


if __name__ == "__main__":
    main()
