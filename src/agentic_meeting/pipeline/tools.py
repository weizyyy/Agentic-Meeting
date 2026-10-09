"""实时模型的工具（docs/interfaces.md §7、docs/pipecat-notes.md §6）。

用 Pipecat 的「直接函数」写法：函数签名和文档字符串就是工具定义（名字、参数名用英文，描述用中文——这些描述是给
实时模型看的）。三个查东西的同步工具：``recall``、``get_digest``、``look_at_screen``；三个任务工具：
``delegate_task``（异步：先回报已受理，任务做完再回报结果）、``task_status``、``cancel_task``。
后台任务关掉时（``agent.enabled = false``）不给任务工具。

工具通过 ``params.app_resources``（应用共享的 ``AppResources``）拿到存储、嵌入客户端、配置；
「现在是哪场会议、会议进行到第几秒」来自正在进行的那路连接（``resources.sessions.live``）。

约定：

* 时间参数是「距现在多少分钟」，在函数里换算成会话时间轴上的范围；返回里的时间是 ``时:分:秒``。
* 返回值是能序列化成 JSON 的字典，字段尽量少；查不到东西时不报错，而是在 ``note`` 里用一句话说明原因，
  让模型能据此如实回答。
* 工具本身出错（数据库坏了之类）也不抛异常：回一个带 ``error`` 的结果，转录和这一轮应答都不受影响。
* **对助理提的要求不算会议内容**：键入的文字，以及刚刚那句叫了助理名字的话，不出现在工具结果里——
  不然问「谁提到过学习率」时，这句提问自己就会被找回来。
"""

from __future__ import annotations

import asyncio
import base64
import functools
import re
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger
from pipecat.adapters.schemas.direct_function import tool_options
from pipecat.frames.frames import FunctionCallResultProperties, LLMConfigureOutputFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import FunctionCallParams

from agentic_meeting.pipeline.clock import format_hms
from agentic_meeting.screen.attach import select_frames
from agentic_meeting.screen.caption import IRRELEVANT_CAPTION
from agentic_meeting.screen.ingest import media_type_of
from agentic_meeting.store.embeddings import recall as recall_utterances
from agentic_meeting.types import SPEAKER_TYPED, SPEAKER_UNKNOWN, NamedUtterance

RECALL_DEFAULT_LIMIT = 10
RECALL_MAX_LIMIT = 30
ITEM_MAX_CHARS = 300  # 单条发言在工具结果里最多这么长
SINCE_DIGEST_MAX_ITEMS = 40  # get_digest 里「纪要之后的发言」最多带多少条
RECENT_MAX_ITEMS = 60
REQUEST_WINDOW_SECS = 60.0  # 这么近的、带助理名字的发言，当作「正在对助理提的要求」
SPARE = 5  # 多取几条，过滤掉对助理提的要求之后仍然够数
IN_PROGRESS_WAIT_SECS = 0.5  # look_at_screen：等这次调用的记录进了上下文，再把图片放在它后面

DEFAULT_CONTEXT_MINUTES = 5.0  # delegate_task 默认带最近几分钟的转录
MAX_CONTEXT_MINUTES = 60.0
MAX_TASK_FRAMES = (
    3  # 带给后台 agent 的截图最多几张（取范围内最近的）；agent.attach_frames 打开时带全部
)
MAX_LOOK_FRAMES = 3  # look_at_screen 一次最多回看几张之前的截图
MAX_EARLIER_LISTED = 20  # look_at_screen 列出多少张更早的截图供模型挑
MAX_GOAL_CHARS = 2000
QUIET_WAIT_SECS = 20.0  # 任务完成后等「没有人在说话」再口头简报，最多等这么久
DELEGATE_TIMEOUT_MARGIN_SECS = 120.0  # delegate_task 的调用超时 = 任务超时 + 这么多余量

NO_MEETING = "现在没有进行中的会议"
NO_TASKS = "后台任务功能没有开启"


