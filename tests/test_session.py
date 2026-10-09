"""会话管理（pipeline/session.py）：同一时刻只有一路活动连接、新连接顶替旧连接、断开不结束会议、
继续一场已有的会议、说话人区分的流跨连接复用、合并说话人。

用真实的 SQLite 临时库和假的 worker；``simulate_bot`` 模仿 ``run_bot`` 的生命周期：
begin → 登记 worker → 一直运行到被取消 → finish。
"""

from __future__ import annotations

import asyncio

import pytest
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame

from agentic_meeting.diar.base import NullDiarizer
from agentic_meeting.diar.stream import Anchor, SessionDiarizer
from agentic_meeting.pipeline.session import (
    RENUMBERED_NOTICE,
    LiveConnection,
    SessionManager,
    SessionNotFound,
)
from agentic_meeting.store.db import Store
from agentic_meeting.types import SpeakerSegment, Utterance


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "meetings.db", 4, assistant_name="Nova")
    yield s
    await s.close()


class Clock:
    """可以拨的墙上时钟。"""

    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def manager(store, clock):
    return SessionManager(store, now=clock, notify_grace_secs=0.0, takeover_timeout_secs=0.5)


class FakeRecorder:
    def __init__(self, elapsed: float = 0.0):
        self.elapsed_secs = elapsed
        self.names: dict[int, str] = {}

    def set_speaker_name(self, idx: int, name: str) -> None:
        self.names[idx] = name


class FakeWorker:
    def __init__(self):
        self.frames = []
        self.cancelled = asyncio.Event()

    async def queue_frame(self, frame):
        self.frames.append(frame)

    async def cancel(self):
        self.cancelled.set()

    def messages(self, kind: str) -> list[dict]:
        return [
            f.data
            for f in self.frames
            if isinstance(f, RTVIServerMessageFrame) and f.data.get("type") == kind
        ]


class Bot:
    """一路模拟的连接：begin 之后一直运行到 worker 被取消，然后 finish。"""

    def __init__(
        self,
        manager: SessionManager,
        *,
        elapsed: float = 12.5,
        stubborn: bool = False,
        session_id: str | None = None,
    ):
        self.manager = manager
        self.session_id = session_id  # 给了就是继续那一场
        self.worker = FakeWorker()
        self.recorder = FakeRecorder(elapsed)
        self.live: LiveConnection | None = None
        self.started = asyncio.Event()
        self.stubborn = stubborn  # True：被取消后不肯收尾（用来测顶替的超时）
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        if self.session_id is not None:
            live = await self.manager.attach(self.session_id)
            self.recorder.elapsed_secs += live.base_secs  # 记录器的时间从本次连接的起点接着走
        else:
            live = await self.manager.begin()
        await self.manager.register(live, self.worker, self.recorder)
        self.live = live
        self.started.set()
        try:
            await self.worker.cancelled.wait()
            if self.stubborn:
                await asyncio.sleep(3600)
        finally:
            await self.manager.finish(live)

    async def wait_started(self) -> LiveConnection:
        await asyncio.wait_for(self.started.wait(), 2)
        assert self.live is not None
        return self.live

    async def hang_up(self) -> None:
        """模拟用户关了页面：连接自己断开。"""
        self.worker.cancelled.set()
        await asyncio.wait_for(self.task, 2)


async def test_begin_creates_a_session_and_a_connection_row_and_registers_it(manager, store):
    bot = Bot(manager)
    live = await bot.wait_started()
    assert manager.live is live and manager.is_live(live.session.id)
    assert (await store.get_session(live.session.id)).ended_at is None
    (conn,) = await store.list_connections(live.session.id)
    assert (conn.t_from, conn.disconnected_at) == (0.0, None)
    assert (await manager.current_session()).id == live.session.id
    await bot.hang_up()


async def test_hanging_up_closes_the_connection_but_does_not_end_the_session(manager, store):
    bot = Bot(manager, elapsed=12.5)
    live = await bot.wait_started()
    await bot.hang_up()
    assert manager.live is None and not manager.is_live(live.session.id)
    session = await store.get_session(live.session.id)
    assert session.ended_at is None  # 断开 = 已中断，不写 ended_at
    (conn,) = await store.list_connections(live.session.id)
    assert conn.disconnected_at is not None and conn.t_to == 12.5  # 实际覆盖的会话时间
    assert (await manager.current_session()).id == live.session.id  # 没有连接时取最近未结束的


