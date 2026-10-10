import assert from "node:assert/strict";
import { test } from "node:test";

import type { SessionDetail, SessionSummary } from "./api.ts";
import type { HistoryItem } from "./captions.ts";
import {
  MAX_NOTICES,
  PAGE_SIZE,
  SESSION_CLOSED_TEXT,
  connectionOf,
  initialState,
  reduce,
  type Action,
  type MeetingState,
} from "./meetingState.ts";
import type { CaptionMessage, UtteranceMessage } from "./protocol.ts";

function run(actions: Action[], from: MeetingState = initialState): MeetingState {
  return actions.reduce(reduce, from);
}

test("传输层状态映射成界面的连接状态", () => {
  assert.equal(connectionOf("ready"), "connected");
  assert.equal(connectionOf("error"), "error");
  assert.equal(connectionOf("disconnected"), "disconnected");
  assert.equal(connectionOf("disconnecting"), "disconnected");
  const inProgress = [
    "initializing",
    "initialized",
    "authenticating",
    "authenticated",
    "connecting",
    "connected",
  ];
  for (const s of inProgress) assert.equal(connectionOf(s), "connecting", s);
});

test("助理的流式文字：开始一轮清空，随后逐段追加", () => {
  const state = run([
    { type: "llmStarted" },
    { type: "llmText", text: "好的，" },
    { type: "llmText", text: "马上整理。" },
  ]);
  assert.equal(state.assistantText, "好的，马上整理。");
  assert.equal(state.assistantState, "thinking");

  const next = run([{ type: "llmStarted" }, { type: "llmText", text: "第二轮" }], state);
  assert.equal(next.assistantText, "第二轮");
});

test("断开连接：助理状态回到空闲，字幕和助理最后一轮的话保留", () => {
  const live = run([
    { type: "transport", transport: "ready" },
    {
      type: "caption",
      message: {
        type: "caption",
        segment_id: 1,
        speaker_idx: 0,
        speaker_name: "未知",
        t_start: 0,
        stable: "你好",
        unstable: "",
      },
    },
    { type: "llmStarted" },
    { type: "llmText", text: "在。" },
    { type: "assistantState", state: "speaking" },
  ]);
  const after = reduce(live, { type: "transport", transport: "disconnected" });
  assert.equal(after.connection, "disconnected");
  assert.equal(after.assistantState, "idle");
  assert.equal(after.captions.length, 1);
  assert.equal(after.assistantText, "在。");
});

test("连接过程中收到的状态变化不会清掉助理状态", () => {
  const state = run([
    { type: "assistantState", state: "listening" },
    { type: "transport", transport: "ready" },
  ]);
  assert.equal(state.assistantState, "listening");
});

test("提示：编号递增，只保留最近几条，可以逐条关闭", () => {
  let state = initialState;
  for (let i = 0; i < MAX_NOTICES + 2; i++) {
    state = reduce(state, { type: "notice", level: "warn", text: `第${i}条` });
  }
  assert.equal(state.notices.length, MAX_NOTICES);
  assert.equal(state.notices[0].text, "第2条");
  const id = state.notices[1].id;
  state = reduce(state, { type: "dismissNotice", id });
  assert.equal(state.notices.length, MAX_NOTICES - 1);
  assert.ok(!state.notices.some((n) => n.id === id));
});

function cap(id: number, stable: string, unstable = "", idx = 0, name = "未知"): CaptionMessage {
  return {
    type: "caption",
    segment_id: id,
    speaker_idx: idx,
    speaker_name: name,
    t_start: id,
    stable,
    unstable,
  };
}

function utt(
  id: number | null,
  segment: number | null,
  text: string,
  idx = 1,
  name = "说话人 1",
): UtteranceMessage {
  return {
    type: "utterance",
    id,
    segment_id: segment,
    speaker_idx: idx,
    speaker_name: name,
    t_start: 1,
    t_end: 2,
    text,
    source: "asr",
  };
}

