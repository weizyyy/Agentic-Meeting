"""代码执行沙箱的创建与清理（docs/agents-sdk-notes.md §4）。

后台 agent 需要算数、处理数据、画图时写代码执行，代码跑在沙箱里：

* ``sandbox.kind = "docker"``：Agents SDK 的 ``DockerSandboxClient``，每个任务一个容器，用完删除。
  ``sandbox.network = false`` 时容器不联网（``network_mode="none"``）。
* ``"local"``：``UnixLocalSandboxClient``，直接在本机的临时目录里执行，只支持 macOS / Linux。
* ``"none"``：不给代码执行能力。

沙箱的工作区在沙箱**里面**（默认 ``/workspace``），不是把宿主机的目录挂进去。所以：

* 任务带的截图在会话启动后写进工作区的 ``input/``（``put_inputs``）；
* 任务结束后按 agent 报告的文件名把产物从工作区读回任务目录（``fetch``）。

沙箱开不起来（没装 Docker、Docker 没启动、镜像没配、Windows 上选了 local）不算任务失败：
``open_sandbox`` 抛 ``SandboxUnavailable``，运行器据此退回到「只能检索和读图」，并把原因告诉 agent 和用户
（architecture.md §9）。
"""

from __future__ import annotations

import asyncio
import io
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from loguru import logger

from agentic_meeting.config import SandboxConfig
from agentic_meeting.store.work import drain_io

MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
INPUT_DIR = "input"


class SandboxUnavailable(Exception):
    """沙箱开不起来。消息是一句可以直接告诉用户的中文。"""


class SandboxHandle(Protocol):
    """运行器用到的沙箱接口（测试里换成假的）。"""

    @property
    def session(self) -> Any:
        """SDK 的沙箱会话，放进 ``SandboxRunConfig(session=...)``。"""

    async def put_inputs(self, files: dict[str, bytes]) -> None: ...

    async def fetch(
        self, names: list[str], dest: Path, *, write: Callable[[Path, bytes], None] | None = None
    ) -> list[str]: ...


def sandbox_capabilities() -> list[Any]:
    """给沙箱里的 agent 的能力：只有 Shell（``exec_command``，以及会话支持伪终端时的 ``write_stdin``）。

    SDK 的默认是 ``[Filesystem(), Shell(), Compaction()]``，但 ``Filesystem`` 带的 ``apply_patch`` 是一种
    「自由格式」的工具，只有 Responses API 支持；我们走的是 chat completions，带上它整个请求都发不出去
    （否则报 ``Hosted tools are not supported with the ChatCompletions API``）。
    写文件、看文件都可以用 shell 做，所以只留 Shell。``Compaction`` 也是为 Responses API 准备的，一并不要。
    """
    from agents.sandbox.capabilities.shell import Shell

    return [Shell()]


def safe_artifact_name(name: str) -> str | None:
    """agent 报告的产物文件名 → 工作区里的相对路径；绝对路径、带 ``..`` 的、空的一律不要。"""
    cleaned = name.strip().replace("\\", "/")
    for prefix in ("/workspace/", "./"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    path = PurePosixPath(cleaned)
    if not cleaned or path.is_absolute() or ".." in path.parts or ":" in cleaned:
        return None
    return path.as_posix()


class SdkSandbox:
    """包着一个 SDK 沙箱会话：往里放输入、往外取产物。"""

    def __init__(self, session: Any) -> None:
        self._session = session

    @property
    def session(self) -> Any:
        return self._session

    async def put_inputs(self, files: dict[str, bytes]) -> None:
        if not files:
            return
        await self._session.mkdir(Path(INPUT_DIR), parents=True)
        for name, data in files.items():
            await self._session.write(Path(INPUT_DIR) / name, io.BytesIO(data))

    async def fetch(
        self, names: list[str], dest: Path, *, write: Callable[[Path, bytes], None] | None = None
    ) -> list[str]:
        """把工作区里的这些文件读回 ``dest``。读不到的、太大的跳过并记日志；返回实际取回的文件名。"""
        fetched: list[str] = []
        for raw in names:
            name = safe_artifact_name(raw)
            if name is None:
                logger.warning(f"产物文件名不合规，已跳过：{raw!r}")
                continue
            try:
                stream = await self._session.read(Path(name))
                try:
                    data = await drain_io(asyncio.to_thread(stream.read, MAX_ARTIFACT_BYTES + 1))
                finally:
                    stream.close()
            except Exception as e:
                logger.warning(f"取不回产物 {name}：{type(e).__name__}: {e}")
                continue
            if len(data) > MAX_ARTIFACT_BYTES:
                logger.warning(f"产物 {name} 超过 {MAX_ARTIFACT_BYTES // (1024 * 1024)} MB，已跳过")
                continue
            target = dest / name
            await drain_io(asyncio.to_thread(write or _write_file, target, data))
            fetched.append(name)
        return fetched


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@asynccontextmanager
async def open_sandbox(cfg: SandboxConfig) -> AsyncIterator[SdkSandbox | None]:
    """按配置开一个沙箱，用完清理。``kind = "none"`` 时给出 ``None``；开不起来抛 ``SandboxUnavailable``。"""
    if cfg.kind == "none":
        yield None
        return
    client, options = await _client_and_options(cfg)
    try:
        session = await client.create(options=options)
        await session.start()
    except Exception as e:
        logger.warning(f"代码执行环境启动失败：{type(e).__name__}: {e}")
        raise SandboxUnavailable(f"代码执行环境启动失败（{type(e).__name__}）") from e
    try:
        yield SdkSandbox(session)
    finally:
        # 清理不能因为一处失败就漏掉后面的：容器要删掉
        for step in (session.aclose, lambda: client.delete(session)):
            try:
                await step()
            except Exception as e:
                logger.warning(f"清理代码执行环境时出错：{type(e).__name__}: {e}")


async def _client_and_options(cfg: SandboxConfig) -> tuple[Any, Any]:
    if cfg.kind == "docker":
        if not cfg.docker_image:
            raise SandboxUnavailable("没有配置沙箱镜像（agent.sandbox.docker_image）")
        try:
            from agents.sandbox.sandboxes.docker import (
                DockerSandboxClient,
                DockerSandboxClientOptions,
            )

            import docker

            docker_client = await asyncio.to_thread(docker.from_env)
            await asyncio.to_thread(docker_client.ping)
        except ImportError as e:
            raise SandboxUnavailable(
                "没有安装 Docker 的 Python 依赖（uv sync --extra agent）"
            ) from e
        except Exception as e:
            raise SandboxUnavailable("连不上 Docker（没有安装，或者没有启动）") from e
        options = DockerSandboxClientOptions(
            image=cfg.docker_image, network_mode=None if cfg.network else "none"
        )
        return DockerSandboxClient(docker_client), options

    # kind == "local"
    if sys.platform == "win32":
        raise SandboxUnavailable(
            "local 沙箱不支持 Windows，请把 agent.sandbox.kind 改成 docker 或 none"
        )
    from agents.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxClientOptions,
    )

    return UnixLocalSandboxClient(), UnixLocalSandboxClientOptions()
