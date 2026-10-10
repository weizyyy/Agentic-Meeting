"""匿名健康接口与应用独有的按需服务快照（interfaces.md §5.8）。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from agentic_meeting.config import AppConfig
from agentic_meeting.services.supervisor import ProbeTarget, build_probe_targets, probe_service
from agentic_meeting.store.db import Store

SERVICE_TTL_SECS = 5.0
SERVICE_BUDGET_SECS = 2.0
STORAGE_BUDGET_SECS = 0.5
REQUEST_BUDGET_SECS = 2.5

ServiceStatus = dict[str, bool | str]
Services = dict[str, ServiceStatus]
StorageStatus = dict[str, str]


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

    def start(self, store: Store) -> None:
        self.store = store
        self._services = None
        self._sampled_at = None
        self._pending_services = None
        self.lifecycle = "running"

    async def close(self) -> None:
        """先停止就绪，再回收采集；调用方随后才能关闭数据库。"""
        self.lifecycle = "stopping"
        tasks = [task for task in (self._service_task, self._storage_task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._service_task = self._storage_task = None
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
            return {"status": "unavailable", "reason": "timeout"}
        except Exception:
            return {"status": "unavailable", "reason": "storage_error"}
        return {"status": "ok", "reason": "checked"}


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