function summary(id: string, over: Partial<SessionSummary> = {}): SessionSummary {
  return {
    id,
    title: "",
    started_at: 100,
    keep: false,
    deletion_pending: false,
    ended_at: null,
    last_active_at: 100,
    state: "interrupted",
    duration_secs: 0,
    utterance_count: 0,
    speakers: [],
    preview: [],
    ...over,
  };
}

function detail(id: string, over: Partial<SessionDetail> = {}): SessionDetail {
  return { ...summary(id), screen: {}, members: ["王老师", "李同学"], connections: [], ...over };
}

function history(from: number, count: number): HistoryItem[] {
  return Array.from({ length: count }, (_, i) => ({
    id: from + i,
    speaker_idx: 1,
    speaker_name: "说话人 1",
    t_start: from + i,
    t_end: from + i + 1,
    text: `第${from + i}句`,
    source: "asr" as const,
  }));
}

test("发言定稿：替换对应的实时字幕，并把新出现的说话人加进列表", () => {
  const state = run([
    { type: "caption", message: cap(1, "你好", "", 2, "说话人 2") },
    { type: "utterance", message: utt(10, 1, "你好。", 2, "说话人 2") },
  ]);
  assert.deepEqual(
    state.captions.map((l) => [l.final, l.utteranceId, l.stable]),
    [[true, 10, "你好。"]],
  );
  assert.deepEqual(state.speakers, [{ idx: 2, display_name: "说话人 2" }]);
});

test("说话人改名和事后更正：字幕行与说话人列表一起变", () => {
  let state = run([
    { type: "utterance", message: utt(10, null, "甲", 1, "说话人 1") },
    { type: "utterance", message: utt(11, null, "乙", 2, "说话人 2") },
  ]);
  state = run([{ type: "speakerRenamed", idx: 2, displayName: "王老师" }], state);
  assert.deepEqual(
    state.captions.map((l) => l.speakerName),
    ["说话人 1", "王老师"],
  );
  assert.deepEqual(
    state.speakers.map((s) => s.display_name),
    ["说话人 1", "王老师"],
  );
  state = run(
    [
      {
        type: "utteranceUpdate",
        message: { type: "utterance_update", id: 10, speaker_idx: 2, speaker_name: "王老师" },
      },
    ],
    state,
  );
  assert.deepEqual(
    state.captions.map((l) => l.speakerIdx),
    [2, 2],
  );
});

test("新会议的连接建立：字幕区切到它并清空；同一场会议则保留已有内容", () => {
  const message = {
    type: "session" as const,
    keep: false,
    id: "new",
    title: "",
    started_at: 5,
    resumed: false,
    base_secs: 0,
    state: "live",
  };
  const before = run([
    {
      type: "viewLoaded",
      detail: detail("old"),
      items: history(1, 3),
      speakers: [{ idx: 1, display_name: "说话人 1" }],
    },
  ]);
  const switched = reduce(before, { type: "sessionStarted", message });
  assert.equal(switched.viewing?.id, "new");
  assert.equal(switched.liveSessionId, "new");
  assert.deepEqual(
    [switched.captions.length, switched.speakers.length, switched.hasOlder],
    [0, 0, false],
  );

  const again = reduce(switched, { type: "caption", message: cap(1, "话") });
  const same = reduce(again, { type: "sessionStarted", message });
  assert.equal(same.captions.length, 1); // 同一场会议的重复通知不清空
});

test("断开连接后不再有进行中的会议编号，字幕区仍显示这场会议", () => {
  const live = run([
    {
      type: "sessionStarted",
      message: {
        type: "session",
        keep: false,
        id: "s1",
        title: "",
        started_at: 1,
        resumed: false,
        base_secs: 0,
        state: "live",
      },
    },
    { type: "transport", transport: "ready" },
  ]);
  assert.equal(live.liveSessionId, "s1");
  const after = reduce(live, { type: "transport", transport: "disconnected" });
  assert.equal(after.liveSessionId, null);
  assert.equal(after.viewing?.id, "s1");
});

