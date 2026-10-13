import { defineMessages } from "./define.ts";

/** 登录页和浏览器检查 */
export const login = defineMessages(
  {
    expired: "登录已过期",
    expiredHint: "请重新输入访问口令。会议不受影响。",
    prompt: "请输入访问口令。",
    password: "访问口令",
    submit: "登录",
    busy: "登录中…",
    tooManyIn: (minutes: number) => `尝试次数过多，请 ${minutes} 分钟后再试`,
    tooMany: "尝试次数过多，请稍后再试",
    failed: (status: number) => `登录失败（${status}）`,
    statusFailed: (status: number) => `读取登录状态失败（${status}）`,
    insecure:
      "这个页面不是通过 HTTPS 或 localhost 打开的，浏览器不允许使用麦克风，不能开始或继续会议；历史会议仍可查看。请改用 HTTPS 地址打开。",
    missing: (features: string, browsers: string) =>
      `这个浏览器缺少${features}接口，不能开始或继续会议；历史会议仍可查看。请使用 ${browsers} 或更新的版本，并检查浏览器设置或扩展是否关掉了这些功能。`,
    microphoneCapture: "麦克风采集",
  },
  {
    expired: "Session expired",
    expiredHint: "Enter the access password again. The meeting is not affected.",
    prompt: "Enter the access password.",
    password: "Access password",
    submit: "Log in",
    busy: "Logging in…",
    tooManyIn: (minutes: number) =>
      `Too many attempts. Try again in ${minutes} ${minutes === 1 ? "minute" : "minutes"}`,
    tooMany: "Too many attempts. Try again later",
    failed: (status: number) => `Login failed (${status})`,
    statusFailed: (status: number) => `Could not read the login status (${status})`,
    insecure:
      "This page was not opened over HTTPS or localhost, so the browser does not allow the microphone and meetings cannot be started or continued; past meetings can still be viewed. Open the HTTPS address instead.",
    missing: (features: string, browsers: string) =>
      `This browser lacks the ${features} interfaces, so meetings cannot be started or continued; past meetings can still be viewed. Use ${browsers} or newer, and check whether browser settings or extensions turned these features off.`,
    microphoneCapture: "microphone capture",
  },
);
