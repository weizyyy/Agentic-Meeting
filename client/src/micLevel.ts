// 麦克风轨道的诊断信息。

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
