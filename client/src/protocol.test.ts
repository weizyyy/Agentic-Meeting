import assert from "node:assert/strict";
import { test } from "node:test";

import { fatalErrorText, parseServerMessage } from "./protocol.ts";

const caption = {
  type: "caption",
  segment_id: 3,
  speaker_idx: 0,
  speaker_name: "未知",
  t_start: 12.5,
  stable: "你好",
  unstable: "吗",
};

test("合法的 caption 原样通过", () => {
  assert.deepEqual(parseServerMessage(caption), caption);
});

test("caption 缺字段或类型不对返回 null", () => {
  for (const key of Object.keys(caption).filter((k) => k !== "type")) {
    const broken: Record<string, unknown> = { ...caption };
    delete broken[key];
    assert.equal(parseServerMessage(broken), null, `缺 ${key}`);
  }
  assert.equal(parseServerMessage({ ...caption, segment_id: "3" }), null);
  assert.equal(parseServerMessage({ ...caption, stable: 5 }), null);
});

test("assistant_state 只认四种状态", () => {
  for (const state of ["idle", "listening", "thinking", "speaking"]) {
    assert.deepEqual(parseServerMessage({ type: "assistant_state", state }), {
      type: "assistant_state",
      state,
    });
  }
  assert.equal(parseServerMessage({ type: "assistant_state", state: "dancing" }), null);
});

test("notice 需要合法的 level 和文字", () => {
  assert.deepEqual(
    parseServerMessage({ type: "notice", level: "warn", text: "识别服务暂时不可用" }),
    { type: "notice", level: "warn", text: "识别服务暂时不可用" },
  );
  assert.equal(parseServerMessage({ type: "notice", level: "fatal", text: "x" }), null);
  assert.equal(parseServerMessage({ type: "notice", level: "info" }), null);
});

test("不认识的类型、非对象一律返回 null，不抛异常", () => {
  for (const value of [null, undefined, 7, "caption", [], { type: "utterance" }, { type: 5 }, {}]) {
    assert.equal(parseServerMessage(value), null);
  }
});

const utterance = {
  type: "utterance",
  id: 7,
  segment_id: 3,
  speaker_idx: 2,
  speaker_name: "王老师",
  t_start: 12.5,
  t_end: 15,
  text: "我们先看验证集",
  source: "asr",
};

test("合法的 utterance 原样通过；id 和 segment_id 可以是 null", () => {
  assert.deepEqual(parseServerMessage(utterance), utterance);
  const unsaved = { ...utterance, id: null, segment_id: null, source: "assistant" };
  assert.deepEqual(parseServerMessage(unsaved), unsaved);
});

test("utterance 缺字段、类型不对、来源不认识都返回 null", () => {
  for (const key of Object.keys(utterance).filter((k) => k !== "type")) {
    const broken: Record<string, unknown> = { ...utterance };
    delete broken[key];
    assert.equal(parseServerMessage(broken), null, `缺 ${key}`);
  }
  assert.equal(parseServerMessage({ ...utterance, source: "tv" }), null);
  assert.equal(parseServerMessage({ ...utterance, id: "7" }), null);
  assert.equal(parseServerMessage({ ...utterance, text: null }), null);
  assert.deepEqual(parseServerMessage({ ...utterance, source: "text" })?.type, "utterance");
});

test("utterance_update 和 speaker", () => {
  const update = { type: "utterance_update", id: 7, speaker_idx: 3, speaker_name: "说话人 3" };
  assert.deepEqual(parseServerMessage(update), update);
  assert.equal(parseServerMessage({ ...update, id: null }), null);
  const rename = { type: "speaker", idx: 2, display_name: "王老师" };
  assert.deepEqual(parseServerMessage(rename), rename);
  assert.equal(parseServerMessage({ type: "speaker", idx: "2", display_name: "x" }), null);
  assert.equal(parseServerMessage({ type: "speaker", idx: 2 }), null);
});

