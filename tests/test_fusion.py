"""文字归属与发言切分（diar/fusion.py）。纯逻辑：全部用手写的分段数据，不依赖 Pipecat 和任何模型。

多数用例把识别延迟设为 0，让每个增量的时间区间就是 [上一个增量的终点, 本增量的终点]，
这样「谁在这个区间里说话」一眼看得清；默认的 0.2 秒平移单独测。
"""

from __future__ import annotations

import random

import pytest

from agentic_meeting.diar.fusion import (
    CaptionUpdate,
    TranscriptAssembler,
    UtteranceFinal,
    dominant_speaker,
)
from agentic_meeting.types import SPEAKER_UNKNOWN, ASRDelta, SpeakerSegment


def seg(start: float, end: float, speaker: int) -> SpeakerSegment:
    return SpeakerSegment(start, end, speaker)


def delta(stable: str, end: float, unstable: str = "", *, last: bool = False) -> ASRDelta:
    return ASRDelta(stable, unstable, end, segment_end=last)


def asm(**kwargs) -> TranscriptAssembler:
    return TranscriptAssembler(**{"recognition_latency_secs": 0.0, **kwargs})


def finals(events) -> list[UtteranceFinal]:
    return [e for e in events if isinstance(e, UtteranceFinal)]


def captions(events) -> list[CaptionUpdate]:
    return [e for e in events if isinstance(e, CaptionUpdate)]


def feed(a: TranscriptAssembler, steps, segments) -> list:
    """steps 是 ASRDelta 的列表；返回全部事件。"""
    out = []
    for d in steps:
        out += a.on_delta(d, segments)
    return out


# --------------------------------------------------------------------------- #
# 归属
# --------------------------------------------------------------------------- #


def test_dominant_speaker_is_the_one_with_the_longest_overlap():
    segments = [seg(0, 2, 1), seg(2, 5, 2), seg(5, 6, 1)]
    assert dominant_speaker(segments, 0.0, 3.0) == 1  # 1 号 2 秒，2 号 1 秒
    assert dominant_speaker(segments, 1.0, 5.5) == 2  # 1 号 1 + 0.5，2 号 3
    assert dominant_speaker(segments, 5.2, 6.0) == 1


def test_overlapping_segments_count_each_speakers_own_time():
    segments = [seg(0, 4, 1), seg(1, 3, 2)]  # 多人同时说话
    assert dominant_speaker(segments, 0.0, 4.0) == 1
    assert dominant_speaker(segments, 1.0, 3.0) == 1  # 各 2 秒：平手取编号小的
    assert dominant_speaker(segments, 3.0, 4.0) == 1


def test_no_overlap_means_no_answer():
    assert dominant_speaker([], 0.0, 1.0) is None
    assert dominant_speaker([seg(5, 6, 1)], 0.0, 1.0) is None
    assert dominant_speaker([seg(0, 1, 1)], 1.0, 2.0) is None  # 只是挨着不算重叠


def test_a_zero_length_interval_uses_the_segment_covering_that_instant():
    assert dominant_speaker([seg(0, 2, 3)], 1.0, 1.0) == 3
    assert dominant_speaker([seg(0, 2, 3)], 5.0, 5.0) is None


# --------------------------------------------------------------------------- #
# 一个人说话
# --------------------------------------------------------------------------- #


def test_single_speaker_segment_produces_captions_then_one_final_utterance():
    a = asm()
    segments = [seg(0, 6, 1)]
    a.on_speech_started(0.0)
    events = feed(
        a,
        [delta("今天", 1.0, "我们"), delta("我们", 2.0, "讨论"), delta("讨论数据", 3.0, last=True)],
        segments,
    )
    assert [(c.segment_id, c.speaker_idx, c.stable, c.unstable) for c in captions(events)] == [
        (1, 1, "今天", "我们"),
        (1, 1, "今天我们", "讨论"),
        (1, 1, "今天我们讨论数据", ""),
    ]
    assert all(c.t_start == 0.0 for c in captions(events))
    (final,) = finals(events)
    assert (final.segment_id, final.speaker_idx, final.text) == (1, 1, "今天我们讨论数据")
    assert (final.t_start, final.t_end) == (0.0, 3.0)
    assert events[-1] is final  # 定稿在最后


