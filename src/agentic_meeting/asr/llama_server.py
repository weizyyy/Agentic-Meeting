"""llama-server 流式识别后端：滚动窗口 + 前缀续写。

算法说明见 docs/interfaces.md §3.2。**不要「优化」token 预算和回退方式**：
最初每步放开生成 32 个 token、按字符回退，结果丢字、乱加标点、句子截断（见 docs/benchmarks.md）。

结构分两层：

* ``_StreamState`` 和模块里的纯函数：给定状态和模型的续写文本，算出新状态和增量，不碰网络，
  单独测试。分词结果作为参数传进来。
* ``LlamaServerASR``：后台 worker 串行地做「取音频 → 请求 → 分词 → 算增量」，
  同一时刻只有一个请求在途；新音频到达时只累积到 ``pending``。

本文件不出现任何模型名、模板字符串或 ``<asr_text>`` 之类的标记——全部来自 ``ASRProfile``。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import re
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import numpy as np
from loguru import logger

from agentic_meeting.asr.base import ASR_SAMPLE_RATE, ASRBackendError
from agentic_meeting.config import ASRConfig, ASRProfile, is_loopback
from agentic_meeting.types import ASRDelta

# 80 毫秒音频对应 1 个 token 的生成预算，取自官方 ws_server.py。
SAMPLES_PER_TOKEN = 1280
# 一步最多吃掉的步长数；落后时自动合并，多的留到下一步。
MAX_CHUNKS_PER_STEP = 3
MAX_CONSECUTIVE_FAILURES = 5
# pending 积压超过这么多秒就告警；超过窗口长度则丢掉最早的（见 _StreamState.feed）。
BACKLOG_WARN_SECS = 5.0
# 服务启动后的第一次请求可能很慢（实测头几次要十几秒，见 docs/benchmarks.md），留足余量。
REQUEST_TIMEOUT_SECS = 30.0
CONNECT_TIMEOUT_SECS = 5.0
MAX_RETRY_DELAY_SECS = 2.0
WARMUP_SECS = 1.0

_CJK = "一-鿿"
_SPACE_BETWEEN_CJK = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])")
_REPETITION = re.compile(r"(.{1,6}?)\1{5,}")
_EMPTY = np.zeros(0, dtype=np.int16)


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿"


def _is_ascii_word(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def token_budget(new_samples: int, prefix: str, *, final: bool, language: str, ceiling: int) -> int:
    """这一步最多让模型生成几个 token。

    官方服务端的做法：每 80 毫秒新增音频给 1 个 token，上一个定稿字符是汉字时翻倍，并设上限。
    模型是按「只输出已经稳定的内容」训练的，预算给多了它会抢跑甚至照抄热词。
    收尾时音频已经结束，放开到配置的上限把尾巴取完。
    """
    if final:
        return ceiling
    base = max(1, new_samples // SAMPLES_PER_TOKEN)
    chinese = _is_cjk(prefix[-1]) if prefix else language == "Chinese"
    return min(ceiling, max(4, 2 * base), base * 2 if chinese else base)


def find_stable_end(full: str, floor: int, pieces: list[bytes], keep_back: int) -> int:
    """按 token 回退：返回 ``full`` 里可以定稿的字符数（不小于 ``floor``）。

    必须按模型自己的 token 边界回退。按字符回退会把一个 token 切成两半（例如把「现在」切成
    「现」），模型接着半个词续写时会丢字、加标点——实测如此。
    另外不把英文单词从中间切开（唤醒匹配依赖「一个英文单词不会拆进两条增量」）：
    切点落在单词中间时继续左移。
    """
    if b"".join(pieces).decode("utf-8", "replace") != full:
        return floor  # 分词结果拼不回原文（不该发生），这一步不定稿
    for cut in range(len(pieces) - keep_back, 0, -1):
        try:
            end = len(b"".join(pieces[:cut]).decode("utf-8"))
        except UnicodeDecodeError:
            continue  # 切在了一个多字节字符中间
        if end <= floor:
            break
        if end < len(full) and _is_ascii_word(full[end - 1]) and _is_ascii_word(full[end]):
            continue  # 切在了英文单词中间
        return end
    return floor


def echo_len(text: str, hotwords: list[str]) -> int:
    """``text`` 开头若是在照抄热词表（从第一个热词起连续两个以上），返回照抄部分的字符数，否则 0。"""
    pos = matched = 0
    for word in hotwords:
        m = re.match(r"[\s,，、;；]*" + re.escape(word), text[pos:], re.IGNORECASE)
        if not m:
            break
        pos += m.end()
        matched += 1
    return pos if matched >= 2 else 0


def looks_like_repetition(text: str) -> bool:
    """同一个 1–6 字符的片段连续重复 6 次以上，视为幻觉。"""
    return bool(_REPETITION.search(text))


def clean_text(raw: str, profile: ASRProfile, language: str) -> str:
    """清洗模型续写：去掉替换字符，在截断标记处截断，中文识别时去掉相邻汉字之间的空格。"""
    text = raw.replace("�", "")
    for marker in profile.cut_markers:
        text = text.split(marker, 1)[0]
    if language == "Chinese":
        text = _SPACE_BETWEEN_CJK.sub("", text)
    return text


def extract_continuation(content: str, *, head: str, prefix_text: str, text_marker: str) -> str:
    """从服务端返回的 content 里剥掉前缀，剩下的才是续写。

    实测返回内容是「前缀 + 续写」；个别情况下前缀不是原样回显，退而按正文标记切。
    """
    full_prefix = head + prefix_text
    if content.startswith(full_prefix):
        return content[len(full_prefix) :]
    if text_marker and text_marker in content:
        content = content.split(text_marker, 1)[1]
        if content.startswith(prefix_text):
            content = content[len(prefix_text) :]
    return content


def wav_bytes(samples: np.ndarray) -> bytes:
    """16 kHz 单声道 16 位 WAV。采样值原样写入，不经过浮点。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(ASR_SAMPLE_RATE)
        w.writeframes(samples.astype("<i2", copy=False).tobytes())
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# 状态机（不涉及 IO）
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Request:
    """一步要发给服务端的内容，以及失败时回滚需要的快照。"""

    window: np.ndarray
    prefix_text: str
    max_tokens: int
    final: bool
    take: int  # 本步从 pending 取走的采样数
    audio_end_secs: float  # 发起本步时调用方给的最新值，原样带回增量
    taken: np.ndarray
    prev_window: np.ndarray
    prev_steps: list[tuple[int, str]]