test("session 与 session_closed", () => {
  const session = {
    type: "session",
    keep: false,
    id: "abc",
    title: "",
    started_at: 1790000000.5,
    resumed: false,
    base_secs: 0,
    state: "live",
  };
  assert.deepEqual(parseServerMessage(session), session);
  assert.equal(parseServerMessage({ ...session, resumed: "no" }), null);
  assert.deepEqual(parseServerMessage({ ...session, keep: true }), { ...session, keep: true });
  for (const keep of [undefined, null, 0, "false"])
    assert.equal(parseServerMessage({ ...session, keep }), null);
  for (const reason of ["taken_over", "ended", "server_stopping"]) {
    assert.deepEqual(parseServerMessage({ type: "session_closed", reason }), {
      type: "session_closed",
      reason,
    });
  }
  assert.equal(parseServerMessage({ type: "session_closed", reason: "bored" }), null);
});

test("frame 与 frame_caption", () => {
  const frame = { type: "frame", id: 7, t: 62.5, width: 1920, height: 1080 };
  assert.deepEqual(parseServerMessage(frame), frame);
  assert.deepEqual(parseServerMessage({ ...frame, extra: 1 }), frame); // 多余字段丢掉
  assert.equal(parseServerMessage({ ...frame, id: "7" }), null);
  assert.equal(parseServerMessage({ type: "frame", id: 7, t: 1, width: 10 }), null);
  const caption = { type: "frame_caption", id: 7, caption: "幻灯片：消融实验结果表" };
  assert.deepEqual(parseServerMessage(caption), caption);
  assert.equal(parseServerMessage({ type: "frame_caption", id: 7, caption: null }), null);
  assert.equal(parseServerMessage({ type: "frame_caption", caption: "x" }), null);
});

test("task 与 task_event", () => {
  const task = {
    type: "task",
    id: "abc.t1",
    label: "t1",
    goal: "核实引用数",
    status: "running",
    brief: null,
    error: null,
    modality: "text",
    created_at: 1790000000,
  };
  assert.deepEqual(parseServerMessage(task), task);
  // 可选字段缺了按 null；不认识的模态按语音
  assert.deepEqual(
    parseServerMessage({
      type: "task",
      id: "a.t2",
      label: "t2",
      goal: "x",
      status: "queued",
      created_at: 1,
    }),
    {
      type: "task",
      id: "a.t2",
      label: "t2",
      goal: "x",
      status: "queued",
      brief: null,
      error: null,
      modality: "voice",
      created_at: 1,
    },
  );
  assert.equal(parseServerMessage({ ...task, status: "exploded" }), null);
  assert.equal(parseServerMessage({ ...task, id: 5 }), null);
  assert.equal(parseServerMessage({ ...task, brief: 5 }), null);
  const event = {
    type: "task_event",
    task_id: "abc.t1",
    at: 1790000001.5,
    kind: "tool_call",
    summary: "正在检索",
  };
  assert.deepEqual(parseServerMessage(event), event);
  assert.equal(parseServerMessage({ ...event, summary: null }), null);
});

test("speakers_merged", () => {
  const merged = { type: "speakers_merged", from: 3, into: 1, display_name: "王老师" };
  assert.deepEqual(parseServerMessage(merged), merged);
  assert.equal(parseServerMessage({ ...merged, into: "1" }), null);
  assert.equal(parseServerMessage({ type: "speakers_merged", from: 3, into: 1 }), null);
});

test("RTVI 的 error 消息只有致命的才显示", () => {
  assert.equal(fatalErrorText({ error: "Error during completion", fatal: false }), null);
  assert.equal(fatalErrorText({ error: "boom" }), null);
  assert.equal(fatalErrorText("boom"), null);
  assert.equal(fatalErrorText({ error: "boom", fatal: true }), "服务端出错：boom");
  assert.equal(fatalErrorText({ fatal: true }), "服务端出错，连接可能已经中断");
});
