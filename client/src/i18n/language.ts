// 界面语言：默认跟随浏览器，页面上选过就记在这个浏览器里（与 theme.ts 的做法相同）。
// 会议语言（提示词、唤醒词、识别）不归这里管。

import { LANGUAGES, type Language } from "./define.ts";

export type LanguageChoice = "system" | Language;

export const LANGUAGE_CHOICES: readonly LanguageChoice[] = ["system", ...LANGUAGES];

/** 存在 localStorage 里的键；只是这个浏览器的偏好，不上传服务端。 */
const STORAGE_KEY = "agentic-meeting.language";

function isLanguageChoice(value: unknown): value is LanguageChoice {
  return typeof value === "string" && (LANGUAGE_CHOICES as readonly string[]).includes(value);
}

/** 按浏览器的语言偏好挑：按顺序找第一个中文或英文；都不是就用英文。 */
export function browserLanguage(preferred: readonly string[]): Language {
  for (const tag of preferred) {
    if (/^zh(-|$)/i.test(tag)) return "zh-CN";
    if (/^en(-|$)/i.test(tag)) return "en";
  }
  return "en";
}

function navigatorLanguages(): readonly string[] {
  if (typeof navigator === "undefined") return [];
  return navigator.languages?.length ? navigator.languages : [navigator.language];
}

/** 读保存的选择；没存过、存的值不认识或浏览器不让用存储时都是跟随浏览器。 */
export function loadLanguage(): LanguageChoice {
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    return isLanguageChoice(saved) ? saved : "system";
  } catch {
    return "system";
  }
}

let choice: LanguageChoice = "system";
let language: Language = "zh-CN";
const listeners = new Set<() => void>();

function render(): void {
  const next = choice === "system" ? browserLanguage(navigatorLanguages()) : choice;
  document.documentElement.lang = next;
  if (next === language) return;
  language = next;
  for (const listener of listeners) listener();
}

/** 页面启动时调用一次：按保存的选择显示，跟随浏览器时随浏览器的语言设置切换。 */
export function initLanguage(): void {
  choice = loadLanguage();
  language = choice === "system" ? browserLanguage(navigatorLanguages()) : choice;
  document.documentElement.lang = language;
  window.addEventListener("languagechange", render);
}

/** 应用到页面并保存；存不下（隐私模式等）时只对当前页面生效。 */
export function setLanguage(next: LanguageChoice): void {
  choice = next;
  try {
    if (next === "system") window.localStorage.removeItem(STORAGE_KEY);
    else window.localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // 存储不可用：这次的选择照样生效，只是刷新后回到跟随浏览器
  }
  render();
  // 选择本身变了（比如从「中文」换成「跟随浏览器」而语言没变）也要让菜单更新
  for (const listener of listeners) listener();
}

export function currentLanguage(): Language {
  return language;
}

export function currentChoice(): LanguageChoice {
  return choice;
}

export function subscribeLanguage(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
