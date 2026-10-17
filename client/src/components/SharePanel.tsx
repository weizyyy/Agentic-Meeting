import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n/index.ts";
import { shareRequest, type Share } from "../sharing.ts";
import { Button } from "./ui/button";
import { Input } from "./ui/input";

/** 主持人只管理当前会议的临时来源，凭据不做持久缓存。 */
export function SharePanel({ sessionId }: { sessionId: string }) {
  const { t } = useI18n();
  const [shares, setShares] = useState<Share[]>([]);
  const [password, setPassword] = useState("");
  const [ttl, setTtl] = useState(86400);
  const [questions, setQuestions] = useState(false);
  const [link, setLink] = useState("");
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const base = `/api/sessions/${encodeURIComponent(sessionId)}/shares`;
  useEffect(
    () => () => {
      generation.current += 1;
    },
    [base],
  );
  async function change(path: string, method: string, value?: unknown) {
    const request = ++generation.current;
    setBusy(true);
    setError("");
    setLink("");
    setCopied(false);
    try {
      const result = await shareRequest<{ share?: Share; token?: string }>(
        base + path,
        method,
        value,
      );
      if (request !== generation.current) return;
      if (result.share && result.token)
        setLink(`${location.origin}/#share=${sessionId}.${result.share.id}.${result.token}`);
      const listing = await shareRequest<{ items: Share[] }>(base);
      if (request === generation.current) setShares(listing.items);
    } catch (e) {
      if (request === generation.current) setError(String(e));
    } finally {
      if (request === generation.current) {
        setBusy(false);
        setPassword("");
      }
    }
  }
  return (
    <details
      onToggle={(event) => {
        const request = ++generation.current;
        setLink("");
        setPassword("");
        setCopied(false);
        setBusy(false);
        setError("");
        if (event.currentTarget.open) {
          void shareRequest<{ items: Share[] }>(base)
            .then((r) => {
              if (request === generation.current) setShares(r.items);
            })
            .catch((e: unknown) => {
              if (request === generation.current) setError(String(e));
            });
        }
      }}
    >
      <summary>{t.sharing.title}</summary>
      <p>{t.sharing.scope}</p>
      <p>{sessionId}</p>
      <label>
        {t.sharing.ttl}
        <select
          aria-label={t.sharing.ttl}
          value={ttl}
          onChange={(e) => setTtl(Number(e.target.value))}
        >
          <option value={3600}>{t.sharing.hour}</option>
          <option value={86400}>{t.sharing.day}</option>
          <option value={604800}>{t.sharing.week}</option>
        </select>
      </label>
      <label>
        <input
          type="checkbox"
          checked={questions}
          onChange={(e) => setQuestions(e.target.checked)}
        />
        {t.sharing.questions}
      </label>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void change("/password", "PUT", {
            password,
            expires_in: ttl,
            allow_questions: questions,
          });
        }}
      >
        <Input
          type="password"
          aria-label={t.sharing.password}
          placeholder={t.sharing.password}
          value={password}
          minLength={12}
          maxLength={128}
          required
          autoComplete="new-password"
          onChange={(e) => setPassword(e.target.value)}
        />
        <Button type="submit" disabled={busy}>
          {t.sharing.setPassword}
        </Button>
      </form>
      <Button
        disabled={busy}
        onClick={() =>
          void change("/links", "POST", { expires_in: ttl, allow_questions: questions })
        }
      >
        {t.sharing.createLink}
      </Button>
      {link && (
        <div>
          <p>{t.sharing.once}</p>
          <Input aria-label={t.sharing.copy} value={link} readOnly />
          <Button
            onClick={() => {
              const request = generation.current;
              void navigator.clipboard
                .writeText(link)
                .then(() => {
                  if (request === generation.current) setCopied(true);
                })
                .catch((e: unknown) => {
                  if (request === generation.current) setError(String(e));
                });
            }}
          >
            {copied ? t.sharing.copied : t.sharing.copy}
          </Button>
        </div>
      )}
      <ul>
        {shares.map((s) => (
          <li key={s.id}>
            {s.kind === "password" ? t.sharing.passwordEntry : t.sharing.open} · {t.sharing.expires}
            : {new Date(s.expires_at * 1000).toLocaleString()} · {t.login.watcher}
            {s.allow_questions ? ` · ${t.sharing.questions}` : ""}
            {s.kind === "link" && (
              <Button
                disabled={busy}
                onClick={() =>
                  void change(`/${s.id}/rotate`, "POST", {
                    expires_in: ttl,
                    allow_questions: questions,
                  })
                }
              >
                {t.sharing.rotate}
              </Button>
            )}
            <Button disabled={busy} onClick={() => void change(`/${s.id}`, "DELETE")}>
              {t.sharing.revoke}
            </Button>
          </li>
        ))}
      </ul>
      {error && <p role="alert">{error}</p>}
    </details>
  );
}
