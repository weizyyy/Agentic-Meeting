import { PipecatClient } from "@pipecat-ai/client-js";
import { SmallWebRTCTransport } from "@pipecat-ai/small-webrtc-transport";
import {
  useCallback,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type Dispatch,
} from "react";

import { ApiError, createApi, type Api, type SessionDetail } from "./api.ts";
import { backfillAfterId, firstUtteranceId } from "./captions.ts";
import { describeTrackSettings } from "./micLevel.ts";
import { PAGE_SIZE, initialState, reduce, type Action, type MeetingState } from "./meetingState.ts";
import { fatalErrorText, parseServerMessage } from "./protocol.ts";
import { REPORT_POLL_MS } from "./report.ts";
import { reconnectDelayMs, shouldReconnect } from "./resume.ts";
import type { TaskDetail } from "./tasks.ts";
import { validateText } from "./textInput.ts";
import { useScreenShare } from "./useScreenShare.ts";

/** 服务端信令地址（docs/interfaces.md §5.2）。开发时由 Vite 代理到应用。 */
const OFFER_ENDPOINT = "/api/offer";

/** 连接期间多久核对一次「字幕区显示的是不是进行中的这场会议」。 */
const SESSION_CHECK_MS = 5000;

/** 只读地看一场正在别的设备上进行的会议时，多久取一次新发言。 */
const WATCH_POLL_MS = 3000;

/**
 * 保证麦克风开着回声消除：助理朗读的声音会从扬声器传回麦克风，没有回声消除会被识别成有人说话。
 * 浏览器通常默认就是开的；这里检查一次，没开就要求开启。降噪与自动增益保持浏览器默认。
 */
export async function ensureEchoCancellation(track: MediaStreamTrack): Promise<void> {
  try {
    if (track.getSettings().echoCancellation === true) return;
    await track.applyConstraints({ echoCancellation: true });
  } catch (error) {
    console.warn("无法开启麦克风的回声消除：", error);
  }
}

function errorText(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === "string") return error;
  return "未知错误";
}

/** 创建 Pipecat 客户端，并把它的回调接到界面状态的 dispatch 上。 */
export function createMeetingClient(
  dispatch: Dispatch<Action>,
  onMicTrack: (track: MediaStreamTrack | null) => void = () => undefined,
): PipecatClient {
  return new PipecatClient({
    transport: new SmallWebRTCTransport(),
    enableMic: true,
    enableCam: false,
    callbacks: {
      onTransportStateChanged: (transport) => dispatch({ type: "transport", transport }),
      onServerMessage: (data) => {
        const message = parseServerMessage(data);
        if (!message) return; // 不认识的消息类型先忽略
        switch (message.type) {
          case "caption":
            dispatch({ type: "caption", message });
            break;
          case "utterance":
            dispatch({ type: "utterance", message });
            break;
          case "utterance_update":
            dispatch({ type: "utteranceUpdate", message });
            break;
          case "speaker":
            dispatch({ type: "speakerRenamed", idx: message.idx, displayName: message.display_name });
            break;
          case "speakers_merged":
            dispatch({
              type: "speakersMerged",
              from: message.from,
              into: message.into,
              displayName: message.display_name,
            });
            break;
          case "session":
            dispatch({ type: "sessionStarted", message });
            break;
          case "session_closed":
            dispatch({ type: "sessionClosed", message });
            break;
          case "assistant_state":
            dispatch({ type: "assistantState", state: message.state });
            break;
          case "notice":
            dispatch({ type: "notice", level: message.level, text: message.text });
            break;
          case "frame": {
            const { id, t, width, height } = message;
            dispatch({ type: "frame", frame: { id, t, width, height } });
            break;
          }
          case "frame_caption":
            dispatch({ type: "frameCaption", id: message.id, caption: message.caption });
            break;
          case "task": {
            const { type: _type, ...task } = message;
            dispatch({ type: "task", task });
            break;
          }
          case "task_event":
            dispatch({ type: "taskEvent", taskId: message.task_id, summary: message.summary });
            break;
        }
      },
      onBotLlmStarted: () => dispatch({ type: "llmStarted" }),
      onBotLlmText: ({ text }) => dispatch({ type: "llmText", text }),
      onBotStartedSpeaking: () => dispatch({ type: "assistantState", state: "speaking" }),
      onBotStoppedSpeaking: () => dispatch({ type: "assistantState", state: "idle" }),
      onTrackStarted: (track, participant) => {
        if (!participant?.local || track.kind !== "audio") return;
        void ensureEchoCancellation(track);
        // 浏览器的自动增益 / 降噪有没有开，直接影响麦克风有多轻（docs/pipecat-notes.md §12）。
        console.info("麦克风轨道设置：", describeTrackSettings(track.getSettings()));
        onMicTrack(track);
      },
      onTrackStopped: (track, participant) => {
        if (participant?.local && track.kind === "audio") onMicTrack(null);
      },
      onDeviceError: (error) =>
        dispatch({ type: "notice", level: "error", text: `麦克风不可用：${errorText(error)}` }),
      onError: (message) => {
        // 实时模型、语音合成这类出错了还能继续开会的情况，服务端会另发一条中文的 notice；这里只管致命的
        const text = fatalErrorText(message.data);
        if (text === null) console.warn("服务端报告了一个错误：", message.data);
        else dispatch({ type: "notice", level: "error", text });
      },
    },
  });
}

