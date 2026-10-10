import { useEffect, useLayoutEffect, useRef, useState } from "react";

import {
  adjacentFrameIndex,
  formatFrameTime,
  frameImageUrl,
  type FrameItem,
} from "../screenCapture.ts";
import { wallClockLabel, type TimeBase } from "../timeline.ts";

interface Props {
  frames: readonly FrameItem[];
  /** 这场会议的时间基准：算每张截图当时墙上的钟点 */
  timeBase: TimeBase | null;
  /** 正在共享屏幕 */
  sharing: boolean;
  /** 可以开始共享（已连接、配置里没关） */
  canShare: boolean;
  onStart: () => void;
  onStop: () => void;
}

/** 屏幕时间线：截图缩略图（点击放大）和画面摘要；顶部是共享屏幕的开关。 */
export function FrameTimeline({ frames, timeBase, sharing, canShare, onStart, onStop }: Props) {
  // 钟点在前（没有时间基准时退回会议时间），会议进行到哪跟在后面
  const wall = (t: number) => wallClockLabel(t, timeBase) || formatFrameTime(t);
  const [openId, setOpenId] = useState<number | null>(null);
  const strip = useRef<HTMLUListElement>(null);
  const open = openId === null ? null : (frames.find((f) => f.id === openId) ?? null);

  // 有新截图就滚到最新的一张
  useLayoutEffect(() => {
    const el = strip.current;
    if (el) el.scrollLeft = el.scrollWidth;
  }, [frames.length]);

  // 缩略图是横着排的：鼠标滚轮上下滚，换成左右滚（滚动条照常能用）
  const hasFrames = frames.length > 0;
  useEffect(() => {
    const el = strip.current;
    if (!el) return;
    const onWheel = (event: WheelEvent) => {
      if (event.deltaY === 0 || event.ctrlKey || el.scrollWidth <= el.clientWidth) return;
      event.preventDefault();
      // 按行滚的鼠标给的是行数，不是像素
      el.scrollLeft +=
        event.deltaMode === WheelEvent.DOM_DELTA_PIXEL ? event.deltaY : event.deltaY * 40;
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [hasFrames]);

  // 大图打开时：Esc 关闭，左右方向键换上一张 / 下一张（到头就停）
  useEffect(() => {
    if (openId === null) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpenId(null);
      const step = event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : 0;
      if (step === 0) return;
      const next = frames[adjacentFrameIndex(frames, openId, step)];
      if (next) {
        event.preventDefault();
        setOpenId(next.id);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openId, frames]);

  const latest = frames.length > 0 ? frames[frames.length - 1] : null;

  return (
    <section className="panel frames-panel" aria-label="屏幕">
      <h2>
        屏幕
        {sharing && <span className="badge badge-speaking">共享中</span>}
        <span className="spacer" />
        {sharing ? (
          <button type="button" className="link" onClick={onStop}>
            停止共享
          </button>
        ) : (
          <button type="button" className="link" disabled={!canShare} onClick={onStart}>
            共享屏幕
          </button>
        )}
      </h2>
      {frames.length === 0 ? (
        <p className="empty">
          {canShare || sharing
            ? "共享屏幕后，画面有变化时会自动截图，并出现在这里。"
            : "会议进行中可以共享屏幕，截图会出现在这里。"}
        </p>
      ) : (
        <>
          <ul className="frames" ref={strip}>
            {frames.map((frame) => (
              <li key={frame.id}>
                <button
                  type="button"
                  className="frame-thumb"
                  title={[`会议进行到 ${formatFrameTime(frame.t)}`, frame.caption]
                    .filter(Boolean)
                    .join(" ")}
                  onClick={() => setOpenId(frame.id)}
                >
                  <img
                    src={frameImageUrl(frame.id)}
                    alt={frame.caption ?? `${formatFrameTime(frame.t)} 的屏幕截图`}
                    loading="lazy"
                    width={frame.width}
                    height={frame.height}
                  />
                  <time>{wall(frame.t)}</time>
                </button>
              </li>
            ))}
          </ul>
          {latest?.caption && (
            <p className="frame-caption">
              <time>{wall(latest.t)}</time>{" "}
              <time className="elapsed">{formatFrameTime(latest.t)}</time> {latest.caption}
            </p>
          )}
        </>
      )}
      {open && (
        <div
          className="lightbox"
          role="dialog"
          aria-modal="true"
          aria-label="屏幕截图"
          onClick={() => setOpenId(null)}
        >
          <figure onClick={(event) => event.stopPropagation()}>
            <img src={frameImageUrl(open.id)} alt={open.caption ?? "屏幕截图"} />
            <figcaption>
              <time>{wall(open.t)}</time>
              <time className="elapsed">{formatFrameTime(open.t)}</time>
              <span>{open.caption ?? "（还没有画面摘要）"}</span>
              <span className="lightbox-hint">
                {frames.findIndex((f) => f.id === open.id) + 1} / {frames.length} · ← → 切换
              </span>
              <button type="button" className="link" onClick={() => setOpenId(null)}>
                关闭
              </button>
            </figcaption>
          </figure>
        </div>
      )}
    </section>
  );
}
