// 服务端的 HTTP 接口（docs/interfaces.md §5.1、§5.4）。页面一打开就用它们取会议列表和最近的对话，
// 不需要音频连接。错误统一是 {"error", "code", "params"}（interfaces.md §5.9），这里按界面语言转成 ApiError。
// 启用了访问口令时（§5.7），改动性的请求带上 CSRF 令牌；回 401 说明登录失效，通知页面重新登录。

import { authSession, withCsrf, type AuthSession } from "./auth.ts";
import type { HistoryItem } from "./captions.ts";
import { errorText, messages } from "./i18n/index.ts";
import type { ReportInfo } from "./report.ts";
import type { FrameItem } from "./screenCapture.ts";
import type { TaskDetail, TaskItem } from "./tasks.ts";

export type SessionState = "live" | "interrupted" | "ended";

export interface SessionSummary {
  access?: {
    role: "host" | "watcher";
    allow_questions: boolean;
    tool_enabled?: boolean;
    share_expires_at?: number | null;
  } | null;
  id: string;
  title: string;
  keep: boolean;
  deletion_pending: boolean;
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
  /** 这位说话人已采集的声纹语音秒数；没开说话人记忆时没有这一项 */
  voice_secs?: number;
  /** 关联的声纹档案；没开说话人记忆时没有这一项 */
  voice_profile?: { id: number; name: string } | null;
}

