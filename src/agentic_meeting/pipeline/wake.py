"""唤醒：用 Pipecat 自带的唤醒策略，只修正它对「词边界」的判断。

助理的名字约定为英文单词（见 docs/architecture.md §5.2）。Pipecat 自带的
``WakePhraseUserTurnStartStrategy`` 本身就支持英文唤醒词，状态机、超时、事件都直接沿用；
唯一的问题是它用正则的 ``\\b`` 判断词边界，而 ``\\b`` 把汉字也算作单词字符：

    "请 Jarvis 帮我查一下"   命中（名字两侧是空格）
    "请Jarvis帮我查一下"     不命中（名字紧贴汉字，没有 \\b 边界）
    "Jarvis，帮我查一下"     不命中（它先删掉标点，名字就贴上了后面的汉字）

中文句子里的英文名字，识别结果有一半左右是后两种写法（已用真实权重实测，
见 docs/benchmarks.md）。所以这里把边界条件换成「名字前后不是英文字母或数字」。

另外加了一个 ``sleep()``：自带策略在「每次都要叫名字」的模式下，叫一次名字之后会一直醒到超时为止，
这段时间里任何人说的话都会被当成对助理说的。管线在助理答完时调用 ``sleep()`` 让它立刻回到待唤醒状态
（接线在 pipeline/bot.py 的 ``wire_wake_sleep``）。其余行为与自带策略完全一致。
"""

from __future__ import annotations

import re

from pipecat.turns.user_start.wake_phrase_user_turn_start_strategy import (
    WakePhraseUserTurnStartStrategy,
    _WakeState,
)

_NOT_ASCII_WORD_BEFORE = r"(?<![A-Za-z0-9])"
_NOT_ASCII_WORD_AFTER = r"(?![A-Za-z0-9])"


def wake_pattern(phrase: str) -> re.Pattern[str]:
    """把一个唤醒词编译成正则：大小写不敏感，词与词之间允许任意空白，前后不能紧贴英文字母或数字。

    全是汉字的别名（识别把名字听成的那几个字，见 ``session.wake_aliases``）没有词边界可言：
    按连续出现匹配，字与字之间允许空白（策略会在相邻两条转录之间加一个空格）。
    """
    if not phrase.isascii():
        return re.compile(r"\s*".join(re.escape(char) for char in phrase if not char.isspace()))
    body = r"\s*".join(re.escape(word) for word in phrase.split())
    return re.compile(_NOT_ASCII_WORD_BEFORE + body + _NOT_ASCII_WORD_AFTER, re.IGNORECASE)


class WakeWordUserTurnStartStrategy(WakePhraseUserTurnStartStrategy):
    """自带唤醒策略 + 适用于中英混排文本的词边界。

    用法、参数、事件（``on_wake_phrase_detected``、``on_wake_phrase_timeout``）与父类相同。
    只覆盖父类在构造时编译好的匹配模式（私有属性 ``_patterns``）；Pipecat 的版本是锁定的，
    tests/test_wake.py 会在这个属性消失或行为变化时失败。
    """

    def __init__(self, *, phrases: list[str], **kwargs):
        super().__init__(phrases=phrases, **kwargs)
        self._patterns = [wake_pattern(phrase) for phrase in phrases]

    @property
    def awake(self) -> bool:
        return self._state == _WakeState.AWAKE

    def sleep(self) -> None:
        """立刻回到待唤醒状态（醒着才有动作）；和超时走同一条路，也会触发 ``on_wake_phrase_timeout``。"""
        if self.awake:
            self._transition_to_idle()
