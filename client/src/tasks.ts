// 后台任务面板的纯逻辑（docs/interfaces.md §5.5、§6.1）。不碰 DOM，可以用 npm test 直接测。

export const TASK_STATUSES = ["queued", "running", "succeeded", "failed", "cancelled"] as const;
export type TaskStatus = (typeof TASK_STATUSES)[number];

export const TASK_STATUS_LABELS: Record<TaskStatus, string> = {
  queued: "排队中",
  running: "进行中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

/** 任务面板里的一行。`id` 是完整编号，`label` 是会议内的短编号（t3）。 */
export interface TaskItem {
  id: string;
  label: string;
  goal: string;
  status: TaskStatus;
  brief: string | null;
  error: string | null;
  /** 委托当时的应答模态：文字委托的任务完成后不出声 */
  modality: "voice" | "text";
  created_at: number;
  /** 最近一条进度（任务进行中显示在目标下面） */
  lastStep?: string;
}

export interface TaskEventItem {
  at: number;
  kind: string;
  summary: string;
}

/** 任务详情（GET /api/tasks/{id}）。 */
export interface TaskDetail extends TaskItem {
  started_at: number | null;
  finished_at: number | null;
  detail_md: string;
  sources: string[];
  artifacts: string[];
  requested_by: string;
  requested_t: number;
  events: TaskEventItem[];
  /** 这次任务外发给远端模型的内容 */
  outbound: {
    goal: string;
    t_from: number;
    t_to: number;
    frames: { id: number; t: number }[];
    model_host: string;
  };
}

export function isFinished(status: TaskStatus): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}

function sortTasks(tasks: TaskItem[]): TaskItem[] {
  return tasks.sort((a, b) => a.created_at - b.created_at || a.id.localeCompare(b.id));
}

/** 任务创建或状态变化：有就更新（保留最近一条进度），没有就加进来。按创建时间排。 */
export function applyTask(tasks: readonly TaskItem[], task: TaskItem): TaskItem[] {
  const existing = tasks.find((t) => t.id === task.id);
  const merged: TaskItem = { ...task, lastStep: task.lastStep ?? existing?.lastStep };
  return sortTasks([...tasks.filter((t) => t.id !== task.id), merged]);
}

/** 一条进度：记到对应任务的「最近一步」上。不认识的任务不变。 */
export function applyTaskEvent(
  tasks: readonly TaskItem[],
  taskId: string,
  summary: string,
): TaskItem[] {
  if (!tasks.some((t) => t.id === taskId)) return tasks as TaskItem[];
  return tasks.map((t) => (t.id === taskId ? { ...t, lastStep: summary } : t));
}

/** 用服务端的列表替换（重连、切换会议）：服务端没有「最近一步」，本地已有的留着。 */
export function replaceTasks(tasks: readonly TaskItem[], items: readonly TaskItem[]): TaskItem[] {
  const steps = new Map(tasks.map((t) => [t.id, t.lastStep]));
  return sortTasks(items.map((t) => ({ ...t, lastStep: t.lastStep ?? steps.get(t.id) })));
}

/** 任务行第二行显示什么：做完了是结论，失败 / 取消是原因，进行中是最近一步。 */
export function taskSubline(task: TaskItem): string {
  if (task.status === "succeeded") return task.brief ?? "已完成";
  if (task.status === "failed") return task.error ? `没办成：${task.error}` : "没办成";
  if (task.status === "cancelled") return "已取消";
  if (task.status === "queued") return "排队中，前面的任务做完就开始";
  return task.lastStep ?? "开始处理";
}

/** 产物文件的地址。任务编号和文件名都要编码（文件名可以带子目录）。 */
export function artifactUrl(taskId: string, name: string): string {
  const path = name.split("/").map(encodeURIComponent).join("/");
  return `/api/tasks/${encodeURIComponent(taskId)}/artifacts/${path}`;
}

/** 能直接当图片显示的产物。 */
export function isImageArtifact(name: string): boolean {
  return /\.(png|jpe?g|webp|gif)$/i.test(name);
}

/** 来源链接只认 http(s)：远端模型给的内容不可信，别的协议（javascript: 之类）不做成链接。 */
export function safeLink(source: string): string | null {
  try {
    const url = new URL(source);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

/** 用时：不到一分钟写秒，否则「N 分 SS 秒」。还没结束返回空串。 */
export function taskDuration(startedAt: number | null, finishedAt: number | null): string {
  if (startedAt === null || finishedAt === null) return "";
  const secs = Math.max(0, Math.round(finishedAt - startedAt));
  if (secs < 60) return `${secs} 秒`;
  return `${Math.floor(secs / 60)} 分 ${String(secs % 60).padStart(2, "0")} 秒`;
}
