import { defineMessages } from "./define.ts";

/** 助理区、文字输入框、助理声音和演示模式 */
export const assistant = defineMessages(
  {
    title: "助理",
    states: { idle: "待命", listening: "在听", thinking: "思考中", speaking: "朗读中" },
    voiceReply: "语音回答",
    textReply: "文字回答",
    dismissNotice: "关闭提示",
    empty: "叫一声助理的名字，或在下面打字提问，它的回答会显示在这里。",
    emptyWatching: "会议在另一台设备上进行。在下面打字向助理提问，回答会出现在字幕里。",
    input: {
      label: "给助理发文字消息",
      placeholder: "打字向助理提问，它只用文字回答。",
      placeholderIdle: "开始会议后，可以在这里打字向助理提问。",
      placeholderWatching: "打字向助理提问，回答会出现在字幕里。",
      remaining: (n: number) => `还能输入 ${n} 字`,
      over: (n: number) => `超出 ${n} 字`,
      send: "发送",
      newline: "换行",
      empty: "消息是空的，没有发送",
      tooLong: (max: number) => `消息太长（最多 ${max} 字），没有发送`,
    },
    audioBlocked: "浏览器拦下了助理的声音。",
    enableAudio: "打开助理声音",
    presentation: {
      label: "演示模式",
      exit: "退出演示",
      captions: "实时字幕",
      empty: "说的话会实时显示在这里。",
      states: {
        idle: "",
        listening: "助理在听…",
        thinking: "助理在思考…",
        speaking: "助理在回答",
      },
      hint: "退出演示 · 字幕自动跟随最新",
    },
  },
  {
    title: "Assistant",
    states: { idle: "Idle", listening: "Listening", thinking: "Thinking", speaking: "Speaking" },
    voiceReply: "Spoken reply",
    textReply: "Text reply",
    dismissNotice: "Dismiss notice",
    empty: "Call the assistant by name or type a question below; its answer appears here.",
    emptyWatching:
      "The meeting is running on another device. Type a question below; the answer appears in the captions.",
    input: {
      label: "Text message to the assistant",
      placeholder: "Type a question; the assistant answers in text only.",
      placeholderIdle: "Once the meeting starts, you can type questions to the assistant here.",
      placeholderWatching: "Type a question; the answer appears in the captions.",
      remaining: (n: number) => `${n} ${n === 1 ? "character" : "characters"} left`,
      over: (n: number) => `${n} ${n === 1 ? "character" : "characters"} over`,
      send: "Send",
      newline: "new line",
      empty: "The message is empty and was not sent",
      tooLong: (max: number) =>
        `The message is too long (at most ${max} characters) and was not sent`,
    },
    audioBlocked: "The browser blocked the assistant's voice.",
    enableAudio: "Turn on the assistant's voice",
    presentation: {
      label: "Presentation mode",
      exit: "Exit presentation",
      captions: "Live captions",
      empty: "What people say appears here as they speak.",
      states: {
        idle: "",
        listening: "The assistant is listening…",
        thinking: "The assistant is thinking…",
        speaking: "The assistant is answering",
      },
      hint: "to exit · captions follow the latest",
    },
  },
);
