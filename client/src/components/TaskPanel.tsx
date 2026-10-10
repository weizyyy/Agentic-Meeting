import { useEffect, useState } from "react";

import { formatFrameTime, frameImageUrl } from "../screenCapture.ts";
import {
  TASK_STATUS_LABELS,
  artifactUrl,
  isFinished,
  isImageArtifact,
  safeLink,
  taskDuration,
  taskSubline,
  type TaskDetail,
  type TaskItem,
} from "../tasks.ts";

interface Props {
  tasks: readonly TaskItem[];
  onLoad: (id: string) => Promise<TaskDetail | null>;
  onCancel: (id: string) => void;
}

/** 后台任务面板：列表（状态、最近一步或结论），点开看详细结果、来源、产物、进度，以及这次任务外发了什么。 */
export function TaskPanel({ tasks, onLoad, onCancel }: Props) {
  const [openId, setOpenId] = useState<string | null>(null);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const open = openId === null ? null : (tasks.find((t) => t.id === openId) ?? null);

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

  useEffect(() => {
    if (openId === null) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpenId(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openId]);

  if (tasks.length === 0) return null; // 没有交办过任务时不占地方

  return (
    <section className="panel tasks-panel" aria-label="后台任务">
      <h2>后台任务</h2>
      <ul className="tasks">
        {[...tasks].reverse().map((task) => (
          <li key={task.id}>
            <button type="button" className="task" onClick={() => setOpenId(task.id)}>
              <span className="task-head">
                <span className="task-label">{task.label}</span>
                <span className={`badge badge-task-${task.status}`}>
                  {TASK_STATUS_LABELS[task.status]}
                </span>
                {task.modality === "text" && <span className="tag">文字</span>}
              </span>
              <span className="task-goal">{task.goal}</span>
              <span className="task-sub">{taskSubline(task)}</span>
            </button>
          </li>
        ))}
      </ul>
      {open && (
        <div
          className="lightbox"
          role="dialog"
          aria-modal="true"
          aria-label={`任务 ${open.label}`}
          onClick={() => setOpenId(null)}
        >
          <article className="task-detail" onClick={(event) => event.stopPropagation()}>
            <header>
              <h3>
                {open.label}{" "}
                <span className={`badge badge-task-${open.status}`}>
                  {TASK_STATUS_LABELS[open.status]}
                </span>
              </h3>
              <span className="spacer" />
              {!isFinished(open.status) && (
                <button
                  type="button"
                  className="link link-danger"
                  onClick={() => onCancel(open.id)}
                >
                  取消任务
                </button>
              )}
              <button type="button" className="link" onClick={() => setOpenId(null)}>
                关闭
              </button>
            </header>
            <p className="task-detail-goal">{open.goal}</p>
            {open.status === "succeeded" && open.brief && (
              <p className="task-detail-brief">{open.brief}</p>
            )}
            {(open.status === "failed" || open.status === "cancelled") && open.error && (
              <p className="notice notice-error">{open.error}</p>
            )}
            {detail === null || detail.id !== open.id ? (
              <p className="empty">正在读取…</p>
            ) : (
              <TaskBody detail={detail} />
            )}
          </article>
        </div>
      )}
    </section>
  );
}

function TaskBody({ detail }: { detail: TaskDetail }) {
  const duration = taskDuration(detail.started_at, detail.finished_at);
  const images = detail.artifacts.filter(isImageArtifact);
  const files = detail.artifacts.filter((name) => !isImageArtifact(name));
  return (
    <>
      {detail.detail_md && (
        <>
          <h4>详细结果</h4>
          {/* 远端模型写的内容按纯文字显示，不当 HTML 解释 */}
          <pre className="task-markdown">{detail.detail_md}</pre>
        </>
      )}
      {images.length > 0 && (
        <div className="task-images">
          {images.map((name) => (
            <a key={name} href={artifactUrl(detail.id, name)} target="_blank" rel="noreferrer">
              <img src={artifactUrl(detail.id, name)} alt={name} loading="lazy" />
            </a>
          ))}
        </div>
      )}
      {files.length > 0 && (
        <>
          <h4>产物文件</h4>
          <ul className="task-list">
            {files.map((name) => (
              <li key={name}>
                <a className="link" href={artifactUrl(detail.id, name)} download>
                  {name}
                </a>
              </li>
            ))}
          </ul>
        </>
      )}
      {detail.sources.length > 0 && (
        <>
          <h4>来源</h4>
          <ul className="task-list">
            {detail.sources.map((source) => {
              const href = safeLink(source);
              return (
                <li key={source}>
                  {href ? (
                    <a className="link" href={href} target="_blank" rel="noreferrer">
                      {source}
                    </a>
                  ) : (
                    source
                  )}
                </li>
              );
            })}
          </ul>
        </>
      )}
      <h4>进度{duration && `（用时 ${duration}）`}</h4>
      <ol className="task-events">
        {detail.events.map((event, index) => (
          <li key={index} className={`task-event task-event-${event.kind}`}>
            {event.summary}
          </li>
        ))}
      </ol>
      <h4>这次任务外发的内容</h4>
      <p className="task-outbound">
        {detail.outbound.model_host
          ? `发给了 ${detail.outbound.model_host} 上的模型：`
          : "发给了后台模型："}
        任务目标（上面那段话）、{formatFrameTime(detail.outbound.t_from)} 到{" "}
        {formatFrameTime(detail.outbound.t_to)} 的会议转录
        {detail.outbound.frames.length > 0
          ? `，以及下面 ${detail.outbound.frames.length} 张屏幕截图。`
          : "，没有带屏幕截图。"}
        {` 交办人：${detail.requested_by}。`}
      </p>
      {detail.outbound.frames.length > 0 && (
        <div className="task-images task-images-small">
          {detail.outbound.frames.map((frame) => (
            <a key={frame.id} href={frameImageUrl(frame.id)} target="_blank" rel="noreferrer">
              <img
                src={frameImageUrl(frame.id)}
                alt={`${formatFrameTime(frame.t)} 的屏幕截图`}
                loading="lazy"
              />
            </a>
          ))}
        </div>
      )}
    </>
  );
}
