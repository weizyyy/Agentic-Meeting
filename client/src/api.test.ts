import assert from "node:assert/strict";
import { test } from "node:test";

import { ApiError, createApi } from "./api.ts";

interface Call {
  url: string;
  init?: RequestInit;
}

function fakeFetch(reply: (call: Call) => Response | Promise<Response>) {
  const calls: Call[] = [];
  const fn = async (url: string, init?: RequestInit) => {
    const call = { url, init };
    calls.push(call);
    return reply(call);
  };
  return { fn, calls };
}

const ok = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

test("会议列表：参数拼进查询串，只取 items", async () => {
  const { fn, calls } = fakeFetch(() => ok({ items: [{ id: "a" }] }));
  const api = createApi(fn);
  assert.deepEqual(await api.listSessions(), [{ id: "a" }]);
  await api.listSessions(10, 123.5);
  assert.equal(calls[0].url, "/api/sessions");
  assert.equal(calls[1].url, "/api/sessions?limit=10&before=123.5");
});

test("当前会话：404 表示还没有会议，返回 null；其他错误照常抛出", async () => {
  const none = createApi(fakeFetch(() => ok({ error: "现在没有会议" }, 404)).fn);
  assert.equal(await none.currentSession(), null);
  const broken = createApi(fakeFetch(() => ok({ error: "存储尚未就绪" }, 503)).fn);
  await assert.rejects(broken.currentSession(), (e: unknown) => {
    assert.ok(e instanceof ApiError);
    assert.equal(e.status, 503);
    assert.equal(e.message, "存储尚未就绪");
    return true;
  });
  const found = createApi(fakeFetch(() => ok({ id: "s1", state: "live" })).fn);
  assert.equal((await found.currentSession())?.id, "s1");
});

test("发言：只带给出的参数；会话编号要编码", async () => {
  const { fn, calls } = fakeFetch(() => ok({ items: [{ id: 1 }] }));
  const api = createApi(fn);
  assert.deepEqual(await api.listUtterances("s 1", { tail: 50 }), [{ id: 1 }]);
  await api.listUtterances("s1", { beforeId: 9, limit: 20 });
  await api.listUtterances("s1", { afterId: 3 });
  assert.equal(calls[0].url, "/api/utterances?session_id=s+1&tail=50");
  assert.equal(calls[1].url, "/api/utterances?session_id=s1&before_id=9&limit=20");
  assert.equal(calls[2].url, "/api/utterances?session_id=s1&after_id=3");
});

test("重命名、结束、删除：方法、路径和请求体", async () => {
  const { fn, calls } = fakeFetch((call) =>
    ok(call.init?.method === "DELETE" ? { id: "s/1" } : { id: "s/1", title: "新", ended_at: 5 }),
  );
  const api = createApi(fn);
  await api.renameSession("s/1", "新");
  await api.endSession("s/1");
  await api.deleteSession("s/1");
  assert.equal(calls[0].url, "/api/sessions/s%2F1");
  assert.equal(calls[0].init?.method, "PATCH");
  assert.equal(calls[0].init?.body, JSON.stringify({ title: "新" }));
  assert.deepEqual(calls[0].init?.headers, { "content-type": "application/json" });
  assert.equal(calls[1].url, "/api/sessions/s%2F1/end");
  assert.equal(calls[1].init?.method, "POST");
  assert.equal(calls[2].url, "/api/sessions/s%2F1");
  assert.equal(calls[2].init?.method, "DELETE");
});

test("说话人：列表和改名", async () => {
  const { fn, calls } = fakeFetch((call) =>
    call.init?.method === "PUT" ? ok({ idx: 2, display_name: "王老师" }) : ok({ items: [{ idx: 1, display_name: "说话人 1" }] }),
  );
  const api = createApi(fn);
  assert.deepEqual(await api.listSpeakers("s1"), [{ idx: 1, display_name: "说话人 1" }]);
  assert.deepEqual(await api.renameSpeaker("s1", 2, "王老师"), { idx: 2, display_name: "王老师" });
  assert.equal(calls[0].url, "/api/speakers?session_id=s1");
  assert.equal(calls[1].url, "/api/speakers/2");
  assert.equal(calls[1].init?.method, "PUT");
  assert.equal(calls[1].init?.body, JSON.stringify({ session_id: "s1", display_name: "王老师" }));
});

test("错误：用服务端的中文说明；响应不是 JSON 时给通用说明", async () => {
  const withMessage = createApi(fakeFetch(() => ok({ error: "找不到这场会议" }, 404)).fn);
  await assert.rejects(withMessage.getSession("x"), { message: "找不到这场会议", status: 404 });
  const html = createApi(
    fakeFetch(() => new Response("<html>bad gateway</html>", { status: 502 })).fn,
  );
  await assert.rejects(html.getSession("x"), { message: "请求失败（502）", status: 502 });
});

test("网络不通：ApiError，状态码 0", async () => {
  const api = createApi(async () => {
    throw new TypeError("fetch failed");
  });
  await assert.rejects(api.listSessions(), { message: "无法连接到服务端", status: 0 });
});

test("对时：取 server_time", async () => {
  const { fn, calls } = fakeFetch(() => ok({ server_time: 1790000000.25 }));
  assert.equal(await createApi(fn).serverTime(), 1790000000.25);
  assert.equal(calls[0].url, "/api/time");
});

