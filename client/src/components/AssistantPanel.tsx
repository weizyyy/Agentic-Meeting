import { MessageSquareTextIcon, Volume2Icon, XIcon } from "lucide-react";

import { AudioVisualizerBar } from "@/components/pipecat/audio-visualizer-bar";
import { Button } from "@/components/ui/button";
import { noticeText, useI18n } from "../i18n/index.ts";
import type { Notice, ReplyMode } from "../meetingState.ts";
import type { AssistantStateName, NoticeLevel } from "../protocol.ts";
import { TextInput } from "./TextInput.tsx";

/** 状态条的底色：待命是灰的，在听、思考、朗读时是助理的紫色 */
const STATE_TONES: Record<AssistantStateName, string> = {
  idle: "bg-muted text-muted-foreground",
  listening: "bg-agent text-agent-foreground",
  thinking: "bg-agent text-agent-foreground",
  speaking: "bg-agent text-agent-foreground",
};

const NOTICE_TONES: Record<NoticeLevel, string> = {
  info: "bg-muted text-foreground",
  warn: "bg-warning text-warning-foreground",
  error: "bg-destructive/10 text-destructive",
};

interface Props {
  state: AssistantStateName;
  text: string;
  replyMode: ReplyMode | null;
  notices: readonly Notice[];
  /** 已连接或在旁观：可以用文字输入框向助理提问 */
  canSend: boolean;
  /** 旁观另一台设备上进行的会议：回答记进字幕，不在这里流出来 */
  watching?: boolean;
  onSend: (text: string) => boolean;
  onDismissNotice: (id: number) => void;
}

/** 助理区：状态、这一轮应答的流式文字（以及是文字回答还是语音回答）、需要让用户知道的提示、文字输入框。 */
export function AssistantPanel({
  state,
  text,
  replyMode,
  notices,
  canSend,
  watching = false,
  onSend,
  onDismissNotice,
}: Props) {
  const { t } = useI18n();
  return (
    <section
      className="flex flex-1 flex-col rounded-xl border bg-card"
      aria-label={t.assistant.title}
    >
      <div className="flex items-center gap-2 px-4 pt-3 pb-2">
        <h2 className="text-sm font-medium">{t.assistant.title}</h2>
        {text && replyMode && (
          <span className="ml-auto inline-flex items-center gap-1 text-xs text-agent-foreground">
            {replyMode === "voice" ? (
              <Volume2Icon aria-hidden className="size-3.5" />
            ) : (
              <MessageSquareTextIcon aria-hidden className="size-3.5" />
            )}
            {replyMode === "voice" ? t.assistant.voiceReply : t.assistant.textReply}
          </span>
        )}
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-2 px-4 pb-4">
        <p
          className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium ${STATE_TONES[state]}`}
        >
          <span aria-hidden className="flex w-8 justify-center">
            <AudioVisualizerBar
              participantType="bot"
              barCount={5}
              barWidth={3}
              barGap={3}
              barMaxHeight={16}
              barColor={state === "idle" ? "--muted-foreground" : "--agent-foreground"}
              isThinking={state === "thinking"}
              thinkingHeight={0.6}
            />
          </span>
          {t.assistant.states[state]}
        </p>
        {notices.length > 0 && (
          <ul className="flex flex-col gap-1.5">
            {notices.map((notice) => (
              <li
                key={notice.id}
                className={`flex items-start gap-2 rounded-lg py-1.5 pr-1.5 pl-3 text-sm ${NOTICE_TONES[notice.level]}`}
              >
                <span className="min-w-0 flex-1 py-0.5">{noticeText(t, notice)}</span>
                <Button
                  variant="ghost"
                  size="icon-xs"
                  className="text-current hover:bg-current/10 hover:text-current"
                  aria-label={t.assistant.dismissNotice}
                  onClick={() => onDismissNotice(notice.id)}
                >
                  <XIcon />
                </Button>
              </li>
            ))}
          </ul>
        )}
        {/* 回答至少留四行；再长只在框里滚动，不把面板撑高 */}
        <div className="min-h-[6.5em] flex-1 basis-0 overflow-y-auto text-sm leading-relaxed">
          {text ? (
            <p className="whitespace-pre-wrap">{text}</p>
          ) : (
            <p className="text-subtle-foreground">
              {watching ? t.assistant.emptyWatching : t.assistant.empty}
            </p>
          )}
        </div>
        <TextInput enabled={canSend} watching={watching} onSend={onSend} />
      </div>
    </section>
  );
}
