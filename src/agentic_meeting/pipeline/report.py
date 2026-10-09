"""会后报告（接口见 docs/interfaces.md §5.6）。

``ReportWorker.start(session_id)`` 建一行 ``running`` 的报告并在后台生成，立刻返回报告编号；
同一场会议同一时间只允许一份在生成。生成的步骤：

1. 先让滚动纪要补到最后（有的话），报告和纪要看到的是同一份材料；
2. 转录不长（不超过 ``report.max_input_chars``）就把整份转录交给模型，一次生成；
3. 太长则分段：按滚动纪要的时间窗口和字数上限切开，先对每一段提要点，再把各段要点合并成报告；
4. 结果写回 ``reports`` 表（``done``），出错写 ``failed`` 和原因。

模型是「后台模型的入口」（``pipeline/background.py``）：助理正在应答时会被抢占，这里等它空下来再重试那一次请求。
报告开头的标题和时间由代码写（这些是确定的事实，不让模型转述），正文才是模型写的。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Protocol

from loguru import logger

from agentic_meeting.pipeline.background import Preempted
from agentic_meeting.pipeline.clock import format_hms
from agentic_meeting.screen.caption import IRRELEVANT_CAPTION
from agentic_meeting.store.db import Store
from agentic_meeting.types import NamedUtterance, ScreenFrame, SessionSummary, TaskRecord

REPORT_MAX_TOKENS = 4096
SECTION_MAX_TOKENS = 1024
REPORT_TIMEOUT_SECS = 900.0
FINAL_DIGEST_TIMEOUT_SECS = 120.0
MAX_PREEMPTIONS = 30  # 一次请求最多被实时应答打断这么多回，再多就算失败
MAX_FRAME_LINES = 60
NOTHING = "（没有）"
TYPED_TAG = "（打字）"
INTERRUPTED_REASON = "服务停止了，这份报告没有生成完"

_TASK_STATUS = {
    "queued": "排队中",
    "running": "进行中",
    "succeeded": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}


class ReportBusy(RuntimeError):
    """这场会议已经有一份报告正在生成。"""


class ReportUnavailable(RuntimeError):
    """没有可用的模型，生成不了报告。"""


class ReportModel(Protocol):
    async def run(
        self, messages: Sequence[dict[str, Any]], *, system: str = "", max_tokens: int
    ) -> str: ...

    async def wait_resumed(self) -> None: ...


# --------------------------------------------------------------------------- #
# 把数据整理成给模型看的文字（纯函数）
# --------------------------------------------------------------------------- #


def transcript_lines(
    utterances: Sequence[NamedUtterance], frames: Sequence[ScreenFrame]
) -> list[tuple[float, str]]:
    """发言和画面摘要按时间排成行：``(会话时间, 文字)``。

    发言是 ``[时:分:秒 说话人] 文字``，键入的文字在名字后面标「（打字）」；画面是 ``[画面 时:分:秒] 摘要``
    （连续相同的只留第一条，「无关画面」和没有摘要的不出现）。
    """
    rows: list[tuple[float, int, str]] = []
    for item in utterances:
        u = item.utterance
        name = item.speaker_name + (TYPED_TAG if u.source == "text" else "")
        rows.append((u.t_start, 0, f"[{format_hms(u.t_start)} {name}] {u.text}"))
    last_caption: str | None = None
    for frame in sorted(frames, key=lambda f: (f.t, f.id or 0)):
        caption = (frame.caption or "").strip()
        if not caption or caption == IRRELEVANT_CAPTION or caption == last_caption:
            continue
        last_caption = caption
        rows.append((frame.t, 1, f"[画面 {format_hms(frame.t)}] {caption}"))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [(t, text) for t, _, text in rows]


def split_sections(
    lines: Sequence[tuple[float, str]], boundaries: Sequence[float], max_chars: int
) -> list[list[tuple[float, str]]]:
    """把转录切成若干段，每段不超过 ``max_chars`` 个字（单独一行就超了的那一行自成一段）。

    ``boundaries`` 是滚动纪要各个时间窗口的终点：一段已经有半满、又正好跨过一个窗口的边界时，就在那里切开，
    这样段落大致和议题的自然段落对齐；否则写满了才切。
    """
    edges = sorted(boundaries)
    sections: list[list[tuple[float, str]]] = []
    current: list[tuple[float, str]] = []
    size = 0
    edge = 0
    for t, text in lines:
        crossed = False
        while edge < len(edges) and t >= edges[edge]:
            edge += 1
            crossed = True
        cost = len(text) + 1
        if current and (size + cost > max_chars or (crossed and size * 2 >= max_chars)):
            sections.append(current)
            current, size = [], 0
        current.append((t, text))
        size += cost
    if current:
        sections.append(current)
    return sections


def tasks_text(tasks: Sequence[TaskRecord]) -> str:
    """后台任务的清单：短编号、状态、目标、结论或原因。"""
    if not tasks:
        return NOTHING
    out = []
    for task in tasks:
        line = f"{task.label}（{_TASK_STATUS.get(task.status, task.status)}）目标：{' '.join(task.goal.split())}"
        if task.status == "succeeded" and task.brief:
            line += f"；结论：{task.brief}"
        elif task.error:
            line += f"；原因：{task.error}"
        out.append(line)
    return "\n".join(out)


def frames_text(frames: Sequence[ScreenFrame], limit: int = MAX_FRAME_LINES) -> str:
    """截图索引的素材：时间 + 摘要（连续相同的只留第一条）。太多时只留前 ``limit`` 条并注明。"""
    rows: list[str] = []
    last: str | None = None
    for frame in sorted(frames, key=lambda f: (f.t, f.id or 0)):
        caption = (frame.caption or "").strip()
        if not caption or caption == IRRELEVANT_CAPTION or caption == last:
            continue
        last = caption
        rows.append(f"{format_hms(frame.t)} {caption}")
    if not rows:
        return NOTHING
    if len(rows) > limit:
        rows = [*rows[:limit], f"（后面还有 {len(rows) - limit} 张，从略）"]
    return "\n".join(rows)


def format_duration(secs: float) -> str:
    minutes = int(max(0.0, secs) // 60)
    if minutes < 1:
        return "不到 1 分钟"
    if minutes < 60:
        return f"{minutes} 分钟"
    return f"{minutes // 60} 小时 {minutes % 60:02d} 分"


def meeting_title(summary: SessionSummary, tz: Any = None) -> str:
    title = summary.session.title.strip()
    started = datetime.fromtimestamp(summary.session.started_at, tz).strftime("%Y-%m-%d %H:%M")
    return title or f"未命名会议 {started}"


def meeting_info(summary: SessionSummary, tz: Any = None) -> str:
    """给模型看的基本信息（也用在报告开头）。"""
    session = summary.session
    lines = [
        f"标题：{meeting_title(summary, tz)}",
        f"开始时间：{datetime.fromtimestamp(session.started_at, tz).strftime('%Y-%m-%d %H:%M')}",
        f"实际时长：{format_duration(summary.duration_secs)}",
        f"发言人：{'、'.join(summary.speakers) if summary.speakers else '未提及'}",
    ]
    return "\n".join(lines)


def report_header(summary: SessionSummary, *, created_at: float, tz: Any = None) -> str:
    """报告开头：标题和确定的事实，由代码写。"""
    session = summary.session
    lines = [
        f"# {meeting_title(summary, tz)} · 会后报告",
        "",
        f"- 开始时间：{datetime.fromtimestamp(session.started_at, tz).strftime('%Y-%m-%d %H:%M')}",
        f"- 实际时长：{format_duration(summary.duration_secs)}",
    ]
    if summary.speakers:
        lines.append(f"- 发言人：{'、'.join(summary.speakers)}")
    lines.append(
        f"- 报告生成于：{datetime.fromtimestamp(created_at, tz).strftime('%Y-%m-%d %H:%M')}"
    )
    return "\n".join(lines)


def failure_reason(error: BaseException) -> str:
    if isinstance(error, TimeoutError):
        return "生成超时"
    text = " ".join(str(error).split())
    if len(text) > 200:
        text = text[:200] + "…"
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #


class ReportWorker:
    def __init__(
        self,
        *,
        store: Store,
        model: ReportModel | None,
        provider: str,
        render_report: Callable[..., str],
        render_section: Callable[..., str],
        max_input_chars: int,
        digests: Any = None,
        attacher: Any = None,
        max_tokens: int = REPORT_MAX_TOKENS,
        section_max_tokens: int = SECTION_MAX_TOKENS,
        timeout_secs: float = REPORT_TIMEOUT_SECS,
        now: Callable[[], float] = time.time,
        tz: Any = None,
    ) -> None:
        """``render_report(**变量)`` / ``render_section(**变量)`` 给出完整的提示词
        （``config/prompts/report.md``、``report_section.md``）；``digests`` 是滚动纪要（可以没有）。
        ``attacher``（``screen/attach.py`` 的 ``FrameAttacher``）给了就把截图原图附在请求里：转录不长时附在
        写报告的那一次请求上；分段提要点时各段附自己那段时间里的截图，最后合并的那一次不再附。
        """
        self._store = store
        self._model = model
        self._provider = provider
        self._render_report = render_report
        self._render_section = render_section
        self._max_chars = max_input_chars
        self._digests = digests
        self._attacher = attacher
        self._max_tokens = max_tokens
        self._section_max_tokens = section_max_tokens
        self._timeout = timeout_secs
        self._now = now
        self._tz = tz
        self._running: dict[str, asyncio.Task | None] = {}  # 会议编号 → 正在生成的任务

    @property
    def enabled(self) -> bool:
        return self._model is not None

    def is_running(self, session_id: str) -> bool:
        return session_id in self._running

    async def recover(self) -> int:
        """服务启动时调用：上次没生成完的报告标为失败。"""
        return await self._store.fail_running_reports(INTERRUPTED_REASON)

    async def stop(self) -> None:
        tasks = [t for t in self._running.values() if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def wait(self, session_id: str) -> None:
        """等这场会议正在生成的那份报告结束（测试用）。"""
        task = self._running.get(session_id)
        if task is not None:
            await asyncio.wait({task})

    async def start(self, session_id: str) -> int:
        """开始生成一份报告，返回报告编号。没有模型抛 ``ReportUnavailable``，已有一份在生成抛 ``ReportBusy``。"""
        if self._model is None:
            raise ReportUnavailable("没有可用的模型，生成不了报告")
        if session_id in self._running:
            raise ReportBusy(session_id)
        # 先占位再写库：两次请求同时进来时，第二个在上面那一行就被拦下
        self._running[session_id] = None
        try:
            report_id = await self._store.create_report(
                session_id, provider=self._provider, now=self._now()
            )
        except BaseException:
            self._running.pop(session_id, None)
            raise
        task = asyncio.create_task(self._run(session_id, report_id), name="meeting-report")
        self._running[session_id] = task

        def _done(finished: asyncio.Task) -> None:
            if self._running.get(session_id) is finished:
                del self._running[session_id]

        task.add_done_callback(_done)
        return report_id

    async def _run(self, session_id: str, report_id: int) -> None:
        started = self._now()
        try:
            text = await asyncio.wait_for(self.generate(session_id, started), self._timeout)
            await self._store.finish_report(report_id, text)
            logger.info(f"会后报告已生成（{len(text)} 字，用时 {self._now() - started:.0f} 秒）")
        except asyncio.CancelledError:
            await self._mark_failed(report_id, INTERRUPTED_REASON)
            raise
        except Exception as e:
            logger.warning(f"会后报告生成失败：{type(e).__name__}: {e}")
            await self._mark_failed(report_id, failure_reason(e))

    async def _mark_failed(self, report_id: int, reason: str) -> None:
        try:
            await self._store.fail_report(report_id, reason)
        except Exception:
            logger.exception("把报告标为失败时出错")

    async def generate(self, session_id: str, created_at: float | None = None) -> str:
        """生成报告的全文（不写库）。会议不存在、没有任何发言时抛 ``ValueError``。"""
        if self._model is None:
            raise ReportUnavailable("没有可用的模型，生成不了报告")
        await self._final_digest(session_id)
        summary = await self._store.get_summary(session_id)
        if summary is None:
            raise ValueError("找不到这场会议")
        utterances = await self._store.all_utterances(session_id)
        if not utterances:
            raise ValueError("这场会议没有发言记录，写不出报告")
        frames = await self._store.list_frames(session_id)
        attached: list[ScreenFrame] = []  # 附在最后那一次请求上的截图
        tasks = await self._store.list_tasks(session_id)
        digests = await self._store.list_digests(session_id)
        lines = transcript_lines(utterances, frames)
        total = sum(len(text) + 1 for _, text in lines)

        if total <= self._max_chars:
            material_name = "完整的会议转录"
            material = "\n".join(text for _, text in lines)
            attached = frames
        else:
            sections = split_sections(lines, [d.t_to for d in digests], self._max_chars)
            logger.info(
                f"转录有 {total} 字，超过单次上限 {self._max_chars}，分 {len(sections)} 段提要点"
            )
            notes = []
            for index, section in enumerate(sections, start=1):
                span = f"{format_hms(section[0][0])}–{format_hms(section[-1][0])}"
                prompt = self._render_section(
                    part=index,
                    total=len(sections),
                    span=span,
                    transcript="\n".join(text for _, text in section),
                )
                in_span = [f for f in frames if section[0][0] <= f.t <= section[-1][0]]
                note = await self._ask(prompt, self._section_max_tokens, in_span)
                if not note:
                    raise RuntimeError(f"模型对第 {index} 段没有给出要点")
                notes.append(f"【第 {index} 段 {span}】\n{note}")
            material_name = f"各段的要点（转录太长，已经分成 {len(sections)} 段分别提过要点）"
            material = "\n\n".join(notes)

        prompt = self._render_report(
            meeting_info=meeting_info(summary, self._tz),
            digest=digests[-1].text.strip() if digests else NOTHING,
            material_name=material_name,
            material=material,
            tasks=tasks_text(tasks),
            frames=frames_text(frames),
        )
        body = await self._ask(prompt, self._max_tokens, attached)
        if not body:
            raise RuntimeError(
                "模型给出的报告是空的。如果这个模型会先思考，输出额度可能被思考用光了："
                "把 agent.generation_max_tokens 调大，或者在 extra_body 里把思考的强度调低"
            )
        header = report_header(
            summary, created_at=self._now() if created_at is None else created_at, tz=self._tz
        )
        return f"{header}\n\n{body}\n"

    async def _ask(self, prompt: str, max_tokens: int, frames: Sequence[ScreenFrame] = ()) -> str:
        """发一次请求；被实时应答抢占就等助理空下来再发。``frames`` 是要附上原图的截图（没配附图就不附）。"""
        assert self._model is not None
        message: dict[str, Any] = {"role": "user", "content": prompt}
        if self._attacher is not None and frames:
            message = await self._attacher.user_message(prompt, frames)
        for _ in range(MAX_PREEMPTIONS):
            try:
                return await self._model.run([message], max_tokens=max_tokens)
            except Preempted:
                await self._model.wait_resumed()
        raise RuntimeError("助理一直在应答，报告的请求反复被打断")

    async def _final_digest(self, session_id: str) -> None:
        """报告之前把滚动纪要补到最后。补不上不拦着报告。"""
        if self._digests is None:
            return
        try:
            await asyncio.wait_for(self._digests.catch_up(session_id), FINAL_DIGEST_TIMEOUT_SECS)
        except Preempted:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"报告之前补纪要失败，用已有的纪要：{type(e).__name__}: {e}")
