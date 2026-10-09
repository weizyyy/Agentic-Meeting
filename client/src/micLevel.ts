/**
 * 麦克风电平的纯逻辑（可以用 `npm test` 直接测）：从 AnalyserNode 的时域数据算电平，映射成电平条的显示值。
 * 这里看到的是浏览器采集后、**服务端收音增强之前**的电平，用来让人一眼看出「麦克风有没有收到声音、有多轻」。
 */

/** 电平条的下沿：低于它显示为空（dBFS）。 */
export const METER_FLOOR_DB = -60;
/** 低于它（说话时）就偏轻了，电平条变成提醒色。与服务端的「电平过低」阈值同一量级。 */
export const LOW_LEVEL_DB = -50;

/** `AnalyserNode.getByteTimeDomainData` 给出的是 0–255、中点 128，换算成 RMS（0–1）。 */
export function rmsFromTimeDomain(data: Uint8Array): number {
  if (data.length === 0) return 0;
  let sum = 0;
  for (const v of data) {
    const x = (v - 128) / 128;
    sum += x * x;
  }
  return Math.sqrt(sum / data.length);
}

/** RMS（0–1）→ dBFS；静音返回 `-Infinity`。 */
export function toDbfs(rms: number): number {
  return rms > 0 ? 20 * Math.log10(rms) : -Infinity;
}

/** 电平条的显示值 0–1：−60 dBFS 以下为 0，0 dBFS 为 1，中间按分贝线性。 */
export function meterFraction(rms: number): number {
  const db = toDbfs(rms);
  if (!Number.isFinite(db)) return 0;
  return Math.min(1, Math.max(0, (db - METER_FLOOR_DB) / -METER_FLOOR_DB));
}

export type MeterZone = "silent" | "low" | "ok";

/** 电平条的颜色分区：几乎没有声音 / 偏轻 / 正常。 */
export function meterZone(rms: number): MeterZone {
  const db = toDbfs(rms);
  if (db < METER_FLOOR_DB) return "silent";
  return db < LOW_LEVEL_DB ? "low" : "ok";
}

/** 取一小段时间内的最大电平，让电平条下降得慢一点、不闪烁。 */
export function holdPeak(previous: number, current: number, decayPerFrame = 0.9): number {
  return Math.max(current, previous * decayPerFrame);
}

/** 把轨道设置整理成一行说明，打到控制台，方便确认浏览器的自动增益、降噪、回声消除有没有开。 */
export function describeTrackSettings(settings: MediaTrackSettings): string {
  // 个别浏览器把 echoCancellation 报成字符串（"all" / "remote-only"），原样写出来。
  const flag = (value: boolean | string | undefined) =>
    value === undefined ? "未知" : typeof value === "string" ? value : value ? "开" : "关";
  const parts = [
    `设备 ${settings.deviceId ?? "未知"}`,
    `自动增益 ${flag(settings.autoGainControl)}`,
    `降噪 ${flag(settings.noiseSuppression)}`,
    `回声消除 ${flag(settings.echoCancellation)}`,
  ];
  if (settings.sampleRate !== undefined) parts.push(`采样率 ${settings.sampleRate}`);
  return parts.join("，");
}