test("连接被关闭的原因变成一条提示", () => {
  for (const reason of ["taken_over", "ended", "server_stopping"] as const) {
    const state = reduce(initialState, {
      type: "sessionClosed",
      message: { type: "session_closed", reason },
    });
    assert.equal(state.notices[0].text, SESSION_CLOSED_TEXT[reason]);
    assert.equal(state.notices[0].level, "warn");
  }
});

test("页面一打开：显示最近的对话；不足一页就没有更早的", () => {
  const state = run([
    {
      type: "viewLoaded",
      detail: detail("s1"),
      items: history(1, 3),
      speakers: [{ idx: 1, display_name: "说话人 1" }],
    },
  ]);
  assert.equal(state.viewing?.id, "s1");
  assert.equal(state.captions.length, 3);
  assert.equal(state.hasOlder, false);
  assert.deepEqual(state.members, ["王老师", "李同学"]);

  const full = run([
    { type: "viewLoaded", detail: detail("s1"), items: history(1, PAGE_SIZE), speakers: [] },
  ]);
  assert.equal(full.hasOlder, true);
});

test("没有任何会议：字幕区是空的", () => {
  const state = run([{ type: "viewLoaded", detail: null, items: [], speakers: [] }]);
  assert.equal(state.viewing, null);
  assert.deepEqual([state.captions.length, state.hasOlder], [0, false]);
});

test("向上翻页和断线补齐", () => {
  let state = run([
    { type: "viewLoaded", detail: detail("s1"), items: history(51, PAGE_SIZE), speakers: [] },
  ]);
  state = run([{ type: "olderLoaded", items: history(1, PAGE_SIZE) }], state);
  assert.equal(state.captions[0].utteranceId, 1);
  assert.equal(state.captions.length, PAGE_SIZE * 2);
  assert.equal(state.hasOlder, true);
  state = run([{ type: "olderLoaded", items: history(0, 1) }], state);
  assert.equal(state.hasOlder, false);

  state = run([{ type: "backfilled", items: history(100, 2) }], state);
  assert.deepEqual(
    state.captions.slice(-2).map((l) => l.utteranceId),
    [100, 101],
  );
});

test("会议列表：加载、改名、删除", () => {
  let state = run([
    { type: "sessionsLoaded", items: [summary("a"), summary("b")] },
    { type: "viewLoaded", detail: detail("a"), items: history(1, 2), speakers: [] },
  ]);
  state = run([{ type: "sessionUpdated", summary: summary("a", { title: "周一组会" }) }], state);
  assert.equal(state.viewing?.title, "周一组会");
  assert.equal(state.sessions[0].title, "周一组会");
  assert.equal(state.sessions[1].title, "");

  state = run([{ type: "sessionRemoved", id: "b" }], state);
  assert.deepEqual(
    state.sessions.map((x) => x.id),
    ["a"],
  );
  assert.equal(state.viewing?.id, "a"); // 删的不是正在看的

  state = run([{ type: "sessionRemoved", id: "a" }], state);
  assert.deepEqual([state.sessions.length, state.viewing, state.captions.length], [0, null, 0]);
});

test("应答模式：文字请求 → 文字回答；被叫到名字或开始朗读 → 语音回答；其余状态变化不改它", () => {
  assert.equal(initialState.replyMode, null);
  let state = run([{ type: "textSent" }]);
  assert.equal(state.replyMode, "text");
  state = run([{ type: "llmStarted" }, { type: "assistantState", state: "idle" }], state);
  assert.equal(state.replyMode, "text"); // 思考、待命都不改变这一轮的模式
  state = run([{ type: "assistantState", state: "listening" }], state);
  assert.equal(state.replyMode, "voice");
  state = run([{ type: "textSent" }, { type: "assistantState", state: "speaking" }], state);
  assert.equal(state.replyMode, "voice");
});

// ---- 截图时间线 ----

const frameOf = (id: number, t: number, caption: string | null = null) => ({
  id,
  t,
  width: 16,
  height: 9,
  caption,
});

