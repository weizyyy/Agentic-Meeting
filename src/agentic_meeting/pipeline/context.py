"""实时模型的上下文管理（docs/architecture.md §6）。

1 秒应答靠的是实时模型每次只处理**新增**的那一点上下文，所以这里管三件事：

* **估算长度**：按「中文 1 字 ≈ 1 token、其余 4 个字符 ≈ 1 token」粗估，不调分词接口。图片按固定的 token 数算。
* **压缩**：估算值超过 ``realtime.context_budget_tokens`` 且助理空闲时，把上下文整体换成
  「最新一份滚动纪要 + 最近 ``keep_recent_minutes`` 分钟的转录行和画面行」（从数据库重新生成），然后立刻预热。
  系统提示词和工具定义不在消息列表里，不受影响。
* **预热**：只在 ``cfg.realtime_llm.cache_warm`` 为真时做。用与正式请求**完全相同**的消息和工具定义发一次只生成
  1 个 token 的请求，让服务端把前缀先算好。触发点：检测到唤醒词的那一刻（用户还要说几秒才说完）、压缩之后、
  以及每 ``cache_warm_interval_secs`` 的定时器（上下文自上次预热以来没变就跳过）。

互斥：助理正在生成或朗读时不预热、不压缩；压缩还要求助理完全空闲（没有被叫到名字、没有待答的文字请求）。

``build_context_messages`` 单独写成一个函数：继续会议时用同一段代码重建上下文。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from loguru import logger
from pipecat.processors.aggregators.llm_context import LLMContext

from agentic_meeting.pipeline.background import Preempted
from agentic_meeting.pipeline.clock import context_line
from agentic_meeting.screen.caption import IRRELEVANT_CAPTION, screen_line
from agentic_meeting.store.db import Store
from agentic_meeting.types import TaskRecord

DIGEST_PREFIX = "【此前会议纪要】"
NO_DIGEST_NOTE = "（较早的讨论还没有纪要，需要时用 recall 查找）"
IMAGE_TOKENS = 1500  # 上下文里每张图片按这么多 token 估（实际取决于模型和分辨率）
MESSAGE_OVERHEAD_TOKENS = 4  # 每条消息的角色标记等
CHECK_INTERVAL_SECS = 5.0
COMPACT_COOLDOWN_SECS = 60.0  # 两次压缩之间至少隔这么久，免得压不下去时反复重试
MIN_KEEP_RECENT_SECS = 60.0
TARGET_FRACTION = 0.8  # 压缩后希望不超过预算的这个比例，留出继续追加的余地


# --------------------------------------------------------------------------- #
# 估算
# --------------------------------------------------------------------------- #


def _is_wide(ch: str) -> bool:
    """中日韩文字、假名、全角标点：大致一个字符一个 token。"""
    code = ord(ch)
    return (
        0x2E80 <= code <= 0x9FFF
        or 0xAC00 <= code <= 0xD7AF
        or 0xF900 <= code <= 0xFAFF
        or 0xFF00 <= code <= 0xFFEF
        or 0x3000 <= code <= 0x303F
        or code >= 0x20000
    )


def estimate_tokens(text: str) -> int:
    """粗估 token 数：中文 1 字 ≈ 1 token，其余 4 个字符 ≈ 1 token（向上取整）。"""
    wide = sum(1 for ch in text if _is_wide(ch))
    return wide + (len(text) - wide + 3) // 4


def estimate_message(message: Any) -> int:
    if not isinstance(message, dict):
        return MESSAGE_OVERHEAD_TOKENS
    total = MESSAGE_OVERHEAD_TOKENS
    content = message.get("content")
    if isinstance(content, str):
        total += estimate_tokens(content)
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                total += estimate_tokens(str(part.get("text", "")))
            elif part.get("type") in ("image_url", "image"):
                total += IMAGE_TOKENS  # 不去数 base64 的长度
    for call in message.get("tool_calls") or []:
        function = call.get("function", {}) if isinstance(call, dict) else {}
        total += estimate_tokens(str(function.get("name", ""))) + estimate_tokens(
            str(function.get("arguments", ""))
        )
    return total


def estimate_messages(messages: list) -> int:
    return sum(estimate_message(m) for m in messages)


# --------------------------------------------------------------------------- #
# 从数据库重建上下文
# --------------------------------------------------------------------------- #


async def build_context_messages(
    store: Store, session_id: str, now_secs: float, *, keep_recent_secs: float
) -> list[dict[str, Any]]:
    """压缩后（或继续会议时）的上下文消息：最新纪要 + 纪要没覆盖到的和最近 ``keep_recent_secs`` 秒的原文。

    * 纪要作为一条用户消息，前缀 ``DIGEST_PREFIX``。
    * 原文从「纪要已经纳入到哪条发言」和「最近若干分钟的起点」两者里**更早**的那个开始，中间不留空档。
      还没有纪要、而较早的内容又被丢掉时，放一条说明，让模型知道可以用 recall 去找。
    * 发言一行一条用户消息 ``[时:分:秒 说话人] 文字``；助理自己说过的话是 assistant 消息（和平时聚合器写进去的一样）；
      画面摘要是 ``[画面 时:分:秒] 摘要``（连续相同的只留第一条，「无关画面」不要）。
    * 最近这段时间里做完的后台任务各一行：``[任务 t2 完成] 结论`` / ``[任务 t2 失败] 原因``
      （时间按任务结束的时刻算；还在做的任务不写，它做完时结果会自己送到）。
    * 工具调用的记录和看过的截图不保留。
    """
    cutoff = max(0.0, now_secs - keep_recent_secs)
    digest = await store.latest_digest(session_id)
    messages: list[dict[str, Any]] = []
    if digest is not None:
        messages.append({"role": "user", "content": f"{DIGEST_PREFIX}\n{digest.text}"})
        frames_from = min(cutoff, digest.t_to)
        utterances = await store.utterances_for_context(
            session_id, after_id=digest.last_utterance_id, t_from=cutoff
        )
    else:
        frames_from = cutoff
        utterances = await store.utterances_for_context(session_id, after_id=None, t_from=cutoff)
        if cutoff > 0:
            messages.append({"role": "user", "content": f"{DIGEST_PREFIX}\n{NO_DIGEST_NOTE}"})

    lines: list[tuple[float, int, dict[str, Any]]] = []
    for item in utterances:
        u = item.utterance
        if u.source == "assistant":
            message = {"role": "assistant", "content": u.text}
        else:
            message = {
                "role": "user",
                "content": context_line(u.t_start, item.speaker_name, u.text),
            }
        lines.append((u.t_start, u.id or 0, message))
    last_caption: str | None = None
    for frame in await store.list_frames(session_id, t_from=frames_from):
        caption = (frame.caption or "").strip()
        if not caption or caption == IRRELEVANT_CAPTION or caption == last_caption:
            continue
        last_caption = caption
        # 同一时刻的发言排在画面行前面（-1 之外的发言编号都是正的，画面用很大的序号垫后）
        lines.append((frame.t, 1 << 60, {"role": "user", "content": screen_line(frame.t, caption)}))
    session = await store.get_session(session_id)
    for task in await store.list_tasks(session_id):
        if not task.finished or task.finished_at is None or session is None:
            continue
        t = task.finished_at - session.started_at
        if t < cutoff:
            continue
        lines.append((t, (1 << 60) + 1, {"role": "user", "content": task_line(task)}))
    lines.sort(key=lambda x: (x[0], x[1]))
    messages.extend(message for _, _, message in lines)
    return messages


def task_line(task: TaskRecord) -> str:
    """做完的后台任务在上下文里的一行（architecture.md §6.1；与 pipeline/async_tools.py 改写出来的一致）。"""
    if task.status == "succeeded":
        return f"[任务 {task.label} 完成] {task.brief or '已完成，详情见任务面板'}"
    if task.status == "cancelled":
        return f"[任务 {task.label} 已取消] {task.error or ''}".rstrip()
    return f"[任务 {task.label} 失败] {task.error or '没有做完'}"


# --------------------------------------------------------------------------- #
# 管理器
# --------------------------------------------------------------------------- #


class WarmableLLM(Protocol):
    """用到的实时模型服务接口（``RealtimeLLMService`` 满足它）。"""

    def static_prompt_tokens_text(self, context: LLMContext) -> str: ...

    async def warm_cache(self, context: LLMContext) -> None: ...


class ContextRecorder(Protocol):
    """用到的记录器接口：重建上下文必须经过它，和追加行走同一个顺序。"""

    @property
    def elapsed_secs(self) -> float: ...

    async def rebuild_context(
        self, build: Callable[[], Awaitable[list[dict[str, Any]]]]
    ) -> int: ...


class ActivityView(Protocol):
    @property
    def busy(self) -> bool: ...

    @property
    def responding(self) -> bool: ...


class ContextManager:
    def __init__(
        self,
        *,
        llm: WarmableLLM,
        context: LLMContext,
        store: Store | None,
        session_id: str,
        recorder: ContextRecorder,
        activity: ActivityView,
        budget_tokens: int,
        keep_recent_secs: float,
        cache_warm: bool,
        warm_interval_secs: float,
        digests: Any = None,
        check_interval_secs: float = CHECK_INTERVAL_SECS,
        compact_cooldown_secs: float = COMPACT_COOLDOWN_SECS,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        """``digests`` 是滚动纪要（``DigestWorker``，可以没有）：压缩前先让它把纪要补到最新。"""
        self._llm = llm
        self._context = context
        self._store = store
        self._session_id = session_id
        self._recorder = recorder
        self._activity = activity
        self._budget = budget_tokens
        self._keep_recent = keep_recent_secs
        self._cache_warm = cache_warm
        self._warm_interval = warm_interval_secs
        self._digests = digests
        self._check_interval = check_interval_secs
        self._cooldown = compact_cooldown_secs
        self._now = now

        self._static_tokens: int | None = None  # 系统提示词 + 工具定义，算一次
        self._warming = False
        self._compacting = False
        self._last_warm_at: float | None = None
        self._warmed_fingerprint: tuple[int, int] | None = None
        self._last_compact_at: float | None = None
        self._task: asyncio.Task | None = None
        self._spawned: set[asyncio.Task] = set()

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="context-manager")

    async def stop(self) -> None:
        tasks = [t for t in (self._task, *self._spawned) if t is not None]
        self._task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def on_wake(self) -> None:
        """检测到唤醒词：趁用户还没说完，先把前缀算好。不等它。"""
        self.warm_soon("唤醒")

    def warm_soon(self, reason: str) -> None:
        """在后台预热一次，不等它（没开预热就什么都不做）。"""
        if not self._cache_warm:
            return
        task = asyncio.create_task(self.warm(reason), name="context-warm")
        self._spawned.add(task)
        task.add_done_callback(self._spawned.discard)

    # ---- 估算 ----

    def estimate(self) -> int:
        """当前上下文的估算 token 数（系统提示词 + 工具定义 + 全部消息）。"""
        if self._static_tokens is None:
            try:
                self._static_tokens = estimate_tokens(
                    self._llm.static_prompt_tokens_text(self._context)
                )
            except Exception:
                logger.exception("估算系统提示词和工具定义的长度失败，按 0 算")
                self._static_tokens = 0
        return self._static_tokens + estimate_messages(self._context.get_messages())

    def _fingerprint(self) -> tuple[int, int]:
        messages = self._context.get_messages()
        return len(messages), estimate_messages(messages)

    # ---- 预热 ----

    async def warm(self, reason: str = "定时") -> bool:
        """发一次预热请求。没开预热、助理正在生成或朗读、已有一次预热在途、上下文是空的——都不发。"""
        if not self._cache_warm or self._warming or self._activity.responding:
            return False
        if not self._context.get_messages():
            return False
        self._warming = True
        fingerprint = self._fingerprint()
        started = self._now()
        try:
            await self._llm.warm_cache(self._context)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                f"上下文预热失败（不影响应答，只是下一次首字会慢一些）：{type(e).__name__}: {e}"
            )
            return False
        finally:
            self._warming = False
            self._last_warm_at = self._now()
        self._warmed_fingerprint = fingerprint
        logger.debug(f"上下文预热完成（{reason}），用时 {self._now() - started:.2f} 秒")
        return True

    async def _warm_if_due(self) -> None:
        if not self._cache_warm or self._activity.responding:
            return
        if (
            self._last_warm_at is not None
            and self._now() - self._last_warm_at < self._warm_interval
        ):
            return
        if self._fingerprint() == self._warmed_fingerprint:
            self._last_warm_at = self._now()  # 没有新内容：服务端的缓存还是那一份，不用再发
            return
        await self.warm("定时")

    # ---- 压缩 ----

    async def maybe_compact(self) -> bool:
        """超预算且助理空闲时压缩一次。返回是否真的压缩了。"""
        if self._compacting or self._store is None or self._activity.busy:
            return False
        if (
            self._last_compact_at is not None
            and self._now() - self._last_compact_at < self._cooldown
        ):
            return False
        before = self.estimate()
        if before <= self._budget:
            return False
        self._compacting = True
        try:
            await self._refresh_digest()
            if self._activity.busy:
                return False  # 补纪要的工夫助理被叫到了：让路，下次再压
            built: list[dict[str, Any]] = []

            async def build() -> list[dict[str, Any]]:
                built[:] = await self._build_within_budget()
                return built

            await self._recorder.rebuild_context(build)
            self._last_compact_at = self._now()
            after = (self._static_tokens or 0) + estimate_messages(built)
            logger.info(f"上下文已压缩：估算 {before} → {after} token（预算 {self._budget}）")
            if after > self._budget:
                logger.warning(
                    "压缩之后仍然超过预算：最近的原文本身就很长。"
                    "可以调大 realtime.context_budget_tokens 或调小 realtime.keep_recent_minutes"
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("上下文压缩失败，保持原样")
            self._last_compact_at = self._now()
            return False
        finally:
            self._compacting = False
        await self.warm("压缩后")
        return True

    async def _refresh_digest(self) -> None:
        """压缩前把滚动纪要补到最新（没有后台模型、被抢占、出错都不拦着压缩）。"""
        if self._digests is None:
            return
        try:
            await self._digests.catch_up(self._session_id)
        except Preempted:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"压缩前补纪要失败，用已有的纪要：{type(e).__name__}: {e}")

    async def _build_within_budget(self) -> list[dict[str, Any]]:
        """重建上下文；最近的原文太长时把保留时长逐次减半，直到放得下或只剩一分钟。"""
        assert self._store is not None
        static = self._static_tokens or 0
        target = max(1, int(self._budget * TARGET_FRACTION))
        keep = self._keep_recent
        while True:
            messages = await build_context_messages(
                self._store, self._session_id, self._recorder.elapsed_secs, keep_recent_secs=keep
            )
            if static + estimate_messages(messages) <= target or keep <= MIN_KEEP_RECENT_SECS:
                return messages
            keep = max(MIN_KEEP_RECENT_SECS, keep / 2)

    # ---- 定时 ----

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._check_interval)
            try:
                if not await self.maybe_compact():
                    await self._warm_if_due()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("上下文管理的定时检查出错")


def static_prompt_text(system_instruction: str, tools: Any) -> str:
    """系统提示词 + 工具定义拼成的文本（只用来估算长度）。"""
    try:
        tools_text = json.dumps(tools, ensure_ascii=False) if tools else ""
    except (TypeError, ValueError):
        tools_text = str(tools)
    return (system_instruction or "") + tools_text
