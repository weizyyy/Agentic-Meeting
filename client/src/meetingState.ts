import type { ConnectionSpan, SessionDetail, SessionSummary, SpeakerInfo } from "./api.ts";
import {
  appendHistory,
  applyCaption,
  applySpeakerMerge,
  applySpeakerRename,
  applyUtterance,
  applyUtteranceUpdate,
  prependHistory,
  replaceWithHistory,
  type CaptionLine,
  type HistoryItem,
} from "./captions.ts";
import type {
  AssistantStateName,
  CaptionMessage,
  NoticeLevel,
  SessionClosedMessage,
  SessionMessage,
  UtteranceMessage,
  UtteranceUpdateMessage,
} from "./protocol.ts";
import {
  DEFAULT_SCREEN_CONFIG,
  applyFrame,
  applyFrameCaption,
  mergeFrames,
  parseScreenConfig,
  replaceFrames,
  type FrameItem,
  type ScreenConfig,
} from "./screenCapture.ts";
import type { ReportInfo } from "./report.ts";
import { summaryFromSessionMessage } from "./sessionView.ts";
import { applyTask, applyTaskEvent, replaceTasks, type TaskItem } from "./tasks.ts";

export type ConnectionState = "disconnected" | "connecting" | "connected" | "error";

export interface Notice {
  id: number;
  level: NoticeLevel;
  text: string;
}

export interface MeetingState {
  connection: ConnectionState;
  /** 字幕区的行：已落库的发言（定稿）和还在说的实时字幕 */
  captions: CaptionLine[];
  /** 助理这一轮应答的流式文字 */
  assistantText: string;
  assistantState: AssistantStateName;
  notices: Notice[];
  nextNoticeId: number;
  /** 字幕区正在显示哪一场会议；还没有任何会议时为 null */
  viewing: SessionSummary | null;
  /** 连接期间：正在进行的那场会议的编号 */
  liveSessionId: string | null;
  /** 会议列表（最近的在前） */
  sessions: SessionSummary[];
  /** 正在显示的那场会议里出现过的说话人 */
  speakers: SpeakerInfo[];
  /** 配置里的成员名单，给说话人改名当候选 */
  members: string[];
  /** 还有更早的发言可以向上加载 */
  hasOlder: boolean;
  /** 助理这一轮是怎么回答的：文字请求只出文字，语音唤醒是文字加朗读；还没有应答时为 null */
  replyMode: ReplyMode | null;
  /** 正在显示的那场会议的截图时间线（按时间排） */
  frames: FrameItem[];
  /** 正在显示的那场会议的后台任务（按创建时间排） */
  tasks: TaskItem[];
  /** 服务端下发的截图参数 */
  screen: ScreenConfig;
  /** 正在共享屏幕 */
  sharing: boolean;
  /** 正在显示的那场会议的各次连接（用来画「中断了多久」的分隔线） */
  connections: ConnectionSpan[];
  /** 服务端在断开这路连接之前说明的原因；新一次连接开始时清掉 */
  closedReason: SessionClosedMessage["reason"] | null;
  /** 连接意外断开后正在自动重连：这是第几次；0 表示没有在重连 */
  reconnectAttempt: number;
  /** 正在显示的那场会议最近的一份会后报告；没有是 null */
  report: ReportInfo | null;
}

export type ReplyMode = "text" | "voice";

export const MAX_NOTICES = 5;
/** 页面一打开显示最近多少条发言，以及每次向上翻页加载多少条。 */
export const PAGE_SIZE = 50;

export const initialState: MeetingState = {
  connection: "disconnected",
  captions: [],
  assistantText: "",
  assistantState: "idle",
  notices: [],
  nextNoticeId: 1,
  viewing: null,
  liveSessionId: null,
  sessions: [],
  speakers: [],
  members: [],
  hasOlder: false,
  replyMode: null,
  frames: [],
  tasks: [],
  screen: DEFAULT_SCREEN_CONFIG,
  sharing: false,
  connections: [],
  closedReason: null,
  reconnectAttempt: 0,
  report: null,
};