def test_next_segment_gets_the_next_id_and_its_own_start_time():
    a = asm()
    segments = [seg(0, 20, 1)]
    a.on_speech_started(0.0)
    first = feed(a, [delta("第一段", 2.0, last=True)], segments)
    a.on_speech_started(10.0)
    second = feed(a, [delta("第二段", 12.0, last=True)], segments)
    assert [f.segment_id for f in finals(first)] == [1]
    (f2,) = finals(second)
    assert (f2.segment_id, f2.t_start, f2.t_end) == (2, 10.0, 12.0)


def test_unstable_only_deltas_update_the_caption_but_not_the_text():
    a = asm()
    a.on_speech_started(0.0)
    events = a.on_delta(delta("", 0.5, "你好"), [seg(0, 5, 1)])
    (c,) = captions(events)
    assert (c.stable, c.unstable) == ("", "你好")
    assert finals(events) == []


def test_identical_captions_are_not_repeated():
    a = asm()
    a.on_speech_started(0.0)
    first = a.on_delta(delta("你好", 0.5, "吗"), [seg(0, 5, 1)])
    again = a.on_delta(delta("", 0.8, "吗"), [seg(0, 5, 1)])
    assert len(captions(first)) == 1
    assert captions(again) == []


def test_segment_end_without_any_text_clears_the_caption_instead_of_saving_an_utterance():
    a = asm()
    a.on_speech_started(0.0)
    a.on_delta(delta("", 0.5, "嗯"), [seg(0, 5, 1)])  # 屏幕上显示了灰色的「嗯」
    events = a.on_delta(delta("", 1.0, last=True), [seg(0, 5, 1)])
    assert finals(events) == []
    (clear,) = captions(events)
    assert (clear.segment_id, clear.stable, clear.unstable) == (1, "", "")


def test_segment_end_with_nothing_shown_emits_nothing():
    a = asm()
    a.on_speech_started(0.0)
    assert a.on_delta(delta("", 1.0, last=True), []) == []


def test_final_text_is_stripped():
    a = asm()
    a.on_speech_started(0.0)
    events = a.on_delta(delta("  你好 ", 1.0, last=True), [seg(0, 5, 1)])
    assert finals(events)[0].text == "你好"


# --------------------------------------------------------------------------- #
# 换人切分
# --------------------------------------------------------------------------- #


def test_two_speakers_in_one_segment_are_split_where_the_second_takes_over():
    a = asm()
    segments = [seg(0, 3, 1), seg(3, 6, 2)]
    a.on_speech_started(0.0)
    events = feed(
        a,
        [
            delta("甲1", 1.0),
            delta("甲2", 2.0),
            delta("甲3", 3.0),
            delta("乙1", 4.0),  # 乙已经说了 1 秒 ≥ 0.8，在这里切开
            delta("乙2", 5.0),
            delta("乙3", 6.0, last=True),
        ],
        segments,
    )
    first, second = finals(events)
    assert (first.segment_id, first.speaker_idx, first.text) == (1, 1, "甲1甲2甲3")
    assert (first.t_start, first.t_end) == (0.0, 3.0)
    assert (second.segment_id, second.speaker_idx, second.text) == (2, 2, "乙1乙2乙3")
    assert (second.t_start, second.t_end) == (3.0, 6.0)
    # 切开的那一刻：先收尾甲，再给乙开一行新字幕
    split_at = events.index(first)
    after = events[split_at + 1]
    assert isinstance(after, CaptionUpdate) and (after.segment_id, after.speaker_idx) == (2, 2)
    assert after.stable == "乙1" and after.t_start == 3.0


def test_a_short_blip_of_another_speaker_does_not_split():
    a = asm()
    segments = [seg(0, 1.0, 1), seg(1.0, 1.5, 2), seg(1.5, 4, 1)]  # 乙只有 0.5 秒
    a.on_speech_started(0.0)
    events = feed(
        a,
        [
            delta("甲1", 1.0),
            delta("嗯", 1.5),  # 乙，0.5 秒，不足 0.8
            delta("甲2", 2.5),
            delta("甲3", 4.0, last=True),
        ],
        segments,
    )
    (only,) = finals(events)
    assert (only.speaker_idx, only.text) == (
        1,
        "甲1嗯甲2甲3",
    )  # 抖动的那一小段并回甲的发言，顺序不乱


