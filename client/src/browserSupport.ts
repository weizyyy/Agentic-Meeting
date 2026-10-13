/**
 * 浏览器能不能开会（docs/user-guide.md「支持的浏览器」）。
 *
 * 最低版本同时是 vite.config.ts 里的构建目标：比它旧的浏览器连脚本都解析不了，由 index.html 里的兜底脚本提示。
 * 这里只查版本够新、但开会要用的接口拿不到的情况，最常见的是没通过 HTTPS 打开。
 */

import type { Messages } from "./i18n/index.ts";

/** 支持的浏览器及最低版本；`target` 是给构建工具的写法。 */
export const MIN_BROWSERS = [
  { name: "Chrome", version: "111", target: "chrome111" },
  { name: "Edge", version: "111", target: "edge111" },
  { name: "Firefox", version: "128", target: "firefox128" },
  { name: "Safari", version: "16.4", target: "safari16.4" },
] as const;

/** 「Chrome 111、Edge 111、Firefox 128、Safari 16.4」 */
export function minBrowsersText(separator: string): string {
  return MIN_BROWSERS.map((b) => `${b.name} ${b.version}`).join(separator);
}

/** 只取用到的几个属性，方便在不同全局对象上检查。 */
export interface BrowserEnv {
  isSecureContext: boolean;
  RTCPeerConnection?: unknown;
  AudioContext?: unknown;
  navigator: { mediaDevices?: { getUserMedia?: unknown } };
}

/** 缺的接口：WebRTC、麦克风采集、Web Audio */
export type MissingFeature = "webrtc" | "microphone" | "webaudio";

/** 不能开会的原因：不是安全来源，或缺接口。显示时按界面语言写成文字（unsupportedText）。 */
export type UnsupportedReason =
  { kind: "insecure" } | { kind: "missing"; features: readonly MissingFeature[] };

/** 能开会返回 null；否则返回原因（只读地看历史会议不受影响）。 */
export function meetingUnsupportedReason(env: BrowserEnv = window): UnsupportedReason | null {
  // 不是 HTTPS 也不是 localhost 时浏览器直接不给麦克风接口，先说这个，换浏览器没用。
  if (!env.isSecureContext) return { kind: "insecure" };
  const features: MissingFeature[] = [];
  if (typeof env.RTCPeerConnection !== "function") features.push("webrtc");
  if (typeof env.navigator.mediaDevices?.getUserMedia !== "function") features.push("microphone");
  if (typeof env.AudioContext !== "function") features.push("webaudio");
  return features.length === 0 ? null : { kind: "missing", features };
}

/** 给用户看的原因。 */
export function unsupportedText(t: Messages, reason: UnsupportedReason): string {
  if (reason.kind === "insecure") return t.login.insecure;
  const names = { webrtc: "WebRTC", microphone: t.login.microphoneCapture, webaudio: "Web Audio" };
  return t.login.missing(
    reason.features.map((f) => names[f]).join(t.common.listSeparator),
    minBrowsersText(t.common.listSeparator),
  );
}
