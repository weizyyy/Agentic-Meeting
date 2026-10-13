import type { PipecatClient } from "@pipecat-ai/client-js";
import { useEffect, useState, type Dispatch } from "react";

import { formatSpan, messages } from "./i18n/index.ts";
import type { Action } from "./meetingState.ts";

/**
 * 手机上尽量不让收音断掉（docs/user-guide.md「手机上」）：录音期间请求保持亮屏，
 * 收音被系统停掉过（锁屏、切到后台、来电）就在恢复时说一声断了多久。
 *
 * 浏览器停掉麦克风时会把轨道标成 muted；页面在后台时脚本可能被冻结，事件要回到前台才收到，
 * 所以刚回前台就收到的 mute 从切到后台那一刻算起。
 */

/** 收音停了多久以上才提示：一两秒的切换不打扰。 */
const GAP_NOTICE_SECS = 3;
/** 回到前台多久之内收到的 mute 算作后台期间发生的。 */
const RETURN_GRACE_MS = 2000;
/** 多久看一次麦克风轨道换没换（换麦克风、系统默认设备变化时是一条新轨道）。 */
const TRACK_CHECK_MS = 1000;

/** 保持亮屏：浏览器没有这个接口、正保持着、没拿到（省电模式、页面不在前台等）。 */
export type KeepAwake = "unsupported" | "held" | "refused";

const WAKE_LOCK_SUPPORTED = "wakeLock" in navigator;

interface Options {
  client: PipecatClient;
  /** 这台设备正在录音（连着会议） */
  recording: boolean;
  dispatch: Dispatch<Action>;
}

export function useKeepRecording({ client, recording, dispatch }: Options): KeepAwake {
  const [keepAwake, setKeepAwake] = useState<KeepAwake>(
    WAKE_LOCK_SUPPORTED ? "held" : "unsupported",
  );

  // 保持亮屏。页面切到后台时浏览器会自己释放，回到前台再要一次。
  useEffect(() => {
    if (!recording || !WAKE_LOCK_SUPPORTED) return;
    let lock: WakeLockSentinel | null = null;
    let done = false;
    const acquire = async () => {
      if (document.visibilityState !== "visible" || (lock && !lock.released)) return;
      try {
        const next = await navigator.wakeLock.request("screen");
        if (done) {
          void next.release().catch(() => undefined);
          return;
        }
        lock = next;
        setKeepAwake("held");
        next.addEventListener("release", () => {
          // 前台时被收回（比如进了省电模式）才算没保持住；切后台的释放回来会再要
          if (!done && document.visibilityState === "visible") setKeepAwake("refused");
        });
      } catch (error) {
        console.info("没能保持亮屏：", error);
        if (!done) setKeepAwake("refused");
      }
    };
    void acquire();
    const onVisibility = () => void acquire();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      done = true;
      document.removeEventListener("visibilitychange", onVisibility);
      void lock?.release().catch(() => undefined);
    };
  }, [recording]);

  // 收音中断：轨道 muted 到恢复之间超过几秒就提示。
  useEffect(() => {
    if (!recording) return;
    let track: MediaStreamTrack | undefined;
    let hiddenAt: number | null = document.visibilityState === "hidden" ? Date.now() : null;
    let visibleAt = 0;
    let mutedSince: number | null = null;

    const onMute = () => {
      if (mutedSince !== null) return;
      const now = Date.now();
      const justReturned = hiddenAt !== null && now - visibleAt < RETURN_GRACE_MS;
      mutedSince = document.visibilityState === "hidden" || justReturned ? (hiddenAt ?? now) : now;
    };
    const onUnmute = () => {
      if (mutedSince === null) return;
      const secs = (Date.now() - mutedSince) / 1000;
      mutedSince = null;
      if (secs < GAP_NOTICE_SECS) return;
      const t = messages();
      dispatch({
        type: "notice",
        level: "warn",
        text: t.notices.micInterrupted(formatSpan(t, secs)),
      });
    };
    const onVisibility = () => {
      if (document.visibilityState === "hidden") hiddenAt = Date.now();
      else visibleAt = Date.now();
    };
    const follow = () => {
      let current: MediaStreamTrack | undefined;
      try {
        current = client.tracks().local.audio;
      } catch {
        return; // 传输层还没准备好
      }
      if (current === track) return;
      track?.removeEventListener("mute", onMute);
      track?.removeEventListener("unmute", onUnmute);
      track = current;
      // 换了一条正常的新轨道，说明收音已经恢复
      if (track && !track.muted) onUnmute();
      track?.addEventListener("mute", onMute);
      track?.addEventListener("unmute", onUnmute);
      if (track?.muted) onMute();
    };
    follow();
    const timer = window.setInterval(follow, TRACK_CHECK_MS);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
      track?.removeEventListener("mute", onMute);
      track?.removeEventListener("unmute", onUnmute);
    };
  }, [client, recording, dispatch]);

  return keepAwake;
}
