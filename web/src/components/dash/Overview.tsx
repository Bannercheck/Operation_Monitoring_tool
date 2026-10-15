/** "Genel bakış" panels of their own: fleet health (donut by state with the count in the middle, one square per server; a slice
 *  narrows the squares and lists those servers), fleet CPU / memory / disk (/ GPU) gauges with their levels, and the recent alarms
 *  (donut by severity with the total, the list with time; a click opens the server or the alarm's page). */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Activity, Bell, FileText, Gauge as GaugeIc } from "lucide-react";
import { get } from "../../api";
import { useT } from "../../i18n";
import { fmtPct, hostState } from "../ops/util";
import { BarTable, Donut, Gauge, Panel, ST, type Lv } from "./kit";
import { useDash } from "./ctx";
import { useInfra } from "./Infra";

const STATE: Record<string, Lv> = { ok: "ok", warn: "warn", crit: "crit", silent: "none" };

export function HealthPanel({ hosts, w = 4, qs }: { hosts: any[]; w?: number; qs?: any[] }) {
  const { t } = useT(); const c = useDash(); const [sel, setSel] = useState<string | null>(null);
  const by: Record<string, any[]> = { crit: [], warn: [], silent: [], ok: [] };
  for (const h of hosts) by[hostState(h)].push(h);
  const order = ["crit", "warn", "silent", "ok"]; const label: Record<string, string> = { ok: t("db_lv_ok"), warn: t("db_lv_warn"), crit: t("db_lv_crit"), silent: t("hm_silent") };
  const shown = sel ? by[sel] : order.flatMap((k) => by[k]);
  return <Panel w={w} title={t("db_health")} sub={t("db_health_s")} qs={qs} explore={{ to: "/inventory", label: t("db_x_inv") }} empty={!hosts.length} emptyText={t("fl_empty")}
    csv={() => ({ name: "watchover-fleet-health", cols: ["host", "pool", "env", "state", "lines", "errors", "error_rate"], rows: hosts.map((h) => [h.host, h.pool, h.env, hostState(h), h.events, h.errors, h.error_rate]) })}>
    <Donut parts={order.map((k) => ({ key: k, label: label[k], value: by[k].length, color: ST[STATE[k]] }))} center={<b>{sel ? by[sel].length : hosts.length}</b>} label={sel ? label[sel] : t("hm_hosts")}
      sel={sel} onPick={(k) => setSel(sel === k ? null : k)} />
    <div className="hsq" role="list">{shown.slice(0, 300).map((h) => { const s = hostState(h);
      return <button key={h.host} role="listitem" className="hsq-i" style={{ background: ST[STATE[s]] }} title={`${h.host} · ${h.pool} · ${label[s]} · ERROR+ ${fmtPct(h.error_rate, 1)}`} aria-label={`${h.host} ${label[s]}`} onClick={() => c.openHost(h.host)} />; })}</div>
    {sel && <div className="hsq-list"><BarTable rows={by[sel].slice(0, 8)} rowKey={(r) => r.host} onRow={(r) => c.openHost(r.host)} cols={[{ k: "host", label: t("db_c_host") }, { k: "pool", label: "Pool" },
      { k: "error_rate", label: "ERROR+", bar: { value: (r) => r.error_rate, max: 0.1, lv: () => STATE[sel], fmt: (v) => fmtPct(v, 1) } }]} />
      {by[sel].length > 8 && <div className="dim small" style={{ padding: "4px 2px" }}>+{by[sel].length - 8}</div>}</div>}
  </Panel>;
}