const detailOf = (id: string, screen: Record<string, unknown> = {}): SessionDetail => ({
  id,
  title: "",
  started_at: 1,
  keep: false,
  deletion_pending: false,
  ended_at: null,
  last_active_at: 1,
  state: "interrupted",
  duration_secs: 0,
  utterance_count: 0,
  speakers: [],
  preview: [],
  screen,
  members: [],
  connections: [],
});

const sessionMessage = (id: string) => ({
  type: "session" as const,
  keep: false,
  id,
  title: "",
  started_at: 1,
  resumed: false,
  base_secs: 0,
  state: "live",
});

test("截图：新截图进时间线，摘要随后补上", () => {
  const state = run([
    { type: "sessionStarted", message: sessionMessage("s1") },
    { type: "frame", frame: { id: 2, t: 20, width: 16, height: 9 } },
    { type: "frame", frame: { id: 1, t: 10, width: 16, height: 9 } },
    { type: "frameCaption", id: 2, caption: "一张表" },
    { type: "frameCaption", id: 99, caption: "不认识的" },
  ]);
  assert.deepEqual(state.frames, [frameOf(1, 10), frameOf(2, 20, "一张表")]);
});

test("截图：换一场会议时清空，列表取回来后填上；过期的响应丢掉", () => {
  let state = run([
    { type: "viewLoaded", detail: detailOf("a"), items: [], speakers: [] },
    { type: "framesLoaded", sessionId: "a", items: [frameOf(1, 10)], merge: false },
  ]);
  assert.equal(state.frames.length, 1);
  // 同一场会议重新加载（比如断开后刷新）：时间线不闪一下
  state = reduce(state, { type: "viewLoaded", detail: detailOf("a"), items: [], speakers: [] });
  assert.equal(state.frames.length, 1);
  state = reduce(state, { type: "viewLoaded", detail: detailOf("b"), items: [], speakers: [] });
  assert.deepEqual(state.frames, []);
  // 给 a 的响应这时才到：不能混进 b
  state = reduce(state, {
    type: "framesLoaded",
    sessionId: "a",
    items: [frameOf(1, 10)],
    merge: false,
  });
  assert.deepEqual(state.frames, []);
  state = reduce(state, {
    type: "framesLoaded",
    sessionId: "b",
    items: [frameOf(7, 5)],
    merge: false,
  });
  assert.deepEqual(state.frames, [frameOf(7, 5)]);
});

test("截图：重连后的列表是合并，不丢掉刚收到的", () => {
  const state = run([
    { type: "sessionStarted", message: sessionMessage("s1") },
    { type: "frame", frame: { id: 3, t: 30, width: 16, height: 9 } },
    { type: "framesLoaded", sessionId: "s1", items: [frameOf(1, 10, "旧的")], merge: true },
  ]);
  assert.deepEqual(state.frames, [frameOf(1, 10, "旧的"), frameOf(3, 30)]);
  const replaced = reduce(state, {
    type: "framesLoaded",
    sessionId: "s1",
    items: [frameOf(1, 10, "旧的")],
    merge: false,
  });
  assert.deepEqual(replaced.frames, [frameOf(1, 10, "旧的")]);
});

test("截图：新会议开始时清空上一场的时间线；删除正在看的会议也清空", () => {
  let state = run([
    { type: "viewLoaded", detail: detailOf("old"), items: [], speakers: [] },
    { type: "framesLoaded", sessionId: "old", items: [frameOf(1, 10)], merge: false },
    { type: "sessionStarted", message: sessionMessage("new") },
  ]);
  assert.deepEqual(state.frames, []);
  state = run(
    [
      { type: "frame", frame: { id: 2, t: 1, width: 16, height: 9 } },
      { type: "sessionRemoved", id: "other" },
    ],
    state,
  );
  assert.equal(state.frames.length, 1);
  state = reduce(state, { type: "sessionRemoved", id: "new" });
  assert.deepEqual(state.frames, []);
});

