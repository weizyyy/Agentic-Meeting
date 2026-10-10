import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  DEFAULT_SCREEN_CONFIG,
  MAX_TIMELINE_FRAMES,
  applyFrame,
  applyFrameCaption,
  clockOffset,
  adjacentFrameIndex,
  formatFrameTime,
  frameDifference,
  frameImageUrl,
  mergeFrames,
  parseScreenConfig,
  replaceFrames,
  scaledSize,
  toGray,
  uploadDecision,
  type FrameItem,
} from "./screenCapture.ts";

describe("parseScreenConfig", () => {
  it("读取服务端下发的 screen 段", () => {
    assert.deepEqual(
      parseScreenConfig({
        enabled: false,
        min_interval_secs: 3,
        heartbeat_secs: 30,
        max_side_px: 1280,
        change_threshold: 0.1,
        caption: true,
        caption_provider: "realtime_llm",
      }),
      {
        enabled: false,
        minIntervalSecs: 3,
        heartbeatSecs: 30,
        maxSidePx: 1280,
        changeThreshold: 0.1,
      },
    );
  });

  it("缺的、类型不对的、不合理的字段用默认值", () => {
    assert.deepEqual(parseScreenConfig(null), DEFAULT_SCREEN_CONFIG);
    assert.deepEqual(parseScreenConfig("x"), DEFAULT_SCREEN_CONFIG);
    assert.deepEqual(
      parseScreenConfig({
        enabled: "yes",
        min_interval_secs: 0,
        heartbeat_secs: -1,
        max_side_px: "big",
        change_threshold: 2,
      }),
      DEFAULT_SCREEN_CONFIG,
    );
    // 阈值 0 是合法的（任何变化都上传）
    assert.equal(parseScreenConfig({ change_threshold: 0 }).changeThreshold, 0);
  });
});

describe("clockOffset", () => {
  it("取往返最短的一次，按请求中点估算", () => {
    const offset = clockOffset([
      { t0: 100, t1: 100.4, serverTime: 160.9 }, // 往返 0.4
      { t0: 101, t1: 101.1, serverTime: 161.05 }, // 往返 0.1 ← 用这次
      { t0: 102, t1: 102.3, serverTime: 162.7 },
    ]);
    assert.ok(Math.abs(offset - 60) < 1e-9);
  });

  it("服务端时间更早时偏移量为负；没有样本为 0；坏样本跳过", () => {
    assert.equal(clockOffset([{ t0: 10, t1: 10, serverTime: 4 }]), -6);
    assert.equal(clockOffset([]), 0);
    assert.equal(
      clockOffset([
        { t0: 5, t1: 4, serverTime: 999 }, // 往返为负：时钟被调过
        { t0: 5, t1: 5.2, serverTime: Number.NaN },
        { t0: 5, t1: 6, serverTime: 7.5 },
      ]),
      2,
    );
  });
});

describe("toGray / frameDifference", () => {
  it("RGBA 转灰度，忽略透明度", () => {
    const gray = toGray([255, 255, 255, 0, 0, 0, 0, 255, 255, 0, 0, 255, 1, 2]);
    assert.deepEqual([...gray], [255, 0, 76]); // 末尾不足一个像素的丢弃
  });

  it("标准缩略图：取变化最大的那一块", () => {
    const size = 64 * 36;
    const white = new Uint8Array(size).fill(255);
    assert.equal(frameDifference(white, white), 0);
    assert.equal(frameDifference(white, new Uint8Array(size)), 1);
    // 白底上只改了几行字：整张平均差很小，有字的那一块差一半
    const page = white.slice();
    for (let row = 12; row < 15; row += 1) page.fill(0, row * 64 + 8, row * 64 + 24);
    assert.equal(frameDifference(white, page), 0.5);
  });

  it("非标准尺寸：差的平均值 / 255", () => {
    const black = new Uint8Array(8);
    const white = new Uint8Array(8).fill(255);
    assert.equal(frameDifference(black, black), 0);
    assert.equal(frameDifference(black, white), 1);
    assert.equal(frameDifference(white, black), 1);
    const half = new Uint8Array([255, 255, 255, 255, 0, 0, 0, 0]);
    assert.equal(frameDifference(black, half), 0.5);
  });

  it("长度对不上或为空按完全不同算", () => {
    assert.equal(frameDifference(new Uint8Array(4), new Uint8Array(5)), 1);
    assert.equal(frameDifference(new Uint8Array(0), new Uint8Array(0)), 1);
  });
});

