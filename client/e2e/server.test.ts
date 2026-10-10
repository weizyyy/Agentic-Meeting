import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { test } from "node:test";
import type { ChildProcessWithoutNullStreams } from "node:child_process";
import { waitForServer } from "./server.ts";

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
