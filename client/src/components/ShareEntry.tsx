import { useState } from "react";
import { type AuthStatus } from "../auth.ts";
import { useI18n } from "../i18n/index.ts";
import { exchangeShare, type ShareEntry as Entry } from "../sharing.ts";
import { Button } from "./ui/button";
import { Input } from "./ui/input";

export function ShareEntry({
  entry,
  status,
  onOpened,
  onClose,
}: {
  entry: Entry;
  status: AuthStatus;
  onOpened: (auth: AuthStatus, sessionId: string) => void;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const [sid, setSid] = useState(entry.session_id);
  const [password, setPassword] = useState("");
  const [site, setSite] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <section className="p-4">
      <h2>{t.sharing.open}</h2>
      <p>{t.sharing.scope}</p>
      <p>
        {t.sharing.identity}:{" "}
        {status.principal?.guest
          ? t.sharing.guest
          : (status.principal?.display_name ?? t.sharing.guest)}
      </p>
      <p>{t.sharing.personal}</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setBusy(true);
          setError("");
          void exchangeShare({
            session_id: sid,
            ...(entry.token
              ? { kind: "link", share_id: entry.share_id, token: entry.token }
              : { kind: "password", password }),
            ...(status.site_password_required ? { site_password: site } : {}),
          })
            .then((r) => onOpened(r.auth, r.session_id))
            .catch((e: unknown) => setError(String(e)))
            .finally(() => {
              setBusy(false);
              setPassword("");
              setSite("");
            });
        }}
      >
        <Input
          aria-label={t.sharing.meeting}
          placeholder={t.sharing.meeting}
          value={sid}
          readOnly={!!entry.token}
          required
          onChange={(e) => setSid(e.target.value)}
        />
        {!entry.token && (
          <Input
            type="password"
            aria-label={t.sharing.password}
            placeholder={t.sharing.password}
            value={password}
            required
            maxLength={128}
            autoComplete="off"
            onChange={(e) => setPassword(e.target.value)}
          />
        )}
        {status.site_password_required && (
          <Input
            type="password"
            aria-label={t.sharing.site}
            placeholder={t.sharing.site}
            required
            value={site}
            autoComplete="off"
            onChange={(e) => setSite(e.target.value)}
          />
        )}
        <Button type="submit" disabled={busy}>
          {t.sharing.enter}
        </Button>
        <Button type="button" onClick={onClose}>
          {t.sharing.close}
        </Button>
      </form>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
