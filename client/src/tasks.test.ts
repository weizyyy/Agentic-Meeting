import assert from "node:assert/strict";
import { test } from "node:test";

import {
  TASK_STATUS_LABELS,
  applyTask,
  applyTaskEvent,
  artifactUrl,
  isFinished,
  isImageArtifact,
  replaceTasks,
  safeLink,
  taskDuration,
  taskSubline,
  type TaskItem,
} from "./tasks.ts";

const task = (id: string, created: number, extra: Partial<TaskItem> = {}): TaskItem => ({
  id,
  label: id.split(".").pop() ?? id,
  goal: "核实引用数",
  status: "running",
  brief: null,
  error: null,
  modality: "voice",
  created_at: created,
  ...extra,
});

test("状态标签与是否结束", () => {
  assert.equal(TASK_STATUS_LABELS.running, "进行中");
  assert.deepEqual(
    (["queued", "running", "succeeded", "failed", "cancelled"] as const).map(isFinished),
    [false, false, true, true, true],
  );
});

test("任务消息：新任务按创建时间插入，状态变化时保留最近一步", () => {
  let tasks = applyTask([], task("s.t2", 20));
  tasks = applyTask(tasks, task("s.t1", 10));
  assert.deepEqual(
    tasks.map((t) => t.label),
    ["t1", "t2"],
  );
  tasks = applyTaskEvent(tasks, "s.t1", "正在检索「引用数」");
  tasks = applyTask(tasks, task("s.t1", 10, { status: "succeeded", brief: "被引 1243 次" }));
  assert.equal(tasks.length, 2);
  assert.equal(tasks[0].status, "succeeded");
  assert.equal(tasks[0].lastStep, "正在检索「引用数」");
});

test("进度只记到对应的任务上；不认识的任务不变", () => {
  const tasks = [task("s.t1", 10), task("s.t2", 20)];
  const updated = applyTaskEvent(tasks, "s.t2", "正在运行一段代码");
  assert.equal(updated[0].lastStep, undefined);
  assert.equal(updated[1].lastStep, "正在运行一段代码");
  assert.equal(applyTaskEvent(tasks, "s.t9", "x"), tasks);
});

test("用服务端的列表替换时，本地记着的最近一步留着", () => {
  const local = [task("s.t1", 10, { lastStep: "正在检索" }), task("s.t9", 90)];
  const replaced = replaceTasks(local, [task("s.t2", 20), task("s.t1", 10, { status: "queued" })]);
  assert.deepEqual(
    replaced.map((t) => [t.label, t.status, t.lastStep]),
    [
      ["t1", "queued", "正在检索"],
      ["t2", "running", undefined],
    ],
  );
});

test("任务行第二行：结论、原因或最近一步", () => {
  assert.equal(taskSubline(task("a", 1, { status: "succeeded", brief: "被引 1243 次" })), "被引 1243 次");
  assert.equal(taskSubline(task("a", 1, { status: "succeeded" })), "已完成");
  assert.equal(
    taskSubline(task("a", 1, { status: "failed", error: "连不上远端模型" })),
    "没办成：连不上远端模型",
  );
  assert.equal(taskSubline(task("a", 1, { status: "failed" })), "没办成");
  assert.equal(taskSubline(task("a", 1, { status: "cancelled", error: "已取消" })), "已取消");
  assert.equal(taskSubline(task("a", 1, { status: "queued" })), "排队中，前面的任务做完就开始");
  assert.equal(taskSubline(task("a", 1, { lastStep: "正在运行一段代码" })), "正在运行一段代码");
  assert.equal(taskSubline(task("a", 1)), "开始处理");
});

test("产物地址：编号和文件名都编码，子目录保留", () => {
  assert.equal(artifactUrl("abc.t1", "plot.png"), "/api/tasks/abc.t1/artifacts/plot.png");
  assert.equal(
    artifactUrl("abc.t1", "out/图 1.png"),
    "/api/tasks/abc.t1/artifacts/out/%E5%9B%BE%201.png",
  );
  assert.equal(artifactUrl("a/b", "x?y#z"), "/api/tasks/a%2Fb/artifacts/x%3Fy%23z");
});

test("哪些产物当图片显示", () => {
  for (const name of ["plot.png", "a/b.JPG", "x.jpeg", "y.webp", "z.gif"]) {
    assert.ok(isImageArtifact(name), name);
  }
  for (const name of ["data.csv", "page.html", "pic.svg", "png", "plot.png.exe"]) {
    assert.ok(!isImageArtifact(name), name);
  }
});

test("来源链接只认 http 和 https", () => {
  assert.equal(safeLink("https://example.org/a?b=1"), "https://example.org/a?b=1");
  assert.equal(safeLink("http://example.org"), "http://example.org/");
  assert.equal(safeLink("javascript:alert(1)"), null);
  assert.equal(safeLink("data:text/html,<script>"), null);
  assert.equal(safeLink("某篇论文，2023"), null);
  assert.equal(safeLink(""), null);
});

test("用时", () => {
  assert.equal(taskDuration(100, 145.4), "45 秒");
  assert.equal(taskDuration(100, 225), "2 分 05 秒");
  assert.equal(taskDuration(100, null), "");
  assert.equal(taskDuration(null, 5), "");
  assert.equal(taskDuration(100, 90), "0 秒");
});
