import { useState } from "react";

import { MAX_TEXT_CHARS, keyAction, remainingChars } from "../textInput.ts";

interface Props {
  /** 已连接才能发 */
  enabled: boolean;
  /** 发出去返回 true，输入框随即清空 */
  onSend: (text: string) => boolean;
}

/** 文字输入框：不方便出声时打字向助理提问或委托任务。回车发送，Shift+回车换行；助理只用文字回答，不会出声。 */
export function TextInput({ enabled, onSend }: Props) {
  const [text, setText] = useState("");
  const left = remainingChars(text);

  const send = () => {
    if (onSend(text)) setText("");
  };

  return (
    <form
      className="text-input"
      onSubmit={(event) => {
        event.preventDefault();
        send();
      }}
    >
      <textarea
        aria-label="给助理发文字消息"
        placeholder={
          enabled
            ? "打字提问，助理只回文字。回车发送，Shift+回车换行。"
            : "开始会议后，可以在这里打字向助理提问。"
        }
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
      <div className="text-input-row">
        {left !== null && (
          <span className={left < 0 ? "text-input-over" : "text-input-left"}>
            {left >= 0 ? `还能输入 ${left} 字` : `超出 ${-left} 字`}
          </span>
        )}
        <button type="submit" className="button button-start" disabled={!enabled || !text.trim()}>
          发送
        </button>
      </div>
    </form>
  );
}
