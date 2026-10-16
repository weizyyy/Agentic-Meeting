import { defineMessages } from "./define.ts";

/** 登录页和浏览器检查 */
export const login = defineMessages(
  {
    expired: "登录已过期",
    expiredHint: "请重新输入访问口令。会议不受影响。",
    expiredSsoHint: "请重新登录。登录页会在新标签页里打开，登录后回到这里即可，会议不受影响。",
    prompt: "请输入访问口令。",
    promptEither: "请用单位账号登录，或输入访问口令。",
    promptSso: "请用单位账号登录。",
    promptProxy: "这台服务器通过单位的登录门户认证。请先在门户登录，再重新打开这个页面。",
    signInWith: (name: string) => `用 ${name} 登录`,
    or: "或",
    reload: "重新打开",
    ssoFailed: "单点登录没有完成，请重试",
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
    expiredSsoHint:
      "Sign in again. The sign-in page opens in a new tab; come back here afterwards. The meeting is not affected.",
    prompt: "Enter the access password.",
    promptEither: "Sign in with your organization account, or enter the access password.",
    promptSso: "Sign in with your organization account.",
    promptProxy:
      "This server signs you in through your organization's login portal. Sign in there, then open this page again.",
    signInWith: (name: string) => `Sign in with ${name}`,
    or: "or",
    reload: "Open again",
    ssoFailed: "Single sign-on did not complete. Please try again.",
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
