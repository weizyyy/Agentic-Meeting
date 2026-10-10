import { useCallback, useEffect, useRef, type Dispatch } from "react";

import { ApiError, type Api } from "./api.ts";
import type { Action } from "./meetingState.ts";
import {
  THUMB_HEIGHT,
  THUMB_WIDTH,
  clockOffset,
  frameDifference,
  scaledSize,
  toGray,
  uploadDecision,
  type ClockSample,
  type ScreenConfig,
} from "./screenCapture.ts";

/** 对时测几次（取往返最短的一次，docs/interfaces.md §5.1）。 */
const CLOCK_SAMPLES = 3;
/** 多久看一眼画面。要不要上传由 uploadDecision 按配置的间隔决定，这里只是检查的节拍。 */
const MAX_TICK_MS = 1000;
const MIN_TICK_MS = 250;
const WEBP_QUALITY = 0.8;
const JPEG_QUALITY = 0.85;

interface Capture {
  stream: MediaStream;
  video: HTMLVideoElement;
  timer: number;
}

interface Options {
  api: Api;
  /** 已连接：只有会议进行中才采集和上传 */
  connected: boolean;
  config: ScreenConfig;
  dispatch: Dispatch<Action>;
  /** 告诉服务端共享开始 / 停止（screen_state 消息，仅用于界面状态与日志） */
  sendState: (sharing: boolean) => void;
}

export interface ScreenShare {
  start: () => Promise<void>;
  stop: () => void;
}

function toBlob(canvas: HTMLCanvasElement, type: string, quality: number): Promise<Blob | null> {
  return new Promise((resolve) => canvas.toBlob(resolve, type, quality));
}

/** 编码成 WebP；浏览器不会编码 WebP 时（会悄悄给出 PNG）退回 JPEG——服务端只收这两种。 */
async function encode(canvas: HTMLCanvasElement): Promise<Blob> {
  const webp = await toBlob(canvas, "image/webp", WEBP_QUALITY);
  if (webp && webp.type === "image/webp") return webp;
  const jpeg = await toBlob(canvas, "image/jpeg", JPEG_QUALITY);
  if (jpeg && jpeg.type === "image/jpeg") return jpeg;
  throw new Error("浏览器无法把画面编码成图片");
}

/**
 * 屏幕共享与截图上传。
 *
 * 不走 WebRTC 视频轨：把共享的画面画到离屏画布上，缩成 64×36 的灰度图与上一张**已上传**的比较，
 * 变化超过阈值或满兜底间隔时，把原画面等比缩小后编码上传。采集时刻换算成服务端时钟。
 */
