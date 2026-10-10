import assert from "node:assert/strict";
import { test } from "node:test";

import {
  AuthSession,
  CSRF_HEADER,
  fetchAuthStatus,
  login,
  loginErrorText,
  logout,
  parseStatus,
  withCsrf,
} from "./auth.ts";

const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });

test("令牌只加在改动性的请求上，原有请求头保留", () => {
  assert.equal(withCsrf(undefined, "t"), undefined);
  assert.deepEqual(withCsrf({ method: "GET" }, "t"), { method: "GET" });
  const post = withCsrf({ method: "post", headers: { "content-type": "application/json" } }, "t");
  const headers = new Headers(post?.headers);
  assert.equal(headers.get(CSRF_HEADER), "t");
  assert.equal(headers.get("content-type"), "application/json");
  const form = new FormData();
  assert.equal(withCsrf({ method: "POST", body: form }, "t")?.body, form);
  // 没有令牌（没启用口令）时原样返回
  const init = { method: "DELETE" };
  assert.equal(withCsrf(init, null), init);
});

test("登录状态：没启用口令就当作已登录；字段缺失或类型不对按未登录处理", () => {
  assert.deepEqual(parseStatus({ enabled: false, authenticated: true, csrf_token: null }), {
    enabled: false,
    authenticated: true,
    csrf_token: null,
  });
  assert.deepEqual(parseStatus({ enabled: true, authenticated: true, csrf_token: "abc" }), {
    enabled: true,
    authenticated: true,
    csrf_token: "abc",
  });
  assert.deepEqual(parseStatus({ enabled: true, authenticated: "yes", csrf_token: 3 }), {
    enabled: true,
    authenticated: false,
    csrf_token: null,
  });
  assert.equal(parseStatus(null).authenticated, true);
});

test("登录失败的提示：限速给出等待时间，其余用服务端的说明", () => {
  assert.equal(loginErrorText(429, { error: "x" }, "125"), "尝试次数过多，请 3 分钟后再试");
  assert.equal(loginErrorText(429, null, null), "尝试次数过多，请稍后再试");
  assert.equal(loginErrorText(401, { error: "口令不对" }, null), "口令不对");
  assert.equal(loginErrorText(500, "oops", null), "登录失败（500）");
  assert.equal(loginErrorText(0, null, null), "无法连接到服务端");
});

test("会话失效：通知所有订阅者并清掉令牌；取消订阅后不再收到", () => {
  const session = new AuthSession();
  session.setToken("t");
  assert.equal(session.headers().get(CSRF_HEADER), "t");
  let calls = 0;
  const stop = session.onUnauthorized(() => calls++);
  session.notifyUnauthorized();
  assert.equal(calls, 1);
  assert.equal(session.csrfToken, null);
  assert.equal(session.headers().has(CSRF_HEADER), false);
  stop();
  session.notifyUnauthorized();
  assert.equal(calls, 1);
});

test("读状态、登录、退出：令牌随之更新", async () => {
  const session = new AuthSession();
  const calls: { url: string; init?: RequestInit }[] = [];
  const replies: Response[] = [
    json({ enabled: true, authenticated: false, csrf_token: null }),
    json({ error: "口令不对" }, 401),
    json({ enabled: true, authenticated: true, csrf_token: "tok" }),
    json({ enabled: true, authenticated: false, csrf_token: null }),
  ];
  const fetchFn = async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return replies.shift()!;
  };

  const status = await fetchAuthStatus(fetchFn, session);
  assert.equal(status.authenticated, false);

  assert.deepEqual(await login("wrong", fetchFn, session), { ok: false, message: "口令不对" });
  assert.equal(session.csrfToken, null);
  const ok = await login("right", fetchFn, session);
  assert.equal(ok.ok, true);
  assert.equal(session.csrfToken, "tok");
  assert.equal(calls[2].url, "/api/auth/login");
  assert.equal(calls[2].init?.body, JSON.stringify({ password: "right" }));

  await logout(fetchFn, session);
  assert.equal(new Headers(calls[3].init?.headers).get(CSRF_HEADER), "tok");
  assert.equal(session.csrfToken, null);
});

test("登录时连不上服务端：给出提示而不是抛错", async () => {
  const result = await login("x", async () => {
    throw new TypeError("failed to fetch");
  });
  assert.deepEqual(result, { ok: false, message: "无法连接到服务端" });
});
