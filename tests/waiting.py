"""异步测试的有界条件等待。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable


async def wait_until[T](
    predicate: Callable[[], T | Awaitable[T]],
    *,
    timeout_secs: float = 2.0,
    description: str = "等待条件",
) -> T:
    """返回条件满足时的值；异常及取消原样传播，连异步条件也受总超时限制。"""
    deadline = asyncio.timeout(timeout_secs)
    try:
        async with deadline:
            while True:
                value = predicate()
                if inspect.isawaitable(value):
                    value = await value
                if value:
                    return value
                await asyncio.sleep(0.005)
    except TimeoutError:
        if not deadline.expired():
            raise
        raise AssertionError(f"{description}：{timeout_secs} 秒内未满足") from None
