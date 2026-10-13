import {
  BookmarkCheckIcon,
  BookmarkIcon,
  ChevronDownIcon,
  DownloadIcon,
  PencilIcon,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import type { SessionState, SessionSummary } from "../api.ts";
import type { ConnectionState } from "../meetingState.ts";
import { formatSpan, interruptedSecs } from "../resume.ts";
import {
  STATE_LABELS,
  canResume,
  exportUrl,
  formatDuration,
  formatStartedAt,
  sessionTitle,
} from "../sessionView.ts";

interface Props {
  session: SessionSummary | null;
  connection: ConnectionState;
  /** 连接意外断开后正在自动重连：第几次；0 表示没有 */
  reconnectAttempt: number;
  /** 正在只读地看一场在别的设备上进行的会议 */
  watching: boolean;
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
  onRename,
  onKeep,
  keepPending,
  onResume,
  onStart,
}: Props) {
  const [editing, setEditing] = useState<string | null>(null);
  // 「已中断 12 分钟」要跟着时间走：半分钟刷新一次
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  if (!session) {
    return (
      <p className="mb-4 text-sm text-muted-foreground">
        还没有会议。点右上角的「开始新会议」，说的话会实时显示在下面，并保存在服务端。
      </p>
    );
  }

  const commit = () => {
    if (editing !== null && editing.trim() !== session.title.trim()) onRename(session.id, editing);
    setEditing(null);
  };
  const idle = !session.deletion_pending && canResume(connection) && reconnectAttempt === 0;
  const interrupted = session.state === "interrupted" && idle;
  const pending = session.deletion_pending;

  return (
    <div
      className="mb-4 flex flex-wrap items-start gap-x-4 gap-y-3"
      role="group"
      aria-label="当前查看的会议"
    >
      <div className="min-w-64 flex-1">
        <div className="flex min-h-9 items-center gap-1">
          {editing === null ? (
            <>
              <h2 className="truncate text-2xl font-semibold tracking-tight">
                {sessionTitle(session)}
              </h2>
              <Button
                variant="ghost"
                size="icon-sm"
                className="text-muted-foreground"
                disabled={pending}
                aria-label="重命名这场会议"
                onClick={() => setEditing(session.title)}
              >
                <PencilIcon />
              </Button>
            </>
          ) : (
            <Input
              className="h-9 max-w-md text-lg md:text-lg"
              aria-label="会议标题"
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
            {pending ? "待删除" : STATE_LABELS[session.state]}
          </span>
          <span>开始于 {formatStartedAt(session.started_at)}</span>
          <span aria-hidden>·</span>
          <span className="tabular-nums">{formatDuration(session.duration_secs)}</span>
          <span aria-hidden>·</span>
          <span>{session.utterance_count} 条发言</span>
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {session.state === "ended" && idle && (
          <Button variant="outline" onClick={() => onResume(session.id)}>
            继续这场会议
          </Button>
        )}
        <Button
          variant="ghost"
          aria-pressed={session.keep}
          disabled={pending || keepPending}
          title="免于自动清理；仍可人工删除"
          onClick={() => onKeep(session.id, !session.keep)}
        >
          {session.keep ? (
            <BookmarkCheckIcon data-icon="inline-start" />
          ) : (
            <BookmarkIcon data-icon="inline-start" />
          )}
          {keepPending ? "保存中…" : session.keep ? "已保留" : "保留"}
        </Button>
        {!pending && (
          <DropdownMenu>
            <DropdownMenuTrigger render={<Button variant="outline" />}>
              <DownloadIcon data-icon="inline-start" />
              导出
              <ChevronDownIcon data-icon="inline-end" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-56">
              <DropdownMenuItem render={<a href={exportUrl(session.id)} download />}>
                转录
                <span className="ml-auto text-xs text-muted-foreground">Markdown</span>
              </DropdownMenuItem>
              <DropdownMenuItem render={<a href={exportUrl(session.id, "json")} download />}>
                JSON
              </DropdownMenuItem>
              <DropdownMenuItem
                render={<a href={exportUrl(session.id, "zip")} download />}
                title="转录、JSON、报告、全部截图和任务产物，打成一个压缩包"
              >
                完整包
                <span className="ml-auto text-xs text-muted-foreground">含截图和产物</span>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>
      {pending && (
        <Hint tone="error">删除尚未完成，内容可能已部分移除。请在会议列表重试删除。</Hint>
      )}
      {reconnectAttempt > 0 && (
        <Hint tone="warn">连接断开了，正在自动重连（第 {reconnectAttempt} 次）…</Hint>
      )}
      {watching && !pending && (
        <Hint tone="info">
          正在另一台设备上进行，这里只读，每 3 秒刷新一次。
          <Button variant="outline" size="sm" onClick={() => onResume(session.id)}>
            在这台设备上继续
          </Button>
        </Hint>
      )}
      {interrupted && (
        <Hint tone="warn">
          {interruptedSecs(session, now) < 60
            ? "这场会议刚刚中断"
            : `这场会议已中断 ${formatSpan(interruptedSecs(session, now))}`}
          <Button
            size="sm"
            className="bg-active text-active-foreground hover:bg-active/85"
            onClick={() => onResume(session.id)}
          >
            继续
          </Button>
          <Button variant="ghost" size="sm" onClick={onStart}>
            开始新会议
          </Button>
        </Hint>
      )}
    </div>
  );
}
