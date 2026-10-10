// 会议列表和会话信息的显示逻辑（纯函数，可以用 npm test 直接测）。

import type { SessionState, SessionSummary, SpeakerInfo } from "./api.ts";

export const STATE_LABELS: Record<SessionState, string> = {
  live: "进行中",
  interrupted: "已中断",
  ended: "已结束",
};

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** Unix 秒 → 本地时间的「10月8日 14:03」。 */
export function formatStartedAt(unixSecs: number): string {
  const d = new Date(unixSecs * 1000);
  return `${d.getMonth() + 1}月${d.getDate()}日 ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 会议标题；没起名字的用开始时间代替。 */
export function sessionTitle(session: { title: string; started_at: number }): string {
  const title = session.title.trim();
  return title || `未命名会议 · ${formatStartedAt(session.started_at)}`;
}

/** 时长：不到一分钟、N 分钟、N 小时 NN 分。 */
export function formatDuration(secs: number): string {
  const minutes = Math.floor(Math.max(0, secs) / 60);
  if (minutes < 1) return "不到 1 分钟";
  if (minutes < 60) return `${minutes} 分钟`;
  return `${Math.floor(minutes / 60)} 小时 ${pad(minutes % 60)} 分`;
}

/** 列表里每场会议下面那行预览：最后一句话，太长截断。 */
export function previewLine(summary: Pick<SessionSummary, "preview">, maxChars = 36): string {
  const last = summary.preview[summary.preview.length - 1];
  if (!last) return "";
  const text = last.text.length > maxChars ? `${last.text.slice(0, maxChars)}…` : last.text;
  return `${last.speaker}：${text}`;
}

export type ExportFormat = "md" | "json" | "zip";

/** 导出的下载地址（docs/interfaces.md §5.6）：Markdown 转录、结构化 JSON、带截图和任务产物的完整包。 */
export function exportUrl(sessionId: string, format: ExportFormat = "md"): string {
  return `/api/export/${encodeURIComponent(sessionId)}.${format}`;
}

/** 进行中的会议不能删除（服务端也会拒绝）。 */
export function canDelete(summary: Pick<SessionSummary, "state">): boolean {
  return summary.state !== "live";
}

/** 可以改名的说话人：说话人区分输出的（从 1 起）和键入文字的（-2）；助理的名字来自配置，未知（0）不是一个人。 */
export function renamableSpeakers(speakers: readonly SpeakerInfo[]): SpeakerInfo[] {
  return speakers.filter((s) => s.idx >= 1 || s.idx === -2);
}

/** 可以合并到谁：别的、由说话人区分给出的说话人（从 1 起）。键入的文字、助理、未知都不参与合并。 */
export function mergeTargets(speakers: readonly SpeakerInfo[], idx: number): SpeakerInfo[] {
  if (idx < 1) return [];
  return speakers.filter((s) => s.idx >= 1 && s.idx !== idx);
}

/** 这场会议现在能不能点「继续」：已中断、已结束的都可以；正在别的设备上进行的也可以（把连接接过来）。 */
export function canResume(connection: string): boolean {
  return connection === "disconnected" || connection === "error";
}

/** 说话人改名输入框的候选：配置里的成员名单，去掉已经被别人用着的名字。 */
export function nameSuggestions(
  members: readonly string[],
  speakers: readonly SpeakerInfo[],
  editing: number | null,
): string[] {
  const used = new Set(speakers.filter((s) => s.idx !== editing).map((s) => s.display_name));
  return members.filter((m) => !used.has(m));
}

/** 把一条 session 消息变成字幕区顶部要显示的最小摘要（详细数字随后由 HTTP 补全）。 */
export function summaryFromSessionMessage(message: {
  id: string;
  title: string;
  keep: boolean;
  started_at: number;
}): SessionSummary {
  return {
    id: message.id,
    title: message.title,
    keep: message.keep,
    deletion_pending: false,
    started_at: message.started_at,
    ended_at: null,
    last_active_at: message.started_at,
    state: "live",
    duration_secs: 0,
    utterance_count: 0,
    speakers: [],
    preview: [],
  };
}

/** ready 是本机管线就绪；不能用异端会议的 live 状态或可关闭提示替代。 */
export function recordingLabel(
  connection: string,
  closedReason: string | null,
  retry: number,
): string {
  if (closedReason !== null) return "转录已停止";
  if (connection === "connected") return "正在转录 · 发言和共享画面保存在服务器";
  if (retry > 0) return "转录已断开 · 正在重连";
  if (connection === "connecting") return "正在连接 · 尚未转录";
  return "未在本机转录";
}