test("截图列表：只留时间线用的字段，没有摘要的是 null", async () => {
  const { fn, calls } = fakeFetch(() =>
    ok({
      items: [
        { id: 1, t: 3, width: 16, height: 9, caption: "一张表", caption_status: "done" },
        { id: 2, t: 9, width: 16, height: 9, caption: null, caption_status: "pending" },
      ],
    }),
  );
  assert.deepEqual(await createApi(fn).listFrames("s 1"), [
    { id: 1, t: 3, width: 16, height: 9, caption: "一张表" },
    { id: 2, t: 9, width: 16, height: 9, caption: null },
  ]);
  assert.equal(calls[0].url, "/api/frames?session_id=s+1");
});

test("上传截图：multipart，带采集时刻；文件名跟着实际格式走", async () => {
  const { fn, calls } = fakeFetch(() => ok({ id: 5, t: 12.5 }));
  const api = createApi(fn);
  const webp = new Blob([new Uint8Array([1, 2, 3])], { type: "image/webp" });
  assert.deepEqual(await api.uploadFrame(webp, 1790000012.5), { id: 5, t: 12.5 });
  assert.equal(calls[0].url, "/api/frames");
  assert.equal(calls[0].init?.method, "POST");
  // 不自己写 content-type：边界串要由浏览器生成
  assert.equal(calls[0].init?.headers, undefined);
  const form = calls[0].init?.body;
  assert.ok(form instanceof FormData);
  assert.equal(form.get("captured_at"), "1790000012.5");
  const file = form.get("image");
  assert.ok(file instanceof File);
  assert.equal(file.name, "frame.webp");
  assert.equal(file.size, 3);

  await api.uploadFrame(new Blob([], { type: "image/jpeg" }), 1);
  const second = calls[1].init?.body;
  assert.ok(second instanceof FormData);
  assert.equal((second.get("image") as File).name, "frame.jpg");
});

test("上传截图被拒：带上服务端的说明", async () => {
  const api = createApi(fakeFetch(() => ok({ error: "现在没有进行中的会议，截图没有保存" }, 404)).fn);
  await assert.rejects(api.uploadFrame(new Blob([]), 1), {
    message: "现在没有进行中的会议，截图没有保存",
    status: 404,
  });
});

test("任务：列表、详情、取消；编号要编码", async () => {
  const { fn, calls } = fakeFetch((call) =>
    call.url.startsWith("/api/tasks?") ? ok({ items: [{ id: "s 1.t1" }] }) : ok({ id: "s 1.t1", status: "cancelled" }),
  );
  const api = createApi(fn);
  assert.deepEqual(await api.listTasks("s 1"), [{ id: "s 1.t1" }]);
  assert.equal((await api.getTask("s 1.t1")).id, "s 1.t1");
  assert.equal((await api.cancelTask("s 1.t1")).status, "cancelled");
  assert.equal(calls[0].url, "/api/tasks?session_id=s+1");
  assert.equal(calls[1].url, "/api/tasks/s%201.t1");
  assert.equal(calls[2].url, "/api/tasks/s%201.t1/cancel");
  assert.equal(calls[2].init?.method, "POST");
});

test("合并说话人：POST 到被合并的那个，请求体里是并到谁", async () => {
  const done = { from: 3, into: 1, display_name: "王老师", moved: 4 };
  const { fn, calls } = fakeFetch(() => ok(done));
  assert.deepEqual(await createApi(fn).mergeSpeakers("s 1", 3, 1), done);
  assert.equal(calls[0].url, "/api/speakers/3/merge");
  assert.equal(calls[0].init?.method, "POST");
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), { session_id: "s 1", into: 1 });
});

test("会后报告：没有时返回 null；触发生成返回编号", async () => {
  const none = createApi(fakeFetch(() => ok({ error: "这场会议还没有报告" }, 404)).fn);
  assert.equal(await none.getReport("s1"), null);
  const report = { id: 3, status: "done", created_at: 1, provider: "realtime_llm", text_md: "# x", error: null };
  const { fn, calls } = fakeFetch((call) =>
    call.init?.method === "POST" ? ok({ report_id: 4, status: "running" }, 202) : ok(report),
  );
  const api = createApi(fn);
  assert.deepEqual(await api.getReport("s 1"), report);
  assert.equal(await api.startReport("s 1"), 4);
  assert.deepEqual(
    calls.map((c) => [c.init?.method ?? "GET", c.url]),
    [
      ["GET", "/api/sessions/s%201/report"],
      ["POST", "/api/sessions/s%201/report"],
    ],
  );
  const busy = createApi(fakeFetch(() => ok({ error: "这场会议已经有一份报告正在生成" }, 409)).fn);
  await assert.rejects(busy.startReport("s1"), /正在生成/);
});


test("改发言人：已有的说话人给编号，新建的给名字", async () => {
  const done = { speaker: { idx: 2, display_name: "小李" }, ids: [5, 6] };
  const { fn, calls } = fakeFetch(() => ok(done));
  const api = createApi(fn);
  assert.deepEqual(await api.assignSpeaker("s1", [5, 6], { speakerIdx: 2 }), done);
  await api.assignSpeaker("s1", [7], { newSpeaker: "张老师" });
  assert.equal(calls[0].url, "/api/utterances/speaker");
  assert.equal(calls[0].init?.method, "POST");
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)), {
    session_id: "s1",
    ids: [5, 6],
    speaker_idx: 2,
  });
  assert.deepEqual(JSON.parse(String(calls[1].init?.body)), {
    session_id: "s1",
    ids: [7],
    new_speaker: "张老师",
  });
});
