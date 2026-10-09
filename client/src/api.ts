// 服务端的 HTTP 接口（docs/interfaces.md §5.1、§5.4）。页面一打开就用它们取会议列表和最近的对话，
// 不需要音频连接。错误统一是 {"error": "<中文说明>"}，这里转成 ApiError。

import type { HistoryItem } from "./captions.ts";
import type { ReportInfo } from "./report.ts";
import type { FrameItem } from "./screenCapture.ts";
import type { TaskDetail, TaskItem } from "./tasks.ts";

export type SessionState = "live" | "interrupted" | "ended";

export interface SessionSummary {
  id: string;
  title: string;
  started_at: number;
  ended_at: number | null;
  last_active_at: number;
  state: SessionState;
  /** 各次连接实际时长之和，不含中断的空档 */
  duration_secs: number;
  utterance_count: number;
  speakers: string[];
  preview: { speaker: string; text: string }[];
}

export interface ConnectionSpan {
  connected_at: number;
  disconnected_at: number | null;
  t_from: number;
  t_to: number | null;
}

export interface SessionDetail extends SessionSummary {
  screen: Record<string, unknown>;
  /** 配置里的成员名单，给说话人改名当候选 */
  members: string[];
  connections: ConnectionSpan[];
}

export interface SpeakerInfo {
  idx: number;
  display_name: string;
}

