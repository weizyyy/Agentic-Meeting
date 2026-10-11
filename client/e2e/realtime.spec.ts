import { test, expect, emit, serverReady } from "./fixtures.ts";

test("刷新后通过新连接继续同一会议并保留字幕和时间轴", async ({ page, request }) => {
  const seeds = await (await request.get("/__test/state")).json();
  await page.goto("/");
  await page.getByRole("button", { name: "会议列表", exact: true }).click();
  await page.getByRole("button", { name: "继续：月面计划讨论", exact: true }).click();
  await expect(page.getByRole("status").first()).toHaveText("已连接");
  await serverReady(request, 1);
  const before = await emit(request, { text: "刷新前保存的发言" });
  await expect(page.getByText("刷新前保存的发言", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("刷新前保存的发言", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "会议列表", exact: true }).click();
  await page.getByRole("button", { name: "继续：月面计划讨论", exact: true }).click();
  await expect(page.getByRole("status").first()).toHaveText("已连接");
  await serverReady(request, 2);
  const session = await (await request.get(`/api/sessions/${seeds.interrupted}`)).json();
  expect(session.id).toBe(seeds.interrupted);
  expect(session.connections).toHaveLength(2);
  expect(session.connections[0].disconnected_at).not.toBeNull();
  expect(session.connections[1].t_from).toBeGreaterThanOrEqual(before.t_end);
  await expect(page.getByText("月面计划保留的历史字幕", { exact: true })).toBeVisible();
  await emit(request, { text: "刷新后实时增量", final: false });
  await expect(page.getByText("刷新后实时增量", { exact: true })).toBeVisible();
  const after = await emit(request, { text: "刷新后保存的发言" });
  expect(after.session_id).toBe(seeds.interrupted);
  expect(after.t_start).toBeGreaterThanOrEqual(before.t_end);
  await expect(page.getByText("刷新后保存的发言", { exact: true })).toBeVisible();
  const stored = await (
    await request.get(`/api/utterances?session_id=${seeds.interrupted}`)
  ).json();
  expect(stored.items.map((item: { text: string }) => item.text)).toEqual([
    "月面计划保留的历史字幕",
    "刷新前保存的发言",
    "刷新后保存的发言",
  ]);
  await page.getByRole("button", { name: "结束会议", exact: true }).click();
  await expect(page.getByRole("status").first()).toHaveText("未连接");
});

test("第二页面通过真实轮询旁听且不接管或采集媒体", async ({ page, context, request }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "开始新会议", exact: true }).first().click();
  await expect(page.getByRole("status").first()).toHaveText("已连接");
  await serverReady(request, 1);
  const initial = await (await request.get("/__test/state")).json();
  const observer = await context.newPage();
  const offers: string[] = [];
  const polls: string[] = [];
  observer.on("request", (req) => {
    if (req.url().endsWith("/api/offer")) offers.push(req.url());
    if (req.url().includes("/api/utterances?")) polls.push(req.url());
  });
  try {
    await observer.goto("/");
    await expect(observer.getByText(/正在另一台设备上进行，这里只读/)).toBeVisible();
    await expect(observer.getByRole("textbox", { name: "给助理发文字消息" })).toBeDisabled();
    await expect(observer.getByRole("button", { name: "共享屏幕", exact: true })).toBeDisabled();
    const count = polls.length;
    await emit(request, { text: "主页面继续发言，旁听页面轮询收到" });
    await expect(
      observer.getByText("主页面继续发言，旁听页面轮询收到", { exact: true }),
    ).toBeVisible();
    expect(polls.length).toBeGreaterThan(count);
    expect(offers).toEqual([]);
    expect(await observer.evaluate(() => (window as any).e2eMedia.tracks.length)).toBe(0);
    await expect(page.getByRole("status").first()).toHaveText("已连接");
    await expect(page.getByText("主页面继续发言，旁听页面轮询收到", { exact: true })).toBeVisible();
    const state = await (await request.get("/__test/state")).json();
    expect(state.live).toBe(initial.live);
    expect(state.ready).toBe(1);
    const session = await (await request.get(`/api/sessions/${state.live}`)).json();
    expect(session.connections).toHaveLength(1);
    expect(session.connections[0].disconnected_at).toBeNull();
  } finally {
    await observer.close();
  }
  await page.getByRole("button", { name: "结束会议", exact: true }).click();
  await expect(page.getByRole("status").first()).toHaveText("未连接");
});

