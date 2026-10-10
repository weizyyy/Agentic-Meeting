import { test, expect } from "./fixtures.ts";

test("真实页面、WebRTC、RTVI 与合成媒体探针", async ({ page, request }) => {
  const seeds = await (await request.get("/__test/state")).json();
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "组会助理" })).toBeVisible();
  await page.getByRole("button", { name: "会议列表", exact: true }).click();
  await expect(page.getByRole("button", { name: /星河项目复盘 已结束/ })).toBeVisible();
  await page.getByRole("button", { name: "继续：月面计划讨论", exact: true }).click();
  await expect(page.getByRole("status").first()).toHaveText("已连接");
  await expect.poll(async () => (await (await request.get("/__test/state")).json()).ready).toBe(1);
  await expect
    .poll(async () => (await (await request.get("/__test/state")).json()).audio_frames)
    .toBeGreaterThan(0);
  expect((await (await request.get("/__test/state")).json()).live).toBe(seeds.interrupted);
  await request.post("/__test/emit", { data: { text: "来自真实数据通道的字幕", final: false } });
  await expect(page.getByText("来自真实数据通道的字幕", { exact: true })).toBeVisible();
  const final = await request.post("/__test/emit", { data: { text: "已经保存的实时定稿" } });
  expect(final.ok()).toBe(true);
  await expect(page.getByText("已经保存的实时定稿", { exact: true })).toBeVisible();
  const detail = await (
    await request.get(`/api/utterances?session_id=${seeds.interrupted}`)
  ).json();
  expect(JSON.stringify(detail)).toContain("已经保存的实时定稿");
  const screen = await page.evaluate(async () => {
    const stream = await navigator.mediaDevices.getDisplayMedia({ video: true });
    const result = stream
      .getVideoTracks()
      .map((track) => ({ state: track.readyState, kind: track.kind }));
    stream.getTracks().forEach((track) => track.stop());
    return result;
  });
  expect(screen).toEqual([{ state: "live", kind: "video" }]);
  await page.getByRole("button", { name: "结束会议", exact: true }).click();
  await expect(page.getByRole("status").first()).toHaveText("未连接");
  await expect
    .poll(async () => (await (await request.get("/__test/state")).json()).live)
    .toBeNull();
  await expect
    .poll(() =>
      page.evaluate(() =>
        (window as any).e2eMedia.tracks.every(
          (track: MediaStreamTrack) => track.readyState === "ended",
        ),
      ),
    )
    .toBe(true);
});
