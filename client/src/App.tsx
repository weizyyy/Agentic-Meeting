import { PipecatClientAudio, PipecatClientProvider } from "@pipecat-ai/client-react";
import { useEffect, useMemo, useState } from "react";

import { AssistantPanel } from "./components/AssistantPanel.tsx";
import { CaptionList } from "./components/CaptionList.tsx";
import { ControlBar } from "./components/ControlBar.tsx";
import { FrameTimeline } from "./components/FrameTimeline.tsx";
import { ReportPanel } from "./components/ReportPanel.tsx";
import { SessionBanner } from "./components/SessionBanner.tsx";
import { SessionList } from "./components/SessionList.tsx";
import { SpeakerBar } from "./components/SpeakerBar.tsx";
import { TaskPanel } from "./components/TaskPanel.tsx";
import { pruneSelection } from "./selection.ts";
import type { TimeBase } from "./timeline.ts";
import { useMeetingClient } from "./useMeetingClient.ts";

interface Props {
  /** 启用了访问口令时才有：顶部栏显示「退出登录」 */
  onLogout?: () => void;
}

export function App({ onLogout }: Props = {}) {
  const meeting = useMeetingClient();
  const { client, state } = meeting;
  const [listOpen, setListOpen] = useState(false);
  const [tab, setTab] = useState<"captions" | "report">("captions");
  // 字幕里选中的发言（按发言编号）：在字幕左边点或拖选，再点上方的说话人归过去。
  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set());
  useEffect(() => {
    setSelected((current) => pruneSelection(state.captions, current));
  }, [state.captions]);
  useEffect(() => {
    if (selected.size === 0) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSelected(new Set());
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected.size]);
  const startedAt = state.viewing?.started_at ?? null;
  const timeBase = useMemo<TimeBase | null>(
    () => (startedAt === null ? null : { startedAt, connections: state.connections }),
    [startedAt, state.connections],
  );
  const inMeeting =
    state.connection === "connected" ||
    state.connection === "connecting" ||
    state.reconnectAttempt > 0;

  const emptyText = inMeeting
    ? "会议已经开始，说的话会实时显示在这里。"
    : state.viewing
      ? "这场会议还没有发言记录。"
      : "点右上角的「开始新会议」，说话的内容会实时显示在这里。";

  return (
    <PipecatClientProvider client={client}>
      <PipecatClientAudio />
      <div className="app">
        <ControlBar
          connection={state.connection}
          reconnectAttempt={state.reconnectAttempt}
          micTrack={meeting.micTrack}
          closedReason={state.closedReason}
          listOpen={listOpen}
          onToggleList={() => {
            setListOpen((open) => !open);
            void meeting.refreshSessions();
          }}
          canStart={meeting.unsupported === null}
          onStart={() => void meeting.start()}
          onStop={() => void meeting.stop()}
          onLogout={onLogout}
        />
        {meeting.unsupported && (
          <p className="unsupported" role="alert">
            {meeting.unsupported}
          </p>
        )}
        <SessionBanner
          session={state.viewing}
          connection={state.connection}
          reconnectAttempt={state.reconnectAttempt}
          watching={meeting.watching}
          keepPending={meeting.keepPending.has(state.viewing?.id ?? "")}
          onKeep={(id, keep) => void meeting.keepSession(id, keep)}
          onRename={(id, title) => void meeting.renameSession(id, title)}
          onResume={(id) => void meeting.resume(id)}
          onStart={() => void meeting.start()}
        />
        {!state.viewing?.deletion_pending && (
          <main className="layout">
            <div className="column">
              <SpeakerBar
                speakers={state.speakers}
                members={state.members}
                onRename={(idx, name) => void meeting.renameSpeaker(idx, name)}
                onMerge={(idx, into) => void meeting.mergeSpeakers(idx, into)}
                selectedCount={tab === "captions" ? selected.size : 0}
                onAssign={(target) => {
                  void meeting.assignSpeaker([...selected], target).then((done) => {
                    if (done) setSelected(new Set());
                  });
                }}
                onClearSelection={() => setSelected(new Set())}
              />
              <div className="tabs" role="tablist" aria-label="字幕和报告">
                <button
                  type="button"
                  role="tab"
                  aria-selected={tab === "captions"}
                  className={`tab${tab === "captions" ? " tab-current" : ""}`}
                  onClick={() => setTab("captions")}
                >
                  字幕
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={tab === "report"}
                  className={`tab${tab === "report" ? " tab-current" : ""}`}
                  onClick={() => setTab("report")}
                >
                  报告
                  {state.report?.status === "running" && (
                    <span className="tab-dot" aria-label="生成中" />
                  )}
                </button>
              </div>
              {tab === "captions" ? (
                <CaptionList
                  lines={state.captions}
                  hasOlder={state.hasOlder}
                  connections={state.connections}
                  emptyText={emptyText}
                  onLoadOlder={() => void meeting.loadOlder()}
                  timeBase={timeBase}
                  selected={selected}
                  onSelect={setSelected}
                />
              ) : (
                <ReportPanel
                  session={state.viewing}
                  report={state.report}
                  onGenerate={() => void meeting.generateReport()}
                />
              )}
            </div>
            <div className="column side">
              <AssistantPanel
                state={state.assistantState}
                text={state.assistantText}
                replyMode={state.replyMode}
                notices={state.notices}
                canSend={state.connection === "connected"}
                onSend={meeting.sendText}
                onDismissNotice={meeting.dismissNotice}
              />
              <TaskPanel
                tasks={state.tasks}
                onLoad={meeting.loadTask}
                onCancel={(id) => void meeting.cancelTask(id)}
              />
              <FrameTimeline
                frames={state.frames}
                timeBase={timeBase}
                sharing={state.sharing}
                canShare={state.connection === "connected" && state.screen.enabled}
                onStart={() => void meeting.startScreenShare()}
                onStop={meeting.stopScreenShare}
              />
            </div>
          </main>
        )}
        {listOpen && (
          <SessionList
            sessions={state.sessions}
            viewingId={state.viewing?.id ?? null}
            locked={inMeeting}
            onSelect={(id) => {
              void meeting.selectSession(id);
              setListOpen(false);
            }}
            onResume={(id) => {
              void meeting.resume(id);
              setListOpen(false);
            }}
            onRename={(id, title) => void meeting.renameSession(id, title)}
            keepPending={meeting.keepPending}
            onKeep={(id, keep) => void meeting.keepSession(id, keep)}
            onDelete={(id) => void meeting.deleteSession(id)}
            onClose={() => setListOpen(false)}
          />
        )}
      </div>
    </PipecatClientProvider>
  );
}