test("合成麦克风与屏幕经过真实采集上传流程并释放轨道", async ({ page, request }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "开始新会议", exact: true }).first().click();
  await expect(page.getByRole("status").first()).toHaveText("已连接");
  await expect
    .poll(async () => (await (await request.get("/__test/state")).json()).audio_frames)
    .toBeGreaterThan(0);
  const id = (await (await request.get("/__test/state")).json()).live;
  const uploaded = page.waitForResponse(
    (response) => response.url().endsWith("/api/frames") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "共享屏幕", exact: true }).click();
  expect((await uploaded).ok()).toBe(true);
  const frames = () =>
    request.get(`/api/frames?session_id=${id}`).then((response) => response.json());
  await expect.poll(async () => (await frames()).items.length).toBe(1);
  const first = (await frames()).items[0];
  expect(first).toMatchObject({ width: 640, height: 360 });
  const panel = page.getByRole("region", { name: "屏幕", exact: true });
  await expect(panel.getByRole("img")).toHaveCount(1);
  const firstImage = await request.get(`/api/frames/${first.id}/image`);
  expect(firstImage.ok()).toBe(true);
  expect(firstImage.headers()["content-type"]).toMatch(/^image\/(webp|jpeg)/);
  const firstBytes = await firstImage.body();
  await page.evaluate(() => (window as any).e2eMedia.changeScreen());
  await expect.poll(async () => (await frames()).items.length).toBe(2);
  const second = (await frames()).items[1];
  expect(second.t).toBeGreaterThan(first.t);
  expect(await (await request.get(`/api/frames/${second.id}/image`)).body()).not.toEqual(
    firstBytes,
  );
  await expect(panel.getByRole("img")).toHaveCount(2);
  await panel.getByRole("img").last().click();
  const image = page.getByRole("dialog", { name: "屏幕截图" }).getByRole("img");
  await expect
    .poll(() => image.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth))
    .toBe(640);
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: "停止共享", exact: true }).click();
  await expect
    .poll(() =>
      page.evaluate(() =>
        (window as any).e2eMedia.tracks
          .filter((track: MediaStreamTrack) => track.kind === "video")
          .every((track: MediaStreamTrack) => track.readyState === "ended"),
      ),
    )
    .toBe(true);
  expect(
    await page.evaluate(() =>
      (window as any).e2eMedia.tracks.some(
        (track: MediaStreamTrack) => track.kind === "audio" && track.readyState === "live",
      ),
    ),
  ).toBe(true);
  await page.getByRole("button", { name: "结束会议", exact: true }).click();
  await expect(page.getByRole("status").first()).toHaveText("未连接");
  await expect
    .poll(() =>
      page.evaluate(() =>
        (window as any).e2eMedia.tracks.every(
          (track: MediaStreamTrack) => track.readyState === "ended",
        ),
      ),
    )
    .toBe(true);
  await expect
    .poll(async () => (await (await request.get("/__test/state")).json()).live)
    .toBeNull();
});

test("选择窗口里取消、选择失败、浏览器自带停止共享之后都能再次共享", async ({ page, request }) => {
  const media = (mode: "share" | "cancel" | "fail") =>
    page.evaluate((value) => ((window as any).e2eMedia.screenMode = value), mode);
  const calls = () => page.evaluate(() => (window as any).e2eMedia.displayCalls.length);
  await page.goto("/");
  await page.getByRole("button", { name: "开始新会议", exact: true }).first().click();
  await expect(page.getByRole("status").first()).toHaveText("已连接");
  await serverReady(request, 1);
  const id = (await (await request.get("/__test/state")).json()).live;
  const share = page.getByRole("button", { name: "共享屏幕", exact: true });
  const stopShare = page.getByRole("button", { name: "停止共享", exact: true });
  const errors = page.locator(".notice-error");

  // 用户在选择窗口里点取消：各浏览器都抛 NotAllowedError，不算出错。
  await media("cancel");
  await share.click();
  await expect.poll(calls).toBe(1);
  await expect(share).toBeEnabled();
  await expect(errors).toHaveCount(0);

  // 浏览器读不到所选画面：说明原因，按钮仍可再点。
  await media("fail");
  await share.click();
  await expect(errors).toHaveText(/无法共享屏幕：无法读取所选窗口/);
  await expect(share).toBeEnabled();
  await errors.getByRole("button", { name: "关闭提示" }).click();
  await expect(errors).toHaveCount(0);

  // 正常共享后，用户点浏览器自带的「停止共享」。
  await media("share");
  await share.click();
  await expect(stopShare).toBeVisible();
  const frames = () =>
    request.get(`/api/frames?session_id=${id}`).then((response) => response.json());
  await expect.poll(async () => (await frames()).items.length).toBe(1);
  expect(await page.evaluate(() => (window as any).e2eMedia.endScreen())).toBe(1);
  await expect(share).toBeEnabled();
  await expect(stopShare).toHaveCount(0);
  await expect
    .poll(() =>
      page.evaluate(() =>
        (window as any).e2eMedia.tracks
          .filter((track: MediaStreamTrack) => track.kind === "video")
          .every((track: MediaStreamTrack) => track.readyState === "ended"),
      ),
    )
    .toBe(true);
  await expect(page.getByRole("status").first()).toHaveText("已连接");
  expect(
    await page.evaluate(() =>
      (window as any).e2eMedia.tracks.some(
        (track: MediaStreamTrack) => track.kind === "audio" && track.readyState === "live",
      ),
    ),
  ).toBe(true);

  // 同一场会议里可以再次共享；每次请求共享都发生在用户点击之内（Safari 要求）。
  await share.click();
  await expect(stopShare).toBeVisible();
  await stopShare.click();
  await expect(share).toBeEnabled();
  expect(await page.evaluate(() => (window as any).e2eMedia.displayCalls)).toEqual(
    Array(4).fill(true),
  );
  await page.getByRole("button", { name: "结束会议", exact: true }).click();
  await expect(page.getByRole("status").first()).toHaveText("未连接");
});
