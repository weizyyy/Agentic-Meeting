/**
 * 本机麦克风轨道与 WebRTC 连接的对接（可以用 `npm test` 直接测）。
 *
 * 页面用 SDK 自带的 `WavMediaManager` 取麦克风：默认的 `DailyMediaManager` 要从 c.daily.co 下载脚本，
 * 浏览器上不了外网就开不了会（docs/pipecat-notes.md §12）。`WavMediaManager` 在系统默认麦克风变化时会换一条新轨道，
 * 但不会把它换进连接；这里补上这一步。
 */

/** SmallWebRTC 传输层上取音频收发器的方法（1.10.8 的内部方法，类型声明里没有）。 */
interface AudioTransceiverSource {
  getAudioTransceiver?: () => RTCRtpTransceiver | undefined;
}

/**
 * 让连接发送的是这条麦克风轨道。还没有连接、或者发的已经是它时什么都不做。
 * 返回是否换了轨道。
 */
export async function sendLocalAudio(
  transport: unknown,
  track: MediaStreamTrack,
): Promise<boolean> {
  const source = transport as AudioTransceiverSource | null;
  if (typeof source?.getAudioTransceiver !== "function") return false;
  let sender: RTCRtpSender | undefined;
  try {
    sender = source.getAudioTransceiver()?.sender;
  } catch {
    return false; // 还没有 PeerConnection
  }
  if (!sender || sender.track === track) return false;
  await sender.replaceTrack(track);
  return true;
}
