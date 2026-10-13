import { ArrowUpIcon } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { useI18n } from "../i18n/index.ts";
import { MAX_TEXT_CHARS, keyAction, remainingChars } from "../textInput.ts";

interface Props {
  /** 已连接才能发 */
  enabled: boolean;
  /** 发出去返回 true，输入框随即清空 */
  onSend: (text: string) => boolean;
}

/** 文字输入框：不方便出声时打字向助理提问或委托任务。回车发送，Shift+回车换行；助理只用文字回答，不会出声。 */
export function TextInput({ enabled, onSend }: Props) {
  const { t } = useI18n();
  const [text, setText] = useState("");
  const left = remainingChars(text);

  const send = () => {
    if (onSend(text)) setText("");
  };

  return (
    <form
      className="rounded-lg border bg-background transition-colors focus-within:border-ring focus-within:ring-3 focus-within:ring-ring/50 has-disabled:bg-muted/50 dark:bg-input/30"
      onSubmit={(event) => {
        event.preventDefault();
        send();
      }}
    >
      <textarea
        className="block max-h-40 min-h-10 w-full resize-none bg-transparent px-3 pt-2 text-sm outline-none [field-sizing:content] placeholder:text-muted-foreground disabled:cursor-not-allowed"
        aria-label={t.assistant.input.label}
        placeholder={enabled ? t.assistant.input.placeholder : t.assistant.input.placeholderIdle}
        value={text}
        rows={2}
        disabled={!enabled}
        maxLength={MAX_TEXT_CHARS + 200}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          const action = keyAction({
            key: event.key,
            shiftKey: event.shiftKey,
            isComposing: event.nativeEvent.isComposing,
          });
          if (action === "send") {
            event.preventDefault();
            send();
          }
        }}
      />
      <div className="flex items-center gap-2 px-2 pb-2 pl-3 text-xs text-muted-foreground">
        {left !== null ? (
          <span className={left < 0 ? "text-destructive" : undefined}>
            {left >= 0 ? t.assistant.input.remaining(left) : t.assistant.input.over(-left)}
          </span>
        ) : (
          <span className="flex items-center gap-1">
            <Kbd>Enter</Kbd> {t.assistant.input.send} <Kbd>Shift+Enter</Kbd>{" "}
            {t.assistant.input.newline}
          </span>
        )}
        <Button
          type="submit"
          size="icon-sm"
          className="ml-auto"
          aria-label={t.assistant.input.send}
          disabled={!enabled || !text.trim()}
        >
          <ArrowUpIcon />
        </Button>
      </div>
    </form>
  );
}