export interface MeetingClient {
  client: PipecatClient;
  state: MeetingState;
  /** 本机麦克风轨道（连接期间有值），给电平条用 */
  micTrack: MediaStreamTrack | null;
  /** 开始一场新会议（别的没结束的会议原样留着，之后还能继续） */
  start: () => Promise<void>;
  /** 继续一场已有的会议（已中断、已结束、或正在别的设备上进行的） */
  resume: (id: string) => Promise<void>;
  /** 正在只读地看一场在别的设备上进行的会议 */
  watching: boolean;
  /** 把一个说话人的全部发言并入另一个 */
  mergeSpeakers: (idx: number, into: number) => Promise<void>;
  /** 把选中的发言改成另一个说话人；成功返回 true */
  assignSpeaker: (
    ids: readonly number[],
    target: { speakerIdx: number } | { newSpeaker: string },
  ) => Promise<boolean>;
  /** 给正在显示的这场会议生成（或重新生成）会后报告 */
  generateReport: () => Promise<void>;
  /** 结束当前这场会议并断开连接 */
  stop: () => Promise<void>;
  dismissNotice: (id: number) => void;
  /** 在字幕区查看另一场会议（连接期间不可用） */
  selectSession: (id: string) => Promise<void>;
  /** 向上加载更早的发言 */
  loadOlder: () => Promise<void>;
  refreshSessions: () => Promise<void>;
  renameSession: (id: string, title: string) => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
  renameSpeaker: (idx: number, displayName: string) => Promise<void>;
  /** 向助理发一条文字消息（只用文字回答）。发出去返回 true；没通过校验或还没连接返回 false 并给出提示。 */
  sendText: (raw: string) => boolean;
  /** 开始共享屏幕（弹出浏览器的选择窗口）；只在连接期间有效 */
  startScreenShare: () => Promise<void>;
  stopScreenShare: () => void;
  /** 取一个任务的全部内容（结果、进度、外发了什么）；失败返回 null 并给出提示 */
  loadTask: (id: string) => Promise<TaskDetail | null>;
  cancelTask: (id: string) => Promise<void>;
}

