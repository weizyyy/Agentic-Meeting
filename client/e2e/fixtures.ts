import { test as base, expect, type APIRequestContext } from "@playwright/test";
import { spawn } from "node:child_process";
import { createWriteStream } from "node:fs";
import { once } from "node:events";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { installMedia } from "./media.ts";
import { stopServer, waitForServer, type ServerExit } from "./server.ts";

const root = fileURLToPath(new URL("../../", import.meta.url));

export const test = base.extend<{ serverURL: string }>({
  serverURL: async ({}, use, info) => {
    const python = path.join(
      root,
      ".venv",
      process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
    );
    const child = spawn(python, ["-m", "tests.browser.server"], {
      cwd: root,
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
    });
    const logPath = info.outputPath("server.log");
    const log = createWriteStream(logPath);
    child.stdout.pipe(log, { end: false });
    child.stderr.pipe(log, { end: false });
    const closed = new Promise<ServerExit>((resolve) =>
      child.once("close", (code, signal) => resolve({ code, signal })),
    );
    try {
      const url = await waitForServer(child);
      await use(url);
    } finally {
      try {
        await stopServer(child, closed);
      } finally {
        log.end();
        await once(log, "finish");
        await info.attach("server", { path: logPath, contentType: "text/plain" });
      }
    }
  },
  baseURL: async ({ serverURL }, use) => use(serverURL),
  context: async ({ context, serverURL, browserName }, use) => {
    const external: string[] = [];
    await context.route("**/*", async (route) => {
      const url = route.request().url();
      if (url.startsWith(serverURL + "/") || url.startsWith("blob:") || url.startsWith("data:")) {
        await route.continue();
      } else {
        external.push(url);
        await route.abort("blockedbyclient");
      }
    });
    await context.addInitScript(installMedia, { nativeAudio: browserName === "firefox" });
    await use(context);
    expect(external, "页面不应请求外部服务").toEqual([]);
  },
});
export { expect };

/**
 * 等受控管线处理完第 ``count`` 次 client-ready。页面显示「已连接」只说明浏览器发出了 client-ready，
 * 服务端可能还没挂上这一路连接（docs/pipecat-notes.md §12），这时推字幕会得到 409。
 */
export async function serverReady(request: APIRequestContext, count: number) {
  await expect
    .poll(async () => (await (await request.get("/__test/state")).json()).ready)
    .toBe(count);
}

/** 推一条受控字幕；服务端拒绝时立即失败并给出原因，而不是等字幕出现超时。 */
export async function emit(request: APIRequestContext, data: { text: string; final?: boolean }) {
  const response = await request.post("/__test/emit", { data });
  expect(response.status(), await response.text()).toBe(200);
  return response.json();
}
