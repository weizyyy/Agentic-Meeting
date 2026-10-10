"""有界的条件等待：条件满足就返回它的值，超时则让测试失败。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any


async def until(
    predicate: Callable[[], Awaitable[Any]], description: str, timeout_secs: float = 30
) -> Any:
    deadline = time.monotonic() + timeout_secs
    while True:
        value = await predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"{description}：{timeout_secs} 秒内没有等到")
        await asyncio.sleep(0.1)


async def until_sync(
    predicate: Callable[[], Any], description: str, timeout_secs: float = 30
) -> Any:
    async def wrapped() -> Any:
        return predicate()

    return await until(wrapped, description, timeout_secs)
