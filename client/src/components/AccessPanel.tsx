import { useEffect, useState } from "react";
import { createApi, type MeetingMember, type SessionSummary } from "../api.ts";
import type { AuthStatus } from "../auth.ts";
import { useI18n } from "../i18n/index.ts";
import { Button } from "./ui/button";
import { AccessLog } from "./AccessLog.tsx";
import { SharePanel } from "./SharePanel.tsx";
import { Input } from "./ui/input";

const api = createApi();

/** 会议主持人按明确主体授权，不提供站点账号枚举。 */
export function AccessPanel({
  auth,
  session,
}: {
  auth: AuthStatus;
  session: SessionSummary | null;
}) {
  const { t } = useI18n();
  const [members, setMembers] = useState<MeetingMember[]>([]);
  const [principal, setPrincipal] = useState("");
  const [role, setRole] = useState<"host" | "watcher">("watcher");
  const [questions, setQuestions] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const sid = session?.id;
  const host = session?.access?.role === "host";
  useEffect(() => {
    let cancelled = false;
    setMembers([]);
    setError("");
    if (sid && host)
      void api
        .listMembers(sid)
        .then((items) => {
          if (!cancelled) setMembers(items);
        })
        .catch((e) => {
          if (!cancelled) setError(String(e));
        });
    return () => {
      cancelled = true;
    };
  }, [sid, host]);
  async function change(id: string, next: "host" | "watcher" | null) {
    if (!sid) return;
    setBusy(true);
    setError("");
    try {
      setMembers(await api.setMember(sid, id, next, questions));
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="border-b p-3 text-sm">
      <p>
        {t.login.identity}: {auth.principal?.guest ? t.sharing.guest : auth.principal?.display_name}{" "}
        ({auth.principal?.id})
      </p>
      {session?.access?.share_expires_at && (
        <p>
          {t.sharing.scope} {t.sharing.expires}:{" "}
          {new Date(session.access.share_expires_at * 1000).toLocaleString()}
        </p>
      )}
      {session && <p>{session.access?.tool_enabled ? t.login.toolOwner : t.login.readOnly}</p>}
      {host && (
        <details>
          <summary>{t.login.access}</summary>
          <ul>
            {members.map((member) => (
              <li key={member.principal_id}>
                {member.display_name} ({member.principal_id}) ·{" "}
                {member.role === "host" ? t.login.host : t.login.watcher}{" "}
                <Button
                  size="sm"
                  disabled={busy}
                  onClick={() => void change(member.principal_id, null)}
                >
                  {t.login.revoke}
                </Button>
              </li>
            ))}
          </ul>
          <form
            className="flex flex-wrap items-center gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              void change(principal, role);
            }}
          >
            <Input
              aria-label={t.login.principalId}
              placeholder={t.login.principalId}
              value={principal}
              onChange={(e) => setPrincipal(e.target.value)}
              required
            />
            <select
              aria-label={t.login.access}
              value={role}
              onChange={(e) => setRole(e.target.value as "host" | "watcher")}
            >
              <option value="watcher">{t.login.watcher}</option>
              <option value="host">{t.login.host}</option>
            </select>
            <label>
              <input
                type="checkbox"
                checked={questions}
                onChange={(e) => setQuestions(e.target.checked)}
              />
              {t.login.questions}
            </label>
            <Button type="submit" disabled={busy || !principal}>
              {t.login.grant}
            </Button>
          </form>
        </details>
      )}
      {host && sid && <SharePanel key={sid} sessionId={sid} />}
      {host && sid && <AccessLog key={sid} sessionId={sid} />}
      {error && (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      )}
    </section>
  );
}