async def test_a_new_connection_takes_over_and_leaves_the_old_session_interrupted(manager, store):
    first = Bot(manager)
    old = await first.wait_started()
    second = Bot(manager)
    new = await second.wait_started()

    assert old.session.id != new.session.id and manager.live is new
    assert first.worker.cancelled.is_set()  # 旧连接被取消
    closed = first.worker.messages("session_closed")
    assert closed == [{"type": "session_closed", "reason": "taken_over"}]  # 旧页面被告知原因
    # 被顶替的那场只是「已中断」，之后还能继续（新建不会顺手结束别的会议）
    assert (await store.get_session(old.session.id)).ended_at is None
    assert (await store.get_session(new.session.id)).ended_at is None
    await asyncio.wait_for(first.task, 2)
    (conn,) = await store.list_connections(old.session.id)
    assert conn.disconnected_at is not None
    await second.hang_up()


async def test_beginning_leaves_other_unended_sessions_alone(manager, store):
    stale = await store.create_session("断线的一场", now=100.0)
    done = await store.create_session("早已结束", now=50.0)
    await store.end_session(done.id, now=80.0)

    bot = Bot(manager)
    live = await bot.wait_started()
    assert (await store.get_session(stale.id)).ended_at is None
    assert (await store.get_session(done.id)).ended_at == 80.0
    assert (live.base_secs, live.resumed) == (0.0, False)
    await bot.hang_up()


async def test_takeover_does_not_hang_forever_on_a_connection_that_will_not_stop(manager, store):
    first = Bot(manager, stubborn=True)
    await first.wait_started()
    second = Bot(manager)
    new = await asyncio.wait_for(second.wait_started(), 3)  # 超时（0.5 秒）之后照样继续
    assert manager.live is new
    first.task.cancel()
    await second.hang_up()


async def test_ending_the_live_session_stops_its_connection_and_marks_it_ended(manager, store):
    bot = Bot(manager)
    live = await bot.wait_started()
    ended = await manager.end(live.session.id)
    assert ended is not None and ended.ended_at is not None
    assert bot.worker.cancelled.is_set()
    assert bot.worker.messages("session_closed") == [{"type": "session_closed", "reason": "ended"}]
    await asyncio.wait_for(bot.task, 2)
    assert manager.live is None


async def test_ending_an_interrupted_session_only_marks_it(manager, store):
    bot = Bot(manager)
    live = await bot.wait_started()
    await bot.hang_up()
    ended = await manager.end(live.session.id)
    assert ended.ended_at is not None
    assert await manager.end("不存在") is None


async def test_current_session_falls_back_to_the_latest_unended_one(manager, store):
    assert await manager.current_session() is None
    a = await store.create_session("a", now=100.0)
    b = await store.create_session("b", now=200.0)
    assert (await manager.current_session()).id == b.id
    await store.end_session(b.id)
    assert (await manager.current_session()).id == a.id


async def test_push_reaches_only_the_live_connection(manager):
    assert await manager.push({"type": "speaker", "idx": 1, "display_name": "王老师"}) is False
    bot = Bot(manager)
    await bot.wait_started()
    assert await manager.push({"type": "speaker", "idx": 1, "display_name": "王老师"}) is True
    assert bot.worker.messages("speaker") == [
        {"type": "speaker", "idx": 1, "display_name": "王老师"}
    ]
    await bot.hang_up()
    assert await manager.push({"type": "speaker"}) is False


async def test_concurrent_connections_leave_exactly_one_live(manager, store):
    bots = [Bot(manager) for _ in range(3)]
    # 等三路都登记完、被顶替的两路都收尾完再检查；按固定时间等的话，慢机器上顶替还没做完
    lives = await asyncio.gather(*(bot.wait_started() for bot in bots))
    live = manager.live
    assert live is not None and any(live is item for item in lives)
    await asyncio.gather(*(asyncio.wait_for(b.task, 2) for b in bots if b.live is not live))
    rows = await store.list_sessions()
    assert len(rows) == 3
    open_rows = []
    for row in rows:
        (conn,) = await store.list_connections(row.session.id)
        if conn.disconnected_at is None:
            open_rows.append(row.session.id)
    assert open_rows == [live.session.id]  # 其余两场都被顶替、连接已关闭
    for bot in bots:
        if not bot.task.done():
            bot.worker.cancelled.set()
    await asyncio.gather(*(asyncio.wait_for(b.task, 2) for b in bots))


