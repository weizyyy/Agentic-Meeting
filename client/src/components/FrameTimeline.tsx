import { ChevronLeftIcon, ChevronRightIcon, MonitorOffIcon, MonitorUpIcon } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { formatClock } from "../captions.ts";
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

/** 屏幕时间线：截图缩略图（点击放大）和画面摘要；右上角是共享屏幕的开关。 */
export function FrameTimeline({ frames, timeBase, sharing, canShare, onStart, onStop }: Props) {
  // 钟点在前（没有时间基准时退回会议时间），会议进行到哪跟在后面
  const wall = (t: number) => wallClockLabel(t, timeBase) || formatFrameTime(t);
  const [openId, setOpenId] = useState<number | null>(null);
  const strip = useRef<HTMLUListElement>(null);
  const openIndex = openId === null ? -1 : frames.findIndex((f) => f.id === openId);
  const open = openIndex < 0 ? null : frames[openIndex];

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

  const step = (delta: -1 | 1) => {
    if (openId === null) return;
    const next = frames[adjacentFrameIndex(frames, openId, delta)];
    if (next) setOpenId(next.id);
  };

  // 大图打开时左右方向键换上一张 / 下一张（到头就停）；Esc 由对话框自己关
  useEffect(() => {
    if (openId === null) return;
    const onKey = (event: KeyboardEvent) => {
      const delta = event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : 0;
      if (delta === 0) return;
      const next = frames[adjacentFrameIndex(frames, openId, delta)];
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
    <section className="flex flex-col rounded-xl border bg-card" aria-label="屏幕">
      <div className="flex items-center gap-2 px-4 pt-3 pb-2">
        <h2 className="text-sm font-medium">屏幕</h2>
        {sharing && (
          <span className="inline-flex items-center gap-1.5 text-xs font-medium text-success-foreground">
            <span aria-hidden className="size-1.5 rounded-full bg-current" />
            共享中
          </span>
        )}
        {sharing ? (
          <Button
            variant="outline"
            size="sm"
            className="ml-auto border-transparent bg-success text-success-foreground hover:bg-success/80 hover:text-success-foreground"
            onClick={onStop}
          >
            <MonitorOffIcon data-icon="inline-start" />
            停止共享
          </Button>
        ) : (
          <Button
            variant="outline"
            size="sm"
            className="ml-auto"
            disabled={!canShare}
            onClick={onStart}
          >
            <MonitorUpIcon data-icon="inline-start" />
            共享屏幕
          </Button>
        )}
      </div>
      {frames.length === 0 ? (
        <p className="px-4 pb-4 text-sm text-subtle-foreground">
          {canShare || sharing
            ? "共享屏幕后，画面有变化时会自动截图，并出现在这里。"
            : "会议进行中可以共享屏幕，截图会出现在这里。"}
        </p>
      ) : (
        <>
          <ul className="flex gap-3 overflow-x-auto px-4 pt-1 pb-3" ref={strip}>
            {frames.map((frame) => (
              <li key={frame.id} className="shrink-0">
                <button
                  type="button"
                  className="group flex flex-col gap-1 rounded-lg text-left outline-none"
                  title={[`会议进行到 ${formatFrameTime(frame.t)}`, frame.caption]
                    .filter(Boolean)
                    .join(" ")}
                  onClick={() => setOpenId(frame.id)}
                >
                  <img
                    className={`block aspect-video w-32 rounded-lg border bg-muted object-cover transition-colors group-hover:border-ring group-focus-visible:ring-3 group-focus-visible:ring-ring/50 ${
                      frame === latest ? "border-client-foreground" : ""
                    }`}
                    src={frameImageUrl(frame.id)}
                    alt={frame.caption ?? `${formatFrameTime(frame.t)} 的屏幕截图`}
                    loading="lazy"
                    width={frame.width}
                    height={frame.height}
                  />
                  <time className="font-mono text-xs text-muted-foreground tabular-nums">
                    {wall(frame.t)}
                  </time>
                </button>
              </li>
            ))}
          </ul>
          {latest?.caption && (
            <p className="line-clamp-2 px-4 pb-3 text-sm text-muted-foreground">
              <time className="mr-1.5 font-mono text-xs tabular-nums">{wall(latest.t)}</time>
              <time className="mr-1.5 font-mono text-xs text-subtle-foreground tabular-nums">
                +{formatClock(latest.t)}
              </time>
              {latest.caption}
            </p>
          )}
        </>
      )}
      <Dialog
        open={open !== null}
        onOpenChange={(next) => {
          if (!next) setOpenId(null);
        }}
      >
        {open && (
          <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-5xl">
            <DialogHeader>
              <DialogTitle className="flex items-baseline gap-2 pr-8">
                屏幕截图
                <time className="font-mono text-sm font-normal tabular-nums">{wall(open.t)}</time>
                <time className="font-mono text-xs font-normal text-subtle-foreground tabular-nums">
                  +{formatClock(open.t)}
                </time>
              </DialogTitle>
              <DialogDescription>{open.caption ?? "（还没有画面摘要）"}</DialogDescription>
            </DialogHeader>
            <img
              className="max-h-[70dvh] w-full rounded-lg border bg-muted object-contain"
              src={frameImageUrl(open.id)}
              alt={open.caption ?? "屏幕截图"}
            />
            <DialogFooter className="flex-row items-center justify-between sm:justify-between">
              <Button variant="outline" disabled={openIndex <= 0} onClick={() => step(-1)}>
                <ChevronLeftIcon data-icon="inline-start" />
                上一张
              </Button>
              <span className="text-center text-xs text-muted-foreground tabular-nums">
                {openIndex + 1} / {frames.length} · ← → 切换
              </span>
              <Button
                variant="outline"
                disabled={openIndex >= frames.length - 1}
                onClick={() => step(1)}
              >
                下一张
                <ChevronRightIcon data-icon="inline-end" />
              </Button>
            </DialogFooter>
          </DialogContent>
        )}
      </Dialog>
    </section>
  );
}