def test_pending_text_is_shown_under_the_current_line_until_the_split_is_decided():
    a = asm()
    segments = [seg(0, 1.0, 1), seg(1.0, 3.0, 2)]
    a.on_speech_started(0.0)
    a.on_delta(delta("甲", 1.0), segments)
    events = a.on_delta(delta("乙1", 1.5), segments)  # 乙 0.5 秒，还没到切开的时候
    (c,) = captions(events)
    assert (c.segment_id, c.speaker_idx, c.stable) == (1, 1, "甲乙1")


def test_the_candidate_has_to_stay_in_one_run_to_count():
    a = asm()
    # 乙、丙交替各 0.5 秒：谁都没连续说满 0.8 秒，不切
    segments = [seg(0, 1, 1), seg(1, 1.5, 2), seg(1.5, 2, 3), seg(2, 2.5, 2), seg(2.5, 3, 3)]
    a.on_speech_started(0.0)
    events = feed(
        a,
        [
            delta("甲", 1.0),
            delta("乙", 1.5),
            delta("丙", 2.0),
            delta("乙", 2.5),
            delta("丙", 3.0, last=True),
        ],
        segments,
    )
    (only,) = finals(events)
    assert (only.speaker_idx, only.text) == (1, "甲乙丙乙丙")


def test_consecutive_deltas_of_the_new_speaker_add_up_to_the_threshold():
    a = asm()
    segments = [seg(0, 1, 1), seg(1, 5, 2)]
    a.on_speech_started(0.0)
    events = feed(
        a,
        [delta("甲", 1.0), delta("乙1", 1.5), delta("乙2", 2.0), delta("乙3", 3.0, last=True)],
        segments,
    )
    # 乙 0.5 + 0.5 = 1.0 ≥ 0.8，在第二个乙的增量处切开，两个乙的增量都归新的发言
    first, second = finals(events)
    assert (first.text, first.t_end) == ("甲", 1.0)
    assert (second.speaker_idx, second.text, second.t_start) == (2, "乙1乙2乙3", 1.0)


def test_switch_threshold_is_configurable():
    segments = [seg(0, 1, 1), seg(1, 2, 2)]
    steps = [delta("甲", 1.0), delta("乙", 1.5, last=True)]  # 乙只有 0.5 秒
    default = asm()
    default.on_speech_started(0.0)
    assert len(finals(feed(default, steps, segments))) == 1
    a = asm(min_switch_secs=0.4)
    a.on_speech_started(0.0)
    assert len(finals(feed(a, steps, segments))) == 2


def test_blank_stable_text_does_not_count_towards_a_switch():
    a = asm()
    segments = [seg(0, 1, 1), seg(1, 4, 2)]
    a.on_speech_started(0.0)
    events = feed(a, [delta("甲", 1.0), delta(" ", 2.0), delta("乙", 2.2, last=True)], segments)
    # 空白增量即使落在乙的时间里，也不算乙「说了话」；乙的有字增量只有 0.2 秒
    assert [f.speaker_idx for f in finals(events)] == [1]


def test_exactly_the_threshold_is_enough_to_switch():
    a = asm(min_switch_secs=0.75)  # 0.75 在二进制里是精确的，边界不受浮点误差影响
    a.on_speech_started(0.0)
    events = feed(a, [delta("甲", 1.0), delta("乙", 1.75, last=True)], [seg(0, 1, 1), seg(1, 3, 2)])
    assert [f.speaker_idx for f in finals(events)] == [1, 2]


# --------------------------------------------------------------------------- #
# 没有分段 / 说话人未知
# --------------------------------------------------------------------------- #


def test_no_segment_in_the_interval_keeps_the_previous_speaker():
    a = asm()
    segments = [seg(0, 2, 2)]  # 之后的时间区间里没有任何分段（说话人区分还没跟上）
    a.on_speech_started(0.0)
    events = feed(
        a,
        [delta("甲", 1.0), delta("乙", 2.0), delta("丙", 3.0), delta("丁", 4.0, last=True)],
        segments,
    )
    (only,) = finals(events)
    assert (only.speaker_idx, only.text) == (2, "甲乙丙丁")


