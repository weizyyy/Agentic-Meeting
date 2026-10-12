import { DownloadIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { SessionSummary } from "../api.ts";
import {
  generateLabel,
  reportBlocker,
  reportDownloadUrl,
  reportHeadline,
  type ReportInfo,
} from "../report.ts";

interface Props {
  session: SessionSummary | null;
  report: ReportInfo | null;
  onGenerate: () => void;
}

/** 会后报告：生成、查看（保留换行的文本）、下载。生成期间页面每 2 秒问一次服务端。 */
export function ReportPanel({ session, report, onGenerate }: Props) {
  const blocker = reportBlocker(session, report);
  return (
    <section className="flex min-h-0 flex-1 flex-col gap-3 px-4 py-3" aria-label="会后报告">
      <div className="flex flex-wrap items-center gap-3">
        <p
          className={`mr-auto text-sm ${
            report?.status === "failed" ? "text-destructive" : "text-muted-foreground"
          }`}
        >
          {reportHeadline(report)}
        </p>
        {session && report?.status === "done" && (
          <Button
            variant="outline"
            nativeButton={false}
            render={<a href={reportDownloadUrl(session.id)} download />}
          >
            <DownloadIcon data-icon="inline-start" />
            下载 .md
          </Button>
        )}
        <Button disabled={blocker !== null} title={blocker ?? undefined} onClick={onGenerate}>
          {generateLabel(report)}
        </Button>
      </div>
      {blocker !== null && report?.status !== "running" && (
        <p className="text-sm text-muted-foreground">{blocker}。</p>
      )}
      {report?.status === "done" && (
        // 模型写的内容按纯文字显示，不当 HTML 解释
        <pre className="min-h-0 flex-1 overflow-auto font-sans text-[0.9375rem] leading-relaxed whitespace-pre-wrap [overflow-wrap:anywhere]">
          {report.text_md}
        </pre>
      )}
    </section>
  );
}
