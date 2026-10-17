import type { SessionDetail, SpeakerInfo } from "./api.ts";
import type { HistoryItem } from "./captions.ts";
import type { AssistantAnswerMessage, AssistantStateName, CaptionMessage } from "./protocol.ts";
import type { FrameItem } from "./screenCapture.ts";
import type { TaskDetail } from "./tasks.ts";

export interface MeetingSnapshot {
  session: SessionDetail;
  utterances: HistoryItem[];
  speakers: SpeakerInfo[];
  frames: FrameItem[];
  tasks: TaskDetail[];
  transient: {
    captions: CaptionMessage[];
    answer: AssistantAnswerMessage | null;
    assistant_state: AssistantStateName;
    speaking: boolean;
  };
}

export interface MeetingEnvelope {
  session_id: string;
  epoch: string;
  seq: number;
  data: Record<string, unknown>;
}

/** 网络边界只接受当前会议的完整事件信封；业务消息继续用共享协议校验。 */
export function parseMeetingEnvelope(raw: string, sessionId: string): MeetingEnvelope | null {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof value !== "object" || value === null) return null;
  const event = value as MeetingEnvelope;
  return event.session_id === sessionId &&
    typeof event.epoch === "string" &&
    Number.isSafeInteger(event.seq) &&
    event.seq >= 0 &&
    typeof event.data === "object" &&
    event.data !== null
    ? event
    : null;
}

/** 快照来自同一详情/列表接口；完整面板到齐才替换本地状态。 */
export function parseMeetingSnapshot(event: MeetingEnvelope): MeetingSnapshot | null {
  const data = event.data as unknown as MeetingSnapshot;
  return data.session?.id === event.session_id &&
    Array.isArray(data.utterances) &&
    Array.isArray(data.speakers) &&
    Array.isArray(data.frames) &&
    Array.isArray(data.tasks) &&
    Array.isArray(data.transient?.captions) &&
    typeof data.transient.assistant_state === "string" &&
    typeof data.transient.speaking === "boolean"
    ? data
    : null;
}
