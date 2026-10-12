import { test, expect, emit, serverReady } from "./fixtures.ts";

test("往上翻看字幕时新发言不把页面拽回底部，点「跟随最新」回到最新一条", async ({
  page,
  request,
}) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto("/");
  await page.getByRole("button", { name: "开始新会议", exact: true }).first().click();
  await expect(page.getByRole("status").first()).toHaveText("已连接");
  await serverReady(request, 1);

  const captions = page.getByRole("region", { name: "字幕", exact: true });
  for (let i = 1; i <= 30; i += 1) await emit(request, { text: `第 ${i} 条测试发言` });
  const last = captions.getByText("第 30 条测试发言", { exact: true });
  await expect(last).toBeInViewport();
  const follow = captions.getByRole("button", { name: "跟随最新", exact: true });
  await expect(follow).toHaveCount(0);

  await captions.getByText("第 1 条测试发言", { exact: true }).scrollIntoViewIfNeeded();
  await expect(follow).toBeVisible();
  await emit(request, { text: "翻看时说的新发言" });
  const latest = captions.getByText("翻看时说的新发言", { exact: true });
  await expect(latest).toHaveCount(1);
  await expect(latest).not.toBeInViewport();
  await expect(captions.getByText("第 1 条测试发言", { exact: true })).toBeInViewport();

  await follow.click();
  await expect(latest).toBeInViewport();
  await expect(follow).toHaveCount(0);
  await emit(request, { text: "回到底部后的发言" });
  await expect(captions.getByText("回到底部后的发言", { exact: true })).toBeInViewport();
});
