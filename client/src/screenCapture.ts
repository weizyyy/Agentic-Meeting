// 屏幕截图的纯逻辑（docs/interfaces.md §5.1、§5.3）：对时、画面变化的判断、
// 要不要上传、缩放尺寸、时间线。不碰 DOM，可以用 npm test 直接测；真正的采集在 useScreenShare.ts。

/** 服务端下发的截图参数（/api/session 响应里的 screen 字段，取自配置的 screen 段）。 */
export interface ScreenConfig {
  enabled: boolean;
  /** 画面变化触发上传的最小间隔（秒），也是检查画面的周期 */
  minIntervalSecs: number;
  /** 画面不变时的兜底间隔（秒） */
  heartbeatSecs: number;
  /** 上传图片的长边上限（像素） */
  maxSidePx: number;
  /** 缩略图上变化最大的那一小块的平均差 / 255 超过它算「画面变了」 */
  changeThreshold: number;
}

export const DEFAULT_SCREEN_CONFIG: ScreenConfig = {
  enabled: true,
  minIntervalSecs: 2,
  heartbeatSecs: 60,
  maxSidePx: 1920,
  changeThreshold: 0.04,
};

/** 判断画面变化用的缩略图尺寸（服务端用同样的尺寸再判一次）。 */
export const THUMB_WIDTH = 64;
export const THUMB_HEIGHT = 36;

function positive(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : fallback;
}

/** 把服务端下发的 screen 段变成 ScreenConfig；缺的、类型不对的字段用默认值。 */
export function parseScreenConfig(raw: unknown): ScreenConfig {
  const d = DEFAULT_SCREEN_CONFIG;
  if (typeof raw !== "object" || raw === null) return d;
  const r = raw as Record<string, unknown>;
  const threshold = r.change_threshold;
  return {
    enabled: typeof r.enabled === "boolean" ? r.enabled : d.enabled,
    minIntervalSecs: positive(r.min_interval_secs, d.minIntervalSecs),
    heartbeatSecs: positive(r.heartbeat_secs, d.heartbeatSecs),
    maxSidePx: Math.round(positive(r.max_side_px, d.maxSidePx)),
    changeThreshold:
      typeof threshold === "number" && threshold >= 0 && threshold <= 1
        ? threshold
        : d.changeThreshold,
  };
}

// ---- 对时（interfaces.md §5.1） ----

export interface ClockSample {
  /** 发请求前的本地时间（秒） */
  t0: number;
  /** 收到响应时的本地时间（秒） */
  t1: number;
  /** 响应里的服务端时间（Unix 秒） */
  serverTime: number;
}

/** 「服务端时间 − 本地时间」的偏移量：取往返最短的那一次，按请求中点估算。没有样本返回 0。 */
export function clockOffset(samples: readonly ClockSample[]): number {
  let best: ClockSample | null = null;
  for (const sample of samples) {
    const trip = sample.t1 - sample.t0;
    if (trip < 0 || !Number.isFinite(sample.serverTime)) continue;
    if (best === null || trip < best.t1 - best.t0) best = sample;
  }
  return best === null ? 0 : best.serverTime - (best.t0 + best.t1) / 2;
}

// ---- 画面变化 ----

/** RGBA 像素 → 灰度（BT.601 亮度）。 */
export function toGray(rgba: ArrayLike<number>): Uint8Array {
  const gray = new Uint8Array(Math.floor(rgba.length / 4));
  for (let i = 0; i < gray.length; i += 1) {
    const p = i * 4;
    gray[i] = Math.round(0.299 * rgba[p] + 0.587 * rgba[p + 1] + 0.114 * rgba[p + 2]);
  }
  return gray;
}

/** 比较时把缩略图分成的小块（像素）：8 列 × 6 行。和服务端 screen/ingest.py 的 DIFF_BLOCK 一致。 */
export const DIFF_BLOCK_WIDTH = 8;
export const DIFF_BLOCK_HEIGHT = 6;

/**
 * 两张灰度缩略图差多少：分成小块，取变化最大的那一块的平均差 / 255。长度对不上或为空按「完全不同」算。
 *
 * 不用整张图的平均差：两页都是白底、只是文字不同的幻灯片，整张平均下来只差 1% 左右，会被当成没变。
 * 尺寸不是标准缩略图时退回整张的平均差。
 */
export function frameDifference(a: ArrayLike<number>, b: ArrayLike<number>): number {
  if (a.length !== b.length || a.length === 0) return 1;
  if (a.length !== THUMB_WIDTH * THUMB_HEIGHT) {
    let sum = 0;
    for (let i = 0; i < a.length; i += 1) sum += Math.abs(a[i] - b[i]);
    return sum / a.length / 255;
  }
  const columns = THUMB_WIDTH / DIFF_BLOCK_WIDTH;
  const sums = new Float64Array(columns * (THUMB_HEIGHT / DIFF_BLOCK_HEIGHT));
  for (let y = 0; y < THUMB_HEIGHT; y += 1) {
    const row = Math.floor(y / DIFF_BLOCK_HEIGHT) * columns;
    for (let x = 0; x < THUMB_WIDTH; x += 1) {
      const i = y * THUMB_WIDTH + x;
      sums[row + Math.floor(x / DIFF_BLOCK_WIDTH)] += Math.abs(a[i] - b[i]);
    }
  }
  let max = 0;
  for (const sum of sums) max = Math.max(max, sum);
  return max / (DIFF_BLOCK_WIDTH * DIFF_BLOCK_HEIGHT) / 255;
}

