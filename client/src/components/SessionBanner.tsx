import { useEffect, useState } from "react";

import type { SessionSummary } from "../api.ts";
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
  onRename: (id: string, title: string) => void;
  onResume: (id: string) => void;
  onStart: () => void;
}

/** 字幕区上方的一行：正在看的是哪一场会议、什么状态、开始于何时；标题可以点开改；没在开的会议可以继续。 */
export function SessionBanner({
  session,
  connection,
  reconnectAttempt,
  watching,
  onRename,
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
      <p className="banner banner-empty">
        还没有会议。点右上角的「开始新会议」，说的话会实时显示在下面，并保存在服务端。
      </p>
    );
  }

  const commit = () => {
    if (editing !== null && editing.trim() !== session.title.trim()) onRename(session.id, editing);
    setEditing(null);
  };
  const idle = canResume(connection) && reconnectAttempt === 0;
  const interrupted = session.state === "interrupted" && idle;

  return (
    <div className="banner" role="group" aria-label="当前查看的会议">
      {editing === null ? (
        <>
          <strong className="banner-title">{sessionTitle(session)}</strong>
          <button
            type="button"
            className="link"
            aria-label="重命名这场会议"
            onClick={() => setEditing(session.title)}
          >
            ✎
          </button>
        </>
      ) : (
        <input
          className="banner-input"
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
      <span className={`badge badge-session-${session.state}`}>{STATE_LABELS[session.state]}</span>
      <span className="banner-meta">
        开始于 {formatStartedAt(session.started_at)} · {formatDuration(session.duration_secs)} ·{" "}
        {session.utterance_count} 条发言
      </span>
      <span className="banner-exports">
        导出：
        <a className="link" href={exportUrl(session.id)} download>
          转录
        </a>
        <a className="link" href={exportUrl(session.id, "json")} download>
          JSON
        </a>
        <a
          className="link"
          href={exportUrl(session.id, "zip")}
          download
          title="转录、JSON、报告、全部截图和任务产物，打成一个压缩包"
        >
          完整包
        </a>
      </span>
      {reconnectAttempt > 0 && (
        <span className="banner-hint banner-hint-warn">
          连接断开了，正在自动重连（第 {reconnectAttempt} 次）…
        </span>
      )}
      {watching && (
        <span className="banner-hint">
          正在另一台设备上进行，这里只读，每 3 秒刷新一次。
          <button type="button" className="link" onClick={() => onResume(session.id)}>
            在这台设备上继续
          </button>
        </span>
      )}
      {interrupted && (
        <span className="banner-hint">
          {interruptedSecs(session, now) < 60
            ? "这场会议刚刚中断"
            : `这场会议已中断 ${formatSpan(interruptedSecs(session, now))}`}
          <button
            type="button"
            className="button button-start button-small"
            onClick={() => onResume(session.id)}
          >
            继续
          </button>
          <button type="button" className="link" onClick={onStart}>
            开始新会议
          </button>
        </span>
      )}
      {session.state === "ended" && idle && (
        <button type="button" className="link" onClick={() => onResume(session.id)}>
          继续这场会议
        </button>
      )}
    </div>
  );
}
