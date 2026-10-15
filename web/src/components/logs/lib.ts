/** Log explorer ("Loglar", docs/design/logs.md section 6): shared API helper, URL state, formatting and query-text helpers.
 *  The query string is the single source of truth for every filter; facet clicks and "filter by this value" go through the
 *  server's /logs/rewrite so typing and clicking never disagree. */
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "../../api";
import type { Range } from "../ops/util";

/** one id per browser tab: the live-tail rate limit is per (user, tab), so two open tabs never throttle each other */
const TAB = (() => { try { const k = "wo_logs_tab"; let v = sessionStorage.getItem(k); if (!v) { v = Math.random().toString(36).slice(2, 12); sessionStorage.setItem(k, v); } return v; } catch { return Math.random().toString(36).slice(2, 12); } })();

/** a parse error or refusal from the API: {code, pos, len, tr, en} (400), {code, retry_ms} (429) */
export type LgErr = { status: number; code?: string; pos?: number; len?: number; tr?: string; en?: string; retry_ms?: number; message: string };

export function toErr(e: any): LgErr {
  if (e && typeof e === "object" && "status" in e && !(e instanceof ApiError)) return e as LgErr;
  const status = e instanceof ApiError ? e.status : 0; const message = String(e?.message ?? e ?? "");
  try { const d = JSON.parse(message); if (d && typeof d === "object") return { status, message, ...d }; } catch { /* plain text detail */ }
  return { status, message };
}
export const errText = (e: LgErr | null | undefined, lang: string): string => (!e ? "" : (lang === "tr" ? e.tr : e.en) || e.tr || e.en || e.code || e.message);

export function qs(p: Record<string, any>): string {
  const u = new URLSearchParams(); for (const [k, v] of Object.entries(p)) if (v !== undefined && v !== null && v !== "") u.set(k, String(v));
  const s = u.toString(); return s ? `?${s}` : "";
}
export async function lg<T = any>(path: string, p: Record<string, any> = {}, opts: { method?: string; body?: any } = {}): Promise<T> {
  try { return await api<T>(`/logs${path}${qs(p)}`, { ...opts, headers: { "X-Tab-Id": TAB } }); } catch (e) { throw toErr(e); }
}

export const rangeParams = (r: Range): Record<string, string> => ("last" in r ? { last: String(r.last) } : { start: r.start, end: r.end });
export const rangeKey = (r: Range) => ("last" in r ? `last=${r.last}` : `${r.start}|${r.end}`);
export const rangeSeconds = (r: Range) => ("last" in r ? r.last * 60 : Math.max(1, (new Date(r.end).getTime() - new Date(r.start).getTime()) / 1000));

export const LEVELS = ["CRITICAL", "ERROR", "WARN", "INFO", "DEBUG"] as const;
export const LV_COLOR: Record<string, string> = { CRITICAL: "var(--violet)", ERROR: "var(--red)", WARN: "var(--amber)", INFO: "var(--accent2)", DEBUG: "var(--dim)" };
export const LV_ALIAS: Record<string, string> = { CRITICAL: "critical", ERROR: "error", WARN: "warn", INFO: "info", DEBUG: "debug" };
export const FACETS = ["level", "service", "host", "env", "pool", "source"] as const;
/** columns the table can show; time, level and message are always on */
export const BASE_COLS = ["time", "level", "service", "host", "message"];
export const OPTIONAL_COLS = ["service", "host", "source", "env", "pool", "agent", "trace", "session"];

/** "dd.MM HH:mm:ss.SSS" in local time (thousands of lines share a second, so the milliseconds matter) */
export function tsLocal(iso: string, opts: { date?: boolean; ms?: boolean } = {}): string {
  const d = new Date(iso); if (Number.isNaN(d.getTime())) return iso;
  const p = (x: number, n = 2) => String(x).padStart(n, "0");
  const t = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}${opts.ms === false ? "" : `.${p(d.getMilliseconds(), 3)}`}`;
  return opts.date === false ? t : `${p(d.getDate())}.${p(d.getMonth() + 1)} ${t}`;
}
export function tsFull(iso: string): string {
  const d = new Date(iso); if (Number.isNaN(d.getTime())) return iso;
  const p = (x: number, n = 2) => String(x).padStart(n, "0");
  return `${p(d.getDate())}.${p(d.getMonth() + 1)}.${d.getFullYear()} ${tsLocal(iso, { date: false })}`;
}
export const tsUtc = (iso: string) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? iso : d.toISOString().replace("T", " ").replace("Z", " UTC"); };
export const fullN = (n: number | null | undefined, lang: string) => (n == null ? "–" : n.toLocaleString(lang === "tr" ? "tr-TR" : "en-US"));
/** an estimate keeps two or three significant digits: "≈ 12.400", never a fake-precise number */
export function roundEst(n: number): number { if (n < 100) return Math.round(n); const k = Math.pow(10, Math.floor(Math.log10(n)) - 2); return Math.round(n / k) * k; }
export function fmtSkew(s: number): string { const a = Math.abs(s); return a < 120 ? `${Math.round(a)} sn` : a < 7200 ? `${Math.round(a / 60)} dk` : a < 172800 ? `${(a / 3600).toFixed(1)} sa` : `${Math.round(a / 86400)} gün`; }

/** a value as query text: bare when it is a plain word, else quoted with \" and \\ escaped (a quoted * or ? is literal) */
const OPS = new Set(["OR", "AND", "NOT", "VEYA", "VE", "DEĞİL"]);
export function qv(v: string): string {
  return /^[\p{L}\p{N}_.@/][\p{L}\p{N}_.\-@/]*$/u.test(v) && !OPS.has(v) ? v : `"${v.replace(/(["\\])/g, "\\$1")}"`;
}

