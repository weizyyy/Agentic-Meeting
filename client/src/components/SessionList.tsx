import { useState } from "react";

import type { SessionSummary } from "../api.ts";
import {
  STATE_LABELS,
  canDelete,
  formatDuration,
  formatStartedAt,
  previewLine,
  sessionTitle,
} from "../sessionView.ts";

interface Props {
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

/** 会议列表（抽屉）：最近的会议，点一场在字幕区查看；可以继续、重命名、删除。 */
export function SessionList({
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

  const commit = () => {
    if (editing) onRename(editing.id, editing.title);
    setEditing(null);
  };

  return (
    <aside className="drawer" id="session-list" aria-label="会议列表">
      <div className="drawer-head">
        <h2>最近的会议</h2>
        <button type="button" className="link" aria-label="关闭会议列表" onClick={onClose}>
          ×
        </button>
      </div>
      {locked && <p className="drawer-hint">会议进行中，结束后才能查看其他会议。</p>}
      {sessions.length === 0 ? (
        <p className="empty">还没有会议记录。</p>
      ) : (
        <ul className="sessions">
          {sessions.map((s) => (
            <li
              key={s.id}
              className={`session session-${s.state}${s.id === viewingId ? " session-current" : ""}`}
            >
              {editing?.id === s.id ? (
                <input
                  className="banner-input"
                  aria-label="会议标题"
                  value={editing.title}
                  maxLength={200}
                  autoFocus
                  onChange={(event) => setEditing({ id: s.id, title: event.target.value })}
                  onBlur={commit}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") commit();
                    if (event.key === "Escape") setEditing(null);
                  }}
                />
              ) : (
                <button
                  type="button"
                  className="session-main"
                  disabled={locked && s.state !== "live"}
                  onClick={() => onSelect(s.id)}
                >
                  <span className="session-title">{sessionTitle(s)}</span>
                  <span className={`badge badge-session-${s.state}`}>
                    {s.deletion_pending ? "待删除" : STATE_LABELS[s.state]}
                  </span>
                  <span className="session-meta">
                    {formatStartedAt(s.started_at)} · {formatDuration(s.duration_secs)} ·{" "}
                    {s.utterance_count} 条
                  </span>
                  {!s.deletion_pending && previewLine(s) && (
                    <span className="session-preview">{previewLine(s)}</span>
                  )}
                </button>
              )}
              <span className="session-actions">
                <button
                  type="button"
                  className="link"
                  aria-pressed={s.keep}
                  aria-label={`保留：${sessionTitle(s)}`}
                  disabled={s.deletion_pending || keepPending.has(s.id)}
                  title="免于自动清理；仍可人工删除"
                  onClick={() => onKeep(s.id, !s.keep)}
                >
                  {keepPending.has(s.id) ? "保存中…" : s.keep ? "已保留" : "保留"}
                </button>
                {!locked && !s.deletion_pending && (
                  <button
                    type="button"
                    className="link"
                    aria-label={`继续：${sessionTitle(s)}`}
                    title={
                      s.state === "live"
                        ? "这场会议正在另一台设备上进行；点了之后连接会转到这台设备"
                        : undefined
                    }
                    onClick={() => onResume(s.id)}
                  >
                    {s.state === "live" ? "接过来继续" : "继续"}
                  </button>
                )}
                <button
                  type="button"
                  className="link"
                  disabled={s.deletion_pending}
                  aria-label={`重命名：${sessionTitle(s)}`}
                  onClick={() => setEditing({ id: s.id, title: s.title })}
                >
                  重命名
                </button>
                <button
                  type="button"
                  className="link link-danger"
                  aria-label={`删除：${sessionTitle(s)}`}
                  disabled={!canDelete(s)}
                  title={canDelete(s) ? undefined : "会议进行中，请先结束"}
                  onClick={() => {
                    if (
                      window.confirm(
                        `确定删除「${sessionTitle(s)}」吗？它的全部发言、截图和任务记录都会被删除，不能恢复。`,
                      )
                    ) {
                      onDelete(s.id);
                    }
                  }}
                >
                  {s.deletion_pending ? "重试删除" : "删除"}
                </button>
              </span>
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}
