import { PlusIcon, XIcon } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

import type { SpeakerInfo } from "../api.ts";
import { assignTargets } from "../selection.ts";
import { mergeTargets, nameSuggestions, renamableSpeakers } from "../sessionView.ts";

/** 把选中的发言归给谁：已有的说话人，或者新建一个（给名字）。 */
export type AssignTarget = { speakerIdx: number } | { newSpeaker: string };

/** 这一排的布局；字幕卡片里，标题下面一行 */
const ROW = "flex flex-wrap items-center gap-1.5 border-b px-4 py-2.5";
/** 说话人小圆角按钮 */
const CHIP = "rounded-full px-2.5 font-normal";
/** 改名 / 新建时那一组：输入框、成员名单、合并到… */
const EDITING = "inline-flex flex-wrap items-center gap-1.5";
const NAME_INPUT = "h-6 w-36 rounded-full px-2.5 text-xs md:text-xs";

/** 名字前面的小圆点：说话人的颜色（与字幕里一致） */
function Dot() {
  return <span aria-hidden className="size-1.5 rounded-full bg-client-foreground" />;
}

interface Props {
  speakers: readonly SpeakerInfo[];
  members: readonly string[];
  onRename: (idx: number, displayName: string) => void;
  onMerge: (idx: number, into: number) => void;
  /** 字幕里选中了几条发言；大于 0 时这一排变成「点一下就归过去」 */
  selectedCount: number;
  onAssign: (target: AssignTarget) => void;
  onClearSelection: () => void;
}

/**
 * 说话人面板：本场出现过的说话人。
 * 平时点一个就能改名（配置里的成员名单作候选），或把他并到另一个人名下；
 * 字幕里选中了发言时，点一个就是把选中的发言归到他名下，末尾多一个「新说话人」。
 */
