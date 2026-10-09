// 会后报告的显示逻辑（docs/interfaces.md §5.6）。纯函数，可以用 npm test 直接测。

import type { SessionState } from "./api.ts";

export type ReportStatus = "running" | "done" | "failed";

/** `GET /api/sessions/{id}/report` 返回的最近一份报告。 */
export interface ReportInfo {
  id: number;
  status: ReportStatus;
  created_at: number;
  provider: string;
  text_md: string;
  error: string | null;
}

/** 生成期间多久问一次服务端。 */
export const REPORT_POLL_MS = 2000;

export const REPORT_PROVIDER_LABELS: Record<string, string> = {
  realtime_llm: "实时模型",
  agent_llm: "后台的远端模型",
};

/** 报告的下载地址。 */
export function reportDownloadUrl(sessionId: string): string {
  return `/api/sessions/${encodeURIComponent(sessionId)}/report.md`;
}

/**
 * 现在为什么不能生成报告；可以生成时返回 null。
 * 会议要先结束或断开（服务端也会拒绝进行中的会议）；已经有一份在生成时不能再点。
 */
export function reportBlocker(
  session: { state: SessionState; utterance_count: number } | null,
  report: Pick<ReportInfo, "status"> | null,
): string | null {
  if (!session) return "还没有会议";
  if (session.state === "live") return "会议正在进行，结束或断开之后才能生成报告";
  if (report?.status === "running") return "报告正在生成";
  if (session.utterance_count === 0) return "这场会议没有发言记录，写不出报告";
  return null;
}

/** 按钮上的字：没有报告时「生成报告」，有了之后「重新生成」。 */
export function generateLabel(report: Pick<ReportInfo, "status"> | null): string {
  return report === null ? "生成报告" : "重新生成";
}

/** 报告区顶上那行说明。 */
export function reportHeadline(report: ReportInfo | null): string {
  if (report === null) return "这场会议还没有报告。";
  const who = REPORT_PROVIDER_LABELS[report.provider] ?? report.provider;
  switch (report.status) {
    case "running":
      return `正在生成报告（由${who}撰写），长会议可能要几分钟…`;
    case "failed":
      return `报告没有生成出来：${report.error ?? "原因不明"}`;
    case "done":
      return `由${who}生成。`;
  }
}
