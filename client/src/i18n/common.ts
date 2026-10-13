import { defineMessages } from "./define.ts";

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** 好几个区域都用到的词：产品名、通用按钮、时长 */
export const common = defineMessages(
  {
    appName: "组会助理",
    close: "关闭",
    cancel: "取消",
    delete: "删除",
    saving: "保存中…",
    keep: "保留",
    kept: "已保留",
    keepHint: "免于自动清理；仍可人工删除",
    unknownError: "未知错误",
    /** 列表里两个词之间的分隔 */
    listSeparator: "、",
    duration: {
      underMinute: "不到 1 分钟",
      seconds: (n: number) => `${n} 秒`,
      minutes: (n: number) => `${n} 分钟`,
      hoursMinutes: (h: number, m: number) => `${h} 小时 ${String(m).padStart(2, "0")} 分`,
    },
  },
  {
    appName: "Agentic Meeting",
    close: "Close",
    cancel: "Cancel",
    delete: "Delete",
    saving: "Saving…",
    keep: "Keep",
    kept: "Kept",
    keepHint: "Exempt from automatic cleanup; can still be deleted by hand",
    unknownError: "unknown error",
    listSeparator: ", ",
    duration: {
      underMinute: "under a minute",
      seconds: (n: number) => plural(n, "second", "seconds"),
      minutes: (n: number) => plural(n, "minute", "minutes"),
      hoursMinutes: (h: number, m: number) => `${h} h ${String(m).padStart(2, "0")} min`,
    },
  },
);
