"""导出（docs/interfaces.md §5.6）：Markdown 转录、结构化 JSON 和完整的压缩包。

Markdown 的结构：标题与时间 → 纪要 → 按时间排列的发言（相邻同一说话人的合并成段；键入的文字标注「（文字）」）
和画面（摘要 + 图片的相对路径）→ 后台任务的结果（目标、结论、详细结果、来源、产物文件的相对路径）。

图片用相对路径 ``frames/<文件名>`` 引用：和压缩包里的布局一致，解压后直接能看图；
单独下载这份 Markdown 时图片不显示，但摘要文字在。

``render_markdown`` 是纯函数（给定数据 → 文本），接口只负责取数据和设置下载用的响应头。
"""

from __future__ import annotations

import json
import re
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agentic_meeting.pipeline.clock import format_hms
from agentic_meeting.screen.caption import IRRELEVANT_CAPTION
from agentic_meeting.types import (
    Connection,
    Digest,
    NamedUtterance,
    Report,
    ScreenFrame,
    SessionSummary,
    SpeakerInfo,
    TaskEvent,
    TaskRecord,
)

MERGE_GAP_SECS = 120.0  # 同一个人隔了这么久才接着说，另起一段（时间戳才有参考价值）
NO_DIGEST = "（这场会议没有生成纪要。）"
NO_TRANSCRIPT = "（这场会议没有发言记录。）"
_ESCAPE = re.compile(r"([\\`*_\[\]<>#|])")
_ASCII_WORD = re.compile(r"[A-Za-z0-9]")


def escape(text: str) -> str:
    """让转录文字在 Markdown 里按原样显示（识别结果里偶尔会有 * _ # 之类的符号）。"""
    return _ESCAPE.sub(r"\\\1", text)


def plain_block(text: str) -> str:
    """把一段纯文本放进 Markdown：符号转义，原来的换行保留（行尾两个空格是 Markdown 的强制换行）。

    后台任务的详细结果是纯文本（提示词要求不用 Markdown 符号）；万一模型还是写了，转义之后也只是按字面显示。
    """
    paragraphs: list[str] = []
    current: list[str] = []
    for line in text.strip().splitlines():
        line = line.strip()
        if line:
            current.append(escape(line))
        elif current:
            paragraphs.append("  \n".join(current))
            current = []
    if current:
        paragraphs.append("  \n".join(current))
    return "\n\n".join(paragraphs)


def join_text(first: str, second: str) -> str:
    """把同一个人相邻的两句接成一段：中文之间不加空格，两边都是英文或数字时加一个。"""
    if first and second and _ASCII_WORD.match(first[-1]) and _ASCII_WORD.match(second[0]):
        return f"{first} {second}"
    return first + second


