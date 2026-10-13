// 会议列表和会话信息的显示逻辑（纯函数，可以用 npm test 直接测）。

import type { SessionSummary, SpeakerInfo } from "./api.ts";
import { formatDateTime } from "./i18n/format.ts";
import type { Language, Messages } from "./i18n/index.ts";

/** 会议标题；没起名字的用开始时间代替。 */
export function sessionTitle(
  t: Messages,
  language: Language,
  session: { title: string; started_at: number },
): string {
  const title = session.title.trim();
  return title || t.session.untitled(formatDateTime(language, session.started_at));
}

/** 列表里每场会议下面那行预览：最后一句话，太长截断。 */
export function previewLine(
  t: Messages,
  summary: Pick<SessionSummary, "preview">,
  maxChars = 36,
): string {
  const last = summary.preview[summary.preview.length - 1];
  if (!last) return "";
  const text = last.text.length > maxChars ? `${last.text.slice(0, maxChars)}…` : last.text;
  return t.session.preview(last.speaker, text);
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
  t: Messages,
  connection: string,
  closedReason: string | null,
  retry: number,
): string {
  if (closedReason !== null) return t.header.recording.stopped;
  if (connection === "connected") return t.header.recording.active;
  if (retry > 0) return t.header.recording.reconnecting;
  if (connection === "connecting") return t.header.recording.connecting;
  return t.header.recording.idle;
}
