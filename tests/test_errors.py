"""管线错误 → 页面提示（pipeline/errors.py）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentic_meeting.pipeline.errors import ErrorNotifier, describe_reason


class FakeStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class APIConnectionError(Exception):
    pass


class APITimeoutError(Exception):
    pass


def frame(processor: object, error: str = "boom", exception: Exception | None = None):
    return SimpleNamespace(processor=processor, error=error, exception=exception)


@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (FakeStatusError(401), "密钥被拒绝"),
        (FakeStatusError(404), "404"),
        (FakeStatusError(429), "限流"),
        (FakeStatusError(503), "服务端出错（503）"),
        (APIConnectionError("refused"), "连不上服务"),
        (APITimeoutError("slow"), "请求超时"),
    ],
)
def test_known_failures_are_described_in_plain_chinese(exception, expected):
    assert expected in describe_reason(frame(object(), exception=exception))


def test_unknown_failures_fall_back_to_the_original_text_truncated():
    assert describe_reason(frame(object(), error="x" * 500)).endswith("…")
    assert describe_reason(frame(object(), error="奇怪的错误")) == "奇怪的错误"


async def test_llm_and_tts_errors_become_separate_notices_and_others_are_ignored():
    llm, tts, other = object(), object(), object()
    sent: list[tuple[str, str]] = []

    async def notice(level: str, text: str) -> None:
        sent.append((level, text))

    notifier = ErrorNotifier(notice, llm=llm, tts=tts)
    await notifier.on_error(frame(llm, exception=APIConnectionError()))
    await notifier.on_error(frame(tts, exception=APIConnectionError()))
    await notifier.on_error(frame(other))
    await notifier.on_error(frame(None))

    assert sent == [
        ("warn", "助理暂不可用：连不上服务"),
        ("warn", "语音不可用，助理这次只出文字：连不上服务"),
    ]


async def test_repeated_errors_of_the_same_kind_are_throttled():
    llm = object()
    now = 0.0
    sent: list[str] = []

    async def notice(_level: str, text: str) -> None:
        sent.append(text)

    notifier = ErrorNotifier(notice, llm=llm, min_interval_secs=30.0, clock=lambda: now)
    await notifier.on_error(frame(llm))
    now = 10.0
    await notifier.on_error(frame(llm))  # 每句话都报一次错，不刷屏
    now = 45.0
    await notifier.on_error(frame(llm))
    assert len(sent) == 2


async def test_a_failing_notice_does_not_raise():
    llm = object()

    async def notice(_level: str, _text: str) -> None:
        raise RuntimeError("数据通道没了")

    await ErrorNotifier(notice, llm=llm).on_error(frame(llm))
