// 文字输入框的纯逻辑（docs/interfaces.md §6.2：text_input 不超过 2000 字、去掉首尾空白后不能为空）。

export const MAX_TEXT_CHARS = 2000;

export type Validation = { ok: true; text: string } | { ok: false; reason: string };

/** 校验要发给助理的文字；通过时返回去掉首尾空白的文本。长度按字符（不是 UTF-16 码元）数。 */
export function validateText(raw: string, max: number = MAX_TEXT_CHARS): Validation {
  const text = raw.trim();
  if (!text) return { ok: false, reason: "消息是空的，没有发送" };
  if ([...text].length > max) return { ok: false, reason: `消息太长（最多 ${max} 字），没有发送` };
  return { ok: true, text };
}

export type KeyAction = "send" | "newline" | "none";

/**
 * 输入框里按键的含义：回车发送，Shift+回车换行。
 * 中文输入法选字时按的回车是确认候选词，不能当成发送（`isComposing`）。
 */
export function keyAction(event: {
  key: string;
  shiftKey: boolean;
  isComposing: boolean;
}): KeyAction {
  if (event.key !== "Enter" || event.isComposing) return "none";
  return event.shiftKey ? "newline" : "send";
}

/** 字数接近上限时给个提示：返回「还能输入多少字」，离上限还远时返回 null。 */
export function remainingChars(
  raw: string,
  max: number = MAX_TEXT_CHARS,
  warnWithin = 200,
): number | null {
  const left = max - [...raw].length;
  return left <= warnWithin ? left : null;
}
