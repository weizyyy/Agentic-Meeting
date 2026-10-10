"""取消请求后等待实际工作收尾，保护数据库与文件所有权。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable


async def drain_io[T](work: Awaitable[T]) -> T:
    """外层取消也等实际工作结束，防止线程在删除成功后重建目录。"""
    task = asyncio.ensure_future(work)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
            continue
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
        if cancelled:
            raise asyncio.CancelledError
        return result
