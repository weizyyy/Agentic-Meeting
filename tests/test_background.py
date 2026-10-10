"""后台模型的入口（串行、可暂停、可抢占）和助理忙闲状态。"""

from __future__ import annotations

import asyncio

import pytest
from waiting import wait_until

from agentic_meeting.pipeline.activity import AssistantActivity
from agentic_meeting.pipeline.background import BackgroundModel, Preempted


class FakeLLM:
    """假的模型服务：记录调用；可以卡住（等放行）、报错。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.release = asyncio.Event()
        self.release.set()
        self.started = asyncio.Event()
        self.error: Exception | None = None
        self.reply: str | None = "  一段摘要\n"
        self.active = 0
        self.max_active = 0
        self.cancelled = 0

    async def run_inference(self, context, max_tokens=None, system_instruction=None):
        self.calls.append(
            {
                "messages": list(context.get_messages()),
                "max_tokens": max_tokens,
                "system": system_instruction,
            }
        )
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            await self.release.wait()
            if self.error is not None:
                raise self.error
            return self.reply
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1


async def settle(times=5):
    for _ in range(times):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------- #
# BackgroundModel
# --------------------------------------------------------------------------- #


async def test_run_passes_messages_system_and_max_tokens():
    llm = FakeLLM()
    model = BackgroundModel(llm)
    messages = [{"role": "user", "content": "看图"}]
    assert await model.run(messages, system="你是摘要员", max_tokens=77) == "一段摘要"
    assert llm.calls == [{"messages": messages, "max_tokens": 77, "system": "你是摘要员"}]
    # 没有系统提示词时传 None，不传空串
    await model.run(messages, max_tokens=5)
    assert llm.calls[1]["system"] is None
    llm.reply = None
    assert await model.run(messages, max_tokens=5) == ""


async def test_requests_run_one_at_a_time():
    llm = FakeLLM()
    llm.release.clear()
    model = BackgroundModel(llm)
    tasks = [asyncio.create_task(model.run([], max_tokens=1)) for _ in range(3)]
    await settle()
    assert len(llm.calls) == 1  # 另外两个在排队
    llm.release.set()
    await asyncio.gather(*tasks)
    assert len(llm.calls) == 3 and llm.max_active == 1


async def test_pause_holds_new_requests_until_resume():
    llm = FakeLLM()
    model = BackgroundModel(llm)
    model.pause()
    model.pause()  # 重复暂停无害
    assert model.paused
    task = asyncio.create_task(model.run([], max_tokens=1))
    waiter = asyncio.create_task(model.wait_resumed())
    await settle()
    assert llm.calls == [] and not task.done() and not waiter.done()
    model.resume()
    assert await task == "一段摘要"
    await waiter
    assert not model.paused


async def test_pause_cancels_inflight_request_and_caller_sees_preempted():
    llm = FakeLLM()
    llm.release.clear()
    model = BackgroundModel(llm)
    task = asyncio.create_task(model.run([], max_tokens=1))
    await llm.started.wait()
    model.pause()
    with pytest.raises(Preempted):
        await task
    assert llm.cancelled == 1 and llm.active == 0
    # 恢复后可以重做，而且不会把上一次的「被抢占」记到这一次头上
    model.resume()
    llm.release.set()
    assert await model.run([], max_tokens=1) == "一段摘要"


async def test_cancelling_the_caller_is_not_reported_as_preempted():
    llm = FakeLLM()
    llm.release.clear()
    model = BackgroundModel(llm)
    task = asyncio.create_task(model.run([], max_tokens=1))
    await llm.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert llm.cancelled == 1  # 请求也停掉了
    # 锁已经放开，后面的请求不受影响
    llm.release.set()
    assert await model.run([], max_tokens=1) == "一段摘要"


async def test_caller_cancelled_while_being_preempted_still_gets_cancelled():
    llm = FakeLLM()
    llm.release.clear()
    model = BackgroundModel(llm)
    task = asyncio.create_task(model.run([], max_tokens=1))
    await llm.started.wait()
    model.pause()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_model_errors_propagate_and_do_not_block_later_requests():
    llm = FakeLLM()
    llm.error = RuntimeError("503")
    model = BackgroundModel(llm)
    with pytest.raises(RuntimeError):
        await model.run([], max_tokens=1)
    llm.error = None
    assert await model.run([], max_tokens=1) == "一段摘要"


# --------------------------------------------------------------------------- #
# AssistantActivity
# --------------------------------------------------------------------------- #


def tracked(**kw):
    kw.setdefault("idle_delay_secs", 0)
    activity = AssistantActivity(**kw)
    seen: list[bool] = []
    activity.subscribe(seen.append)
    return activity, seen


async def test_voice_round_is_busy_from_wake_to_end_of_speech():
    activity, seen = tracked()
    assert not activity.busy
    await activity.wake_detected()
    assert seen == [True] and activity.busy
    await activity.set_generating(True)
    await activity.set_speaking(True)
    await activity.set_generating(False)  # 生成完了，还在朗读
    assert seen == [True]
    await activity.set_speaking(False)
    assert seen == [True, False] and not activity.busy


async def test_text_request_is_busy_until_the_answer_is_generated():
    activity, seen = tracked()
    await activity.request_sent()
    await activity.set_generating(True)
    await activity.set_generating(False)
    assert seen == [True, False]


async def test_wake_without_a_reply_ends_with_wake_window_or_watchdog():
    activity, seen = tracked()
    await activity.wake_detected()
    await activity.wake_expired()
    assert seen == [True, False]

    activity, seen = tracked(awaiting_timeout_secs=0.03)
    await activity.wake_detected()
    try:
        await wait_until(lambda: seen == [True, False], description="助理看门狗解除忙状态")
        assert seen == [True, False]
    finally:
        await activity.close()


async def test_watchdog_does_not_fire_once_generation_started():
    activity, seen = tracked(awaiting_timeout_secs=0.03)
    await activity.wake_detected()
    await activity.set_generating(True)
    await asyncio.sleep(0.1)
    assert seen == [True]  # 还在生成，不能被看门狗判成闲
    await activity.wake_expired()  # 唤醒窗口过期也不影响正在进行的生成
    assert seen == [True]
    await activity.set_generating(False)
    assert seen == [True, False]


async def test_short_gap_between_generation_and_speech_does_not_flap():
    activity, seen = tracked(idle_delay_secs=0.05)
    await activity.set_generating(True)
    await activity.set_generating(False)
    await asyncio.sleep(0.01)
    await activity.set_speaking(True)  # 空隙里又忙了
    await asyncio.sleep(0.1)
    assert seen == [True] and activity.busy
    await activity.set_speaking(False)
    assert activity.busy  # 还没过延迟
    try:
        await wait_until(lambda: seen == [True, False], description="助理看门狗解除忙状态")
        assert seen == [True, False]
    finally:
        await activity.close()


async def test_listener_errors_are_contained_and_async_listeners_are_awaited():
    activity = AssistantActivity(idle_delay_secs=0)
    seen: list[bool] = []

    def broken(_busy):
        raise RuntimeError("boom")

    async def slow(busy):
        await asyncio.sleep(0)
        seen.append(busy)

    activity.subscribe(broken)
    activity.subscribe(slow)
    await activity.wake_detected()
    assert seen == [True]


async def test_close_reports_idle_and_ignores_later_events():
    activity, seen = tracked(idle_delay_secs=5)
    await activity.set_generating(True)
    await activity.close()
    assert seen == [True, False] and not activity.busy
    await activity.wake_detected()
    await activity.set_speaking(True)
    assert seen == [True, False]

    quiet, seen2 = tracked()
    await quiet.close()
    assert seen2 == []  # 本来就闲着，不多发一次


async def test_activity_drives_background_model():
    llm = FakeLLM()
    llm.release.clear()
    model = BackgroundModel(llm)
    activity = AssistantActivity(idle_delay_secs=0)
    activity.subscribe(lambda busy: model.pause() if busy else model.resume())
    task = asyncio.create_task(model.run([], max_tokens=1))
    await llm.started.wait()
    await activity.wake_detected()
    with pytest.raises(Preempted):
        await task
    assert model.paused
    await activity.set_generating(True)
    await activity.set_generating(False)
    assert not model.paused
