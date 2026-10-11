/** 仅替代设备采集：原生轨道仍经过 SDK、RTCPeerConnection 和截图上传。 */
export function installMedia({ nativeAudio = false } = {}) {
  const tracks: MediaStreamTrack[] = [];
  const contexts: AudioContext[] = [];
  let color = "#345678";
  const media = {
    tracks,
    contexts,
    /** 下一次选择窗口的结果：选中画面、用户点取消，或浏览器读不到所选画面。 */
    screenMode: "share" as "share" | "cancel" | "fail",
    /** 每次 getDisplayMedia 调用时页面是否处于用户手势中（Safari 只在手势里允许共享）。 */
    displayCalls: [] as (boolean | null)[],
    changeScreen: () => {
      color = "#f1d234";
    },
    /** 模拟用户点浏览器自带的「停止共享」：只触发 ended，轨道由页面自己停止；返回派发前各画面轨道的状态。 */
    endScreen: () => {
      const video = tracks.filter((track) => track.kind === "video");
      const states = video.map((track) => track.readyState);
      for (const track of video)
        if (track.readyState === "live") track.dispatchEvent(new Event("ended"));
      return states;
    },
  };
  Object.assign(window, { e2eMedia: media });
  const getUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  navigator.mediaDevices.getUserMedia = async (constraints) => {
    if (nativeAudio) {
      const stream = await getUserMedia(constraints);
      tracks.push(...stream.getTracks());
      return stream;
    }
    const context = new AudioContext();
    contexts.push(context);
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    const destination = context.createMediaStreamDestination();
    gain.gain.value = 0.02;
    oscillator.frequency.value = 440;
    oscillator.connect(gain).connect(destination);
    oscillator.start();
    await context.resume();
    const track = destination.stream.getAudioTracks()[0];
    tracks.push(track);
    const stop = track.stop.bind(track);
    track.stop = () => {
      stop();
      oscillator.stop();
      void context.close();
    };
    return destination.stream;
  };
  if (!nativeAudio) navigator.mediaDevices.enumerateDevices = async () => [];
  navigator.mediaDevices.getDisplayMedia = async () => {
    media.displayCalls.push(navigator.userActivation?.isActive ?? null);
    if (media.screenMode === "cancel")
      throw new DOMException("Permission denied", "NotAllowedError");
    if (media.screenMode === "fail") throw new DOMException("无法读取所选窗口", "NotReadableError");
    const canvas = document.createElement("canvas");
    canvas.width = 640;
    canvas.height = 360;
    const context = canvas.getContext("2d")!;
    const paint = () => {
      context.fillStyle = color;
      context.fillRect(0, 0, 640, 360);
    };
    paint();
    const stream = canvas.captureStream(10);
    const timer = setInterval(paint, 100);
    for (const track of stream.getTracks()) {
      tracks.push(track);
      const stop = track.stop.bind(track);
      track.stop = () => {
        clearInterval(timer);
        stop();
      };
    }
    return stream;
  };
}
