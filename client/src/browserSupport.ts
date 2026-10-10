/**
 * 浏览器能不能开会（docs/user-guide.md「支持的浏览器」）。
 *
 * 最低版本同时是 vite.config.ts 里的构建目标：比它旧的浏览器连脚本都解析不了，由 index.html 里的兜底脚本提示。
 * 这里只查版本够新、但开会要用的接口拿不到的情况，最常见的是没通过 HTTPS 打开。
 */

/** 支持的浏览器及最低版本；`target` 是给构建工具的写法。 */
export const MIN_BROWSERS = [
  { name: "Chrome", version: "111", target: "chrome111" },
  { name: "Edge", version: "111", target: "edge111" },
  { name: "Firefox", version: "114", target: "firefox114" },
  { name: "Safari", version: "16.4", target: "safari16.4" },
] as const;

/** 「Chrome 111、Edge 111、Firefox 114、Safari 16.4」 */
export function minBrowsersText(): string {
  return MIN_BROWSERS.map((b) => `${b.name} ${b.version}`).join("、");
}

/** 只取用到的几个属性，方便在不同全局对象上检查。 */
export interface BrowserEnv {
  isSecureContext: boolean;
  RTCPeerConnection?: unknown;
  AudioContext?: unknown;
  navigator: { mediaDevices?: { getUserMedia?: unknown } };
}

/** 能开会返回 null；否则返回给用户看的原因（只读地看历史会议不受影响）。 */
export function meetingUnsupportedReason(env: BrowserEnv = window): string | null {
  // 不是 HTTPS 也不是 localhost 时浏览器直接不给麦克风接口，先说这个，换浏览器没用。
  if (!env.isSecureContext) {
    return "这个页面不是通过 HTTPS 或 localhost 打开的，浏览器不允许使用麦克风，不能开始或继续会议；历史会议仍可查看。请改用 HTTPS 地址打开。";
  }
  const missing: string[] = [];
  if (typeof env.RTCPeerConnection !== "function") missing.push("WebRTC");
  if (typeof env.navigator.mediaDevices?.getUserMedia !== "function") missing.push("麦克风采集");
  if (typeof env.AudioContext !== "function") missing.push("Web Audio");
  if (missing.length === 0) return null;
  return `这个浏览器缺少${missing.join("、")}接口，不能开始或继续会议；历史会议仍可查看。请使用 ${minBrowsersText()} 或更新的版本，并检查浏览器设置或扩展是否关掉了这些功能。`;
}
