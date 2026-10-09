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
    <section className="panel report-panel" aria-label="会后报告">
      <div className="report-head">
        <p className={report?.status === "failed" ? "notice notice-error" : "report-status"}>
          {reportHeadline(report)}
        </p>
        <span className="spacer" />
        {session && report?.status === "done" && (
          <a className="link" href={reportDownloadUrl(session.id)} download>
            下载 .md
          </a>
        )}
        <button
          type="button"
          className="button button-start button-small"
          disabled={blocker !== null}
          title={blocker ?? undefined}
          onClick={onGenerate}
        >
          {generateLabel(report)}
        </button>
      </div>
      {blocker !== null && report?.status !== "running" && (
        <p className="report-hint">{blocker}。</p>
      )}
      {report?.status === "done" && (
        // 模型写的内容按纯文字显示，不当 HTML 解释
        <pre className="report-text">{report.text_md}</pre>
      )}
    </section>
  );
}
