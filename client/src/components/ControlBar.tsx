import type { ConnectionState } from "../meetingState.ts";
import { recordingLabel } from "../sessionView.ts";
import { MicMeter } from "./MicMeter.tsx";

const LABELS: Record<ConnectionState, string> = {
  disconnected: "未连接",
  connecting: "连接中…",
  connected: "已连接",
  error: "连接出错",
};

interface Props {
  connection: ConnectionState;
  /** 连接意外断开后正在自动重连：第几次；0 表示没有 */
  reconnectAttempt: number;
  closedReason: string | null;
  micTrack: MediaStreamTrack | null;
  listOpen: boolean;
  onToggleList: () => void;
  /** 这个浏览器能不能开会；不能时「开始新会议」变灰，原因显示在顶部栏下面 */
  canStart: boolean;
  onStart: () => void;
  onStop: () => void;
  /** 启用了访问口令时才有。会议进行中不显示，免得退出后页面上留着一个没人管的连接 */
  onLogout?: () => void;
}

/** 顶部栏：标题、连接状态、麦克风电平、会议列表开关、开始新会议 / 结束会议。 */
export function ControlBar({
  connection,
  reconnectAttempt,
  micTrack,
  closedReason,
  listOpen,
  onToggleList,
  canStart,
  onStart,
  onStop,
  onLogout,
}: Props) {
  // 自动重连的间隙里连接是断开的，但用户仍然「在会议里」：按钮还是「结束会议」
  const active = connection === "connected" || connection === "connecting" || reconnectAttempt > 0;
  const label =
    reconnectAttempt > 0 && connection !== "connected"
      ? `正在重连（第 ${reconnectAttempt} 次）…`
      : LABELS[connection];
  return (
    <header className="topbar">
      <h1>组会助理</h1>
      <span className={`status status-${connection}`} role="status">
        {label}
      </span>
      <span className="status" role="status" aria-label="转录状态">
        {recordingLabel(connection, closedReason, reconnectAttempt)}
      </span>
      <MicMeter track={micTrack} />
      <div className="spacer" />
      <button
        type="button"
        className="button button-quiet"
        aria-expanded={listOpen}
        aria-controls="session-list"
        onClick={onToggleList}
      >
        会议列表
      </button>
      {active ? (
        <button type="button" className="button button-stop" onClick={onStop}>
          结束会议
        </button>
      ) : (
        <button
          type="button"
          className="button button-start"
          disabled={!canStart}
          onClick={onStart}
        >
          开始新会议
        </button>
      )}
      {onLogout && !active && (
        <button type="button" className="button button-quiet" onClick={onLogout}>
          退出登录
        </button>
      )}
    </header>
  );
}
