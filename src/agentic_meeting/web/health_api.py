"""匿名健康、就绪和指标接口；按需快照归当前应用拥有（interfaces.md §5.8）。"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from agentic_meeting.config import AppConfig
from agentic_meeting.pipeline.services import caption_provider
from agentic_meeting.services.supervisor import ProbeTarget, build_probe_targets, probe_service
from agentic_meeting.store.db import Store

if TYPE_CHECKING:
    from agentic_meeting.pipeline.bot import AppResources

SERVICE_TTL_SECS = 5.0
SERVICE_BUDGET_SECS = 2.0
STORAGE_BUDGET_SECS = 0.5
REQUEST_BUDGET_SECS = 2.5
TASK_TTL_SECS = 5.0
TASK_BUDGET_SECS = 0.5
TASK_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")

ServiceStatus = dict[str, bool | str]
Services = dict[str, ServiceStatus]
StorageStatus = dict[str, str]


def _count(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("指标计数无效")
    return value


def _seconds(value: Any) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("指标时间无效")
    return value


class HealthState:
    """只合并在途采集；服务缓存及单调时钟属于当前应用。"""

    def __init__(self, cfg: AppConfig, *, clock: Callable[[], float] = time.monotonic):
        self.lifecycle = "starting"
        self.targets = build_probe_targets(cfg)
        self.clock = clock
        self.store: Store | None = None
        self._services: Services | None = None
        self._sampled_at: float | None = None
        self._pending_services: Services | None = None
        self._service_task: asyncio.Task[Services] | None = None
        self._storage_task: asyncio.Task[StorageStatus] | None = None
        self._counts_task: asyncio.Task[None] | None = None
        self._counts: dict[str, int] | None = None
        self._counts_sampled_at: float | None = None
        self._counts_generation = 0
        self.screen_caption_enabled = caption_provider(cfg) is not None

    def start(self, store: Store, *, screen_caption_enabled: bool | None = None) -> None:
        self.store = store
        self._services = None
        self._sampled_at = None
        self._pending_services = None
        self._invalidate_task_counts()
        if screen_caption_enabled is not None:
            self.screen_caption_enabled = screen_caption_enabled
        self.lifecycle = "running"

    async def close(self) -> None:
        """先停止就绪，再回收采集；调用方随后才能关闭数据库。"""
        self.lifecycle = "stopping"
        self._invalidate_task_counts()
        tasks = [
            task
            for task in (self._service_task, self._storage_task, self._counts_task)
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._service_task = self._storage_task = None
        self._counts_task = None
        self._services = None
        self._sampled_at = None
        self.store = None

    def unknown_services(self, reason: str) -> Services:
        return {
            target.name: {
                "enabled": target.enabled,
                "required": target.required,
                "status": "unknown" if target.enabled else "disabled",
                "reason": reason if target.enabled else "disabled",
            }
            for target in self.targets
        }

    def current_services(self) -> tuple[Services, float | None]:
        """过期缓存不冒充当前观测；已完成的 unknown 可复用但不公开 age。"""
        if self.lifecycle != "running":
            reason = "not_started" if self.lifecycle == "starting" else "shutting_down"
            return self.unknown_services(reason), None
        if self._services is not None and self._sampled_at is not None:
            age = max(0.0, self.clock() - self._sampled_at)
            if age < SERVICE_TTL_SECS:
                incomplete = any(s["status"] == "unknown" for s in self._services.values())
                return self._services, None if incomplete else age
        return self._pending_services or self.unknown_services("budget_exhausted"), None

    async def service_snapshot(self) -> tuple[Services, float | None]:
        """readyz/metrics 共用的入口；请求取消不能取消别人的刷新。"""
        if self.lifecycle != "running":
            return self.current_services()
        fresh = self._sampled_at is not None and self.clock() - self._sampled_at < SERVICE_TTL_SECS
        if not fresh:
            if self._service_task is None or self._service_task.done():
                self._service_task = asyncio.create_task(self._refresh_services())
            try:
                await asyncio.shield(self._service_task)
            except Exception:
                self._services = self.unknown_services("refresh_failed")
                self._sampled_at = self.clock()
        return self.current_services()

    async def _probe(self, target: ProbeTarget, results: Services) -> None:
        try:
            results[target.name] = await probe_service(target, timeout_secs=SERVICE_BUDGET_SECS)
        except Exception:
            results[target.name] = self.unknown_services("refresh_failed")[target.name]

    async def _refresh_services(self) -> Services:
        results = self.unknown_services("budget_exhausted")
        self._pending_services = results
        tasks = [asyncio.create_task(self._probe(target, results)) for target in self.targets]
        try:
            await asyncio.wait(tasks, timeout=SERVICE_BUDGET_SECS)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._pending_services = None
        self._services = results
        self._sampled_at = self.clock()
        return results

    async def storage_status(self) -> StorageStatus:
        if self.lifecycle != "running":
            reason = "not_started" if self.lifecycle == "starting" else "shutting_down"
            return {"status": "unknown", "reason": reason}
        if self._storage_task is None or self._storage_task.done():
            self._storage_task = asyncio.create_task(self._check_storage())
        return await asyncio.shield(self._storage_task)

    async def _check_storage(self) -> StorageStatus:
        try:
            async with asyncio.timeout(STORAGE_BUDGET_SECS):
                assert self.store is not None
                await self.store.check_available()
        except TimeoutError:
            self._invalidate_task_counts()
            return {"status": "unavailable", "reason": "timeout"}
        except Exception:
            self._invalidate_task_counts()
            return {"status": "unavailable", "reason": "storage_error"}
        return {"status": "ok", "reason": "checked"}

    def _invalidate_task_counts(self) -> None:
        # 旧查询即使随后成功，也不能覆盖已经观察到的存储故障。
        self._counts_generation += 1
        self._counts = self._counts_sampled_at = None

    def current_task_counts(self) -> tuple[dict[str, int] | None, float | None]:
        if self.lifecycle == "running" and self._counts is not None:
            assert self._counts_sampled_at is not None
            age = max(0.0, self.clock() - self._counts_sampled_at)
            if math.isfinite(age) and age < TASK_TTL_SECS:
                return self._counts, age
        return None, None

    async def task_counts_snapshot(self) -> None:
        """只缓存成功计数；并发请求共享一次查询，取消不取消共享任务。"""
        if self.lifecycle != "running" or self.current_task_counts()[0] is not None:
            return
        if self._counts_task is None or self._counts_task.done():
            self._counts_task = asyncio.create_task(self._refresh_task_counts())
        try:
            await asyncio.shield(self._counts_task)
        except Exception:
            self._invalidate_task_counts()

    async def _refresh_task_counts(self) -> None:
        generation = self._counts_generation
        self._counts = self._counts_sampled_at = None
        try:
            async with asyncio.timeout(TASK_BUDGET_SECS):
                assert self.store is not None
                raw = await self.store.task_counts()
                counts = {name: _count(raw[name]) for name in TASK_STATUSES}
                sampled_at = _seconds(self.clock())
                assert sampled_at is not None
        except Exception:
            self._invalidate_task_counts()
            return
        if self.lifecycle == "running" and generation == self._counts_generation:
            self._counts, self._counts_sampled_at = counts, sampled_at

    def runtime_metrics(self, resources: AppResources | None) -> tuple[dict[str, Any], bool]:
        """按字段读取公共数字指标；无样本和空闲不算失败，不触碰业务内容。"""
        screen = {
            "enabled": self.screen_caption_enabled,
            "depth": None if self.screen_caption_enabled else 0,
        }
        values = {
            "live_connections": None,
            "caption_lag_seconds": None,
            "caption_sample_age_seconds": None,
            "queues": {"transcript_retry": None, "screen_caption": screen, "agent_tasks": None},
        }
        if self.lifecycle != "running" or resources is None:
            return values, True
        partial = False
        try:
            assert resources.sessions is not None
            live = resources.sessions.live
            active = live is not None and not live.stop_requested and not live.done.is_set()
            registered = active and live.worker is not None and live.recorder is not None
            values["live_connections"] = int(registered)
            if not registered:
                if active and live.worker is not None:
                    partial = True
                else:
                    values["queues"]["transcript_retry"] = 0
        except Exception:
            registered = False
            partial = True
        if registered:
            try:
                lag = _seconds(live.recorder.caption_lag_seconds)
                age = _seconds(live.recorder.caption_sample_age_seconds)
                if (lag is None) != (age is None):
                    raise ValueError("字幕样本不完整")
                values["caption_lag_seconds"], values["caption_sample_age_seconds"] = lag, age
            except Exception:
                partial = True
            try:
                values["queues"]["transcript_retry"] = _count(live.recorder.unsaved_utterance_count)
            except Exception:
                partial = True
        try:
            if resources.captions is not None:
                enabled = resources.captions.enabled
                if type(enabled) is not bool:
                    raise ValueError("画面摘要状态无效")
                screen["enabled"] = enabled
                screen["depth"] = 0 if not enabled else None
                if enabled:
                    depth = _count(resources.captions.pending_count)
                    if depth > 1:
                        raise ValueError("画面摘要槽位无效")
                    screen["depth"] = depth
            elif screen["enabled"]:
                partial = True
        except Exception:
            partial = True
        return values, partial


def register(app: FastAPI, cfg: AppConfig) -> None:
    health = HealthState(cfg)
    app.state.health = health

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        storage: StorageStatus = {"status": "unavailable", "reason": "timeout"}

        async def collect_storage() -> None:
            nonlocal storage
            storage = await health.storage_status()

        try:
            async with asyncio.timeout(REQUEST_BUDGET_SECS):
                (services, age), _ = await asyncio.gather(
                    health.service_snapshot(), collect_storage()
                )
        except TimeoutError:
            services, age = health.current_services()
        except asyncio.CancelledError:
            # 共享采集被 shutdown 回收时返回停止快照；客户端自身取消仍传播。
            task = asyncio.current_task()
            if health.lifecycle != "stopping" or (task is not None and task.cancelling()):
                raise
            services, age = health.current_services()
        if health.lifecycle != "running":
            services, age = health.current_services()
            storage = await health.storage_status()
        status = "ok"
        if (
            health.lifecycle != "running"
            or storage["status"] != "ok"
            or services["asr"]["status"] != "ok"
        ):
            status = "not_ready"
        elif any(s["status"] in {"unavailable", "unknown"} for s in services.values()):
            status = "degraded"
        return JSONResponse(
            {
                "status": status,
                "lifecycle": health.lifecycle,
                "storage": storage,
                "services": services,
                "service_snapshot_age_seconds": age,
            },
            status_code=503 if status == "not_ready" else 200,
        )

    @app.get("/metrics")
    async def metrics() -> JSONResponse:
        try:
            async with asyncio.timeout(REQUEST_BUDGET_SECS):
                await asyncio.gather(health.service_snapshot(), health.task_counts_snapshot())
        except TimeoutError:
            pass
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if health.lifecycle != "stopping" or (task is not None and task.cancelling()):
                raise
        # 等待服务时可能已发生存储故障或停止；组装时重读有效缓存与当前连接。
        services, service_age = health.current_services()
        counts, counts_age = health.current_task_counts()
        values, partial = health.runtime_metrics(getattr(app.state, "resources", None))
        values["queues"]["agent_tasks"] = counts["queued"] if counts is not None else None
        partial = (
            partial or counts is None or any(s["status"] == "unknown" for s in services.values())
        )
        return JSONResponse(
            {
                "status": "partial" if partial else "ok",
                "lifecycle": health.lifecycle,
                **values,
                "task_counts": counts,
                "task_counts_age_seconds": counts_age,
                "services": services,
                "service_snapshot_age_seconds": service_age,
            }
        )