def test_no_information_keeps_counting_for_the_candidate_speaker():
    """乙刚开口 0.5 秒（还没到切开的门槛），之后说话人区分还没跟上、区间里没有分段：沿用乙，继续累计。"""
    a = asm()
    a.on_speech_started(0.0)
    events = []
    events += a.on_delta(delta("甲", 1.0), [seg(0, 1, 1), seg(1, 1.5, 2)])
    events += a.on_delta(delta("乙1", 1.5), [seg(0, 1, 1), seg(1, 1.5, 2)])
    events += a.on_delta(
        delta("乙2", 2.5, last=True), [seg(0, 1, 1), seg(1, 1.5, 2)]
    )  # 之后没有分段
    first, second = finals(events)
    assert (first.speaker_idx, first.text) == (1, "甲")
    assert (second.speaker_idx, second.text) == (2, "乙1乙2")


def test_unknown_start_adopts_the_first_known_speaker_without_splitting():
    a = asm()
    a.on_speech_started(0.0)
    # 前两个增量的区间里说话人区分还没有结论（标注比音频晚约 1 秒），第三个增量才有
    steps = [delta("甲", 1.0), delta("乙", 2.0), delta("丙", 3.0, last=True)]
    events = []
    events += a.on_delta(steps[0], [])
    events += a.on_delta(steps[1], [])
    events += a.on_delta(steps[2], [seg(1.0, 3.0, 3)])
    (only,) = finals(events)
    assert (only.speaker_idx, only.text) == (3, "甲乙丙")  # 不会因为「未知 → 3」而切成两条


def test_unknown_when_nothing_is_ever_known():
    a = asm()
    a.on_speech_started(0.0)
    events = a.on_delta(delta("你好", 1.0, last=True), [])
    assert finals(events)[0].speaker_idx == SPEAKER_UNKNOWN


# --------------------------------------------------------------------------- #
# 时间换算
# --------------------------------------------------------------------------- #


def test_default_latency_shifts_the_attribution_interval_earlier():
    # 增量终点 10.2、识别延迟 0.2 → 区间是 [9.8, 10.0]，只有 2 号在说话；
    # 如果不平移，区间 [9.8, 10.2] 里 1 号（10.0 之后）和 2 号各 0.2 秒，平手取编号小的，会错判成 1 号
    a = TranscriptAssembler()
    segments = [seg(9.0, 10.0, 2), seg(10.0, 15.0, 1)]
    a.on_speech_started(9.8)
    events = a.on_delta(delta("话", 10.2, last=True), segments)
    (f,) = finals(events)
    assert (f.speaker_idx, f.t_end) == (2, pytest.approx(10.0))


def test_the_first_interval_starts_at_the_speech_start_and_intervals_tile():
    a = TranscriptAssembler(recognition_latency_secs=0.2)
    a.on_speech_started(5.0)
    events = feed(
        a, [delta("a", 6.2), delta("b", 7.2), delta("c", 8.2, last=True)], [seg(0, 20, 1)]
    )
    (f,) = finals(events)
    assert (f.t_start, f.t_end) == (5.0, pytest.approx(8.0))


def test_interval_never_runs_backwards():
    a = TranscriptAssembler(recognition_latency_secs=0.2)
    a.on_speech_started(1.0)
    events = a.on_delta(delta("早", 1.1, last=True), [seg(0, 5, 1)])  # 终点减延迟比开始时刻还早
    (f,) = finals(events)
    assert f.t_start == 1.0 and f.t_end >= f.t_start


# --------------------------------------------------------------------------- #
# 助理说话时段
# --------------------------------------------------------------------------- #


def test_deltas_mostly_inside_the_bots_speech_are_dropped():
    a = asm()
    a.on_bot_speaking(2.0, 5.0)
    a.on_speech_started(2.0)
    events = feed(
        a, [delta("助理的回声", 3.0, "还在说"), delta("还在说", 4.0, last=True)], [seg(0, 9, 1)]
    )
    assert finals(events) == [] and captions(events) == []


