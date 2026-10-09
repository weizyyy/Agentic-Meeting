// 服务端 → 浏览器的自定义消息（docs/interfaces.md §6.1）。
// 目前处理：caption、utterance、utterance_update、speaker、speakers_merged、session、session_closed、
// assistant_state、notice、
// frame、frame_caption、task、task_event；不认识的类型收到时忽略。

import { TASK_STATUSES, type TaskStatus } from "./tasks.ts";

export interface CaptionMessage {
  type: "caption";
  segment_id: number;
  speaker_idx: number;
  speaker_name: string;
  t_start: number;
  stable: string;
  unstable: string;
}

export const ASSISTANT_STATES = ["idle", "listening", "thinking", "speaking"] as const;
export type AssistantStateName = (typeof ASSISTANT_STATES)[number];

export interface AssistantStateMessage {
  type: "assistant_state";
  state: AssistantStateName;
}

export const NOTICE_LEVELS = ["info", "warn", "error"] as const;
export type NoticeLevel = (typeof NOTICE_LEVELS)[number];

export interface NoticeMessage {
  type: "notice";
  level: NoticeLevel;
  text: string;
}

export const UTTERANCE_SOURCES = ["asr", "assistant", "text"] as const;
export type UtteranceSource = (typeof UTTERANCE_SOURCES)[number];

/** 一条发言定稿。`id` 为 null 表示落库失败（服务端会重试），`segment_id` 为 null 表示不对应某行实时字幕（助理的话、键入的文字）。 */
export interface UtteranceMessage {
  type: "utterance";
  id: number | null;
  segment_id: number | null;
  speaker_idx: number;
  speaker_name: string;
  t_start: number;
  t_end: number;
  text: string;
  source: UtteranceSource;
}

/** 说话人更正：这条发言事后被认定为另一个人说的。 */
export interface UtteranceUpdateMessage {
  type: "utterance_update";
  id: number;
  speaker_idx: number;
  speaker_name: string;
}

/** 说话人改名。 */
export interface SpeakerMessage {
  type: "speaker";
  idx: number;
  display_name: string;
}

/** 说话人 `from` 并入了 `into`；`display_name` 是 `into` 的显示名。 */
export interface SpeakersMergedMessage {
  type: "speakers_merged";
  from: number;
  into: number;
  display_name: string;
}

/** 连接建立后服务端告诉页面：这路连接挂在哪个会话上。 */
export interface SessionMessage {
  type: "session";
  id: string;
  title: string;
  started_at: number;
  resumed: boolean;
  base_secs: number;
  state: string;
}

export const SESSION_CLOSED_REASONS = ["taken_over", "ended", "server_stopping"] as const;
export type SessionClosedReason = (typeof SESSION_CLOSED_REASONS)[number];

export interface SessionClosedMessage {
  type: "session_closed";
  reason: SessionClosedReason;
}

/** 新截图进了时间线。 */
export interface FrameMessage {
  type: "frame";
  id: number;
  t: number;
  width: number;
  height: number;
}

/** 某张截图的画面摘要生成好了。 */
export interface FrameCaptionMessage {
  type: "frame_caption";
  id: number;
  caption: string;
}

/** 后台任务创建或状态变化。`id` 是完整编号，`label` 是会议内的短编号。 */
export interface TaskMessage {
  type: "task";
  id: string;
  label: string;
  goal: string;
  status: TaskStatus;
  brief: string | null;
  error: string | null;
  modality: "voice" | "text";
  created_at: number;
}

/** 后台任务的一条进度。 */
export interface TaskEventMessage {
  type: "task_event";
  task_id: string;
  at: number;
  kind: string;
  summary: string;
}

export type ServerMessage =
  | CaptionMessage
  | TaskMessage
  | TaskEventMessage
  | FrameMessage
  | FrameCaptionMessage
  | UtteranceMessage
  | UtteranceUpdateMessage
  | SpeakerMessage
  | SpeakersMergedMessage
  | SessionMessage
  | SessionClosedMessage
  | AssistantStateMessage
  | NoticeMessage;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** 校验并收窄服务端发来的消息；格式不对或类型不认识返回 null（不抛异常，字幕不能因为一条坏消息停下）。 */
/**
 * RTVI 的 error 消息（`{error, fatal}`）要不要显示给用户：只显示致命的。
 * 不致命的（某个服务暂时不可用）由服务端另发中文的 notice，这里返回 null。
 */
export function fatalErrorText(data: unknown): string | null {
  if (typeof data !== "object" || data === null) return null;
  const { error, fatal } = data as { error?: unknown; fatal?: unknown };
  if (fatal !== true) return null;
  return typeof error === "string" && error.trim() ? `服务端出错：${error}` : "服务端出错，连接可能已经中断";
}

