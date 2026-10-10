// 用 Node 自带的测试运行器：npm test（不引入测试框架依赖）。
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  appendHistory,
  applyCaption,
  applySpeakerRename,
  applyUtterance,
  applyUtteranceUpdate,
  firstUtteranceId,
  formatClock,
  lastUtteranceId,
  prependHistory,
  replaceWithHistory,
  type CaptionLine,
  type HistoryItem,
} from "./captions.ts";
import type { CaptionMessage, UtteranceMessage } from "./protocol.ts";

function caption(id: number, stable: string, unstable = "", speaker = "未知"): CaptionMessage {
  return {
    type: "caption",
    segment_id: id,
    speaker_idx: 0,
    speaker_name: speaker,
    t_start: id * 10,
    stable,
    unstable,
  };
}

test("新的 segment_id 追加成新的一行", () => {
  const lines = applyCaption(applyCaption([], caption(1, "你好")), caption(2, "再见"));
  assert.deepEqual(
    lines.map((l) => [l.segmentId, l.stable]),
    [
      [1, "你好"],
      [2, "再见"],
    ],
  );
});

test("同一个 segment_id 的后一条整体覆盖前一条，位置不变", () => {
  let lines: CaptionLine[] = [];
  lines = applyCaption(lines, caption(1, "你", "好"));
  lines = applyCaption(lines, caption(2, "第二段"));
  lines = applyCaption(lines, caption(1, "你好", "吗"));
  assert.equal(lines.length, 2);
  assert.deepEqual([lines[0].stable, lines[0].unstable], ["你好", "吗"]);
  assert.equal(lines[1].segmentId, 2);
});

test("不修改传入的数组", () => {
  const before: CaptionLine[] = applyCaption([], caption(1, "甲"));
  const snapshot = JSON.stringify(before);
  applyCaption(before, caption(1, "乙"));
  applyCaption(before, caption(2, "丙"));
  assert.equal(JSON.stringify(before), snapshot);
});

test("超过上限时丢掉最早的行", () => {
  let lines: CaptionLine[] = [];
  for (let i = 1; i <= 5; i++) lines = applyCaption(lines, caption(i, `第${i}段`), 3);
  assert.deepEqual(
    lines.map((l) => l.segmentId),
    [3, 4, 5],
  );
});

test("时间格式：不满一小时是 m:ss，满一小时是 h:mm:ss", () => {
  assert.equal(formatClock(0), "0:00");
  assert.equal(formatClock(5.9), "0:05");
  assert.equal(formatClock(65), "1:05");
  assert.equal(formatClock(3599), "59:59");
  assert.equal(formatClock(3600), "1:00:00");
  assert.equal(formatClock(3725), "1:02:05");
  assert.equal(formatClock(-3), "0:00");
});

function utt(
  id: number | null,
  segment: number | null,
  text: string,
  over: Partial<UtteranceMessage> = {},
): UtteranceMessage {
  return {
    type: "utterance",
    id,
    segment_id: segment,
    speaker_idx: 1,
    speaker_name: "说话人 1",
    t_start: 1,
    t_end: 2,
    text,
    source: "asr",
    ...over,
  };
}

function item(id: number, text: string, over: Partial<HistoryItem> = {}): HistoryItem {
  return {
    id,
    speaker_idx: 1,
    speaker_name: "说话人 1",
    t_start: id,
    t_end: id + 1,
    text,
    source: "asr",
    ...over,
  };
}

test("实时字幕行带着说话人编号，没有发言编号，还没定稿", () => {
  const [line] = applyCaption([], {
    ...caption(4, "你好", "吗"),
    speaker_idx: 2,
    speaker_name: "王老师",
  });
  assert.deepEqual(
    [line.speakerIdx, line.speakerName, line.utteranceId, line.final, line.source],
    [2, "王老师", null, false, "asr"],
  );
});

test("发言定稿：对应的实时字幕行原地变成定稿行，位置不变", () => {
  let lines: CaptionLine[] = [];
  lines = applyCaption(lines, caption(1, "第一句"));
  lines = applyCaption(lines, caption(2, "第二句", "尾"));
  lines = applyUtterance(lines, utt(10, 1, "第一句。"));
  assert.equal(lines.length, 2);
  assert.deepEqual(
    [lines[0].utteranceId, lines[0].stable, lines[0].unstable, lines[0].final],
    [10, "第一句。", "", true],
  );
  assert.deepEqual([lines[1].final, lines[1].unstable], [false, "尾"]); // 另一行还在说
});

