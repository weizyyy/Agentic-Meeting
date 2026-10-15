import {
  BookmarkCheckIcon,
  BookmarkIcon,
  ChevronDownIcon,
  DownloadIcon,
  EyeIcon,
  PencilIcon,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import type { SessionState, SessionSummary } from "../api.ts";
import type { ConnectionState } from "../meetingState.ts";
import type { Watcher } from "../protocol.ts";
import { formatDateTime, formatNumber, formatTimeOfDay } from "../i18n/format.ts";
import { formatDuration, formatSpan, useI18n } from "../i18n/index.ts";
import { interruptedSecs } from "../resume.ts";
import { canResume, exportUrl, sessionTitle } from "../sessionView.ts";

interface Props {
  session: SessionSummary | null;
  connection: ConnectionState;
  /** 连接意外断开后正在自动重连：第几次；0 表示没有 */
  reconnectAttempt: number;
  /** 正在只读地看一场在别的设备上进行的会议 */
  watching: boolean;
  /** 这台设备开着这场会议时：别的设备里谁在旁观 */
  watchers: Watcher[];
  keepPending: boolean;
  onKeep: (id: string, keep: boolean) => void;
  onRename: (id: string, title: string) => void;
  onResume: (id: string) => void;
  onStart: () => void;
}

/** 会议状态前面那个点的颜色 */
export const STATE_TONES: Record<SessionState, string> = {
  live: "text-success-foreground",
  interrupted: "text-warning-foreground",
  ended: "text-subtle-foreground",
};

/** 标题下方的提示条：中断了可以继续、正在重连、在别的设备上进行、删除没完成 */
function Hint({ tone, children }: { tone: "info" | "warn" | "error"; children: ReactNode }) {
  const tones = {
    info: "bg-muted text-foreground",
    warn: "bg-warning text-warning-foreground",
    error: "bg-destructive/10 text-destructive",
  };
  return (
    <p
      className={`flex basis-full flex-wrap items-center gap-x-3 gap-y-1 rounded-lg px-3 py-2 text-sm ${tones[tone]}`}
    >
      {children}
    </p>
  );
}

/**
 * 字幕区上方：正在看的是哪一场会议（标题可以点笔改）、什么状态、开始于何时；
 * 右边是「保留记录」和「导出」；没在开的会议可以继续。
 */
export function SessionBanner({
  session,
  connection,
  reconnectAttempt,
  watching,
  watchers,
  onRename,
  onKeep,
  keepPending,
  onResume,
  onStart,
}: Props) {
  const { t, language } = useI18n();
  const [editing, setEditing] = useState<string | null>(null);
  // 「已中断 12 分钟」要跟着时间走：半分钟刷新一次
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  if (!session) {
    return <p className="mb-4 text-sm text-muted-foreground">{t.session.none}</p>;
  }

  const commit = () => {
    if (editing !== null && editing.trim() !== session.title.trim()) onRename(session.id, editing);
    setEditing(null);
  };
  const idle = !session.deletion_pending && canResume(connection) && reconnectAttempt === 0;
  const interrupted = session.state === "interrupted" && idle;
  const pending = session.deletion_pending;

  return (
    <section
      className="mb-4 flex flex-wrap items-start gap-x-4 gap-y-3"
      aria-label={t.session.current}
    >
      <div className="min-w-64 flex-1">
        <div className="flex min-h-9 items-center gap-1">
          {editing === null ? (
            <>
              <h2 className="truncate text-2xl font-semibold tracking-tight">
                {sessionTitle(t, language, session)}
              </h2>
              <Button
                variant="ghost"
                size="icon-sm"
                className="text-muted-foreground"
                disabled={pending}
                aria-label={t.session.rename}
                onClick={() => setEditing(session.title)}
              >
                <PencilIcon />
              </Button>
            </>
          ) : (
            <Input
              className="h-9 max-w-md text-lg md:text-lg"
              aria-label={t.session.title}
              value={editing}
              maxLength={200}
              autoFocus
              onChange={(event) => setEditing(event.target.value)}
              onBlur={commit}
              onKeyDown={(event) => {
                if (event.key === "Enter") commit();
                if (event.key === "Escape") setEditing(null);
              }}
            />
          )}
        </div>
        <p className="mt-1 flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
          <span
            className={`inline-flex items-center gap-1.5 font-medium ${
              pending ? "text-destructive" : STATE_TONES[session.state]
            }`}
          >
            <span aria-hidden className="size-1.5 rounded-full bg-current" />
            {pending ? t.session.deletionPending : t.session.states[session.state]}
          </span>
          <span>{t.session.startedAt(formatDateTime(language, session.started_at))}</span>
          <span aria-hidden>·</span>
          <span className="tabular-nums">{formatDuration(t, session.duration_secs)}</span>
          <span aria-hidden>·</span>
          <span>{t.session.utterances(formatNumber(language, session.utterance_count))}</span>
          {watchers.length > 0 && (
            <DropdownMenu>
              <DropdownMenuTrigger
                render={
                  <Button
                    variant="ghost"
                    size="xs"
                    className="-my-1 text-xs font-normal text-muted-foreground"
                  />
                }
              >
                <EyeIcon data-icon="inline-start" />
                {t.session.watchers.count(formatNumber(language, watchers.length))}
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="w-64">
                <DropdownMenuGroup>
                  <DropdownMenuLabel>{t.session.watchers.title}</DropdownMenuLabel>
                  {watchers.map((w) => (
                    <DropdownMenuItem key={w.id} className="justify-between">
                      <span className="truncate">
                        {w.device || t.session.watchers.unknownDevice}
                      </span>
                      <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                        {t.session.watchers.since(formatTimeOfDay(language, w.since))}
                      </span>
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {session.state === "ended" && idle && (
          <Button variant="outline" onClick={() => onResume(session.id)}>
            {t.session.resumeEnded}
          </Button>
        )}
        <Button
          variant="ghost"
          aria-pressed={session.keep}
          disabled={pending || keepPending}
          title={t.common.keepHint}
          onClick={() => onKeep(session.id, !session.keep)}
        >
          {session.keep ? (
            <BookmarkCheckIcon data-icon="inline-start" />
          ) : (
            <BookmarkIcon data-icon="inline-start" />
          )}
          {keepPending ? t.common.saving : session.keep ? t.common.kept : t.common.keep}
        </Button>
        {!pending && (
          <DropdownMenu>
            <DropdownMenuTrigger render={<Button variant="outline" />}>
              <DownloadIcon data-icon="inline-start" />
              {t.session.export}
              <ChevronDownIcon data-icon="inline-end" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-56">
              <DropdownMenuItem render={<a href={exportUrl(session.id)} download />}>
                {t.session.exportTranscript}
                <span className="ml-auto text-xs text-muted-foreground">Markdown</span>
              </DropdownMenuItem>
              <DropdownMenuItem render={<a href={exportUrl(session.id, "json")} download />}>
                JSON
              </DropdownMenuItem>
              <DropdownMenuItem
                render={<a href={exportUrl(session.id, "zip")} download />}
                title={t.session.exportFullHint}
              >
                {t.session.exportFull}
                <span className="ml-auto text-xs text-muted-foreground">
                  {t.session.exportFullNote}
                </span>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
      {pending && <Hint tone="error">{t.session.deletionIncomplete}</Hint>}
      {reconnectAttempt > 0 && <Hint tone="warn">{t.session.reconnecting(reconnectAttempt)}</Hint>}
      {watching && !pending && (
        <Hint tone="info">
          {t.session.watching}
          <Button variant="outline" size="sm" onClick={() => onResume(session.id)}>
            {t.session.takeOverHere}
          </Button>
        </Hint>
      )}
      {interrupted && (
        <Hint tone="warn">
          {interruptedSecs(session, now) < 60
            ? t.session.justInterrupted
            : t.session.interruptedFor(formatSpan(t, interruptedSecs(session, now)))}
          <Button
            size="sm"
            className="bg-active text-active-foreground hover:bg-active/85"
            onClick={() => onResume(session.id)}
          >
            {t.session.resume}
          </Button>
          <Button variant="ghost" size="sm" onClick={onStart}>
            {t.header.start}
          </Button>
        </Hint>
      )}
    </section>
  );
}