test("截图参数来自服务端的 screen 段；共享状态在断开时复位", () => {
  let state = reduce(initialState, {
    type: "viewLoaded",
    detail: detailOf("a", { enabled: false, heartbeat_secs: 30 }),
    items: [],
    speakers: [],
  });
  assert.equal(state.screen.enabled, false);
  assert.equal(state.screen.heartbeatSecs, 30);
  // 没有任何会议时保留原来的参数
  state = reduce(state, { type: "viewLoaded", detail: null, items: [], speakers: [] });
  assert.equal(state.screen.heartbeatSecs, 30);

  state = run(
    [
      { type: "transport", transport: "ready" },
      { type: "sharing", sharing: true },
    ],
    state,
  );
  assert.equal(state.sharing, true);
  assert.equal(reduce(state, { type: "transport", transport: "connecting" }).sharing, true);
  assert.equal(reduce(state, { type: "transport", transport: "disconnected" }).sharing, false);
  assert.equal(reduce(state, { type: "transport", transport: "error" }).sharing, false);
});

// ---- 后台任务 ----

const taskOf = (id: string, created: number, status: "running" | "succeeded" = "running") => ({
  id,
  label: id.split(".").pop() ?? id,
  goal: "核实引用数",
  status,
  brief: null,
  error: null,
  modality: "voice" as const,
  created_at: created,
});

test("任务：消息进面板，进度记到最近一步，别的会议的任务不混进来", () => {
  let state = run([
    { type: "sessionStarted", message: sessionMessage("s1") },
    { type: "task", task: taskOf("s1.t1", 10) },
    { type: "taskEvent", taskId: "s1.t1", summary: "正在检索「引用数」" },
    { type: "task", task: taskOf("other.t1", 5) },
    { type: "taskEvent", taskId: "other.t1", summary: "别的会议的进度" },
  ]);
  assert.deepEqual(
    state.tasks.map((t) => [t.id, t.lastStep]),
    [["s1.t1", "正在检索「引用数」"]],
  );
  state = reduce(state, { type: "task", task: taskOf("s1.t1", 10, "succeeded") });
  assert.equal(state.tasks[0].status, "succeeded");
  assert.equal(state.tasks[0].lastStep, "正在检索「引用数」");
});

test("任务：换会议时清空，列表取回来后填上；过期的响应丢掉", () => {
  let state = run([
    { type: "viewLoaded", detail: detailOf("a"), items: [], speakers: [] },
    { type: "tasksLoaded", sessionId: "a", items: [taskOf("a.t1", 10)] },
  ]);
  assert.equal(state.tasks.length, 1);
  state = reduce(state, { type: "viewLoaded", detail: detailOf("a"), items: [], speakers: [] });
  assert.equal(state.tasks.length, 1); // 同一场会议重新加载：不闪
  state = reduce(state, { type: "viewLoaded", detail: detailOf("b"), items: [], speakers: [] });
  assert.deepEqual(state.tasks, []);
  state = reduce(state, { type: "tasksLoaded", sessionId: "a", items: [taskOf("a.t1", 10)] });
  assert.deepEqual(state.tasks, []);
  state = reduce(state, { type: "sessionStarted", message: sessionMessage("c") });
  state = reduce(state, { type: "task", task: taskOf("c.t1", 1) });
  assert.equal(state.tasks.length, 1);
  state = reduce(state, { type: "sessionRemoved", id: "c" });
  assert.deepEqual(state.tasks, []);
});

// ---- 继续会议 ----

const resumedDetail = (id: string, extra: Partial<SessionDetail> = {}): SessionDetail => ({
  id,
  title: "周三组会",
  started_at: 1000,
  keep: false,
  deletion_pending: false,
  ended_at: null,
  last_active_at: 1100,
  state: "interrupted",
  duration_secs: 60,
  utterance_count: 1,
  speakers: [],
  preview: [],
  screen: {},
  members: [],
  connections: [{ connected_at: 1000, disconnected_at: 1060, t_from: 0, t_to: 60 }],
  ...extra,
});

