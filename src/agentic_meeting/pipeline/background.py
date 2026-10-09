"""后台模型：画面摘要和滚动纪要共用的那个模型入口（docs/architecture.md §6、docs/pipecat-notes.md §5）。

实时应答优先。后台请求（画面摘要、滚动纪要）要给它让路：llama.cpp 部署方式下是让出 GPU，
通用接口方式下是不和实时应答抢同一个接口的并发额度。所以这里统一做三件事：

* **串行**：同一时刻只有一个后台请求（后台只有一个槽位）。
* **可暂停**：``pause()`` 之后新的请求排队等着，``resume()`` 之后才发。
* **可抢占**：``pause()`` 同时取消在途的请求；发起它的调用方收到 ``Preempted``，自己决定稍后重做还是放弃。

谁来暂停：``AssistantActivity``（``pipeline/activity.py``）在助理被叫到名字、收到文字请求、生成、朗读时判为忙，
忙就暂停，闲下来再恢复。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Protocol

from pipecat.processors.aggregators.llm_context import LLMContext


class Preempted(Exception):
    """这次后台请求被实时应答抢占、取消了。稍后可以重做。"""


class InferenceLLM(Protocol):
    """用到的模型服务接口（``RealtimeLLMService`` 满足它；测试里换成假的）。"""

    async def run_inference(
        self,
        context: LLMContext,
        max_tokens: int | None = None,
        system_instruction: str | None = None,
    ) -> str | None: ...


class BackgroundModel:
    def __init__(self, llm: InferenceLLM) -> None:
        self._llm = llm
        self._lock = asyncio.Lock()
        self._resumed = asyncio.Event()
        self._resumed.set()
        self._inflight: asyncio.Task | None = None
        self._preempting = False

    @property
    def paused(self) -> bool:
        return not self._resumed.is_set()

    def pause(self) -> None:
        """暂停，并取消在途的请求（它的调用方会收到 ``Preempted``）。重复调用无害。"""
        self._resumed.clear()
        task = self._inflight
        if task is not None and not task.done():
            self._preempting = True
            task.cancel()

    def resume(self) -> None:
        self._resumed.set()

    async def wait_resumed(self) -> None:
        """等到不在暂停中。调用方可以先等它，再决定这次要做什么（比如挑最新的一张截图）。"""
        await self._resumed.wait()

    async def run(
        self, messages: Sequence[dict[str, Any]], *, system: str = "", max_tokens: int
    ) -> str:
        """发一次不经过管线的请求，返回模型的文字（去掉首尾空白，可能为空串）。

        暂停期间在这里等。被 ``pause()`` 取消时抛 ``Preempted``；模型服务出错时原样抛出它的异常。
        """
        async with self._lock:
            await self._resumed.wait()
            context = LLMContext(list(messages))  # type: ignore[arg-type]
            self._preempting = False
            task = asyncio.ensure_future(
                self._llm.run_inference(
                    context, max_tokens=max_tokens, system_instruction=system or None
                )
            )
            self._inflight = task
            try:
                text = await task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                cancelled_from_outside = current is not None and current.cancelling() > 0
                if self._preempting and not cancelled_from_outside:
                    raise Preempted() from None
                task.cancel()  # 是调用方自己被取消了：把请求也停掉
                raise
            finally:
                self._inflight = None
                self._preempting = False
        return (text or "").strip()