export function SpeakerBar({
  speakers,
  members,
  onRename,
  onMerge,
  selectedCount,
  onAssign,
  onClearSelection,
}: Props) {
  const [editing, setEditing] = useState<{ idx: number; name: string } | null>(null);
  const [adding, setAdding] = useState<string | null>(null);
  const assigning = selectedCount > 0;
  useEffect(() => {
    if (assigning) setEditing(null);
    else setAdding(null);
  }, [assigning]);

  if (assigning) {
    const commitNew = () => {
      const name = adding?.trim();
      setAdding(null);
      if (name) onAssign({ newSpeaker: name });
    };
    return (
      <div className={ROW} role="group" aria-label="把选中的发言归到">
        <span className="text-xs font-medium text-client-foreground">
          已选 {selectedCount} 条，归到
        </span>
        {assignTargets(speakers).map((p) => (
          <Button
            key={p.idx}
            variant="outline"
            size="xs"
            className={`${CHIP} border-client-foreground/50 hover:bg-client`}
            title={`把选中的 ${selectedCount} 条发言归到「${p.display_name}」名下`}
            onClick={() => onAssign({ speakerIdx: p.idx })}
          >
            <Dot />
            {p.display_name}
          </Button>
        ))}
        {adding === null ? (
          <Button
            variant="outline"
            size="xs"
            className={`${CHIP} border-dashed border-client-foreground/50 hover:bg-client`}
            title="新建一个说话人，把选中的发言归到他名下"
            onClick={() => setAdding("")}
          >
            <PlusIcon data-icon="inline-start" />
            新说话人
          </Button>
        ) : (
          <span
            className={EDITING}
            onBlur={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget)) setAdding(null);
            }}
          >
            <Input
              className={NAME_INPUT}
              aria-label="新说话人的名字"
              placeholder="名字，回车确认"
              list="speaker-names"
              value={adding}
              maxLength={50}
              autoFocus
              onChange={(event) => setAdding(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") commitNew();
                if (event.key === "Escape") {
                  event.stopPropagation();
                  setAdding(null);
                }
              }}
            />
            <datalist id="speaker-names">
              {nameSuggestions(members, speakers, null).map((name) => (
                <option key={name} value={name} />
              ))}
            </datalist>
            <MemberChoices
              names={nameSuggestions(members, speakers, null)}
              label={(name) => `新建说话人「${name}」，把选中的发言归到他名下`}
              onPick={(name) => {
                setAdding(null);
                onAssign({ newSpeaker: name });
              }}
            />
          </span>
        )}
        <Button variant="ghost" size="xs" onClick={onClearSelection}>
          <XIcon data-icon="inline-start" />
          取消选择
        </Button>
      </div>
    );
  }

  const people = renamableSpeakers(speakers);
  if (people.length === 0) return null;

  const commit = () => {
    if (editing) {
      const current = people.find((p) => p.idx === editing.idx);
      const name = editing.name.trim();
      if (name && name !== current?.display_name) onRename(editing.idx, name);
    }
    setEditing(null);
  };

  return (
    <div className={ROW} role="group" aria-label="说话人">
      <span className="text-xs text-muted-foreground">说话人</span>
      {people.map((p) =>
        editing?.idx === p.idx ? (
          <span
            key={p.idx}
            className={EDITING}
            onBlur={(event) => {
              // 焦点还在这个小面板里（从输入框移到「合并到…」）就不收起
              if (!event.currentTarget.contains(event.relatedTarget)) commit();
            }}
          >
            <Input
              className={NAME_INPUT}
              aria-label={`给「${p.display_name}」改名`}
              list="speaker-names"
              value={editing.name}
              maxLength={50}
              autoFocus
              onChange={(event) => setEditing({ idx: p.idx, name: event.target.value })}
              onKeyDown={(event) => {
                if (event.key === "Enter") commit();
                if (event.key === "Escape") setEditing(null);
              }}
            />
            <datalist id="speaker-names">
              {nameSuggestions(members, speakers, p.idx).map((name) => (
                <option key={name} value={name} />
              ))}
            </datalist>
            <MemberChoices
              names={nameSuggestions(members, speakers, p.idx).filter((n) => n !== p.display_name)}
              label={(name) => `把「${p.display_name}」改名为「${name}」`}
              onPick={(name) => {
                setEditing(null);
                onRename(p.idx, name);
              }}
            />
            {mergeTargets(speakers, p.idx).length > 0 && (
              <select
                className="h-6 rounded-full border border-input bg-background px-2 text-xs text-foreground dark:bg-input/30"
                aria-label={`把「${p.display_name}」合并到另一个说话人`}
                value=""
                onChange={(event) => {
                  const into = Number(event.target.value);
                  const target = speakers.find((s) => s.idx === into);
                  setEditing(null);
                  if (
                    target &&
                    window.confirm(
                      `把「${p.display_name}」的全部发言都算到「${target.display_name}」名下吗？合并之后不能拆开。`,
                    )
                  ) {
                    onMerge(p.idx, into);
                  }
                }}
              >
                <option value="">合并到…</option>
                {mergeTargets(speakers, p.idx).map((target) => (
                  <option key={target.idx} value={target.idx}>
                    {target.display_name}
                  </option>
                ))}
              </select>
            )}
          </span>
        ) : (
          <Button
            key={p.idx}
            variant="outline"
            size="xs"
            className={CHIP}
            title="点击改名，或合并到另一个说话人"
            onClick={() => setEditing({ idx: p.idx, name: p.display_name })}
          >
            <Dot />
            {p.display_name}
          </Button>
        ),
      )}
    </div>
  );
}

/**
 * 成员名单里的候选，直接显示成按钮点一下就用。
 * 不能只靠 datalist：输入框里已经有当前名字，浏览器只列出包含这几个字的候选，名单看上去就是空的。
 */
function MemberChoices({
  names,
  label,
  onPick,
}: {
  names: readonly string[];
  label: (name: string) => string;
  onPick: (name: string) => void;
}) {
  if (names.length === 0) return null;
  return (
    <span className="inline-flex flex-wrap gap-1" role="group" aria-label="成员名单">
      {names.map((name) => (
        <Button
          key={name}
          variant="outline"
          size="xs"
          className={`${CHIP} border-dashed text-muted-foreground hover:border-client-foreground`}
          title={label(name)}
          aria-label={label(name)}
          // 不抢输入框的焦点：Safari 点按钮不给按钮焦点，失焦会先把编辑框收起来，点击就落空了
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => onPick(name)}
        >
          {name}
        </Button>
      ))}
    </span>
  );
}
