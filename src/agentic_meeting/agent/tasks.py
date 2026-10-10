"""任务管理器（docs/interfaces.md §8.1、docs/architecture.md §5.4）。

实时模型把复杂的事委托出来（``delegate_task``），这里负责：建任务、排队、控制并发、限时、取消，
把每一次状态变化和每一条进度落库并推给页面。真正干活的是「运行器」——一个通过构造参数注入的可调用对象
``async def run(task, on_event) -> TaskResult``（正式的是 ``agent/runner.py``，测试里用假的）。

* 任务属于一场会议，但管理器是整个应用一个：连接断开、会议换了，任务照样跑完，结果在库里。
* 并发上限 ``agent.max_concurrent_tasks``，超出的排队；单个任务限时 ``agent.task_timeout_secs``。
* 应用启动时（``recover``）把上次遗留的排队中 / 运行中的任务标为失败——它们的执行已经不在了。
* 任务编号：库里是 ``<会话 id>.t<序号>``（全库唯一），口头和界面上用点号后面的短编号（``t3``）。

失败和取消对调用方表现为 ``TaskFailed``，``reason`` 是一句可以直接告诉用户的中文。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loguru import logger

from agentic_meeting.store.db import Store
from agentic_meeting.store.work import drain_io
from agentic_meeting.types import TaskRecord, TaskResult

RECENT_STEPS = 3  # 回答「做到哪了」时带最近几条进度
RESTART_REASON = "服务重启，任务没有做完"
SHUTDOWN_REASON = "服务停止，任务没有做完"
CANCELLED_TEXT = "已取消"

OnEvent = Callable[[str, str, dict | None], Awaitable[None]]
Runner = Callable[[TaskRecord, OnEvent], Awaitable[TaskResult]]
Notify = Callable[[str, dict], Awaitable[Any]]


class TaskFailed(Exception):
    """任务没有成功。``status`` 是 ``failed`` 或 ``cancelled``，``reason`` 是给用户看的中文原因。"""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class RunnerError(Exception):
    """运行器报告的、可以直接告诉用户的失败原因（比如「远端模型连不上」）。"""


def task_dir(data_dir: Path, task: TaskRecord) -> Path:
    """任务的工作目录：``<data_dir>/sessions/<会话>/tasks/<短编号>/``（截图原件、代码产物都在这里）。"""
    return Path(data_dir) / "sessions" / task.session_id / "tasks" / task.label


def task_message(task: TaskRecord) -> dict[str, Any]:
    """推给页面的 ``task`` 消息（interfaces.md §6.1）。"""
    return {
        "type": "task",
        "id": task.id,
        "label": task.label,
        "goal": task.goal,
        "status": task.status,
        "brief": task.brief,
        "error": task.error,
        "modality": task.modality,
        "created_at": task.created_at,
    }


class TaskManager:
    def __init__(
        self,
        *,
        store: Store,
        runner: Runner,
        notify: Notify,
        max_concurrent: int,
        timeout_secs: float,
        now: Callable[[], float] = time.time,
    ) -> None:
        """``notify(session_id, data)`` 向页面推消息（只在那场会议正在进行时真的发出去，由调用方保证）。"""
        self._store = store
        self._runner = runner
        self._notify = notify
        self._timeout = timeout_secs
        self._now = now
        self._slots = asyncio.Semaphore(max(1, max_concurrent))
        self._running: dict[str, asyncio.Task] = {}
        self._done: dict[str, asyncio.Event] = {}
        self._cancel_requested: set[str] = set()
        self._closing = False
        self._admissions: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def recover(self) -> int:
        """应用启动时调用：遗留的排队中 / 运行中的任务标为失败。返回条数。"""
        count = await self._store.fail_unfinished_tasks(RESTART_REASON, now=self._now())
        if count:
            logger.warning(f"上次运行遗留了 {count} 个没做完的后台任务，已标为失败")
        return count

    async def close(self) -> None:
        """应用关闭：停掉还在跑的任务，标为失败（原因写明是服务停止）。"""
        self._closing = True
        if self._admissions:
            await asyncio.gather(*self._admissions, return_exceptions=True)
        records = [await self._store.get_task(tid) for tid in list(self._running)]
        tasks = list(self._running.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for record in records:
            if record is None:
                continue
            current = await self._store.get_task(record.id)
            if current is not None and not current.finished:
                await self._finish(record, "failed", error=SHUTDOWN_REASON)
            self._running.pop(record.id, None)
            done = self._done.get(record.id)
            if done is not None:
                done.set()

    # ------------------------------------------------------------------ #
    # 接口（interfaces.md §8.1）
    # ------------------------------------------------------------------ #

    async def submit(
        self,
        *,
        session_id: str,
        goal: str,
        requested_by: int = 0,
        requested_t: float = 0.0,
        transcript_window: tuple[float, float] = (0.0, 0.0),
        frame_ids: list[int] | None = None,
        modality: str = "voice",
    ) -> TaskRecord:
        """建任务（排队中）、落库、通知页面、安排执行。立刻返回，不等任务做完。"""

        async def admit() -> TaskRecord:
            async with self._store.session_work(session_id):
                task = await self._store.create_task(
                    session_id,
                    goal=goal,
                    requested_by=requested_by,
                    requested_t=requested_t,
                    t_from=transcript_window[0],
                    t_to=transcript_window[1],
                    frame_ids=frame_ids or [],
                    modality=modality,
                    now=self._now(),
                )
                self._done[task.id] = asyncio.Event()
                await self._announce(task)
                self._running[task.id] = asyncio.create_task(
                    self._run(task), name=f"task-{task.label}"
                )
                return task

        if self._closing:
            raise RunnerError("服务正在停止")
        admission = asyncio.create_task(admit(), name="task-admission")
        self._admissions.add(admission)
        admission.add_done_callback(self._admissions.discard)
        return await drain_io(admission)

    async def wait(self, task_id: str) -> TaskResult:
        """等任务结束并返回结果；失败或取消时抛 ``TaskFailed``。任务不存在抛 ``LookupError``。"""
        done = self._done.get(task_id)
        if done is not None:
            await done.wait()
        task = await self._store.get_task(task_id)
        if task is None:
            raise LookupError("没有这个任务")
        if task.status == "succeeded":
            return TaskResult(
                brief=task.brief or "",
                detail_md=task.detail_md or "",
                sources=task.sources,
                artifacts=task.artifacts,
            )
        if task.status in ("failed", "cancelled"):
            raise TaskFailed(task.status, task.error or CANCELLED_TEXT)
        raise TaskFailed("failed", RESTART_REASON)  # 库里还没结束、内存里又没有它：执行已经丢了

    async def cancel(self, task_id: str) -> TaskRecord | None:
        """取消任务：中断运行器，状态置为 ``cancelled``。已经结束的原样返回；不存在返回 ``None``。"""
        return await drain_io(self._cancel(task_id))

    async def _cancel(self, task_id: str) -> TaskRecord | None:
        task = await self._store.get_task(task_id)
        if task is None or task.finished:
            return task
        runner_task = self._running.get(task_id)
        if runner_task is None:  # 库里没结束、内存里没有：直接改状态
            return await self._finish(task, "cancelled", error=CANCELLED_TEXT)
        self._cancel_requested.add(task_id)
        runner_task.cancel()
        await asyncio.gather(runner_task, return_exceptions=True)
        current = await self._store.get_task(task_id)
        if current is not None and not current.finished:
            current = await self._finish(task, "cancelled", error=CANCELLED_TEXT)
        self._running.pop(task_id, None)
        self._cancel_requested.discard(task_id)
        done = self._done.get(task_id)
        if done is not None:
            done.set()
        return current

    async def status(self, session_id: str, label: str | None = None) -> dict[str, Any] | None:
        """给实时模型回答「做到哪了」用。``label`` 是短编号，不给就取这场会议最近的一个；没有任务返回 ``None``。"""
        task = await self._store.find_task(session_id, label)
        if task is None:
            return None
        events = await self._store.list_task_events(task.id, last=RECENT_STEPS)
        result: dict[str, Any] = {
            "task_id": task.label,
            "status": task.status,
            "goal": task.goal,
            "recent_steps": [e.summary for e in events],
        }
        if task.status == "succeeded" and task.brief:
            result["brief"] = task.brief
        if task.status in ("failed", "cancelled") and task.error:
            result["reason"] = task.error
        return result

    async def add_event(
        self, task_id: str, kind: str, summary: str, payload: dict | None = None
    ) -> None:
        """记一条进度：落库 + 推 ``task_event`` 消息。出错只记日志，不影响任务本身。"""
        try:
            event = await self._store.add_task_event(
                task_id, kind, summary, payload, now=self._now()
            )
            session_id = task_id.rsplit(".", 1)[0]
            await self._notify(
                session_id,
                {
                    "type": "task_event",
                    "task_id": task_id,
                    "at": event.at,
                    "kind": kind,
                    "summary": summary,
                },
            )
        except Exception:
            logger.exception("记录任务进度失败")

    async def mark_announced(self, task_id: str) -> None:
        await self._store.update_task(task_id, announced=True)

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #

    async def _run(self, task: TaskRecord) -> None:
        async with self._store.session_work(task.session_id):
            try:
                try:
                    async with self._slots:  # 超出并发上限的在这里排队
                        await self._execute(task)
                except asyncio.CancelledError:
                    # 还在排队时就被取消了（或应用正在关闭）
                    await self._on_cancelled(task)
            finally:
                self._running.pop(task.id, None)
                self._cancel_requested.discard(task.id)
                done = self._done.get(task.id)
                if done is not None:
                    done.set()

    async def _execute(self, task: TaskRecord) -> None:
        updated = await self._store.update_task(task.id, status="running", started_at=self._now())
        await self._announce(updated or task)
        await self.add_event(task.id, "status", "开始处理")

        async def on_event(kind: str, summary: str, payload: dict | None = None) -> None:
            await self.add_event(task.id, kind, summary, payload)

        try:
            async with asyncio.timeout(self._timeout):
                result = await self._runner(task, on_event)
        except TimeoutError:
            minutes = max(1, round(self._timeout / 60))
            await self._finish(task, "failed", error=f"超时（超过 {minutes} 分钟没有做完）")
        except asyncio.CancelledError:
            await self._on_cancelled(task)
        except RunnerError as e:
            await self._finish(task, "failed", error=str(e) or "任务执行失败")
        except Exception as e:
            logger.exception(f"任务 {task.label} 执行出错")
            await self._finish(task, "failed", error=f"内部错误（{type(e).__name__}）")
        else:
            await self._finish(task, "succeeded", result=result)

    async def _on_cancelled(self, task: TaskRecord) -> None:
        if task.id in self._cancel_requested:
            await self._finish(task, "cancelled", error=CANCELLED_TEXT)
        else:  # 不是用户取消的：应用在关闭
            await self._finish(task, "failed", error=SHUTDOWN_REASON)

    async def _finish(
        self,
        task: TaskRecord,
        status: str,
        *,
        result: TaskResult | None = None,
        error: str | None = None,
    ) -> TaskRecord | None:
        """写下结束状态、通知页面、记最后一条进度。收尾不能被再次取消打断，所以包在 shield 里。"""

        async def write() -> TaskRecord | None:
            async with self._store.session_work(task.session_id):
                fields: dict[str, Any] = {"status": status, "finished_at": self._now()}
                if result is not None:
                    fields.update(
                        brief=result.brief,
                        detail_md=result.detail_md,
                        sources=result.sources,
                        artifacts=result.artifacts,
                    )
                if error is not None:
                    fields["error"] = error
                updated = await self._store.update_task(task.id, **fields)
                if updated is not None:
                    await self._announce(updated)
                summary = {
                    "succeeded": "已完成",
                    "cancelled": CANCELLED_TEXT,
                }.get(status, f"失败：{error}")
                await self.add_event(task.id, "status", summary)
                return updated

        try:
            return await drain_io(write())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"写任务 {task.label} 的结束状态失败")
            return None

    async def _announce(self, task: TaskRecord) -> None:
        try:
            await self._notify(task.session_id, task_message(task))
        except Exception:
            logger.exception("向页面推送任务状态失败")
