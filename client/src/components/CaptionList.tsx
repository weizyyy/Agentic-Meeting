import { Fragment, useEffect, useLayoutEffect, useMemo, useRef } from "react";

import type { ConnectionSpan } from "../api.ts";
import { formatClock, type CaptionLine } from "../captions.ts";
import { connectionGaps, formatSpan, gapPositions } from "../resume.ts";
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
 * 字幕：定稿的文字正常颜色，还没定稿的尾巴灰色；助理的话和键入的文字有各自的样式。
 * 每行左边是两条轨的时间（会议进行到哪、当时的钟点）和说话人——这一块可以点（选中 / 取消）、
 * 可以按住往上下拖（连选一段）；文字那一块不参与，照常可以划选复制。
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
      <p className="caption-gap" role="separator">
        — 中断了 {formatSpan(secs)} —
      </p>
    );
  };
  const scroller = useRef<HTMLDivElement>(null);
  const stuck = useRef(true);
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
    <section className="panel" aria-label="字幕">
      <h2>字幕</h2>
      <div
        className="captions"
        ref={scroller}
        onScroll={(event) => {
          const el = event.currentTarget;
          stuck.current = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_THRESHOLD_PX;
        }}
      >
        {hasOlder && (
          <button type="button" className="link older" onClick={onLoadOlder}>
            加载更早的发言
          </button>
        )}
        {lines.length === 0 ? (
          <p className="empty">{emptyText}</p>
        ) : (
          lines.map((line, index) => {
            const id = selectableId(line);
            const picked = id !== null && selected.has(id);
            const wall = wallClockLabel(line.tStart, timeBase);
            const head = (
              <>
                <time className="wall" title="当时的时间">
                  {wall}
                </time>
                <time className="elapsed" title="会议进行到">
                  {formatClock(line.tStart)}
                </time>
                <span className="speaker">{line.speakerName}</span>
              </>
            );
            return (
              <Fragment key={lineKey(line, index)}>
                {divider(index)}
                <p
                  className={`caption caption-${line.source}${line.final ? "" : " caption-live"}${
                    picked ? " caption-selected" : ""
                  }`}
                  onMouseEnter={() => dragOver(index)}
                >
                  {id !== null ? (
                    <button
                      type="button"
                      className="caption-head caption-handle"
                      aria-pressed={picked}
                      aria-label={`${picked ? "取消选中" : "选中"} ${line.speakerName} 在 ${formatClock(line.tStart)} 的发言`}
                      title="点一下选中，按住上下拖可以连选；选中后点上方的说话人，就归到他名下"
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
                    <span className="caption-head">{head}</span>
                  )}
                  <span className="text">
                    {line.source === "text" && <span className="tag">文字</span>}
                    {line.stable}
                    <span className="unstable">{line.unstable}</span>
                  </span>
                </p>
              </Fragment>
            );
          })
        )}
        {lines.length > 0 && divider(lines.length)}
      </div>
    </section>
  );
}
