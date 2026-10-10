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
    const tracks = stream
      .getVideoTracks()
      .map((track) => ({ state: track.readyState, kind: track.kind }));
    const video = document.createElement("video");
    video.muted = true;
    video.playsInline = true;
    video.srcObject = stream;
    let playback = "pending";
    void video.play().then(
      () => (playback = "resolved"),
      (error) => (playback = String(error)),
    );
    try {
      const deadline = Date.now() + 3000;
      while (video.readyState < 2 && Date.now() < deadline)
        await new Promise((resolve) => setTimeout(resolve, 50));
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = 1;
      const context = canvas.getContext("2d")!;
      if (video.readyState >= 2) context.drawImage(video, 0, 0, 1, 1);
      return {
        tracks,
        width: video.videoWidth,
        height: video.videoHeight,
        readyState: video.readyState,
        playback,
        pixel: Array.from(context.getImageData(0, 0, 1, 1).data),
      };
    } finally {
      stream.getTracks().forEach((track) => track.stop());
      video.srcObject = null;
    }
  });
  expect(screen.tracks).toEqual([{ state: "live", kind: "video" }]);
  expect(screen).toMatchObject({
    width: 640,
    height: 360,
    pixel: [52, 86, 120, 255],
  });
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
