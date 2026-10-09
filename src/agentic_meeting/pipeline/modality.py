"""应答模态：什么模态输入，就用什么模态回答（docs/architecture.md §5.5、docs/interfaces.md §6.3）。

实时模型服务把 ``LLMConfigureOutputFrame(skip_tts=…)`` 记在自己身上，之后产出的文字帧都带着这个标记，
语音合成服务对带标记的帧直接放行、不合成。问题是「之后」有多长：一次请求可能不止生成一次——模型先发起工具调用，
工具结果回来后再生成一次才是真正的回答。所以不能在请求后面紧跟一个「恢复朗读」的帧（那样工具之后的回答就被念出来了），
而是在**每一次新请求进入模型之前**设定这一次的模态：

* 文字入口在请求前面放一个 ``TextRequestFrame`` 作记号；
* ``ModalityGate`` 放在用户侧聚合器和实时模型之间。每当一个新请求（向下游走的 ``LLMContextFrame``）经过，
  它先推一个 ``LLMConfigureOutputFrame``：前面有记号就是 ``skip_tts=True``（只出文字），没有就是 ``False``（照常朗读）。

工具结果回来后的再次生成，走的是助理侧聚合器**向上游**推的上下文帧，不经过这里，于是沿用这次请求的模态。

已知的边界：一个文字请求的工具还没执行完时又来了一个语音请求（或反过来），后到的请求会改掉模态，
先到的那个请求在工具之后的回答就跟着变了。工具执行通常只有几十到几百毫秒，文字入口在助理忙时也会排队，实际很难碰到。
"""

from __future__ import annotations

from dataclasses import dataclass

from pipecat.frames.frames import DataFrame, Frame, LLMConfigureOutputFrame, LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


@dataclass
class TextRequestFrame(DataFrame):
    """记号：紧跟在后面交给模型的那个请求是文字请求，只用文字回答。"""


class ModalityGate(FrameProcessor):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._text_next = False
        self._text_only = False

    @property
    def modality(self) -> str:
        """最近一次进入模型的请求的模态：``"text"`` 或 ``"voice"``。委托任务时记下它，任务完成后按它播报。"""
        return "text" if self._text_only else "voice"

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, TextRequestFrame):
                self._text_next = True
                return  # 记号到此为止，不往下传
            if isinstance(frame, LLMContextFrame):
                self._text_only, self._text_next = self._text_next, False
                await self.push_frame(LLMConfigureOutputFrame(skip_tts=self._text_only))
        await self.push_frame(frame, direction)
