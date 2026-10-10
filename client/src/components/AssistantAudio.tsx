import { usePipecatClientMediaTrack } from "@pipecat-ai/client-react";
import { useCallback, useEffect, useRef, useState } from "react";

/**
 * 播放助理的声音（代替 Pipecat 自带的 PipecatClientAudio）。
 * 浏览器的自动播放限制可能拦下播放（多见于 Safari、Firefox，或点「开始」之后隔了一会儿声音才到）：
 * 这时不再悄悄没声音，而是显示一个按钮，用户点一下就在这次点击里重新播放。
 */
export function AssistantAudio() {
  const audio = useRef<HTMLAudioElement>(null);
  const track = usePipecatClientMediaTrack("audio", "bot");
  const [blocked, setBlocked] = useState(false);

  const play = useCallback(async () => {
    const element = audio.current;
    if (!element?.srcObject) return;
    try {
      await element.play();
      setBlocked(false);
    } catch (error) {
      // 只有自动播放限制算「被拦」；换轨道打断上一次 play() 的 AbortError 不用管
      if (error instanceof DOMException && error.name === "NotAllowedError") setBlocked(true);
      else console.warn("播放助理声音失败：", error);
    }
  }, []);

  useEffect(() => {
    const element = audio.current;
    if (!element || !track) return;
    const current = element.srcObject instanceof MediaStream ? element.srcObject : null;
    if (current?.getAudioTracks()[0]?.id === track.id) return;
    element.srcObject = new MediaStream([track]);
    void play();
  }, [track, play]);

  return (
    <>
      <audio ref={audio} />
      {blocked && (
        <p className="audio-blocked" role="alert">
          浏览器拦下了助理的声音。
          <button type="button" className="button button-small" onClick={() => void play()}>
            打开助理声音
          </button>
        </p>
      )}
    </>
  );
}