export function parseServerMessage(data: unknown): ServerMessage | null {
  if (!isRecord(data)) return null;
  switch (data.type) {
    case "caption": {
      const { segment_id, speaker_idx, speaker_name, t_start, stable, unstable } = data;
      if (
        typeof segment_id === "number" &&
        typeof speaker_idx === "number" &&
        typeof speaker_name === "string" &&
        typeof t_start === "number" &&
        typeof stable === "string" &&
        typeof unstable === "string"
      ) {
        return { type: "caption", segment_id, speaker_idx, speaker_name, t_start, stable, unstable };
      }
      return null;
    }
    case "assistant_state": {
      const state = ASSISTANT_STATES.find((s) => s === data.state);
      return state ? { type: "assistant_state", state } : null;
    }
    case "notice": {
      const level = NOTICE_LEVELS.find((l) => l === data.level);
      if (level && typeof data.text === "string") return { type: "notice", level, text: data.text };
      return null;
    }
    case "utterance": {
      const source = UTTERANCE_SOURCES.find((x) => x === data.source);
      const { id, segment_id, speaker_idx, speaker_name, t_start, t_end, text } = data;
      if (
        source &&
        (id === null || typeof id === "number") &&
        (segment_id === null || typeof segment_id === "number") &&
        typeof speaker_idx === "number" &&
        typeof speaker_name === "string" &&
        typeof t_start === "number" &&
        typeof t_end === "number" &&
        typeof text === "string"
      ) {
        return {
          type: "utterance",
          id,
          segment_id,
          speaker_idx,
          speaker_name,
          t_start,
          t_end,
          text,
          source,
        };
      }
      return null;
    }
    case "utterance_update": {
      const { id, speaker_idx, speaker_name } = data;
      if (
        typeof id === "number" &&
        typeof speaker_idx === "number" &&
        typeof speaker_name === "string"
      ) {
        return { type: "utterance_update", id, speaker_idx, speaker_name };
      }
      return null;
    }
    case "speaker": {
      const { idx, display_name } = data;
      if (typeof idx === "number" && typeof display_name === "string") {
        return { type: "speaker", idx, display_name };
      }
      return null;
    }
    case "speakers_merged": {
      const { from, into, display_name } = data;
      if (typeof from === "number" && typeof into === "number" && typeof display_name === "string") {
        return { type: "speakers_merged", from, into, display_name };
      }
      return null;
    }
    case "session": {
      const { id, title, started_at, resumed, base_secs, state } = data;
      if (
        typeof id === "string" &&
        typeof title === "string" &&
        typeof started_at === "number" &&
        typeof resumed === "boolean" &&
        typeof base_secs === "number" &&
        typeof state === "string"
      ) {
        return { type: "session", id, title, started_at, resumed, base_secs, state };
      }
      return null;
    }
    case "session_closed": {
      const reason = SESSION_CLOSED_REASONS.find((r) => r === data.reason);
      return reason ? { type: "session_closed", reason } : null;
    }
    case "frame": {
      const { id, t, width, height } = data;
      if (
        typeof id === "number" &&
        typeof t === "number" &&
        typeof width === "number" &&
        typeof height === "number"
      ) {
        return { type: "frame", id, t, width, height };
      }
      return null;
    }
    case "task": {
      const status = TASK_STATUSES.find((s) => s === data.status);
      const { id, label, goal, brief, error, modality, created_at } = data;
      if (
        status &&
        typeof id === "string" &&
        typeof label === "string" &&
        typeof goal === "string" &&
        (brief === null || brief === undefined || typeof brief === "string") &&
        (error === null || error === undefined || typeof error === "string") &&
        typeof created_at === "number"
      ) {
        return {
          type: "task",
          id,
          label,
          goal,
          status,
          brief: brief ?? null,
          error: error ?? null,
          modality: modality === "text" ? "text" : "voice",
          created_at,
        };
      }
      return null;
    }
    case "task_event": {
      const { task_id, at, kind, summary } = data;
      if (
        typeof task_id === "string" &&
        typeof at === "number" &&
        typeof kind === "string" &&
        typeof summary === "string"
      ) {
        return { type: "task_event", task_id, at, kind, summary };
      }
      return null;
    }
    case "frame_caption": {
      const { id, caption } = data;
      if (typeof id === "number" && typeof caption === "string") {
        return { type: "frame_caption", id, caption };
      }
      return null;
    }
    default:
      return null;
  }
}
