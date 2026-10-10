"""llama-server 识别后端（asr/llama_server.py）。

不需要权重：用 ``httpx.MockTransport`` 同时模拟 ``/v1/chat/completions`` 和 ``/tokenize``。
模拟的服务端和真实的一样，返回「assistant 前缀 + 续写」；分词默认按单个字符切，
需要按词切时在用例里给出。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import wave
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple

import httpx
import numpy as np
import pytest
from waiting import wait_until

from agentic_meeting.asr import build_asr_backend
from agentic_meeting.asr.base import ASRBackendError
from agentic_meeting.asr.llama_server import (
    LlamaServerASR,
    _StreamState,
    find_stable_end,
    token_budget,
)
from agentic_meeting.config import ASRConfig, ASRProfile
from agentic_meeting.types import ASRDelta

# 故意写成与真实档案不同的格式，确保代码用的是档案里的字符串而不是写死的。
PROFILE = ASRProfile(
    chat_template="unused",
    assistant_prefix="lang {language}<t>",
    text_marker="<t>",
    cut_markers=["|", "#"],
    hotwords_template="热词：{hotwords}",
    hotwords_joiner=",",
)
HEAD = "lang Chinese<t>"
HOTWORDS = ["alphaterm", "betaterm", "gammaterm"]
SAMPLES_PER_CHUNK = 5120  # 320 毫秒


# --------------------------------------------------------------------------- #
# 模拟服务端与测试夹具
# --------------------------------------------------------------------------- #


@dataclass
class Raw:
    """原样作为 ``content`` 返回，不接 assistant 前缀（模拟不回显前缀的服务端）。"""

    content: str


MALFORMED = object()  # 200，但响应里没有 choices


class FakeAsrServer:
    """模拟 llama-server。``replies`` 按顺序对应每一次聊天请求：

    * ``str``：模型的续写，服务端会在前面接上请求里的 assistant 前缀再返回；
    * ``int``：直接返回这个 HTTP 状态码；
    * :class:`Raw`、``MALFORMED``：见上；
    * 可调用对象：收到请求体，返回上面几种之一。
    用完之后一律返回空续写（相当于静音）。
    """

    def __init__(self, replies=(), *, pieces: dict[str, list] | None = None):
        self.replies: deque = deque(replies)
        self.pieces = pieces or {}
        self.chat: list[dict] = []
        self.tokenize: list[dict] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.gate: asyncio.Event | None = None
        self.tokenize_failures = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/tokenize":
            self.tokenize.append(body)
            if self.tokenize_failures:
                self.tokenize_failures -= 1
                return httpx.Response(500)
            content = body["content"]
            pieces = self.pieces.get(content) or list(content)
            return httpx.Response(
                200, json={"tokens": [{"id": i, "piece": p} for i, p in enumerate(pieces)]}
            )
        assert request.url.path == "/v1/chat/completions"
        self.chat.append(body)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.gate is not None:
                await self.gate.wait()
            reply = self.replies.popleft() if self.replies else ""
            if callable(reply):
                reply = reply(body)
            if reply is MALFORMED:
                return httpx.Response(200, json={"oops": True})
            if isinstance(reply, Raw):
                return httpx.Response(
                    200, json={"choices": [{"message": {"content": reply.content}}]}
                )
            if isinstance(reply, int):
                return httpx.Response(reply)
            assistant = body["messages"][-1]["content"]
            return httpx.Response(
                200, json={"choices": [{"message": {"content": assistant + reply}}]}
            )
        finally:
            self.in_flight -= 1


class Wav(NamedTuple):
    rate: int
    channels: int
    width: int
    frames: int
    data: bytes


def wav_of(body: dict) -> Wav:
    """取出请求体里的音频，并按 WAV 解析（不是合法 WAV 会直接抛异常）。"""
    part = body["messages"][1]["content"][0]
    assert part["type"] == "input_audio" and part["input_audio"]["format"] == "wav"
    with wave.open(io.BytesIO(base64.b64decode(part["input_audio"]["data"]))) as w:
        n = w.getnframes()
        return Wav(w.getframerate(), w.getnchannels(), w.getsampwidth(), n, w.readframes(n))


def frames(body: dict) -> int:
    return wav_of(body).frames


def assistant_of(body: dict) -> str:
    return body["messages"][2]["content"]


def pcm(ms: int, value: int = 1000) -> bytes:
    return np.full(16 * ms, value, dtype="<i2").tobytes()


class Feeder:
    """像调用方（识别服务）一样按采样数累计会话时间轴。"""

    def __init__(self, backend: LlamaServerASR):
        self.backend = backend
        self.samples = 0

    async def push(self, ms: int = 320, value: int = 1000) -> None:
        data = pcm(ms, value)
        self.samples += len(data) // 2
        await self.backend.push_audio(data, self.samples / 16000)


@dataclass
class Rig:
    cfg: Any
    server: FakeAsrServer
    backend: LlamaServerASR
    feeder: Feeder
    client: httpx.AsyncClient
    it: Any  # deltas() 的异步迭代器


async def get(rig: Rig, secs: float = 2.0) -> ASRDelta:
    return await asyncio.wait_for(anext(rig.it), secs)


async def quiet(rig: Rig, secs: float = 0.05) -> None:
    """断言这段时间里没有增量产生。超时会终止迭代器，所以顺手换一个新的。"""
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(rig.it), secs)
    rig.it = rig.backend.deltas()


@pytest.fixture
async def make_rig(make_cfg):
    rigs: list[Rig] = []

    async def factory(
        *,
        replies=(),
        pieces: dict[str, list] | None = None,
        hotwords=HOTWORDS,
        configure: Callable[[ASRConfig], None] | None = None,
        start: bool = True,
    ) -> Rig:
        cfg = make_cfg()
        if configure:
            configure(cfg.asr)
        server = FakeAsrServer(replies, pieces=pieces)
        client = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
        backend = LlamaServerASR(cfg.asr, PROFILE, list(hotwords), client=client, retry_base_secs=0)
        rig = Rig(cfg, server, backend, Feeder(backend), client, backend.deltas())
        if start:
            server.replies.appendleft("")  # 预热请求
            await backend.start()
            server.chat.clear()
        rigs.append(rig)
        return rig

    yield factory
    for rig in rigs:
        await rig.backend.close()
        await rig.client.aclose()


def pb(*items) -> list[bytes]:
    """把 str / 字节值列表混合的写法转成 token 的字节串。"""
    return [i.encode() if isinstance(i, str) else bytes(i) for i in items]


# --------------------------------------------------------------------------- #
# 纯函数：token 预算与按 token 回退
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("new_samples", "prefix", "final", "language", "ceiling", "expected"),
    [
        (SAMPLES_PER_CHUNK, "你好", False, "Chinese", 32, 8),  # 上一个字符是汉字：翻倍
        (SAMPLES_PER_CHUNK, "hello", False, "Chinese", 32, 4),  # 英文：每 80 毫秒 1 个
        (3 * SAMPLES_PER_CHUNK, "你好", False, "Chinese", 32, 24),  # 合并 3 个步长
        (3 * SAMPLES_PER_CHUNK, "hello", False, "Chinese", 32, 12),
        (3 * SAMPLES_PER_CHUNK, "你好", False, "Chinese", 16, 16),  # 不超过配置上限
        (SAMPLES_PER_CHUNK, "", False, "Chinese", 32, 8),  # 段首：按识别语言
        (SAMPLES_PER_CHUNK, "", False, "English", 32, 4),
        (SAMPLES_PER_CHUNK, "", False, "", 32, 4),
        (100, "hello", False, "Chinese", 32, 1),  # 不足 80 毫秒也至少 1 个
        (0, "你好", True, "Chinese", 32, 32),  # 收尾：放开到上限
        (SAMPLES_PER_CHUNK, "hello", True, "Chinese", 32, 32),
    ],
)
def test_token_budget(new_samples, prefix, final, language, ceiling, expected):
    assert (
        token_budget(new_samples, prefix, final=final, language=language, ceiling=ceiling)
        == expected
    )


def test_stable_end_backs_off_by_tokens():
    # 「现在屏幕上」：去掉最后 1 个 token（「上」）
    pieces = pb("现在", "屏幕", "上")
    assert find_stable_end("现在屏幕上", 0, pieces, keep_back=1) == 4
    assert find_stable_end("现在屏幕上", 0, pieces, keep_back=2) == 2
    assert find_stable_end("现在屏幕上", 0, pieces, keep_back=0) == 5


def test_stable_end_never_goes_below_floor():
    pieces = pb("现在", "屏幕", "上")
    assert find_stable_end("现在屏幕上", 5, pieces, keep_back=1) == 5
    assert find_stable_end("现在屏幕上", 0, pieces, keep_back=3) == 0


def test_stable_end_steps_back_over_split_characters():
    # 「你」被拆成两个 token：e4 bd | a0。切点落在两半之间时要再往前退一个 token。
    pieces = pb("现", [0xE4, 0xBD], [0xA0], "好")
    assert find_stable_end("现你好", 0, pieces, keep_back=2) == 1
    assert find_stable_end("现你好", 0, pieces, keep_back=1) == 2


def test_stable_end_does_not_split_an_english_word():
    pieces = pb("Jar", "vis", "你好")
    full = "Jarvis你好"
    assert find_stable_end(full, 0, pieces, keep_back=1) == 6  # 停在 Jarvis 之后
    assert find_stable_end(full, 0, pieces, keep_back=2) == 0  # Jar|vis 之间不行，退到单词之前


def test_stable_end_gives_up_when_pieces_do_not_rebuild_the_text():
    assert find_stable_end("你好吗", 1, pb("你", "好"), keep_back=1) == 1


# --------------------------------------------------------------------------- #
# 状态机里不涉及 IO 的部分
# --------------------------------------------------------------------------- #


def test_backlog_beyond_the_window_keeps_only_the_most_recent_audio(make_cfg):
    cfg = make_cfg()
    state = _StreamState(cfg.asr, PROFILE, [])
    ramp = (np.arange(20 * 16000) % 30000).astype(np.int16)
    assert state.feed(ramp[:1600], 0.1) == 0  # 没积压
    state.pending = state.pending[:0]
    dropped = state.feed(ramp, 20.0)
    limit = int(cfg.asr.window_secs * 16000)
    assert dropped == len(ramp) - limit
    assert len(state.pending) == limit
    assert state.pending[0] == ramp[dropped]  # 丢的是最早的
    assert state.pending[-1] == ramp[-1]


# --------------------------------------------------------------------------- #
# 流式行为
# --------------------------------------------------------------------------- #


async def test_each_step_prefixes_the_request_with_everything_stable_so_far(make_rig):
    rig = await make_rig(replies=["现在屏幕上", "上显示的是图", "图表。"])
    seen = []
    for _ in range(3):
        await rig.feeder.push()
        seen.append(await get(rig))

    assert [(d.stable_text, d.unstable_text) for d in seen] == [
        ("现在屏幕", "上"),
        ("上显示的是", "图"),
        ("图表", "。"),
    ]
    assert [assistant_of(b) for b in rig.server.chat] == [
        HEAD,
        HEAD + "现在屏幕",
        HEAD + "现在屏幕上显示的是",
    ]
    # 只追加：每一步的定稿文字拼起来，永远是下一步请求里前缀的延伸
    joined = ""
    for delta, body in zip(seen, rig.server.chat[1:] + [None], strict=True):
        joined += delta.stable_text
        if body is not None:
            assert assistant_of(body) == HEAD + joined


async def test_request_body_follows_the_contract(make_rig):
    rig = await make_rig(replies=["你"])
    await rig.feeder.push(320, value=1234)
    await get(rig)

    body = rig.server.chat[0]
    assert set(body) == {"messages", "temperature", "max_tokens", "stream"}  # 没有 chat_template
    assert body["temperature"] == 0
    assert body["stream"] is False
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant"]
    assert body["messages"][0]["content"] == "热词：alphaterm,betaterm,gammaterm"
    wav = wav_of(body)
    assert (wav.rate, wav.channels, wav.width, wav.frames) == (16000, 1, 2, SAMPLES_PER_CHUNK)
    assert wav.data == pcm(320, 1234)  # 送进去的 16 位采样原样进了 WAV


async def test_system_message_is_empty_without_hotwords(make_rig):
    rig = await make_rig(replies=["你"], hotwords=[])
    await rig.feeder.push()
    await get(rig)
    assert rig.server.chat[0]["messages"][0] == {"role": "system", "content": ""}


async def test_no_assistant_head_when_language_is_empty(make_rig):
    rig = await make_rig(replies=["你好"], configure=lambda asr: setattr(asr, "language", ""))
    await rig.feeder.push()
    await get(rig)
    assert assistant_of(rig.server.chat[0]) == ""


async def test_token_budget_follows_the_last_stable_character(make_rig):
    rig = await make_rig(replies=["你好吗", "好吗？"])
    await rig.feeder.push()
    await get(rig)  # 段首、识别语言是中文
    await rig.feeder.push()
    await get(rig)  # 上一个定稿字符「你」是汉字
    assert [b["max_tokens"] for b in rig.server.chat] == [8, 8]


async def test_token_budget_for_english_prefix(make_rig):
    rig = await make_rig(
        replies=["hello world", "world again"],
        configure=lambda asr: setattr(asr, "language", "English"),
    )
    await rig.feeder.push()
    await get(rig)
    await rig.feeder.push()
    await get(rig)
    assert [b["max_tokens"] for b in rig.server.chat] == [4, 4]


async def test_backlog_is_merged_into_at_most_three_chunks_per_step(make_rig):
    rig = await make_rig(replies=["你好吗", "你好"])
    for _ in range(4):  # push_audio 不会让出事件循环，四块音频在第一步开始前就都到了
        await rig.feeder.push()
    first, second = await get(rig), await get(rig)

    assert [frames(b) for b in rig.server.chat] == [3 * SAMPLES_PER_CHUNK, 4 * SAMPLES_PER_CHUNK]
    assert [b["max_tokens"] for b in rig.server.chat] == [24, 8]
    assert first.audio_end_secs == second.audio_end_secs == pytest.approx(1.28)


async def test_merged_budget_never_exceeds_the_configured_ceiling(make_rig):
    rig = await make_rig(replies=["你好"], configure=lambda asr: setattr(asr, "max_new_tokens", 16))
    for _ in range(3):
        await rig.feeder.push()
    await get(rig)
    assert rig.server.chat[0]["max_tokens"] == 16


async def test_only_one_request_in_flight_and_delta_carries_the_end_at_request_time(make_rig):
    rig = await make_rig(replies=["你好", "好吗"])
    rig.server.gate = asyncio.Event()
    await rig.feeder.push()  # 到 0.32 秒
    await wait_until(lambda: rig.server.in_flight == 1)
    await rig.feeder.push()  # 请求在途时又到了两块
    await rig.feeder.push()
    await asyncio.sleep(0.05)
    assert len(rig.server.chat) == 1  # 不会并发发第二个请求

    rig.server.gate.set()
    first, second = await get(rig), await get(rig)
    assert first.audio_end_secs == pytest.approx(0.32)  # 发起请求时已收到的最新值
    assert second.audio_end_secs == pytest.approx(0.96)
    assert frames(rig.server.chat[1]) == 3 * SAMPLES_PER_CHUNK  # 在途期间到的两块合并成一步
    assert rig.server.max_in_flight == 1


async def test_odd_trailing_byte_is_ignored(make_rig):
    rig = await make_rig(replies=["你"])
    await rig.backend.push_audio(pcm(320) + b"\x01", 0.32)
    await get(rig)
    assert frames(rig.server.chat[0]) == SAMPLES_PER_CHUNK
    await rig.backend.push_audio(b"", 0.32)  # 空音频直接忽略
    await quiet(rig)


# --------------------------------------------------------------------------- #
# 窗口滑动
# --------------------------------------------------------------------------- #


async def test_window_slides_and_drops_the_matching_text(make_rig):
    def narrow(asr: ASRConfig) -> None:
        asr.window_secs = 4.0
        asr.window_drop_secs = 2.0

    chars = [chr(0x4E00 + k) for k in range(13)]
    rig = await make_rig(replies=[c + "。" for c in chars], configure=narrow)  # 每步定稿一个字
    for _ in range(13):
        await rig.feeder.push()
        await get(rig)

    sizes = [frames(b) for b in rig.server.chat]
    assert max(sizes) <= 4 * 16000  # 请求里的音频不超过窗口上限
    # 第 13 步窗口超限（13 × 5120 = 66560 > 64000）：从头弹出 7 步（35840 ≥ 32000 个采样）。
    assert sizes[12] == 13 * SAMPLES_PER_CHUNK - 7 * SAMPLES_PER_CHUNK
    # 被弹出的是最早 7 步的文字，剩下的 5 个字与剩下的音频对齐。
    assert assistant_of(rig.server.chat[12]) == HEAD + "".join(chars[7:12])


# --------------------------------------------------------------------------- #
# 收尾
# --------------------------------------------------------------------------- #


async def test_flush_finalises_the_tail_and_clears_the_window(make_rig):
    rig = await make_rig(replies=["你好吗", "吗？", "好"])
    await rig.feeder.push()
    first = await get(rig)
    assert (first.stable_text, first.unstable_text) == ("你好", "吗")

    await rig.backend.flush()
    last = await get(rig)
    assert (last.stable_text, last.unstable_text, last.segment_end) == ("吗？", "", True)
    assert rig.server.chat[1]["max_tokens"] == 32  # 收尾放开到上限
    assert frames(rig.server.chat[1]) == SAMPLES_PER_CHUNK  # 窗口里原有的音频

    await rig.feeder.push()  # 下一段从干净的状态开始
    await get(rig)
    assert assistant_of(rig.server.chat[2]) == HEAD
    assert frames(rig.server.chat[2]) == SAMPLES_PER_CHUNK


async def test_flush_takes_audio_that_has_not_filled_a_step_yet(make_rig):
    rig = await make_rig(replies=["你好"])
    await rig.feeder.push(100)
    await quiet(rig)  # 不足一个步长，不发请求
    assert rig.server.chat == []

    await rig.backend.flush()
    delta = await get(rig)
    assert (delta.stable_text, delta.unstable_text, delta.segment_end) == ("你好", "", True)
    assert frames(rig.server.chat[0]) == 1600
    assert delta.audio_end_secs == pytest.approx(0.1)


async def test_flush_with_nothing_pending_still_ends_the_segment(make_rig):
    rig = await make_rig()
    await rig.backend.flush()
    delta = await get(rig)
    assert delta == ASRDelta("", "", 0.0, True)
    assert rig.server.chat == []  # 没有音频就不发请求


@pytest.mark.parametrize(
    ("language", "expected"),
    [("Chinese", "消融实验 ablation study"), ("English", "消 融 实验 ablation study")],
)
async def test_chinese_text_has_its_spaces_removed(make_rig, language, expected):
    rig = await make_rig(
        replies=["消 融 实验 ablation study"],
        configure=lambda asr: setattr(asr, "language", language),
    )
    await rig.feeder.push(100)
    await rig.backend.flush()
    assert (await get(rig)).stable_text == expected


async def test_cut_markers_and_replacement_characters_are_removed(make_rig):
    rig = await make_rig(replies=["你�好|其他信息"])
    await rig.feeder.push(100)
    await rig.backend.flush()
    assert (await get(rig)).stable_text == "你好"


async def test_text_marker_is_used_when_the_server_does_not_echo_the_prefix(make_rig):
    # 个别服务端版本不会把前缀原样返回，而是只给「元信息 + 标记 + 正文」。
    rig = await make_rig(replies=[Raw("meta<t>你好")])
    await rig.feeder.push(100)
    await rig.backend.flush()
    assert (await get(rig)).stable_text == "你好"


# --------------------------------------------------------------------------- #
# 照抄热词、复读
# --------------------------------------------------------------------------- #


async def test_echoed_hotword_list_at_segment_start_is_discarded(make_rig):
    rig = await make_rig(replies=["alphaterm betaterm gammaterm", "你好"])
    await rig.feeder.push()
    dropped = await get(rig)
    assert (dropped.stable_text, dropped.unstable_text, dropped.segment_end) == ("", "", False)

    await rig.feeder.push()
    real = await get(rig)
    assert (real.stable_text, real.unstable_text) == ("你", "好")
    assert assistant_of(rig.server.chat[1]) == HEAD  # 照抄的内容没有进前缀
    assert frames(rig.server.chat[1]) == 2 * SAMPLES_PER_CHUNK  # 但那段音频还留在窗口里


async def test_a_single_hotword_is_not_an_echo(make_rig):
    rig = await make_rig(replies=["alphaterm 你好"])
    await rig.feeder.push()
    delta = await get(rig)
    assert (delta.stable_text, delta.unstable_text) == ("alphaterm 你", "好")


async def test_flush_strips_only_the_echoed_part(make_rig):
    rig = await make_rig(replies=["alphaterm betaterm，你好"])
    await rig.feeder.push(100)
    await rig.backend.flush()
    delta = await get(rig)
    assert (delta.stable_text, delta.unstable_text, delta.segment_end) == ("你好", "", True)


async def test_repeated_output_is_treated_as_hallucination_and_resets_the_window(make_rig):
    rig = await make_rig(replies=["啊" * 20, "你好"])
    await rig.feeder.push()
    delta = await get(rig)
    assert (delta.stable_text, delta.unstable_text, delta.segment_end) == ("", "", False)

    await rig.feeder.push()
    await get(rig)
    assert assistant_of(rig.server.chat[1]) == HEAD
    assert frames(rig.server.chat[1]) == SAMPLES_PER_CHUNK  # 窗口已被清空


# --------------------------------------------------------------------------- #
# 预热、失败与恢复
# --------------------------------------------------------------------------- #


async def test_start_sends_exactly_one_warmup_request_and_no_delta(make_rig):
    rig = await make_rig(start=False)
    rig.server.replies.append("你好")  # 就算预热得到了文字也不能变成增量
    await rig.backend.start()

    assert len(rig.server.chat) == 1
    warmup = wav_of(rig.server.chat[0])
    assert warmup.frames == 16000 and set(warmup.data) == {0}  # 1 秒静音
    assert assistant_of(rig.server.chat[0]) == HEAD
    await quiet(rig)


async def test_start_failure_raises_a_backend_error_and_can_be_retried(make_rig):
    rig = await make_rig(replies=[500], start=False)
    with pytest.raises(ASRBackendError):
        await rig.backend.start()

    rig.server.replies.extend(["", "你好"])  # 预热、第一步
    await rig.backend.start()
    await rig.feeder.push()
    assert (await get(rig)).stable_text == "你"


async def test_failed_step_is_retried_with_the_same_audio_and_prefix(make_rig):
    rig = await make_rig(replies=[500, "你好"])
    await rig.feeder.push()
    delta = await get(rig)  # 失败的那一步没有增量，重试成功后才有
    assert (delta.stable_text, delta.unstable_text) == ("你", "好")
    assert len(rig.server.chat) == 2
    assert rig.server.chat[0]["messages"] == rig.server.chat[1]["messages"]


async def test_audio_arriving_during_a_failed_step_is_not_lost(make_rig):
    rig = await make_rig(replies=[500, "你好吗"])
    rig.server.gate = asyncio.Event()
    await rig.feeder.push()
    await wait_until(lambda: rig.server.in_flight == 1)
    await rig.feeder.push()  # 第一步还在途时到的
    rig.server.gate.set()
    await get(rig)
    # 重试时：失败那块 + 在途期间到的那块，一起送出
    assert [frames(b) for b in rig.server.chat] == [SAMPLES_PER_CHUNK, 2 * SAMPLES_PER_CHUNK]


async def test_tokenize_failure_rolls_the_step_back_too(make_rig):
    rig = await make_rig(replies=["你好", "你好"])
    rig.server.tokenize_failures = 1
    await rig.feeder.push()
    delta = await get(rig)
    assert delta.stable_text == "你"
    assert len(rig.server.chat) == 2 and len(rig.server.tokenize) == 2


async def test_five_consecutive_failures_end_the_iteration_with_an_error(make_rig):
    rig = await make_rig(replies=[500] * 5)
    await rig.feeder.push()
    with pytest.raises(ASRBackendError):
        await get(rig)
    assert len(rig.server.chat) == 5

    # 之后继续送音频、收尾都不报错，也不再发请求（转录计时由调用方负责，不能因此中断）
    await rig.feeder.push()
    await rig.backend.flush()
    await asyncio.sleep(0.05)
    assert len(rig.server.chat) == 5


async def test_backend_can_be_restarted_after_it_gave_up(make_rig):
    rig = await make_rig(replies=[500] * 5)
    await rig.feeder.push()
    with pytest.raises(ASRBackendError):
        await get(rig)

    rig.server.replies.extend(["", "你好"])  # 预热、第一步
    await rig.backend.start()
    rig.it = rig.backend.deltas()
    await rig.feeder.push()
    delta = await get(rig)
    assert delta.stable_text == "你"
    assert frames(rig.server.chat[-1]) == SAMPLES_PER_CHUNK  # 失败前积压的音频已清掉


async def test_failure_counter_resets_after_a_success(make_rig):
    rig = await make_rig(replies=[500] * 4 + ["你好"] + [500] * 4 + ["好吗"])
    await rig.feeder.push()
    assert (await get(rig)).stable_text == "你"
    await rig.feeder.push()
    assert (await get(rig)).unstable_text == "吗"


async def test_flush_retries_a_failed_final_step(make_rig):
    rig = await make_rig(replies=[500, "你好"])
    await rig.feeder.push(100)
    await rig.backend.flush()
    delta = await get(rig)
    assert (delta.stable_text, delta.segment_end) == ("你好", True)
    assert rig.server.chat[0]["messages"] == rig.server.chat[1]["messages"]


async def test_malformed_response_counts_as_a_failure(make_rig):
    rig = await make_rig(replies=[MALFORMED, "你好"])
    await rig.feeder.push()
    assert (await get(rig)).stable_text == "你"
    assert len(rig.server.chat) == 2


# --------------------------------------------------------------------------- #
# 关闭、工厂
# --------------------------------------------------------------------------- #


async def test_close_ends_iteration_and_is_idempotent(make_rig):
    rig = await make_rig()

    async def collect() -> list[ASRDelta]:
        return [d async for d in rig.backend.deltas()]

    consumer = asyncio.create_task(collect())
    await asyncio.sleep(0)
    await rig.backend.close()
    assert await asyncio.wait_for(consumer, 1) == []
    await rig.backend.close()
    assert [d async for d in rig.backend.deltas()] == []  # 关闭之后再迭代也立即结束


async def test_close_does_not_wait_for_a_request_in_flight(make_rig):
    rig = await make_rig(replies=["你好"])
    rig.server.gate = asyncio.Event()  # 永远不放行
    await rig.feeder.push()
    await wait_until(lambda: rig.server.in_flight == 1)
    await asyncio.wait_for(rig.backend.close(), 1)
    await wait_until(lambda: rig.server.in_flight == 0)


async def test_audio_after_close_is_ignored(make_rig):
    rig = await make_rig()
    await rig.backend.close()
    await rig.feeder.push()
    await rig.backend.flush()
    assert rig.server.chat == []
    with pytest.raises(ASRBackendError):
        await rig.backend.start()


async def test_factory_builds_the_backend_with_hotwords_and_the_assistants_name(make_cfg):
    cfg = make_cfg()
    cfg.session.assistant_name = "Nova"
    cfg.session.wake_aliases = ["Novah"]
    cfg.session.hotwords = ["课题词", "Nova"]  # 已有的名字不重复；别名不当热词
    backend = build_asr_backend(cfg)
    try:
        assert isinstance(backend, LlamaServerASR)
        assert backend.hotwords == ["课题词", "Nova"]
    finally:
        await backend.close()