def test_a_delta_is_kept_when_less_than_half_of_it_overlaps_the_bot():
    a = asm()
    a.on_bot_speaking(0.0, 1.2)  # 加上 0.3 秒的尾巴 = 1.5
    a.on_speech_started(0.0)
    keep = a.on_delta(
        delta("甲", 2.0, last=True), [seg(0, 9, 1)]
    )  # 区间 [0, 2]：重叠 1.5 > 1.0 → 丢
    assert finals(keep) == []

    b = asm()
    b.on_bot_speaking(0.0, 0.9)  # 尾巴到 1.2
    b.on_speech_started(0.0)
    kept = b.on_delta(delta("乙", 2.0, last=True), [seg(0, 9, 1)])  # 重叠 1.2 > 1.0 → 丢
    assert finals(kept) == []

    c = asm()
    c.on_bot_speaking(0.0, 0.6)  # 尾巴到 0.9
    c.on_speech_started(0.0)
    kept = c.on_delta(delta("丙", 2.0, last=True), [seg(0, 9, 1)])  # 重叠 0.9 ≤ 1.0 → 留
    assert [f.text for f in finals(kept)] == ["丙"]


def test_exactly_half_overlap_is_kept():
    a = asm(bot_tail_secs=0.5)
    a.on_bot_speaking(0.0, 0.5)  # 加尾巴到 1.0；区间 [0, 2] 重叠恰好 1.0，不算「超过一半」
    a.on_speech_started(0.0)
    assert [f.text for f in finals(a.on_delta(delta("留下", 2.0, last=True), [seg(0, 9, 1)]))] == [
        "留下"
    ]


def test_a_speech_start_that_overtakes_the_previous_segments_end_is_remembered():
    """系统帧会插队：下一次「开始说话」可能先于上一段的收尾增量到达。"""
    a = asm()
    segments = [seg(0, 30, 1)]
    a.on_speech_started(0.0)
    a.on_delta(delta("上一段", 1.0), segments)
    a.on_speech_started(5.0)  # 上一段还没收尾
    a.on_delta(delta("", 1.2, last=True), segments)
    (f,) = finals(a.on_delta(delta("下一段", 7.0, last=True), segments))
    assert (f.text, f.t_start) == ("下一段", 5.0)


def test_an_ongoing_bot_utterance_has_no_end_yet():
    a = asm()
    a.on_bot_speaking(1.0, None)
    a.on_speech_started(1.0)
    assert finals(a.on_delta(delta("回声", 3.0, last=True), [seg(0, 9, 1)])) == []
    a.on_bot_speaking(1.0, 3.0)  # 说完了，窗口补上结尾
    a.on_speech_started(10.0)
    assert [
        f.text for f in finals(a.on_delta(delta("之后的话", 12.0, last=True), [seg(0, 20, 1)]))
    ] == ["之后的话"]


def test_a_dropped_segment_end_still_closes_what_was_open():
    a = asm()
    a.on_speech_started(0.0)
    a.on_delta(delta("人的话", 1.0), [seg(0, 9, 1)])
    a.on_bot_speaking(1.0, 4.0)  # 助理在人话说到一半时开口
    events = a.on_delta(delta("回声", 3.0, last=True), [seg(0, 9, 1)])
    (f,) = finals(events)
    assert f.text == "人的话"  # 回声没混进来，而已有的发言也没丢
    assert f.t_end == 1.0  # 被丢弃的回声不延长这条发言的结束时间


def test_bot_tail_is_configurable():
    a = asm(bot_tail_secs=2.0)
    a.on_bot_speaking(0.0, 1.0)  # 尾巴到 3.0
    a.on_speech_started(1.0)
    assert finals(a.on_delta(delta("回声", 2.5, last=True), [seg(0, 9, 1)])) == []


# --------------------------------------------------------------------------- #
# 事后更正
# --------------------------------------------------------------------------- #


def test_recheck_returns_the_new_speaker_only_when_it_changed():
    a = asm()
    final = UtteranceFinal(1, 0, 4.0, 8.0, "刚才没认出来")
    assert a.recheck(final, [seg(0, 20, 2)]) == 2  # 之前是未知，现在认出是 2 号
    assert a.recheck(UtteranceFinal(1, 2, 4.0, 8.0, "x"), [seg(0, 20, 2)]) is None  # 没变
    assert a.recheck(final, []) is None  # 没有任何分段：不改
    assert a.recheck(UtteranceFinal(1, 1, 4.0, 8.0, "x"), [seg(100, 200, 3)]) is None


def test_recheck_uses_the_whole_utterance_not_a_single_interval():
    a = asm()
    final = UtteranceFinal(1, 1, 0.0, 10.0, "一整句")
    assert a.recheck(final, [seg(0, 3, 1), seg(3, 10, 2)]) == 2


