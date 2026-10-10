// 访问口令（docs/interfaces.md §5.7）。服务端没启用口令时这里什么也不做：状态接口报告 enabled=false，
// 不需要令牌。启用时：页面先问一次登录状态，没登录就显示登录页；登录后拿到的 CSRF 令牌记在 authSession 里，
// 所有改动数据的请求（包括 WebRTC 的 offer 和 ICE 候选）都带上它。任何接口回 401 时通知页面重新登录。

export const CSRF_HEADER = "X-CSRF-Token";

export interface AuthStatus {
  /** 服务端是否启用了访问口令 */
  enabled: boolean;
  /** 当前是否可以访问（没启用口令时总是 true） */
  authenticated: boolean;
  csrf_token: string | null;
}

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

export function parseStatus(body: unknown): AuthStatus {
  const value = (typeof body === "object" && body !== null ? body : {}) as Record<string, unknown>;
  const enabled = value.enabled === true;
  const token = typeof value.csrf_token === "string" && value.csrf_token ? value.csrf_token : null;
  return { enabled, authenticated: !enabled || value.authenticated === true, csrf_token: token };
}

/** 登录失败时给用户看的话。 */
export function loginErrorText(status: number, body: unknown, retryAfter: string | null): string {
  if (status === 429) {
    const secs = Number(retryAfter);
    return Number.isFinite(secs) && secs > 0
      ? `尝试次数过多，请 ${Math.ceil(secs / 60)} 分钟后再试`
      : "尝试次数过多，请稍后再试";
  }
  if (typeof body === "object" && body !== null && "error" in body) {
    const text = (body as { error: unknown }).error;
    if (typeof text === "string" && text) return text;
  }
  return status === 0 ? "无法连接到服务端" : `登录失败（${status}）`;
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
  if (!response.ok) throw new Error(`读取登录状态失败（${response.status}）`);
  const status = parseStatus(await readJson(response));
  session.setToken(status.csrf_token);
  return status;
}

export type LoginResult = { ok: true; status: AuthStatus } | { ok: false; message: string };

export async function login(
  password: string,
  fetchFn: FetchLike = defaultFetch,
  session: AuthSession = authSession,
): Promise<LoginResult> {
  let response: Response;
  try {
    response = await fetchFn("/api/auth/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ password }),
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
