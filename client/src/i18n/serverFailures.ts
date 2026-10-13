import { defineMessages } from "./define.ts";

// 任务和报告的失败原因（docs/interfaces.md §5.10）：服务端给代码和参数，这里按界面语言翻译。
// 新代码先写进 interfaces.md 的表，再加到 FAILURE_PARAMS 和下面的目录里，服务端的表在 src/agentic_meeting/messages.py；
// tests/test_repository_rules.py 检查三处的代码一致。不认得的代码、参数不对的，页面显示服务端给的中文 error。
// report.failed 的 detail 是异常的类型和说明，两种语言都原样显示。

type Kind = "string" | "number";

/** 每个代码带的参数及其类型：收到的参数对不上就不翻译。 */
export const FAILURE_PARAMS = {
  "task.cancelled": {},
  "task.restarted": {},
  "task.shutdown": {},
  "task.timeout": { minutes: "number" },
  "task.internal": { type: "string" },
  "agent.disabled": {},
  "agent.not_configured": {},
  "agent.not_installed": {},
  "agent.stopping": {},
  "agent.too_many_steps": { max: "number" },
  "agent.key_env_missing": {},
  "agent.key_unset": { env: "string" },
  "agent.key_rejected": { env: "string" },
  "agent.unreachable": {},
  "agent.http_error": { status: "number" },
  "agent.error": { type: "string" },
  "report.interrupted": {},
  "report.timeout": {},
  "report.failed": { detail: "string" },
} as const satisfies Record<string, Record<string, Kind>>;

export type FailureCode = keyof typeof FAILURE_PARAMS;

export const serverFailures = defineMessages(
  {
    "task.cancelled": "已取消",
    "task.restarted": "服务重启，任务没有做完",
    "task.shutdown": "服务停止，任务没有做完",
    "task.timeout": ({ minutes }: { minutes: number }) => `超时（超过 ${minutes} 分钟没有做完）`,
    "task.internal": ({ type }: { type: string }) => `内部错误（${type}）`,
    "agent.disabled": "后台任务功能已在配置里关闭",
    "agent.not_configured": "后台 agent 还没有配置（config.toml 的 [agent] 里要填模型地址和名字）",
    "agent.not_installed": "没有安装后台 agent 的依赖（uv sync --extra agent）",
    "agent.stopping": "服务正在停止",
    "agent.too_many_steps": ({ max }: { max: number }) => `步骤太多（超过 ${max} 步）还没有做完`,
    "agent.key_env_missing":
      "远端模型要求密钥，但配置里没有写密钥的环境变量名（agent.api_key_env）",
    "agent.key_unset": ({ env }: { env: string }) =>
      `远端模型的密钥没有设置（环境变量 ${env} 是空的；设好之后要重新启动）`,
    "agent.key_rejected": ({ env }: { env: string }) => `远端模型不接受这个密钥（环境变量 ${env}）`,
    "agent.unreachable": "连不上远端模型",
    "agent.http_error": ({ status }: { status: number }) => `远端模型返回了错误（${status}）`,
    "agent.error": ({ type }: { type: string }) => `agent 运行出错（${type}）`,
    "report.interrupted": "服务停止了，这份报告没有生成完",
    "report.timeout": "生成超时",
    "report.failed": ({ detail }: { detail: string }) => `${detail}`,
  },
  {
    "task.cancelled": "Cancelled",
    "task.restarted": "The server restarted before the task finished",
    "task.shutdown": "The server stopped before the task finished",
    "task.timeout": ({ minutes }) => `Timed out (not finished within ${minutes} minutes)`,
    "task.internal": ({ type }) => `Internal error (${type})`,
    "agent.disabled": "Background tasks are turned off in the configuration",
    "agent.not_configured":
      "The background agent is not configured (set the model address and name under [agent] in config.toml)",
    "agent.not_installed":
      "The background agent's dependencies are not installed (uv sync --extra agent)",
    "agent.stopping": "The server is stopping",
    "agent.too_many_steps": ({ max }) => `Not finished after too many steps (more than ${max})`,
    "agent.key_env_missing":
      "The remote model requires a key, but the configuration names no key variable (agent.api_key_env)",
    "agent.key_unset": ({ env }) =>
      `The remote model's key is not set (the environment variable ${env} is empty; restart after setting it)`,
    "agent.key_rejected": ({ env }) =>
      `The remote model rejected the key (environment variable ${env})`,
    "agent.unreachable": "Cannot reach the remote model",
    "agent.http_error": ({ status }) => `The remote model returned an error (${status})`,
    "agent.error": ({ type }) => `The agent failed (${type})`,
    "report.interrupted": "The server stopped before this report was finished",
    "report.timeout": "Generation timed out",
    "report.failed": ({ detail }) => `${detail}`,
  },
);

type Catalog = (typeof serverFailures)["en"];
type Params = Readonly<Record<string, string | number>>;

/** 按界面语言翻译一条失败原因；不认得的代码或参数对不上时返回 null，由调用方退回服务端的 error。 */
export function serverFailureText(t: Catalog, code: unknown, params: unknown): string | null {
  if (typeof code !== "string" || !Object.hasOwn(FAILURE_PARAMS, code)) return null;
  if (typeof params !== "object" || params === null) return null;
  const known = code as FailureCode;
  const spec: Readonly<Record<string, Kind>> = FAILURE_PARAMS[known];
  const values = params as Params;
  if (!Object.entries(spec).every(([name, kind]) => typeof values[name] === kind)) return null;
  const entry: string | ((p: never) => string) = t[known];
  return typeof entry === "string" ? entry : entry(values as never);
}
