// 页面外观（浅色 / 深色）：默认跟随系统，页面上选过就记在这个浏览器里。
// 深色时 <html> 带 class="dark"（shadcn 的约定，index.css 里的深色变量挂在它上面）。

export type ThemeChoice = "system" | "light" | "dark";

export const THEME_CHOICES: readonly ThemeChoice[] = ["system", "light", "dark"];

export const THEME_LABELS: Record<ThemeChoice, string> = {
  system: "跟随系统",
  light: "浅色",
  dark: "深色",
};

/** 存在 localStorage 里的键；只是这个浏览器的偏好，不上传服务端。 */
const STORAGE_KEY = "agentic-meeting.theme";

const darkQuery = (): MediaQueryList | null =>
  typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-color-scheme: dark)")
    : null;

function isThemeChoice(value: unknown): value is ThemeChoice {
  return typeof value === "string" && (THEME_CHOICES as readonly string[]).includes(value);
}

/** 读保存的选择；没存过、存的值不认识或浏览器不让用存储时都是跟随系统。 */
export function loadTheme(): ThemeChoice {
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    return isThemeChoice(saved) ? saved : "system";
  } catch {
    return "system";
  }
}

let current: ThemeChoice = "system";

function render(): void {
  const dark = current === "dark" || (current === "system" && darkQuery()?.matches === true);
  document.documentElement.classList.toggle("dark", dark);
}

/** 页面启动时调用一次：按保存的选择显示，并在跟随系统时随系统切换。 */
export function initTheme(): void {
  current = loadTheme();
  render();
  darkQuery()?.addEventListener("change", render);
}

/** 应用到页面并保存；存不下（隐私模式等）时只对当前页面生效。 */
export function setTheme(choice: ThemeChoice): void {
  current = choice;
  render();
  try {
    if (choice === "system") window.localStorage.removeItem(STORAGE_KEY);
    else window.localStorage.setItem(STORAGE_KEY, choice);
  } catch {
    // 存储不可用：这次的选择照样生效，只是刷新后回到跟随系统
  }
}
