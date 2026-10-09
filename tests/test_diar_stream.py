"""一场会议的说话人区分流（diar/stream.py）：流内时间与会话时间的换算、编号偏移、合并后的别名。

换算是纯函数，用手写的数据测；``SessionDiarizer`` 配一个假的底层流。
"""

from __future__ import annotations

import pytest

from agentic_meeting.diar.stream import (
    Anchor,
    SessionDiarizer,
    map_segments,
    resolve_alias,
    session_to_stream,
)
from agentic_meeting.types import SpeakerSegment as Seg

SECOND = b"\x00" * 32000  # 一秒 16 kHz 单声道 s16le

# 三次连接：会话 0–60 秒（流 0–60）、断了 10 分钟、会话 660–700 秒（流 60–100）、又断了、会话 1000 秒起（流 100 起）
ANCHORS = [Anchor(0.0, 0.0), Anchor(60.0, 660.0), Anchor(100.0, 1000.0)]


class FakeInner:
    max_speakers = 4

    def __init__(self):
        self.pushed: list[bytes] = []
        self.found: list[Seg] = []
        self.asked: list[float] = []
        self.closed = 0
        self.fail = False

    async def start(self):
        raise AssertionError("底层的流交过来之前已经启动，不该再启动一次")

    async def push_audio(self, pcm16):
        if self.fail:
            raise RuntimeError("推理出错")
        self.pushed.append(pcm16)

    async def segments(self, since_secs=0.0):
        self.asked.append(since_secs)
        return [s for s in self.found if s.end_secs > since_secs]

    async def close(self):
        self.closed += 1


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("session_secs", "stream_secs"),
    [
        (0.0, 0.0),
        (30.0, 30.0),
        (60.0, 60.0),
        (300.0, 60.0),  # 落在第一段空档里：对应空档之后那次连接的起点
        (660.0, 60.0),
        (680.0, 80.0),
        (700.0, 100.0),
        (900.0, 100.0),  # 第二段空档
        (1000.0, 100.0),
        (1012.5, 112.5),
    ],
)
def test_session_time_maps_into_the_stream(session_secs, stream_secs):
    assert session_to_stream(ANCHORS, session_secs) == stream_secs


def test_session_time_before_the_first_connection_and_without_anchors():
    later = [Anchor(0.0, 500.0)]  # 服务重启后新开的流：第一次连接就从会话 500 秒开始
    assert session_to_stream(later, 100.0) == 0.0
    assert session_to_stream(later, 530.0) == 30.0
    assert session_to_stream([], 12.0) == 12.0


def test_segments_are_shifted_into_session_time_per_connection():
    mapped = map_segments(ANCHORS, [Seg(10.0, 20.0, 1), Seg(70.0, 75.0, 2), Seg(101.0, 104.0, 1)])
    assert mapped == [Seg(10.0, 20.0, 1), Seg(670.0, 675.0, 2), Seg(1001.0, 1004.0, 1)]


def test_a_segment_across_a_reconnect_is_cut_at_the_gap():
    # 流里 55–65 秒是「同一个人一直在说」，其实中间隔着 10 分钟的断线
    mapped = map_segments(ANCHORS, [Seg(55.0, 65.0, 1)])
    assert mapped == [Seg(55.0, 60.0, 1), Seg(660.0, 665.0, 1)]
    # 跨两个接续点的也一样
    mapped = map_segments(ANCHORS, [Seg(50.0, 110.0, 3)])
    assert mapped == [Seg(50.0, 60.0, 3), Seg(660.0, 700.0, 3), Seg(1000.0, 1010.0, 3)]


def test_offset_then_alias_and_results_are_sorted():
    mapped = map_segments(
        [Anchor(0.0, 500.0)],
        [Seg(5.0, 6.0, 2), Seg(1.0, 2.0, 1)],
        speaker_offset=3,
        alias={5: 2},
    )
    assert mapped == [Seg(501.0, 502.0, 4), Seg(505.0, 506.0, 2)]
    assert map_segments([], [Seg(1.0, 2.0, 1)]) == [Seg(1.0, 2.0, 1)]


def test_alias_chains_and_loops():
    assert resolve_alias(3, {3: 2, 2: 1}) == 1
    assert resolve_alias(4, {3: 2}) == 4
    assert resolve_alias(1, None) == 1
    assert resolve_alias(1, {1: 2, 2: 1}) in (1, 2)  # 成环也不会死循环


# --------------------------------------------------------------------------- #
# SessionDiarizer
# --------------------------------------------------------------------------- #


async def test_each_connection_continues_at_the_end_of_the_stream():
    inner = FakeInner()
    stream = SessionDiarizer(inner, session_id="s1")
    stream.begin_connection(0.0)
    await stream.start()  # 不会再启动底层
    for _ in range(3):
        await stream.push_audio(SECOND)
    await stream.close()  # 连接结束：不关底层
    assert inner.closed == 0 and stream.fed_secs == 3.0

    stream.begin_connection(600.0)  # 十分钟后继续，空档不补喂静音
    await stream.push_audio(SECOND)
    assert stream.anchors == [Anchor(0.0, 0.0), Anchor(3.0, 600.0)]
    assert len(inner.pushed) == 4

    inner.found = [Seg(1.0, 2.0, 1), Seg(3.2, 3.9, 2)]
    assert await stream.segments(0.0) == [Seg(1.0, 2.0, 1), Seg(600.2, 600.9, 2)]
    # 只要最近的：会话 590 秒落在空档里，问底层的是第二次连接在流里的起点
    assert await stream.segments(590.0) == [Seg(600.2, 600.9, 2)]
    assert inner.asked[-1] == 3.0
    assert await stream.segments(600.5) == [Seg(600.2, 600.9, 2)]
    assert inner.asked[-1] == 3.5

    await stream.shutdown()
    assert inner.closed == 1


async def test_a_connection_that_fed_nothing_leaves_no_anchor_behind():
    stream = SessionDiarizer(FakeInner(), session_id="s1")
    stream.begin_connection(0.0)
    stream.begin_connection(50.0)  # 上一次连接没来得及送音频就断了
    assert stream.anchors == [Anchor(0.0, 50.0)]


async def test_offset_and_merge_apply_to_what_the_stream_outputs():
    inner = FakeInner()
    stream = SessionDiarizer(inner, session_id="s1", speaker_offset=2)
    stream.begin_connection(100.0)
    inner.found = [Seg(0.0, 1.0, 1), Seg(1.0, 2.0, 2)]
    assert [s.speaker for s in await stream.segments()] == [3, 4]
    stream.merge(4, 1)  # 重启后被认成新人的 4 其实是原来的 1
    assert [s.speaker for s in await stream.segments()] == [3, 1]
    assert stream.max_speakers == 4


async def test_an_error_in_the_stream_marks_it_failed():
    inner = FakeInner()
    stream = SessionDiarizer(inner, session_id="s1")
    stream.begin_connection(0.0)
    inner.fail = True
    with pytest.raises(RuntimeError):
        await stream.push_audio(SECOND)
    assert stream.failed  # 会话管理器据此在下次连接时重开一条
