import { useState, type FormEvent } from "react";

import { login, type AuthStatus } from "../auth.ts";

interface Props {
  /** 登录已失效、浮在会议页面上方重新登录（会议连接不受影响） */
  overlay?: boolean;
  onLoggedIn: (status: AuthStatus) => void;
}

/** 访问口令的登录页（docs/interfaces.md §5.7）。 */
export function LoginPage({ overlay = false, onLoggedIn }: Props) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!password || busy) return;
    setBusy(true);
    setError(null);
    const result = await login(password);
    setBusy(false);
    if (result.ok) {
      setPassword("");
      onLoggedIn(result.status);
    } else {
      setError(result.message);
    }
  };

  return (
    <div className={overlay ? "login login-overlay" : "login"}>
      <form className="login-card" onSubmit={(event) => void submit(event)}>
        <h1>组会助理</h1>
        <p className="login-hint">
          {overlay ? "登录已过期，请重新输入访问口令。会议不受影响。" : "请输入访问口令。"}
        </p>
        <label htmlFor="login-password">访问口令</label>
        <input
          id="login-password"
          type="password"
          autoComplete="current-password"
          autoFocus
          value={password}
          aria-invalid={error !== null}
          aria-describedby={error ? "login-error" : undefined}
          onChange={(event) => setPassword(event.target.value)}
        />
        {error && (
          <p id="login-error" className="login-error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" className="button button-start" disabled={busy || !password}>
          {busy ? "登录中…" : "登录"}
        </button>
      </form>
    </div>
  );
}