export interface SpeakerMerge {
  from: number;
  into: number;
  display_name: string;
  moved: number;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export interface UtteranceQuery {
  /** 最近 N 条（整场的结尾），页面一打开显示「最近对话」用 */
  tail?: number;
  afterId?: number;
  beforeId?: number;
  limit?: number;
}

export interface Api {
  listSessions(limit?: number, before?: number): Promise<SessionSummary[]>;
  /** 当前会话：活动连接所在的会话，否则最近一个未结束的；没有返回 null */
  currentSession(): Promise<SessionDetail | null>;
  getSession(id: string): Promise<SessionDetail>;
  renameSession(id: string, title: string): Promise<SessionSummary>;
  endSession(id: string): Promise<{ id: string; ended_at: number }>;
  deleteSession(id: string): Promise<void>;
  listUtterances(sessionId: string, query?: UtteranceQuery): Promise<HistoryItem[]>;
  listSpeakers(sessionId: string): Promise<SpeakerInfo[]>;
  renameSpeaker(sessionId: string, idx: number, displayName: string): Promise<SpeakerInfo>;
  /** 把说话人 idx 的全部发言并入 into，并删除 idx */
  mergeSpeakers(sessionId: string, idx: number, into: number): Promise<SpeakerMerge>;
  /** 把选中的发言改成另一个说话人：已有的（speakerIdx），或者新建一个（newSpeaker 是名字） */
  assignSpeaker(
    sessionId: string,
    ids: readonly number[],
    target: { speakerIdx: number } | { newSpeaker: string },
  ): Promise<{ speaker: SpeakerInfo; ids: number[] }>;
  /** 服务端时钟（Unix 秒），对时用 */
  serverTime(): Promise<number>;
  listFrames(sessionId: string): Promise<FrameItem[]>;
  /** 上传一张截图；capturedAt 是已经换算成服务端时钟的采集时刻（Unix 秒） */
  uploadFrame(image: Blob, capturedAt: number): Promise<{ id: number; t: number }>;
  listTasks(sessionId: string): Promise<TaskItem[]>;
  getTask(id: string): Promise<TaskDetail>;
  cancelTask(id: string): Promise<TaskItem>;
  /** 最近一份会后报告；还没有返回 null */
  getReport(sessionId: string): Promise<ReportInfo | null>;
  /** 开始生成一份报告（异步）；返回报告编号 */
  startReport(sessionId: string): Promise<number>;
}

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined) search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export function createApi(fetchFn: FetchLike = (input, init) => fetch(input, init)): Api {
  async function request<T>(path: string, init?: RequestInit): Promise<T> {
    let response: Response;
    try {
      response = await fetchFn(path, init);
    } catch {
      throw new ApiError("无法连接到服务端", 0);
    }
    if (!response.ok) {
      let message = `请求失败（${response.status}）`;
      try {
        const body: unknown = await response.json();
        if (typeof body === "object" && body !== null && "error" in body) {
          const text = (body as { error: unknown }).error;
          if (typeof text === "string" && text) message = text;
        }
      } catch {
        // 响应不是 JSON：用上面的通用说明
      }
      throw new ApiError(message, response.status);
    }
    return (await response.json()) as T;
  }

  const json = (method: string, body: unknown): RequestInit => ({
    method,
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  const enc = encodeURIComponent;

  return {
    async listSessions(limit, before) {
      const body = await request<{ items: SessionSummary[] }>(
        `/api/sessions${query({ limit, before })}`,
      );
      return body.items;
    },
    async currentSession() {
      try {
        return await request<SessionDetail>("/api/session");
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }
    },
    getSession: (id) => request<SessionDetail>(`/api/sessions/${enc(id)}`),
    renameSession: (id, title) =>
      request<SessionSummary>(`/api/sessions/${enc(id)}`, json("PATCH", { title })),
    endSession: (id) =>
      request<{ id: string; ended_at: number }>(`/api/sessions/${enc(id)}/end`, { method: "POST" }),
    async deleteSession(id) {
      await request<{ id: string }>(`/api/sessions/${enc(id)}`, { method: "DELETE" });
    },
    async listUtterances(sessionId, q = {}) {
      const body = await request<{ items: HistoryItem[] }>(
        `/api/utterances${query({
          session_id: sessionId,
          tail: q.tail,
          after_id: q.afterId,
          before_id: q.beforeId,
          limit: q.limit,
        })}`,
      );
      return body.items;
    },
    async listSpeakers(sessionId) {
      const body = await request<{ items: SpeakerInfo[] }>(
        `/api/speakers${query({ session_id: sessionId })}`,
      );
      return body.items;
    },
    renameSpeaker: (sessionId, idx, displayName) =>
      request<SpeakerInfo>(
        `/api/speakers/${idx}`,
        json("PUT", { session_id: sessionId, display_name: displayName }),
      ),
    mergeSpeakers: (sessionId, idx, into) =>
      request<SpeakerMerge>(
        `/api/speakers/${idx}/merge`,
        json("POST", { session_id: sessionId, into }),
      ),
    assignSpeaker: (sessionId, ids, target) =>
      request<{ speaker: SpeakerInfo; ids: number[] }>(
        "/api/utterances/speaker",
        json("POST", {
          session_id: sessionId,
          ids,
          ...("newSpeaker" in target
            ? { new_speaker: target.newSpeaker }
            : { speaker_idx: target.speakerIdx }),
        }),
      ),
    async serverTime() {
      const body = await request<{ server_time: number }>("/api/time");
      return body.server_time;
    },
    async listFrames(sessionId) {
      const body = await request<{ items: FrameItem[] }>(
        `/api/frames${query({ session_id: sessionId })}`,
      );
      return body.items.map(({ id, t, width, height, caption }) => ({
        id,
        t,
        width,
        height,
        caption: caption ?? null,
      }));
    },
    uploadFrame(image, capturedAt) {
      const form = new FormData();
      form.set("captured_at", String(capturedAt));
      form.set("image", image, image.type === "image/jpeg" ? "frame.jpg" : "frame.webp");
      return request<{ id: number; t: number }>("/api/frames", { method: "POST", body: form });
    },
    async listTasks(sessionId) {
      const body = await request<{ items: TaskItem[] }>(
        `/api/tasks${query({ session_id: sessionId })}`,
      );
      return body.items;
    },
    getTask: (id) => request<TaskDetail>(`/api/tasks/${enc(id)}`),
    cancelTask: (id) => request<TaskItem>(`/api/tasks/${enc(id)}/cancel`, { method: "POST" }),
    async getReport(sessionId) {
      try {
        return await request<ReportInfo>(`/api/sessions/${enc(sessionId)}/report`);
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }
    },
    async startReport(sessionId) {
      const body = await request<{ report_id: number }>(`/api/sessions/${enc(sessionId)}/report`, {
        method: "POST",
      });
      return body.report_id;
    },
  };
}
