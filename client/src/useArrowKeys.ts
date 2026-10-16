import { useEffect, type RefObject } from "react";

/** 方向键在这些地方本来就有用处：输入框里移光标，分页、菜单、列表里换项，对话框和演示视图自己处理 */
const OWN_ARROWS = [
  "input",
  "textarea",
  "select",
  '[contenteditable]:not([contenteditable="false"])',
  ...[
    "tab",
    "tablist",
    "menu",
    "menubar",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "listbox",
    "option",
    "combobox",
    "slider",
    "spinbutton",
    "radio",
    "radiogroup",
    "tree",
    "treeitem",
    "grid",
    "dialog",
    "alertdialog",
  ].map((role) => `[role="${role}"]`),
].join(", ");

const ARROWS: Record<string, "up" | "down" | "left" | "right"> = {
  ArrowUp: "up",
  ArrowDown: "down",
  ArrowLeft: "left",
  ArrowRight: "right",
};

/**
 * 页面上没有哪个控件要用这次方向键时，返回方向；否则返回 null。
 * 焦点在按钮上不算：方向键对按钮没有用处，浏览器只会拿它去滚页面。
 */
export function pageArrow(event: KeyboardEvent): "up" | "down" | "left" | "right" | null {
  if (event.defaultPrevented || event.isComposing) return null;
  // 带修饰键的组合留给浏览器（Alt+← 是后退，Shift+方向键是扩选文字）
  if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return null;
  const arrow = ARROWS[event.key];
  if (!arrow) return null;
  if (event.target instanceof Element && event.target.closest(OWN_ARROWS)) return null;
  // 对话框、演示视图开着时，后面的页面不归方向键管
  if (document.querySelector('[role="dialog"], [role="alertdialog"]')) return null;
  return arrow;
}

/** 元素现在显示在页面上：不在没选中的分页里，手机上也不在藏起来的那几块里 */
export function isShown(el: Element | null): el is HTMLElement {
  return el !== null && el.getClientRects().length > 0;
}

/** 浏览器按一下方向键滚动的距离 */
const ARROW_STEP_PX = 40;

/**
 * 没有选中控件时，上下方向键滚动这块区域（它显示着的时候）。
 * 焦点已经在区域里面时浏览器自己会滚，这里不再插手。
 */
export function useArrowScroll(ref: RefObject<HTMLElement | null>): void {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const arrow = pageArrow(event);
      if (arrow !== "up" && arrow !== "down") return;
      const el = ref.current;
      if (!isShown(el) || (event.target instanceof Node && el.contains(event.target))) return;
      event.preventDefault();
      el.scrollBy({ top: arrow === "up" ? -ARROW_STEP_PX : ARROW_STEP_PX });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [ref]);
}
