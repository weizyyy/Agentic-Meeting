import { defineMessages } from "./define.ts";

// 服务端发来的提示（docs/interfaces.md §6.1.1）：服务端只给代码和参数，这里按界面语言翻译。
// 新代码先写进 interfaces.md 的表，再加到 PARAMS 和下面的目录里，服务端的表在 src/agentic_meeting/messages.py；
// tests/test_repository_rules.py 检查三处的代码一致。不认得的代码、参数不对的，页面显示服务端给的中文 text。

type Kind = "string" | "number";

/** 每个代码带的参数及其类型：收到的参数对不上就不翻译。 */
export const NOTICE_PARAMS = {
  "recording.active": {},
  "mic.low_level": {},
  "asr.unavailable": {},
  "asr.recovered": {},
  "diar.unavailable": { error: "string" },
  "diar.failed": { error: "string" },
  "speakers.renumbered": {},
  "llm.unavailable": { reason: "string", status: "number", detail: "string" },
  "tts.unavailable": { reason: "string", status: "number", detail: "string" },
  "text.malformed": {},
  "text.empty": {},
  "text.too_long": { max: "number" },
  "text.queue_full": { max: "number" },
  "text.queued": { position: "number" },
} as const satisfies Record<string, Record<string, Kind>>;

export type NoticeCode = keyof typeof NOTICE_PARAMS;

/** llm.unavailable / tts.unavailable 的 reason */
export const NOTICE_REASONS = [
  "auth",
  "not_found",
  "rate_limited",
  "server_error",
  "timeout",
  "connection",
  "other",
] as const;

export const serverNotices = defineMessages(
  {
    codes: {
      "recording.active": "会议正在转录，发言和共享画面会保存在服务器上",
      "mic.low_level": "麦克风音量过低，请调高系统的输入音量或靠近麦克风",
      "asr.unavailable": "识别服务暂时不可用，正在重试",
      "asr.recovered": "识别服务已恢复",
      "diar.unavailable": (error: string) =>
        `说话人区分不可用，所有发言将记为「未知」，不分说话人：${error}`,
      "diar.failed": (error: string) => `说话人区分出错，之后的发言将记为「未知」：${error}`,
      "speakers.renumbered":
        "服务重启过，说话人编号可能与之前不同；认错的可以在说话人那里用「合并到…」收拾",
      "llm.unavailable": (reason: string) => `助理暂不可用：${reason}`,
      "tts.unavailable": (reason: string) => `语音不可用，助理这次只出文字：${reason}`,
      "text.malformed": "文字消息的格式不对，没有发送",
      "text.empty": "消息是空的，没有发送",
      "text.too_long": (max: number) => `消息太长（最多 ${max} 字），没有发送`,
      "text.queue_full": (max: number) => `助理正忙，排队已满（${max} 条），这条消息没有发送`,
      "text.queued": (position: number) => `助理正忙，这条消息排在第 ${position} 位，稍后回答`,
    },
    reasons: {
      auth: "密钥被拒绝，请检查配置里的密钥变量",
      not_found: "接口地址或模型名不对（404）",
      rate_limited: "被服务端限流了，稍后再试",
      server_error: (status: number) => `服务端出错（${status}）`,
      timeout: "请求超时",
      connection: "连不上服务",
    },
  },
  {
    codes: {
      "recording.active":
        "This meeting is being transcribed; speech and shared screens are saved on the server.",
      "mic.low_level":
        "The microphone level is too low. Turn up the system input volume or move closer to the microphone.",
      "asr.unavailable": "Speech recognition is unavailable; retrying.",
      "asr.recovered": "Speech recognition is back.",
      "diar.unavailable": (error) =>
        `Speaker identification is unavailable; speech will not be attributed to speakers: ${error}`,
      "diar.failed": (error) =>
        `Speaker identification failed; further speech will not be attributed to speakers: ${error}`,
      "speakers.renumbered":
        "The server restarted, so speaker numbers may differ from before. Use Merge into… on a speaker to fix any mix-ups.",
      "llm.unavailable": (reason) => `The assistant is unavailable: ${reason}`,
      "tts.unavailable": (reason) =>
        `Voice is unavailable; the assistant answers in text this time: ${reason}`,
      "text.malformed": "The message was malformed and was not sent.",
      "text.empty": "The message is empty and was not sent.",
      "text.too_long": (max) =>
        `The message is too long (at most ${max} characters) and was not sent.`,
      "text.queue_full": (max) =>
        `The assistant is busy and the queue is full (${max} messages); this message was not sent.`,
      "text.queued": (position) =>
        `The assistant is busy; this message is number ${position} in the queue and will be answered shortly.`,
    },
    reasons: {
      auth: "the key was rejected; check the key variable in the configuration",
      not_found: "wrong endpoint or model name (404)",
      rate_limited: "rate limited by the server; try again later",
      server_error: (status) => `server error (${status})`,
      timeout: "the request timed out",
      connection: "cannot reach the service",
    },
  },
);

type Catalog = (typeof serverNotices)["en"];
export type NoticeParams = Readonly<Record<string, string | number>>;

function paramsMatch(code: NoticeCode, params: NoticeParams): boolean {
  const spec: Readonly<Record<string, Kind>> = NOTICE_PARAMS[code];
  return Object.entries(spec).every(([name, kind]) => typeof params[name] === kind);
}

function reasonText(t: Catalog, params: NoticeParams): string | null {
  const { status, detail } = params as { status: number; detail: string };
  const reason = NOTICE_REASONS.find((r) => r === params.reason);
  switch (reason) {
    case undefined:
      return null;
    case "other":
      return detail;
    case "server_error":
      return t.reasons.server_error(status);
    default:
      return t.reasons[reason];
  }
}

/** 按界面语言翻译一条服务端提示；不认得的代码或参数对不上时返回 null，由调用方退回服务端的 text。 */
export function serverNoticeText(
  t: Catalog,
  code: string | null,
  params: NoticeParams,
): string | null {
  if (code === null || !Object.hasOwn(NOTICE_PARAMS, code)) return null;
  const known = code as NoticeCode;
  if (!paramsMatch(known, params)) return null;
  const p = params as Record<string, never>;
  switch (known) {
    case "llm.unavailable":
    case "tts.unavailable": {
      const reason = reasonText(t, params);
      return reason === null ? null : t.codes[known](reason);
    }
    case "diar.unavailable":
    case "diar.failed":
      return t.codes[known](p.error);
    case "text.too_long":
    case "text.queue_full":
      return t.codes[known](p.max);
    case "text.queued":
      return t.codes[known](p.position);
    default:
      return t.codes[known];
  }
}
