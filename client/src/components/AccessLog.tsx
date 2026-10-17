import { useState } from "react";
import { createApi, type AccessLogEntry } from "../api.ts";
import { useI18n } from "../i18n/index.ts";
import { Button } from "./ui/button";

const api = createApi();

/** 记录里值得直接显示的对象：成员、导出格式或产出文件名 */
function target(entry: AccessLogEntry): string {
  const { principal_id, format, name } = entry.detail;
  const value = principal_id ?? format ?? name;
  return typeof value === "string" ? value : "";
}

/** 主持人查看谁打开、导出过会议，谁改过权限和分享。展开时才读取，读取本身不记一条。 */
export function AccessLog({ sessionId }: { sessionId: string }) {
  const { t } = useI18n();
  const [items, setItems] = useState<AccessLogEntry[] | null>(null);
  const [more, setMore] = useState(false);
  const [error, setError] = useState("");
  async function load(beforeId?: number) {
    setError("");
    try {
      const page = await api.accessLog(sessionId, beforeId);
      setItems((current) => (beforeId === undefined ? page : [...(current ?? []), ...page]));
      setMore(page.length === 100);
    } catch (e) {
      setError(String(e));
    }
  }
  return (
    <details
      onToggle={(e) => {
        if (e.currentTarget.open) void load();
      }}
    >
      <summary>{t.login.accessLog}</summary>
      {items?.length === 0 && <p>{t.login.logEmpty}</p>}
      <ul>
        {items?.map((entry) => (
          <li key={entry.id}>
            {new Date(entry.at * 1000).toLocaleString()} · {entry.display_name} (
            {entry.principal_id}) ·{" "}
            {(t.login.logActions as Record<string, string>)[entry.action] ?? entry.action}
            {target(entry) && ` · ${target(entry)}`}
          </li>
        ))}
      </ul>
      {more && items && (
        <Button size="sm" onClick={() => void load(items[items.length - 1].id)}>
          {t.login.logMore}
        </Button>
      )}
      {error && (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      )}
    </details>
  );
}
