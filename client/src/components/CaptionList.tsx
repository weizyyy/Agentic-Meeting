import { ArrowDownIcon } from "lucide-react";
import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";

import type { ConnectionSpan } from "../api.ts";
import { formatClock, type CaptionLine } from "../captions.ts";
import { formatSpan, useI18n } from "../i18n/index.ts";
import { connectionGaps, gapPositions } from "../resume.ts";
import { selectRange, selectSpan, selectableId, toggleSelection } from "../selection.ts";
import { wallClockLabel, type TimeBase } from "../timeline.ts";

interface Props {
  lines: readonly CaptionLine[];
  hasOlder: boolean;
  /** 这场会议的各次连接：相邻两次之间的空档画成一行分隔 */
  connections: readonly ConnectionSpan[];
  /** 这场会议的时间基准：算每一行当时墙上的钟点；没有会议时是 null */
  timeBase: TimeBase | null;
  /** 空态说明：没连接时提示怎么开始，连接中提示可以说话了 */
  emptyText: string;
  onLoadOlder: () => void;
  /** 选中的发言（按发言编号）；选中之后点上方的说话人就归到他名下 */
  selected: ReadonlySet<number>;
  onSelect: (next: ReadonlySet<number>) => void;
}

/** 距离底部多少像素以内算「跟着最新字幕走」；用户往上翻看时不要把他拽回底部。 */
const STICK_THRESHOLD_PX = 80;

function lineKey(line: CaptionLine, index: number): string {
  if (line.segmentId !== null) return `s${line.segmentId}`;
  if (line.utteranceId !== null) return `u${line.utteranceId}`;
  return `i${index}`;
}

/** 一次按住拖动：从哪条发言按下的、按下之前选中了哪些、有没有拖到别的行。 */
interface Drag {
  anchorId: number;
  base: ReadonlySet<number>;
  moved: boolean;
}

/**
 * 字幕：定稿的文字正常颜色，还没定稿的尾巴灰色；助理的话底色是助理的紫色，键入的文字带「文字」标记。
 * 每条上面一行是说话人、当时的钟点和会议进行到哪——这一行可以点（选中 / 取消）、
 * 可以按住往上下拖（连选一段）；下面的文字不参与，照常可以划选复制。
 */
