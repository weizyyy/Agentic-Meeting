import {
  CircleIcon,
  DownloadIcon,
  FileTextIcon,
  LoaderCircleIcon,
  PackageIcon,
  RotateCwIcon,
} from "lucide-react";
import { useMemo } from "react";

import { Button } from "@/components/ui/button";
import type { SessionSummary } from "../api.ts";
import { formatNumber } from "../i18n/format.ts";
import { useI18n } from "../i18n/index.ts";
import {
  generateLabel,
  parseReport,
  reportBlocker,
  reportDownloadUrl,
  reportHeadline,
  type ReportBlock,
  type ReportInfo,
  type ReportSection,
} from "../report.ts";
import { exportUrl } from "../sessionView.ts";

interface Props {
  session: SessionSummary | null;
  report: ReportInfo | null;
  /** 这场会议的后台任务数和截图数，写在「会议资料」里 */
  taskCount: number;
  frameCount: number;
  onGenerate: () => void;
  /** 切回字幕页签 */
  onShowCaptions: () => void;
}

/** 模型写「未提及」的地方淡一些，一眼能看出哪几节是空的（报告用会议的语言写，两种写法都认） */
const EMPTY = new Set(["未提及", "Not mentioned"]);

/**
 * 会后报告：按小节排版的报告正文（只认标题和列表，模型写的内容从不当 HTML 解释），
 * 宽屏时右边是「会议资料」。生成期间页面每 2 秒问一次服务端。
 */
export function ReportPanel({
  session,
  report,
  taskCount,
  frameCount,
  onGenerate,
  onShowCaptions,
}: Props) {
  const { t, language } = useI18n();
  const blocker = reportBlocker(t, session, report);
  const sections = useMemo(
    () => (report?.status === "done" ? parseReport(report.text_md) : []),
    [report],
  );
  const running = report?.status === "running";

  const generate = (
    <Button
      variant={report === null ? "default" : "outline"}
      disabled={blocker !== null}
      title={blocker ?? undefined}
      onClick={onGenerate}
    >
      {running ? (
        <LoaderCircleIcon data-icon="inline-start" className="animate-spin" />
      ) : report === null ? (
        <FileTextIcon data-icon="inline-start" />
      ) : (
        <RotateCwIcon data-icon="inline-start" />
      )}
      {generateLabel(t, report)}
    </Button>
  );

  return (
    <section
      className="flex min-h-0 flex-1 gap-6 overflow-y-auto px-4 py-4 md:px-6"
      aria-label={t.report.label}
    >
      <div className="flex min-w-0 flex-1 flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2">
          <p
            className={`mr-auto text-sm ${
              report?.status === "failed" ? "text-destructive" : "text-muted-foreground"
            }`}
          >
            {reportHeadline(t, report)}
          </p>
          {session && report?.status === "done" && (
            <Button
              variant="outline"
              nativeButton={false}
              render={<a href={reportDownloadUrl(session.id)} download />}
            >
              <DownloadIcon data-icon="inline-start" />
              {t.report.download}
            </Button>
          )}
          {report !== null && generate}
        </div>
        {report === null ? (
          <div className="flex flex-col items-center gap-3 rounded-xl border border-dashed px-6 py-10 text-center">
            <FileTextIcon aria-hidden className="size-8 text-subtle-foreground" />
            <p className="max-w-sm text-sm text-muted-foreground">{t.report.sections}</p>
            {generate}
            {blocker !== null && <p className="text-sm text-subtle-foreground">{blocker}</p>}
          </div>
        ) : (
          blocker !== null && !running && <p className="text-sm text-muted-foreground">{blocker}</p>
        )}
        {report?.status === "done" && (
          <article className="flex max-w-3xl flex-col gap-6">
            {sections.map((section, index) => (
              <Section key={index} section={section} />
            ))}
          </article>
        )}
      </div>
      {session && (
        <aside
          className="sticky top-0 hidden w-56 shrink-0 flex-col gap-3 self-start rounded-xl border p-4 lg:flex"
          aria-label={t.report.materials}
        >
          <h3 className="text-sm font-medium">{t.report.materials}</h3>
          <ul className="flex flex-col gap-1 text-sm text-muted-foreground tabular-nums">
            <li>{t.report.utterances(formatNumber(language, session.utterance_count))}</li>
            <li>{t.report.taskCount(formatNumber(language, taskCount))}</li>
            <li>{t.report.frameCount(formatNumber(language, frameCount))}</li>
          </ul>
          <div className="flex flex-col items-start gap-1.5 border-t pt-3">
            <Button variant="outline" size="sm" onClick={onShowCaptions}>
              <FileTextIcon data-icon="inline-start" />
              {t.report.showCaptions}
            </Button>
            <Button
              variant="outline"
              size="sm"
              nativeButton={false}
              render={<a href={exportUrl(session.id, "zip")} download />}
              title={t.session.exportFullHint}
            >
              <PackageIcon data-icon="inline-start" />
              {t.report.exportFull}
            </Button>
          </div>
        </aside>
      )}
    </section>
  );
}

function Section({ section }: { section: ReportSection }) {
  // 待办事项一条一行、像清单那样排；别的小节是普通列表
  const todo = section.title !== null && /待办|action items/i.test(section.title);
  const groups = groupBlocks(section.blocks);
  return (
    <section className="flex flex-col gap-2">
      {section.title && <h3 className="text-base font-semibold">{section.title}</h3>}
      {groups.map((group, index) =>
        group.kind === "text" ? (
          group.blocks.map((block, i) => (
            <p key={`${index}-${i}`} className={textTone(block, "leading-relaxed")}>
              {block.text}
            </p>
          ))
        ) : todo ? (
          <ul key={index} className="flex flex-col gap-1.5">
            {group.blocks.map((block, i) => (
              <li
                key={i}
                className={textTone(
                  block,
                  "flex items-start gap-2.5 rounded-lg bg-muted px-3 py-2",
                )}
              >
                <CircleIcon aria-hidden className="mt-1 size-3.5 shrink-0 text-subtle-foreground" />
                <span className="min-w-0 [overflow-wrap:anywhere]">{block.text}</span>
              </li>
            ))}
          </ul>
        ) : (
          <ul
            key={index}
            className="flex list-disc flex-col gap-1 pl-5 marker:text-subtle-foreground"
          >
            {group.blocks.map((block, i) => (
              <li key={i} className={textTone(block, "leading-relaxed [overflow-wrap:anywhere]")}>
                {block.text}
              </li>
            ))}
          </ul>
        ),
      )}
    </section>
  );
}

function textTone(block: ReportBlock, base: string): string {
  return EMPTY.has(block.text) ? `${base} text-subtle-foreground` : base;
}

/** 相邻的列表项合成一个列表，相邻的文字行各自成段 */
function groupBlocks(
  blocks: readonly ReportBlock[],
): { kind: ReportBlock["kind"]; blocks: ReportBlock[] }[] {
  const groups: { kind: ReportBlock["kind"]; blocks: ReportBlock[] }[] = [];
  for (const block of blocks) {
    const last = groups[groups.length - 1];
    if (last && last.kind === block.kind) last.blocks.push(block);
    else groups.push({ kind: block.kind, blocks: [block] });
  }
  return groups;
}