@dataclass(frozen=True, slots=True)
class _Settle:
    """续写文本通过了复读、照抄两道检查，待分词后确定定稿位置。"""

    full: str


class _StreamState:
    """一路音频流的状态（含义见 docs/interfaces.md §3.2）。

    ``steps`` 的元素是 ``(本步新增采样数, 本步新定稿文字)``，与 ``window`` 里的音频一一对应；
    ``prefix_text`` 是它们文字的拼接。所有数组只会被整体替换、不会原地修改，回滚才可以直接换回快照。
    """

    def __init__(self, cfg: ASRConfig, profile: ASRProfile, hotwords: list[str]):
        self.cfg = cfg
        self.profile = profile
        self.hotwords = hotwords
        self.pending = _EMPTY
        self.window = _EMPTY
        self.steps: list[tuple[int, str]] = []
        self.unstable = ""
        self.latest_end_secs = 0.0

    @property
    def chunk_samples(self) -> int:
        return self.cfg.chunk_ms * ASR_SAMPLE_RATE // 1000

    @property
    def prefix_text(self) -> str:
        return "".join(text for _, text in self.steps)

    @property
    def head(self) -> str:
        """assistant 前缀的固定开头；识别语言为空时不加，由模型自己输出语言标记。"""
        language = self.cfg.language
        return self.profile.assistant_prefix.format(language=language) if language else ""

    def reset(self) -> None:
        self.pending = _EMPTY
        self.latest_end_secs = 0.0
        self.clear_segment()

    def clear_segment(self) -> None:
        self.window = _EMPTY
        self.steps = []
        self.unstable = ""

    def feed(self, pcm: np.ndarray, audio_end_secs: float) -> int:
        """收一段音频。返回因积压过多而丢掉的采样数（通常是 0）。

        追不上实时（pending 积压超过窗口长度）时，只保留最近 ``window_secs`` 的音频。
        """
        self.pending = np.concatenate([self.pending, pcm])
        self.latest_end_secs = audio_end_secs
        excess = len(self.pending) - int(self.cfg.window_secs * ASR_SAMPLE_RATE)
        if excess > 0:
            self.pending = self.pending[excess:]
            return excess
        return 0

    def backlog_secs(self) -> float:
        return len(self.pending) / ASR_SAMPLE_RATE

    def ready(self) -> bool:
        return len(self.pending) >= self.chunk_samples

    def begin(self, final: bool) -> _Request:
        """开始一步：吃掉已攒的音频（收尾时全部，否则最多 3 个步长），必要时滑动窗口，算 token 预算。"""
        take = len(self.pending)
        if not final:
            take = min(take, MAX_CHUNKS_PER_STEP * self.chunk_samples)
        taken, rest = self.pending[:take], self.pending[take:]
        prev_window, prev_steps = self.window, list(self.steps)
        self.pending = rest
        self.window = np.concatenate([self.window, taken])
        self._slide_window()
        prefix = self.prefix_text
        return _Request(
            window=self.window,
            prefix_text=prefix,
            max_tokens=token_budget(
                take,
                prefix,
                final=final,
                language=self.cfg.language,
                ceiling=self.cfg.max_new_tokens,
            ),
            final=final,
            take=take,
            audio_end_secs=self.latest_end_secs,
            taken=taken,
            prev_window=prev_window,
            prev_steps=prev_steps,
        )

    def _slide_window(self) -> None:
        """窗口超限：从头部丢掉音频，同时丢掉与之对应的那些步的文字。

        音频按弹出的 ``steps`` 的实际累计采样数砍，保证音频与文字始终对齐。
        """
        if len(self.window) <= int(self.cfg.window_secs * ASR_SAMPLE_RATE):
            return
        to_drop = int(self.cfg.window_drop_secs * ASR_SAMPLE_RATE)
        dropped = 0
        while self.steps and dropped < to_drop:
            n, _ = self.steps.pop(0)
            dropped += n
        self.window = self.window[dropped:]

    def rollback(self, req: _Request) -> None:
        """请求失败：把本步取走的音频放回 pending 最前面（失败期间新到的音频留在后面）。"""
        self.pending = np.concatenate([req.taken, self.pending])
        self.window = req.prev_window
        self.steps = req.prev_steps

    def interpret(self, req: _Request, raw_cont: str) -> ASRDelta | _Settle:
        """处理模型的续写。

        复读和照抄热词两种情况直接给出结果（状态已相应处理）；否则返回 ``_Settle``，
        由调用方分词后再调 :meth:`commit`。
        """
        cont = clean_text(raw_cont, self.profile, self.cfg.language)
        if looks_like_repetition(cont):
            # 复读保护：丢弃这次输出并重置窗口。
            self.clear_segment()
            return ASRDelta("", "", req.audio_end_secs, req.final)
        if not req.prefix_text:
            # 照抄热词的防护：段落开头音频还很短或接近静音时，模型有时会把 system 里的热词表原样念出来。
            # 一旦定稿就永远留在前缀里，所以非收尾的步直接作废（音频仍留在窗口里）；
            # 收尾时只去掉照抄的那一段。
            echoed = echo_len(cont, self.hotwords)
            if echoed and not req.final:
                self.steps.append((req.take, ""))
                self.unstable = ""
                return ASRDelta("", "", req.audio_end_secs, False)
            cont = cont[echoed:].lstrip(" ,，、")
        return _Settle(req.prefix_text + cont)

    def commit(self, req: _Request, full: str, stable_end: int) -> ASRDelta:
        stable_new = full[len(req.prefix_text) : stable_end]
        self.unstable = full[stable_end:]
        self.steps.append((req.take, stable_new))
        delta = ASRDelta(stable_new, self.unstable, req.audio_end_secs, req.final)
        if req.final:
            self.clear_segment()
        return delta


