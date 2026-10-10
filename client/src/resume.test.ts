import assert from "node:assert/strict";
import { test } from "node:test";

import {
  connectionGaps,
  formatSpan,
  gapPositions,
  interruptedSecs,
  reconnectDelayMs,
  shouldReconnect,
} from "./resume.ts";

test("重连的退避是 1、2、4、8、8 秒，最多 5 次", () => {
  assert.deepEqual(
    [1, 2, 3, 4, 5].map((n) => reconnectDelayMs(n)),
    [1000, 2000, 4000, 8000, 8000],
  );
  assert.equal(reconnectDelayMs(6), null);
  assert.equal(reconnectDelayMs(0), null);
  assert.equal(reconnectDelayMs(1.5), null);
});

test("意外断开才重连；被接管、会议已结束、用户自己离开都不重连", () => {
  const base = { inMeeting: true, closedReason: null, sessionId: "s1" } as const;
  assert.equal(shouldReconnect(base), true);
  assert.equal(shouldReconnect({ ...base, closedReason: "server_stopping" }), true); // 服务重启后接着开
  assert.equal(shouldReconnect({ ...base, closedReason: "taken_over" }), false);
  assert.equal(shouldReconnect({ ...base, closedReason: "ended" }), false);
  assert.equal(shouldReconnect({ ...base, inMeeting: false }), false);
  assert.equal(shouldReconnect({ ...base, sessionId: null }), false);
});

const span = (connected_at: number, disconnected_at: number | null, t_from: number) => ({
  connected_at,
  disconnected_at,
  t_from,
  t_to: disconnected_at === null ? null : t_from + (disconnected_at - connected_at),
});

test("相邻两次连接之间的空档", () => {
  const connections = [
    span(1000, 1060, 0),
    span(1780, 1900, 780), // 断了 12 分钟
    span(1903, 2000, 903), // 自动重连，3 秒：不画
    span(5600, null, 4600), // 又断了一个小时，现在还连着
  ];
  assert.deepEqual(connectionGaps(connections), [
    { t: 780, secs: 720 },
    { t: 4600, secs: 3600 },
  ]);
  assert.deepEqual(connectionGaps(connections, 1).length, 3);
  assert.deepEqual(connectionGaps([span(1000, 1060, 0)]), []);
  assert.deepEqual(connectionGaps([]), []);
  // 顺序乱了也按时间排；上一次连接没有关闭时间的不算
  assert.deepEqual(connectionGaps([span(1780, null, 780), span(1000, 1060, 0)]), [
    { t: 780, secs: 720 },
  ]);
  assert.deepEqual(connectionGaps([span(1000, null, 0), span(1780, null, 780)]), []);
});

test("分隔线画在空档之后的第一行上面", () => {
  const gaps = [
    { t: 780, secs: 720 },
    { t: 4600, secs: 3600 },
  ];
  // 行的开始时间：两行在第一次连接里，一行在第二次，一行在第三次
  assert.deepEqual(
    [...gapPositions([10, 50, 790, 4610], gaps, false)],
    [
      [2, 720],
      [3, 3600],
    ],
  );
  // 发言的开始时间可能比连接的起点早一点点（语音检测往前推）：仍算在空档之后
  assert.deepEqual([...gapPositions([10, 779.8], gaps.slice(0, 1), false)], [[1, 720]]);
  // 继续之后还没人说话：画在最后一行下面
  assert.deepEqual([...gapPositions([10, 50], gaps.slice(0, 1), false)], [[2, 720]]);
  // 两段空档之间没有发言：合成一条
  assert.deepEqual([...gapPositions([10, 4610], gaps, false)], [[1, 4320]]);
});

test("只加载了最近一部分字幕时，最早那行之前的空档不画", () => {
  const gaps = [{ t: 780, secs: 720 }];
  assert.deepEqual([...gapPositions([790, 800], gaps, true)], []);
  assert.deepEqual([...gapPositions([790, 800], gaps, false)], [[0, 720]]);
  assert.deepEqual([...gapPositions([], gaps, false)], []); // 一行字幕都没有
});

test("时长的写法", () => {
  assert.equal(formatSpan(40.4), "40 秒");
  assert.equal(formatSpan(720), "12 分钟");
  assert.equal(formatSpan(7500), "2 小时 05 分");
  assert.equal(formatSpan(-3), "0 秒");
});

test("已中断了多久", () => {
  assert.equal(interruptedSecs({ last_active_at: 1000 }, 1720), 720);
  assert.equal(interruptedSecs({ last_active_at: 1000 }, 900), 0); // 时钟有出入也不出负数
});