# --------------------------------------------------------------------------- #
# 继续一场会议
# --------------------------------------------------------------------------- #


async def test_attach_continues_the_timeline_where_the_wall_clock_is(manager, store, clock):
    first = Bot(manager, elapsed=12.5)
    old = await first.wait_started()
    await first.hang_up()

    clock.t += 720.0  # 中断了 12 分钟
    second = Bot(manager, elapsed=30.0, session_id=old.session.id)
    live = await second.wait_started()
    assert live.session.id == old.session.id and live.resumed
    assert live.base_secs == 720.0  # 此刻 − 会议的 0 点
    assert manager.is_live(old.session.id)
    await second.hang_up()

    a, b = await store.list_connections(old.session.id)
    assert (a.t_from, a.t_to) == (0.0, 12.5)
    assert (b.t_from, b.t_to) == (720.0, 750.0)  # 空档照实留在时间轴上
    assert len(await store.list_sessions()) == 1  # 没有新建会议


async def test_attach_reopens_an_ended_session(manager, store, clock):
    bot = Bot(manager)
    old = await bot.wait_started()
    await manager.end(old.session.id)
    await asyncio.wait_for(bot.task, 2)
    assert (await store.get_session(old.session.id)).ended_at is not None

    clock.t += 3600.0
    again = Bot(manager, session_id=old.session.id)
    live = await again.wait_started()
    assert live.session.ended_at is None
    assert (await store.get_session(old.session.id)).ended_at is None
    assert live.base_secs == 3600.0
    await again.hang_up()


async def test_attach_to_a_session_that_does_not_exist(manager, store):
    with pytest.raises(SessionNotFound):
        await manager.attach("nope")
    assert manager.live is None and await store.list_sessions() == []


async def test_attach_takes_over_a_connection_on_the_same_session(manager, store, clock):
    first = Bot(manager, elapsed=40.0)
    old = await first.wait_started()
    clock.t += 60.0
    second = Bot(manager, session_id=old.session.id)  # 另一台设备点了「继续」
    live = await second.wait_started()
    assert first.worker.messages("session_closed") == [
        {"type": "session_closed", "reason": "taken_over"}
    ]
    await asyncio.wait_for(first.task, 2)
    assert manager.live is live and live.base_secs == 60.0
    await second.hang_up()


async def test_the_timeline_never_runs_backwards_on_a_quick_reconnect(manager, store, clock):
    # 音频时钟比墙上时钟走得快了一点：上一次连接的终点是 12.5 秒，墙上只过了 12 秒就重连了
    first = Bot(manager, elapsed=12.5)
    old = await first.wait_started()
    await store.add_utterance(Utterance(old.session.id, 1, 10.0, 12.9, "最后一句"))
    await first.hang_up()
    clock.t += 12.0
    second = Bot(manager, session_id=old.session.id)
    live = await second.wait_started()
    assert live.base_secs == 12.9  # 不早于已经用掉的时间轴（连接终点和最后一条发言里更晚的）
    await second.hang_up()


# --------------------------------------------------------------------------- #
# 说话人区分的流跨连接复用
# --------------------------------------------------------------------------- #


class FakeDiarizer:
    max_speakers = 4

    def __init__(self):
        self.closed = 0
        self.found: list[SpeakerSegment] = []

    async def start(self): ...

    async def push_audio(self, pcm16): ...

    async def segments(self, since_secs=0.0):
        return list(self.found)

    async def close(self):
        self.closed += 1


class Builder:
    def __init__(self, result=None):
        self.built: list = []
        self.result = result

    async def __call__(self):
        made = self.result if self.result is not None else FakeDiarizer()
        self.built.append(made)
        return made


