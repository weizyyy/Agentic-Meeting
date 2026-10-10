/** 仅替代设备采集：原生轨道仍经过 SDK、RTCPeerConnection 和截图上传。 */
export function installMedia({ nativeAudio = false } = {}) {
  const tracks: MediaStreamTrack[] = [];
  const contexts: AudioContext[] = [];
  let color = "#345678";
  const media = {
    tracks,
    contexts,
    changeScreen: () => {
      color = "#f1d234";
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
