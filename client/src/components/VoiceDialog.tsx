import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

import { createApi, type SpeakerInfo, type VoiceProfile } from "../api.ts";
import { useI18n } from "../i18n/index.ts";

const api = createApi();

const INPUT = "h-8";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

interface Props {
  sessionId: string;
  /** 打开的是哪位说话人的声纹；null 时关闭 */
  speaker: SpeakerInfo | null;
  onClose: () => void;
  /** 录入、取消关联或删除档案之后：重新读取说话人列表（名字和关联都可能变了） */
  onChanged: () => void;
}

/**
 * 一位说话人的声纹：征得本人同意后记住他的声音（新建档案或录进已有的档案），
 * 已关联的可以取消关联；下面列出全部档案，可以改名、删除。
 */
export function VoiceDialog({ sessionId, speaker, onClose, onChanged }: Props) {
  const { t } = useI18n();
  const [current, setCurrent] = useState<SpeakerInfo | null>(null);
  const [profiles, setProfiles] = useState<VoiceProfile[]>([]);
  const [need, setNeed] = useState(0);
  const [mode, setMode] = useState<"new" | "existing">("new");
  const [name, setName] = useState("");
  const [profileId, setProfileId] = useState<number | null>(null);
  const [consent, setConsent] = useState(false);
  const [renaming, setRenaming] = useState<{ id: number; name: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const generation = useRef(0);
  const idx = speaker?.idx;

  async function load(request: number) {
    const [listing, speakers] = await Promise.all([
      api.listVoiceProfiles(),
      api.listSpeakers(sessionId),
    ]);
    if (request !== generation.current) return;
    setProfiles(listing.items);
    setNeed(listing.min_enroll_secs);
    // 采集到的秒数在会议进行中一直在涨：打开时现读一次
    setCurrent(speakers.find((s) => s.idx === idx) ?? null);
  }

  useEffect(() => {
    const request = ++generation.current;
    setCurrent(speaker);
    setProfiles([]);
    setMode("new");
    setName(speaker?.display_name ?? "");
    setProfileId(null);
    setConsent(false);
    setRenaming(null);
    setBusy(false);
    setError("");
    setNotice("");
    if (speaker) {
      void load(request).catch((e) => {
        if (request === generation.current) setError(errorMessage(e));
      });
    }
    // 只在换了会议或说话人时重置；列表里同一位说话人改了名不算
  }, [sessionId, idx]);

  /** 改动服务端的操作：同一时间只做一件，做完重新读取 */
  async function change(action: () => Promise<string | void>) {
    const request = ++generation.current;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const done = await action();
      if (request !== generation.current) return;
      if (done) setNotice(done);
      onChanged();
      await load(request);
    } catch (e) {
      if (request === generation.current) setError(errorMessage(e));
    } finally {
      if (request === generation.current) setBusy(false);
    }
  }

  const have = current?.voice_secs ?? 0;
  const linked = current?.voice_profile ?? null;
  const target =
    mode === "new"
      ? name.trim()
        ? { name: name.trim() }
        : null
      : profileId !== null
        ? { profileId }
        : null;
  const canEnroll = consent && target !== null && have >= need && !busy;

  return (
    <Dialog
      open={speaker !== null}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      {speaker && (
        <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>{t.voice.open(current?.display_name ?? speaker.display_name)}</DialogTitle>
            <DialogDescription>{t.voice.description}</DialogDescription>
          </DialogHeader>

          {linked ? (
            <section className="flex flex-wrap items-center gap-2 text-sm">
              <span>{t.voice.linked(linked.name)}</span>
              <Button
                variant="outline"
                size="sm"
                disabled={busy}
                title={t.voice.unlinkHint}
                onClick={() =>
                  void change(async () => {
                    await api.unlinkVoice(sessionId, speaker.idx);
                  })
                }
              >
                {t.voice.unlink}
              </Button>
            </section>
          ) : (
            <form
              className="flex flex-col gap-3 text-sm"
              onSubmit={(event) => {
                event.preventDefault();
                if (!canEnroll || target === null) return;
                void change(async () => {
                  await api.enrollVoice(sessionId, speaker.idx, target);
                });
              }}
            >
              <p className={have >= need ? "text-muted-foreground" : "text-warning-foreground"}>
                {t.voice.collected(have, need)}
              </p>
              <fieldset className="flex flex-col gap-2">
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="voice-target"
                    checked={mode === "new"}
                    onChange={() => setMode("new")}
                  />
                  {t.voice.newProfile}
                </label>
                {mode === "new" && (
                  <Input
                    className={INPUT}
                    aria-label={t.voice.profileName}
                    value={name}
                    maxLength={50}
                    onChange={(event) => setName(event.target.value)}
                  />
                )}
                {profiles.length > 0 && (
                  <label className="flex items-center gap-2">
                    <input
                      type="radio"
                      name="voice-target"
                      checked={mode === "existing"}
                      onChange={() => setMode("existing")}
                    />
                    {t.voice.existingProfile}
                  </label>
                )}
                {mode === "existing" && (
                  <select
                    className="h-8 rounded-md border border-input bg-background px-2 text-sm text-foreground dark:bg-input/30"
                    aria-label={t.voice.chooseProfile}
                    value={profileId ?? ""}
                    onChange={(event) =>
                      setProfileId(event.target.value ? Number(event.target.value) : null)
                    }
                  >
                    <option value="">{t.voice.chooseProfile}</option>
                    {profiles.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </select>
                )}
              </fieldset>
              <label className="flex items-start gap-2">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={consent}
                  onChange={(event) => setConsent(event.target.checked)}
                />
                {t.voice.consent}
              </label>
              <div>
                <Button type="submit" size="sm" disabled={!canEnroll}>
                  {t.voice.remember}
                </Button>
              </div>
            </form>
          )}

          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          {notice && (
            <p role="status" className="text-sm text-muted-foreground">
              {notice}
            </p>
          )}

          <section className="flex flex-col gap-2 border-t pt-3 text-sm">
            <h3 className="font-medium">{t.voice.profiles}</h3>
            {profiles.length === 0 ? (
              <p className="text-muted-foreground">{t.voice.noProfiles}</p>
            ) : (
              <ul className="flex flex-col gap-2">
                {profiles.map((p) => (
                  <li key={p.id} className="flex flex-wrap items-center gap-2">
                    {renaming?.id === p.id ? (
                      <form
                        className="flex flex-1 flex-wrap items-center gap-2"
                        onSubmit={(event) => {
                          event.preventDefault();
                          const next = renaming.name.trim();
                          setRenaming(null);
                          if (next && next !== p.name)
                            void change(async () => {
                              await api.renameVoiceProfile(p.id, next);
                            });
                        }}
                      >
                        <Input
                          className={`${INPUT} w-40`}
                          aria-label={t.voice.profileName}
                          value={renaming.name}
                          maxLength={50}
                          autoFocus
                          onChange={(event) => setRenaming({ id: p.id, name: event.target.value })}
                          onKeyDown={(event) => {
                            if (event.key === "Escape") {
                              event.stopPropagation();
                              setRenaming(null);
                            }
                          }}
                        />
                        <Button type="submit" size="sm" disabled={busy}>
                          {t.voice.save}
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() => setRenaming(null)}
                        >
                          {t.voice.cancel}
                        </Button>
                      </form>
                    ) : (
                      <>
                        <span className="flex-1">
                          {p.name}{" "}
                          <span className="text-xs text-muted-foreground">
                            {t.voice.profileLine(p.samples, p.secs)}
                          </span>
                        </span>
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={busy}
                          onClick={() => setRenaming({ id: p.id, name: p.name })}
                        >
                          {t.voice.rename}
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          className="text-destructive"
                          disabled={busy}
                          onClick={() => {
                            if (!window.confirm(t.voice.deleteConfirm(p.name))) return;
                            void change(async () => {
                              const done = await api.deleteVoiceProfile(p.id);
                              return t.voice.deleted(p.name, done.clips);
                            });
                          }}
                        >
                          {t.voice.delete}
                        </Button>
                      </>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>
        </DialogContent>
      )}
    </Dialog>
  );
}
