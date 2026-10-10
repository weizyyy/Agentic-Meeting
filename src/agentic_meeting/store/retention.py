"""应用管理文件的安全清理与单个后台保留任务（interfaces.md §8.4）。"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from agentic_meeting.config import RetentionConfig
from agentic_meeting.store.db import CleanupPlan, SessionBusy, Store
from agentic_meeting.store.work import drain_io

if TYPE_CHECKING:
    from agentic_meeting.pipeline.session import SessionManager

_SESSION = re.compile(r"[0-9a-f]{32}\Z")
_LABEL = re.compile(r"t[1-9][0-9]*\Z")


class OwnedFiles:
    """只接受固定应用子树，不以数据库 path 或任意配置目录作为删除根。"""

    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir).resolve()

    def _path(self, session_id: str, *parts: str, leaf_link: bool = False) -> Path:
        if not _SESSION.fullmatch(session_id):
            raise ValueError("会议编号不合法")
        path = self.root / "sessions" / session_id
        target = path.joinpath(*parts)
        current = self.root
        components = ("sessions", session_id, *parts)
        for index, component in enumerate(components):
            if component in ("", ".", "..") or "/" in component or "\\" in component:
                raise ValueError("文件路径不合法")
            current /= component
            linked = current.is_symlink() or current.is_junction()
            if linked and not (leaf_link and index == len(components) - 1):
                raise ValueError("文件路径包含链接目录")
        if not target.parent.resolve().is_relative_to(self.root):
            raise ValueError("文件路径超出数据目录")
        return target

    def write_path(self, session_id: str, relative: str) -> Path:
        """在执行落盘的线程内也校验父目录和叶文件，不能沿链接写外部文件。"""
        parts = Path(relative).parts
        if len(parts) < 4 or parts[:2] != ("sessions", session_id):
            raise ValueError("文件不属于这场会议")
        if parts[2] not in ("frames", "tasks"):
            raise ValueError("文件不属于可管理类别")
        if parts[2] == "tasks" and (len(parts) < 5 or not _LABEL.fullmatch(parts[3])):
            raise ValueError("任务短编号不合法")
        return self._path(session_id, *parts[2:])

    def remove_session(self, session_id: str) -> None:
        self._remove(self._path(session_id))

    def remove_frames(self, session_id: str) -> None:
        self._remove(self._path(session_id, "frames"))

    def remove_task(self, session_id: str, label: str) -> None:
        if not _LABEL.fullmatch(label):
            raise ValueError("任务短编号不合法")
        self._remove(self._path(session_id, "tasks", label, leaf_link=True))

    @staticmethod
    def _remove(path: Path) -> None:
        if path.is_junction():
            path.rmdir()
        elif path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)


class RetentionWorker:
    def __init__(
        self,
        store: Store,
        sessions: SessionManager,
        policy: RetentionConfig,
        data_dir: Path,
        *,
        now: Callable[[], float] = time.time,
        on_cleaned: Callable[[CleanupPlan], None] | None = None,
    ) -> None:
        self.store = store
        self.sessions = sessions
        self.policy = policy
        self.files = OwnedFiles(data_dir)
        self._now = now
        self._on_cleaned = on_cleaned
        self._task: asyncio.Task | None = None
        self._attempts: set[asyncio.Task] = set()
        self._closing = False
        self._after = ""
        self._pass_lock = asyncio.Lock()

    def start(self) -> None:
        if self._task is None and not self._closing:
            self._task = asyncio.create_task(self._loop(), name="retention-cleanup")

    async def stop(self) -> None:
        self._closing = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._attempts:
            await asyncio.gather(*self._attempts, return_exceptions=True)

    async def delete_session(self, session_id: str) -> None:
        """人工删除忽略 keep；只在全部文件与数据库提交完成后返回。"""
        if self._closing:
            raise SessionBusy("服务正在停止")
        attempt = asyncio.current_task()
        assert attempt is not None
        self._attempts.add(attempt)
        try:
            plan = await self.sessions.claim_cleanup(
                session_id, self.policy, self._now(), manual=True
            )
            if plan is None:
                raise LookupError("找不到这场会议")
            await self._execute(plan)
        finally:
            self._attempts.discard(attempt)

    async def run_once(self) -> int:
        """有限一轮；pending 无论自动期限是否启用都重试。"""
        async with self._pass_lock:
            enabled = any(
                (
                    self.policy.transcript_days,
                    self.policy.screenshots_days,
                    self.policy.reports_days,
                    self.policy.task_artifacts_days,
                )
            )
            now = self._now()
            deadline = time.monotonic() + 10.0
            count = 0
            for session_id in await self.store.cleanup_candidates(
                enabled=enabled, after=self._after
            ):
                if self._closing or time.monotonic() >= deadline:
                    break
                self._after = session_id
                try:
                    plan = await self.sessions.claim_cleanup(session_id, self.policy, now)
                    if plan is None:
                        continue
                    await self._execute(plan)
                    count += 1
                except SessionBusy:
                    continue
                except Exception as exc:
                    logger.warning("清理失败，之后重试：{} {}", session_id, type(exc).__name__)
            return count

    async def _execute(self, plan: CleanupPlan) -> None:
        attempt = asyncio.current_task()
        self._attempts.add(attempt)
        try:
            await drain_io(asyncio.to_thread(self._remove_files, plan))
            await self.store.complete_cleanup(plan)
            if self._on_cleaned is not None:
                self._on_cleaned(plan)
            if plan.manual:
                await self.sessions.forget(plan.session_id)
            logger.info(
                "清理成功：{} {} transcript={} screenshots={} reports={} digests={} tasks={}",
                plan.session_id,
                "manual" if plan.manual else "retention",
                plan.transcript,
                plan.screenshots,
                len(plan.reports),
                len(plan.digests),
                len(plan.tasks),
            )
        except BaseException as exc:
            logger.warning("删除未完成：{} {}", plan.session_id, type(exc).__name__)
            raise
        finally:
            try:
                await drain_io(self.store.release_cleanup(plan.session_id))
            finally:
                self._attempts.discard(attempt)

    def _remove_files(self, plan: CleanupPlan) -> None:
        if plan.manual:
            self.files.remove_session(plan.session_id)
        else:
            if plan.screenshots:
                self.files.remove_frames(plan.session_id)
            for task_id in plan.tasks:
                self.files.remove_task(plan.session_id, task_id.rsplit(".", 1)[-1])

    async def _loop(self) -> None:
        while not self._closing:
            try:
                await self.run_once()
            except Exception as exc:
                logger.warning("清理轮次失败，之后重试：{}", type(exc).__name__)
            await asyncio.sleep(self.policy.cleanup_interval_secs)
