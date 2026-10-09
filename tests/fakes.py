"""测试用的假实现，供多个测试文件共用。"""

from __future__ import annotations

import asyncio

from agentic_meeting.asr.base import ASRBackendError

_CLOSED = object()


class FakeASR:
    """实现 ``StreamingASR``。

    ``on_push[n]`` 是第 n 次 ``push_audio``（从 1 数）时放进队列的增量，``on_flush[i]`` 是第 i 次
    ``flush`` 时放进去的；元素可以是 ``ASRDelta`` 或要从 ``deltas()`` 抛出的异常。
    """

    def __init__(self, *, start_failures: int = 0):
        self.calls: list[tuple] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.on_push: dict[int, list] = {}
        self.on_flush: list[list] = []
        self.start_failures = start_failures
        self.starts = 0
        self._pushes = 0
        self._flushes = 0

    async def start(self) -> None:
        self.starts += 1
        self.calls.append(("start",))
        if self.start_failures:
            self.start_failures -= 1
            raise ASRBackendError("假后端：启动失败")

    async def push_audio(self, pcm16: bytes, audio_end_secs: float) -> None:
        self._pushes += 1
        self.calls.append(("push", pcm16, audio_end_secs))
        for item in self.on_push.get(self._pushes, []):
            self.queue.put_nowait(item)

    async def flush(self) -> None:
        self.calls.append(("flush",))
        index, self._flushes = self._flushes, self._flushes + 1
        if index < len(self.on_flush):
            for item in self.on_flush[index]:
                self.queue.put_nowait(item)

    async def deltas(self):
        while True:
            item = await self.queue.get()
            if item is _CLOSED:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    async def close(self) -> None:
        self.calls.append(("close",))
        self.queue.put_nowait(_CLOSED)

    @property
    def pushes(self) -> list[tuple[bytes, float]]:
        return [(c[1], c[2]) for c in self.calls if c[0] == "push"]

    def count(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)