export type Action =
  | { type: "connecting" }
  | { type: "transport"; transport: string }
  | { type: "caption"; message: CaptionMessage }
  | { type: "utterance"; message: UtteranceMessage }
  | { type: "utteranceUpdate"; message: UtteranceUpdateMessage }
  | { type: "speakerRenamed"; idx: number; displayName: string }
  | { type: "speakersMerged"; from: number; into: number; displayName: string }
  | { type: "reconnecting"; attempt: number }
  | { type: "reconnectStopped" }
  | { type: "detailRefreshed"; detail: SessionDetail }
  | { type: "reportLoaded"; sessionId: string; report: ReportInfo | null }
  | { type: "sessionStarted"; message: SessionMessage }
  | { type: "sessionClosed"; message: SessionClosedMessage }
  | { type: "assistantState"; state: AssistantStateName }
  | { type: "textSent" }
  | { type: "llmStarted" }
  | { type: "llmText"; text: string }
  | { type: "notice"; level: NoticeLevel; text: string }
  | { type: "dismissNotice"; id: number }
  | { type: "sessionsLoaded"; items: SessionSummary[] }
  | {
      type: "viewLoaded";
      detail: SessionDetail | null;
      items: HistoryItem[];
      speakers: SpeakerInfo[];
    }
  | { type: "olderLoaded"; items: HistoryItem[] }
  | { type: "backfilled"; items: HistoryItem[] }
  | { type: "speakersLoaded"; speakers: SpeakerInfo[] }
  | { type: "sessionRenamed"; summary: SessionSummary }
  | { type: "sessionRemoved"; id: string }
  | { type: "frame"; frame: Omit<FrameItem, "caption"> }
  | { type: "frameCaption"; id: number; caption: string }
  | { type: "framesLoaded"; sessionId: string; items: FrameItem[]; merge: boolean }
  | { type: "sharing"; sharing: boolean }
  | { type: "task"; task: TaskItem }
  | { type: "taskEvent"; taskId: string; summary: string }
  | { type: "tasksLoaded"; sessionId: string; items: TaskItem[] };

/** SDK 的传输层状态 → 界面上的连接状态。"ready" 才是服务端的管线已经就绪。 */
export function connectionOf(transport: string): ConnectionState {
  switch (transport) {
    case "ready":
      return "connected";
    case "error":
      return "error";
    case "disconnected":
    case "disconnecting":
      return "disconnected";
    default:
      // initializing、initialized、authenticating、authenticated、connecting、connected
      return "connecting";
  }
}

export const SESSION_CLOSED_TEXT: Record<SessionClosedMessage["reason"], string> = {
  taken_over:
    "已在另一台设备（或另一个页面）上继续这场会议，这个页面已断开。同一时刻只能有一路连接。",
  ended: "会议已结束。",
  server_stopping: "服务端正在停止，连接已断开；它重新起来之后会自动接着开这场会议。",
};

function pushNotice(state: MeetingState, level: NoticeLevel, text: string): MeetingState {
  const notice: Notice = { id: state.nextNoticeId, level, text };
  return {
    ...state,
    notices: [...state.notices, notice].slice(-MAX_NOTICES),
    nextNoticeId: state.nextNoticeId + 1,
  };
}

/** 发言里出现了新的说话人：加进说话人列表（改名面板要用）。 */
function withSpeaker(speakers: SpeakerInfo[], idx: number, name: string): SpeakerInfo[] {
  const known = speakers.some((s) => s.idx === idx);
  if (known) return speakers.map((s) => (s.idx === idx ? { ...s, display_name: name } : s));
  return [...speakers, { idx, display_name: name }].sort((a, b) => a.idx - b.idx);
}

