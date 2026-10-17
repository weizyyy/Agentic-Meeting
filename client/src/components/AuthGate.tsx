import { useCallback, useEffect, useState } from "react";

import { App } from "../App.tsx";
import { authSession, fetchAuthStatus, logout, type AuthStatus } from "../auth.ts";
import { useI18n } from "../i18n/index.ts";
import { ShareEntry } from "./ShareEntry.tsx";
import { readShareEntry } from "../sharing.ts";
import { LoginPage } from "./LoginPage.tsx";
import { Button } from "./ui/button";

/** 只靠反向代理登录时退出没有意义：代理下一个请求又会带上身份。 */
function canLogOut(status: AuthStatus): boolean {
  return status.enabled && (status.methods.password || status.methods.oidc.length > 0);
}

/** 登录失效时卸载会议页面，清除旧内容并关闭媒体；状态未知时只显示重试。 */
export function AuthGate() {
  const { t } = useI18n();
  const [entry, setEntry] = useState(readShareEntry);
  const [opened, setOpened] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const change = () => setEntry(readShareEntry());
    addEventListener("hashchange", change);
    return () => removeEventListener("hashchange", change);
  }, []);
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setError(false);
    void fetchAuthStatus()
      .then((value) => {
        if (!cancelled) setStatus(value);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);
  useEffect(
    () =>
      authSession.onUnauthorized(() =>
        setStatus((current) =>
          current
            ? { ...current, authenticated: false, csrf_token: null, principal: undefined }
            : null,
        ),
      ),
    [],
  );
  const onLogout = useCallback(async () => {
    setStatus((current) =>
      current ? { ...current, authenticated: false, csrf_token: null, principal: undefined } : null,
    );
    await logout();
  }, []);
  if (error)
    return (
      <div role="alert">
        {t.login.statusUnavailable}
        <Button onClick={() => setAttempt((x) => x + 1)}>{t.login.retry}</Button>
      </div>
    );
  if (status === null) return null;
  if (entry && status.meeting_access)
    return (
      <ShareEntry
        key={entry.token ?? entry.session_id}
        entry={entry}
        status={status}
        onClose={() => setEntry(null)}
        onOpened={(auth, id) => {
          setStatus(auth);
          setOpened(id);
          setVersion((v) => v + 1);
          setEntry(null);
        }}
      />
    );
  return (
    <>
      {status.meeting_access && (
        <Button onClick={() => setEntry({ session_id: "" })}>{t.sharing.open}</Button>
      )}
      {status.enabled && !status.authenticated ? (
        <LoginPage
          status={status}
          onLoggedIn={(auth) => {
            setStatus(auth);
            setOpened(null);
            setVersion((v) => v + 1);
          }}
        />
      ) : (
        <App
          key={version}
          initialSessionId={opened}
          auth={status}
          onLogout={canLogOut(status) ? () => void onLogout() : undefined}
        />
      )}
    </>
  );
}
