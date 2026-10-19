import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { get } from "../../api";
import { useT } from "../../i18n";

/** Shared bits of the APM pages: the scope (live feed or an uploaded dataset, time range, environment) kept in the URL so a
 *  link to a service or a trace opens the same view, number formatting and a stable colour per service. */
export const TIP = { contentStyle: { background: "var(--bg2)", border: "1px solid var(--line2)", borderRadius: 10, fontSize: 12, color: "var(--fg)" } };
export const RANGES = [15, 60, 360, 1440, 10080];
const PAL = ["#2dd4bf", "#60a5fa", "#a78bfa", "#fbbf24", "#93c5fd", "#34d399", "#fb923c", "#22d3ee", "#c084fc", "#facc15", "#4ade80", "#5eead4"];   // no red: red means failure

export function svcColor(name: string): string {
  let h = 0;
  for (const c of name || "") h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return PAL[h % PAL.length];
}

export function fmtMs(v?: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  if (v >= 60000) return `${(v / 60000).toFixed(1)} dk`;
  if (v >= 1000) return `${(v / 1000).toFixed(v >= 10000 ? 0 : 2)} s`;
  if (v >= 10) return `${Math.round(v)} ms`;
  return `${v.toFixed(1)} ms`;
}
export const pct = (v?: number | null, d = 2) => (v === null || v === undefined ? "-" : `${(v * 100).toFixed(d)}%`);
export const rate = (v?: number | null) => (v === null || v === undefined ? "-" : v >= 10 ? v.toFixed(0) : v >= 1 ? v.toFixed(1) : v.toFixed(2));

export function useScope() {
  const [sp, setSp] = useSearchParams();
  const dataset = sp.get("dataset") || "live";
  const minutes = Number(sp.get("minutes") || 60);
  const env = sp.get("env") || "";
  const set = (k: string, v: string) => { const n = new URLSearchParams(sp); if (v) n.set(k, v); else n.delete(k); setSp(n, { replace: true }); };
  const qs = (extra: Record<string, any> = {}) => {
    const p = new URLSearchParams();
    if (dataset !== "live") p.set("dataset", dataset); else p.set("minutes", String(minutes));
    if (env) p.set("env", env);
    for (const [k, v] of Object.entries(extra)) if (v !== undefined && v !== null && v !== "" && v !== false) p.set(k, String(v));
    return p.toString();
  };
  const link = () => { const p = new URLSearchParams(); if (dataset !== "live") p.set("dataset", dataset); else if (minutes !== 60) p.set("minutes", String(minutes)); if (env) p.set("env", env); const s = p.toString(); return s ? `?${s}` : ""; };
  return { dataset, minutes, env, set, qs, link, live: dataset === "live" };
}

export function ScopeBar({ envs = [] as string[] }: { envs?: string[] }) {
  const { t } = useT(); const sc = useScope();
  const ds = useQuery({ queryKey: ["datasets"], queryFn: () => get("/datasets"), staleTime: 30000 });
  return <div className="row" style={{ gap: 8 }}>
    <select className="input" style={{ width: 210 }} value={sc.dataset} onChange={(e) => sc.set("dataset", e.target.value === "live" ? "" : e.target.value)}>
      <option value="live">{t("apm_live")}</option>{(ds.data ?? []).map((d: any) => <option key={d.id} value={d.id}>{d.name}</option>)}</select>
    {sc.live && <select className="input" style={{ width: 130 }} value={sc.minutes} onChange={(e) => sc.set("minutes", e.target.value === "60" ? "" : e.target.value)}>
      {RANGES.map((m) => <option key={m} value={m}>{t("apm_last")} {m < 60 ? `${m} dk` : m < 1440 ? `${m / 60} sa` : `${m / 1440} gün`}</option>)}</select>}
    {envs.length > 0 && <select className="input" style={{ width: 150 }} value={sc.env} onChange={(e) => sc.set("env", e.target.value)}>
      <option value="">{t("apm_env_all")}</option>{envs.map((e) => <option key={e} value={e}>{e}</option>)}</select>}
  </div>;
}

export function LinkBadge({ link, inferred }: { link: string; inferred?: boolean }) {
  const { t } = useT();
  const cls = link === "explicit" ? "green" : link === "session" ? "blue" : "";
  return <span className="row" style={{ gap: 4, display: "inline-flex" }}><span className={`badge ${cls}`} title={t(`apm_link_${link}_hint`)} style={{ textTransform: "none" }}>{t(`apm_link_${link}`)}</span>
    {inferred && <span className="badge amber" title={t("apm_inferred_hint")} style={{ textTransform: "none" }}>{t("apm_inferred")}</span>}</span>;
}