export function CaptionList({
  lines,
  hasOlder,
  connections,
  timeBase,
  emptyText,
  onLoadOlder,
  selected,
  onSelect,
}: Props) {
  const { t, language } = useI18n();
  const drag = useRef<Drag | null>(null);
  // 拖完松手时浏览器可能还会补一个点击事件：这一下不算「点一下切换」
  const swallowClick = useRef(false);
  const lastPicked = useRef<number | null>(null);

  useEffect(() => {
    const end = () => {
      if (drag.current?.moved) {
        swallowClick.current = true;
        window.setTimeout(() => {
          swallowClick.current = false;
        }, 0);
      }
      drag.current = null;
    };
    window.addEventListener("mouseup", end);
    return () => window.removeEventListener("mouseup", end);
  }, []);

  const press = (id: number) => {
    drag.current = { anchorId: id, base: selected, moved: false };
  };
  const dragOver = (index: number) => {
    const current = drag.current;
    if (!current) return;
    const anchor = lines.findIndex((l) => selectableId(l) === current.anchorId);
    if (anchor < 0 || (!current.moved && anchor === index)) return;
    current.moved = true;
    lastPicked.current = current.anchorId;
    onSelect(selectSpan(lines, current.base, anchor, index));
  };
  const click = (id: number, range: boolean) => {
    if (swallowClick.current) {
      swallowClick.current = false;
      return;
    }
    const from = lastPicked.current;
    lastPicked.current = id;
    onSelect(
      range && from !== null
        ? selectRange(lines, selected, from, id)
        : toggleSelection(selected, id),
    );
  };

  const gaps = useMemo(
    () =>
      gapPositions(
        lines.map((l) => l.tStart),
        connectionGaps(connections),
        hasOlder,
      ),
    [lines, connections, hasOlder],
  );
  const divider = (index: number) => {
    const secs = gaps.get(index);
    return secs === undefined ? null : (
      <p
        className="my-3 flex items-center gap-3 text-xs text-muted-foreground before:h-px before:flex-1 before:bg-border after:h-px after:flex-1 after:bg-border"
        role="separator"
      >
        {t.captions.gap(formatSpan(t, secs))}
      </p>
    );
  };
  const scroller = useRef<HTMLDivElement>(null);
  const stuck = useRef(true);
  // 往上翻看时显示「跟随最新」，点一下回到底部
  const [following, setFollowing] = useState(true);
  const before = useRef<{ height: number; firstKey: string | null }>({ height: 0, firstKey: null });

  // 内容变化后：向上加载了更早的发言就保持原来看到的位置；否则在「跟着走」时滚到底。
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const firstKey = lines.length > 0 ? lineKey(lines[0], 0) : null;
    const prepended =
      before.current.firstKey !== null &&
      firstKey !== before.current.firstKey &&
      before.current.height > 0;
    if (prepended) el.scrollTop += el.scrollHeight - before.current.height;
    else if (stuck.current) el.scrollTop = el.scrollHeight;
    before.current = { height: el.scrollHeight, firstKey };
  }, [lines]);

  return (
    <section className="relative flex min-h-0 flex-1 flex-col" aria-label={t.captions.label}>
      <div
        className="min-h-0 flex-1 overflow-y-auto px-2 py-2"
        ref={scroller}
        onScroll={(event) => {
          const el = event.currentTarget;
          stuck.current = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_THRESHOLD_PX;
          setFollowing(stuck.current);
        }}
      >
        {hasOlder && (
          <Button variant="link" size="sm" className="mx-auto mb-2 flex" onClick={onLoadOlder}>
            {t.captions.loadOlder}
          </Button>
        )}
        {lines.length === 0 ? (
          <p className="px-2 py-1 text-sm text-subtle-foreground">{emptyText}</p>
        ) : (
          lines.map((line, index) => {
            const id = selectableId(line);
            const picked = id !== null && selected.has(id);
            const wall = wallClockLabel(language, line.tStart, timeBase);
            const assistant = line.source === "assistant";
            const head = (
              <>
                <span
                  aria-hidden
                  className={`size-1.5 shrink-0 rounded-full ${
                    assistant ? "bg-agent-foreground" : "bg-client-foreground"
                  }`}
                />
                <span
                  className={`max-w-48 truncate text-sm font-medium ${
                    assistant ? "text-agent-foreground" : "text-foreground"
                  }${line.source === "text" ? " italic" : ""}`}
                >
                  {line.speakerName}
                </span>
                {wall && (
                  <time
                    className="font-mono text-xs text-muted-foreground"
                    title={t.captions.wallClock}
                  >
                    {wall}
                  </time>
                )}
                <time
                  className="font-mono text-xs text-subtle-foreground"
                  title={t.captions.meetingClock}
                >
                  +{formatClock(line.tStart)}
                </time>
              </>
            );
            return (
              <Fragment key={lineKey(line, index)}>
                {divider(index)}
                <div
                  className={`mb-1 rounded-lg px-3 py-2 ${
                    picked
                      ? "bg-client ring-1 ring-client-foreground/40"
                      : assistant
                        ? "bg-agent"
                        : ""
                  }`}
                  onMouseEnter={() => dragOver(index)}
                >
                  {id !== null ? (
                    <button
                      type="button"
                      className="-mx-1 inline-flex max-w-full cursor-pointer items-center gap-2 rounded-md px-1 select-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none aria-pressed:bg-client-foreground/15"
                      aria-pressed={picked}
                      aria-label={(picked ? t.captions.unselect : t.captions.select)(
                        line.speakerName,
                        formatClock(line.tStart),
                      )}
                      title={t.captions.selectHint}
                      onMouseDown={(event) => {
                        if (event.button !== 0) return;
                        event.preventDefault(); // 拖的时候不要划选文字
                        press(id);
                      }}
                      onClick={(event) => click(id, event.shiftKey)}
                    >
                      {head}
                    </button>
                  ) : (
                    <span className="inline-flex max-w-full items-center gap-2">{head}</span>
                  )}
                  <p className="mt-0.5 text-[0.9375rem] leading-relaxed [overflow-wrap:anywhere]">
                    {line.source === "text" && (
                      <span className="mr-1.5 rounded bg-muted px-1.5 py-px text-xs text-muted-foreground">
                        {t.captions.typed}
                      </span>
                    )}
                    {line.stable}
                    <span className="text-subtle-foreground">{line.unstable}</span>
                  </p>
                </div>
              </Fragment>
            );
          })
        )}
        {lines.length > 0 && divider(lines.length)}
      </div>
      {!following && (
        <Button
          variant="outline"
          size="sm"
          className="absolute right-4 bottom-3 shadow-sm"
          onClick={() => {
            const el = scroller.current;
            if (el) el.scrollTop = el.scrollHeight;
            stuck.current = true;
            setFollowing(true);
          }}
        >
          <ArrowDownIcon data-icon="inline-start" />
          {t.captions.followLatest}
        </Button>
      )}
    </section>
  );
}
