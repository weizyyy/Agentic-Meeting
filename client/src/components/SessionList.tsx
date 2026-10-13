import {
  BookmarkCheckIcon,
  BookmarkIcon,
  PencilIcon,
  PlayIcon,
  Trash2Icon,
  XIcon,
} from "lucide-react";
import { useState } from "react";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import type { SessionSummary } from "../api.ts";
import {
  STATE_LABELS,
  canDelete,
  formatDuration,
  formatStartedAt,
  previewLine,
  sessionTitle,
} from "../sessionView.ts";
import { STATE_TONES } from "./SessionBanner.tsx";

interface Props {
  open: boolean;
  sessions: readonly SessionSummary[];
  viewingId: string | null;
  /** 连接期间不能切到别的会议去看 */
  locked: boolean;
  onSelect: (id: string) => void;
  onResume: (id: string) => void;
  onRename: (id: string, title: string) => void;
  keepPending: ReadonlySet<string>;
  onKeep: (id: string, keep: boolean) => void;
  onDelete: (id: string) => void;
  onClose: () => void;
}

/** 会议列表（右侧抽屉）：最近的会议，点一场在字幕区查看；可以继续、保留、重命名、删除（删除前确认）。 */
export function SessionList({
  open,
  sessions,
  viewingId,
  locked,
  onSelect,
  onResume,
  onRename,
  onDelete,
  onKeep,
  keepPending,
  onClose,
}: Props) {
  const [editing, setEditing] = useState<{ id: string; title: string } | null>(null);
  // 等用户确认删除的那场会议
  const [deleting, setDeleting] = useState<SessionSummary | null>(null);

  const commit = () => {
    if (editing) onRename(editing.id, editing.title);
    setEditing(null);
  };

  return (
    <Sheet
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <SheetContent
        id="session-list"
        showCloseButton={false}
        className="w-[min(400px,92vw)] gap-0 sm:max-w-none"
      >
        <SheetHeader className="flex-row items-center justify-between border-b py-3">
          <SheetTitle>最近的会议</SheetTitle>
          <Button variant="ghost" size="icon-sm" aria-label="关闭会议列表" onClick={onClose}>
            <XIcon />
          </Button>
        </SheetHeader>
        <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4">
          {locked && (
            <p className="rounded-lg bg-warning px-3 py-2 text-warning-foreground">
              会议进行中，结束后才能查看其他会议。
            </p>
          )}
          {sessions.length === 0 ? (
            <p className="text-subtle-foreground">还没有会议记录。</p>
          ) : (
            <ul className="flex flex-col gap-2">
              {sessions.map((s) => {
                const title = sessionTitle(s);
                const preview = s.deletion_pending ? null : previewLine(s);
                return (
                  <li
                    key={s.id}
                    className={`rounded-lg border p-1 ${
                      s.id === viewingId ? "border-client-foreground/40 bg-client" : ""
                    }`}
                  >
                    {editing?.id === s.id ? (
                      <div className="p-1">
                        <Input
                          aria-label="会议标题"
                          value={editing.title}
                          maxLength={200}
                          autoFocus
                          onChange={(event) => setEditing({ id: s.id, title: event.target.value })}
                          onBlur={commit}
                          onKeyDown={(event) => {
                            if (event.key === "Enter") commit();
                            if (event.key === "Escape") {
                              // 只取消改名，不连带关掉抽屉
                              event.stopPropagation();
                              setEditing(null);
                            }
                          }}
                        />
                      </div>
                    ) : (
                      <button
                        type="button"
                        className="flex w-full flex-col gap-0.5 rounded-md px-2 py-1.5 text-left transition-colors outline-none enabled:hover:bg-muted/60 focus-visible:ring-3 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-60"
                        disabled={locked && s.state !== "live"}
                        onClick={() => onSelect(s.id)}
                      >
                        <span className="font-medium [overflow-wrap:anywhere]">{title}</span>
                        <span className="flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
                          <span
                            className={`inline-flex items-center gap-1.5 font-medium ${
                              s.deletion_pending ? "text-destructive" : STATE_TONES[s.state]
                            }`}
                          >
                            <span aria-hidden className="size-1.5 rounded-full bg-current" />
                            {s.deletion_pending ? "待删除" : STATE_LABELS[s.state]}
                          </span>
                          <span>{formatStartedAt(s.started_at)}</span>
                          <span aria-hidden>·</span>
                          <span className="tabular-nums">{formatDuration(s.duration_secs)}</span>
                          <span aria-hidden>·</span>
                          <span>{s.utterance_count} 条</span>
                        </span>
                        {preview && (
                          <span className="line-clamp-2 text-xs text-muted-foreground [overflow-wrap:anywhere]">
                            {preview}
                          </span>
                        )}
                      </button>
                    )}
                    <div className="flex flex-wrap justify-end gap-0.5 px-1 pb-0.5">
                      <Button
                        variant="ghost"
                        size="xs"
                        aria-pressed={s.keep}
                        aria-label={`保留：${title}`}
                        disabled={s.deletion_pending || keepPending.has(s.id)}
                        title="免于自动清理；仍可人工删除"
                        onClick={() => onKeep(s.id, !s.keep)}
                      >
                        {s.keep ? (
                          <BookmarkCheckIcon data-icon="inline-start" />
                        ) : (
                          <BookmarkIcon data-icon="inline-start" />
                        )}
                        {keepPending.has(s.id) ? "保存中…" : s.keep ? "已保留" : "保留"}
                      </Button>
                      {!locked && !s.deletion_pending && (
                        <Button
                          variant="ghost"
                          size="xs"
                          aria-label={`继续：${title}`}
                          title={
                            s.state === "live"
                              ? "这场会议正在另一台设备上进行；点了之后连接会转到这台设备"
                              : undefined
                          }
                          onClick={() => onResume(s.id)}
                        >
                          <PlayIcon data-icon="inline-start" />
                          {s.state === "live" ? "接过来继续" : "继续"}
                        </Button>
                      )}
                      <Button
                        variant="ghost"
                        size="xs"
                        disabled={s.deletion_pending}
                        aria-label={`重命名：${title}`}
                        onClick={() => setEditing({ id: s.id, title: s.title })}
                      >
                        <PencilIcon data-icon="inline-start" />
                        重命名
                      </Button>
                      <Button
                        variant="ghost"
                        size="xs"
                        className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                        aria-label={`删除：${title}`}
                        disabled={!canDelete(s)}
                        title={canDelete(s) ? undefined : "会议进行中，请先结束"}
                        onClick={() => setDeleting(s)}
                      >
                        <Trash2Icon data-icon="inline-start" />
                        {s.deletion_pending ? "重试删除" : "删除"}
                      </Button>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
        {/* 放在抽屉里面：确认框算抽屉的子对话框，点它或按 Esc 不会连带关掉抽屉 */}
        <AlertDialog
          open={deleting !== null}
          onOpenChange={(next) => {
            if (!next) setDeleting(null);
          }}
        >
          {deleting && (
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>删除「{sessionTitle(deleting)}」？</AlertDialogTitle>
                <AlertDialogDescription>
                  它的全部发言、截图和任务记录都会被删除，不能恢复。
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>取消</AlertDialogCancel>
                <AlertDialogAction
                  variant="destructive"
                  onClick={() => {
                    onDelete(deleting.id);
                    setDeleting(null);
                  }}
                >
                  <Trash2Icon data-icon="inline-start" />
                  删除
                </AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          )}
        </AlertDialog>
      </SheetContent>
    </Sheet>
  );
}
