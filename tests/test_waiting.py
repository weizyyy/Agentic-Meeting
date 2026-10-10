"""条件等待助手：成功、超时、异常与取消边界。"""

from __future__ import annotations

import asyncio

import pytest
from waiting import wait_until


async def test_wait_until_returns_immediate_value():
    assert await wait_until(lambda: "完成") == "完成"


async def test_wait_until_waits_for_async_condition():
    calls = 0

    async def ready():
        nonlocal calls
        calls += 1
        return "完成" if calls == 2 else None

    assert await wait_until(ready) == "完成" and calls == 2


async def test_wait_until_reports_condition_on_timeout():
    with pytest.raises(AssertionError, match="虚构条件"):
        await wait_until(lambda: False, timeout_secs=0.01, description="虚构条件")


@pytest.mark.parametrize("error", [RuntimeError("数据库故障"), TimeoutError("条件自身超时")])
async def test_wait_until_propagates_predicate_errors(error):
    async def broken():
        raise error

    with pytest.raises(type(error)) as caught:
        await wait_until(broken)
    assert caught.value is error


@pytest.mark.parametrize("cancel", [False, True])
async def test_wait_until_bounds_a_hanging_predicate_and_propagates_cancellation(cancel):
    started, stopped = asyncio.Event(), asyncio.Event()

    async def hanging():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    task = asyncio.create_task(wait_until(hanging, timeout_secs=0.02, description="挂起条件"))
    try:
        await asyncio.wait_for(started.wait(), 1.0)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else AssertionError):
            await asyncio.wait_for(task, 1.0)
        assert stopped.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
