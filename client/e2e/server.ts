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

/** 退出事件也覆盖启动失败；强杀或无法退出都显式报告，不留无限等待。 */
export async function stopServer(
  child: ChildProcessWithoutNullStreams,
  closed: Promise<void>,
  grace = 15_000,
): Promise<void> {
  let forced = false;
  let killError: Error | undefined;
  const onError = (error: Error) => {
    killError = error;
  };
  child.on("error", onError);
  let killTimer: ReturnType<typeof setTimeout>;
  let deadline: ReturnType<typeof setTimeout>;
  try {
    child.kill("SIGTERM");
    await Promise.race([
      closed,
      new Promise<never>((_, reject) => {
        killTimer = setTimeout(() => {
          forced = true;
          child.kill("SIGKILL");
        }, grace);
        deadline = setTimeout(
          () => reject(killError ?? new Error("测试服务器无法退出")),
          grace + 1000,
        );
      }),
    ]);
    if (forced) throw new Error("测试服务器未正常关闭，已强杀");
    if (killError) throw killError;
  } finally {
    clearTimeout(killTimer!);
    clearTimeout(deadline!);
    child.off("error", onError);
  }
}
