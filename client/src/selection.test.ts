import assert from "node:assert/strict";
import { test } from "node:test";

import type { CaptionLine } from "./captions.ts";
import {
  assignTargets,
  pruneSelection,
  selectRange,
  selectSpan,
  selectableId,
  toggleSelection,
} from "./selection.ts";

const line = (id: number | null, extra: Partial<CaptionLine> = {}): CaptionLine => ({
  segmentId: null,
  utteranceId: id,
  speakerIdx: 1,
  speakerName: "王老师",
  tStart: id ?? 0,
  stable: "话",
  unstable: "",
  final: true,
  source: "asr",
  ...extra,
});

const LINES = [
  line(1),
  line(2),
  line(3, { source: "assistant", speakerIdx: -1 }),
  line(4, { source: "text", speakerIdx: -2 }),
  line(5),
  line(null, { final: false, segmentId: 9 }), // 还在说的实时字幕
  line(6),
];

test("只有落库的、语音识别来的发言能改发言人", () => {
  assert.deepEqual(LINES.map(selectableId), [1, 2, null, null, 5, null, 6]);
  assert.equal(selectableId(line(null)), null); // 落库失败的那种定稿行也不行
});

test("点一下切换选中", () => {
  const one = toggleSelection(new Set(), 2);
  assert.deepEqual([...one], [2]);
  assert.deepEqual([...toggleSelection(one, 5)].sort(), [2, 5]);
  assert.deepEqual([...toggleSelection(one, 2)], []);
  assert.equal(one.size, 1); // 不改原来的集合
});

test("按住 Shift 选一段：中间不能选的跳过，已经选中的留着", () => {
  assert.deepEqual([...selectRange(LINES, new Set(), 1, 6)].sort(), [1, 2, 5, 6]);
  assert.deepEqual([...selectRange(LINES, new Set(), 6, 2)].sort(), [2, 5, 6]); // 反着选也行
  assert.deepEqual([...selectRange(LINES, new Set([1]), 5, 6)].sort(), [1, 5, 6]);
  // 上一次点的那行已经不在界面上了：只选这一行
  assert.deepEqual([...selectRange(LINES, new Set(), 99, 5)], [5]);
});

test("按住拖动选一段：按行号，两头可以落在不能选的行上", () => {
  assert.deepEqual([...selectSpan(LINES, new Set(), 1, 4)].sort(), [2, 5]);
  assert.deepEqual([...selectSpan(LINES, new Set(), 4, 1)].sort(), [2, 5]);
  assert.deepEqual([...selectSpan(LINES, new Set([1]), 2, 3)], [1]); // 只拖过助理的话和键入的文字
  assert.deepEqual([...selectSpan(LINES, new Set(), 5, 99)], [6]); // 行号越界就到头为止
  assert.deepEqual([...selectSpan(LINES, new Set(), 0, 0)], [1]);
});

test("字幕变了之后，选中的只留还在界面上的", () => {
  const selected = new Set([1, 5, 42]);
  assert.deepEqual([...pruneSelection(LINES, selected)].sort(), [1, 5]);
  const same = new Set([1, 5]);
  assert.equal(pruneSelection(LINES, same), same); // 没变化时还是同一个集合，不触发多余的重画
  assert.equal(pruneSelection([], new Set()).size, 0);
  assert.equal(pruneSelection([], selected).size, 0);
});

test("可以改成谁", () => {
  const speakers = [
    { idx: -2, display_name: "文字输入" },
    { idx: -1, display_name: "Nova" },
    { idx: 0, display_name: "未知" },
    { idx: 1, display_name: "王老师" },
    { idx: 1000, display_name: "旁听的张老师" },
  ];
  assert.deepEqual(
    assignTargets(speakers).map((s) => s.idx),
    [1, 1000],
  );
});
