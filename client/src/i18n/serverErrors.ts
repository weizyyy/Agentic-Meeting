import { defineMessages } from "./define.ts";

// 服务端接口的错误（docs/interfaces.md §5.9）：服务端只给代码和参数，这里按界面语言翻译。
// 新代码先写进 interfaces.md 的表，再加到 ERROR_PARAMS 和下面的目录里，服务端的表在 src/agentic_meeting/messages.py；
// tests/test_repository_rules.py 检查三处的代码一致。不认得的代码、参数不对的，页面显示服务端给的中文 error。
// request.invalid 的 detail 是给接口调用者看的中文说明，英文里不显示。

type Kind = "string" | "number";

/** 每个代码带的参数及其类型：收到的参数对不上就不翻译。 */
export const ERROR_PARAMS = {
  "share.invalid": {},
  "share.limit": {},
  "access.denied": {},
  "access.invalid_member": {},
  "access.last_host": {},
  "access.busy": {},
  "text.context_too_large": {},
  "text.interrupted": {},
  "text.busy": {},
  "text.timeout": {},
  "text.failed": {},

  "request.invalid": { detail: "string" },
  "request.too_large": {},
  "request.failed": { status: "number" },
  "route.not_found": {},
  "auth.required": {},
  "auth.csrf": {},
  "auth.wrong_password": {},
  "auth.too_many": {},
  "auth.not_enabled": {},
  "storage.not_ready": {},
  "cleanup.not_ready": {},
  "session.not_found": {},
  "session.none": {},
  "session.busy": {},
  "session.not_live": {},
  "session.delete_incomplete": {},
  "session.title_too_long": { max: "number" },
  "speaker.not_found": {},
  "speaker.name_too_long": { max: "number" },
  "speaker.not_mergeable": {},
  "speaker.merge_self": {},
  "utterances.too_many": { max: "number" },
  "frames.disabled": {},
  "frames.no_live_session": {},
  "frame.not_found": {},
  "frame.bad_format": {},
  "frame.too_wide": { max: "number" },
  "frame.invalid": {},
  "frame.too_large": { max_mb: "number" },
  "frame.empty": {},
  "frame.clock_skew": { max: "number" },
  "frame.save_failed": {},
  "task.not_found": {},
  "tasks.disabled": {},
  "artifact.not_found": {},
  "report.session_live": {},
  "report.no_model": {},
  "report.busy": {},
  "report.not_found": {},
  "report.not_ready": {},
} as const satisfies Record<string, Record<string, Kind>>;

export type ErrorCode = keyof typeof ERROR_PARAMS;

