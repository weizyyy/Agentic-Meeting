// 继续会议相关的纯逻辑：自动重连的退避、要不要重连、中断的空档。
// 都是纯函数，可以用 npm test 直接测。

import type { ConnectionSpan } from "./api.ts";

/** 连接意外断开后，第 1…5 次重连各等多久（毫秒）。 */
export const RECONNECT_DELAYS_MS: readonly number[] = [1000, 2000, 4000, 8000, 8000];

/** 第 `attempt` 次重连（从 1 数）之前要等的毫秒数；次数用完返回 null。 */
export function reconnectDelayMs(attempt: number): number | null {
  if (!Number.isInteger(attempt) || attempt < 1) return null;
  return RECONNECT_DELAYS_MS[attempt - 1] ?? null;
}

export type ClosedReason = "taken_over" | "ended" | "server_stopping";

export interface DropInfo {
  /** 用户还想留在会议里：连上过，并且没有自己点「结束会议」或关页面 */
  inMeeting: boolean;
  /** 服务端在断开之前说明的原因；没说就是意外断开 */
  closedReason: ClosedReason | null;
  /** 断开之前所在的会议；不知道是哪一场就没法「继续同一场」 */
  sessionId: string | null;
}

/**
 * 连接断开之后要不要自动重连。
 * 被另一个页面接管、会议已结束：不重连（重连只会把连接抢回来，或把结束的会议又打开）。
 * 服务端正在停止：重连——它多半是在重启，起来之后接着开同一场。
 */
export function shouldReconnect(info: DropInfo): boolean {
  if (!info.inMeeting || info.sessionId === null) return false;
  return info.closedReason !== "taken_over" && info.closedReason !== "ended";
}

/** 一段中断：`t` 是它在会话时间轴上结束的位置（下一次连接的起点），`secs` 是断了多久。 */
export interface Gap {
  t: number;
  secs: number;
}

/** 短于这么久的空档不显示（自动重连成功时的一两秒不值得画一条分隔线）。 */
export const MIN_GAP_SECS = 10;

/** 相邻两次连接之间的空档（按墙上时间算断了多久）。 */
export function connectionGaps(
  connections: readonly ConnectionSpan[],
  minSecs: number = MIN_GAP_SECS,
): Gap[] {
  const sorted = [...connections].sort((a, b) => a.connected_at - b.connected_at);
  const gaps: Gap[] = [];
  for (let i = 1; i < sorted.length; i += 1) {
    const before = sorted[i - 1].disconnected_at;
    if (before === null) continue; // 上一次连接还没关（不该出现），不算空档
    const secs = sorted[i].connected_at - before;
    if (secs >= minSecs) gaps.push({ t: sorted[i].t_from, secs });
  }
  return gaps;
}

/**
 * 每条分隔线画在哪一行字幕的上面：行号 → 中断的秒数；行号等于行数表示画在最后一行的下面
 * （继续之后还没人说话）。字幕只加载了最近一部分时，落在最早那行之前的空档不画——它可能属于更早的、还没加载的地方。
 */
export function gapPositions(
  lineStarts: readonly number[],
  gaps: readonly Gap[],
  hasOlder: boolean,
): Map<number, number> {
  const positions = new Map<number, number>();
  for (const gap of gaps) {
    let index = lineStarts.findIndex((t) => t >= gap.t - 0.5);
    if (index < 0) index = lineStarts.length;
    if (index === 0 && (hasOlder || lineStarts.length === 0)) continue;
    positions.set(index, (positions.get(index) ?? 0) + gap.secs);
  }
  return positions;
}

/** 「12 分钟」「40 秒」「2 小时 05 分」。 */
export function formatSpan(secs: number): string {
  const total = Math.max(0, Math.round(secs));
  if (total < 60) return `${total} 秒`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes} 分钟`;
  return `${Math.floor(minutes / 60)} 小时 ${String(minutes % 60).padStart(2, "0")} 分`;
}

/** 一场已中断的会议断了多久了（秒）：从它最后一次活动算到现在。 */
export function interruptedSecs(session: { last_active_at: number }, nowUnixSecs: number): number {
  return Math.max(0, nowUnixSecs - session.last_active_at);
}