describe("uploadDecision", () => {
  const config = { minIntervalSecs: 2, heartbeatSecs: 60, changeThreshold: 0.04 };

  it("第一帧总是上传", () => {
    assert.equal(uploadDecision(1000, null, 0, config), "first");
  });

  it("画面变化超过阈值且满最小间隔才上传", () => {
    assert.equal(uploadDecision(1002, 1000, 0.05, config), "change");
    assert.equal(uploadDecision(1001.9, 1000, 0.9, config), null); // 变了但太快
    assert.equal(uploadDecision(1010, 1000, 0.04, config), null); // 正好等于阈值不算变
    assert.equal(uploadDecision(1010, 1000, 0, config), null);
  });

  it("画面不变时满兜底间隔上传", () => {
    assert.equal(uploadDecision(1059.9, 1000, 0, config), null);
    assert.equal(uploadDecision(1060, 1000, 0, config), "heartbeat");
    assert.equal(uploadDecision(1060, 1000, 0.5, config), "change"); // 两个条件都满足时算变化
  });
});

describe("scaledSize", () => {
  it("等比缩到长边不超过上限，只缩不放", () => {
    assert.deepEqual(scaledSize(3840, 2160, 1920), { width: 1920, height: 1080 });
    assert.deepEqual(scaledSize(1080, 2400, 1920), { width: 864, height: 1920 });
    assert.deepEqual(scaledSize(1280, 720, 1920), { width: 1280, height: 720 });
    assert.deepEqual(scaledSize(1920, 1080, 1920), { width: 1920, height: 1080 });
  });

  it("极端比例不会出现 0，也不会因为取整超过上限", () => {
    assert.deepEqual(scaledSize(10000, 1, 1920), { width: 1920, height: 1 });
    assert.deepEqual(scaledSize(0, 0, 1920), { width: 1, height: 1 });
    const s = scaledSize(2561, 1441, 1920);
    assert.ok(s.width <= 1920 && s.height <= 1920);
  });
});

describe("时间线", () => {
  const f = (id: number, t: number, caption: string | null = null): FrameItem => ({
    id,
    t,
    width: 16,
    height: 9,
    caption,
  });

  it("新截图按时间插入，重复的只留一份且不丢摘要", () => {
    let frames = applyFrame([], f(2, 20));
    frames = applyFrame(frames, f(1, 10));
    assert.deepEqual(
      frames.map((x) => x.id),
      [1, 2],
    );
    frames = applyFrameCaption(frames, 2, "一张表");
    frames = applyFrame(frames, { id: 2, t: 20, width: 16, height: 9 });
    assert.equal(frames.length, 2);
    assert.equal(frames[1].caption, "一张表");
  });

  it("摘要只更新对应的那张；不认识的编号不变", () => {
    const frames = [f(1, 10), f(2, 20)];
    assert.deepEqual(applyFrameCaption(frames, 1, "幻灯片"), [f(1, 10, "幻灯片"), f(2, 20)]);
    assert.equal(applyFrameCaption(frames, 9, "x"), frames);
  });

  it("replaceFrames 排序；mergeFrames 以服务端为准并保留本地多出来的", () => {
    assert.deepEqual(
      replaceFrames([f(3, 30), f(1, 10)]).map((x) => x.id),
      [1, 3],
    );
    const merged = mergeFrames([f(1, 10), f(5, 50)], [f(1, 10, "有摘要了"), f(2, 20)]);
    assert.deepEqual(merged, [f(1, 10, "有摘要了"), f(2, 20), f(5, 50)]);
  });

  it("时间线有上限，丢最早的", () => {
    let frames: FrameItem[] = [];
    for (let i = 0; i < MAX_TIMELINE_FRAMES + 5; i += 1) frames = applyFrame(frames, f(i, i));
    assert.equal(frames.length, MAX_TIMELINE_FRAMES);
    assert.equal(frames[0].id, 5);
  });

  it("图片地址与时间格式", () => {
    assert.equal(frameImageUrl(12), "/api/frames/12/image");
    assert.equal(formatFrameTime(0), "00:00:00");
    assert.equal(formatFrameTime(3725.9), "01:02:05");
    assert.equal(formatFrameTime(-3), "00:00:00");
  });
});

it("大图里左右切换：到头就停，图不在列表里不动", () => {
  const frames = [{ id: 3 }, { id: 7 }, { id: 9 }];
  assert.equal(adjacentFrameIndex(frames, 7, -1), 0);
  assert.equal(adjacentFrameIndex(frames, 7, 1), 2);
  assert.equal(adjacentFrameIndex(frames, 3, -1), -1);
  assert.equal(adjacentFrameIndex(frames, 9, 1), -1);
  assert.equal(adjacentFrameIndex(frames, 42, 1), -1);
  assert.equal(adjacentFrameIndex([], 1, 1), -1);
});
