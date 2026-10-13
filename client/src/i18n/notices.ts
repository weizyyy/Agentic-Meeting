import { defineMessages } from "./define.ts";

/** 页面自己产生的提示：操作失败、连接问题、会议被关掉的原因 */
export const notices = defineMessages(
  {
    failed: (action: string, reason: string) => `${action}：${reason}`,
    actions: {
      listSessions: "读取会议列表失败",
      loadSession: "读取会议内容失败",
      syncSession: "同步会议内容失败",
      endSession: "结束会议失败",
      loadOlder: "加载更早的发言失败",
      rename: "重命名失败",
      keep: "修改保留标记失败",
      delete: "删除失败",
      renameSpeaker: "改名失败",
      assign: "修改发言人失败",
      report: "生成报告失败",
      merge: "合并说话人失败",
      send: "发送失败",
      loadTask: "读取任务失败",
      cancelTask: "取消任务失败",
    },
    micUnavailable: (reason: string) => `麦克风不可用：${reason}`,
    connectFailed: (reason: string) => `连接失败：${reason}`,
    reconnectGaveUp: "自动重连没有成功。网络或服务恢复之后，点会议横幅上的「继续」接着开这场会议。",
    notConnected: "还没有连接，请先点「开始新会议」",
    micInterrupted: (span: string) =>
      `收音中断了 ${span}（锁屏、切到后台或来电时系统会停掉麦克风），这段时间说的话没有记下来。`,
    closed: {
      taken_over:
        "已在另一台设备（或另一个页面）上继续这场会议，这个页面已断开。同一时刻只能有一路连接。",
      ended: "会议已结束。",
      server_stopping: "服务端正在停止，连接已断开；它重新起来之后会自动接着开这场会议。",
    },
    serverError: (error: string) => `服务端出错：${error}`,
    serverErrorUnknown: "服务端出错，连接可能已经中断",
    unreachable: "无法连接到服务端",
    requestFailed: (status: number) => `请求失败（${status}）`,
  },
  {
    failed: (action: string, reason: string) => `${action}: ${reason}`,
    actions: {
      listSessions: "Could not load the meeting list",
      loadSession: "Could not load the meeting",
      syncSession: "Could not sync the meeting",
      endSession: "Could not end the meeting",
      loadOlder: "Could not load earlier captions",
      rename: "Could not rename",
      keep: "Could not change the keep flag",
      delete: "Could not delete",
      renameSpeaker: "Could not rename the speaker",
      assign: "Could not change the speaker",
      report: "Could not generate the report",
      merge: "Could not merge the speakers",
      send: "Could not send",
      loadTask: "Could not load the task",
      cancelTask: "Could not cancel the task",
    },
    micUnavailable: (reason: string) => `Microphone unavailable: ${reason}`,
    connectFailed: (reason: string) => `Could not connect: ${reason}`,
    reconnectGaveUp:
      "Reconnecting did not succeed. Once the network or the server is back, click Resume on the meeting banner to carry on with this meeting.",
    notConnected: "Not connected yet. Click Start meeting first",
    micInterrupted: (span: string) =>
      `The microphone was cut off for ${span} (the system stops it when the screen locks, the page goes to the background or a call comes in); nothing said in that time was recorded.`,
    closed: {
      taken_over:
        "This meeting continued on another device (or page), so this page was disconnected. Only one connection is allowed at a time.",
      ended: "The meeting has ended.",
      server_stopping:
        "The server is stopping and the connection was closed; the meeting continues automatically once the server is back.",
    },
    serverError: (error: string) => `Server error: ${error}`,
    serverErrorUnknown: "Server error; the connection may have dropped",
    unreachable: "Cannot reach the server",
    requestFailed: (status: number) => `Request failed (${status})`,
  },
);