export function useMeetingClient(): MeetingClient {
  const [state, dispatch] = useReducer(reduce, initialState);
  const [micTrack, setMicTrack] = useState<MediaStreamTrack | null>(null);
  // 客户端只建一次，开始 / 结束只是对它 connect / disconnect。
  const [client] = useState(() => createMeetingClient(dispatch, setMicTrack));
  const api: Api = useMemo(() => createApi(), []);

  // 回调里要读最新状态，又不想每次状态变化都重建回调。
  const latest = useRef(state);
  latest.current = state;

  // 交给 SDK 的连接参数。始终是同一个对象：SDK 自己重连（新建 PeerConnection 再发一次 offer）时读的还是它，
  // 所以一旦知道自己在哪场会议里，就把 session_id 写进去——之后不管是谁发起的重连，都是「继续这一场」，不会另开一场。
  const request = useRef({ endpoint: OFFER_ENDPOINT, requestData: {} as Record<string, string> });
  const inMeeting = useRef(false); // 连上过，并且用户没有自己离开
  const lastSessionId = useRef<string | null>(null);
  const retryTimer = useRef<number | null>(null);
  const attempts = useRef(0);

  const rememberSession = useCallback((id: string) => {
    lastSessionId.current = id;
    request.current.requestData.session_id = id;
  }, []);

  const cancelRetry = useCallback(() => {
    if (retryTimer.current !== null) window.clearTimeout(retryTimer.current);
    retryTimer.current = null;
    attempts.current = 0;
    dispatch({ type: "reconnectStopped" });
  }, []);

  const fail = useCallback((what: string, error: unknown) => {
    const text = error instanceof ApiError ? error.message : errorText(error);
    dispatch({ type: "notice", level: "error", text: `${what}：${text}` });
  }, []);

  const refreshSessions = useCallback(async () => {
    try {
      dispatch({ type: "sessionsLoaded", items: await api.listSessions() });
    } catch (error) {
      fail("读取会议列表失败", error);
    }
  }, [api, fail]);

  /** 把字幕区切到一场会议：取它最近的发言和说话人。``id`` 为空时取「当前会话」。 */
  const loadView = useCallback(
    async (id: string | null) => {
      try {
        const detail: SessionDetail | null = id
          ? await api.getSession(id)
          : await api.currentSession();
        if (!detail) {
          dispatch({ type: "viewLoaded", detail: null, items: [], speakers: [] });
          return;
        }
        const [items, speakers] = await Promise.all([
          api.listUtterances(detail.id, { tail: PAGE_SIZE }),
          api.listSpeakers(detail.id),
        ]);
        dispatch({ type: "viewLoaded", detail, items, speakers });
        // 截图时间线单独取：取不到不影响字幕
        void api
          .listFrames(detail.id)
          .then((frames) =>
            dispatch({ type: "framesLoaded", sessionId: detail.id, items: frames, merge: false }),
          )
          .catch(() => undefined);
        void api
          .listTasks(detail.id)
          .then((tasks) => dispatch({ type: "tasksLoaded", sessionId: detail.id, items: tasks }))
          .catch(() => undefined);
        void api
          .getReport(detail.id)
          .then((report) => dispatch({ type: "reportLoaded", sessionId: detail.id, report }))
          .catch(() => undefined);
      } catch (error) {
        fail("读取会议内容失败", error);
      }
    },
    [api, fail],
  );

  /** 连接建立后与服务端对一遍：字幕区显示的是不是进行中的这场；是的话补上断线期间错过的发言。 */
  const syncAfterConnect = useCallback(async () => {
    try {
      const current = await api.currentSession();
      if (!current) return;
      if (current.state === "live") rememberSession(current.id);
      dispatch({ type: "detailRefreshed", detail: current });
      if (latest.current.viewing?.id !== current.id) {
        await loadView(current.id);
        return;
      }
      const after = backfillAfterId(latest.current.captions);
      const items = await api.listUtterances(
        current.id,
        after === null ? { tail: PAGE_SIZE } : { afterId: after },
      );
      if (items.length > 0) dispatch({ type: "backfilled", items });
      const frames = await api.listFrames(current.id);
      dispatch({ type: "framesLoaded", sessionId: current.id, items: frames, merge: true });
      dispatch({
        type: "tasksLoaded",
        sessionId: current.id,
        items: await api.listTasks(current.id),
      });
    } catch (error) {
      fail("同步会议内容失败", error);
    }
  }, [api, fail, loadView, rememberSession]);

  // 页面一打开（包括刷新、换设备）：不需要点「开始」，先看到会议列表和最近的对话。
  useEffect(() => {
    void refreshSessions();
    void loadView(null);
  }, [refreshSessions, loadView]);

  // 连接断开之后：列表里的状态和字幕区的内容以服务端为准重新取一遍。
  const wasConnected = useRef(false);
  useEffect(() => {
    if (state.connection === "connected") wasConnected.current = true;
    if (state.connection === "disconnected" && wasConnected.current) {
      wasConnected.current = false;
      void refreshSessions();
      void loadView(latest.current.viewing?.id ?? null);
    }
  }, [state.connection, refreshSessions, loadView]);

  // 连接就绪（不管是点「开始」还是 SDK 自己重连上的）：与服务端对一遍，并刷新列表。
  useEffect(() => {
    if (state.connection === "connected") {
      void syncAfterConnect();
      void refreshSessions();
    }
  }, [state.connection, syncAfterConnect, refreshSessions]);

  // 连接期间每隔几秒确认一次字幕区显示的还是进行中的这场（服务重启后 SDK 会自己重连，那时会话已经换了）。
  useEffect(() => {
    if (state.connection !== "connected") return;
    const timer = window.setInterval(() => {
      void api
        .currentSession()
        .then((current) => {
          if (!current) return;
          if (current.state === "live") rememberSession(current.id);
          if (current.id !== latest.current.viewing?.id) return loadView(current.id);
          // 同一场：顺手更新横幅上的数字和各次连接（SDK 自己重连成功时页面收不到别的通知）
          dispatch({ type: "detailRefreshed", detail: current });
        })
        .catch(() => undefined);
    }, SESSION_CHECK_MS);
    return () => window.clearInterval(timer);
  }, [state.connection, api, loadView, rememberSession]);

  /** 连接。``sessionId`` 给了就是继续那一场，不给是新建。返回是否连上了。 */
  const connectTo = useCallback(
    async (sessionId: string | null, quiet = false): Promise<boolean> => {
      const data = request.current.requestData;
      delete data.session_id;
      if (sessionId) data.session_id = sessionId;
      dispatch({ type: "connecting" });
      try {
        await client.connect({ webrtcRequestParams: request.current });
        return true;
      } catch (error) {
        if (!quiet) {
          dispatch({ type: "notice", level: "error", text: `连接失败：${errorText(error)}` });
        }
        dispatch({ type: "transport", transport: "error" });
        await client.disconnect().catch(() => undefined);
        return false;
      }
    },
    [client],
  );

  const start = useCallback(async () => {
    cancelRetry();
    lastSessionId.current = null;
    await connectTo(null);
  }, [cancelRetry, connectTo]);

  const resume = useCallback(
    async (id: string) => {
      const connection = latest.current.connection;
      if (connection === "connected" || connection === "connecting") return;
      cancelRetry();
      lastSessionId.current = id;
      if (latest.current.viewing?.id !== id) await loadView(id);
      if (!(await connectTo(id))) void refreshSessions(); // 多半是这场会议已经被删掉了
    },
    [cancelRetry, connectTo, loadView, refreshSessions],
  );

  // 连上之后记下「我在会议里」和是哪一场；之后意外断开就自动重连回同一场。
  useEffect(() => {
    if (state.connection === "connected") {
      inMeeting.current = true;
      attempts.current = 0;
    }
  }, [state.connection]);
  useEffect(() => {
    if (state.liveSessionId !== null) rememberSession(state.liveSessionId);
  }, [state.liveSessionId, rememberSession]);

  // 自动重连：意外断开时用同一个 session_id 重试，退避 1、2、4、8、8 秒，最多 5 次。
  // 被另一个页面接管、会议已结束、用户自己离开，都不重连。
  useEffect(() => {
    if (state.connection !== "disconnected" && state.connection !== "error") return;
    if (retryTimer.current !== null) return; // 已经排了下一次
    const info = {
      inMeeting: inMeeting.current,
      closedReason: latest.current.closedReason,
      sessionId: lastSessionId.current,
    };
    if (!shouldReconnect(info)) {
      inMeeting.current = false;
      attempts.current = 0;
      if (latest.current.reconnectAttempt > 0) dispatch({ type: "reconnectStopped" });
      return;
    }
    const attempt = attempts.current + 1;
    const delay = reconnectDelayMs(attempt);
    if (delay === null) {
      inMeeting.current = false;
      attempts.current = 0;
      dispatch({ type: "reconnectStopped" });
      dispatch({
        type: "notice",
        level: "error",
        text: "自动重连没有成功。网络或服务恢复之后，点会议横幅上的「继续」接着开这场会议。",
      });
      return;
    }
    attempts.current = attempt;
    dispatch({ type: "reconnecting", attempt });
    retryTimer.current = window.setTimeout(() => {
      retryTimer.current = null;
      void connectTo(info.sessionId, true);
    }, delay);
  }, [state.connection, connectTo]);

  // 只读地看一场正在别的设备上进行的会议：每 3 秒取一次新发言和会议状态。
  const watchingId =
    state.connection === "disconnected" &&
    state.reconnectAttempt === 0 &&
    state.viewing?.state === "live"
      ? state.viewing.id
      : null;
  useEffect(() => {
    if (watchingId === null) return;
    const timer = window.setInterval(() => {
      const after = backfillAfterId(latest.current.captions);
      void Promise.all([
        api.getSession(watchingId),
        api.listUtterances(watchingId, after === null ? { tail: PAGE_SIZE } : { afterId: after }),
      ])
        .then(([detail, items]) => {
          dispatch({ type: "detailRefreshed", detail });
          if (items.length > 0) dispatch({ type: "backfilled", items });
        })
        .catch(() => undefined); // 取不到就等下一次
    }, WATCH_POLL_MS);
    return () => window.clearInterval(timer);
  }, [watchingId, api]);

  const stop = useCallback(async () => {
    inMeeting.current = false;
    cancelRetry();
    const id = latest.current.liveSessionId ?? lastSessionId.current ?? latest.current.viewing?.id;
    if (id) {
      try {
        await api.endSession(id); // 服务端写下「已结束」，并停掉这一路连接
      } catch (error) {
        fail("结束会议失败", error);
      }
    }
    await client.disconnect().catch((error: unknown) => console.warn("断开连接时出错：", error));
    void refreshSessions();
    void loadView(latest.current.viewing?.id ?? null);
  }, [api, client, fail, cancelRetry, refreshSessions, loadView]);

  const selectSession = useCallback(
    async (id: string) => {
      const connection = latest.current.connection;
      if (connection === "connected" || connection === "connecting") return;
      await loadView(id);
    },
    [loadView],
  );

  const loadOlder = useCallback(async () => {
    const viewing = latest.current.viewing;
    const before = firstUtteranceId(latest.current.captions);
    if (!viewing || before === null) return;
    try {
      const items = await api.listUtterances(viewing.id, { beforeId: before, limit: PAGE_SIZE });
      dispatch({ type: "olderLoaded", items });
    } catch (error) {
      fail("加载更早的发言失败", error);
    }
  }, [api, fail]);

  const renameSession = useCallback(
    async (id: string, title: string) => {
      try {
        dispatch({ type: "sessionRenamed", summary: await api.renameSession(id, title) });
      } catch (error) {
        fail("重命名失败", error);
      }
    },
    [api, fail],
  );

  const deleteSession = useCallback(
    async (id: string) => {
      try {
        await api.deleteSession(id);
      } catch (error) {
        fail("删除失败", error);
        return;
      }
      const wasViewing = latest.current.viewing?.id === id;
      dispatch({ type: "sessionRemoved", id });
      if (wasViewing) await loadView(null);
    },
    [api, fail, loadView],
  );

  const renameSpeaker = useCallback(
    async (idx: number, displayName: string) => {
      const viewing = latest.current.viewing;
      if (!viewing) return;
      try {
        const done = await api.renameSpeaker(viewing.id, idx, displayName);
        dispatch({ type: "speakerRenamed", idx: done.idx, displayName: done.display_name });
      } catch (error) {
        fail("改名失败", error);
      }
    },
    [api, fail],
  );

  const assignSpeaker = useCallback(
    async (
      ids: readonly number[],
      target: { speakerIdx: number } | { newSpeaker: string },
    ): Promise<boolean> => {
      const viewing = latest.current.viewing;
      if (!viewing || ids.length === 0) return false;
      try {
        const done = await api.assignSpeaker(viewing.id, ids, target);
        for (const id of done.ids) {
          dispatch({
            type: "utteranceUpdate",
            message: {
              type: "utterance_update",
              id,
              speaker_idx: done.speaker.idx,
              speaker_name: done.speaker.display_name,
            },
          });
        }
        // 新建的说话人即使一条都没改成，也要出现在说话人列表里
        dispatch({ type: "speakersLoaded", speakers: await api.listSpeakers(viewing.id) });
        return true;
      } catch (error) {
        fail("修改发言人失败", error);
        return false;
      }
    },
    [api, fail],
  );

  const generateReport = useCallback(async () => {
    const viewing = latest.current.viewing;
    if (!viewing) return;
    try {
      await api.startReport(viewing.id);
      dispatch({
        type: "reportLoaded",
        sessionId: viewing.id,
        report: await api.getReport(viewing.id),
      });
    } catch (error) {
      fail("生成报告失败", error);
    }
  }, [api, fail]);

  // 报告生成期间每 2 秒问一次，不依赖音频连接。
  const pollingReportFor =
    state.report?.status === "running" && state.viewing ? state.viewing.id : null;
  useEffect(() => {
    if (pollingReportFor === null) return;
    const timer = window.setInterval(() => {
      void api
        .getReport(pollingReportFor)
        .then((report) => dispatch({ type: "reportLoaded", sessionId: pollingReportFor, report }))
        .catch(() => undefined); // 取不到就等下一次
    }, REPORT_POLL_MS);
    return () => window.clearInterval(timer);
  }, [pollingReportFor, api]);

  const mergeSpeakers = useCallback(
    async (idx: number, into: number) => {
      const viewing = latest.current.viewing;
      if (!viewing) return;
      try {
        const done = await api.mergeSpeakers(viewing.id, idx, into);
        dispatch({
          type: "speakersMerged",
          from: done.from,
          into: done.into,
          displayName: done.display_name,
        });
      } catch (error) {
        fail("合并说话人失败", error);
      }
    },
    [api, fail],
  );

  const sendText = useCallback(
    (raw: string): boolean => {
      const checked = validateText(raw);
      if (!checked.ok) {
        dispatch({ type: "notice", level: "warn", text: checked.reason });
        return false;
      }
      if (latest.current.connection !== "connected") {
        dispatch({ type: "notice", level: "warn", text: "还没有连接，请先点「开始新会议」" });
        return false;
      }
      try {
        client.sendClientMessage("text_input", { text: checked.text });
      } catch (error) {
        fail("发送失败", error);
        return false;
      }
      dispatch({ type: "textSent" });
      return true;
    },
    [client, fail],
  );

  const sendScreenState = useCallback(
    (sharing: boolean) => {
      if (latest.current.connection !== "connected") return;
      try {
        client.sendClientMessage("screen_state", { sharing });
      } catch {
        // 只用于界面状态与日志，发不出去不要紧
      }
    },
    [client],
  );
  const screenShare = useScreenShare({
    api,
    connected: state.connection === "connected",
    config: state.screen,
    dispatch,
    sendState: sendScreenState,
  });

  const loadTask = useCallback(
    async (id: string): Promise<TaskDetail | null> => {
      try {
        return await api.getTask(id);
      } catch (error) {
        fail("读取任务失败", error);
        return null;
      }
    },
    [api, fail],
  );

  const cancelTask = useCallback(
    async (id: string) => {
      try {
        const { ...task } = await api.cancelTask(id);
        dispatch({ type: "task", task });
      } catch (error) {
        fail("取消任务失败", error);
      }
    },
    [api, fail],
  );

  const dismissNotice = useCallback((id: number) => dispatch({ type: "dismissNotice", id }), []);

  useEffect(() => {
    const leave = () => {
      inMeeting.current = false; // 关页面是用户自己要走，不重连
      void client.disconnect().catch(() => undefined);
    };
    window.addEventListener("pagehide", leave);
    return () => window.removeEventListener("pagehide", leave);
  }, [client]);

  return {
    client,
    state,
    micTrack,
    start,
    resume,
    watching: watchingId !== null,
    mergeSpeakers,
    assignSpeaker,
    generateReport,
    stop,
    dismissNotice,
    selectSession,
    loadOlder,
    refreshSessions,
    renameSession,
    deleteSession,
    renameSpeaker,
    sendText,
    startScreenShare: screenShare.start,
    stopScreenShare: screenShare.stop,
    loadTask,
    cancelTask,
  };
}
