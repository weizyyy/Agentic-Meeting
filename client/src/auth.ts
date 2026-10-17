// 访问口令与单点登录（docs/interfaces.md §5.7）。服务端不需要登录时这里什么也不做：状态接口报告 enabled=false，
// 不需要令牌。启用时：页面先问一次登录状态，没登录就显示登录页（口令框和/或单点登录按钮）；登录后拿到的 CSRF 令牌记在 authSession 里，
// 所有改动数据的请求（包括 WebRTC 的 offer 和 ICE 候选）都带上它。任何接口回 401 时通知页面重新登录。

import { errorText, messages } from "./i18n/index.ts";

export const CSRF_HEADER = "X-CSRF-Token";

export interface SsoProvider {
  id: string;
  display_name: string;
}

/** 登录页能提供的登录方式 */
export interface LoginMethods {
  password: boolean;
  oidc: SsoProvider[];
  /** 由反向代理认证：页面上没有可点的，重新打开页面即可 */
  proxy: boolean;
}

export interface AuthStatus {
  /** 服务端是否需要登录 */
  enabled: boolean;
  meeting_access?: boolean;
  site_password_required?: boolean;
  principal?: { id: string; display_name: string; can_host: boolean; guest?: boolean };
  /** 当前是否可以访问（不需要登录时总是 true） */
  authenticated: boolean;
  csrf_token: string | null;
  methods: LoginMethods;
}

export const NO_METHODS: LoginMethods = { password: false, oidc: [], proxy: false };

/** 当前页面的登录状态：令牌，以及「会话失效了」的监听者。 */
export class AuthSession {
  private token: string | null = null;
  private readonly listeners = new Set<() => void>();

  get csrfToken(): string | null {
    return this.token;
  }

  setToken(token: string | null): void {
    this.token = token;
  }

  /** 订阅「接口回了 401」；返回取消订阅的函数。 */
  onUnauthorized(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  notifyUnauthorized(): void {
    this.token = null;
    for (const listener of [...this.listeners]) listener();
  }

  /** 交给 WebRTC SDK 的请求头（offer 和 ICE 候选都是改动性的请求）。 */
  headers(): Headers {
    const headers = new Headers();
    if (this.token) headers.set(CSRF_HEADER, this.token);
    return headers;
  }
}

export const authSession = new AuthSession();

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

/** 改动性的请求带上令牌；读取不带。原来的请求头保留。 */
export function withCsrf(
  init: RequestInit | undefined,
  token: string | null,
): RequestInit | undefined {
  const method = (init?.method ?? "GET").toUpperCase();
  if (!token || SAFE_METHODS.has(method)) return init;
  const headers = new Headers(init?.headers);
  headers.set(CSRF_HEADER, token);
  return { ...init, headers };
}

function parseMethods(value: unknown, enabled: boolean): LoginMethods {
  // 没有 methods 字段的服务端只有访问口令
  if (typeof value !== "object" || value === null) return { ...NO_METHODS, password: enabled };
  const raw = value as Record<string, unknown>;
  const oidc = Array.isArray(raw.oidc)
    ? raw.oidc.flatMap((item: unknown) => {
        const p = (typeof item === "object" && item !== null ? item : {}) as Record<
          string,
          unknown
        >;
        return typeof p.id === "string" && typeof p.display_name === "string"
          ? [{ id: p.id, display_name: p.display_name }]
          : [];
      })
    : [];
  return { password: raw.password === true, oidc, proxy: raw.proxy === true };
}

export function parseStatus(body: unknown): AuthStatus {
  const value = (typeof body === "object" && body !== null ? body : {}) as Record<string, unknown>;
  if (typeof value.enabled !== "boolean" || typeof value.authenticated !== "boolean")
    throw new Error(messages().login.statusFailed(0));
  const enabled = value.enabled;
  const token = typeof value.csrf_token === "string" && value.csrf_token ? value.csrf_token : null;
  return {
    enabled,
    authenticated: !enabled || value.authenticated === true,
    csrf_token: token,
    methods: parseMethods(value.methods, enabled),
    meeting_access: value.meeting_access === true,
    site_password_required: value.site_password_required === true,
    principal: value.principal as AuthStatus["principal"],
  };
}

/** 单点登录的起点：登录完回到 ``next``（本站路径，默认当前页面）。 */
export function ssoStartUrl(provider: string, next: string): string {
  return `/api/auth/oidc/${encodeURIComponent(provider)}/start?next=${encodeURIComponent(next)}`;
}

const LOGIN_ERROR_KEY = "login_error";

/**
 * 单点登录失败时，服务端跳回页面并把代码放在地址的 # 片段里（login_error=sso.denied 等）。
 * 读出来翻译成提示，并从地址栏去掉，刷新页面不会再出现。没有就返回 null。
 */
export function takeLoginError(win: Window = window): string | null {
  const hash = win.location.hash.replace(/^#/, "");
  if (!hash) return null;
  const params = new URLSearchParams(hash);
  const code = params.get(LOGIN_ERROR_KEY);
  if (code === null) return null;
  params.delete(LOGIN_ERROR_KEY);
  const rest = params.toString();
  const url = win.location.pathname + win.location.search + (rest ? `#${rest}` : "");
  win.history.replaceState(win.history.state, "", url);
  return errorText({ code, params: {} }) ?? messages().login.ssoFailed;
}

/** 登录失败时给用户看的话。 */
export function loginErrorText(status: number, body: unknown, retryAfter: string | null): string {
  if (status === 429) {
    const secs = Number(retryAfter);
    return Number.isFinite(secs) && secs > 0
      ? messages().login.tooManyIn(Math.ceil(secs / 60))
      : messages().login.tooMany;
  }
  const text = errorText(body);
  if (text !== null) return text;
  return status === 0 ? messages().notices.unreachable : messages().login.failed(status);
}

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

const defaultFetch: FetchLike = (input, init) => fetch(input, init);

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

/** 问服务端当前的登录状态。连不上时抛错，由调用方决定怎么办。 */
export async function fetchAuthStatus(
  fetchFn: FetchLike = defaultFetch,
  session: AuthSession = authSession,
): Promise<AuthStatus> {
  const response = await fetchFn("/api/auth");
  if (!response.ok) throw new Error(messages().login.statusFailed(response.status));
  const status = parseStatus(await readJson(response));
  session.setToken(status.csrf_token);
  return status;
}

export type LoginResult = { ok: true; status: AuthStatus } | { ok: false; message: string };

export async function login(
  password: string | { password: string; user_id: string; site_password: string },
  fetchFn: FetchLike = defaultFetch,
  session: AuthSession = authSession,
): Promise<LoginResult> {
  let response: Response;
  try {
    response = await fetchFn("/api/auth/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(typeof password === "string" ? { password } : password),
    });
  } catch {
    return { ok: false, message: loginErrorText(0, null, null) };
  }
  const body = await readJson(response);
  if (!response.ok) {
    return {
      ok: false,
      message: loginErrorText(response.status, body, response.headers.get("retry-after")),
    };
  }
  const status = parseStatus(body);
  session.setToken(status.csrf_token);
  return { ok: true, status };
}

/** 退出登录。不管服务端怎么回，本页都当作已经退出。 */
export async function logout(
  fetchFn: FetchLike = defaultFetch,
  session: AuthSession = authSession,
): Promise<void> {
  try {
    await fetchFn("/api/auth/logout", withCsrf({ method: "POST" }, session.csrfToken));
  } catch {
    // 连不上也照样清掉本地状态
  }
  session.setToken(null);
}
