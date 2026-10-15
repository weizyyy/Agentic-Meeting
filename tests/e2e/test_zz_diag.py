"""临时诊断（不合入）：Windows 上开 HTTPS、有连接在握手中被重置后，Ctrl+Break 能否停下。"""

from __future__ import annotations

import asyncio
import datetime
import os
import socket
import ssl
import struct
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from .conftest import App, app_config, dump_toml


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _self_signed(cert: Path, key: Path) -> None:
    k = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    c = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(k.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(k, hashes.SHA256())
    )
    cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(
        k.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def _rst(sock: socket.socket) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


def _abuse(port: int, kind: str) -> None:
    addr = ("127.0.0.1", port)
    if kind == "verify_fails":
        # 浏览器不信任自签名证书时就是这样：客户端发告警后断开
        for _ in range(3):
            try:
                with socket.create_connection(addr, timeout=5) as raw:
                    ssl.create_default_context().wrap_socket(raw, server_hostname="localhost")
            except (ssl.SSLError, OSError):
                pass
    elif kind == "verify_fails_rst":
        for _ in range(3):
            raw = socket.create_connection(addr, timeout=5)
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            try:
                ssl.create_default_context().wrap_socket(raw, server_hostname="localhost")
            except (ssl.SSLError, OSError):
                pass
            raw.close()
    elif kind == "rst_mid_handshake":
        for _ in range(3):
            raw = socket.create_connection(addr, timeout=5)
            raw.sendall(b"\x16\x03\x01\x00\xa5\x01\x00\x00\xa1\x03\x03")  # 半个 ClientHello
            time.sleep(0.2)
            _rst(raw)
    elif kind == "rst_after_request":
        ctx = ssl._create_unverified_context()
        for _ in range(3):
            raw = socket.create_connection(addr, timeout=5)
            tls = ctx.wrap_socket(raw, server_hostname="localhost")
            tls.sendall(b"GET /api/time HTTP/1.1\r\nHost: x\r\n\r\n")
            tls.recv(65536)
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            tls.close()
    elif kind == "idle_connect":
        socks = [socket.create_connection(addr, timeout=5) for _ in range(3)]
        time.sleep(0.5)
        for s in socks:
            _rst(s)


@pytest.mark.parametrize(
    "kind",
    [
        "none",
        "verify_fails",
        "verify_fails_rst",
        "rst_mid_handshake",
        "rst_after_request",
        "idle_connect",
    ],
)
async def test_diag_tls_stop(inference, tmp_path, kind):
    inference.reset()
    port = _port()
    cfg = app_config(inference, tmp_path / "data", port)
    _self_signed(tmp_path / "cert.pem", tmp_path / "key.pem")
    cfg["server"].update(tls_cert=str(tmp_path / "cert.pem"), tls_key=str(tmp_path / "key.pem"))
    (tmp_path / "config.toml").write_text(dump_toml(cfg), encoding="utf-8")
    log = tmp_path / "app.log"
    env = {k: v for k, v in os.environ.items() if not k.upper().endswith("_PROXY")}
    env.update(PYTHONUTF8="1")
    with log.open("wb") as out:
        proc = subprocess.Popen(  # noqa: ASYNC220
            [
                sys.executable,
                "-m",
                "agentic_meeting.cli",
                "--config",
                str(tmp_path / "config.toml"),
                "serve",
            ],
            stdout=out,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=tmp_path,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    app = App(f"https://127.0.0.1:{port}", tmp_path / "data", log, proc)
    async with httpx.AsyncClient(verify=False, trust_env=False) as http:
        for _ in range(600):
            try:
                if (await http.get(f"{app.base_url}/healthz")).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.1)
        else:
            raise AssertionError(app.log_text()[-3000:])
    if kind != "none":
        await asyncio.to_thread(_abuse, port, kind)
        await asyncio.sleep(1)
    started = time.monotonic()
    rc = await app.shutdown(timeout_secs=25)
    took = time.monotonic() - started
    print(f"==== kind={kind} rc={rc} took={took:.1f}s ====")
    text = app.log_text()
    print("\n".join(line for line in text.splitlines() if "| DEBUG" not in line)[-5000:])
    raise AssertionError(f"diag kind={kind} rc={rc} took={took:.1f}s")
