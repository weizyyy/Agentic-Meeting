import type { ConnectionState } from "../meetingState.ts";
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
  micTrack: MediaStreamTrack | null;
  listOpen: boolean;
  onToggleList: () => void;
  onStart: () => void;
  onStop: () => void;
}

/** 顶部栏：标题、连接状态、麦克风电平、会议列表开关、开始新会议 / 结束会议。 */
export function ControlBar({
  connection,
  reconnectAttempt,
  micTrack,
  listOpen,
  onToggleList,
  onStart,
  onStop,
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
        <button type="button" className="button button-start" onClick={onStart}>
          开始新会议
        </button>
      )}
    </header>
  );
}