test("服务端说明的断开原因被记下，新一次连接开始时清掉", () => {
  const closed = reduce(initialState, {
    type: "sessionClosed",
    message: { type: "session_closed", reason: "taken_over" },
  });
  assert.equal(closed.closedReason, "taken_over");
  assert.equal(reduce(closed, { type: "connecting" }).closedReason, null);
});

test("自动重连：记下第几次，连上或放弃后归零", () => {
  const trying = run([{ type: "reconnecting", attempt: 2 }]);
  assert.equal(trying.reconnectAttempt, 2);
  // 重连过程中传输层在「连接中」「断开」之间来回，不清掉计数
  assert.equal(reduce(trying, { type: "transport", transport: "connecting" }).reconnectAttempt, 2);
  assert.equal(
    reduce(trying, { type: "transport", transport: "disconnected" }).reconnectAttempt,
    2,
  );
  assert.equal(reduce(trying, { type: "transport", transport: "ready" }).reconnectAttempt, 0);
  assert.equal(reduce(trying, { type: "reconnectStopped" }).reconnectAttempt, 0);
});

test("各次连接跟着正在显示的会议走", () => {
  const loaded = reduce(initialState, {
    type: "viewLoaded",
    detail: resumedDetail("s1"),
    items: [],
    speakers: [],
  });
  assert.equal(loaded.connections.length, 1);
  // 继续之后重新取到的会话信息：多了一次连接，状态变成进行中
  const two = [
    ...resumedDetail("s1").connections,
    { connected_at: 1780, disconnected_at: null, t_from: 780, t_to: null },
  ];
  const listed = { ...loaded, sessions: [resumedDetail("s1"), resumedDetail("s2")] };
  const refreshed = reduce(listed, {
    type: "detailRefreshed",
    detail: resumedDetail("s1", { state: "live", connections: two }),
  });
  assert.equal(refreshed.connections.length, 2);
  assert.equal(refreshed.viewing?.state, "live");
  assert.deepEqual(
    refreshed.sessions.map((s) => s.state),
    ["live", "interrupted"],
  );
  // 别的会议的信息不动正在显示的这场
  const other = reduce(refreshed, { type: "detailRefreshed", detail: resumedDetail("s2") });
  assert.equal(other.viewing?.id, "s1");
  assert.equal(other.connections.length, 2);
  // 切到别的会议、删掉正在看的会议：清掉
  const switched = reduce(refreshed, {
    type: "sessionStarted",
    message: {
      type: "session",
      keep: false,
      id: "s9",
      title: "",
      started_at: 5000,
      resumed: false,
      base_secs: 0,
      state: "live",
    },
  });
  assert.deepEqual(switched.connections, []);
  assert.deepEqual(reduce(refreshed, { type: "sessionRemoved", id: "s1" }).connections, []);
});

test("继续同一场会议：字幕和连接记录都留着", () => {
  const loaded = reduce(initialState, {
    type: "viewLoaded",
    detail: resumedDetail("s1"),
    items: [
      {
        id: 1,
        speaker_idx: 1,
        speaker_name: "王老师",
        t_start: 3,
        t_end: 5,
        text: "断线之前说的",
        source: "asr",
      },
    ],
    speakers: [{ idx: 1, display_name: "王老师" }],
  });
  const resumed = reduce(loaded, {
    type: "sessionStarted",
    message: {
      type: "session",
      keep: false,
      id: "s1",
      title: "周三组会",
      started_at: 1000,
      resumed: true,
      base_secs: 780,
      state: "live",
    },
  });
  assert.equal(resumed.liveSessionId, "s1");
  assert.equal(resumed.captions.length, 1);
  assert.equal(resumed.connections.length, 1);
  assert.deepEqual(resumed.speakers, [{ idx: 1, display_name: "王老师" }]);
});