/** 声纹档案（docs/interfaces.md §5.14），只有名字和时长，没有向量 */
export interface VoiceProfile {
  id: number;
  name: string;
  samples: number;
  secs: number;
  created_at: number;
  updated_at: number;
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

export interface MeetingMember {
  principal_id: string;
  display_name: string;
  role: "host" | "watcher";
  allow_questions: boolean;
}

/** 会议访问日志的一条（docs/interfaces.md §5.13），新的在前 */
export interface AccessLogEntry {
  id: number;
  at: number;
  principal_id: string;
  display_name: string;
  action: string;
  detail: Record<string, unknown>;
}

export interface Api {
  listMembers(id: string): Promise<MeetingMember[]>;
  accessLog(id: string, beforeId?: number): Promise<AccessLogEntry[]>;
  setMember(
    id: string,
    principal: string,
    role: "host" | "watcher" | null,
    questions?: boolean,
  ): Promise<MeetingMember[]>;
  listSessions(limit?: number, before?: number): Promise<SessionSummary[]>;
  /** 当前会话：最近连上的进行中的会议，否则最近一个未结束的；没有返回 null。
   * 同时进行好几场会议时这只是个默认值：已经知道是哪一场时用 getSession */
  currentSession(): Promise<SessionDetail | null>;
  getSession(id: string): Promise<SessionDetail>;
  renameSession(id: string, title: string): Promise<SessionSummary>;
  keepSession(id: string, keep: boolean): Promise<SessionSummary>;
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
  /** 全部声纹档案，和录入需要的最少语音秒数 */
  listVoiceProfiles(): Promise<{ items: VoiceProfile[]; min_enroll_secs: number }>;
  renameVoiceProfile(id: number, name: string): Promise<VoiceProfile>;
  /** 删除档案；返回一并删掉的声纹片段数 */
  deleteVoiceProfile(id: number): Promise<{ id: number; clips: number }>;
  /** 记住说话人 idx 的声音：新建档案（name），或者录进已有的档案（profileId）。本人须已同意 */
  enrollVoice(
    sessionId: string,
    idx: number,
    target: { name: string } | { profileId: number },
  ): Promise<{ speaker: SpeakerInfo; voice_profile: VoiceProfile }>;
  /** 取消说话人和档案的关联，档案保留 */
  unlinkVoice(sessionId: string, idx: number): Promise<void>;
  /** 服务端时钟（Unix 秒），对时用 */
  serverTime(): Promise<number>;
  /** 建立 WebRTC 连接时用的 ICE 服务器（docs/interfaces.md §5.2），原样交给 SDK */
  iceServers(): Promise<RTCIceServer[]>;
  listFrames(sessionId: string): Promise<FrameItem[]>;
  /** 上传一张截图；capturedAt 是已经换算成服务端时钟的采集时刻（Unix 秒），
   * sessionId 是这个页面连着的会议（同时进行好几场时服务端靠它分辨） */
  uploadFrame(
    image: Blob,
    capturedAt: number,
    sessionId: string | null,
  ): Promise<{ id: number; t: number }>;
  listTasks(sessionId: string): Promise<TaskItem[]>;
  /** 旁观一场会议的事件流地址（interfaces.md §5.1），给 EventSource 用 */
  eventsUrl(sessionId: string): string;
  /** 旁观时打字提问（interfaces.md §5.1）；notice 是会议页面本来会收到的提示，原样交给 parseServerMessage */
  sendText(sessionId: string, text: string): Promise<{ accepted: boolean; notice: unknown }>;
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

export function createApi(
  fetchFn: FetchLike = (input, init) => fetch(input, init),
  session: AuthSession = authSession,
): Api {
  async function request<T>(path: string, init?: RequestInit): Promise<T> {
    let response: Response;
    try {
      response = await fetchFn(path, withCsrf(init, session.csrfToken));
    } catch {
      throw new ApiError(messages().notices.unreachable, 0);
    }
    if (response.status === 401) session.notifyUnauthorized();
    if (!response.ok) {
      let message = messages().notices.requestFailed(response.status);
      try {
        message = errorText(await response.json()) ?? message;
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
    async listMembers(id) {
      return (await request<{ items: MeetingMember[] }>(`/api/sessions/${enc(id)}/members`)).items;
    },
    async accessLog(id, beforeId) {
      const body = await request<{ items: AccessLogEntry[] }>(
        `/api/sessions/${enc(id)}/access-log${query({ limit: 100, before_id: beforeId })}`,
      );
      return body.items;
    },
    async setMember(id, principal, role, questions = false) {
      return (
        await request<{ items: MeetingMember[] }>(
          `/api/sessions/${enc(id)}/members/${enc(principal)}`,
          role === null ? { method: "DELETE" } : json("PUT", { role, allow_questions: questions }),
        )
      ).items;
    },
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
    keepSession: (id, keep) =>
      request<SessionSummary>(`/api/sessions/${enc(id)}`, json("PATCH", { keep })),
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
    listVoiceProfiles: () =>
      request<{ items: VoiceProfile[]; min_enroll_secs: number }>("/api/voice-profiles"),
    renameVoiceProfile: (id, name) =>
      request<VoiceProfile>(`/api/voice-profiles/${id}`, json("PATCH", { name })),
    deleteVoiceProfile: (id) =>
      request<{ id: number; clips: number }>(`/api/voice-profiles/${id}`, { method: "DELETE" }),
    enrollVoice: (sessionId, idx, target) =>
      request<{ speaker: SpeakerInfo; voice_profile: VoiceProfile }>(
        `/api/speakers/${idx}/voice-profile`,
        json("POST", {
          session_id: sessionId,
          consent: true,
          ...("profileId" in target ? { profile_id: target.profileId } : { name: target.name }),
        }),
      ),
    async unlinkVoice(sessionId, idx) {
      await request<unknown>(
        `/api/speakers/${idx}/voice-profile${query({ session_id: sessionId })}`,
        {
          method: "DELETE",
        },
      );
    },
    async serverTime() {
      const body = await request<{ server_time: number }>("/api/time");
      return body.server_time;
    },
    async iceServers() {
      const body = await request<{ ice_servers: RTCIceServer[] }>("/api/ice");
      return body.ice_servers;
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
    uploadFrame(image, capturedAt, sessionId) {
      const form = new FormData();
      form.set("captured_at", String(capturedAt));
      if (sessionId) form.set("session_id", sessionId);
      form.set("image", image, image.type === "image/jpeg" ? "frame.jpg" : "frame.webp");
      return request<{ id: number; t: number }>("/api/frames", { method: "POST", body: form });
    },
    async listTasks(sessionId) {
      const body = await request<{ items: TaskItem[] }>(
        `/api/tasks${query({ session_id: sessionId })}`,
      );
      return body.items;
    },
    eventsUrl: (sessionId) => `/api/sessions/${enc(sessionId)}/events`,
    sendText: (sessionId, text) =>
      request<{ accepted: boolean; notice: unknown }>(
        `/api/sessions/${enc(sessionId)}/text`,
        json("POST", { text }),
      ),
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
