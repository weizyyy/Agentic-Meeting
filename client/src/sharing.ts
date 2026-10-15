import { authSession, parseStatus, withCsrf, type AuthStatus } from "./auth.ts";
import { errorText, messages } from "./i18n/index.ts";

export type Share = {
  id: string;
  kind: "link" | "password";
  expires_at: number;
  allow_questions: boolean;
};
export type ShareEntry = { session_id: string; share_id?: string; token?: string };

/** fragment从不进入HTTP请求目标；应用认证/API副作用之前清地址。 */
export function readShareEntry(): ShareEntry | null {
  const fragment = location.hash;
  if (!fragment.startsWith("#share=") && !fragment.startsWith("#meeting=")) return null;
  history.replaceState(null, "", location.pathname + location.search);
  const value = fragment.slice(fragment.indexOf("=") + 1).split(".");
  if (fragment.startsWith("#meeting=") && /^[a-f0-9]{32}$/.test(value[0]))
    return { session_id: value[0] };
  if (
    value.length === 3 &&
    value.slice(0, 2).every((s) => /^[a-f0-9]{32}$/.test(s)) &&
    /^[A-Za-z0-9_-]{43}$/.test(value[2])
  )
    return { session_id: value[0], share_id: value[1], token: value[2] };
  return { session_id: "" };
}

export async function shareRequest<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(
    path,
    withCsrf(
      {
        method,
        headers: { "Content-Type": "application/json" },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      },
      authSession.csrfToken,
    ),
  );
  const data = await response.json();
  if (response.status === 401) authSession.notifyUnauthorized();
  if (!response.ok) throw new Error(errorText(data) ?? messages().notices.unreachable);
  return data as T;
}

export async function exchangeShare(
  body: unknown,
): Promise<{ auth: AuthStatus; session_id: string }> {
  const result = await shareRequest<{ auth: unknown; session_id: string }>(
    "/api/shares/exchange",
    "POST",
    body,
  );
  const auth = parseStatus(result.auth);
  authSession.setToken(auth.csrf_token);
  return { auth, session_id: result.session_id };
}
