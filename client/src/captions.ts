import type {
  CaptionMessage,
  SpeakerMessage,
  UtteranceMessage,
  UtteranceSource,
  UtteranceUpdateMessage,
} from "./protocol.ts";

/**
 * 字幕区里的一行。
 *
 * 一行有两种来历：实时字幕（`caption` 消息，按 segment_id 覆盖更新，还没定稿）和已落库的发言
 * （`utterance` 消息，或 HTTP 拉来的历史记录）。发言定稿时，同一个 segment_id 的实时字幕行**原地**变成定稿行。
 */
export interface CaptionLine {
  /** 实时字幕行的编号；来自历史记录的行没有 */
  segmentId: number | null;
  /** 已落库的发言编号；还没定稿或落库失败的行没有 */
  utteranceId: number | null;
  speakerIdx: number;
  speakerName: string;
  tStart: number;
  /** 已定稿的文字，不再回改 */
  stable: string;
  /** 还没定稿的尾巴，下一条消息会整体替换它 */
  unstable: string;
  /** 已定稿（发言已落库或已确认）。定稿的行不再被 caption 消息改写 */
  final: boolean;
  source: UtteranceSource;
}

/** 字幕区最多保留的行数；更早的在服务端的转录里都有，界面不必无限增长。 */
export const MAX_CAPTION_LINES = 300;

function trimTo(lines: CaptionLine[], limit: number): CaptionLine[] {
  return lines.length > limit ? lines.slice(lines.length - limit) : lines;
}

export function applyCaption(
  lines: readonly CaptionLine[],
  message: CaptionMessage,
  limit: number = MAX_CAPTION_LINES,
): CaptionLine[] {
  const index = lines.findIndex((l) => l.segmentId === message.segment_id);
  if (index >= 0 && lines[index].final) return [...lines]; // 已经定稿的行不再被实时字幕改写
  // 服务端在一行字幕没有任何定稿文字就收尾时，发一条文字全空的 caption：清掉灰色的临时字幕。
  if (message.stable === "" && message.unstable === "") {
    return index >= 0 ? lines.filter((_, i) => i !== index) : [...lines];
  }
  const line: CaptionLine = {
    segmentId: message.segment_id,
    utteranceId: null,
    speakerIdx: message.speaker_idx,
    speakerName: message.speaker_name,
    tStart: message.t_start,
    stable: message.stable,
    unstable: message.unstable,
    final: false,
    source: "asr",
  };
  const next = index >= 0 ? lines.map((l, i) => (i === index ? line : l)) : [...lines, line];
  return trimTo(next, limit);
}

function finalLine(message: UtteranceMessage): CaptionLine {
  return {
    segmentId: message.segment_id,
    utteranceId: message.id,
    speakerIdx: message.speaker_idx,
    speakerName: message.speaker_name,
    tStart: message.t_start,
    stable: message.text,
    unstable: "",
    final: true,
    source: message.source,
  };
}

/**
 * 一条发言定稿：对应的实时字幕行原地变成定稿行；没有对应行（助理的话、键入的文字）就追加。
 *
 * 服务端会把同一个人挨得很近的片段并进上一条发言：这时消息里的 `id` 是已经在界面上的那条发言，
 * `segment_id` 是新片段的实时字幕行——把那一行灰色字幕收掉，原来那一行的文字换成并好的。
 */
export function applyUtterance(
  lines: readonly CaptionLine[],
  message: UtteranceMessage,
  limit: number = MAX_CAPTION_LINES,
): CaptionLine[] {
  const line = finalLine(message);
  const bySegment =
    message.segment_id === null ? -1 : lines.findIndex((l) => l.segmentId === message.segment_id);
  const byId = message.id === null ? -1 : lines.findIndex((l) => l.utteranceId === message.id);
  if (byId >= 0 && bySegment >= 0 && byId !== bySegment) {
    const merged = lines.map((l, i) => (i === byId ? line : l)).filter((_, i) => i !== bySegment);
    return trimTo(merged, limit);
  }
  const index = bySegment >= 0 ? bySegment : byId;
  const next = index >= 0 ? lines.map((l, i) => (i === index ? line : l)) : [...lines, line];
  return trimTo(next, limit);
}

/** 说话人更正：按发言编号改那一行的说话人。 */
export function applyUtteranceUpdate(
  lines: readonly CaptionLine[],
  message: UtteranceUpdateMessage,
): CaptionLine[] {
  return lines.map((l) =>
    l.utteranceId === message.id
      ? { ...l, speakerIdx: message.speaker_idx, speakerName: message.speaker_name }
      : l,
  );
}

