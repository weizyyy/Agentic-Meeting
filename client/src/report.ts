// 会后报告的显示逻辑（docs/interfaces.md §5.6）。纯函数，可以用 npm test 直接测。

import type { SessionState } from "./api.ts";
import { failureText, type Messages } from "./i18n/index.ts";

export type ReportStatus = "running" | "done" | "failed";

/** `GET /api/sessions/{id}/report` 返回的最近一份报告。 */
export interface ReportInfo {
  id: number;
  status: ReportStatus;
  created_at: number;
  provider: string;
  text_md: string;
  /** 失败原因的中文；error_code 和 error_params 见 interfaces.md §5.10，显示时用 failureText 翻译 */
  error: string | null;
  error_code: string | null;
  error_params: Readonly<Record<string, string | number>>;
}

/** 生成期间多久问一次服务端。 */
export const REPORT_POLL_MS = 2000;

/** 报告的下载地址。 */
export function reportDownloadUrl(sessionId: string): string {
  return `/api/sessions/${encodeURIComponent(sessionId)}/report.md`;
}

/**
 * 现在为什么不能生成报告；可以生成时返回 null。
 * 会议要先结束或断开（服务端也会拒绝进行中的会议）；已经有一份在生成时不能再点。
 */
export function reportBlocker(
  t: Messages,
  session: { state: SessionState; utterance_count: number } | null,
  report: Pick<ReportInfo, "status"> | null,
): string | null {
  if (!session) return t.report.noSession;
  if (session.state === "live") return t.report.live;
  if (report?.status === "running") return t.report.running;
  if (session.utterance_count === 0) return t.report.noUtterances;
  return null;
}

/** 按钮上的字：没有报告时「生成报告」，有了之后「重新生成」。 */
export function generateLabel(t: Messages, report: Pick<ReportInfo, "status"> | null): string {
  return report === null ? t.report.generate : t.report.regenerate;
}

/** 报告区顶上那行说明。 */
export function reportHeadline(t: Messages, report: ReportInfo | null): string {
  if (report === null) return t.report.none;
  const who = t.report.providers[report.provider] ?? report.provider;
  switch (report.status) {
    case "running":
      return t.report.generating(who);
    case "failed":
      return t.report.failed(failureText(t, report) ?? t.report.unknownReason);
    case "done":
      return t.report.done(who);
  }
}

/** 报告里的一段：列表的一条，或一行普通文字。 */
export interface ReportBlock {
  kind: "item" | "text";
  text: string;
}

/** 报告的一节；第一个标题之前的文字归在没有标题的一节里。 */
export interface ReportSection {
  title: string | null;
  blocks: ReportBlock[];
}

/**
 * 把报告拆成小节，好按节排版。提示词只让模型用「## 」标题和「- 」列表两种标记（config/prompts/report.md），
 * 这里也只认这两种（多几个 # 或用 * 开头的列表同样认）；别的行原样当文字，从不当 HTML 解释。
 */
export function parseReport(markdown: string): ReportSection[] {
  const sections: ReportSection[] = [];
  let current: ReportSection | null = null;
  for (const raw of markdown.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    const heading = /^#{1,6}\s+(.*)$/.exec(line);
    if (heading) {
      current = { title: heading[1].trim(), blocks: [] };
      sections.push(current);
      continue;
    }
    if (!current) {
      current = { title: null, blocks: [] };
      sections.push(current);
    }
    const item = /^[-*+]\s+(.*)$/.exec(line);
    current.blocks.push(item ? { kind: "item", text: item[1] } : { kind: "text", text: line });
  }
  return sections;
}
