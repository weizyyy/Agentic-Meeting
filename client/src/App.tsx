import { PipecatClientProvider } from "@pipecat-ai/client-react";
import { TriangleAlertIcon } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { AssistantAudio } from "./components/AssistantAudio.tsx";
import { AssistantPanel } from "./components/AssistantPanel.tsx";
import { CaptionList } from "./components/CaptionList.tsx";
import { ControlBar } from "./components/ControlBar.tsx";
import { FrameTimeline } from "./components/FrameTimeline.tsx";
import { PresentationView } from "./components/PresentationView.tsx";
import { ReportPanel } from "./components/ReportPanel.tsx";
import { SessionBanner } from "./components/SessionBanner.tsx";
import { SessionList } from "./components/SessionList.tsx";
import { SpeakerBar } from "./components/SpeakerBar.tsx";
import { TaskPanel } from "./components/TaskPanel.tsx";
import { unsupportedText } from "./browserSupport.ts";
import { useI18n } from "./i18n/index.ts";
import { pruneSelection } from "./selection.ts";
import { sessionTitle } from "./sessionView.ts";
import type { TimeBase } from "./timeline.ts";
import { useMeetingClient } from "./useMeetingClient.ts";

/** 手机浏览器没有屏幕共享；不经 HTTPS 或 localhost 访问时桌面浏览器也没有 */
const SHARE_SUPPORTED = typeof navigator.mediaDevices?.getDisplayMedia === "function";

/** 手机上一次只显示会议内容的一块 */
type Section = "captions" | "assistant" | "tasks" | "screen";

/** 手机上不在当前分页的块藏起来；宽屏照常全部显示 */
function shownOn(section: Section, current: Section): string {
  return section === current ? "" : "max-md:hidden";
}

interface Props {
  /** 启用了访问口令时才有：顶部栏显示「退出登录」 */
  onLogout?: () => void;
}