class _Env:
    """一次工具调用用到的东西。"""

    def __init__(self, params: FunctionCallParams) -> None:
        resources = params.app_resources
        if resources is None or resources.store is None or resources.sessions is None:
            raise LookupError(NO_MEETING)
        live = resources.sessions.live
        if live is None:
            raise LookupError(NO_MEETING)
        self.resources = resources
        self.cfg = resources.cfg
        self.store = resources.store
        self.live = live
        self.session_id: str = live.session.id
        # 会议进行到第几秒（按已收到的音频算）
        self.now_secs = float(getattr(live.recorder, "elapsed_secs", 0.0) or 0.0)
        self._wake_phrases = [p.lower() for p in self.cfg.session.wake_phrases]

    def is_request(self, named: NamedUtterance) -> bool:
        """这条发言是不是对助理提的要求（而不是会议内容）。"""
        u = named.utterance
        if u.source == "text":
            return True
        if u.source != "asr" or u.t_start < self.now_secs - REQUEST_WINDOW_SECS:
            return False
        text = u.text.lower()
        return any(phrase in text for phrase in self._wake_phrases)

    def content(self, items: list[NamedUtterance], limit: int) -> list[NamedUtterance]:
        """去掉对助理提的要求，留最近的 ``limit`` 条。"""
        kept = [n for n in items if not self.is_request(n)]
        return kept[-limit:]


def _tool(function: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    """工具的统一外壳：没有会议时回一句说明；内部出错回 ``error``，不让异常冒到管线里。"""

    @functools.wraps(function)
    async def wrapper(params: FunctionCallParams, *args: Any, **kwargs: Any) -> None:
        try:
            await function(params, *args, **kwargs)
        except LookupError as e:
            await params.result_callback({"note": str(e)})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"工具 {function.__name__} 出错")
            await params.result_callback({"error": "这个工具暂时用不了"})

    return wrapper


def _item(named: NamedUtterance) -> dict[str, str]:
    u = named.utterance
    text = u.text if len(u.text) <= ITEM_MAX_CHARS else u.text[:ITEM_MAX_CHARS] + "…"
    return {"time": format_hms(u.t_start), "speaker": named.speaker_name, "text": text}