export type UploadReason = "first" | "change" | "heartbeat";

/**
 * 这一帧要不要上传。
 * - 还没上传过：传（first）。
 * - 距上次上传已满兜底间隔：传（heartbeat），不管画面变没变。
 * - 画面变化超过阈值，且距上次上传已满最小间隔：传（change）。
 * `difference` 是与上一张**已上传**的缩略图的差。
 */
export function uploadDecision(
  nowSecs: number,
  lastUploadSecs: number | null,
  difference: number,
  config: Pick<ScreenConfig, "minIntervalSecs" | "heartbeatSecs" | "changeThreshold">,
): UploadReason | null {
  if (lastUploadSecs === null) return "first";
  const since = nowSecs - lastUploadSecs;
  if (difference > config.changeThreshold && since >= config.minIntervalSecs) return "change";
  if (since >= config.heartbeatSecs) return "heartbeat";
  return null;
}

/** 等比缩到长边不超过 maxSide（只缩不放），结果至少 1×1。 */
export function scaledSize(
  width: number,
  height: number,
  maxSide: number,
): { width: number; height: number } {
  const longest = Math.max(width, height);
  if (longest <= 0) return { width: 1, height: 1 };
  const scale = Math.min(1, maxSide / longest);
  return {
    width: Math.max(1, Math.min(maxSide, Math.round(width * scale))),
    height: Math.max(1, Math.min(maxSide, Math.round(height * scale))),
  };
}

// ---- 时间线 ----

export interface FrameItem {
  id: number;
  /** 会话时间轴上的秒 */
  t: number;
  width: number;
  height: number;
  caption: string | null;
}

/** 时间线里最多留多少张（再多就丢最早的；完整的在服务端）。 */
export const MAX_TIMELINE_FRAMES = 300;

function sortFrames(frames: FrameItem[]): FrameItem[] {
  return frames.sort((a, b) => a.t - b.t || a.id - b.id).slice(-MAX_TIMELINE_FRAMES);
}

/** 用服务端的列表整体替换时间线。 */
export function replaceFrames(items: readonly FrameItem[]): FrameItem[] {
  return sortFrames([...items]);
}

/** 新截图进时间线（按时间排，同一张重复收到只留一份，已有的摘要不丢）。 */
export function applyFrame(
  frames: readonly FrameItem[],
  frame: Omit<FrameItem, "caption"> & { caption?: string | null },
): FrameItem[] {
  const existing = frames.find((f) => f.id === frame.id);
  const merged: FrameItem = { ...frame, caption: frame.caption ?? existing?.caption ?? null };
  return sortFrames([...frames.filter((f) => f.id !== frame.id), merged]);
}

/** 某张截图的摘要生成好了。时间线里没有这张就不变。 */
export function applyFrameCaption(
  frames: readonly FrameItem[],
  id: number,
  caption: string,
): FrameItem[] {
  if (!frames.some((f) => f.id === id)) return frames as FrameItem[];
  return frames.map((f) => (f.id === id ? { ...f, caption } : f));
}

/** 断线重连后把服务端的列表并进来：以服务端为准，本地多出来的（还没同步到的）保留。 */
export function mergeFrames(frames: readonly FrameItem[], items: readonly FrameItem[]): FrameItem[] {
  const ids = new Set(items.map((f) => f.id));
  return sortFrames([...frames.filter((f) => !ids.has(f.id)), ...items]);
}

/** 截图图片的地址。 */
export function frameImageUrl(id: number): string {
  return `/api/frames/${id}/image`;
}

/** 会话时间轴上的秒 → 「时:分:秒」。 */
/**
 * 大图里按左右方向键：从编号是 `id` 的那张往前（-1）或往后（+1）一张，返回它在 `frames` 里的位置；
 * 已经到头、或者那张图已经不在列表里，返回 -1。
 */
export function adjacentFrameIndex(
  frames: readonly { id: number }[],
  id: number,
  step: -1 | 1,
): number {
  const at = frames.findIndex((f) => f.id === id);
  const next = at + step;
  return at < 0 || next < 0 || next >= frames.length ? -1 : next;
}

export function formatFrameTime(t: number): string {
  const total = Math.max(0, Math.floor(t));
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(Math.floor(total / 3600))}:${pad(Math.floor((total % 3600) / 60))}:${pad(total % 60)}`;
}
