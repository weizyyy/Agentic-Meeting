import { useCallback, useEffect, useState } from "react";

import { App } from "../App.tsx";
import { authSession, fetchAuthStatus, logout, type AuthStatus } from "../auth.ts";
import { useI18n } from "../i18n/index.ts";
import { LoginPage } from "./LoginPage.tsx";
import { Button } from "./ui/button";

/** 登录失效时卸载会议页面，清除旧内容并关闭媒体；状态未知时只显示重试。 */
export function AuthGate() {
  const { t } = useI18n();
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
          current ? { ...current, authenticated: false, csrf_token: null } : null,
        ),
      ),
    [],
  );
  const onLogout = useCallback(async () => {
    setStatus((current) =>
      current ? { ...current, authenticated: false, csrf_token: null } : null,
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
  if (status.enabled && !status.authenticated)
    return <LoginPage status={status} onLoggedIn={setStatus} />;
  return <App auth={status} onLogout={status.enabled ? () => void onLogout() : undefined} />;
}