# --------------------------------------------------------------------------- #
# 后端
# --------------------------------------------------------------------------- #


class _RequestError(Exception):
    """一次 HTTP 请求没有得到可用的结果（连接失败、超时、非 2xx、返回格式不对）。"""


class _End:
    """放进队列的哨兵：迭代到这里结束。"""


_END = _End()


class LlamaServerASR:
    """``StreamingASR`` 的 llama-server 实现。

    ``client`` 供测试注入带 ``MockTransport`` 的客户端；不传则自建并在 :meth:`close` 时关闭。
    ``retry_base_secs`` 是失败后重试的退避起点（每次翻倍，上限 2 秒）。
    """

    def __init__(
        self,
        cfg: ASRConfig,
        profile: ASRProfile,
        hotwords: list[str],
        client: httpx.AsyncClient | None = None,
        *,
        retry_base_secs: float = 0.1,
    ):
        self.cfg = cfg
        self.profile = profile
        self.hotwords = list(hotwords)
        self._base = cfg.base_url.rstrip("/")
        self._retry_base_secs = retry_base_secs
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECS, connect=CONNECT_TIMEOUT_SECS),
            # 本机地址不走系统代理，否则设了 HTTP_PROXY 的机器上本机服务会被误判成不通。
            trust_env=not is_loopback(cfg.base_url),
        )
        self._state = _StreamState(cfg, profile, self.hotwords)
        self._queue: asyncio.Queue[ASRDelta | Exception | _End] = asyncio.Queue()
        self._lock = asyncio.Lock()  # 保证同一时刻只有一个请求在途（worker 的步与 flush 共用）
        self._kick = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._failures = 0
        self._dead = False  # 连续失败后置位；在下一次 start() 之前不再处理音频
        self._closed = False
        self._backlogged = False

    # ---- StreamingASR ----

    async def start(self) -> None:
        if self._closed:
            raise ASRBackendError("识别后端已关闭，不能再启动")
        await self._stop_worker()
        self._state.reset()
        self._failures = 0
        self._dead = False
        self._backlogged = False
        self._kick.clear()
        while not self._queue.empty():
            self._queue.get_nowait()
        try:
            await self._post("/v1/chat/completions", self._body(self._silence(), "", None))
        except _RequestError as e:
            raise ASRBackendError(f"识别服务预热失败：{e}") from e
        self._worker = asyncio.create_task(self._run(), name="asr-worker")

    async def push_audio(self, pcm16: bytes, audio_end_secs: float) -> None:
        if self._closed or self._dead:
            return
        samples = len(pcm16) // 2  # 奇数字节的最后一个字节凑不成采样，丢掉
        if not samples:
            return
        dropped = self._state.feed(np.frombuffer(pcm16, dtype="<i2", count=samples), audio_end_secs)
        if dropped:
            logger.warning(f"识别追不上实时，丢弃了最早的 {dropped / ASR_SAMPLE_RATE:.1f} 秒音频")
        backlog = self._state.backlog_secs()
        if backlog > BACKLOG_WARN_SECS and not self._backlogged:
            logger.warning(f"识别落后：已积压 {backlog:.1f} 秒音频")
        self._backlogged = backlog > BACKLOG_WARN_SECS
        self._kick.set()

    async def deltas(self) -> AsyncIterator[ASRDelta]:
        while True:
            item = await self._queue.get()
            if isinstance(item, _End):
                self._queue.put_nowait(item)  # 关闭之后再迭代也立即结束
                return
            if isinstance(item, Exception):
                raise item
            yield item

    async def flush(self) -> None:
        if self._closed or self._dead:
            return
        async with self._lock:  # 等在途的请求结束
            while not self._dead and not self._closed:
                if await self._step(final=True):
                    return
                await asyncio.sleep(self._backoff())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._stop_worker()
        self._queue.put_nowait(_END)
        if self._owns_client:
            await self._client.aclose()

    # ---- worker ----

    async def _stop_worker(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None and not worker.done():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    async def _run(self) -> None:
        try:
            while True:
                await self._kick.wait()
                self._kick.clear()
                while self._state.ready() and not self._dead:
                    async with self._lock:
                        if not self._state.ready():  # 等锁期间被 flush 取走了
                            break
                        ok = await self._step(final=False)
                    if not ok and not self._dead:
                        await asyncio.sleep(self._backoff())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # 不该发生；但不能让字幕无声地停止，消费者必须知道
            logger.exception("识别后端内部错误")
            self._fail(ASRBackendError(f"识别后端内部错误：{e!r}"))

    def _backoff(self) -> float:
        return min(MAX_RETRY_DELAY_SECS, self._retry_base_secs * 2 ** max(0, self._failures - 1))

    def _fail(self, error: ASRBackendError) -> None:
        self._dead = True
        self._queue.put_nowait(error)

    async def _step(self, *, final: bool) -> bool:
        """做一步（调用时必须持有 ``_lock``）。

        成功返回 True；请求失败时状态已回滚，返回 False，由调用方退避后重试。
        连续失败达到上限则让迭代器抛错。
        """
        req = self._state.begin(final)
        try:
            raw = await self._complete(req) if len(req.window) else ""
            outcome = self._state.interpret(req, raw)
            if isinstance(outcome, _Settle):
                full = outcome.full
                if final:
                    end = len(full)
                elif len(full) == len(req.prefix_text):
                    end = len(full)  # 没有新文字，不必分词
                else:
                    pieces = await self._tokenize(full)
                    end = find_stable_end(
                        full, len(req.prefix_text), pieces, self.cfg.unfixed_tokens
                    )
                outcome = self._state.commit(req, full, end)
        except _RequestError as e:
            self._state.rollback(req)
            self._failures += 1
            logger.warning(f"识别请求失败（连续第 {self._failures} 次）：{e}")
            if self._failures >= MAX_CONSECUTIVE_FAILURES:
                self._fail(ASRBackendError(f"识别服务连续 {self._failures} 次请求失败：{e}"))
            return False
        self._failures = 0
        self._queue.put_nowait(outcome)
        return True

    # ---- 与服务端的往返 ----

    def _silence(self) -> np.ndarray:
        return np.zeros(int(WARMUP_SECS * ASR_SAMPLE_RATE), dtype=np.int16)

    def _body(self, window: np.ndarray, prefix_text: str, max_tokens: int | None) -> dict:
        hotwords = self.profile.hotwords_joiner.join(self.hotwords)
        system = self.profile.hotwords_template.format(hotwords=hotwords) if self.hotwords else ""
        audio = base64.b64encode(wav_bytes(window)).decode("ascii")
        return {
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {"type": "input_audio", "input_audio": {"data": audio, "format": "wav"}}
                    ],
                },
                {"role": "assistant", "content": self._state.head + prefix_text},
            ],
            "temperature": 0,
            "max_tokens": max_tokens or self.cfg.max_new_tokens,
            "stream": False,
        }

    async def _post(self, path: str, body: dict) -> dict:
        try:
            response = await self._client.post(self._base + path, json=body)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            raise _RequestError(str(e) or type(e).__name__) from e
        except ValueError as e:
            raise _RequestError(f"返回的不是 JSON：{e}") from e

    async def _complete(self, req: _Request) -> str:
        data = await self._post(
            "/v1/chat/completions", self._body(req.window, req.prefix_text, req.max_tokens)
        )
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise _RequestError(f"返回格式不对：{e!r}") from e
        return extract_continuation(
            content,
            head=self._state.head,
            prefix_text=req.prefix_text,
            text_marker=self.profile.text_marker,
        )

    async def _tokenize(self, text: str) -> list[bytes]:
        data = await self._post(
            "/tokenize", {"content": text, "add_special": False, "with_pieces": True}
        )
        try:
            return [
                t["piece"].encode("utf-8") if isinstance(t["piece"], str) else bytes(t["piece"])
                for t in data["tokens"]
            ]
        except (KeyError, TypeError, ValueError) as e:
            raise _RequestError(f"分词返回格式不对：{e!r}") from e
