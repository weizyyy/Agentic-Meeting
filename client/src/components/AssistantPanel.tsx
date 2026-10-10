import type { Notice, ReplyMode } from "../meetingState.ts";
import type { AssistantStateName } from "../protocol.ts";
import { TextInput } from "./TextInput.tsx";

const STATE_LABELS: Record<AssistantStateName, string> = {
  idle: "待命",
  listening: "在听",
  thinking: "思考中",
  speaking: "朗读中",
};

const REPLY_LABELS: Record<ReplyMode, string> = {
  text: "文字回答",
  voice: "语音回答",
};

interface Props {
  state: AssistantStateName;
  text: string;
  replyMode: ReplyMode | null;
  notices: readonly Notice[];
  /** 已连接：可以用文字输入框向助理提问 */
  canSend: boolean;
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
  onSend,
  onDismissNotice,
}: Props) {
  return (
    <section className="panel" aria-label="助理">
      <h2>
        助理 <span className={`badge badge-${state}`}>{STATE_LABELS[state]}</span>
        {text && replyMode && <span className="reply-mode">{REPLY_LABELS[replyMode]}</span>}
      </h2>
      <ul className="notices">
        {notices.map((notice) => (
          <li key={notice.id} className={`notice notice-${notice.level}`}>
            <span>{notice.text}</span>
            <button type="button" aria-label="关闭提示" onClick={() => onDismissNotice(notice.id)}>
              ×
            </button>
          </li>
        ))}
      </ul>
      <div className="reply">
        {text ? (
          <p>{text}</p>
        ) : (
          <p className="empty">叫一声助理的名字，或在下面打字提问，它的回答会显示在这里。</p>
        )}
      </div>
      <TextInput enabled={canSend} onSend={onSend} />
    </section>
  );
}
