import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { test } from "node:test";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { stopServer, waitForServer } from "./server.ts";

function child() {
  return Object.assign(new EventEmitter(), {
    stdout: new PassThrough(),
  }) as ChildProcessWithoutNullStreams;
}
test("服务器就绪信号支持分块且移除监听器", async () => {
  const process = child();
  const ready = waitForServer(process);
  process.stdout.emit("data", Buffer.from("E2E_RE"));
  process.stdout.emit("data", Buffer.from("ADY http://127.0.0.1:1234\n"));
  assert.equal(await ready, "http://127.0.0.1:1234");
  assert.equal(process.stdout.listenerCount("data"), 0);
});
test("服务器无就绪信号时超时，提前退出时立即失败", async () => {
  await assert.rejects(waitForServer(child(), 1), /就绪超时/);
  const process = child();
  const ready = waitForServer(process);
  process.emit("exit", 2);
  await assert.rejects(ready, /提前退出：2/);
});

test("缺少 Python 时保留启动错误且关闭事件有界完成", async () => {
  const process = spawn("agentic-meeting-test-missing-executable", []);
  const closed = new Promise<void>((resolve) => process.once("close", () => resolve()));
  try {
    await assert.rejects(waitForServer(process), /ENOENT/);
  } finally {
    await stopServer(process, closed, 5);
  }
});

test("正常退出和强杀路径都会移除计时器与监听器", async () => {
  for (const needsKill of [false, true]) {
    const process = child();
    const closed = new Promise<void>((resolve) => process.once("close", () => resolve()));
    process.kill = (signal) => {
      if (!needsKill || signal === "SIGKILL") process.emit("close");
      return true;
    };
    const stopped = stopServer(process, closed, 1);
    if (needsKill) await assert.rejects(stopped, /已强杀/);
    else await stopped;
    assert.equal(process.listenerCount("error"), 0);
  }
});

test("强杀被拒绝时保留错误并有界失败", async () => {
  const process = child();
  process.kill = () => {
    process.emit("error", new Error("kill rejected"));
    return false;
  };
  await assert.rejects(stopServer(process, new Promise(() => {}), 1), /kill rejected/);
  assert.equal(process.listenerCount("error"), 0);
});