export const serverErrors = defineMessages(
  {
    "request.invalid": ({ detail }: { detail: string }) => `请求格式有误：${detail}`,
    "request.too_large": "请求体太大",
    "request.failed": ({ status }: { status: number }) => `请求失败（${status}）`,
    "route.not_found": "没有这个接口或页面",
    "auth.required": "请先登录",
    "share.invalid": "分享无效或已到期／撤销",
    "share.limit": "本会议最多8个有效链接",
    "access.denied": "无权执行此操作",
    "access.invalid_member": "成员不存在或不能主持会议",
    "access.last_host": "会议必须保留一位主持人",
    "access.busy": "录音服务正忙",
    "text.context_too_large": "问题和提示超出上下文预算",
    "text.interrupted": "只读问答被实时应答打断，请稍后重新提问",
    "text.busy": "只读问答队列已满或已有问题处理中",
    "text.timeout": "只读问答等待超时",
    "text.failed": "只读问答没有完成",

    "auth.csrf": "请求缺少有效的安全令牌，请刷新页面后重试",
    "auth.wrong_password": "口令不对",
    "auth.too_many": "尝试次数过多，请稍后再试",
    "auth.not_enabled": "没有启用访问口令",
    "storage.not_ready": "存储尚未就绪",
    "cleanup.not_ready": "清理尚未就绪",
    "session.not_found": "找不到这场会议",
    "session.none": "现在没有会议",
    "session.busy": "会议正在处理或等待删除，请稍后重试",
    "session.not_live": "这场会议现在没有在进行，消息没有发送",
    "session.delete_incomplete": "删除未完成，会议待删除，请稍后重试",
    "session.title_too_long": ({ max }: { max: number }) => `标题太长（最多 ${max} 字）`,
    "speaker.not_found": "这场会议里没有这个说话人",
    "speaker.name_too_long": ({ max }: { max: number }) => `名字太长（最多 ${max} 字）`,
    "speaker.not_mergeable": "只能合并说话人区分给出的说话人（助理、文字输入、「未知」不能合并）",
    "speaker.merge_self": "不能把说话人合并到自己",
    "utterances.too_many": ({ max }: { max: number }) => `一次最多改 ${max} 条发言`,
    "frames.disabled": "截图功能已在配置里关闭",
    "frames.no_live_session": "现在没有进行中的会议，截图没有保存",
    "frame.not_found": "找不到这张截图",
    "frame.bad_format": "只接受 WebP 或 JPEG 格式的截图",
    "frame.too_wide": ({ max }: { max: number }) => `截图的长边不能超过 ${max} 像素`,
    "frame.invalid": "上传的文件不是有效的图片",
    "frame.too_large": ({ max_mb }: { max_mb: number }) => `截图太大（最多 ${max_mb} MB）`,
    "frame.empty": "上传的文件是空的",
    "frame.clock_skew": ({ max }: { max: number }) =>
      `截图的采集时间与服务端时间相差超过 ${max} 秒，请刷新页面重新对时`,
    "frame.save_failed": "截图保存失败（磁盘不可写？）",
    "task.not_found": "找不到这个任务",
    "tasks.disabled": "后台任务功能没有开启",
    "artifact.not_found": "找不到这个产物文件",
    "report.session_live": "会议正在进行，结束或断开之后才能生成报告",
    "report.no_model": "没有可用的模型，生成不了报告",
    "report.busy": "这场会议已经有一份报告正在生成",
    "report.not_found": "这场会议还没有报告",
    "report.not_ready": "这场会议还没有生成好的报告",
  },
  {
    "request.invalid": () => "The request is malformed.",
    "request.too_large": "The request body is too large.",
    "request.failed": ({ status }) => `The request failed (${status}).`,
    "route.not_found": "There is no such endpoint or page.",
    "auth.required": "Please log in first.",
    "share.invalid": "This share is invalid, expired or revoked.",
    "share.limit": "This meeting allows at most eight active links.",
    "access.denied": "You cannot perform this action.",
    "access.invalid_member": "The member is unavailable or cannot host.",
    "access.last_host": "A meeting must retain a host.",
    "access.busy": "The recording service is busy.",
    "text.context_too_large": "The question exceeds the context budget.",
    "text.interrupted": "The question was interrupted by realtime activity. Please ask again.",
    "text.busy": "The question queue is full or your question is already pending.",
    "text.timeout": "The question timed out.",
    "text.failed": "The question could not be completed.",

    "auth.csrf": "The request has no valid security token. Reload the page and try again.",
    "auth.wrong_password": "Wrong password.",
    "auth.too_many": "Too many attempts. Try again later.",
    "auth.not_enabled": "No access password is configured.",
    "storage.not_ready": "Storage is not ready yet.",
    "cleanup.not_ready": "Cleanup is not ready yet.",
    "session.not_found": "Meeting not found.",
    "session.none": "There is no meeting right now.",
    "session.busy": "The meeting is being processed or waiting to be deleted. Try again later.",
    "session.not_live": "This meeting is not in progress; the message was not sent.",
    "session.delete_incomplete":
      "Deletion did not finish; the meeting is marked for deletion. Try again later.",
    "session.title_too_long": ({ max }) => `The title is too long (at most ${max} characters).`,
    "speaker.not_found": "This meeting has no such speaker.",
    "speaker.name_too_long": ({ max }) => `The name is too long (at most ${max} characters).`,
    "speaker.not_mergeable":
      "Only speakers found by speaker identification can be merged (not the assistant, typed input or Unknown).",
    "speaker.merge_self": "A speaker cannot be merged into itself.",
    "utterances.too_many": ({ max }) => `At most ${max} captions can be changed at once.`,
    "frames.disabled": "Screenshots are turned off in the configuration.",
    "frames.no_live_session": "No meeting is running, so the screenshot was not saved.",
    "frame.not_found": "Screenshot not found.",
    "frame.bad_format": "Only WebP or JPEG screenshots are accepted.",
    "frame.too_wide": ({ max }) => `The screenshot's longer side cannot exceed ${max} pixels.`,
    "frame.invalid": "The uploaded file is not a valid image.",
    "frame.too_large": ({ max_mb }) => `The screenshot is too large (at most ${max_mb} MB).`,
    "frame.empty": "The uploaded file is empty.",
    "frame.clock_skew": ({ max }) =>
      `The screenshot's capture time differs from the server's clock by more than ${max} seconds. Reload the page to resynchronize.`,
    "frame.save_failed": "The screenshot could not be saved (is the disk writable?).",
    "task.not_found": "Task not found.",
    "tasks.disabled": "Background tasks are not enabled.",
    "artifact.not_found": "File not found.",
    "report.session_live":
      "The meeting is still running. End or disconnect it before generating a report.",
    "report.no_model": "No model is available to generate a report.",
    "report.busy": "A report for this meeting is already being generated.",
    "report.not_found": "This meeting has no report yet.",
    "report.not_ready": "This meeting has no finished report yet.",
  },
);

type Catalog = (typeof serverErrors)["en"];
type Params = Readonly<Record<string, string | number>>;

/** 按界面语言翻译一条服务端错误；不认得的代码或参数对不上时返回 null，由调用方退回服务端的 error。 */
export function serverErrorText(t: Catalog, code: unknown, params: unknown): string | null {
  if (typeof code !== "string" || !Object.hasOwn(ERROR_PARAMS, code)) return null;
  if (typeof params !== "object" || params === null) return null;
  const known = code as ErrorCode;
  const spec: Readonly<Record<string, Kind>> = ERROR_PARAMS[known];
  const values = params as Params;
  if (!Object.entries(spec).every(([name, kind]) => typeof values[name] === kind)) return null;
  const entry: string | ((p: never) => string) = t[known];
  return typeof entry === "string" ? entry : entry(values as never);
}
