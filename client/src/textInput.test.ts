import assert from "node:assert/strict";
import { test } from "node:test";

import { MAX_TEXT_CHARS, keyAction, remainingChars, validateText } from "./textInput.ts";

test("校验：去首尾空白，空的拒绝", () => {
  assert.deepEqual(validateText("  帮我查一下论文 \n"), { ok: true, text: "帮我查一下论文" });
  for (const raw of ["", "   ", "\n\t "]) {
    const result = validateText(raw);
    assert.equal(result.ok, false);
  }
});

test("校验：上限按字符数算，恰好等于上限可以", () => {
  assert.equal(validateText("字".repeat(MAX_TEXT_CHARS)).ok, true);
  const tooLong = validateText("字".repeat(MAX_TEXT_CHARS + 1));
  assert.equal(tooLong.ok, false);
  if (!tooLong.ok) assert.match(tooLong.reason, /2000/);
  // 一个表情符号是两个 UTF-16 码元，但只算一个字
  assert.equal(validateText("😀".repeat(MAX_TEXT_CHARS)).ok, true);
  assert.equal(validateText("ab", 1).ok, false);
});

test("按键：回车发送，Shift+回车换行，输入法选字时的回车什么也不做", () => {
  assert.equal(keyAction({ key: "Enter", shiftKey: false, isComposing: false }), "send");
  assert.equal(keyAction({ key: "Enter", shiftKey: true, isComposing: false }), "newline");
  assert.equal(keyAction({ key: "Enter", shiftKey: false, isComposing: true }), "none");
  assert.equal(keyAction({ key: "Enter", shiftKey: true, isComposing: true }), "none");
  assert.equal(keyAction({ key: "a", shiftKey: false, isComposing: false }), "none");
});

test("快到上限时提示还能输入多少字", () => {
  assert.equal(remainingChars("你好"), null);
  assert.equal(remainingChars("字".repeat(MAX_TEXT_CHARS - 200)), 200);
  assert.equal(remainingChars("字".repeat(MAX_TEXT_CHARS)), 0);
  assert.equal(remainingChars("字".repeat(MAX_TEXT_CHARS + 5)), -5);
});
