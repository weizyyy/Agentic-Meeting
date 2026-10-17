// 界面文案的入口：组件里用 useI18n() 取当前语言的文案（语言一换就重新渲染）；
// 不在渲染里、事后才生成的文字（出错提示等）用 messages() 取当时的语言。

import { useSyncExternalStore } from "react";

import { assistant } from "./assistant.ts";
import { captions } from "./captions.ts";
import { common } from "./common.ts";
import type { Language } from "./define.ts";
import { devices } from "./devices.ts";
import { header } from "./header.ts";
import { sharing } from "./sharing.ts";
import { login } from "./login.ts";
import { notices } from "./notices.ts";
import { report } from "./report.ts";
import { screen } from "./screen.ts";
import { serverErrors, serverErrorText } from "./serverErrors.ts";
import { serverFailures, serverFailureText } from "./serverFailures.ts";
import { serverNotices, serverNoticeText, type NoticeParams } from "./serverNotices.ts";
import { session } from "./session.ts";
import { tasks } from "./tasks.ts";
import {
  currentChoice,
  currentLanguage,
  subscribeLanguage,
  type LanguageChoice,
} from "./language.ts";

const areas = {
  common,
  header,
  session,
  captions,
  assistant,
  devices,
  tasks,
  screen,
  report,
  login,
  sharing,
  notices,
  serverNotices,
  serverErrors,
  serverFailures,
};

export type Messages = { readonly [K in keyof typeof areas]: (typeof areas)[K]["en"] };

function pick(language: Language): Messages {
  return Object.fromEntries(
    Object.entries(areas).map(([name, area]) => [name, area[language]]),
  ) as unknown as Messages;
}

const CATALOGS: Record<Language, Messages> = { "zh-CN": pick("zh-CN"), en: pick("en") };

export function messagesFor(language: Language): Messages {
  return CATALOGS[language];
}

/** 当前界面语言的文案；只给渲染以外的代码用。 */
export function messages(): Messages {
  return CATALOGS[currentLanguage()];
}

export interface I18n {
  t: Messages;
  language: Language;
  choice: LanguageChoice;
}

function snapshot(): string {
  return `${currentLanguage()}|${currentChoice()}`;
}

/** 组件里取文案：t 是当前语言的目录，language 给日期和数字格式化用。 */
export function useI18n(): I18n {
  useSyncExternalStore(subscribeLanguage, snapshot);
  const language = currentLanguage();
  return { t: CATALOGS[language], language, choice: currentChoice() };
}

/** 时长：不到一分钟、N 分钟、N 小时 NN 分。 */
export function formatDuration(t: Messages, secs: number): string {
  const minutes = Math.floor(Math.max(0, secs) / 60);
  if (minutes < 1) return t.common.duration.underMinute;
  if (minutes < 60) return t.common.duration.minutes(minutes);
  return t.common.duration.hoursMinutes(Math.floor(minutes / 60), minutes % 60);
}

/** 断了多久这类精确到秒的时长：40 秒、12 分钟、2 小时 05 分。 */
export function formatSpan(t: Messages, secs: number): string {
  const total = Math.max(0, Math.round(secs));
  if (total < 60) return t.common.duration.seconds(total);
  return formatDuration(t, total);
}

export type { Language } from "./define.ts";
export type { LanguageChoice } from "./language.ts";

/** 提示的文字：服务端的按当前语言翻译（认不出来就用服务端给的中文），页面自己的已经是生成时的语言。 */
export function noticeText(
  t: Messages,
  notice: { text: string; server?: { code: string | null; params: NoticeParams } },
): string {
  if (!notice.server) return notice.text;
  return serverNoticeText(t.serverNotices, notice.server.code, notice.server.params) ?? notice.text;
}

/** 服务端接口的错误响应 {"error", "code", "params"}（interfaces.md §5.9）：按当前语言翻译，认不出来就用 error 原文。 */
export function errorText(body: unknown): string | null {
  if (typeof body !== "object" || body === null) return null;
  const { error, code, params } = body as { error?: unknown; code?: unknown; params?: unknown };
  const translated = serverErrorText(messages().serverErrors, code, params);
  if (translated !== null) return translated;
  return typeof error === "string" && error ? error : null;
}

/** 任务和报告的失败原因（interfaces.md §5.10）：按当前语言翻译，认不出来就用 error 原文。 */
export function failureText(
  t: Messages,
  item: { error: string | null; error_code?: string | null; error_params?: unknown },
): string | null {
  return serverFailureText(t.serverFailures, item.error_code, item.error_params) ?? item.error;
}