/** the operations card's scope and filters as query text: env:prod pool:x host:(a OR b) level>=warn service:y "text" */
export function opsToQuery(o: { env?: string; pool?: string; hosts?: string[]; sev?: string; service?: string; text?: string }): string {
  const out: string[] = [];
  if (o.env) out.push(`env:${qv(o.env)}`);
  if (o.pool) out.push(`pool:${qv(o.pool)}`);
  if (o.hosts?.length) out.push(o.hosts.length === 1 ? `host:${qv(o.hosts[0])}` : `host:(${o.hosts.map(qv).join(" OR ")})`);
  if (o.sev === "ERROR") out.push("level>=error"); else if (o.sev === "WARN") out.push("level>=warn");
  if (o.service) out.push(`service:${qv(o.service)}`);
  const tx = (o.text ?? "").trim(); if (tx) out.push(/[\s:()"\\]/.test(tx) || OPS.has(tx) ? `"${tx.replace(/(["\\])/g, "\\$1")}"` : tx);
  return out.join(" ");
}

export type LogsState = ReturnType<typeof useLogsState>;
/** URL state (shareable): q, last | start+end, live=1 (tail), group=1, cols=…, by=…, line=<id> */
export function useLogsState() {
  const [sp, setSp] = useSearchParams();
  const range: Range = sp.get("start") ? { start: sp.get("start")!, end: sp.get("end") || new Date().toISOString() } : { last: Math.max(1, Number(sp.get("last") || 15)) };
  const q = sp.get("q") ?? "";
  const tail = sp.get("live") === "1";
  const group = sp.get("group") === "1" && !tail;
  const by = sp.get("by") || "level";
  const line = sp.get("line") || "";
  const colsUrl = sp.get("cols");
  const cols = (colsUrl ? colsUrl.split(",") : storedCols()).filter(Boolean);
  const set = (patch: Record<string, string | null>, push = false) => setSp((cur) => { const n = new URLSearchParams(cur); for (const [k, v] of Object.entries(patch)) { if (v) n.set(k, v); else n.delete(k); } return n; }, { replace: !push });
  return {
    range, q, tail, group, by, line, cols, set,
    setQ: (v: string) => set({ q: v || null, line: null }, true),
    setLast: (m: number) => set({ last: String(m), start: null, end: null, live: null }),
    setAbs: (s: Date, e: Date) => set({ start: s.toISOString(), end: e.toISOString(), last: null, live: null }, true),
    setTail: (on: boolean) => set(on ? { live: "1", last: "15", start: null, end: null, group: null } : { live: null }),
    setGroup: (on: boolean) => set({ group: on ? "1" : null, live: null }),
    setBy: (b: string) => set({ by: b === "level" ? null : b }),
    setLine: (id: string | null) => set({ line: id }),
    setCols: (c: string[]) => { try { localStorage.setItem("wo_logs_cols", JSON.stringify(c)); } catch { /* private mode */ } set({ cols: c.join(",") }); },
  };
}
function storedCols(): string[] { try { const v = JSON.parse(localStorage.getItem("wo_logs_cols") || "null"); if (Array.isArray(v) && v.length) return v; } catch { /* ignore */ } return BASE_COLS; }

export async function copyText(s: string): Promise<boolean> {
  try { await navigator.clipboard.writeText(s); return true; } catch { /* insecure context: fall back */ }
  try { const ta = document.createElement("textarea"); ta.value = s; ta.style.position = "fixed"; ta.style.opacity = "0"; document.body.appendChild(ta); ta.select(); const ok = document.execCommand("copy"); ta.remove(); return ok; } catch { return false; }
}