export function GaugesPanel({ w = 4 }: { w?: number }) {
  const { t } = useT(); const c = useDash(); const q = useInfra(); const d = q.data;
  const ms = (["cpu", "memory", "disk", "gpu"] as const).filter((m) => (d?.fleet?.[m]?.hosts ?? 0) > 0);
  const name = { cpu: "CPU", memory: t("db_mem"), disk: "Disk", gpu: "GPU" } as const;
  return <Panel w={w} title={t("db_resources")} sub={d?.hosts ? t("db_hosts_n", { n: d.hosts }) : undefined} qs={[q]} empty={!ms.length} emptyText={t("db_no_metrics")}
    explore={{ go: () => c.setDash("infra"), label: t("db_d_infra") }}
    csv={() => ({ name: "watchover-fleet-resources", cols: ["metric", "avg", "max", "worst", "hosts", "warn", "crit"], rows: ms.map((m) => [m, d.fleet[m].avg, d.fleet[m].max, d.fleet[m].worst, d.fleet[m].hosts, d.fleet[m].warn, d.fleet[m].crit]) })}>
    <div className={`gg-grid n${ms.length}`}>{ms.map((m) => { const f = d.fleet[m];
      return <Gauge key={m} value={f.avg} warn={f.warn} crit={f.crit} title={name[m]} size={132} onClick={() => c.openMetric(m)}
        sub={<span className="dim small ell">{t("od_max")} %{Math.round(f.max)} · {f.worst}</span>} />; })}</div>
  </Panel>;
}

const KIND_IC: Record<string, any> = { threshold: GaugeIc, anomaly: Activity, alert: Bell, log: FileText };

export function AlarmsPanel({ w = 4, id }: { w?: number; id?: string }) {
  const { t, lang } = useT(); const c = useDash(); const nav = useNavigate(); const [sel, setSel] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["dash-alarms", c.rq], queryFn: () => get(`/dash/alarms?${c.rq}&limit=40`), refetchInterval: c.ms });
  const d = q.data; const items = ((d?.items ?? []) as any[]).filter((i) => !sel || i.sev === sel);
  const SEV: Record<string, Lv> = { crit: "crit", warn: "warn", info: "none" };
  const hm = (iso: string) => { const x = new Date(iso); const today = x.toDateString() === new Date().toDateString();
    return x.toLocaleString(lang === "tr" ? "tr-TR" : "en-US", today ? { hour: "2-digit", minute: "2-digit", hour12: false } : { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }); };
  const open = (i: any) => (i.host ? c.openHost(i.host) : nav(i.kind === "anomaly" ? "/anomalies" : "/alerts"));
  return <Panel w={w} id={id} title={t("db_alarms")} sub={d ? t("db_alarms_s", { n: d.active }) : undefined} qs={[q]} explore={{ to: "/anomalies", label: t("db_x_anom") }}
    csv={() => ({ name: "watchover-alarms", cols: ["time", "severity", "kind", "host", "title", "detail", "active"], rows: (d?.items ?? []).map((i: any) => [i.ts, i.sev, i.kind, i.host, i.title, i.detail, i.active]) })}>
    <Donut parts={(["crit", "warn", "info"] as const).map((k) => ({ key: k, label: t("db_sev_" + k), value: d?.by_sev?.[k] ?? 0, color: ST[SEV[k]] }))} size={120} thick={12}
      center={<><Bell size={15} className="dim" /><b>{d?.total ?? 0}</b></>} label={t("db_alarm")} sel={sel} onPick={(k) => setSel(sel === k ? null : k)} />
    {!items.length ? <div className="pn-empty sm">{t("db_no_alarms")}</div> :
      <div className="al-list">{items.slice(0, 30).map((i) => { const Ic = KIND_IC[i.kind] ?? Bell;
        return <button key={i.id} className="al-i" onClick={() => open(i)} title={i.detail || i.title}>
          <span className="al-bar" style={{ background: ST[SEV[i.sev] ?? "none"] }} />
          <Ic size={13} className="dim" />
          <span className="al-t"><b className="ell">{i.title}</b><span className="dim small ell">{t("db_kind_" + i.kind)}{i.host ? ` · ${i.host}` : ""}{i.active ? ` · ${t("db_active")}` : ""}</span></span>
          <span className="al-ts mono">{i.ts ? hm(i.ts) : ""}</span>
        </button>; })}</div>}
  </Panel>;
}
