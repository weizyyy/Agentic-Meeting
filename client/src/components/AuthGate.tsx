import { useCallback, useEffect, useState } from "react";

import { App } from "../App.tsx";
import { authSession, fetchAuthStatus, logout, type AuthStatus } from "../auth.ts";
import { LoginPage } from "./LoginPage.tsx";

/** 没有启用口令时的状态；读不到登录状态时也按它处理，交给页面自己的请求去报错。 */
const OPEN: AuthStatus = { enabled: false, authenticated: true, csrf_token: null };

/**
 * 页面入口：先问服务端要不要登录（docs/interfaces.md §5.7）。
 * 没登录就只显示登录页，会议页面不加载；登录过的页面上任何接口回 401 时，在会议页面上方盖一层登录框，
 * 不卸载会议页面，正在进行的会议连接不受影响。
 */
export function AuthGate() {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [expired, setExpired] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchAuthStatus()
      .catch(() => OPEN)
      .then((value) => {
        if (!cancelled) setStatus(value);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => authSession.onUnauthorized(() => setExpired(true)), []);

  const onLogout = useCallback(async () => {
    await logout();
    setExpired(false);
    setStatus((current) =>
      current ? { ...current, authenticated: false, csrf_token: null } : current,
    );
  }, []);

  if (status === null) return null; // 只等一个很快的请求，不闪加载提示
  if (status.enabled && !status.authenticated) {
    return <LoginPage onLoggedIn={setStatus} />;
  }
  return (
    <>
      <App onLogout={status.enabled ? () => void onLogout() : undefined} />
      {expired && (
        <LoginPage
          overlay
          onLoggedIn={(value) => {
            setStatus(value);
            setExpired(false);
          }}
        />
      )}
    </>
  );
}
