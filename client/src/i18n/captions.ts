import { defineMessages } from "./define.ts";

/** 字幕区和上方的说话人 */
export const captions = defineMessages(
  {
    label: "字幕",
    gap: (span: string) => `中断了 ${span}`,
    loadOlder: "加载更早的发言",
    wallClock: "当时的时间",
    meetingClock: "会议进行到",
    select: (speaker: string, time: string) => `选中 ${speaker} 在 ${time} 的发言`,
    unselect: (speaker: string, time: string) => `取消选中 ${speaker} 在 ${time} 的发言`,
    selectHint: "点一下选中，按住上下拖可以连选；选中后点上方的说话人，就归到他名下",
    typed: "文字",
    followLatest: "跟随最新",
    speakers: {
      label: "说话人",
      assignLabel: "把选中的发言归到",
      selected: (n: number) => `已选 ${n} 条，归到`,
      assignTo: (n: number, name: string) => `把选中的 ${n} 条发言归到「${name}」名下`,
      newSpeaker: "新说话人",
      newSpeakerHint: "新建一个说话人，把选中的发言归到他名下",
      newSpeakerName: "新说话人的名字",
      namePlaceholder: "名字，回车确认",
      newSpeakerAs: (name: string) => `新建说话人「${name}」，把选中的发言归到他名下`,
      clearSelection: "取消选择",
      renameLabel: (name: string) => `给「${name}」改名`,
      renameTo: (from: string, to: string) => `把「${from}」改名为「${to}」`,
      mergeLabel: (name: string) => `把「${name}」合并到另一个说话人`,
      mergeConfirm: (from: string, into: string) =>
        `把「${from}」的全部发言都算到「${into}」名下吗？合并之后不能拆开。`,
      mergeInto: "合并到…",
      chipHint: "点击改名，或合并到另一个说话人",
      members: "成员名单",
      voice: "声纹…",
      voiceHint: "记住这位说话人的声音，或管理声纹档案",
      voiceLinked: (name: string) => `已关联声纹档案「${name}」`,
      maybe: (name: string) => `可能是 ${name}`,
      suggestionLabel: (speaker: string, name: string) =>
        `「${speaker}」听起来像声纹档案里的「${name}」`,
      acceptSuggestion: (speaker: string, name: string) =>
        `是 ${name}：把「${speaker}」改名为「${name}」并关联档案`,
      dismissSuggestion: "不是，这场会议里不再提示",
    },
  },
  {
    label: "Captions",
    gap: (span: string) => `Interrupted for ${span}`,
    loadOlder: "Load earlier captions",
    wallClock: "Time of day",
    meetingClock: "Meeting time",
    select: (speaker: string, time: string) => `Select ${speaker}'s caption at ${time}`,
    unselect: (speaker: string, time: string) => `Deselect ${speaker}'s caption at ${time}`,
    selectHint:
      "Click to select, or hold and drag up or down to select several; then click a speaker above to assign them",
    typed: "Typed",
    followLatest: "Follow latest",
    speakers: {
      label: "Speakers",
      assignLabel: "Assign the selected captions to",
      selected: (n: number) => `${n} selected, assign to`,
      assignTo: (n: number, name: string) =>
        `Assign the ${n} selected ${n === 1 ? "caption" : "captions"} to “${name}”`,
      newSpeaker: "New speaker",
      newSpeakerHint: "Create a speaker and assign the selected captions to them",
      newSpeakerName: "New speaker's name",
      namePlaceholder: "Name, then Enter",
      newSpeakerAs: (name: string) =>
        `Create the speaker “${name}” and assign the selected captions to them`,
      clearSelection: "Clear selection",
      renameLabel: (name: string) => `Rename “${name}”`,
      renameTo: (from: string, to: string) => `Rename “${from}” to “${to}”`,
      mergeLabel: (name: string) => `Merge “${name}” into another speaker`,
      mergeConfirm: (from: string, into: string) =>
        `Move all of “${from}”'s captions to “${into}”? Merged speakers cannot be split again.`,
      mergeInto: "Merge into…",
      chipHint: "Click to rename, or merge into another speaker",
      members: "Members",
      voice: "Voice…",
      voiceHint: "Remember this speaker's voice, or manage voice profiles",
      voiceLinked: (name: string) => `Linked to the voice profile “${name}”`,
      maybe: (name: string) => `Maybe ${name}`,
      suggestionLabel: (speaker: string, name: string) =>
        `“${speaker}” sounds like “${name}” from the voice profiles`,
      acceptSuggestion: (speaker: string, name: string) =>
        `Yes, ${name}: rename “${speaker}” to “${name}” and link the profile`,
      dismissSuggestion: "No, stop suggesting this in this meeting",
    },
  },
);
