// 按界面语言格式化时间和数字（Intl）。钟点一律 24 小时制，和会议里说的时间对得上。

import type { Language } from "./define.ts";

const cache = new Map<string, Intl.DateTimeFormat>();

function dateFormat(language: Language, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${language}|${JSON.stringify(options)}`;
  let format = cache.get(key);
  if (!format) {
    format = new Intl.DateTimeFormat(language, options);
    cache.set(key, format);
  }
  return format;
}

/** Unix 秒 → 本地的日期和时刻：「10月8日 14:03」「Oct 8, 14:03」。 */
export function formatDateTime(language: Language, unixSecs: number): string {
  return dateFormat(language, {
    month: language === "en" ? "short" : "long",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(new Date(unixSecs * 1000));
}

/** Unix 秒 → 本地钟点「14:03:05」。 */
export function formatTimeOfDay(language: Language, unixSecs: number): string {
  return dateFormat(language, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).format(new Date(unixSecs * 1000));
}

/** 整数加千分位：「1,234」。 */
export function formatNumber(language: Language, value: number): string {
  return new Intl.NumberFormat(language).format(value);
}
