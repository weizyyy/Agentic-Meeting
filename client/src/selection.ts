// 选中字幕、批量改发言人的纯逻辑（可以用 npm test 直接测）。

import type { SpeakerInfo } from "./api.ts";
import type { CaptionLine } from "./captions.ts";

/** 这一行能不能改发言人：已经落库的、语音识别来的发言才行（助理的话、键入的文字、还在说的实时字幕都不行）。 */
export function selectableId(line: CaptionLine): number | null {
  return line.final && line.source === "asr" ? line.utteranceId : null;
}

/** 点一下：选中的取消，没选的选上。 */
export function toggleSelection(selected: ReadonlySet<number>, id: number): Set<number> {
  const next = new Set(selected);
  if (!next.delete(id)) next.add(id);
  return next;
}

/** 按住 Shift 点（按发言编号）：从上一次点的那一行到这一行，中间能选的全选上（不取消已经选中的）。 */
export function selectRange(
  lines: readonly CaptionLine[],
  selected: ReadonlySet<number>,
  fromId: number,
  toId: number,
): Set<number> {
  const ids = lines.map(selectableId);
  const a = ids.indexOf(fromId);
  const b = ids.indexOf(toId);
  if (a < 0 || b < 0) return new Set(selected).add(toId);
  return selectSpan(lines, selected, a, b);
}

/**
 * 按住拖动：从第 `fromIndex` 行到第 `toIndex` 行（哪头在前都行），中间能选的全选上，加到 `selected` 里。
 * 两头可以落在不能选的行上（拖过助理的话），那些行跳过。
 */
export function selectSpan(
  lines: readonly CaptionLine[],
  selected: ReadonlySet<number>,
  fromIndex: number,
  toIndex: number,
): Set<number> {
  const next = new Set(selected);
  const lo = Math.max(0, Math.min(fromIndex, toIndex));
  const hi = Math.min(lines.length - 1, Math.max(fromIndex, toIndex));
  for (let i = lo; i <= hi; i += 1) {
    const id = selectableId(lines[i]);
    if (id !== null) next.add(id);
  }
  return next;
}

/** 字幕变了（换了会议、旧的行被挤掉）：选中的里面只留还在界面上的。内容没变时返回原来那个集合。 */
export function pruneSelection(
  lines: readonly CaptionLine[],
  selected: ReadonlySet<number>,
): ReadonlySet<number> {
  if (selected.size === 0) return selected;
  const present = new Set(lines.map(selectableId).filter((id) => id !== null));
  const kept = [...selected].filter((id) => present.has(id));
  return kept.length === selected.size ? selected : new Set(kept);
}

/** 可以改成谁：说话人区分给出的和手动建的人（编号从 1 起），排除助理、键入的文字、未知。 */
export function assignTargets(speakers: readonly SpeakerInfo[]): SpeakerInfo[] {
  return speakers.filter((s) => s.idx >= 1);
}
