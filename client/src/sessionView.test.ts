import assert from "node:assert/strict";
import { test } from "node:test";

import {
  STATE_LABELS,
  canDelete,
  exportUrl,
  formatDuration,
  formatStartedAt,
  nameSuggestions,
  previewLine,
  renamableSpeakers,
  sessionTitle,
  summaryFromSessionMessage,
} from "./sessionView.ts";

test("状态用中文标签", () => {
  assert.deepEqual(STATE_LABELS, { live: "进行中", interrupted: "已中断", ended: "已结束" });
});

test("没起名字的会议用开始时间代替标题（本地时间）", () => {
  const started = new Date(2026, 9, 8, 14, 3, 20).getTime() / 1000;
  assert.equal(formatStartedAt(started), "10月8日 14:03");
  assert.equal(sessionTitle({ title: "", started_at: started }), "未命名会议 · 10月8日 14:03");
  assert.equal(sessionTitle({ title: "   ", started_at: started }), "未命名会议 · 10月8日 14:03");
  assert.equal(sessionTitle({ title: " 周一组会 ", started_at: started }), "周一组会");
  const early = new Date(2026, 0, 5, 7, 5).getTime() / 1000;
  assert.equal(formatStartedAt(early), "1月5日 07:05");
});

test("时长的说法", () => {
  assert.equal(formatDuration(0), "不到 1 分钟");
  assert.equal(formatDuration(59.9), "不到 1 分钟");
  assert.equal(formatDuration(60), "1 分钟");
  assert.equal(formatDuration(12 * 60 + 30), "12 分钟");
  assert.equal(formatDuration(3600), "1 小时 00 分");
  assert.equal(formatDuration(3600 + 5 * 60 + 59), "1 小时 05 分");
  assert.equal(formatDuration(-5), "不到 1 分钟");
});

test("预览取最后一句，太长截断", () => {
  assert.equal(previewLine({ preview: [] }), "");
  assert.equal(
    previewLine({
      preview: [
        { speaker: "王老师", text: "第一句" },
        { speaker: "李同学", text: "第二句" },
      ],
    }),
    "李同学：第二句",
  );
  assert.equal(
    previewLine({ preview: [{ speaker: "甲", text: "一二三四五六七八九十" }] }, 5),
    "甲：一二三四五…",
  );
  assert.equal(
    previewLine({ preview: [{ speaker: "甲", text: "一二三四五" }] }, 5),
    "甲：一二三四五",
  );
});

test("进行中的会议不能删", () => {
  assert.equal(canDelete({ state: "live" }), false);
  assert.equal(canDelete({ state: "interrupted" }), true);
  assert.equal(canDelete({ state: "ended" }), true);
});

test("可以改名的说话人：从 1 起的和键入文字的，不含助理和未知", () => {
  const all = [
    { idx: -2, display_name: "文字输入" },
    { idx: -1, display_name: "Nova" },
    { idx: 0, display_name: "未知" },
    { idx: 1, display_name: "说话人 1" },
    { idx: 2, display_name: "说话人 2" },
  ];
  assert.deepEqual(
    renamableSpeakers(all).map((s) => s.idx),
    [-2, 1, 2],
  );
});

test("改名候选：成员名单里还没被别人用的名字", () => {
  const speakers = [
    { idx: 1, display_name: "王老师" },
    { idx: 2, display_name: "说话人 2" },
  ];
  assert.deepEqual(nameSuggestions(["王老师", "李同学", "张同学"], speakers, 2), [
    "李同学",
    "张同学",
  ]);
  // 正在改名的这个人自己现在叫什么，不算「被别人用了」
  assert.deepEqual(nameSuggestions(["王老师", "李同学"], speakers, 1), ["王老师", "李同学"]);
  assert.deepEqual(nameSuggestions([], speakers, 1), []);
});

test("session 消息对应的最小摘要", () => {
  const summary = summaryFromSessionMessage({ id: "s1", title: "", started_at: 100 });
  assert.deepEqual(
    [summary.id, summary.state, summary.ended_at, summary.utterance_count],
    ["s1", "live", null, 0],
  );
});

test("导出地址：会议编号要编码", () => {
  assert.equal(exportUrl("abc123"), "/api/export/abc123.md");
  assert.equal(exportUrl("a b/c"), "/api/export/a%20b%2Fc.md");
});

test("合并说话人的候选：别的、说话人区分给出的人", async () => {
  const { mergeTargets, canResume } = await import("./sessionView.ts");
  const all = [
    { idx: -2, display_name: "文字输入" },
    { idx: -1, display_name: "Nova" },
    { idx: 0, display_name: "未知" },
    { idx: 1, display_name: "王老师" },
    { idx: 2, display_name: "小李" },
    { idx: 3, display_name: "说话人 3" },
  ];
  assert.deepEqual(
    mergeTargets(all, 3).map((s) => s.idx),
    [1, 2],
  );
  assert.deepEqual(mergeTargets(all, -2), []); // 键入的文字不是一个说话的人
  assert.deepEqual(mergeTargets(all, 0), []);
  assert.equal(canResume("disconnected"), true);
  assert.equal(canResume("error"), true);
  assert.equal(canResume("connected"), false);
  assert.equal(canResume("connecting"), false);
});

test("导出地址的三种格式", () => {
  assert.equal(exportUrl("abc", "json"), "/api/export/abc.json");
  assert.equal(exportUrl("a b", "zip"), "/api/export/a%20b.zip");
});
