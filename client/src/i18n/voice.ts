import { defineMessages } from "./define.ts";

/** 声纹档案：说话人旁「记住这个声音」和档案管理（docs/interfaces.md §5.14） */
export const voice = defineMessages(
  {
    open: (name: string) => `「${name}」的声纹`,
    title: "声纹",
    description:
      "记住一位说话人的声音，以后的会议里就能认出他。声纹只保存在这台服务器上，随时可以删除。",
    collected: (have: number, need: number) =>
      `这场会议里已采集 ${have} 秒单人语音，录入至少需要 ${need} 秒。`,
    linked: (name: string) => `已关联到声纹档案「${name}」。`,
    unlink: "取消关联",
    unlinkHint: "这场会议不再算作这份档案的样本；档案本身保留",
    newProfile: "新建档案",
    existingProfile: "录进已有的档案",
    profileName: "档案名字",
    chooseProfile: "选择档案",
    consent: "本人知道并同意保存他的声纹",
    remember: "记住这个声音",
    profiles: "全部声纹档案",
    noProfiles: "还没有声纹档案。",
    profileLine: (samples: number, secs: number) => `${samples} 场会议，共 ${secs} 秒`,
    rename: "改名",
    save: "保存",
    cancel: "取消",
    delete: "删除",
    deleteConfirm: (name: string) =>
      `删除「${name}」的声纹档案吗？他在各场会议里的声纹片段也会一起删除，发言和字幕保留。删除后不能恢复。`,
    deleted: (name: string, clips: number) => `已删除「${name}」的声纹档案和 ${clips} 个声纹片段。`,
  },
  {
    open: (name: string) => `Voice of “${name}”`,
    title: "Voice",
    description:
      "Remember a speaker's voice so later meetings can recognize them. Voiceprints stay on this server and can be deleted at any time.",
    collected: (have: number, need: number) =>
      `${have} s of single-speaker speech collected in this meeting; at least ${need} s are needed.`,
    linked: (name: string) => `Linked to the voice profile “${name}”.`,
    unlink: "Unlink",
    unlinkHint: "This meeting no longer counts as a sample of the profile; the profile stays",
    newProfile: "New profile",
    existingProfile: "Add to an existing profile",
    profileName: "Profile name",
    chooseProfile: "Choose a profile",
    consent: "This person knows and agrees that their voiceprint is kept",
    remember: "Remember this voice",
    profiles: "All voice profiles",
    noProfiles: "No voice profiles yet.",
    profileLine: (samples: number, secs: number) =>
      `${samples} ${samples === 1 ? "meeting" : "meetings"}, ${secs} s in total`,
    rename: "Rename",
    save: "Save",
    cancel: "Cancel",
    delete: "Delete",
    deleteConfirm: (name: string) =>
      `Delete the voice profile “${name}”? Their voice clips in every meeting are deleted too; captions are kept. This cannot be undone.`,
    deleted: (name: string, clips: number) =>
      `Deleted the voice profile “${name}” and ${clips} voice ${clips === 1 ? "clip" : "clips"}.`,
  },
);
