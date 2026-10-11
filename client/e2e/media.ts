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
    /** 页面在屏幕轨道上注册的 ended 监听。 */
    screenEnded: [] as { track: MediaStreamTrack; listener: EventListenerOrEventListenerObject }[],
    /**
     * 模拟用户点浏览器自带的「停止共享」：把 ended 交给页面注册的监听，轨道由页面自己停止。
     * Firefox 不把脚本派发的 ended 送到轨道的监听上，所以直接调用监听。返回调用了几个监听。
     */
    endScreen: () => {
      const pending = media.screenEnded.splice(0);
      for (const { track, listener } of pending) {
        const event = new Event("ended");
        if (typeof listener === "function") listener.call(track, event);
        else listener.handleEvent(event);
      }
      return pending.length;
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
      const add = track.addEventListener.bind(track);
      track.addEventListener = (
        type: string,
        listener: EventListenerOrEventListenerObject | null,
        options?: boolean | AddEventListenerOptions,
      ) => {
        if (type === "ended" && listener) media.screenEnded.push({ track, listener });
        add(type, listener, options);
      };
      const stop = track.stop.bind(track);
      track.stop = () => {
        clearInterval(timer);
        stop();
      };
    }
    return stream;
  };
}
