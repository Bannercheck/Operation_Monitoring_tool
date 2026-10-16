import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Pencil } from "lucide-react";
import { del, get, post, put } from "../../api";
import { useAuth } from "../../auth";
import { useT } from "../../i18n";
import { Card, Empty } from "../ui";

const UNIT: Record<string, string> = { cpu: "%", memory: "%", disk: "%", gpu: "%", error_rate: "%", errors_per_min: "/dk", p95_ms: "ms", silent_min: "dk" };
const METRICS = Object.keys(UNIT);
const fmtV = (v: any, m: string) => (v == null ? "–" : `${Number(v).toLocaleString("tr-TR", { maximumFractionDigits: 1 })}${UNIT[m] === "%" ? "%" : ` ${UNIT[m]}`}`);
type Scope = { scope: "host" | "pool" | "env" | "application"; value: string };

/** "Alarm metrikleri" in the server detail: every alarm metric with its current value, the warn / critical levels in force and where
 *  they come from (this server, its pool, its environment, all servers, system defaults). "Düzenle" sets the levels right here, for
 *  this server or for its whole group (pool / environment): a policy per metric at that scope is created or updated (recipients,
 *  sustain and cooldown are copied from the policy in force), "Devral" removes the override so the wider level applies again. */
export function HostAlarms({ host, pool, env, th }: { host: string; pool?: string; env?: string; th: any }) {
  const { t } = useT(); const { can } = useAuth(); const qc = useQueryClient();
  const writable = can("sys.notify") || can("act.thresholds");
  const [edit, setEdit] = useState(false); const [sc, setSc] = useState<Scope>({ scope: "host", value: host });
  const [vals, setVals] = useState<Record<string, { warn: string; crit: string }>>({}); const [busy, setBusy] = useState(false); const [msg, setMsg] = useState("");
  const pol = useQuery({ queryKey: ["thr-policies"], queryFn: () => get("/thresholds"), enabled: edit });
  const scopes: Scope[] = [{ scope: "host", value: host }, ...((th?.apps ?? []) as string[]).map((a) => ({ scope: "application" as const, value: a })),   // the host's applications
    ...(pool ? [{ scope: "pool" as const, value: pool }] : []), ...(env ? [{ scope: "env" as const, value: env }] : [])];
  const scLabel = (s: Scope) => (s.scope === "host" ? t("ha_this_host") : `${t(`thr_scope_${s.scope}`)}: ${s.value}`);
  const at = (m: string) => (pol.data ?? []).find((p: any) => p.metric === m && p.scope === sc.scope && (p.scope_value || "") === sc.value);
  const open = () => {
    const v: Record<string, { warn: string; crit: string }> = {};
    for (const m of METRICS) { const x = th?.metrics?.[m] ?? {}; v[m] = { warn: x.warn == null ? "" : String(x.warn), crit: x.crit == null ? "" : String(x.crit) }; }
    setVals(v); setMsg(""); setEdit(true);
  };
  const num = (s: string) => (s.trim() === "" ? null : Number(s.replace(",", ".")));
  const save = async () => {
    setBusy(true); setMsg("");
    try {
      let n = 0;
      for (const m of METRICS) {
        const x = th?.metrics?.[m] ?? {}; const w = num(vals[m]?.warn ?? ""); const c = num(vals[m]?.crit ?? "");
        if ((w != null && Number.isNaN(w)) || (c != null && Number.isNaN(c))) throw new Error(`${t(`thr_m_${m}`)}: ${t("ha_bad_number")}`);
        const mine = at(m);
        const same = (mine ? mine.warn : x.warn) === w && (mine ? mine.crit : x.crit) === c;
        if (same || (!mine && w == null && c == null)) continue;
        const src = (pol.data ?? []).find((p: any) => p.id === x.source?.policy) ?? {};
        const body = { name: `${sc.value} · ${t(`thr_m_${m}`)}`, scope: sc.scope, scope_value: sc.value, metric: m, warn: w, crit: c,
          for_min: mine?.for_min ?? src.for_min ?? 3, channels: mine?.channels ?? src.channels ?? "email", targets: mine?.targets ?? src.targets ?? "",
          cooldown_min: mine?.cooldown_min ?? src.cooldown_min ?? 30, notify_resolved: mine?.notify_resolved ?? src.notify_resolved ?? true, enabled: true,
          note: mine?.note ?? t("ha_note"), team_id: mine?.team_id ?? null };
        if (mine) await put(`/thresholds/${mine.id}`, body); else await post("/thresholds", body);
        n++;
      }
      await qc.invalidateQueries({ queryKey: ["thr-ov"] }); await qc.invalidateQueries({ queryKey: ["thr-policies"] });
      setMsg(n ? t("ha_saved", { n }) : t("ha_nothing")); if (n) setEdit(false);
    } catch (e: any) { setMsg(e?.message || String(e)); } finally { setBusy(false); }
  };
  const inherit = async (m: string) => {
    const mine = at(m); if (!mine) return;
    setBusy(true);
    try { await del(`/thresholds/${mine.id}`); await qc.invalidateQueries({ queryKey: ["thr-ov"] }); await qc.invalidateQueries({ queryKey: ["thr-policies"] }); setEdit(false); }
    catch (e: any) { setMsg(e?.message || String(e)); } finally { setBusy(false); }
  };
  const right = <div className="row" style={{ gap: 6 }}>
    {writable && !edit && <button className="btn sm" onClick={open}><Pencil size={13} /> {t("ha_edit")}</button>}
    <Link className="btn sm" to={`/thresholds?tab=hosts&host=${encodeURIComponent(host)}`}>{t("ha_all")}</Link></div>;
  if (!edit) return <Card title={t("ha_title")} right={right}>{!th ? <Empty>{t("thr_no_hosts")}</Empty> :
    <div className="hd-thr">{METRICS.map((m) => { const x = th.metrics?.[m] ?? {}; return <div key={m} className={`hd-thr-r ${x.level === "warn" || x.level === "crit" ? x.level : ""}`}>
      <span>{t(`thr_m_${m}`)}</span><b>{fmtV(x.value, m)}</b>
      <span className="dim small">{x.warn != null || x.crit != null ? `${x.warn ?? "–"} / ${x.crit ?? "–"}${x.source ? ` · ${x.source.scope === "host" ? t("ha_this_host") : `${t(`thr_scope_${x.source.scope}`)}${x.source.value ? `: ${x.source.value}` : ""}`}` : ""}` : t("thr_no_level")}</span></div>; })}
    </div>}{msg && <div className="small dim" style={{ marginTop: 6 }}>{msg}</div>}</Card>;
  return <Card title={t("ha_title")} right={<button className="btn sm" onClick={() => setEdit(false)}>{t("cancel")}</button>}>
    <div className="stack" style={{ gap: 10 }}>
      <div className="row wrap" style={{ gap: 6, alignItems: "center" }}><span className="small dim">{t("ha_apply_to")}</span>
        <div className="seg">{scopes.map((s) => <button key={`${s.scope}:${s.value}`} className={sc.scope === s.scope && sc.value === s.value ? "on" : ""} onClick={() => setSc(s)}>{scLabel(s)}</button>)}</div></div>
      <div className="ha-grid">
        <span className="dim small">{t("ha_metric")}</span><span className="dim small">{t("thr_warn")}</span><span className="dim small">{t("thr_crit")}</span><span />
        {METRICS.map((m) => { const mine = at(m); return [
          <span key={m + "n"} className="small">{t(`thr_m_${m}`)} <span className="dim">({UNIT[m]})</span>{th?.metrics?.[m]?.value != null && <span className="dim"> · {fmtV(th.metrics[m].value, m)}</span>}</span>,
          <input key={m + "w"} className="input sm" inputMode="decimal" value={vals[m]?.warn ?? ""} onChange={(e) => setVals({ ...vals, [m]: { ...vals[m], warn: e.target.value } })} />,
          <input key={m + "c"} className="input sm" inputMode="decimal" value={vals[m]?.crit ?? ""} onChange={(e) => setVals({ ...vals, [m]: { ...vals[m], crit: e.target.value } })} />,
          <span key={m + "x"}>{mine && <button className="btn sm ghost" disabled={busy} onClick={() => inherit(m)} title={t("ha_inherit_hint")}>{t("ha_inherit")}</button>}</span>]; })}
      </div>
      <div className="row between wrap" style={{ gap: 8 }}><span className="small dim">{t("ha_hint")}</span>
        <button className="btn sm primary" disabled={busy || pol.isLoading} onClick={save}>{t("ha_save", { s: scLabel(sc) })}</button></div>
      {msg && <div className="small" style={{ color: "var(--red)" }}>{msg}</div>}
    </div>
  </Card>;
}