test("合并说话人：发言改到对方名下，被合并的人从列表里消失", () => {
  const history = (id: number, speaker_idx: number, speaker_name: string): HistoryItem => ({
    id,
    speaker_idx,
    speaker_name,
    t_start: id,
    t_end: id + 1,
    text: `第 ${id} 句`,
    source: "asr",
  });
  const loaded = reduce(initialState, {
    type: "viewLoaded",
    detail: resumedDetail("s1"),
    items: [history(1, 1, "王老师"), history(2, 3, "说话人 3"), history(3, 2, "小李")],
    speakers: [
      { idx: 1, display_name: "王老师" },
      { idx: 2, display_name: "小李" },
      { idx: 3, display_name: "说话人 3" },
    ],
  });
  const merged = reduce(loaded, {
    type: "speakersMerged",
    from: 3,
    into: 1,
    displayName: "王老师",
  });
  assert.deepEqual(
    merged.captions.map((l) => [l.speakerIdx, l.speakerName]),
    [
      [1, "王老师"],
      [1, "王老师"],
      [2, "小李"],
    ],
  );
  assert.deepEqual(
    merged.speakers.map((s) => s.idx),
    [1, 2],
  );
  // 同一条消息到两次（HTTP 的响应和数据通道各一次）结果一样
  assert.deepEqual(
    reduce(merged, { type: "speakersMerged", from: 3, into: 1, displayName: "王老师" }),
    merged,
  );
});

test("会后报告跟着正在显示的会议走", () => {
  const info = {
    id: 1,
    status: "running",
    created_at: 1,
    provider: "realtime_llm",
    text_md: "",
    error: null,
  } as const;
  const loaded = reduce(initialState, {
    type: "viewLoaded",
    detail: resumedDetail("s1"),
    items: [],
    speakers: [],
  });
  const withReport = reduce(loaded, { type: "reportLoaded", sessionId: "s1", report: info });
  assert.equal(withReport.report?.status, "running");
  // 别的会议的报告（请求回来时已经切走了）不收
  assert.equal(
    reduce(loaded, { type: "reportLoaded", sessionId: "s2", report: info }).report,
    null,
  );
  // 重新加载同一场：留着；切到另一场、删掉：清掉
  const again = reduce(withReport, {
    type: "viewLoaded",
    detail: resumedDetail("s1"),
    items: [],
    speakers: [],
  });
  assert.equal(again.report?.id, 1);
  const other = reduce(withReport, {
    type: "viewLoaded",
    detail: resumedDetail("s2"),
    items: [],
    speakers: [],
  });
  assert.equal(other.report, null);
  assert.equal(reduce(withReport, { type: "sessionRemoved", id: "s1" }).report, null);
  assert.equal(
    reduce(withReport, { type: "reportLoaded", sessionId: "s1", report: null }).report,
    null,
  );
});

test("待删除详情清内容，列表同步保留状态，迟到内容不重新显示", () => {
  let state = reduce(initialState, {
    type: "viewLoaded",
    detail: detail("s1"),
    items: [],
    speakers: [],
  });
  state = { ...state, sessions: [summary("s1"), summary("s2")] };
  state = reduce(state, {
    type: "detailRefreshed",
    detail: detail("s1", { keep: true, deletion_pending: true }),
  });
  assert.equal(state.sessions[0].keep, true);
  assert.equal(state.sessions[0].deletion_pending, true);
  assert.equal(state.sessions[1].deletion_pending, false);
  assert.deepEqual(state.captions, []);
  assert.equal(state.hasOlder, false);
  assert.equal(reduce(state, { type: "reportLoaded", sessionId: "s1", report: null }), state);
});
test("关闭立刻停指示，同ready不恢复；SDK重连无需session消息恢复", () => {
  let state = reduce(initialState, { type: "transport", transport: "ready" });
  state = reduce(state, {
    type: "sessionClosed",
    message: { type: "session_closed", reason: "taken_over" },
  });
  assert.equal(state.liveSessionId, null);
  assert.equal(reduce(state, { type: "transport", transport: "ready" }).closedReason, "taken_over");
  state = reduce(state, { type: "transport", transport: "disconnected" });
  state = reduce(state, { type: "transport", transport: "ready" });
  assert.equal(state.connection, "connected");
  assert.equal(state.closedReason, null);
});
test("保留成功只更新目标摘要，不影响另一会议", () => {
  const before = {
    ...initialState,
    viewing: summary("s2"),
    sessions: [summary("s1"), summary("s2")],
  };
  const after = reduce(before, { type: "sessionUpdated", summary: summary("s1", { keep: true }) });
  assert.equal(after.viewing, before.viewing);
  assert.equal(after.sessions[0].keep, true);
  assert.equal(before.sessions[0].keep, false);
});