async def test_the_same_stream_serves_every_connection_of_a_session(manager, store, clock):
    build, notices = Builder(), []

    async def notice(level, text):
        notices.append((level, text))

    first = Bot(manager)
    old = await first.wait_started()
    stream = await manager.diarizer_for(old, build, notice)
    assert isinstance(stream, SessionDiarizer) and stream.speaker_offset == 0
    await store.add_utterance(Utterance(old.session.id, 2, 1.0, 2.0, "有两个人说过话"))
    await stream.push_audio(b"\x00" * 32000 * 5)
    await first.hang_up()
    await stream.close()

    clock.t += 300.0
    second = Bot(manager, session_id=old.session.id)
    live = await second.wait_started()
    again = await manager.diarizer_for(live, build, notice)
    assert again is stream and len(build.built) == 1  # 同一条流：说话人编号不变
    assert stream.anchors == [Anchor(0.0, 0.0), Anchor(5.0, 300.0)]
    assert stream.speaker_offset == 0 and notices == []
    assert build.built[0].closed == 0
    await second.hang_up()


async def test_after_a_restart_a_new_stream_is_numbered_after_the_known_speakers(
    manager, store, clock
):
    session = await store.create_session("重启之前开的会", now=clock.t - 900.0)
    await store.add_utterance(Utterance(session.id, 1, 1.0, 2.0, "甲"))
    await store.add_utterance(Utterance(session.id, 3, 3.0, 4.0, "丙"))
    build, notices = Builder(), []

    async def notice(level, text):
        notices.append((level, text))

    bot = Bot(manager, session_id=session.id)
    live = await bot.wait_started()
    stream = await manager.diarizer_for(live, build, notice)
    assert stream.speaker_offset == 3  # 已有的最大编号
    assert notices == [("info", RENUMBERED_NOTICE)]
    build.built[0].found = [SpeakerSegment(0.0, 1.0, 1)]
    (segment,) = await stream.segments()
    assert segment.speaker == 4  # 宁可当成新的人，也不和原来的 1 混在一起
    assert segment.start_secs == live.base_secs == 900.0
    await bot.hang_up()


async def test_a_resumed_session_without_speakers_needs_no_offset_or_notice(manager, store, clock):
    session = await store.create_session("还没人说话", now=clock.t - 60.0)
    notices = []

    async def notice(level, text):
        notices.append(text)

    bot = Bot(manager, session_id=session.id)
    live = await bot.wait_started()
    stream = await manager.diarizer_for(live, Builder(), notice)
    assert stream.speaker_offset == 0 and notices == []
    await bot.hang_up()


async def test_the_stream_is_released_when_the_meeting_changes_or_ends(manager, store):
    build = Builder()
    first = Bot(manager)
    old = await first.wait_started()
    await manager.diarizer_for(old, build)
    second = Bot(manager)  # 开始另一场新会议
    new = await second.wait_started()
    assert build.built[0].closed == 1
    await manager.diarizer_for(new, build)
    await manager.end(new.session.id)
    assert build.built[1].closed == 1
    await asyncio.wait_for(second.task, 2)

    # 继续第一场：它的流已经放掉了，只能新开
    third = Bot(manager, session_id=old.session.id)
    live = await third.wait_started()
    stream = await manager.diarizer_for(live, build)
    assert len(build.built) == 3 and isinstance(stream, SessionDiarizer)
    await manager.forget(old.session.id)  # 会议被删掉
    assert build.built[2].closed == 1
    await third.hang_up()


async def test_a_failed_stream_is_replaced_and_a_null_diarizer_is_not_kept(manager, store, clock):
    build = Builder()
    first = Bot(manager)
    old = await first.wait_started()
    stream = await manager.diarizer_for(old, build)
    stream.failed = True
    await first.hang_up()
    second = Bot(manager, session_id=old.session.id)
    live = await second.wait_started()
    fresh = await manager.diarizer_for(live, build)
    assert fresh is not stream and build.built[0].closed == 1
    await second.hang_up()

    null = NullDiarizer()
    third = Bot(manager, session_id=old.session.id)
    live = await third.wait_started()
    await manager.forget(old.session.id)
    assert await manager.diarizer_for(live, Builder(null)) is null  # 没开说话人区分：原样返回
    await third.hang_up()