export function useScreenShare({
  api,
  connected,
  config,
  dispatch,
  sendState,
}: Options): ScreenShare {
  const capture = useRef<Capture | null>(null);
  const offset = useRef(0);
  const latestConfig = useRef(config);
  latestConfig.current = config;
  const latestSend = useRef(sendState);
  latestSend.current = sendState;

  // 连接建立时对时：之后每张截图的采集时刻都按这个偏移量换算成服务端时钟。
  useEffect(() => {
    if (!connected) return;
    let cancelled = false;
    void (async () => {
      const samples: ClockSample[] = [];
      for (let i = 0; i < CLOCK_SAMPLES; i += 1) {
        try {
          const t0 = Date.now() / 1000;
          const serverTime = await api.serverTime();
          samples.push({ t0, t1: Date.now() / 1000, serverTime });
        } catch {
          // 这一次没测成：用其余几次的
        }
      }
      if (!cancelled && samples.length > 0) offset.current = clockOffset(samples);
    })();
    return () => {
      cancelled = true;
    };
  }, [connected, api]);

  const stop = useCallback(() => {
    const current = capture.current;
    if (!current) return;
    capture.current = null;
    window.clearInterval(current.timer);
    for (const track of current.stream.getTracks()) track.stop();
    current.video.srcObject = null;
    dispatch({ type: "sharing", sharing: false });
    latestSend.current(false);
  }, [dispatch]);

  const start = useCallback(async () => {
    if (capture.current) return;
    if (!navigator.mediaDevices?.getDisplayMedia) {
      dispatch({
        type: "notice",
        level: "error",
        text: "这个浏览器不能共享屏幕（需要桌面浏览器，并通过 HTTPS 或 localhost 访问）",
      });
      return;
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getDisplayMedia({
        video: { frameRate: 2 },
        audio: false,
      });
    } catch (error) {
      // 用户在选择窗口里点了取消：不算出错
      if (!(error instanceof DOMException && error.name === "NotAllowedError")) {
        const text = error instanceof Error ? error.message : "未知错误";
        dispatch({ type: "notice", level: "error", text: `无法共享屏幕：${text}` });
      }
      return;
    }

    const video = document.createElement("video");
    video.muted = true;
    video.playsInline = true;
    video.srcObject = stream;
    void video.play().catch(() => undefined);

    const thumbCanvas = document.createElement("canvas");
    thumbCanvas.width = THUMB_WIDTH;
    thumbCanvas.height = THUMB_HEIGHT;
    const thumbContext = thumbCanvas.getContext("2d", { willReadFrequently: true });
    const fullCanvas = document.createElement("canvas");
    const fullContext = fullCanvas.getContext("2d");

    let lastThumb: Uint8Array | null = null;
    let lastUploadSecs: number | null = null;
    let busy = false;
    let failures = 0;

    const tick = async () => {
      if (busy || !thumbContext || !fullContext) return;
      if (video.videoWidth === 0 || video.videoHeight === 0) return; // 画面还没来
      const cfg = latestConfig.current;
      thumbContext.drawImage(video, 0, 0, THUMB_WIDTH, THUMB_HEIGHT);
      const gray = toGray(thumbContext.getImageData(0, 0, THUMB_WIDTH, THUMB_HEIGHT).data);
      const now = Date.now() / 1000;
      const difference = lastThumb ? frameDifference(lastThumb, gray) : 1;
      if (uploadDecision(now, lastUploadSecs, difference, cfg) === null) return;

      busy = true;
      try {
        const size = scaledSize(video.videoWidth, video.videoHeight, cfg.maxSidePx);
        fullCanvas.width = size.width;
        fullCanvas.height = size.height;
        fullContext.drawImage(video, 0, 0, size.width, size.height);
        const blob = await encode(fullCanvas);
        await api.uploadFrame(blob, now + offset.current);
        lastThumb = gray;
        lastUploadSecs = now;
        failures = 0;
      } catch (error) {
        failures += 1;
        // 连续失败只提示第一次，之后每个节拍照常重试
        if (failures === 1) {
          const text =
            error instanceof ApiError || error instanceof Error ? error.message : "未知错误";
          dispatch({ type: "notice", level: "warn", text: `截图上传失败：${text}` });
        }
      } finally {
        busy = false;
      }
    };

    const period = Math.min(
      MAX_TICK_MS,
      Math.max(MIN_TICK_MS, latestConfig.current.minIntervalSecs * 500),
    );
    const timer = window.setInterval(() => void tick(), period);
    capture.current = { stream, video, timer };
    video.addEventListener("loadeddata", () => void tick(), { once: true });
    // 用户在浏览器自带的「停止共享」上点了停止
    for (const track of stream.getVideoTracks())
      track.addEventListener("ended", stop, { once: true });

    dispatch({ type: "sharing", sharing: true });
    latestSend.current(true);
  }, [api, dispatch, stop]);

  // 连接断开（会议结束或中断）就停止共享：没有进行中的会议，截图没处可放。
  useEffect(() => {
    if (!connected) stop();
  }, [connected, stop]);

  useEffect(() => stop, [stop]);

  return { start, stop };
}
