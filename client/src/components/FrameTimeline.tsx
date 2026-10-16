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
import { useI18n } from "../i18n/index.ts";
import {
  adjacentFrameIndex,
  formatFrameTime,
  frameImageUrl,
  type FrameItem,
} from "../screenCapture.ts";
import { wallClockLabel, type TimeBase } from "../timeline.ts";
import { isShown, pageArrow } from "../useArrowKeys.ts";

interface Props {
  frames: readonly FrameItem[];
  /** 这场会议的时间基准：算每张截图当时墙上的钟点 */
  timeBase: TimeBase | null;
  /** 正在共享屏幕 */
  sharing: boolean;
  /** 可以开始共享（已连接、配置里没关） */
  canShare: boolean;
  /** 这个浏览器能共享屏幕（手机浏览器都不能）；不能时不放共享按钮 */
  shareSupported: boolean;
  onStart: () => void;
  onStop: () => void;
}

/** 屏幕时间线：截图缩略图（点击放大）和画面摘要；右上角是共享屏幕的开关。 */
export function FrameTimeline({
  frames,
  timeBase,
  sharing,
  canShare,
  shareSupported,
  onStart,
  onStop,
}: Props) {
  const { t, language } = useI18n();
  // 钟点在前（没有时间基准时退回会议时间），会议进行到哪跟在后面
  const wall = (secs: number) => wallClockLabel(language, secs, timeBase) || formatFrameTime(secs);
  const [openId, setOpenId] = useState<number | null>(null);
  const strip = useRef<HTMLUListElement>(null);
  const openIndex = openId === null ? -1 : frames.findIndex((f) => f.id === openId);
  const open = openIndex < 0 ? null : frames[openIndex];
  // 当前这张：标题旁边显示它的摘要、缩略图加框。没挑过（或挑回了最后一张）就跟着最新的走
  const [pickedId, setPickedId] = useState<number | null>(null);
  const pickedIndex = pickedId === null ? -1 : frames.findIndex((f) => f.id === pickedId);
  const currentIndex = pickedIndex >= 0 ? pickedIndex : frames.length - 1;
  const current = currentIndex < 0 ? null : frames[currentIndex];
  const following = pickedIndex < 0;

  /** 挑中第几张；挑到最后一张就回到跟着最新的走 */
  const pick = (index: number) => {
    const frame = frames[index];
    if (frame) setPickedId(index === frames.length - 1 ? null : frame.id);
  };

  // 跟着最新的走时，有新截图就滚到最后；挑了某一张时让它留在看得见的地方
  useLayoutEffect(() => {
    const el = strip.current;
    if (!el) return;
    if (following) {
      el.scrollLeft = el.scrollWidth;
      return;
    }
    const item = el.children[currentIndex];
    if (!item) return;
    const box = el.getBoundingClientRect();
    const { left, right } = item.getBoundingClientRect();
    const pad = 16;
    if (left < box.left + pad) el.scrollLeft -= box.left + pad - left;
    else if (right > box.right - pad) el.scrollLeft += right - (box.right - pad);
  }, [frames.length, following, currentIndex]);

  // 焦点不在输入框、菜单这些控件上时，左右方向键换上一张 / 下一张，不去滚缩略图
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const arrow = pageArrow(event);
      if ((arrow !== "left" && arrow !== "right") || !isShown(strip.current)) return;
      event.preventDefault();
      const next = currentIndex + (arrow === "left" ? -1 : 1);
      if (next >= 0 && next < frames.length) pick(next);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

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

  /** 换到上一张 / 下一张；到头了返回 false */
  const step = (delta: -1 | 1): boolean => {
    if (openId === null) return false;
    const next = frames[adjacentFrameIndex(frames, openId, delta)];
    if (!next) return false;
    setOpenId(next.id);
    return true;
  };

  return (
    <section className="flex flex-col rounded-xl border bg-card" aria-label={t.screen.title}>
      {/* 标题、当前这张的摘要和共享按钮挤在一行，把高度留给字幕；手机上摘要另起一行 */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 px-4 py-2">
        <h2 className="text-sm font-medium">{t.screen.title}</h2>
        {sharing && (
          <span className="inline-flex items-center gap-1.5 text-xs font-medium text-success-foreground">
            <span aria-hidden className="size-1.5 rounded-full bg-current" />
            {t.screen.sharing}
          </span>
        )}
        {current && (current.caption || !following) && (
          <p
            className="order-last basis-full text-sm text-muted-foreground max-md:line-clamp-2 md:order-none md:min-w-0 md:flex-1 md:basis-0 md:truncate"
            title={current.caption ?? undefined}
          >
            <time className="mr-1.5 font-mono text-xs tabular-nums">{wall(current.t)}</time>
            <time className="mr-1.5 font-mono text-xs text-subtle-foreground tabular-nums">
              +{formatClock(current.t)}
            </time>
            {current.caption ?? t.screen.noCaption}
          </p>
        )}
        {sharing ? (
          <Button
            variant="outline"
            size="sm"
            className="ml-auto border-transparent bg-success text-success-foreground hover:bg-success/80 hover:text-success-foreground"
            onClick={onStop}
          >
            <MonitorOffIcon data-icon="inline-start" />
            {t.screen.stop}
          </Button>
        ) : (
          shareSupported && (
            <Button
              variant="outline"
              size="sm"
              className="ml-auto"
              disabled={!canShare}
              onClick={onStart}
            >
              <MonitorUpIcon data-icon="inline-start" />
              {t.screen.share}
            </Button>
          )
        )}
      </div>
      {frames.length === 0 ? (
        <p className="px-4 pb-3 text-sm text-subtle-foreground">
          {!shareSupported
            ? t.screen.emptyNoShare
            : canShare || sharing
              ? t.screen.emptySharing
              : t.screen.emptyIdle}
        </p>
      ) : (
        <ul className="flex gap-2 overflow-x-auto px-4 pb-3" ref={strip}>
          {frames.map((frame) => (
            <li key={frame.id} className="shrink-0">
              <button
                type="button"
                className="group relative block rounded-lg text-left outline-none"
                aria-current={frame === current ? "true" : undefined}
                title={[t.screen.meetingTime(formatFrameTime(frame.t)), frame.caption]
                  .filter(Boolean)
                  .join(" ")}
                onClick={() => setOpenId(frame.id)}
              >
                <img
                  className={`block aspect-video w-28 rounded-lg max-md:w-32 border bg-muted object-cover transition-colors group-hover:border-ring group-focus-visible:ring-3 group-focus-visible:ring-ring/50 ${
                    frame === current ? "border-client-foreground" : ""
                  }`}
                  src={frameImageUrl(frame.id)}
                  alt={frame.caption ?? t.screen.frameAlt(formatFrameTime(frame.t))}
                  loading="lazy"
                  width={frame.width}
                  height={frame.height}
                />
                {/* 钟点压在缩略图左下角，不再单占一行 */}
                <time className="absolute bottom-1 left-1 rounded bg-black/65 px-1 font-mono text-[0.7rem] leading-4 text-white tabular-nums">
                  {wall(frame.t)}
                </time>
              </button>
            </li>
          ))}
        </ul>
      )}
      <Dialog
        open={open !== null}
        onOpenChange={(next) => {
          if (next) return;
          // 关掉时停在最后看的那张，方向键接着从这里换
          if (openIndex >= 0) pick(openIndex);
          setOpenId(null);
        }}
      >
        {open && (
          <DialogContent
            className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-5xl"
            onKeyDown={(event) => {
              // 左右方向键换上一张 / 下一张（到头就停）；Esc 由对话框自己关。
              // 对话框会拦下方向键、不让它冒泡到 window，所以只能在对话框本身上接。
              const delta = event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : 0;
              if (delta !== 0 && step(delta)) event.preventDefault();
            }}
          >
            <DialogHeader>
              <DialogTitle className="flex items-baseline gap-2 pr-8">
                {t.screen.screenshot}
                <time className="font-mono text-sm font-normal tabular-nums">{wall(open.t)}</time>
                <time className="font-mono text-xs font-normal text-subtle-foreground tabular-nums">
                  +{formatClock(open.t)}
                </time>
              </DialogTitle>
              <DialogDescription>{open.caption ?? t.screen.noCaption}</DialogDescription>
            </DialogHeader>
            <img
              className="max-h-[70dvh] w-full rounded-lg border bg-muted object-contain"
              src={frameImageUrl(open.id)}
              alt={open.caption ?? t.screen.screenshot}
            />
            <DialogFooter className="flex-row items-center justify-between sm:justify-between">
              <Button variant="outline" disabled={openIndex <= 0} onClick={() => step(-1)}>
                <ChevronLeftIcon data-icon="inline-start" />
                {t.screen.previous}
              </Button>
              <span className="text-center text-xs text-muted-foreground tabular-nums">
                {t.screen.position(openIndex + 1, frames.length)}
              </span>
              <Button
                variant="outline"
                disabled={openIndex >= frames.length - 1}
                onClick={() => step(1)}
              >
                {t.screen.next}
                <ChevronRightIcon data-icon="inline-end" />
              </Button>
            </DialogFooter>
          </DialogContent>
        )}
      </Dialog>
    </section>
  );
}
