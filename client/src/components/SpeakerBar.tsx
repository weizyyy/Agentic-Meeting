import { AudioLinesIcon, CheckIcon, PlusIcon, XIcon } from "lucide-react";
import { Fragment, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

import type { SpeakerInfo } from "../api.ts";
import { useI18n } from "../i18n/index.ts";
import type { SpeakerSuggestion } from "../protocol.ts";
import { assignTargets } from "../selection.ts";
import { mergeTargets, nameSuggestions, renamableSpeakers } from "../sessionView.ts";

/** 把选中的发言归给谁：已有的说话人，或者新建一个（给名字）。 */
export type AssignTarget = { speakerIdx: number } | { newSpeaker: string };

/** 这一排的布局；字幕卡片里，标题下面一行 */
const ROW = "flex flex-wrap items-center gap-1.5 border-b px-4 py-2.5 max-md:px-3 max-md:py-1.5";
/** 手机上说话人只排一行、左右滑动，不折成好几行挤掉字幕；改名、新建时照常折行，输入框和候选都看得见 */
const ONE_LINE = "max-md:flex-nowrap max-md:overflow-x-auto max-md:[&>*]:shrink-0";
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
  /** 打开这位说话人的声纹（记住他的声音）；没开说话人记忆时不给 */
  onVoice?: (idx: number) => void;
  /** 「可能是某某」的提示（只有主持人有） */
  suggestions?: readonly SpeakerSuggestion[];
  onAcceptSuggestion?: (suggestion: SpeakerSuggestion) => void;
  onDismissSuggestion?: (suggestion: SpeakerSuggestion) => void;
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
  onVoice,
  suggestions = [],
  onAcceptSuggestion,
  onDismissSuggestion,
}: Props) {
  const { t } = useI18n();
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
      <div
        className={`${ROW} ${adding === null ? ONE_LINE : ""}`}
        role="group"
        aria-label={t.captions.speakers.assignLabel}
      >
        <span className="text-xs font-medium text-client-foreground">
          {t.captions.speakers.selected(selectedCount)}
        </span>
        {assignTargets(speakers).map((p) => (
          <Button
            key={p.idx}
            variant="outline"
            size="xs"
            className={`${CHIP} border-client-foreground/50 hover:bg-client`}
            title={t.captions.speakers.assignTo(selectedCount, p.display_name)}
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
            title={t.captions.speakers.newSpeakerHint}
            onClick={() => setAdding("")}
          >
            <PlusIcon data-icon="inline-start" />
            {t.captions.speakers.newSpeaker}
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
              aria-label={t.captions.speakers.newSpeakerName}
              placeholder={t.captions.speakers.namePlaceholder}
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
              label={t.captions.speakers.newSpeakerAs}
              onPick={(name) => {
                setAdding(null);
                onAssign({ newSpeaker: name });
              }}
            />
          </span>
        )}
        <Button variant="ghost" size="xs" onClick={onClearSelection}>
          <XIcon data-icon="inline-start" />
          {t.captions.speakers.clearSelection}
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
    <div
      className={`${ROW} ${editing === null ? ONE_LINE : ""}`}
      role="group"
      aria-label={t.captions.speakers.label}
    >
      <span className="text-xs text-muted-foreground">{t.captions.speakers.label}</span>
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
              aria-label={t.captions.speakers.renameLabel(p.display_name)}
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
              label={(name) => t.captions.speakers.renameTo(p.display_name, name)}
              onPick={(name) => {
                setEditing(null);
                onRename(p.idx, name);
              }}
            />
            {mergeTargets(speakers, p.idx).length > 0 && (
              <select
                className="h-6 rounded-full border border-input bg-background px-2 text-xs text-foreground dark:bg-input/30"
                aria-label={t.captions.speakers.mergeLabel(p.display_name)}
                value=""
                onChange={(event) => {
                  const into = Number(event.target.value);
                  const target = speakers.find((s) => s.idx === into);
                  setEditing(null);
                  if (
                    target &&
                    window.confirm(
                      t.captions.speakers.mergeConfirm(p.display_name, target.display_name),
                    )
                  ) {
                    onMerge(p.idx, into);
                  }
                }}
              >
                <option value="">{t.captions.speakers.mergeInto}</option>
                {mergeTargets(speakers, p.idx).map((target) => (
                  <option key={target.idx} value={target.idx}>
                    {target.display_name}
                  </option>
                ))}
              </select>
            )}
            {onVoice && p.idx >= 1 && p.voice_secs !== undefined && (
              <Button
                variant="outline"
                size="xs"
                className={CHIP}
                title={t.captions.speakers.voiceHint}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  commit();
                  onVoice(p.idx);
                }}
              >
                <AudioLinesIcon data-icon="inline-start" />
                {t.captions.speakers.voice}
              </Button>
            )}
          </span>
        ) : (
          <Fragment key={p.idx}>
            <Button
              variant="outline"
              size="xs"
              className={CHIP}
              title={
                p.voice_profile
                  ? `${t.captions.speakers.chipHint}\n${t.captions.speakers.voiceLinked(p.voice_profile.name)}`
                  : t.captions.speakers.chipHint
              }
              onClick={() => setEditing({ idx: p.idx, name: p.display_name })}
            >
              <Dot />
              {p.display_name}
              {p.voice_profile && (
                <AudioLinesIcon
                  data-icon="inline-end"
                  aria-label={t.captions.speakers.voiceLinked(p.voice_profile.name)}
                />
              )}
            </Button>
            <Suggestion
              suggestion={suggestions.find((s) => s.speaker_idx === p.idx)}
              speaker={p.display_name}
              onAccept={onAcceptSuggestion}
              onDismiss={onDismissSuggestion}
            />
          </Fragment>
        ),
      )}
    </div>
  );
}

/** 说话人旁边的「可能是某某」：确认（改名并关联档案）或忽略。 */
function Suggestion({
  suggestion,
  speaker,
  onAccept,
  onDismiss,
}: {
  suggestion: SpeakerSuggestion | undefined;
  speaker: string;
  onAccept?: (suggestion: SpeakerSuggestion) => void;
  onDismiss?: (suggestion: SpeakerSuggestion) => void;
}) {
  const { t } = useI18n();
  if (!suggestion || !onAccept || !onDismiss) return null;
  return (
    <span
      className="inline-flex items-center gap-0.5 rounded-full border border-dashed border-client-foreground/50 pl-2 text-xs text-client-foreground"
      role="group"
      aria-label={t.captions.speakers.suggestionLabel(speaker, suggestion.name)}
    >
      {t.captions.speakers.maybe(suggestion.name)}
      <Button
        variant="ghost"
        size="icon-xs"
        className="rounded-full"
        title={t.captions.speakers.acceptSuggestion(speaker, suggestion.name)}
        aria-label={t.captions.speakers.acceptSuggestion(speaker, suggestion.name)}
        onClick={() => onAccept(suggestion)}
      >
        <CheckIcon />
      </Button>
      <Button
        variant="ghost"
        size="icon-xs"
        className="rounded-full"
        title={t.captions.speakers.dismissSuggestion}
        aria-label={t.captions.speakers.dismissSuggestion}
        onClick={() => onDismiss(suggestion)}
      >
        <XIcon />
      </Button>
    </span>
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
  const { t } = useI18n();
  if (names.length === 0) return null;
  return (
    <span
      className="inline-flex flex-wrap gap-1"
      role="group"
      aria-label={t.captions.speakers.members}
    >
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