async def test_shutdown_tells_the_page_and_releases_everything(manager, store):
    build = Builder()
    bot = Bot(manager)
    live = await bot.wait_started()
    await manager.diarizer_for(live, build)
    await manager.shutdown()
    assert bot.worker.messages("session_closed") == [
        {"type": "session_closed", "reason": "server_stopping"}
    ]
    await asyncio.wait_for(bot.task, 2)
    assert manager.live is None and build.built[0].closed == 1
    assert (await store.get_session(live.session.id)).ended_at is None  # 服务停了，会议只是中断
    await manager.shutdown()  # 没有连接时再调一次也没事


# --------------------------------------------------------------------------- #
# 合并说话人
# --------------------------------------------------------------------------- #


async def test_merging_speakers_moves_utterances_and_tells_the_page(manager, store):
    bot = Bot(manager)
    live = await bot.wait_started()
    sid = live.session.id
    stream = await manager.diarizer_for(live, Builder())
    await store.add_utterance(Utterance(sid, 1, 0.0, 1.0, "甲说的"))
    await store.add_utterance(Utterance(sid, 3, 2.0, 3.0, "其实也是甲"))
    await store.rename_speaker(sid, 1, "王老师")

    merged = await manager.merge_speakers(sid, 3, 1)
    assert merged == {"from": 3, "into": 1, "display_name": "王老师", "moved": 1}
    assert [u.utterance.speaker_idx for u in await store.all_utterances(sid)] == [1, 1]
    assert [s.idx for s in await store.list_speakers(sid)] == [1]
    assert bot.worker.messages("speakers_merged") == [
        {"type": "speakers_merged", "from": 3, "into": 1, "display_name": "王老师"}
    ]
    assert bot.recorder.names == {3: "王老师"}
    # 流里之后再输出 3，也算作 1
    stream._inner.found = [SpeakerSegment(0.0, 1.0, 3)]
    assert [s.speaker for s in await stream.segments()] == [1]

    assert await manager.merge_speakers(sid, 3, 1) is None  # 3 已经没有了
    assert await manager.merge_speakers(sid, 1, 9) is None
    await bot.hang_up()


async def test_merging_in_a_meeting_that_is_not_live_only_touches_the_database(manager, store):
    session = await store.create_session("回看")
    await store.add_utterance(Utterance(session.id, 1, 0.0, 1.0, "甲"))
    await store.add_utterance(Utterance(session.id, 2, 2.0, 3.0, "乙"))
    merged = await manager.merge_speakers(session.id, 2, 1)
    assert merged is not None and merged["moved"] == 1


async def test_finish_survives_a_store_error_and_still_releases_the_slot(
    manager, store, monkeypatch
):
    bot = Bot(manager)
    await bot.wait_started()

    async def broken(*args, **kwargs):
        raise RuntimeError("库坏了")

    monkeypatch.setattr(store, "close_connection", broken)
    await bot.hang_up()  # finish 里存储出错也不能抛出，更不能占着位置不放
    assert manager.live is None
    second = Bot(manager)
    await second.wait_started()
    await second.hang_up()


async def test_a_stop_requested_before_the_pipeline_is_ready_cancels_it_on_register(manager):
    live = await manager.begin()
    ender = asyncio.create_task(manager.end(live.session.id))  # 管线还没造好，先等着
    await asyncio.sleep(0.05)
    worker = FakeWorker()
    await manager.register(live, worker, FakeRecorder())
    assert worker.cancelled.is_set()  # 登记的那一刻立刻取消
    await manager.finish(live)
    ended = await ender
    assert ended is not None and ended.ended_at is not None


async def test_wait_idle(manager):
    await manager.wait_idle(0.05)  # 没有活动连接：立刻返回
    bot = Bot(manager)
    live = await bot.wait_started()
    started = asyncio.get_running_loop().time()
    await manager.wait_idle(0.1)  # 连接一直不结束：等到超时就返回，不会卡住
    assert 0.08 <= asyncio.get_running_loop().time() - started < 1.0
    waiter = asyncio.create_task(manager.wait_idle(2.0))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    await bot.hang_up()
    await asyncio.wait_for(waiter, 1.0)  # 连接收尾之后马上返回
    assert live.done.is_set()
