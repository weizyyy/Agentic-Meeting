import { ExternalLinkIcon, LockKeyholeIcon, LogInIcon, MicIcon } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";

import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  fetchAuthStatus,
  login,
  ssoStartUrl,
  takeLoginError,
  type AuthStatus,
  type LoginMethods,
} from "../auth.ts";
import { useI18n } from "../i18n/index.ts";

interface Props {
  /** 登录已失效、浮在会议页面上方重新登录（会议连接不受影响） */
  overlay?: boolean;
  methods: LoginMethods;
  onLoggedIn: (status: AuthStatus) => void;
}

// 单点登录失败时服务端带着代码跳回来：页面打开时读一次（读完就从地址栏去掉）
let loginError: string | null | undefined;
function initialLoginError(): string | null {
  if (loginError === undefined) loginError = takeLoginError();
  const value = loginError;
  loginError = null;
  return value;
}

/** 登录页：访问口令和/或单点登录（docs/interfaces.md §5.7）。 */
export function LoginPage({ overlay = false, methods, onLoggedIn }: Props) {
  const { t } = useI18n();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(initialLoginError);
  const [busy, setBusy] = useState(false);
  const sso = methods.oidc;

  // 浮层里的单点登录在新标签页里进行（离开当前页面会断开会议）。回到这个标签页时再问一次登录状态。
  useEffect(() => {
    if (!overlay || sso.length === 0) return;
    const recheck = () => {
      if (document.visibilityState !== "visible") return;
      fetchAuthStatus()
        .then((status) => {
          if (status.authenticated) onLoggedIn(status);
        })
        .catch(() => {});
    };
    window.addEventListener("focus", recheck);
    document.addEventListener("visibilitychange", recheck);
    return () => {
      window.removeEventListener("focus", recheck);
      document.removeEventListener("visibilitychange", recheck);
    };
  }, [overlay, sso.length, onLoggedIn]);

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

  const here = window.location.pathname + window.location.search;
  const prompt = overlay
    ? methods.password
      ? t.login.expiredHint
      : t.login.expiredSsoHint
    : methods.password
      ? sso.length > 0
        ? t.login.promptEither
        : t.login.prompt
      : sso.length > 0
        ? t.login.promptSso
        : t.login.promptProxy;

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
            {overlay ? t.login.expired : t.common.appName}
          </h1>
          <p className="text-sm text-muted-foreground">{prompt}</p>
        </div>
        {sso.length > 0 && (
          <div className="flex flex-col gap-2">
            {sso.map((provider) => (
              <a
                key={provider.id}
                className={buttonVariants({ variant: "outline", size: "lg" })}
                href={ssoStartUrl(provider.id, here)}
                {...(overlay ? { target: "_blank", rel: "noopener" } : {})}
              >
                {overlay ? (
                  <ExternalLinkIcon aria-hidden data-icon="inline-start" />
                ) : (
                  <LogInIcon aria-hidden data-icon="inline-start" />
                )}
                {t.login.signInWith(provider.display_name)}
              </a>
            ))}
          </div>
        )}
        {methods.password && sso.length > 0 && (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span className="h-px flex-1 bg-border" />
            {t.login.or}
            <span className="h-px flex-1 bg-border" />
          </div>
        )}
        {methods.password && (
          <div className="flex flex-col gap-2">
            <Label htmlFor="login-password">{t.login.password}</Label>
            <Input
              id="login-password"
              type="password"
              autoComplete="current-password"
              autoFocus={sso.length === 0}
              value={password}
              aria-invalid={error !== null}
              aria-describedby={error ? "login-error" : undefined}
              onChange={(event) => setPassword(event.target.value)}
            />
          </div>
        )}
        {error && (
          <p id="login-error" className="text-sm text-destructive" role="alert">
            {error}
          </p>
        )}
        {methods.password && (
          <Button type="submit" disabled={busy || !password}>
            {busy ? t.login.busy : t.login.submit}
          </Button>
        )}
        {!methods.password && sso.length === 0 && (
          <Button type="button" onClick={() => window.location.reload()}>
            {t.login.reload}
          </Button>
        )}
      </form>
    </div>
  );
}