def format_duration(secs: float) -> str:
    minutes = int(max(0.0, secs) // 60)
    if minutes < 1:
        return "不到 1 分钟"
    if minutes < 60:
        return f"{minutes} 分钟"
    return f"{minutes // 60} 小时 {minutes % 60:02d} 分"


def format_wall_time(unix_secs: float, tz: tzinfo | None) -> str:
    return datetime.fromtimestamp(unix_secs, tz).strftime("%Y-%m-%d %H:%M")


def export_title(summary: SessionSummary, tz: tzinfo | None = None) -> str:
    title = summary.session.title.strip()
    return title or f"未命名会议 {format_wall_time(summary.session.started_at, tz)}"


def render_markdown(
    summary: SessionSummary,
    *,
    state: str,
    digest: Digest | None,
    utterances: list[NamedUtterance],
    frames: list[ScreenFrame],
    tasks: list[TaskRecord] | None = None,
    tz: tzinfo | None = None,
) -> str:
    """一场会议的 Markdown 转录。``state`` 是 ``live`` / ``interrupted`` / ``ended``；``tz`` 不给就用本机时区。"""
    session = summary.session
    state_text = {"live": "进行中", "interrupted": "已中断", "ended": "已结束"}.get(state, state)
    out: list[str] = [f"# {escape(export_title(summary, tz))}", ""]
    out.append(f"- 开始时间：{format_wall_time(session.started_at, tz)}")
    if session.ended_at is not None:
        out.append(f"- 结束时间：{format_wall_time(session.ended_at, tz)}")
    out.append(f"- 状态：{state_text}")
    out.append(f"- 时长：{format_duration(summary.duration_secs)}")
    if summary.speakers:
        out.append(f"- 发言人：{escape('、'.join(summary.speakers))}")
    out += ["", "## 纪要", ""]
    if digest is not None and digest.text.strip():
        out.append(digest.text.strip())
        out += ["", f"（纪要覆盖到 {format_hms(digest.t_to)}。）"]
    else:
        out.append(NO_DIGEST)
    out += ["", "## 转录", ""]

    # 发言和画面按时间排在一起；同一时刻发言在前
    events: list[tuple[float, int, int, Any]] = []
    for item in utterances:
        events.append((item.utterance.t_start, 0, item.utterance.id or 0, item))
    for frame in frames:
        events.append((frame.t, 1, frame.id or 0, frame))
    events.sort(key=lambda e: e[:3])
    if not events:
        out.append(NO_TRANSCRIPT)

    paragraph: dict[str, Any] | None = None  # 正在攒的那一段发言
    last_caption: str | None = None

    def flush() -> None:
        nonlocal paragraph
        if paragraph is None:
            return
        tag = "（文字）" if paragraph["source"] == "text" else ""
        out.append(f"**{escape(paragraph['name'])}**{tag} `{format_hms(paragraph['t'])}`")
        out.extend(["", escape(paragraph["text"]), ""])
        paragraph = None

    for _t, kind, _id, item in events:
        if kind == 0:
            u = item.utterance
            same = (
                paragraph is not None
                and paragraph["speaker"] == u.speaker_idx
                and paragraph["source"] == u.source
                and u.t_start - paragraph["end"] <= MERGE_GAP_SECS
            )
            if same:
                assert paragraph is not None
                paragraph["text"] = join_text(paragraph["text"], u.text.strip())
                paragraph["end"] = u.t_end
            else:
                flush()
                paragraph = {
                    "speaker": u.speaker_idx,
                    "source": u.source,
                    "name": item.speaker_name,
                    "t": u.t_start,
                    "end": u.t_end,
                    "text": u.text.strip(),
                }
            continue
        frame = item
        caption = (frame.caption or "").strip()
        if caption == IRRELEVANT_CAPTION:
            continue  # 与会议无关的画面不放进导出
        if caption and caption == last_caption:
            continue  # 画面没变（兜底截图沿用了上一张的摘要）：不重复
        last_caption = caption or last_caption
        flush()
        label = f"> **画面** `{format_hms(frame.t)}`"
        out.append(f"{label}：{escape(caption)}" if caption else label)
        out += [">", f"> ![{format_hms(frame.t)} 的屏幕截图]({frame_file(frame)})", ""]
    flush()

    if tasks:
        out += ["## 后台任务", ""]
        status_text = {
            "queued": "排队中",
            "running": "进行中",
            "succeeded": "已完成",
            "failed": "失败",
            "cancelled": "已取消",
        }
        for task in tasks:
            out += [f"### {task.label}：{escape(' '.join(task.goal.split()))}", ""]
            out.append(f"- 状态：{status_text.get(task.status, task.status)}")
            if task.status == "succeeded" and task.brief:
                out.append(f"- 结论：{escape(task.brief)}")
            if task.status in ("failed", "cancelled") and task.error:
                out.append(f"- 原因：{escape(task.error)}")
            out.append("")
            if task.detail_md and task.detail_md.strip():
                out += [plain_block(task.detail_md), ""]
            if task.sources:
                out += ["来源：", "", *[f"- {source}" for source in task.sources], ""]
            if artifacts := safe_artifacts(task):
                listed = [f"- `tasks/{task.label}/{name}`" for name in artifacts]
                out += ["产物文件：", "", *listed, ""]
    return "\n".join(out).rstrip("\n") + "\n"


def download_headers(
    title: str, session_id: str, *, ext: str = "md", stem: str = "meeting"
) -> dict[str, str]:
    """下载用的文件名：中文标题走 RFC 5987 的 ``filename*``，另给一个纯 ASCII 的后备名。"""
    # 文件名里不能有的字符换成空格，再把连续的空白（含换行）压成一个
    safe = " ".join(re.sub(r'[\\/:*?"<>|]+', " ", title).split()) or "meeting"
    fallback = f"{stem}-{session_id[:8]}.{ext}"
    encoded = quote(f"{safe}.{ext}", safe="")
    return {
        "Content-Disposition": f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"
    }


# --------------------------------------------------------------------------- #
# 结构化 JSON 与压缩包
# --------------------------------------------------------------------------- #

EXPORT_FORMAT_VERSION = 1


@dataclass
class ExportData:
    """一场会议导出时用到的全部数据（一次取齐，三种格式共用）。"""

    summary: SessionSummary
    state: str  # "live" | "interrupted" | "ended"
    speakers: list[SpeakerInfo]
    utterances: list[NamedUtterance]
    frames: list[ScreenFrame]
    digests: list[Digest]
    tasks: list[TaskRecord]
    task_events: dict[str, list[TaskEvent]] = field(default_factory=dict)
    connections: list[Connection] = field(default_factory=list)
    report: Report | None = None  # 最近一份生成好的报告

    @property
    def digest(self) -> Digest | None:
        return self.digests[-1] if self.digests else None


async def collect(store: Any, session_id: str, *, live: bool, now: float) -> ExportData | None:
    summary = await store.get_summary(session_id, now=now)
    if summary is None:
        return None
    state = "live" if live else ("ended" if summary.session.ended_at is not None else "interrupted")
    tasks = await store.list_tasks(session_id)
    return ExportData(
        summary=summary,
        state=state,
        speakers=await store.list_speakers(session_id),
        utterances=await store.all_utterances(session_id),
        frames=await store.list_frames(session_id),
        digests=await store.list_digests(session_id),
        tasks=tasks,
        task_events={task.id: await store.list_task_events(task.id) for task in tasks},
        connections=await store.list_connections(session_id),
        report=await store.latest_report(session_id, done_only=True),
    )


def frame_file(frame: ScreenFrame) -> str:
    """截图在导出包里的相对路径（也是 Markdown 里引用它的路径）。"""
    return f"frames/{PurePosixPath(frame.path.replace(chr(92), '/')).name}"


def export_markdown(data: ExportData, tz: tzinfo | None = None) -> str:
    return render_markdown(
        data.summary,
        state=data.state,
        digest=data.digest,
        utterances=data.utterances,
        frames=data.frames,
        tasks=data.tasks,
        tz=tz,
    )


def export_json(data: ExportData) -> dict[str, Any]:
    """结构化导出（interfaces.md §5.6）。时间：``t*`` 是会话时间轴上的秒，``*_at`` 是 Unix 秒。"""
    session = data.summary.session
    report = data.report
    return {
        "format_version": EXPORT_FORMAT_VERSION,
        "session": {
            "id": session.id,
            "title": session.title,
            "started_at": session.started_at,
            "ended_at": session.ended_at,
            "last_active_at": session.last_active_at,
            "state": data.state,
            "duration_secs": data.summary.duration_secs,
        },
        "connections": [
            {
                "connected_at": c.connected_at,
                "disconnected_at": c.disconnected_at,
                "t_from": c.t_from,
                "t_to": c.t_to,
            }
            for c in data.connections
        ],
        "speakers": [{"idx": s.idx, "display_name": s.display_name} for s in data.speakers],
        "utterances": [
            {
                "id": item.utterance.id,
                "speaker_idx": item.utterance.speaker_idx,
                "speaker_name": item.speaker_name,
                "t_start": item.utterance.t_start,
                "t_end": item.utterance.t_end,
                "text": item.utterance.text,
                "source": item.utterance.source,
            }
            for item in data.utterances
        ],
        "frames": [
            {
                "id": f.id,
                "t": f.t,
                "file": frame_file(f),
                "width": f.width,
                "height": f.height,
                "caption": f.caption,
                "caption_status": f.caption_status,
            }
            for f in data.frames
        ],
        "digests": [
            {"t_from": d.t_from, "t_to": d.t_to, "text": d.text, "created_at": d.created_at}
            for d in data.digests
        ],
        "tasks": [
            {
                "id": task.id,
                "label": task.label,
                "goal": task.goal,
                "status": task.status,
                "brief": task.brief,
                "detail_md": task.detail_md,
                "sources": list(task.sources),
                "artifacts": [f"tasks/{task.label}/{name}" for name in safe_artifacts(task)],
                "error": task.error,
                "modality": task.modality,
                "requested_by": task.requested_by,
                "requested_t": task.requested_t,
                "created_at": task.created_at,
                "started_at": task.started_at,
                "finished_at": task.finished_at,
                "events": [
                    {"at": e.at, "kind": e.kind, "summary": e.summary}
                    for e in data.task_events.get(task.id, [])
                ],
            }
            for task in data.tasks
        ],
        "report": None
        if report is None
        else {
            "id": report.id,
            "created_at": report.created_at,
            "provider": report.provider,
            "text_md": report.text_md,
        },
    }


def safe_artifacts(task: TaskRecord) -> list[str]:
    """任务报告过的产物里文件名规矩的那些（绝对路径、带 ``..`` 的不要：不能借导出读到数据目录之外）。"""
    names = []
    for name in task.artifacts:
        cleaned = name.strip().replace("\\", "/")
        path = PurePosixPath(cleaned)
        if not cleaned or path.is_absolute() or ".." in path.parts or ":" in cleaned:
            continue
        names.append(path.as_posix())
    return names


def archive_members(data: ExportData, data_dir: Path) -> list[tuple[str, Path]]:
    """压缩包里要放的文件：``(包内路径, 磁盘上的路径)``。只收确实存在、而且在这场会议目录里面的。"""
    root = Path(data_dir).resolve()
    session_root = (root / "sessions" / data.summary.session.id).resolve()
    members: list[tuple[str, Path]] = []

    def add(name: str, path: Path) -> None:
        full = path.resolve()
        if full.is_relative_to(session_root) and full.is_file():
            members.append((name, full))

    for frame in data.frames:
        add(frame_file(frame), root / frame.path)
    for task in data.tasks:
        for name in safe_artifacts(task):
            add(f"tasks/{task.label}/{name}", session_root / "tasks" / task.label / name)
    return members


class _Chunks:
    """只会 ``write`` 的输出流：zipfile 把它当成不能回退的流来写，写出来的字节攒在这里，随取随清。"""

    def __init__(self) -> None:
        self._parts: list[bytes] = []

    def write(self, data: bytes) -> int:
        self._parts.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        return None

    def take(self) -> bytes:
        data, self._parts = b"".join(self._parts), []
        return data


def zip_stream(
    texts: list[tuple[str, str]], files: list[tuple[str, Path]], *, chunk_size: int = 1 << 16
) -> Iterator[bytes]:
    """流式生成压缩包：文字成员压缩，文件成员（图片为主，本来就压过）只打包不压缩，边读边出。"""
    sink = _Chunks()
    with zipfile.ZipFile(sink, "w") as archive:  # type: ignore[arg-type]
        for name, text in texts:
            archive.writestr(name, text.encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
            yield sink.take()
        for name, path in files:
            try:
                source = path.open("rb")
            except OSError:
                continue  # 取数据之后文件被删了：跳过这一个，别让整个下载失败
            with source, archive.open(zipfile.ZipInfo(name), "w", force_zip64=True) as target:
                while block := source.read(chunk_size):
                    target.write(block)
                    if data := sink.take():
                        yield data
            if data := sink.take():
                yield data
    if data := sink.take():
        yield data


def register(app: FastAPI) -> None:
    @app.get("/api/export/{name}")
    async def export(request: Request, name: str) -> Response:
        resources = request.app.state.resources
        if resources.store is None or resources.sessions is None:
            raise StarletteHTTPException(503, "存储尚未就绪")
        stem, dot, suffix = name.rpartition(".")
        if not dot or not stem or suffix not in ("md", "json", "zip"):
            raise StarletteHTTPException(
                404, "导出地址的格式是 /api/export/<会议编号>.md（或 .json、.zip）"
            )
        if await resources.store.get_session(stem) is not None:
            await resources.store.require_available(stem)
        data = await collect(
            resources.store, stem, live=resources.sessions.is_live(stem), now=time.time()
        )
        if data is None:
            raise StarletteHTTPException(404, "找不到这场会议")
        title = export_title(data.summary)
        if suffix == "md":
            return Response(
                export_markdown(data),
                media_type="text/markdown; charset=utf-8",
                headers=download_headers(title, stem),
            )
        if suffix == "json":
            return Response(
                json.dumps(export_json(data), ensure_ascii=False, indent=2),
                media_type="application/json; charset=utf-8",
                headers=download_headers(title, stem, ext="json"),
            )
        cfg = resources.cfg
        texts = [
            ("transcript.md", export_markdown(data)),
            ("session.json", json.dumps(export_json(data), ensure_ascii=False, indent=2)),
        ]
        if data.report is not None:
            texts.append(("report.md", data.report.text_md))
        files = archive_members(data, cfg.resolve(cfg.session.data_dir))
        return StreamingResponse(
            zip_stream(texts, files),
            media_type="application/zip",
            headers=download_headers(title, stem, ext="zip"),
        )
