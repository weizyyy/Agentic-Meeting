import { useEffect, useState } from "react";

import { holdPeak, rmsFromTimeDomain } from "./micLevel.ts";

const FRAME_INTERVAL_MS = 50; // 电平条约 20 帧/秒就够了

/**
 * 读取本机麦克风轨道的实时电平（RMS，0–1，带缓慢回落）。
 * 只做显示用的分析，不把声音接到扬声器上。没有轨道时返回 0。
 */
export function useMicLevel(track: MediaStreamTrack | null): number {
  const [level, setLevel] = useState(0);

  useEffect(() => {
    if (!track) {
      setLevel(0);
      return;
    }
    let context: AudioContext | undefined;
    let timer: number | undefined;
    try {
      context = new AudioContext();
      void context.resume().catch(() => undefined);
      const analyser = context.createAnalyser();
      analyser.fftSize = 1024;
      context.createMediaStreamSource(new MediaStream([track])).connect(analyser);
      const data = new Uint8Array(analyser.fftSize);
      let held = 0;
      timer = window.setInterval(() => {
        analyser.getByteTimeDomainData(data);
        held = holdPeak(held, rmsFromTimeDomain(data));
        setLevel(held);
      }, FRAME_INTERVAL_MS);
    } catch (error) {
      console.warn("无法读取麦克风电平：", error);
    }
    return () => {
      if (timer !== undefined) window.clearInterval(timer);
      void context?.close().catch(() => undefined);
      setLevel(0);
    };
  }, [track]);

  return level;
}