export function reduce(state: MeetingState, action: Action): MeetingState {
  switch (action.type) {
    case "connecting":
      return { ...state, connection: "connecting", closedReason: null };
    case "reconnecting":
      return { ...state, reconnectAttempt: action.attempt };
    case "reportLoaded":
      if (state.viewing?.id !== action.sessionId) return state; // 取回来时已经切到别的会议了
      return { ...state, report: action.report };
    case "reconnectStopped":
      return { ...state, reconnectAttempt: 0 };
    case "detailRefreshed": {
      // 轮询或重连之后拿到的最新会话信息：只更新正在显示的这一场
      if (state.viewing?.id !== action.detail.id) return state;
      return {
        ...state,
        viewing: action.detail,
        connections: action.detail.connections ?? state.connections,
        sessions: state.sessions.map((s) =>
          s.id === action.detail.id ? { ...s, state: action.detail.state } : s,
        ),
      };
    }
    case "speakersMerged": {
      const merge = { from: action.from, into: action.into, display_name: action.displayName };
      return {
        ...state,
        captions: applySpeakerMerge(state.captions, merge),
        speakers: withSpeaker(
          state.speakers.filter((s) => s.idx !== action.from),
          action.into,
          action.displayName,
        ),
      };
    }
    case "transport": {
      const connection = connectionOf(action.transport);
      // 断开后不保留「助理正在说话」之类的状态；字幕和助理的最后一轮话保留，会后还能翻看。
      const reset = connection === "disconnected" || connection === "error";
      return {
        ...state,
        connection,
        assistantState: reset ? "idle" : state.assistantState,
        liveSessionId: reset ? null : state.liveSessionId,
        reconnectAttempt: connection === "connected" ? 0 : state.reconnectAttempt,
        sharing: reset ? false : state.sharing,
      };
    }
    case "caption":
      return { ...state, captions: applyCaption(state.captions, action.message) };
    case "utterance": {
      const m = action.message;
      return {
        ...state,
        captions: applyUtterance(state.captions, m),
        speakers: withSpeaker(state.speakers, m.speaker_idx, m.speaker_name),
      };
    }
    case "utteranceUpdate":
      return {
        ...state,
        captions: applyUtteranceUpdate(state.captions, action.message),
        speakers: withSpeaker(
          state.speakers,
          action.message.speaker_idx,
          action.message.speaker_name,
        ),
      };
    case "speakerRenamed":
      return {
        ...state,
        captions: applySpeakerRename(state.captions, {
          idx: action.idx,
          display_name: action.displayName,
        }),
        speakers: withSpeaker(state.speakers, action.idx, action.displayName),
      };
    case "sessionStarted": {
      // 新一场（或继续的一场）会议的连接建立：字幕区切到它。内容随后由 HTTP 补全。
      const switching = state.viewing?.id !== action.message.id;
      return {
        ...state,
        liveSessionId: action.message.id,
        viewing: switching ? summaryFromSessionMessage(action.message) : state.viewing,
        captions: switching ? [] : state.captions,
        speakers: switching ? [] : state.speakers,
        hasOlder: switching ? false : state.hasOlder,
        frames: switching ? [] : state.frames,
        tasks: switching ? [] : state.tasks,
        connections: switching ? [] : state.connections,
        report: switching ? null : state.report,
      };
    }
    case "sessionClosed":
      return {
        ...pushNotice(state, "warn", SESSION_CLOSED_TEXT[action.message.reason]),
        closedReason: action.message.reason,
      };
    case "assistantState":
      // 被叫到名字（在听）或开始朗读，说明这一轮是语音应答
      return {
        ...state,
        assistantState: action.state,
        replyMode:
          action.state === "listening" || action.state === "speaking" ? "voice" : state.replyMode,
      };
    case "textSent":
      return { ...state, replyMode: "text" };
    case "llmStarted":
      return { ...state, assistantText: "", assistantState: "thinking" };
    case "llmText":
      return { ...state, assistantText: state.assistantText + action.text };
    case "notice":
      return pushNotice(state, action.level, action.text);
    case "dismissNotice":
      return { ...state, notices: state.notices.filter((n) => n.id !== action.id) };
    case "sessionsLoaded":
      return { ...state, sessions: action.items };
    case "viewLoaded":
      return {
        ...state,
        viewing: action.detail,
        captions: replaceWithHistory(action.items),
        speakers: action.speakers,
        members: action.detail?.members ?? state.members,
        hasOlder: action.items.length >= PAGE_SIZE,
        // 换了一场会议：时间线先清空，随后由 framesLoaded 填上
        frames: action.detail?.id === state.viewing?.id ? state.frames : [],
        tasks: action.detail?.id === state.viewing?.id ? state.tasks : [],
        screen: action.detail ? parseScreenConfig(action.detail.screen) : state.screen,
        connections: action.detail?.connections ?? [],
        report: action.detail?.id === state.viewing?.id ? state.report : null,
      };
    case "olderLoaded":
      return {
        ...state,
        captions: prependHistory(state.captions, action.items),
        hasOlder: action.items.length >= PAGE_SIZE,
      };
    case "backfilled":
      return { ...state, captions: appendHistory(state.captions, action.items) };
    case "speakersLoaded":
      return { ...state, speakers: action.speakers };
    case "sessionRenamed":
      return {
        ...state,
        viewing: state.viewing?.id === action.summary.id ? action.summary : state.viewing,
        sessions: state.sessions.map((s) => (s.id === action.summary.id ? action.summary : s)),
      };
    case "sessionRemoved": {
      const viewingIt = state.viewing?.id === action.id;
      return {
        ...state,
        sessions: state.sessions.filter((s) => s.id !== action.id),
        viewing: viewingIt ? null : state.viewing,
        captions: viewingIt ? [] : state.captions,
        speakers: viewingIt ? [] : state.speakers,
        hasOlder: viewingIt ? false : state.hasOlder,
        frames: viewingIt ? [] : state.frames,
        tasks: viewingIt ? [] : state.tasks,
        connections: viewingIt ? [] : state.connections,
        report: viewingIt ? null : state.report,
      };
    }
    case "frame":
      // 截图只属于进行中的那场；字幕区在看别的会议时不混进去
      if (state.liveSessionId !== null && state.viewing?.id !== state.liveSessionId) return state;
      return { ...state, frames: applyFrame(state.frames, action.frame) };
    case "task":
      // 任务消息里带着完整编号（会话 id 在点号前面）：只收正在显示的这场会议的
      if (state.viewing && !action.task.id.startsWith(`${state.viewing.id}.`)) return state;
      return { ...state, tasks: applyTask(state.tasks, action.task) };
    case "taskEvent":
      return { ...state, tasks: applyTaskEvent(state.tasks, action.taskId, action.summary) };
    case "tasksLoaded":
      if (state.viewing?.id !== action.sessionId) return state; // 取回来时已经切到别的会议了
      return { ...state, tasks: replaceTasks(state.tasks, action.items) };
    case "frameCaption":
      return { ...state, frames: applyFrameCaption(state.frames, action.id, action.caption) };
    case "framesLoaded":
      if (state.viewing?.id !== action.sessionId) return state; // 取回来时已经切到别的会议了
      return {
        ...state,
        frames: action.merge
          ? mergeFrames(state.frames, action.items)
          : replaceFrames(action.items),
      };
    case "sharing":
      return { ...state, sharing: action.sharing };
  }
}
