"""唤醒词匹配。先确认 Pipecat 自带策略的边界问题确实存在，再确认我们的修正解决了它。"""

from __future__ import annotations

import pytest
from pipecat.turns.user_start.wake_phrase_user_turn_start_strategy import (
    WakePhraseUserTurnStartStrategy,
    _WakeState,
)

from agentic_meeting.pipeline.wake import WakeWordUserTurnStartStrategy, wake_pattern


def detects(strategy_cls, phrases: list[str], pieces: list[str]) -> bool:
    """把先后到达的若干条转录文本交给策略的匹配函数，看是否命中。

    只测匹配：把「转入唤醒状态」换成空操作，避免在没有任务管理器的情况下创建后台任务。
    """
    strategy = strategy_cls(phrases=phrases)
    strategy._transition_to_awake = lambda phrase: None
    return any(strategy._check_wake_phrase(piece) for piece in pieces)


# 名字紧贴汉字或中文标点的写法：自带策略认不出，我们的要认出。
GLUED = [
    ["请Jarvis帮我查一下"],
    ["这个问题问问Jarvis。"],
    ["Jarvis，帮我总结一下"],
    ["一下Jarvis吧", "。"],
    ["请", "jarvis查", "一下"],
]

# 名字两侧本来就有空白或在句子两端：两种策略都要认出。
SPACED = [
    ["Jarvis"],
    ["请 Jarvis 帮我查一下"],
    ["jarvis, are you there"],
    ["好，那", "Jarvis", "把王老师说的再念一遍"],
]

# 不该命中的。
NEGATIVE = [
    ["我们上周已经把baseline跑完了"],
    ["这个方法和Transformer差不多"],
    ["Jarvisson是另一个人"],  # 名字只是更长单词的一部分
    ["MyJarvis2也不算"],
    ["请 Jar", "vis 帮我"],  # 名字被拆在两条转录里（识别服务保证不会这样拆，见 interfaces.md §3.2）
    [""],
]


@pytest.mark.parametrize("pieces", GLUED)
def test_native_strategy_misses_names_glued_to_chinese(pieces):
    """这条测试记录的是 Pipecat 的现状。升级后如果它失败，说明上游改了匹配方式，
    届时应重新评估 WakeWordUserTurnStartStrategy 是否还需要。"""
    assert not detects(WakePhraseUserTurnStartStrategy, ["Jarvis"], pieces)


@pytest.mark.parametrize("pieces", GLUED + SPACED)
def test_wake_word_strategy_detects(pieces):
    assert detects(WakeWordUserTurnStartStrategy, ["Jarvis"], pieces)


@pytest.mark.parametrize("pieces", NEGATIVE)
def test_wake_word_strategy_ignores(pieces):
    assert not detects(WakeWordUserTurnStartStrategy, ["Jarvis"], pieces)


def test_multi_word_phrase_and_aliases():
    phrases = ["Hey Nova", "Nova"]
    assert detects(WakeWordUserTurnStartStrategy, phrases, ["嘿，hey  nova，你在吗"])
    assert detects(WakeWordUserTurnStartStrategy, phrases, ["问一下nova吧"])
    assert not detects(WakeWordUserTurnStartStrategy, phrases, ["supernova 爆发"])


def test_pattern_is_case_insensitive_and_bounded():
    pattern = wake_pattern("Echo")
    assert pattern.search("请ECHO查一下")
    assert not pattern.search("echocardiogram")
    assert not pattern.search("Echo2")


def test_strategy_keeps_native_state_machine():
    """除了匹配模式，其余全部继承自带策略。"""
    strategy = WakeWordUserTurnStartStrategy(phrases=["Jarvis"], single_activation=True, timeout=5)
    assert isinstance(strategy, WakePhraseUserTurnStartStrategy)
    assert len(strategy._patterns) == 1
    assert strategy._single_activation is True
    assert strategy._timeout == 5


def test_sleep_returns_to_idle_only_when_awake():
    strategy = WakeWordUserTurnStartStrategy(phrases=["Jarvis"], single_activation=True, timeout=5)
    idled: list[bool] = []
    strategy._transition_to_idle = lambda: idled.append(
        True
    )  # 父类的这一步要用到任务管理器，这里只数次数
    strategy.sleep()
    assert not strategy.awake and idled == []
    strategy._state = _WakeState.AWAKE
    assert strategy.awake
    strategy.sleep()
    assert idled == [True]


def test_chinese_aliases_match_as_plain_runs_of_characters():
    """识别把英文名字听成汉字时（「诺瓦」），靠别名唤醒；汉字没有词边界，连续出现就算。"""
    phrases = ["Nova", "诺瓦"]
    assert detects(WakeWordUserTurnStartStrategy, phrases, ["诺瓦，帮我总结一下"])
    assert detects(
        WakeWordUserTurnStartStrategy, phrases, ["这个问题问问诺", "瓦吧"]
    )  # 拆在两条转录里
    assert not detects(WakeWordUserTurnStartStrategy, phrases, ["承诺过的瓦片"])
    assert wake_pattern("诺瓦").search("请诺瓦看一下")