export function App({ onLogout }: Props = {}) {
  const { t, language } = useI18n();
  const meeting = useMeetingClient();
  const { client, state } = meeting;
  const [listOpen, setListOpen] = useState(false);
  const [tab, setTab] = useState<"captions" | "report">("captions");
  const [section, setSection] = useState<Section>("captions");
  const [presenting, setPresenting] = useState(false);
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
  const unfinishedTasks = state.tasks.filter(
    (task) => task.status === "queued" || task.status === "running",
  ).length;
  const inMeeting =
    state.connection === "connected" ||
    state.connection === "connecting" ||
    state.reconnectAttempt > 0;

  const emptyText = inMeeting
    ? t.session.emptyCaptions.live
    : state.viewing
      ? t.session.emptyCaptions.history
      : t.session.emptyCaptions.none;

  return (
    <PipecatClientProvider client={client}>
      <div className="app">
        {/* 演示时整个工作区不可交互、读屏软件也跳过，只剩演示视图（它是模态的） */}
        <div className="contents" inert={presenting}>
          <ControlBar
            connection={state.connection}
            reconnectAttempt={state.reconnectAttempt}
            closedReason={state.closedReason}
            listOpen={listOpen}
            onToggleList={() => {
              setListOpen((open) => !open);
              void meeting.refreshSessions();
            }}
            canStart={meeting.unsupported === null}
            onStart={() => void meeting.start()}
            onStop={() => void meeting.stop()}
            onPresent={state.viewing ? () => setPresenting(true) : undefined}
            onLogout={onLogout}
          />
          {meeting.unsupported && (
            <p
              className="mb-3 rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive"
              role="alert"
            >
              {unsupportedText(t, meeting.unsupported)}
            </p>
          )}
          <AssistantAudio />
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
            // 宽屏两列：左边字幕（报告）在上、屏幕在下，右边助理和后台任务；
            // 手机上只显示当前分页的那一块
            <main className="flex min-h-0 flex-1 flex-col gap-4 md:grid md:grid-cols-[minmax(0,1fr)_minmax(0,360px)] md:grid-rows-[minmax(0,1fr)_auto] md:[grid-template-areas:'captions_side'_'screen_side']">
              {/* 手机上分页：字幕、助理、任务、画面，一次一块占满下面的高度 */}
              <Tabs
                value={section}
                onValueChange={(value: Section) => setSection(value)}
                className="md:hidden"
              >
                <TabsList aria-label={t.session.sections.label} className="w-full">
                  <TabsTrigger value="captions">{t.session.sections.captions}</TabsTrigger>
                  <TabsTrigger value="assistant">
                    {t.session.sections.assistant}
                    {state.notices.length > 0 && (
                      <span
                        className="size-1.5 rounded-full bg-warning-foreground"
                        aria-label={t.session.sections.notices(state.notices.length)}
                      />
                    )}
                  </TabsTrigger>
                  <TabsTrigger value="tasks">
                    {t.session.sections.tasks}
                    {unfinishedTasks > 0 && (
                      <span
                        className="rounded-full bg-agent px-1.5 text-xs text-agent-foreground tabular-nums"
                        aria-label={t.session.sections.active(unfinishedTasks)}
                      >
                        {unfinishedTasks}
                      </span>
                    )}
                  </TabsTrigger>
                  <TabsTrigger value="screen">{t.session.sections.screen}</TabsTrigger>
                </TabsList>
              </Tabs>
              {state.connection === "connected" && (
                // 手机上录音的限制（docs/user-guide.md「手机上」）；宽屏的电脑没有这个问题
                <p
                  className="flex items-start gap-2 rounded-lg bg-warning px-3 py-2 text-sm text-warning-foreground md:hidden"
                  role="note"
                >
                  <TriangleAlertIcon aria-hidden className="mt-0.5 size-4 shrink-0" />
                  {meeting.keepAwake === "held"
                    ? t.session.phoneRecording.held
                    : t.session.phoneRecording.notHeld}
                </p>
              )}
              <Tabs
                value={tab}
                onValueChange={(value: "captions" | "report") => setTab(value)}
                className={`min-h-0 min-w-0 flex-1 gap-0 overflow-hidden rounded-xl border bg-card [grid-area:captions] ${shownOn("captions", section)}`}
              >
                <div className="flex items-center gap-2 border-b px-4 py-2">
                  <TabsList aria-label={t.session.tabs.label}>
                    <TabsTrigger value="captions" className="px-3">
                      {t.session.tabs.captions}
                    </TabsTrigger>
                    <TabsTrigger value="report" className="px-3">
                      {t.session.tabs.report}
                      {state.report?.status === "running" && (
                        <span
                          className="size-1.5 rounded-full bg-warning-foreground"
                          aria-label={t.session.tabs.generating}
                        />
                      )}
                    </TabsTrigger>
                  </TabsList>
                </div>
                <TabsContent value="captions" className="flex min-h-0 flex-col" tabIndex={-1}>
                  <SpeakerBar
                    speakers={state.speakers}
                    members={state.members}
                    onRename={(idx, name) => void meeting.renameSpeaker(idx, name)}
                    onMerge={(idx, into) => void meeting.mergeSpeakers(idx, into)}
                    selectedCount={selected.size}
                    onAssign={(target) => {
                      void meeting.assignSpeaker([...selected], target).then((done) => {
                        if (done) setSelected(new Set());
                      });
                    }}
                    onClearSelection={() => setSelected(new Set())}
                  />
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
                </TabsContent>
                <TabsContent value="report" className="flex min-h-0 flex-col" tabIndex={-1}>
                  <ReportPanel
                    session={state.viewing}
                    report={state.report}
                    taskCount={state.tasks.length}
                    frameCount={state.frames.length}
                    onGenerate={() => void meeting.generateReport()}
                    onShowCaptions={() => setTab("captions")}
                  />
                </TabsContent>
              </Tabs>
              <div
                className={`flex min-h-0 min-w-0 flex-1 flex-col gap-4 overflow-y-auto [grid-area:side] ${section === "assistant" || section === "tasks" ? "" : "max-md:hidden"}`}
              >
                <div className={`flex flex-1 flex-col ${shownOn("assistant", section)}`}>
                  <AssistantPanel
                    state={state.assistantState}
                    text={state.assistantText}
                    replyMode={state.replyMode}
                    notices={state.notices}
                    canSend={state.connection === "connected" || meeting.watching}
                    watching={meeting.watching}
                    onSend={meeting.sendText}
                    onDismissNotice={meeting.dismissNotice}
                  />
                </div>
                <div className={`contents ${shownOn("tasks", section)}`}>
                  <TaskPanel
                    tasks={state.tasks}
                    onLoad={meeting.loadTask}
                    onCancel={(id) => void meeting.cancelTask(id)}
                  />
                </div>
              </div>
              <div className={`min-w-0 [grid-area:screen] ${shownOn("screen", section)}`}>
                <FrameTimeline
                  frames={state.frames}
                  timeBase={timeBase}
                  sharing={state.sharing}
                  canShare={state.connection === "connected" && state.screen.enabled}
                  shareSupported={SHARE_SUPPORTED}
                  onStart={() => void meeting.startScreenShare()}
                  onStop={meeting.stopScreenShare}
                />
              </div>
            </main>
          )}
          <SessionList
            open={listOpen}
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
        </div>
        {presenting && state.viewing && (
          // 放在 Provider 里面：演示时助理的声音和音量条照常
          <PresentationView
            title={sessionTitle(t, language, state.viewing)}
            connection={state.connection}
            reconnectAttempt={state.reconnectAttempt}
            captions={state.captions}
            assistantState={state.assistantState}
            assistantText={state.assistantText}
            onExit={() => setPresenting(false)}
          />
        )}
      </div>
    </PipecatClientProvider>
  );
}