test("已经定稿的行不再被迟到的实时字幕改写", () => {
  let lines = applyCaption([], caption(1, "你好"));
  lines = applyUtterance(lines, utt(10, 1, "你好。"));
  const after = applyCaption(lines, caption(1, "旧的内容", "尾巴"));
  assert.deepEqual(
    after.map((l) => l.stable),
    ["你好。"],
  );
});

test("文字全空的实时字幕表示清掉这一行的临时字幕", () => {
  let lines = applyCaption([], caption(1, "", "嗯"));
  lines = applyCaption(lines, caption(2, "保留"));
  assert.equal(lines.length, 2);
  lines = applyCaption(lines, caption(1, "", ""));
  assert.deepEqual(
    lines.map((l) => l.segmentId),
    [2],
  );
  assert.deepEqual(
    applyCaption(lines, caption(9, "", "")).map((l) => l.segmentId),
    [2],
  ); // 没有这一行：什么也不做
});

test("助理的话和键入的文字没有对应的实时字幕行：追加在最后", () => {
  let lines = applyCaption([], caption(1, "正在说"));
  lines = applyUtterance(
    lines,
    utt(11, null, "好的。", { speaker_idx: -1, speaker_name: "Nova", source: "assistant" }),
  );
  lines = applyUtterance(
    lines,
    utt(12, null, "查一下", { speaker_idx: -2, speaker_name: "文字输入", source: "text" }),
  );
  assert.deepEqual(
    lines.map((l) => [l.source, l.final]),
    [
      ["asr", false],
      ["assistant", true],
      ["text", true],
    ],
  );
});

test("落库失败的发言（id 为 null）也能定稿显示，之后不会被当成重复", () => {
  let lines = applyCaption([], caption(1, "话"));
  lines = applyUtterance(lines, utt(null, 1, "话。"));
  assert.deepEqual([lines[0].final, lines[0].utteranceId], [true, null]);
  lines = applyUtterance(lines, utt(null, null, "另一句"));
  assert.equal(lines.length, 2);
});

test("同一条发言的重复消息按发言编号去重", () => {
  let lines = applyUtterance([], utt(5, null, "话", { source: "assistant" }));
  lines = applyUtterance(lines, utt(5, null, "话", { source: "assistant" }));
  assert.equal(lines.length, 1);
});

test("说话人更正：只改那一条发言", () => {
  let lines = applyUtterance([], utt(10, null, "甲"));
  lines = applyUtterance(lines, utt(11, null, "乙"));
  lines = applyUtteranceUpdate(lines, {
    type: "utterance_update",
    id: 11,
    speaker_idx: 3,
    speaker_name: "说话人 3",
  });
  assert.deepEqual(
    lines.map((l) => [l.speakerIdx, l.speakerName]),
    [
      [1, "说话人 1"],
      [3, "说话人 3"],
    ],
  );
});

test("说话人改名：这个人的所有行都跟着变，包括已经显示的", () => {
  let lines = applyUtterance([], utt(10, null, "甲", { speaker_idx: 2, speaker_name: "说话人 2" }));
  lines = applyUtterance(lines, utt(11, null, "乙"));
  lines = applyCaption(lines, {
    ...caption(3, "还在说"),
    speaker_idx: 2,
    speaker_name: "说话人 2",
  });
  lines = applySpeakerRename(lines, { idx: 2, display_name: "王老师" });
  assert.deepEqual(
    lines.map((l) => l.speakerName),
    ["王老师", "说话人 1", "王老师"],
  );
});

test("上限对发言同样生效", () => {
  let lines: CaptionLine[] = [];
  for (let i = 1; i <= 5; i++) lines = applyUtterance(lines, utt(i, null, `第${i}句`), 3);
  assert.deepEqual(
    lines.map((l) => l.utteranceId),
    [3, 4, 5],
  );
});

