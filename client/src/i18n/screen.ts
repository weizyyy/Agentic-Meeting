import { defineMessages } from "./define.ts";

/** 屏幕共享和截图时间线 */
export const screen = defineMessages(
  {
    title: "屏幕",
    sharing: "共享中",
    share: "共享屏幕",
    stop: "停止共享",
    emptySharing: "共享屏幕后，画面有变化时会自动截图，并出现在这里。",
    emptyIdle: "会议进行中可以共享屏幕，截图会出现在这里。",
    emptyNoShare: "这个浏览器不能共享屏幕；在电脑上共享的画面会出现在这里。",
    meetingTime: (time: string) => `会议进行到 ${time}`,
    frameAlt: (time: string) => `${time} 的屏幕截图`,
    screenshot: "屏幕截图",
    noCaption: "（还没有画面摘要）",
    previous: "上一张",
    next: "下一张",
    position: (index: number, total: number) => `${index} / ${total} · ← → 切换`,
    unsupported: "这个浏览器不能共享屏幕（需要桌面浏览器，并通过 HTTPS 或 localhost 访问）",
    shareFailed: (reason: string) => `无法共享屏幕：${reason}`,
    uploadFailed: (reason: string) => `截图上传失败：${reason}`,
    encodeFailed: "浏览器无法把画面编码成图片",
  },
  {
    title: "Screen",
    sharing: "Sharing",
    share: "Share screen",
    stop: "Stop sharing",
    emptySharing:
      "While you share, a screenshot is taken whenever the picture changes and appears here.",
    emptyIdle: "During a meeting you can share your screen; screenshots appear here.",
    emptyNoShare:
      "This browser cannot share the screen; screens shared from a computer appear here.",
    meetingTime: (time: string) => `Meeting time ${time}`,
    frameAlt: (time: string) => `Screenshot at ${time}`,
    screenshot: "Screenshot",
    noCaption: "(No description yet)",
    previous: "Previous",
    next: "Next",
    position: (index: number, total: number) => `${index} / ${total} · ← → to switch`,
    unsupported:
      "This browser cannot share the screen (it needs a desktop browser and an HTTPS or localhost address)",
    shareFailed: (reason: string) => `Could not share the screen: ${reason}`,
    uploadFailed: (reason: string) => `Screenshot upload failed: ${reason}`,
    encodeFailed: "The browser could not encode the picture as an image",
  },
);
