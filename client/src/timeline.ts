// 时间的两条轨：会议进行到第几分几秒（会话时间轴），和当时墙上的钟点（浏览器所在时区）。
// 都是纯函数，可以用 npm test 直接测。

import type { ConnectionSpan } from "./api.ts";

/** 一场会议的时间基准：开始的时刻，和各次连接在两条轨上的对应关系。 */
export interface TimeBase {
  /** 会议开始的时刻（Unix 秒） */
  startedAt: number;
  connections: readonly ConnectionSpan[];
}

/** 一次连接已经关了、而时间点落在它结束之后这么多秒以外，就不再按它推算。 */
const SPAN_SLACK_SECS = 1;

/**
 * 会话时间轴上的 `t` 对应的墙上时刻（Unix 秒）。
 * 落在某次连接里的，按那次连接的起点推算（中断过的会议，两条轨差出中断的那一段）；
 * 对不上任何一次连接（连接记录还没取到）时，按「开始时刻 + t」算。
 */
export function wallClockAt(t: number, base: TimeBase): number {
  let span: ConnectionSpan | null = null;
  for (const c of base.connections) {
    if (c.t_from <= t && (span === null || c.t_from > span.t_from)) span = c;
  }
  if (span !== null && (span.t_to === null || t <= span.t_to + SPAN_SLACK_SECS)) {
    return span.connected_at + (t - span.t_from);
  }
  return base.startedAt + t;
}

/** 墙上的钟点「14:03:05」，按浏览器所在时区。 */
export function formatWallClock(unixSecs: number): string {
  const d = new Date(unixSecs * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** `t` 那一刻墙上的钟点；不知道是哪场会议（没有时间基准）时返回空串。 */
export function wallClockLabel(t: number, base: TimeBase | null): string {
  return base === null ? "" : formatWallClock(wallClockAt(t, base));
}