# --------------------------------------------------------------------------- #
# 不变量：不丢字、不重复、时间不倒退
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", range(25))
def test_random_streams_never_lose_or_duplicate_text(seed):
    rng = random.Random(seed)
    a = TranscriptAssembler()
    all_text = ""
    emitted: list[UtteranceFinal] = []
    t = 0.0
    for _ in range(rng.randint(1, 4)):  # 几个识别段
        t += rng.uniform(0.5, 3.0)
        a.on_speech_started(t)
        segments, cursor = [], t
        for _ in range(rng.randint(1, 6)):  # 说话人分段，有时留出空档
            gap = rng.choice([0.0, 0.0, 0.3, 1.0])
            length = rng.uniform(0.3, 3.0)
            segments.append(seg(cursor + gap, cursor + gap + length, rng.randint(1, 3)))
            cursor += gap + length
        n = rng.randint(1, 12)
        for i in range(n):
            t += rng.uniform(0.1, 0.8)
            text = rng.choice(["", " ", "字", "两个", "word"])
            all_text += text
            emitted += finals(a.on_delta(delta(text, t, last=(i == n - 1)), segments))
    assert "".join(f.text for f in emitted).replace(" ", "") == all_text.replace(" ", "")
    assert all(f.text and f.text == f.text.strip() for f in emitted)
    assert all(f.t_end >= f.t_start for f in emitted)
    ids = [f.segment_id for f in emitted]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)  # 递增且不重复


# --------------------------------------------------------------------------- #
# 规则 6：相邻片段并成一条
# --------------------------------------------------------------------------- #


def _merge(**kw):
    from agentic_meeting.diar.fusion import should_merge

    base = {
        "prev_speaker": 1,
        "prev_text": "我们先看一下",
        "prev_end": 10.0,
        "speaker": 1,
        "t_start": 10.5,
    }
    return should_merge(**{**base, **kw})


def test_fragments_of_the_same_speaker_merge_across_a_short_pause():
    assert _merge()
    assert _merge(t_start=12.0)  # 正好 2 秒
    assert not _merge(t_start=12.1)  # 停得久了：另起一条
    assert not _merge(speaker=2)  # 换了人
    assert not _merge(gap_secs=0)  # 关掉
    assert _merge(t_start=14.0, gap_secs=5.0)


def test_an_unknown_speaker_only_merges_across_a_breath():
    # 片段太短，说话人区分还没有结论：只在明显是一句话中间换气时才并
    assert _merge(speaker=0, t_start=10.5)
    assert not _merge(speaker=0, t_start=11.0)  # 别人插的一句短话，不能并进前一个人的话里
    assert _merge(prev_speaker=0, t_start=10.5)
    assert not _merge(prev_speaker=0, t_start=11.5)
    assert _merge(prev_speaker=0, speaker=0, t_start=10.8)


def test_a_finished_sentence_that_is_long_enough_closes_the_record():
    long_done = "这" * 39 + "。"
    assert not _merge(prev_text=long_done)  # 够长、停在句末：下一段另起一条
    assert _merge(prev_text="这" * 40)  # 够长但话没说完：接着并
    assert _merge(prev_text="好的。")  # 停在句末但很短：接着并
    assert not _merge(prev_text=long_done + "  ")  # 末尾的空白不算
    assert _merge(prev_text=long_done, soft_chars=80)
    for mark in "。！？!?…":
        assert not _merge(prev_text="这" * 39 + mark), mark
    # 上限：一个人一直说下去也不会变成无限长的一条
    assert not _merge(prev_text="话" * 200)
    assert _merge(prev_text="话" * 199)
    assert not _merge(prev_text="话" * 50, max_chars=50)


def test_fragments_are_joined_without_stray_spaces():
    from agentic_meeting.diar.fusion import join_fragments

    assert join_fragments("我们先看", "基线") == "我们先看基线"
    assert join_fragments("use the", "baseline") == "use the baseline"
    assert join_fragments("学习率 3", "e-4") == "学习率 3 e-4"
    assert join_fragments("用 Adam", "优化器") == "用 Adam优化器"
    assert join_fragments("好的。 ", " 下一个") == "好的。下一个"
    assert join_fragments("", "开头") == "开头" and join_fragments("结尾", "") == "结尾"