def _number(value: Any, default: float = 0.0) -> float:
    """模型给的数字参数可能是字符串、空值、负数：尽量读成非负数。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and number > 0 else default  # NaN、负数按没给算


def time_range(
    now_secs: float, minutes_ago_from: Any, minutes_ago_to: Any
) -> tuple[float | None, float | None]:
    """「距现在多少分钟」→ 会话时间轴上的 ``(t_from, t_to)``；没限定的一端是 ``None``。

    ``minutes_ago_from`` 是较早的那一端、``minutes_ago_to`` 是较晚的那一端；给反了就换过来。
    """
    older, newer = _number(minutes_ago_from), _number(minutes_ago_to)
    if older and newer and older < newer:
        older, newer = newer, older
    if not older and newer:
        # 只给了较晚的一端：「N 分钟以前的」
        return None, max(0.0, now_secs - newer * 60.0)
    t_from = max(0.0, now_secs - older * 60.0) if older else None
    t_to = max(0.0, now_secs - newer * 60.0) if newer else None
    return t_from, t_to


async def resolve_speaker(store: Any, session_id: str, name: str) -> tuple[int | None, list[str]]:
    """按显示名找说话人：先精确匹配，再看是不是只有一个名字包含它（或被它包含）。

    返回 ``(编号, 这场会议里的说话人名单)``；找不到或有歧义时编号是 ``None``。
    """
    speakers = [s for s in await store.list_speakers(session_id) if s.idx != 0]
    names = [s.display_name for s in speakers]
    wanted = name.strip()
    for s in speakers:
        if s.display_name == wanted:
            return s.idx, names
    folded = wanted.replace(" ", "").lower()
    close = [
        s
        for s in speakers
        if folded
        and (
            folded in s.display_name.replace(" ", "").lower()
            or s.display_name.replace(" ", "").lower() in folded
        )
    ]
    return (close[0].idx if len(close) == 1 else None), names


@_tool
async def recall(
    params: FunctionCallParams,
    query: str = "",
    speaker: str = "",
    minutes_ago_from: float = 0,
    minutes_ago_to: float = 0,
    limit: int = RECALL_DEFAULT_LIMIT,
):
    """查找这场会议里说过的话。最近的转录里找不到要的内容时用它。

    Args:
        query: 关键词，或一句话描述要找的内容。只想按人或按时间找时留空。
        speaker: 说话人的名字（按转录里显示的写）。留空表示不限。
        minutes_ago_from: 时间范围较早的一端，距现在多少分钟。例如找十分钟以内的，填 10。0 表示从会议开头算起。
        minutes_ago_to: 时间范围较晚的一端，距现在多少分钟。0 表示到现在为止。
        limit: 最多返回多少条，默认 10。
    """
    env = _Env(params)
    filters: dict[str, Any] = {}
    if speaker.strip():
        idx, known = await resolve_speaker(env.store, env.session_id, speaker)
        if idx is None:
            await params.result_callback(
                {"items": [], "note": f"没有找到叫「{speaker.strip()}」的说话人", "speakers": known}
            )
            return
        filters["speaker_idx"] = idx
    t_from, t_to = time_range(env.now_secs, minutes_ago_from, minutes_ago_to)
    count = int(min(RECALL_MAX_LIMIT, max(1, _number(limit, RECALL_DEFAULT_LIMIT))))
    found = await recall_utterances(
        env.store,
        env.resources.embedder,
        env.session_id,
        query=query.strip() or None,
        t_from=t_from,
        t_to=t_to,
        limit=count + SPARE,
        **filters,
    )
    found = env.content(found, count)
    result: dict[str, Any] = {"items": [_item(n) for n in found]}
    if not found:
        result["note"] = "没有找到符合条件的发言"
    await params.result_callback(result)


@_tool
async def get_digest(params: FunctionCallParams, scope: str = "all"):
    """取会议纪要，用来回答「整理一下」「总结一下」这类要求。

    Args:
        scope: "all" 取到目前为止的整场纪要，外加纪要之后的发言；"recent" 只取最近几分钟的发言原文。
    """
    env = _Env(params)
    if scope.strip().lower() == "recent":
        window = max(1.0, env.cfg.realtime.digest_interval_minutes) * 60.0
        recent = env.content(
            await env.store.recall(
                env.session_id,
                t_from=max(0.0, env.now_secs - window),
                limit=RECENT_MAX_ITEMS + SPARE,
            ),
            RECENT_MAX_ITEMS,
        )
        result: dict[str, Any] = {
            "digest": "",
            "covers_until": "",
            "since_then": [_item(n) for n in recent],
        }
        if not recent:
            result["note"] = "最近几分钟没有发言"
        await params.result_callback(result)
        return

    digest = await env.store.latest_digest(env.session_id)
    after_id = digest.last_utterance_id if digest else 0
    # 纪要之后的发言：很多的话只带最近的若干条
    later = env.content(
        await env.store.list_utterances(env.session_id, after_id=after_id, limit=500),
        SINCE_DIGEST_MAX_ITEMS,
    )
    result = {
        "digest": digest.text if digest else "",
        "covers_until": format_hms(digest.t_to) if digest else "",
        "since_then": [_item(n) for n in later],
    }
    if digest is None:
        result["note"] = (
            "还没有生成纪要，下面是到目前为止的发言" if later else "会议刚开始，还没有可整理的内容"
        )
    await params.result_callback(result)


def _frame_ids(value: Any) -> list[int]:
    """模型给的截图编号：列表、单个数字、逗号分开的字符串都认；认不出来的丢掉，去重、保持顺序。"""
    if value is None or isinstance(value, bool):
        return []
    items = re.split(r"[,，\s]+", value) if isinstance(value, str) else value
    if isinstance(items, (int, float)):
        items = [items]
    ids: list[int] = []
    try:
        candidates = list(items)
    except TypeError:
        return []
    for item in candidates:
        try:
            number = int(str(item).strip())
        except ValueError:
            continue
        if number not in ids:
            ids.append(number)
    return ids


def _frame_item(frame: Any) -> dict[str, Any]:
    caption = frame.caption if frame.caption and frame.caption != IRRELEVANT_CAPTION else ""
    return {"id": frame.id, "time": format_hms(frame.t), "caption": caption}


async def _read_frame_url(env: _Env, frame: Any) -> str | None:
    frames = env.resources.frames
    path = frames.path_of(frame) if frames is not None else None
    try:
        if path is None:
            raise FileNotFoundError(frame.path)
        data = await asyncio.to_thread(path.read_bytes)
    except OSError:
        logger.warning(f"截图文件读不到：{frame.path}")
        return None
    return f"data:{media_type_of(frame.path)};base64,{base64.b64encode(data).decode('ascii')}"


async def _look_back(params: FunctionCallParams, env: _Env, wanted: list[int]) -> None:
    """回看之前的截图：把点名的几张（最多 ``MAX_LOOK_FRAMES``）附进上下文。"""
    chosen = []
    for frame_id in wanted[:MAX_LOOK_FRAMES]:
        frame = await env.store.get_frame(frame_id)
        if frame is not None and frame.session_id == env.session_id:
            chosen.append(frame)
    if not chosen:
        await params.result_callback(
            {"note": "这场会议里没有这些编号的截图；编号要用不带参数调用时 earlier 清单里的 id"}
        )
        return
    chosen.sort(key=lambda f: f.t)
    loaded = [(frame, await _read_frame_url(env, frame)) for frame in chosen]
    result: dict[str, Any] = {"frames": [_frame_item(frame) for frame, _ in loaded]}
    readable = [(frame, url) for frame, url in loaded if url is not None]
    if not readable:
        result["note"] = "截图文件读不到，只有这些文字摘要"
        await params.result_callback(result)
        return
    await _wait_for_call_record(params)
    for frame, url in readable:
        params.context.add_message(
            LLMContext.create_image_url_message(
                url=url, text=f"[画面 {format_hms(frame.t)}] 之前的屏幕截图（编号 {frame.id}）"
            )
        )
    result["note"] = f"{len(readable)} 张截图已经附在后面，看过再回答"
    if len(wanted) > MAX_LOOK_FRAMES:
        result["note"] += f"；一次最多看 {MAX_LOOK_FRAMES} 张，其余的没有附"
    await params.result_callback(result)


@_tool
async def look_at_screen(params: FunctionCallParams, frame_ids: str = ""):
    """看屏幕。不带参数：把最新的一张屏幕截图拿来给你看，并在 earlier 里列出更早的截图（编号、时间、摘要）。问的是之前的画面（「刚才那页」「前面那张表」）、或者要对比几页时，根据 earlier 里的摘要挑出需要的，再调用一次并把它们的编号填进 frame_ids，看过图再回答。

    Args:
        frame_ids: 要回看的截图编号（来自 earlier 清单），多个用逗号分开，例如 "12,15"，一次最多 3 张。不填就是看最新的一张。
    """
    env = _Env(params)
    if not env.cfg.realtime_llm.active.supports_vision:
        frame_ids = ""  # 看不了图，回看没有意义：只给最新一张的摘要
    wanted = _frame_ids(frame_ids)
    if wanted:
        await _look_back(params, env, wanted)
        return
    frame = await env.store.latest_frame(env.session_id)
    if frame is None:
        await params.result_callback({"note": "还没有屏幕截图（没有人在共享屏幕）"})
        return
    caption = frame.caption if frame.caption and frame.caption != IRRELEVANT_CAPTION else ""
    result: dict[str, Any] = {
        "time": format_hms(frame.t),
        "seconds_ago": max(0, int(env.now_secs - frame.t)),
        "caption": caption,
    }
    if not env.cfg.realtime_llm.active.supports_vision:
        result["note"] = (
            "当前模型不能看图，只有这段文字摘要"
            if caption
            else "当前模型不能看图，也还没有文字摘要"
        )
        await params.result_callback(result)
        return

    # 更早的截图：有摘要、不是无关画面、和相邻的不重复；模型据此判断要不要回看
    history = select_frames(await env.store.list_frames(env.session_id), limit=10_000)
    earlier = [_frame_item(f) for f in history if f.id != frame.id and (f.caption or "").strip()][
        -MAX_EARLIER_LISTED:
    ]
    if earlier:
        result["earlier"] = earlier

    url = await _read_frame_url(env, frame)
    if url is None:
        result["note"] = "截图文件读不到，只有这段文字摘要" if caption else "截图文件读不到"
        await params.result_callback(result)
        return
    # 图片作为一条用户消息进上下文。尽量排在这次工具调用的记录之后（那条记录由助理侧聚合器写入，稍晚一点到）。
    await _wait_for_call_record(params)
    params.context.add_message(
        LLMContext.create_image_url_message(url=url, text=f"[画面 {format_hms(frame.t)}] 屏幕截图")
    )
    result["note"] = "最新的截图已经附在后面"
    if earlier:
        result["note"] += "；要看更早的某几张，带上 frame_ids 再调用一次"
    await params.result_callback(result)


async def _wait_for_call_record(params: FunctionCallParams) -> None:
    waited = 0.0
    while waited < IN_PROGRESS_WAIT_SECS:
        if any(
            isinstance(m, dict) and m.get("tool_call_id") == params.tool_call_id
            for m in params.context.get_messages()
        ):
            return
        await asyncio.sleep(0.01)
        waited += 0.01


# --------------------------------------------------------------------------- #
# 后台任务
# --------------------------------------------------------------------------- #


def _tasks(env: _Env) -> Any:
    tasks = getattr(env.resources, "tasks", None)
    if tasks is None:
        raise LookupError(NO_TASKS)
    return tasks


def _modality(env: _Env) -> str:
    """这次请求是文字还是语音（看模态闸门）。"""
    return getattr(getattr(env.live, "gate", None), "modality", "voice")


async def _announce_when_appropriate(params: FunctionCallParams, env: _Env, modality: str) -> None:
    """任务做完、要把结果交给实时模型之前：定好这次播报的模态，语音播报还要等没有人在说话。

    结果交回去之后，助理侧聚合器会向**上游**推上下文帧触发生成，那一帧不经过模态闸门，
    所以在这里直接往模型服务的队列里放一个配置帧，排在它前面。
    """
    text_only = modality == "text"
    if not text_only:
        wait_quiet = getattr(env.live.recorder, "wait_quiet", None)
        if wait_quiet is not None:
            await wait_quiet(QUIET_WAIT_SECS)  # 文字播报不出声，不用等
    try:
        await params.llm.queue_frame(LLMConfigureOutputFrame(skip_tts=text_only))
    except Exception:
        logger.exception("设定任务播报的模态失败")


async def _delegate(
    params: FunctionCallParams,
    goal: str,
    minutes_of_context: float = DEFAULT_CONTEXT_MINUTES,
    include_screen: bool = False,
):
    env = _Env(params)
    tasks = _tasks(env)
    goal = goal.strip() if isinstance(goal, str) else ""
    if not goal:
        await params.result_callback({"error": "没有说明要做什么（goal 是空的），任务没有创建"})
        return
    minutes = min(MAX_CONTEXT_MINUTES, _number(minutes_of_context, DEFAULT_CONTEXT_MINUTES))
    window = (max(0.0, env.now_secs - minutes * 60.0), env.now_secs)
    frame_ids: list[int] = []
    if env.cfg.agent.attach_frames:
        # 配置要求把截图原图都交给后台模型：到现在为止的全部截图（无关画面、没变的重复截图除外），不看 include_screen
        chosen = select_frames(
            await env.store.list_frames(env.session_id, t_to=env.now_secs),
            env.cfg.agent.max_attached_frames,
        )
        frame_ids = [f.id for f in chosen if f.id is not None]
    elif include_screen is True or str(include_screen).strip().lower() == "true":
        frames = await env.store.list_frames(env.session_id, t_from=window[0], t_to=window[1])
        if not frames:  # 这段时间里没有新截图：屏幕没变，带上最新的一张
            latest = await env.store.latest_frame(env.session_id)
            frames = [latest] if latest is not None else []
        frame_ids = [f.id for f in frames[-MAX_TASK_FRAMES:] if f.id is not None]
    modality = _modality(env)
    requested_by = (
        SPEAKER_TYPED
        if modality == "text"
        else getattr(env.live.recorder, "last_speaker_idx", SPEAKER_UNKNOWN)
    )
    task = await tasks.submit(
        session_id=env.session_id,
        goal=goal[:MAX_GOAL_CHARS],
        requested_by=requested_by,
        requested_t=env.now_secs,
        transcript_window=window,
        frame_ids=frame_ids,
        modality=modality,
    )
    # 先回报「已受理」：对话不等任务做完，助理先口头确认一句
    await params.result_callback(
        {"task_id": task.label, "status": "accepted"},
        properties=FunctionCallResultProperties(is_final=False),
    )
    try:
        result = await tasks.wait(task.id)
        final: dict[str, Any] = {
            "task_id": task.label,
            "status": "succeeded",
            "brief": result.brief,
        }
    except Exception as e:  # TaskFailed（失败、取消）或别的意外：都如实回报，不让模型干等
        status = getattr(e, "status", "failed")
        reason = getattr(e, "reason", None) or "任务没有做完"
        if not hasattr(e, "reason"):
            logger.exception(f"等待任务 {task.label} 的结果时出错")
        final = {"task_id": task.label, "status": status, "reason": reason}
    await _announce_when_appropriate(params, env, modality)

    async def announced() -> None:
        await tasks.mark_announced(task.id)

    await params.result_callback(
        final, properties=FunctionCallResultProperties(on_context_updated=announced)
    )


def delegate_tool(task_timeout_secs: float) -> Callable[..., Awaitable[None]]:
    """造出 ``delegate_task`` 工具。调用超时跟着配置的任务超时走，所以是个工厂而不是模块级函数。"""

    @tool_options(
        cancel_on_interruption=False,
        timeout_secs=task_timeout_secs + QUIET_WAIT_SECS + DELEGATE_TIMEOUT_MARGIN_SECS,
    )
    @_tool
    async def delegate_task(
        params: FunctionCallParams,
        goal: str,
        minutes_of_context: float = DEFAULT_CONTEXT_MINUTES,
        include_screen: bool = False,
    ):
        """把需要联网检索、核实事实、读图分析、计算或画图的事交给后台去做。会立刻返回「已受理」，做完后你会再收到结果。

        Args:
            goal: 要做的事。写成一段完整、独立可读的描述：查什么、为什么查、结果要回答什么问题。
            minutes_of_context: 把最近多少分钟的会议转录一起交给后台作参考，默认 5。
            include_screen: 这件事需要看屏幕上的内容时设为 true，会带上最近的屏幕截图。
        """
        await _delegate(params, goal, minutes_of_context, include_screen)

    return delegate_task


@_tool
async def task_status(params: FunctionCallParams, task_id: str = ""):
    """查后台任务做到哪了。

    Args:
        task_id: 任务编号，例如 t2。留空表示最近交办的那一个。
    """
    env = _Env(params)
    status = await _tasks(env).status(env.session_id, task_id if task_id.strip() else None)
    if status is None:
        note = (
            f"没有编号是 {task_id.strip()} 的任务"
            if task_id.strip()
            else "这场会议里还没有交办过任务"
        )
        await params.result_callback({"note": note})
        return
    await params.result_callback(status)


@_tool
async def cancel_task(params: FunctionCallParams, task_id: str = ""):
    """取消一个后台任务（对方说不用查了的时候）。

    Args:
        task_id: 任务编号，例如 t2。留空表示最近交办的那一个。
    """
    env = _Env(params)
    tasks = _tasks(env)
    task = await env.store.find_task(env.session_id, task_id if task_id.strip() else None)
    if task is None:
        note = (
            f"没有编号是 {task_id.strip()} 的任务"
            if task_id.strip()
            else "这场会议里还没有交办过任务"
        )
        await params.result_callback({"note": note})
        return
    if task.finished:
        await params.result_callback(
            {"task_id": task.label, "status": task.status, "note": "这个任务已经结束了，不用取消"}
        )
        return
    cancelled = await tasks.cancel(task.id)
    await params.result_callback(
        {"task_id": task.label, "status": cancelled.status if cancelled else "cancelled"}
    )


def realtime_tools(cfg: Any = None) -> list[Callable[..., Awaitable[None]]]:
    """给实时模型的工具列表（放进 ``LLMContext(tools=...)`` 即自动注册）。顺序固定，前缀缓存才稳定。

    给了配置且 ``agent.enabled`` 为真时带上三个任务工具；不给配置只有三个查东西的工具。
    """
    tools: list[Callable[..., Awaitable[None]]] = [recall, get_digest, look_at_screen]
    if cfg is not None and cfg.agent.enabled:
        tools += [delegate_tool(cfg.agent.task_timeout_secs), task_status, cancel_task]
    return tools