test("历史记录：整体替换、向上翻页、断线补齐都按发言编号去重", () => {
  let lines = replaceWithHistory([item(5, "五"), item(6, "六"), item(7, "七")]);
  assert.deepEqual(
    lines.map((l) => [l.utteranceId, l.final, l.segmentId]),
    [
      [5, true, null],
      [6, true, null],
      [7, true, null],
    ],
  );

  lines = prependHistory(lines, [item(3, "三"), item(4, "四"), item(5, "重复的五")]);
  assert.deepEqual(
    lines.map((l) => l.stable),
    ["三", "四", "五", "六", "七"],
  );

  lines = appendHistory(lines, [item(7, "重复的七"), item(8, "八"), item(9, "九")]);
  assert.deepEqual(
    lines.map((l) => l.utteranceId),
    [3, 4, 5, 6, 7, 8, 9],
  );

  assert.equal(firstUtteranceId(lines), 3);
  assert.equal(lastUtteranceId(lines), 9);
  assert.equal(firstUtteranceId([]), null);
  assert.equal(lastUtteranceId(applyCaption([], caption(1, "只有实时字幕"))), null);
});

test("补齐不会和实时消息重复显示同一条发言", () => {
  const live = applyUtterance([], utt(8, 4, "实时收到的"));
  const merged = appendHistory(live, [item(8, "实时收到的"), item(9, "断线期间的")]);
  assert.deepEqual(
    merged.map((l) => l.utteranceId),
    [8, 9],
  );
});

test("历史记录的上限：只留最近的", () => {
  const items = Array.from({ length: 6 }, (_, i) => item(i + 1, `第${i + 1}句`));
  assert.deepEqual(
    replaceWithHistory(items, 4).map((l) => l.utteranceId),
    [3, 4, 5, 6],
  );
});

// ---- 相邻片段并成一条 ----

test("并进上一条发言：原来那行换成并好的文字，新片段的实时字幕行收掉", async () => {
  const { applyCaption, applyUtterance } = await import("./captions.ts");
  const said = (id: number, segment_id: number, text: string, t_end: number) =>
    ({
      type: "utterance",
      id,
      segment_id,
      speaker_idx: 1,
      speaker_name: "王老师",
      t_start: 10,
      t_end,
      text,
      source: "asr",
    }) as const;
  let lines = applyUtterance([], said(7, 1, "我们先看一下", 11.5));
  // 换了口气接着说：先是一行灰色的实时字幕
  lines = applyCaption(lines, {
    type: "caption",
    segment_id: 2,
    speaker_idx: 1,
    speaker_name: "王老师",
    t_start: 12,
    stable: "",
    unstable: "消融",
  });
  assert.equal(lines.length, 2);
  // 定稿时服务端把它并进了第 7 条
  lines = applyUtterance(lines, said(7, 2, "我们先看一下消融实验", 13.4));
  assert.equal(lines.length, 1);
  assert.equal(lines[0].stable, "我们先看一下消融实验");
  assert.equal(lines[0].utteranceId, 7);
  assert.equal(lines[0].final, true);
  assert.equal(lines[0].tStart, 10);
  // 没来得及显示实时字幕就定稿了（或者这条消息到了两次）：只更新那一行
  lines = applyUtterance(lines, said(7, 3, "我们先看一下消融实验的结果", 14));
  assert.deepEqual(
    lines.map((l) => l.stable),
    ["我们先看一下消融实验的结果"],
  );
  // 不相干的新发言照常追加
  lines = applyUtterance(lines, said(8, 4, "好的", 20));
  assert.equal(lines.length, 2);
});

test("补齐时已有的发言以服务端为准，并且把最后一条重新取一次", async () => {
  const { appendHistory, backfillAfterId, replaceWithHistory } = await import("./captions.ts");
  const item = (id: number, text: string, speaker_idx = 1, speaker_name = "王老师") => ({
    id,
    speaker_idx,
    speaker_name,
    t_start: id,
    t_end: id + 1,
    text,
    source: "asr" as const,
  });
  const lines = replaceWithHistory([item(5, "更早的"), item(6, "我们先看")]);
  assert.equal(backfillAfterId(lines), 5); // 从第 5 条之后取：第 6 条会被重新取到
  assert.equal(backfillAfterId([]), null);
  const next = appendHistory(lines, [item(6, "我们先看一下基线", 2, "小李"), item(7, "新的一条")]);
  assert.deepEqual(
    next.map((l) => [l.utteranceId, l.stable, l.speakerName]),
    [
      [5, "更早的", "王老师"],
      [6, "我们先看一下基线", "小李"],
      [7, "新的一条", "王老师"],
    ],
  );
});
