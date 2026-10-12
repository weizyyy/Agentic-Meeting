import { test, expect, serverReady } from "./fixtures.ts";
import type { Page } from "@playwright/test";

const background = (page: Page) =>
  page.evaluate(() => getComputedStyle(document.body).backgroundColor);

async function chooseTheme(page: Page, label: string) {
  await page.getByRole("button", { name: "主题", exact: true }).click();
  await page.getByRole("menuitemradio", { name: label, exact: true }).click();
  await expect(page.getByRole("menu")).toBeHidden();
}

test("主题默认跟随系统，选浅色或深色后刷新仍保持，改回跟随系统又随系统变化", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "组会助理" })).toBeVisible();
  const dark = await background(page);
  await page.emulateMedia({ colorScheme: "light" });
  await expect.poll(() => background(page)).not.toBe(dark);
  const light = await background(page);

  await chooseTheme(page, "深色");
  await expect.poll(() => background(page)).toBe(dark);
  await page.reload();
  await expect.poll(() => background(page)).toBe(dark);
  await page.getByRole("button", { name: "主题", exact: true }).click();
  await expect(page.getByRole("menuitemradio", { name: "深色", exact: true })).toBeChecked();
  await page.keyboard.press("Escape");

  await page.emulateMedia({ colorScheme: "dark" });
  await chooseTheme(page, "浅色");
  await expect.poll(() => background(page)).toBe(light);
  await page.reload();
  await expect.poll(() => background(page)).toBe(light);

  await chooseTheme(page, "跟随系统");
  await expect.poll(() => background(page)).toBe(dark);
  await page.reload();
  await page.emulateMedia({ colorScheme: "light" });
  await expect.poll(() => background(page)).toBe(light);
});

test("会议中顶部栏的麦克风按钮可以静音和恢复，结束会议后按钮消失", async ({ page, request }) => {
  await page.goto("/");
  await expect(page.getByRole("button", { name: /麦克风|已静音/ })).toHaveCount(0);
  await page.getByRole("button", { name: "会议列表", exact: true }).click();
  await page.getByRole("button", { name: "继续：月面计划讨论", exact: true }).click();
  await expect(page.getByRole("status").first()).toHaveText("已连接");
  await serverReady(request, 1);

  const mic = page.getByRole("button", { name: "麦克风", exact: true });
  await expect(mic).toHaveAttribute("aria-pressed", "true");
  await mic.click();
  const muted = page.getByRole("button", { name: "已静音", exact: true });
  await expect(muted).toHaveAttribute("aria-pressed", "false");
  await expect(page.getByRole("status").first()).toHaveText("已连接");
  await muted.click();
  await expect(mic).toHaveAttribute("aria-pressed", "true");
  // 恢复后服务端继续收到声音
  const frames = async () => (await (await request.get("/__test/state")).json()).audio_frames;
  const resumed = await frames();
  await expect.poll(frames).toBeGreaterThan(resumed);

  await page.getByRole("button", { name: "音频设备", exact: true }).click();
  await expect(page.getByRole("menu")).toBeVisible();
  await page.keyboard.press("Escape");

  await page.getByRole("button", { name: "结束会议", exact: true }).click();
  await expect(page.getByRole("status").first()).toHaveText("未连接");
  await expect(page.getByRole("button", { name: /麦克风|已静音/ })).toHaveCount(0);
});
