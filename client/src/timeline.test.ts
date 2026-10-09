import assert from "node:assert/strict";
import { test } from "node:test";

import { formatWallClock, wallClockAt, wallClockLabel } from "./timeline.ts";

const span = (connected: number, disconnected: number | null, from: number, to: number | null) => ({
  connected_at: connected,
  disconnected_at: disconnected,
  t_from: from,
  t_to: to,
});

test("没中断过的会议：墙上时刻就是开始时刻加上会议时间", () => {
  assert.equal(wallClockAt(65, { startedAt: 1000, connections: [] }), 1065);
  assert.equal(wallClockAt(65, { startedAt: 1000, connections: [span(1000, null, 0, null)] }), 1065);
});

test("中断过的会议：按所在的那次连接推算", () => {
  // 第一次连了 60 秒；会议时间轴没跟上墙上时间的那种继续（时间轴从 60 接着走，墙上已经过了 780 秒）
  const base = {
    startedAt: 1000,
    connections: [span(1780, null, 60, null), span(1000, 1060, 0, 60)],
  };
  assert.equal(wallClockAt(30, base), 1030);
  assert.equal(wallClockAt(60, base), 1780);
  assert.equal(wallClockAt(90, base), 1810);
});

test("连接记录还没更新（落在最后一次已关闭的连接之后）：退回开始时刻加会议时间", () => {
  const base = { startedAt: 1000, connections: [span(1000, 1060, 0, 60)] };
  assert.equal(wallClockAt(60.5, base), 1060.5); // 一秒以内的出入还按这次连接算
  assert.equal(wallClockAt(800, base), 1800);
});

test("钟点按浏览器所在时区显示，补零", () => {
  const at = new Date(2026, 9, 8, 9, 3, 5).getTime() / 1000;
  assert.equal(formatWallClock(at), "09:03:05");
  assert.equal(wallClockLabel(60, { startedAt: at, connections: [] }), "09:04:05");
  assert.equal(wallClockLabel(60, null), "");
});
