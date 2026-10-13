import { LockKeyholeIcon, MicIcon } from "lucide-react";
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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
    <div
      className={`flex min-h-full items-center justify-center p-4 ${
        overlay
          ? "fixed inset-0 z-[100] bg-background/85 supports-backdrop-filter:backdrop-blur-xs"
          : ""
      }`}
    >
      <form
        className="flex w-full max-w-sm flex-col gap-4 rounded-xl border bg-card p-6 shadow-sm"
        onSubmit={(event) => void submit(event)}
      >
        <div className="flex flex-col gap-1.5">
          <h1 className="flex items-center gap-2 text-xl font-semibold">
            {overlay ? (
              <LockKeyholeIcon aria-hidden className="size-5" />
            ) : (
              <MicIcon aria-hidden className="size-5" />
            )}
            {overlay ? "登录已过期" : "组会助理"}
          </h1>
          <p className="text-sm text-muted-foreground">
            {overlay ? "请重新输入访问口令。会议不受影响。" : "请输入访问口令。"}
          </p>
        </div>
        <div className="flex flex-col gap-2">
          <Label htmlFor="login-password">访问口令</Label>
          <Input
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
            <p id="login-error" className="text-sm text-destructive" role="alert">
              {error}
            </p>
          )}
        </div>
        <Button type="submit" disabled={busy || !password}>
          {busy ? "登录中…" : "登录"}
        </Button>
      </form>
    </div>
  );
}
