import type { Page } from "@playwright/test";
import { test, expect } from "./fixtures.ts";

const MIN_BROWSERS = "Chrome 111、Edge 111、Firefox 114、Safari 16.4";

/** 不能开会的浏览器里：说明原因，开始和继续都不发起连接，历史会议照常可看。 */
async function expectHistoryOnly(page: Page, reason: string | RegExp) {
  const request = page.request;
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText(reason);
  await expect(page.getByRole("button", { name: "开始新会议", exact: true })).toBeDisabled();

  await page.getByRole("button", { name: "会议列表", exact: true }).click();
  await page.getByRole("button", { name: "继续：月面计划讨论", exact: true }).click();
  await expect(page.locator(".notice-error")).toContainText(reason);
  await expect(page.getByRole("status").first()).toHaveText("未连接");

  await page.getByRole("button", { name: "会议列表", exact: true }).click();
  await page.getByRole("button", { name: /星河项目复盘 已结束/ }).click();
  const captions = page.getByRole("region", { name: "字幕", exact: true });
  await expect(captions.getByText("星河测试第一条发言", { exact: true })).toBeVisible();

  const state = await (await request.get("/__test/state")).json();
  expect(state).toMatchObject({ ready: 0, live: null });
}

test("缺少 WebRTC 的浏览器说明原因，不连接，只能查看历史会议", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, "RTCPeerConnection", { value: undefined });
  });
  await expectHistoryOnly(page, "缺少WebRTC接口");
  await expect(page.getByRole("alert")).toContainText(MIN_BROWSERS);
});

test("不是通过 HTTPS 打开时提示改用 HTTPS，不连接，只能查看历史会议", async ({ page }) => {
  // 跨机器用 http:// 打开时浏览器给的就是这样的环境：不是安全上下文，也没有麦克风接口。
  await page.addInitScript(() => {
    Object.defineProperty(window, "isSecureContext", { value: false });
    Object.defineProperty(navigator, "mediaDevices", { value: undefined });
  });
  await expectHistoryOnly(page, "不是通过 HTTPS 或 localhost 打开的");
});

test("主脚本无法运行的旧浏览器显示最低版本，而不是白页", async ({ page }) => {
  // 用一段新语法解析不了的脚本代替打包结果，效果和旧浏览器遇到新语法一样。
  await page.route(/\/assets\/.*\.js$/, (route) =>
    route.fulfill({ contentType: "text/javascript", body: "export const broken = ;" }),
  );
  await page.goto("/");
  await expect(page.getByRole("alert")).toHaveText(
    `页面没能启动。浏览器版本可能太旧，请使用 ${MIN_BROWSERS} 或更新的版本；已经是新版本的话，请刷新页面重试。`,
  );
});

test("能开会的浏览器不显示兼容提示", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "组会助理" })).toBeVisible();
  await expect(page.getByRole("button", { name: "开始新会议", exact: true })).toBeEnabled();
  await expect(page.getByRole("alert")).toHaveCount(0);
});
