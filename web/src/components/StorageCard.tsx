import { useQuery } from "@tanstack/react-query";
import { get } from "../api";
import { useT } from "../i18n";
import { Collapsible, fmtTs } from "./ui";

/** Log storage tiers (GET /api/system/storage, logtiers.py): hot day files, the cold archive and the main database, with usage
 *  bars against the size caps and a threshold-style warning past 85 % of a cap. */
const fmtB = (b?: number | null) => {
  if (b == null) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"]; let v = b; let i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
};
const dayLabel = (d?: string) => (d && d.length === 8 ? `${d.slice(6, 8)}.${d.slice(4, 6)}.${d.slice(0, 4)}` : "–");

function Usage({ bytes, cap, pct }: { bytes: number; cap: number; pct: number | null }) {
  const { t } = useT();
  const warn = pct != null && pct >= 85;
  return <div className="stg-use">
    <div className={`stg-bar ${warn ? "warn" : ""}`} role="meter" aria-valuenow={pct ?? 0} aria-valuemin={0} aria-valuemax={100}><i style={{ width: `${Math.min(100, pct ?? 0)}%` }} /></div>
    <span className={`small ${warn ? "" : "dim"}`} style={warn ? { color: "var(--amber, #f59e0b)" } : undefined}>
      {cap ? t("stg_cap", { u: fmtB(bytes), c: fmtB(cap), p: pct ?? 0 }) : t("stg_nocap", { u: fmtB(bytes) })}</span>
  </div>;
}

export function StorageCard() {
  const { t } = useT();
  const q = useQuery({ queryKey: ["sys-storage"], queryFn: () => get("/system/storage"), refetchInterval: 60000 });
  const d: any = q.data;
  if (!d) return null;
  const hot = d.hot ?? {}; const cold = d.cold; const warns: any[] = d.warnings ?? [];
  const hotDays: any[] = hot.days ?? []; const coldDays: any[] = cold?.days ?? [];
  const last = cold?.archived?.length ? cold.archived[cold.archived.length - 1] : null;
  const mig = d.migration ?? {};
  return <Collapsible id="sys-storage" title={`🗄 ${t("stg_title")}`}
    right={<span className={`badge ${warns.length ? "amber" : "green"}`}>{warns.length ? `${warns.length} ${t("stg_warn")}` : t("stg_ok")}</span>}>
    <div className="muted small" style={{ marginBottom: 8 }}>{d.mode === "files" ? t("stg_hint") : t("stg_dbmode")}</div>
    <div className="grid k3">
      <div className="stg-tier">
        <div className="stg-h">{t("stg_hot", { h: Math.round(d.retention_h ?? 48) })}</div>
        <div className="stg-v">{fmtB(hot.bytes)}</div>
        <div className="small dim">{hotDays.length ? `${t("stg_days", { n: hotDays.length })} · ${dayLabel(hotDays[0]?.day)} → ${dayLabel(hotDays[hotDays.length - 1]?.day)}` : (hot.in_main_db ? t("stg_in_main") : t("stg_empty"))}</div>
        <div className="small dim">{t("stg_lines", { n: (hot.events ?? 0).toLocaleString() })}{hot.bytes_per_line ? ` · ${t("stg_bpl", { b: hot.bytes_per_line })}` : ""}</div>
        <Usage bytes={hot.bytes ?? 0} cap={hot.cap_bytes ?? 0} pct={hot.pct ?? null} />
        {hot.disk && <div className="small dim">{t("stg_free", { f: fmtB(hot.disk.free) })}</div>}
      </div>
      <div className="stg-tier">
        <div className="stg-h">{t("stg_cold", { d: Math.round(d.cold_retention_days ?? 7) })}</div>
        {cold ? <>
          <div className="stg-v">{fmtB(cold.bytes)}</div>
          <div className="small dim">{coldDays.length ? `${t("stg_days", { n: coldDays.length })} · ${dayLabel(coldDays[0]?.day)} → ${dayLabel(coldDays[coldDays.length - 1]?.day)}` : (cold.enabled ? t("stg_never") : t("stg_disabled"))}</div>
          <div className="small dim">{t("stg_lines", { n: (cold.lines ?? 0).toLocaleString() })}{cold.bytes_per_line ? ` · ${t("stg_bpl", { b: cold.bytes_per_line })}` : ""}</div>
          <Usage bytes={cold.bytes ?? 0} cap={cold.cap_bytes ?? 0} pct={cold.pct ?? null} />
          {cold.disk && <div className="small dim">{t("stg_free", { f: fmtB(cold.disk.free) })}</div>}
          {last && <div className="small dim">{t("stg_last", { d: dayLabel(last.day), s: last.seconds, r: (last.lines_per_s ?? 0).toLocaleString() })}</div>}
          {!!cold.last_run?.errors?.length && <div className="small bad">{cold.last_run.errors[0].error}</div>}
        </> : <div className="small dim">{t("stg_dbmode")}</div>}
      </div>
      <div className="stg-tier">
        <div className="stg-h">{t("stg_main")}</div>
        <div className="stg-v">{fmtB(d.main_db?.bytes)}</div>
        <div className="small dim">{d.main_db?.backend === "postgresql" ? "PostgreSQL" : "SQLite"}</div>
        <div className="small dim">{t("stg_main_sub")}</div>
        {mig.state === "running" && <div className="small" style={{ color: "var(--amber, #f59e0b)" }}>{t("stg_migrating", { c: (mig.copied ?? 0).toLocaleString(), n: (mig.total ?? 0).toLocaleString() })}</div>}
        {mig.state === "done" && <div className="small dim">{t("stg_migrated")}</div>}
      </div>
    </div>
    {!!warns.length && <ul className="small" style={{ marginTop: 8, color: "var(--amber, #f59e0b)" }}>
      {warns.map((w) => <li key={w.code}>{t(`stg_w_${w.code}`, { p: w.pct })}</li>)}</ul>}
    {cold?.last_run?.at && <div className="small dim" style={{ marginTop: 6 }}>{t("stg_job", { t: fmtTs(cold.last_run.at) })}</div>}
  </Collapsible>;
}
