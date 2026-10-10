import type { Download, Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { test, expect } from "./fixtures.ts";

async function openEndedMeeting(page: Page) {
  await expect(page.getByRole("group", { name: "当前查看的会议" })).toContainText("月面计划讨论");
  await page.getByRole("button", { name: "会议列表", exact: true }).click();
  await page.getByRole("button", { name: /星河项目复盘 已结束/ }).click();
  await expect(page.getByRole("group", { name: "当前查看的会议" })).toContainText("星河项目复盘");
}

test.beforeEach(async ({ page }) => {
  await page.goto("/");
});

async function downloadText(download: Download) {
  expect(await download.failure()).toBeNull();
  const path = await download.path();
  expect(path).not.toBeNull();
  return readFile(path!, "utf8");
}

test("从会议列表切换历史字幕，并按现有行为刷新恢复当前会议", async ({ page }) => {
  await openEndedMeeting(page);
  const captions = page.getByRole("region", { name: "字幕", exact: true });
  await expect(captions.getByText("星河测试第一条发言", { exact: true })).toBeVisible();
  await expect(captions.getByText("星河测试第二条发言", { exact: true })).toBeVisible();
  await expect(
    captions.getByRole("button", { name: "选中 测试同学1 在 0:01 的发言" }),
  ).toBeVisible();
  await expect(
    captions.getByRole("button", { name: "选中 测试同学2 在 0:02 的发言" }),
  ).toBeVisible();
  await expect(captions).not.toContainText("月面计划保留的历史字幕");

  await page.getByRole("button", { name: "会议列表", exact: true }).click();
  await page.getByRole("button", { name: /月面计划讨论 已中断/ }).click();
  await expect(page.getByRole("group", { name: "当前查看的会议" })).toContainText("月面计划讨论");
  await expect(captions.getByText("月面计划保留的历史字幕", { exact: true })).toBeVisible();
  await expect(captions).not.toContainText("星河测试");
  await expect(page.getByRole("region", { name: "后台任务", exact: true })).toHaveCount(0);
  await expect(
    page.getByRole("region", { name: "屏幕", exact: true }).getByRole("img"),
  ).toHaveCount(0);

  await page.reload();
  await expect(page.getByRole("group", { name: "当前查看的会议" })).toContainText("月面计划讨论");
  await expect(captions.getByText("月面计划保留的历史字幕", { exact: true })).toBeVisible();
  await expect(
    captions.getByRole("button", { name: "选中 测试同学1 在 0:01 的发言" }),
  ).toBeVisible();
  await expect(captions).not.toContainText("星河测试");
  await expect(page.getByRole("status").first()).toHaveText("未连接");
});

test("通过页面改名及合并说话人，刷新后字幕归属与数据库一致", async ({ page, request }) => {
  const seeds = await (await request.get("/__test/state")).json();
  await openEndedMeeting(page);
  const speakers = page.getByRole("group", { name: "说话人", exact: true });
  const captions = page.getByRole("region", { name: "字幕", exact: true });
  await speakers.getByRole("button", { name: "测试同学1", exact: true }).click();
  await speakers.getByRole("combobox", { name: "给「测试同学1」改名" }).fill("星河主持人");
  await speakers.getByRole("combobox", { name: "给「测试同学1」改名" }).press("Enter");
  await expect(speakers.getByRole("button", { name: "星河主持人", exact: true })).toBeVisible();
  await expect(
    captions.getByRole("button", { name: "选中 星河主持人 在 0:01 的发言" }),
  ).toBeVisible();
  const renamed = await (await request.get(`/api/speakers?session_id=${seeds.ended}`)).json();
  expect(renamed.items).toContainEqual({ idx: 1, display_name: "星河主持人" });

  await page.reload();
  await openEndedMeeting(page);
  await expect(speakers.getByRole("button", { name: "星河主持人", exact: true })).toBeVisible();
  await expect(
    captions.getByRole("button", { name: "选中 星河主持人 在 0:01 的发言" }),
  ).toBeVisible();
  await speakers.getByRole("button", { name: "测试同学2", exact: true }).click();
  page.once("dialog", async (dialog) => {
    expect(dialog.type()).toBe("confirm");
    expect(dialog.message()).toContain("把「测试同学2」的全部发言都算到「星河主持人」名下吗？");
    await dialog.accept();
  });
  await speakers
    .getByRole("combobox", { name: "把「测试同学2」合并到另一个说话人" })
    .selectOption("1");
  await expect(speakers.getByRole("button")).toHaveText(["星河主持人"]);
  await expect(captions.getByRole("button", { name: /选中 星河主持人 在/ })).toHaveCount(2);
  await expect(captions).not.toContainText("测试同学2");
  const merged = await (await request.get(`/api/speakers?session_id=${seeds.ended}`)).json();
  expect(merged.items).toEqual([{ idx: 1, display_name: "星河主持人" }]);
  const history = await (await request.get(`/api/utterances?session_id=${seeds.ended}`)).json();
  expect(history.items).toHaveLength(2);
  expect(
    history.items.every(
      (item: { speaker_idx: number; speaker_name: string }) =>
        item.speaker_idx === 1 && item.speaker_name === "星河主持人",
    ),
  ).toBe(true);

  await page.reload();
  await openEndedMeeting(page);
  await expect(speakers.getByRole("button")).toHaveText(["星河主持人"]);
  await expect(captions.getByRole("button", { name: /选中 星河主持人 在/ })).toHaveCount(2);
  await expect(captions.getByText("星河测试第一条发言", { exact: true })).toBeVisible();
  await expect(captions.getByText("星河测试第二条发言", { exact: true })).toBeVisible();
  await expect(captions).not.toContainText("测试同学2");
  const other = await (await request.get(`/api/speakers?session_id=${seeds.interrupted}`)).json();
  expect(other.items).toEqual([{ idx: 1, display_name: "测试同学1" }]);
});

test("打开成功任务详情并下载真实产物", async ({ page, request }) => {
  const seeds = await (await request.get("/__test/state")).json();
  await openEndedMeeting(page);
  const tasks = page.getByRole("region", { name: "后台任务", exact: true });
  await expect(tasks).toContainText("已完成");
  await tasks.getByRole("button", { name: /整理星河测试结论/ }).click();
  const detail = page.getByRole("dialog", { name: /^任务 / });
  await expect(detail).toContainText("已完成");
  await expect(detail).toContainText("已整理虚构结论");
  await expect(
    detail.getByText("## 星河测试结果\n只包含虚构会议内容。", { exact: true }),
  ).toBeVisible();
  const artifact = detail.getByRole("link", { name: "result.txt", exact: true });
  await expect(artifact).toHaveAttribute("href", `/api/tasks/${seeds.task}/artifacts/result.txt`);
  const downloaded = page.waitForEvent("download");
  await artifact.click();
  const download = await downloaded;
  expect(download.suggestedFilename()).toBe("result.txt");
  expect(await downloadText(download)).toBe("星河测试产物\n");
  await detail.getByRole("button", { name: "关闭", exact: true }).click();
  await expect(detail).toHaveCount(0);
});

test("任务多时右列先让助理回答区留够几行，后台任务看板缩到能看全最新一个", async ({ page }) => {
  await page.setViewportSize({ width: 1350, height: 900 });
  await openEndedMeeting(page);
  const tasks = page.getByRole("region", { name: "后台任务", exact: true });
  const cards = tasks.getByRole("button");
  await expect(cards).toHaveCount(3);
  await expect(cards.first()).toContainText("前后两轮虚构结果");
  await expect(cards.first()).toBeInViewport({ ratio: 1 });
  await expect(cards.nth(1)).not.toBeInViewport({ ratio: 1 });
  const placeholder = page
    .getByRole("region", { name: "助理", exact: true })
    .getByText("它的回答会显示在这里");
  const lines = await placeholder.evaluate(
    (element) =>
      element.parentElement!.getBoundingClientRect().height /
      parseFloat(getComputedStyle(element).lineHeight),
  );
  expect(Math.round(lines)).toBeGreaterThanOrEqual(4);
});

test("时间轴截图打开后加载真实图片及对应元数据", async ({ page, request }) => {
  const seeds = await (await request.get("/__test/state")).json();
  await openEndedMeeting(page);
  const screen = page.getByRole("region", { name: "屏幕", exact: true });
  const thumbnail = screen.getByRole("img", { name: "00:00:03 的屏幕截图", exact: true });
  await expect(thumbnail).toBeVisible();
  await expect(thumbnail).toHaveAttribute("src", `/api/frames/${seeds.frame}/image`);
  await expect
    .poll(() =>
      thumbnail.evaluate((image: HTMLImageElement) => [
        image.complete,
        image.naturalWidth,
        image.naturalHeight,
      ]),
    )
    .toEqual([true, 320, 180]);
  await thumbnail.click();
  const lightbox = page.getByRole("dialog", { name: "屏幕截图", exact: true });
  const image = lightbox.getByRole("img", { name: "屏幕截图", exact: true });
  await expect(image).toBeVisible();
  await expect(image).toHaveAttribute("src", `/api/frames/${seeds.frame}/image`);
  await expect
    .poll(() =>
      image.evaluate((element: HTMLImageElement) => [
        element.complete,
        element.naturalWidth,
        element.naturalHeight,
      ]),
    )
    .toEqual([true, 320, 180]);
  await expect(lightbox).toContainText("00:00:03");
  await expect(lightbox).toContainText("（还没有画面摘要）");
  await expect(lightbox).toContainText("1 / 1");
  await lightbox.getByRole("button", { name: "关闭", exact: true }).click();
  await expect(lightbox).toHaveCount(0);
});

test("真实下载转录和 JSON 导出，核对会议身份与内容", async ({ page, request }) => {
  const seeds = await (await request.get("/__test/state")).json();
  await openEndedMeeting(page);
  const banner = page.getByRole("group", { name: "当前查看的会议" });
  const markdownDownload = page.waitForEvent("download");
  await banner.getByRole("link", { name: "转录", exact: true }).click();
  const markdown = await downloadText(await markdownDownload);
  expect(markdown).toContain("星河项目复盘");
  expect(markdown).toContain("测试同学1");
  expect(markdown).toContain("星河测试第一条发言");
  expect(markdown).toContain("星河测试第二条发言");
  expect(markdown).not.toContain("月面计划保留的历史字幕");

  const jsonDownload = page.waitForEvent("download");
  await banner.getByRole("link", { name: "JSON", exact: true }).click();
  const exported = JSON.parse(await downloadText(await jsonDownload));
  expect(exported.session).toMatchObject({
    id: seeds.ended,
    title: "星河项目复盘",
    state: "ended",
  });
  expect(exported.utterances).toMatchObject([
    { text: "星河测试第一条发言", speaker_idx: 1, speaker_name: "测试同学1" },
    { text: "星河测试第二条发言", speaker_idx: 2, speaker_name: "测试同学2" },
  ]);
  expect(exported.frames).toMatchObject([{ id: seeds.frame, t: 3, width: 320, height: 180 }]);
  expect(exported.tasks).toMatchObject([
    { id: seeds.task, status: "succeeded", goal: "整理星河测试结论" },
    { status: "succeeded", goal: expect.stringContaining("四组虚构数据") },
    { status: "succeeded", goal: expect.stringContaining("前后两轮虚构结果") },
  ]);
  expect(JSON.stringify(exported)).not.toContain("月面计划");
});
