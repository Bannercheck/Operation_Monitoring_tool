/** Shared state and formatting for the operations screen: one time range and one scope drive every card, both kept in the URL
 *  so a view can be shared as a link ("look at prd-sap-app between 09:40 and 10:05"). */
import { useSearchParams } from "react-router-dom";

export type Range = { last: number } | { start: string; end: string };
export const QUICK = [15, 60, 360, 1440, 10080];

export function useOpsState() {
  const [sp, setSp] = useSearchParams();
  const range: Range = sp.get("start") ? { start: sp.get("start")!, end: sp.get("end") || new Date().toISOString() } : { last: Number(sp.get("last") || 15) };
  const env = sp.get("env") || ""; const pool = sp.get("pool") || ""; const hosts = (sp.get("host") || "").split(",").filter(Boolean);
  const set = (patch: Record<string, string | null>) => setSp((cur) => { const n = new URLSearchParams(cur); for (const [k, v] of Object.entries(patch)) { if (v) n.set(k, v); else n.delete(k); } return n; }, { replace: true });
  const live = "last" in range;
  const rq = () => { const p = new URLSearchParams(); if ("last" in range) p.set("last", String(range.last)); else { p.set("start", range.start); p.set("end", range.end); } if (env) p.set("env", env); if (pool) p.set("pool", pool); if (hosts.length) p.set("host", hosts.join(",")); return p.toString(); };
  return {
    range, env, pool, hosts, live, rq,
    setLast: (m: number) => set({ last: String(m), start: null, end: null }),
    setAbs: (s: Date, e: Date) => set({ start: s.toISOString(), end: e.toISOString(), last: null }),
    setEnv: (v: string) => set({ env: v || null }), setPool: (v: string) => set({ pool: v || null, host: null }),
    toggleHost: (h: string) => { const n = hosts.includes(h) ? hosts.filter((x) => x !== h) : [...hosts, h]; set({ host: n.join(",") || null }); },
    clearScope: () => set({ env: null, pool: null, host: null }),
  };
}

export function fmtN(v?: number | null, lang = "tr"): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  const loc = lang === "tr" ? "tr-TR" : "en-US"; const a = Math.abs(v);
  if (a >= 1e6) return `${(v / 1e6).toLocaleString(loc, { maximumFractionDigits: 1 })} ${lang === "tr" ? "mn" : "M"}`;
  if (a >= 1e4) return `${(v / 1e3).toLocaleString(loc, { maximumFractionDigits: 1 })} ${lang === "tr" ? "bin" : "k"}`;
  return v.toLocaleString(loc, { maximumFractionDigits: a < 10 ? 1 : 0 });
}
export const fmtPct = (v?: number | null, d = 2) => (v === null || v === undefined ? "–" : `%${(v * 100).toFixed(d)}`);
export function fmtTime(ts: number | string, spanMin: number): string {
  const d = new Date(typeof ts === "number" ? ts * 1000 : ts); const p = (x: number) => String(x).padStart(2, "0");
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
  return spanMin > 1440 ? `${p(d.getDate())}.${p(d.getMonth() + 1)} ${hm}` : hm;
}
export function rangeLabel(r: Range, lang: string): string {
  if ("last" in r) return lang === "tr" ? `Son ${r.last < 60 ? `${r.last} dk` : r.last < 1440 ? `${r.last / 60} sa` : `${r.last / 1440} gün`}` : `Last ${r.last < 60 ? `${r.last}m` : r.last < 1440 ? `${r.last / 60}h` : `${r.last / 1440}d`}`;
  const s = new Date(r.start), e = new Date(r.end); const p = (x: number) => String(x).padStart(2, "0");
  const d = (x: Date) => `${p(x.getDate())}.${p(x.getMonth() + 1)} ${p(x.getHours())}:${p(x.getMinutes())}`;
  return `${d(s)} → ${s.toDateString() === e.toDateString() ? `${p(e.getHours())}:${p(e.getMinutes())}` : d(e)}`;
}
/** delta of a number against the previous range: {text, dir} where dir is "up" / "down" / "" */
export function delta(cur?: number | null, prev?: number | null): { text: string; dir: string } {
  if (cur === null || cur === undefined || prev === null || prev === undefined || prev === 0) return { text: "", dir: "" };
  const d = (cur - prev) / prev; if (Math.abs(d) < 0.02) return { text: "≈", dir: "" };
  return { text: `${d > 0 ? "▲" : "▼"} %${Math.abs(d * 100).toFixed(Math.abs(d) < 0.1 ? 1 : 0)}`, dir: d > 0 ? "up" : "down" };
}
export const SEVC = { err: "var(--red)", warn: "var(--amber)", info: "var(--sev-info)" };
export function hostState(h: any): "silent" | "crit" | "warn" | "ok" { return h.silent ? "silent" : h.errors && h.error_rate >= 0.05 ? "crit" : h.errors && h.error_rate >= 0.005 ? "warn" : "ok"; }