test("旧查看请求的404清空与成功返回都不能覆盖后来选中的会议", () => {
  let state = reduce(initialState, { type: "viewRequested", requestId: 1 });
  state = reduce(state, { type: "viewRequested", requestId: 2 });
  state = reduce(state, {
    type: "viewLoaded",
    requestId: 2,
    detail: detail("s2"),
    items: [],
    speakers: [],
  });
  assert.equal(
    reduce(state, { type: "viewLoaded", requestId: 1, detail: null, items: [], speakers: [] }),
    state,
  );
  assert.equal(
    reduce(state, {
      type: "viewLoaded",
      requestId: 1,
      detail: detail("s1"),
      items: [],
      speakers: [],
    }),
    state,
  );
});
test("待删除不可被迟到保留或改名成功摘要撤销", () => {
  const state = {
    ...initialState,
    viewing: summary("s1", { deletion_pending: true }),
    sessions: [summary("s1", { deletion_pending: true })],
  };
  assert.equal(
    reduce(state, { type: "sessionUpdated", summary: summary("s1", { keep: true }) }),
    state,
  );
});

test("旧详情和列表响应不能撤销已知待删除", () => {
  const pending = summary("s1", { deletion_pending: true });
  const state = { ...initialState, viewing: pending, sessions: [pending] };
  assert.equal(reduce(state, { type: "detailRefreshed", detail: detail("s1") }), state);
  assert.equal(
    reduce(state, { type: "viewLoaded", detail: detail("s1"), items: [], speakers: [] }),
    state,
  );
  const listed = reduce(state, { type: "sessionsLoaded", items: [summary("s1"), summary("s2")] });
  assert.equal(listed.sessions[0].deletion_pending, true);
  assert.equal(listed.sessions[1].deletion_pending, false);
  assert.deepEqual(reduce(listed, { type: "sessionsLoaded", items: [summary("s2")] }).sessions, [
    summary("s2"),
  ]);
});

test("列表发现当前会议待删除时详情也立即禁内容，另一会议不受影响", () => {
  const state = {
    ...initialState,
    viewing: summary("s1"),
    sessions: [summary("s1"), summary("s2")],
  };
  const updated = reduce(state, {
    type: "sessionsLoaded",
    items: [summary("s1", { keep: true, deletion_pending: true }), summary("s2")],
  });
  assert.equal(updated.viewing?.deletion_pending, true);
  assert.equal(updated.viewing?.keep, true);
  assert.equal(updated.sessions[1].deletion_pending, false);
  assert.deepEqual(updated.frames, []);
});

test("待删除忽略非空的旧截图、任务、字幕和说话人响应", () => {
  const state = { ...initialState, viewing: summary("s1", { deletion_pending: true }) };
  for (const action of [
    { type: "framesLoaded", sessionId: "s1", items: [frameOf(1, 0)], merge: false },
    { type: "tasksLoaded", sessionId: "s1", items: [taskOf("s1.t1", 0)] },
    { type: "backfilled", items: history(1, 1) },
    { type: "olderLoaded", items: history(1, 1) },
    { type: "speakersLoaded", speakers: [{ idx: 1, display_name: "虚构成员" }] },
  ] as Action[])
    assert.equal(reduce(state, action), state);
});
