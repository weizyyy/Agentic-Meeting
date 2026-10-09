import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  describeTrackSettings,
  holdPeak,
  meterFraction,
  meterZone,
  rmsFromTimeDomain,
  toDbfs,
} from "./micLevel.ts";

describe("rmsFromTimeDomain", () => {
  it("静音（全是中点 128）为 0，空数据为 0", () => {
    assert.equal(rmsFromTimeDomain(new Uint8Array(1024).fill(128)), 0);
    assert.equal(rmsFromTimeDomain(new Uint8Array(0)), 0);
  });

  it("满幅方波的 RMS 是 1", () => {
    const data = new Uint8Array(1000);
    data.forEach((_, i) => (data[i] = i % 2 === 0 ? 255 : 0));
    // 255 → +0.992，0 → −1：RMS 接近 1
    assert.ok(Math.abs(rmsFromTimeDomain(data) - 1) < 0.01);
  });

  it("半幅方波约为 0.5", () => {
    const data = new Uint8Array(1000);
    data.forEach((_, i) => (data[i] = i % 2 === 0 ? 192 : 64));
    assert.equal(rmsFromTimeDomain(data), 0.5);
  });
});

describe("toDbfs / meterFraction", () => {
  it("0.1 是 −20 dBFS，静音是 −Infinity", () => {
    assert.ok(Math.abs(toDbfs(0.1) + 20) < 1e-9);
    assert.equal(toDbfs(0), -Infinity);
  });

  it("−60 dBFS 以下为 0，0 dBFS 为 1，−30 dBFS 为一半", () => {
    assert.equal(meterFraction(0), 0);
    assert.equal(meterFraction(0.0005), 0); // −66 dBFS
    assert.equal(meterFraction(1), 1);
    assert.equal(meterFraction(2), 1); // 超出不越界
    assert.ok(Math.abs(meterFraction(Math.pow(10, -30 / 20)) - 0.5) < 1e-9);
  });
});

describe("meterZone", () => {
  it("按电平分成几乎没有声音 / 偏轻 / 正常", () => {
    assert.equal(meterZone(0), "silent");
    assert.equal(meterZone(Math.pow(10, -65 / 20)), "silent");
    assert.equal(meterZone(Math.pow(10, -55 / 20)), "low");
    assert.equal(meterZone(Math.pow(10, -40 / 20)), "ok");
  });
});

describe("holdPeak", () => {
  it("上升立刻跟上，下降按比例衰减", () => {
    assert.equal(holdPeak(0.2, 0.6), 0.6);
    assert.ok(Math.abs(holdPeak(0.5, 0.1) - 0.45) < 1e-9);
    assert.equal(holdPeak(0.5, 0.1, 0.5), 0.25);
  });
});

describe("describeTrackSettings", () => {
  it("写明自动增益、降噪、回声消除的开关", () => {
    const text = describeTrackSettings({
      deviceId: "abc",
      autoGainControl: true,
      noiseSuppression: false,
      echoCancellation: true,
      sampleRate: 48000,
    });
    assert.equal(text, "设备 abc，自动增益 开，降噪 关，回声消除 开，采样率 48000");
  });

  it("浏览器没给的项写「未知」", () => {
    assert.equal(
      describeTrackSettings({}),
      "设备 未知，自动增益 未知，降噪 未知，回声消除 未知",
    );
  });
});