/** 说话人改名：这个说话人的所有行都跟着变（包括之前已经显示的）。 */
export function applySpeakerRename(
  lines: readonly CaptionLine[],
  message: Pick<SpeakerMessage, "idx" | "display_name">,
): CaptionLine[] {
  return lines.map((l) =>
    l.speakerIdx === message.idx ? { ...l, speakerName: message.display_name } : l,
  );
}

/** 合并说话人：`from` 的所有行都改到 `into` 名下。 */
export function applySpeakerMerge(
  lines: readonly CaptionLine[],
  merge: { from: number; into: number; display_name: string },
): CaptionLine[] {
  return lines.map((l) =>
    l.speakerIdx === merge.from || l.speakerIdx === merge.into
      ? { ...l, speakerIdx: merge.into, speakerName: merge.display_name }
      : l,
  );
}

/** `GET /api/utterances` 返回的一项（docs/interfaces.md §5.4）。 */
export interface HistoryItem {
  id: number;
  speaker_idx: number;
  speaker_name: string;
  t_start: number;
  t_end: number;
  text: string;
  source: UtteranceSource;
}

export function lineFromHistory(item: HistoryItem): CaptionLine {
  return {
    segmentId: null,
    utteranceId: item.id,
    speakerIdx: item.speaker_idx,
    speakerName: item.speaker_name,
    tStart: item.t_start,
    stable: item.text,
    unstable: "",
    final: true,
    source: item.source,
  };
}

/** 页面刚打开或切换会议：用历史记录整体替换字幕区（只留最近 limit 行）。 */
export function replaceWithHistory(
  items: readonly HistoryItem[],
  limit: number = MAX_CAPTION_LINES,
): CaptionLine[] {
  return trimTo(items.map(lineFromHistory), limit);
}

/** 向上翻页：更早的发言放到最前面，已经在界面上的不重复。 */
export function prependHistory(
  lines: readonly CaptionLine[],
  items: readonly HistoryItem[],
): CaptionLine[] {
  const known = new Set(lines.map((l) => l.utteranceId).filter((id) => id !== null));
  return [...items.filter((i) => !known.has(i.id)).map(lineFromHistory), ...lines];
}

/**
 * 断线重连后补齐（或只读观看时的轮询）：新增的发言追加到最后；已经在界面上的不重复，
 * 但内容以服务端的为准——那条发言之后可能又并进了新的片段，或者被改了说话人。
 */
export function appendHistory(
  lines: readonly CaptionLine[],
  items: readonly HistoryItem[],
  limit: number = MAX_CAPTION_LINES,
): CaptionLine[] {
  const fresh = new Map(items.map((i) => [i.id, i]));
  const known = new Set(lines.map((l) => l.utteranceId).filter((id) => id !== null));
  const updated = lines.map((l) => {
    const item = l.utteranceId === null ? undefined : fresh.get(l.utteranceId);
    return item ? { ...lineFromHistory(item), segmentId: l.segmentId } : l;
  });
  const added = items.filter((i) => !known.has(i.id)).map(lineFromHistory);
  return trimTo([...updated, ...added], limit);
}

/** 补齐时从哪条发言之后取：把界面上最后一条也重新取一次（它可能又变长了）。没有任何发言返回 null。 */
export function backfillAfterId(lines: readonly CaptionLine[]): number | null {
  const last = lastUtteranceId(lines);
  return last === null ? null : last - 1;
}

/** 界面上已知的最大发言编号（补齐时用作 after_id）；没有返回 null。 */
export function lastUtteranceId(lines: readonly CaptionLine[]): number | null {
  let last: number | null = null;
  for (const l of lines)
    if (l.utteranceId !== null && (last === null || l.utteranceId > last)) last = l.utteranceId;
  return last;
}

/** 界面上最早的发言编号（向上翻页时用作 before_id）；没有返回 null。 */
export function firstUtteranceId(lines: readonly CaptionLine[]): number | null {
  let first: number | null = null;
  for (const l of lines)
    if (l.utteranceId !== null && (first === null || l.utteranceId < first)) first = l.utteranceId;
  return first;
}

/** 会话时间轴上的秒数 → "m:ss"，满一小时为 "h:mm:ss"。 */
export function formatClock(secs: number): string {
  const total = Math.max(0, Math.floor(secs));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}
