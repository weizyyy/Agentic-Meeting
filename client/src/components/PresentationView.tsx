import { MicIcon, Minimize2Icon } from "lucide-react";
import { useEffect, useRef } from "react";

import { AudioVisualizerBar } from "@/components/pipecat/audio-visualizer-bar";
import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { cn } from "@/lib/utils";
import { useI18n } from "../i18n/index.ts";
import type { CaptionLine } from "../captions.ts";
import type { ConnectionState } from "../meetingState.ts";
import type { AssistantStateName } from "../protocol.ts";
import { connectionStatus } from "./ControlBar.tsx";

/** 演示时只放最近这几条，字大到会议室后排也看得清 */
const SHOWN_LINES = 3;

interface Props {
  title: string;
  connection: ConnectionState;
  reconnectAttempt: number;
  captions: readonly CaptionLine[];
  assistantState: AssistantStateName;
  assistantText: string;
  onExit: () => void;
}

/**
 * 演示模式：投到会议室大屏上时用。整屏深色，只留会议标题、最近几条大字字幕和助理的状态与回答；
 * 进入时请求全屏，按 Esc 或「退出演示」回到工作区（浏览器自己退出全屏时也一并退出演示）。
 */
export function PresentationView({
  title,
  connection,
  reconnectAttempt,
  captions,
  assistantState,
  assistantText,
  onExit,
}: Props) {
  const { t } = useI18n();
  const exitRef = useRef(onExit);
  exitRef.current = onExit;

  useEffect(() => {
    // 全屏是锦上添花：浏览器不让（没有用户手势、iframe 里）就只铺满窗口
    let entered = false;
    const root = document.documentElement;
    if (root.requestFullscreen && !document.fullscreenElement) {
      root.requestFullscreen().then(
        () => {
          entered = true;
        },
        () => undefined,
      );
    }
    // 全屏时按 Esc 被浏览器拿去退出全屏，不会触发 keydown：靠 fullscreenchange 跟着退出演示
    const onChange = () => {
      if (entered && !document.fullscreenElement) exitRef.current();
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") exitRef.current();
    };
    document.addEventListener("fullscreenchange", onChange);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("fullscreenchange", onChange);
      window.removeEventListener("keydown", onKey);
      if (entered && document.fullscreenElement)
        void document.exitFullscreen().catch(() => undefined);
    };
  }, []);

  const status = connectionStatus(t, connection, reconnectAttempt);
  const lines = captions.filter((line) => line.stable || line.unstable).slice(-SHOWN_LINES);
  const assistantActive = assistantState !== "idle" || assistantText !== "";

  return (
    <div
      className="dark fixed inset-0 z-40 flex flex-col bg-background text-foreground"
      role="dialog"
      aria-modal="true"
      aria-label={t.assistant.presentation.label}
    >
      <header className="flex items-center gap-3 border-b px-6 py-3">
        <MicIcon aria-hidden className="size-4" />
        <span className="text-sm font-medium">{t.common.appName}</span>
        <span className="flex-1" />
        <span className={cn("inline-flex items-center gap-1.5 text-xs font-medium", status.tone)}>
          <span aria-hidden className="size-1.5 rounded-full bg-current" />
          {status.label}
        </span>
        <Button variant="ghost" autoFocus onClick={onExit}>
          <Minimize2Icon data-icon="inline-start" />
          {t.assistant.presentation.exit}
        </Button>
      </header>
      <main className="flex min-h-0 flex-1 flex-col gap-5 overflow-hidden px-6 py-8 md:px-12">
        <h2 className="text-2xl font-semibold text-muted-foreground md:text-3xl">{title}</h2>
        <section
          className="flex min-h-0 flex-1 flex-col justify-end gap-4"
          aria-label={t.assistant.presentation.captions}
        >
          {lines.length === 0 ? (
            <p className="text-2xl text-subtle-foreground">{t.assistant.presentation.empty}</p>
          ) : (
            lines.map((line, index) => {
              const latest = index === lines.length - 1;
              return (
                <article
                  key={line.segmentId ?? `u${line.utteranceId ?? index}`}
                  className={`rounded-2xl px-6 py-5 ${
                    line.source === "assistant" ? "bg-agent" : "bg-card"
                  }`}
                >
                  <p
                    className={`mb-2 text-base font-medium ${
                      line.source === "assistant"
                        ? "text-agent-foreground"
                        : "text-client-foreground"
                    }`}
                  >
                    {line.speakerName}
                  </p>
                  <p
                    className={`leading-snug font-semibold [overflow-wrap:anywhere] ${
                      latest
                        ? "text-3xl md:text-4xl xl:text-5xl"
                        : "text-2xl text-muted-foreground md:text-3xl"
                    }`}
                  >
                    {line.stable}
                    <span className="text-muted-foreground">{line.unstable}</span>
                  </p>
                </article>
              );
            })
          )}
        </section>
        {assistantActive && (
          <div
            className="flex items-center gap-5 rounded-2xl bg-agent px-6 py-4 text-agent-foreground"
            role="status"
          >
            <span aria-hidden className="flex w-12 shrink-0 justify-center">
              <AudioVisualizerBar
                participantType="bot"
                barCount={5}
                barWidth={4}
                barGap={4}
                barMaxHeight={28}
                barColor="--agent-foreground"
                isThinking={assistantState === "thinking"}
                thinkingHeight={0.6}
              />
            </span>
            <p className="line-clamp-2 text-2xl font-semibold md:text-3xl">
              {assistantText || t.assistant.presentation.states[assistantState]}
            </p>
          </div>
        )}
        <p className="flex items-center gap-1.5 text-xs text-subtle-foreground">
          <Kbd>Esc</Kbd> {t.assistant.presentation.hint}
        </p>
      </main>
    </div>
  );
}
