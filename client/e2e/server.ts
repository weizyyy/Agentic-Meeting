import type { ChildProcessWithoutNullStreams } from "node:child_process";

/** 只认服务器完成 lifespan 后的就绪信号；失败有界且由调用方统一清理。 */
export function waitForServer(
  child: ChildProcessWithoutNullStreams,
  timeout = 30_000,
): Promise<string> {
  return new Promise((resolve, reject) => {
    let output = "";
    const cleanup = () => {
      clearTimeout(timer);
      child.stdout.off("data", onData);
      child.off("error", onError);
      child.off("exit", onExit);
    };
    const onError = (error: Error) => {
      cleanup();
      reject(error);
    };
    const onExit = (code: number | null) => onError(new Error(`测试服务器提前退出：${code}`));
    const onData = (data: Buffer) => {
      output += data.toString();
      const match = output.match(/E2E_READY (http:\/\/127\.0\.0\.1:\d+)\r?\n/);
      if (match) {
        cleanup();
        resolve(match[1]);
      }
    };
    const timer = setTimeout(() => onError(new Error("测试服务器就绪超时")), timeout);
    child.stdout.on("data", onData);
    child.once("error", onError);
    child.once("exit", onExit);
  });
}
