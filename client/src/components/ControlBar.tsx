import { HistoryIcon, LogOutIcon, MicIcon, PresentationIcon, XIcon } from "lucide-react";

import { UserAudioControl } from "@/components/pipecat/user-audio-control";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useI18n, type Messages } from "../i18n/index.ts";
import type { ConnectionState } from "../meetingState.ts";
import { recordingLabel } from "../sessionView.ts";
import { LanguageMenu } from "./LanguageMenu.tsx";
import { ThemeMenu } from "./ThemeMenu.tsx";

/** 连接状态前面的圆点和文字颜色：绿色接通、黄色进行中、红色断开或出错。 */
const TONES: Record<ConnectionState, string> = {
  disconnected: "text-destructive",
  connecting: "text-warning-foreground",
  connected: "text-success-foreground",
  error: "text-destructive",
};

/** 顶部的连接状态：文字和颜色。自动重连的间隙里显示第几次重连。演示模式也用它。 */
export function connectionStatus(
  t: Messages,
  connection: ConnectionState,
  reconnectAttempt: number,
): { label: string; tone: string } {
  const reconnecting = reconnectAttempt > 0 && connection !== "connected";
  return {
    label: reconnecting
      ? t.header.connection.reconnecting(reconnectAttempt)
      : t.header.connection[connection],
    tone: TONES[reconnecting ? "connecting" : connection],
  };
}

interface Props {
  connection: ConnectionState;
  /** 连接意外断开后正在自动重连：第几次；0 表示没有 */
  reconnectAttempt: number;
  closedReason: string | null;
  listOpen: boolean;
  onToggleList: () => void;
  /** 这个浏览器能不能开会；不能时「开始新会议」变灰，原因显示在顶部栏下面 */
  canStart: boolean;
  onStart: () => void;
  onStop: () => void;
  /** 打开演示模式；没有可看的会议时不给 */
  onPresent?: () => void;
  /** 启用了访问口令时才有。会议进行中不显示，免得退出后页面上留着一个没人管的连接 */
  onLogout?: () => void;
}

/**
 * 顶部栏（Figma「Meeting workspace」的页头）：产品名和会议列表开关在左；
 * 连接与转录状态、主题、麦克风（Pipecat UI 的 UserAudioControl）和开始 / 结束会议在右。
 */
export function ControlBar({
  connection,
  reconnectAttempt,
  closedReason,
  listOpen,
  onToggleList,
  canStart,
  onStart,
  onStop,
  onPresent,
  onLogout,
}: Props) {
  const { t } = useI18n();
  // 自动重连的间隙里连接是断开的，但用户仍然「在会议里」：按钮还是「结束会议」
  const active = connection === "connected" || connection === "connecting" || reconnectAttempt > 0;
  // 麦克风按钮等接通（或接通过、正在重连）再出现：它一挂上就列设备，列设备会再要一次麦克风权限，
  // 连接还在请求权限时挂上，拒绝授权就会多出一次请求和一条重复的「麦克风不可用」
  const micControl = connection === "connected" || reconnectAttempt > 0;
  const status = connectionStatus(t, connection, reconnectAttempt);
  return (
    <header className="mb-3 flex flex-wrap items-center gap-2 border-b py-3">
      <MicIcon className="size-4" aria-hidden="true" />
      <h1 className="text-sm font-medium">{t.common.appName}</h1>
      <Button
        variant="ghost"
        aria-expanded={listOpen}
        aria-controls="session-list"
        onClick={onToggleList}
      >
        <HistoryIcon data-icon="inline-start" />
        {t.header.meetings}
      </Button>
      {onPresent && (
        // 投到会议室大屏才用得上，手机上不放
        <Button variant="ghost" className="max-md:hidden" onClick={onPresent}>
          <PresentationIcon data-icon="inline-start" />
          {t.header.present}
        </Button>
      )}
      <div className="flex-1" />
      <span className={cn("inline-flex items-center gap-1.5 text-xs font-medium", status.tone)}>
        <span className="size-1.5 rounded-full bg-current" aria-hidden="true" />
        <span role="status">{status.label}</span>
      </span>
      <span
        className="text-xs text-muted-foreground"
        role="status"
        aria-label={t.header.recordingLabel}
      >
        {recordingLabel(t, connection, closedReason, reconnectAttempt)}
      </span>
      <LanguageMenu />
      <ThemeMenu />
      {micControl && (
        <UserAudioControl
          variant="secondary"
          activeText={t.header.microphone}
          inactiveText={t.header.muted}
          noModeSwitch
          // 接通时整组是淡绿色（Figma 的麦克风按钮）；静音时按钮自己换成红色
          className="[&_[data-slot=button]:not([data-state=inactive])]:bg-success [&_[data-slot=button]:not([data-state=inactive])]:text-success-foreground"
        />
      )}
      {active ? (
        <Button variant="destructive" onClick={onStop}>
          <XIcon data-icon="inline-start" />
          {t.header.end}
        </Button>
      ) : (
        <Button
          className="bg-active text-active-foreground hover:bg-active/85"
          disabled={!canStart}
          onClick={onStart}
        >
          <MicIcon data-icon="inline-start" />
          {t.header.start}
        </Button>
      )}
      {onLogout && !active && (
        <Button variant="ghost" onClick={onLogout}>
          <LogOutIcon data-icon="inline-start" />
          {t.header.logout}
        </Button>
      )}
    </header>
  );
}
