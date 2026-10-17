import {
  CircleCheckIcon,
  CircleDashedIcon,
  CircleSlashIcon,
  CircleXIcon,
  LoaderCircleIcon,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { failureText, useI18n } from "../i18n/index.ts";
import { formatFrameTime, frameImageUrl } from "../screenCapture.ts";
import {
  artifactUrl,
  isFinished,
  isImageArtifact,
  safeLink,
  taskDuration,
  taskSubline,
  type TaskDetail,
  type TaskItem,
  type TaskStatus,
} from "../tasks.ts";

/** 每种状态的图标和颜色：排队虚线圈、进行中转圈、完成打勾、失败打叉、取消划斜线 */
const STATUS_ICONS: Record<TaskStatus, { icon: LucideIcon; tone: string }> = {
  queued: { icon: CircleDashedIcon, tone: "text-subtle-foreground" },
  running: { icon: LoaderCircleIcon, tone: "animate-spin text-agent-foreground" },
  succeeded: { icon: CircleCheckIcon, tone: "text-success-foreground" },
  failed: { icon: CircleXIcon, tone: "text-destructive" },
  cancelled: { icon: CircleSlashIcon, tone: "text-subtle-foreground" },
};

const STATUS_TEXT: Record<TaskStatus, string> = {
  queued: "text-muted-foreground",
  running: "text-agent-foreground",
  succeeded: "text-success-foreground",
  failed: "text-destructive",
  cancelled: "text-muted-foreground",
};

const LINK = "text-client-foreground underline-offset-4 hover:underline";

function StatusIcon({ status }: { status: TaskStatus }) {
  const { icon: Icon, tone } = STATUS_ICONS[status];
  return <Icon aria-hidden className={`size-4 shrink-0 ${tone}`} />;
}

interface Props {
  tasks: readonly TaskItem[];
  onLoad: (id: string) => Promise<TaskDetail | null>;
  onCancel: (id: string) => void;
  readOnly?: boolean;
}

/** 后台任务面板：列表（状态、最近一步或结论），点开看详细结果、来源、产物、进度，以及这次任务外发了什么。 */
export function TaskPanel({ tasks, onLoad, onCancel, readOnly }: Props) {
  const { t } = useI18n();
  const [openId, setOpenId] = useState<string | null>(null);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const open = openId === null ? null : (tasks.find((task) => task.id === openId) ?? null);
  const openReason = open ? failureText(t, open) : null;

  // 打开时取一次；任务状态变了（比如做完了）再取一次，拿到结果
  const openStatus = open?.status;
  useEffect(() => {
    if (openId === null) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    void onLoad(openId).then((loaded) => {
      if (!cancelled) setDetail(loaded);
    });
    return () => {
      cancelled = true;
    };
  }, [openId, openStatus, onLoad]);

  if (tasks.length === 0) {
    // 没有任务时也保留入口和说明，让用户能确认任务是否真的创建。
    return (
      <section className="shrink-0 rounded-xl border bg-card" aria-label={t.tasks.title}>
        <h2 className="px-4 pt-3 pb-2 text-sm font-medium">{t.tasks.title}</h2>
        <p className="px-4 pb-4 text-sm text-subtle-foreground">{t.tasks.empty}</p>
      </section>
    );
  }

  return (
    // 右列放不下时先让任务列表缩小、在框里滚动，助理的回答保持完整
    <section
      className="flex max-h-[55%] min-h-24 shrink-[100] flex-col rounded-xl border bg-card max-md:max-h-none"
      aria-label={t.tasks.title}
    >
      <h2 className="px-4 pt-3 pb-2 text-sm font-medium">{t.tasks.title}</h2>
      <ul className="flex min-h-0 flex-col gap-2 overflow-y-auto px-4 pb-4">
        {[...tasks].reverse().map((task) => (
          <li key={task.id}>
            <button
              type="button"
              className="flex w-full items-start gap-3 rounded-lg border px-3 py-2.5 text-left transition-colors outline-none hover:bg-muted/60 focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
              onClick={() => setOpenId(task.id)}
            >
              <span className="pt-0.5">
                <StatusIcon status={task.status} />
              </span>
              <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="line-clamp-2 text-sm font-medium [overflow-wrap:anywhere]">
                  {task.goal}
                </span>
                <span className="flex flex-wrap items-center gap-x-1.5 text-xs">
                  <span className="font-mono text-subtle-foreground">{task.label}</span>
                  <span className={STATUS_TEXT[task.status]}>{t.tasks.statuses[task.status]}</span>
                  {task.modality === "text" && (
                    <Badge variant="secondary" className="h-4 px-1.5">
                      {t.tasks.typed}
                    </Badge>
                  )}
                </span>
                <span className="line-clamp-2 text-xs text-muted-foreground [overflow-wrap:anywhere]">
                  {taskSubline(t, task)}
                </span>
              </span>
            </button>
          </li>
        ))}
      </ul>
      <Dialog
        open={open !== null}
        onOpenChange={(next) => {
          if (!next) setOpenId(null);
        }}
      >
        {open && (
          <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-2xl">
            <DialogHeader>
              <DialogTitle className="flex items-center gap-2 pr-8 text-lg">
                {t.tasks.task(open.label)}
                <span
                  className={`inline-flex items-center gap-1 text-sm font-normal ${STATUS_TEXT[open.status]}`}
                >
                  <StatusIcon status={open.status} />
                  {t.tasks.statuses[open.status]}
                </span>
              </DialogTitle>
              <DialogDescription className="text-foreground">{open.goal}</DialogDescription>
            </DialogHeader>
            {open.status === "succeeded" && open.brief && (
              <p className="rounded-lg bg-success px-3 py-2 text-success-foreground">
                {open.brief}
              </p>
            )}
            {(open.status === "failed" || open.status === "cancelled") && openReason && (
              <p className="rounded-lg bg-destructive/10 px-3 py-2 text-destructive">
                {openReason}
              </p>
            )}
            {detail === null || detail.id !== open.id ? (
              <p className="text-subtle-foreground">{t.tasks.loading}</p>
            ) : (
              <TaskBody detail={detail} />
            )}
            <DialogFooter>
              {!isFinished(open.status) && (
                <Button variant="destructive" disabled={readOnly} onClick={() => onCancel(open.id)}>
                  {t.tasks.cancel}
                </Button>
              )}
              <Button variant="outline" onClick={() => setOpenId(null)}>
                {t.common.close}
              </Button>
            </DialogFooter>
          </DialogContent>
        )}
      </Dialog>
    </section>
  );
}

function Heading({ children }: { children: ReactNode }) {
  return <h3 className="mb-1 text-xs font-medium text-muted-foreground">{children}</h3>;
}

function TaskBody({ detail }: { detail: TaskDetail }) {
  const { t } = useI18n();
  const duration = taskDuration(t, detail.started_at, detail.finished_at);
  const images = detail.artifacts.filter(isImageArtifact);
  const files = detail.artifacts.filter((name) => !isImageArtifact(name));
  return (
    <>
      {detail.detail_md && (
        <div>
          <Heading>{t.tasks.detail}</Heading>
          {/* 远端模型写的内容按纯文字显示，不当 HTML 解释 */}
          <pre className="font-sans leading-relaxed whitespace-pre-wrap [overflow-wrap:anywhere]">
            {detail.detail_md}
          </pre>
        </div>
      )}
      {images.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {images.map((name) => (
            <a key={name} href={artifactUrl(detail.id, name)} target="_blank" rel="noreferrer">
              <img
                className="block max-h-90 max-w-full rounded-md border"
                src={artifactUrl(detail.id, name)}
                alt={name}
                loading="lazy"
              />
            </a>
          ))}
        </div>
      )}
      {files.length > 0 && (
        <div>
          <Heading>{t.tasks.artifacts}</Heading>
          <ul className="list-disc pl-5 [overflow-wrap:anywhere]">
            {files.map((name) => (
              <li key={name}>
                <a className={LINK} href={artifactUrl(detail.id, name)} download>
                  {name}
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}
      {detail.sources.length > 0 && (
        <div>
          <Heading>{t.tasks.sources}</Heading>
          <ul className="list-disc pl-5 [overflow-wrap:anywhere]">
            {detail.sources.map((source) => {
              const href = safeLink(source);
              return (
                <li key={source}>
                  {href ? (
                    <a className={LINK} href={href} target="_blank" rel="noreferrer">
                      {source}
                    </a>
                  ) : (
                    source
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}
      <div>
        <Heading>{duration ? t.tasks.progressTook(duration) : t.tasks.progress}</Heading>
        <ol className="list-decimal pl-5 [overflow-wrap:anywhere]">
          {detail.events.map((event, index) => (
            <li
              key={index}
              className={
                event.kind === "status"
                  ? "font-medium"
                  : event.kind === "note"
                    ? "text-warning-foreground"
                    : undefined
              }
            >
              {event.summary}
            </li>
          ))}
        </ol>
      </div>
      <div className="rounded-lg bg-muted px-3 py-2.5">
        <Heading>{t.tasks.outbound}</Heading>
        <p>
          {detail.outbound.model_host
            ? t.tasks.sentTo(detail.outbound.model_host)
            : t.tasks.sentToAgent}
          {t.tasks.sentRange(
            formatFrameTime(detail.outbound.t_from),
            formatFrameTime(detail.outbound.t_to),
          )}
          {detail.outbound.frames.length > 0
            ? t.tasks.withFrames(detail.outbound.frames.length)
            : t.tasks.withoutFrames}
          {t.tasks.requestedBy(detail.requested_by)}
        </p>
        {detail.outbound.frames.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-2">
            {detail.outbound.frames.map((frame) => (
              <a key={frame.id} href={frameImageUrl(frame.id)} target="_blank" rel="noreferrer">
                <img
                  className="block aspect-video w-40 rounded-md border object-cover"
                  src={frameImageUrl(frame.id)}
                  alt={t.tasks.frameAlt(formatFrameTime(frame.t))}
                  loading="lazy"
                />
              </a>
            ))}
          </div>
        )}
      </div>
    </>
  );
}
