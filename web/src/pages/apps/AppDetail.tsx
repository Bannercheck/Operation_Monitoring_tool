import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Boxes, Pencil, ScrollText, UserPlus } from "lucide-react";
import { ApiError, get, post } from "../../api";
import { useAuth } from "../../auth";
import { useT } from "../../i18n";
import { Btn, Empty, Err } from "../../components/ui";
import { BarTable, Grid, Panel, Stat, TimeChart, seriesCsv } from "../../components/dash/kit";
import { HostDetail } from "../../components/ops/HostDetail";
import { qv } from "../../components/logs/lib";
import { Graph } from "../apm/Topology";
import { AppEditor, CritBadge, LV, StatusBadge, ms, num, pctOf, qlink, sloText, useNarrow } from "./common";
import { AlarmsTab, EventsTab, FlowsTab, LogsTab, RumTab, TracesTab } from "./AppTabs";

const RANGES = [15, 60, 360, 1440, 10080];
const TABS = ["overview", "hosts", "services", "logs", "traces", "rum", "flows", "alarms", "events"] as const;
export const rangeName = (m: number, lang: string) => (lang === "tr" ? (m < 60 ? `${m} dk` : m < 1440 ? `${m / 60} sa` : `${m / 1440} gün`) : (m < 60 ? `${m} min` : m < 1440 ? `${m / 60} h` : `${m / 1440} d`));

/** One application: its record and health, and tabs for everything that belongs to it (overview, hosts, services and their map, logs
 *  with a live tail, traces, user experience, business flows, alarms and thresholds, events). The range drives the charts; health
 *  numbers come from the live counters (at most the last 60 minutes). */
export default function AppDetail() {
  const { id } = useParams(); const { t, lang } = useT(); const { can } = useAuth(); const nav = useNavigate();
  const [sp, setSp] = useSearchParams();
  const tab = (TABS as readonly string[]).includes(sp.get("tab") || "") ? (sp.get("tab") as string) : "overview";
  const range = Number(sp.get("minutes") || 60); const hm = Math.min(range, 60);
  const set = (k: string, v: string | null) => { const n = new URLSearchParams(sp); if (v) n.set(k, v); else n.delete(k); setSp(n, { replace: true }); };
  const d = useQuery({ queryKey: ["app", id, hm], queryFn: () => get(`/apps/${id}?minutes=${hm}`), refetchInterval: 15000, retry: (n, e: any) => !(e instanceof ApiError && e.status === 404) && n < 2 });
  const s = useQuery({ queryKey: ["app-series", id, range], queryFn: () => get(`/apps/${id}/series?minutes=${range}`), refetchInterval: range <= 60 ? 15000 : 60000, enabled: !!d.data });
  const [edit, setEdit] = useState(false); const [hd, setHd] = useState<string | null>(null); const [claim, setClaim] = useState<any>(null); const [err, setErr] = useState<any>(null);
  if (d.error instanceof ApiError && d.error.status === 404) return <Empty>{t("ap_not_found")} · <Link to="/apps">{t("ap_title")}</Link></Empty>;
  if (!d.data) return <div className="stack"><Err e={d.error} />{d.isLoading && <div className="muted">{t("loading")}</div>}</div>;
  const a = d.data; const h = a.health ?? {};
  const doClaim = async () => { setErr(null); try { setClaim(await post(`/apps/${a.id}/claim-hosts`)); } catch (e) { setErr(e); } };
  const label = (k: string) => t(`ap_t_${k === "overview" ? "overview" : k}`);
  return <div className="stack">
    <div className="ap-head">
      <div className="ap-head-t">
        <h2><Boxes size={20} style={{ verticalAlign: -3 }} /> {a.name}</h2>
        <div className="row" style={{ gap: 6 }}><StatusBadge s={h.status} /><CritBadge v={a.criticality} />
          <span className="chip">{a.team || t("ap_install")}</span>{(a.envs ?? []).map((e: string) => <span key={e} className="chip">{e}</span>)}
          {(a.tags ?? []).map((x: string) => <span key={x} className="chip dim">#{x}</span>)}</div>
        {a.description && <div className="muted small">{a.description}</div>}
      </div>
      <div className="row" style={{ gap: 8 }}>
        <select className="input" style={{ width: 130 }} value={range} onChange={(e) => set("minutes", e.target.value === "60" ? null : e.target.value)}>
          {RANGES.map((m) => <option key={m} value={m}>{rangeName(m, lang)}</option>)}</select>
        {can("page.logs") && <Link className="btn sm" to={qlink(a.log_query)}><ScrollText size={14} /> {t("ap_t_logs")}</Link>}
        {a.can_claim && <Btn sm onClick={doClaim} title={t("ap_claim_hint")}><UserPlus size={14} /> {t("ap_claim")}</Btn>}
        {a.editable && <Btn sm kind="primary" onClick={() => setEdit(true)}><Pencil size={14} /> {t("ap_edit")}</Btn>}
      </div>
    </div>
    {claim && <div className="card tight small">{t("ap_claim_done", { a: claim.added.length, r: claim.rejected.length + claim.rejected_hidden })}
      {claim.rejected.length > 0 && <span className="dim"> · {claim.rejected.map((r: any) => `${r.host} (${r.reason})`).join(", ")}</span>}</div>}
    <Err e={err} />
    <div className="ap-tabs">{TABS.map((k) => <button key={k} className={tab === k ? "on" : ""} onClick={() => set("tab", k === "overview" ? null : k)}>{label(k)}</button>)}</div>
    {tab === "overview" && <Overview a={a} s={s} range={range} onHost={setHd} />}
    {tab === "hosts" && <Hosts a={a} onHost={setHd} />}
    {tab === "services" && <Services a={a} range={range} />}
    {tab === "logs" && <LogsTab a={a} />}
    {tab === "traces" && <TracesTab a={a} range={range} />}
    {tab === "rum" && <RumTab a={a} range={range} />}
    {tab === "flows" && <FlowsTab a={a} range={range} />}
    {tab === "alarms" && <AlarmsTab a={a} />}
    {tab === "events" && <EventsTab a={a} range={range} />}
    {edit && <AppEditor app={a} onClose={() => setEdit(false)} />}
    {hd && <HostDetail host={hd} base={`last=${hm}`} span={hm} focused={false} onClose={() => setHd(null)}
      onFocus={(x) => nav(`/ops?host=${encodeURIComponent(x)}&last=${hm}`)}
      onLogs={(f: any) => nav(qlink(`host:${qv(f.host ?? hd)}${f.sev === "ERROR" ? " level>=error" : ""}${f.service ? ` service:${qv(f.service)}` : ""}`))}
      onZoom={(x, st) => nav(`/ops?host=${encodeURIComponent(hd)}&start=${new Date(x * 1000).toISOString()}&end=${new Date((x + st * 60) * 1000).toISOString()}`)} />}
  </div>;
}

function Overview({ a, s, range, onHost }: { a: any; s: any; range: number; onHost: (h: string) => void }) {
  const { t, lang } = useT(); const h = a.health ?? {}; const rq = h.requests ?? {}; const lg = h.logs ?? {}; const hs = h.hosts ?? {}; const al = h.alerts ?? {};
  const traced = (rq.calls ?? 0) > 0; const slo = sloText(t, h.slo); const ser: any[] = s.data?.series ?? [];
  const sub = t("ap_window", { n: h.window_min ?? 60 });
  const hostRows = [...(a.hosts ?? [])].filter((x: any) => x.lines).sort((x: any, y: any) => (y.error_rate ?? 0) - (x.error_rate ?? 0) || y.errors - x.errors).slice(0, 8);
  const svcRows = [...(a.services ?? [])].map((x: any) => ({ ...x, bad: (x.call_errors || 0) + (x.errors || 0) })).filter((x: any) => x.calls || x.lines).sort((x: any, y: any) => y.bad - x.bad).slice(0, 8);
  const alerts = (al.crit ?? 0) + (al.warn ?? 0) + (al.anomalies ?? 0);
  return <><div className="dim small">{sub}{range > 60 ? ` · ${t("ap_p_traffic")}: ${rangeName(range, lang)}` : ""}</div><Grid>
    <Panel w={2} title={traced ? t("ap_rate") : t("ap_lines_min")} qs={[s]}>
      <Stat value={num(traced ? rq.per_min : lg.per_min, lang)} sub={traced ? t("ap_from_traces") : t("ap_from_logs")} spark={ser.map((x) => (traced ? x.calls : x.lines))} color="var(--s1)" /></Panel>
    <Panel w={2} title={t("ap_err_rate")}><Stat value={pctOf(traced ? rq.error_rate : lg.error_rate)} tone={((traced ? rq.error_rate : lg.error_rate) ?? 0) >= 0.05 ? "crit" : ""}
      sub={`${num(traced ? rq.errors : lg.errors, lang)} ${t("ap_errors").toLowerCase()}`} spark={ser.map((x) => (traced ? x.call_error_rate : x.error_rate))} color="var(--s8)" /></Panel>
    <Panel w={2} title={t("ap_p95")}><Stat value={ms(rq.p95_ms ?? lg.p95_ms)} sub={rq.p95_ms != null ? t("ap_from_traces") : t("ap_from_logs")} spark={ser.map((x) => x.p95_ms)} color="var(--s3)" /></Panel>
    <Panel w={2} title={t("ap_slo")}><Stat value={slo.main} tone={slo.tone} sub={slo.sub} /></Panel>
    <Panel w={2} title={t("ap_hosts_k")}><Stat value={`${hs.reporting ?? 0}/${hs.total ?? 0}`} tone={hs.total && !hs.reporting ? "crit" : hs.silent ? "warn" : ""}
      sub={[hs.silent ? `${hs.silent} ${t("ap_state_silent")}` : "", hs.unknown ? `${hs.unknown} ${t("ap_state_unknown")}` : ""].filter(Boolean).join(" · ") || t("ap_state_reporting")} /></Panel>
    <Panel w={2} title={t("ap_alerts_k")}><Stat value={alerts} tone={al.crit ? "crit" : alerts ? "warn" : ""} sub={`${al.crit ?? 0} ${t("ap_st_crit").toLowerCase()} · ${al.warn ?? 0} ${t("ap_st_warn").toLowerCase()} · ${t("ap_anoms", { n: al.anomalies ?? 0 })}`} /></Panel>
    <Panel w={8} title={t("ap_p_traffic")} sub={t("ap_p_traffic_sub")} qs={[s]} empty={!ser.some((x) => x.lines || x.calls)}
      csv={() => seriesCsv(`${a.name}-traffic`, ser, [{ key: "lines", label: "lines" }, { key: "errors", label: "errors" }, { key: "calls", label: "calls" }])}>
      <TimeChart data={ser} span={range} kind="area" fmt={(v) => num(v, lang)} series={[{ key: "lines", label: t("ap_lines"), color: "var(--s1)" }, { key: "errors", label: "ERROR+", color: "var(--s8)" },
        ...(traced ? [{ key: "calls", label: t("ap_calls"), color: "var(--s6)" }] : [])]} /></Panel>
    <Panel w={4} title={t("ap_p_why")}>
      <div className="stack" style={{ gap: 8 }}>
        {(h.reasons ?? []).length === 0 ? <div className="dim">{t("ap_all_good")}</div> : h.reasons.map((r: string) => <div key={r} className="row" style={{ gap: 6 }}>
          <span className={`lvl lv-${["slo_breach", "thr_crit", "all_silent"].includes(r) ? "crit" : "warn"}`}><i /></span>{t(`ap_r_${r}`)}</div>)}
        {h.worst_host && <button className="ap-worst" onClick={() => onHost(h.worst_host.host)}><span className="dim small">{t("ap_worst")} · {t("ap_col_host")}</span>
          <b className="mono">{h.worst_host.host}</b><span className="small">{h.worst_host.error_rate != null ? pctOf(h.worst_host.error_rate) : ""} {h.worst_host.level !== "ok" ? `· ${t(`ap_st_${h.worst_host.level}`)}` : ""}</span></button>}
        {h.worst_service && <div className="ap-worst"><span className="dim small">{t("ap_worst")} · {t("ap_col_service")}</span><b className="mono">{h.worst_service.service}</b>
          <span className="small">{pctOf(h.worst_service.error_rate)} · {h.worst_service.source === "traces" ? t("ap_from_traces") : t("ap_from_logs")}</span></div>}
      </div></Panel>
    <Panel w={6} title={t("ap_p_lat")} qs={[s]} empty={!ser.some((x) => x.p95_ms != null)}>
      <TimeChart data={ser} span={range} fmt={(v) => ms(v)} series={[{ key: "p95_ms", label: "p95", color: "var(--s3)" }]} legend="none" /></Panel>
    <Panel w={6} title={t("ap_p_err")} qs={[s]} empty={!ser.some((x) => x.error_rate != null || x.call_error_rate != null)}>
      <TimeChart data={ser} span={range} fmt={(v) => pctOf(v, 1)} series={[{ key: "error_rate", label: t("ap_log_err"), color: "var(--s8)" }, ...(traced ? [{ key: "call_error_rate", label: t("ap_req_err"), color: "var(--s5)" }] : [])]} /></Panel>
    <Panel w={6} title={t("ap_p_hosts")} sub={sub} empty={!hostRows.length}>
      <BarTable rows={hostRows} rowKey={(r) => r.host} onRow={(r) => onHost(r.host)} cols={[{ k: "host", label: t("ap_col_host"), render: (r) => <span className="mono">{r.host}</span> },
        { k: "error_rate", label: t("ap_err_rate"), bar: { value: (r) => r.error_rate, lv: (r) => (r.error_rate >= 0.05 ? "crit" : r.error_rate > 0 ? "warn" : "ok"), fmt: (v) => pctOf(v, 1) } }]} /></Panel>
    <Panel w={6} title={t("ap_p_services")} sub={sub} empty={!svcRows.length}>
      <BarTable rows={svcRows} rowKey={(r) => r.service} cols={[{ k: "service", label: t("ap_col_service"), render: (r) => <span className="mono">{r.service}</span> },
        { k: "bad", label: t("ap_errors"), bar: { value: (r) => r.bad, color: "var(--s8)", fmt: (v) => num(v, lang) } }]} /></Panel>
  </Grid></>;
}

const stateLv = (s: string) => (s === "reporting" ? "ok" : s === "silent" ? "warn" : "none");
const ago = (s: number | null, lang: string) => (s == null ? "–" : s < 60 ? `${s} ${lang === "tr" ? "sn" : "s"}` : s < 3600 ? `${Math.round(s / 60)} ${lang === "tr" ? "dk" : "min"}` : `${Math.round(s / 3600)} ${lang === "tr" ? "sa" : "h"}`);

function Hosts({ a, onHost }: { a: any; onHost: (h: string) => void }) {
  const { t, lang } = useT(); const narrow = useNarrow();
  const all = [
    { k: "host", label: t("ap_col_host"), render: (r: any) => <span className="row" style={{ gap: 6, flexWrap: "nowrap" }}><span className={`lvl lv-${stateLv(r.state)}`}><i /></span><span className="mono">{r.host}</span></span> },
    { k: "via", label: t("ap_col_via"), render: (r: any) => <span className="dim small">{t(`ap_via_${r.via}`)}</span> },
    { k: "env", label: t("ap_col_env"), render: (r: any) => <span className="small">{r.env || "–"}{r.pool ? ` · ${r.pool}` : ""}</span> },
    { k: "last", label: t("ap_col_last"), num: true, render: (r: any) => <span className="small">{ago(r.last_seen_s, lang)}</span> },
    { k: "level", label: t("ap_col_level"), render: (r: any) => <span className={`lvl lv-${LV[r.level] ?? "ok"}`}><i />{r.level === "ok" ? "" : t(`ap_st_${r.level}`)}</span> },
    { k: "per_min", label: t("ap_lines_min"), bar: { value: (r: any) => r.per_min, color: "var(--s1)", fmt: (v: number) => num(v, lang) } },
    { k: "error_rate", label: t("ap_err_rate"), bar: { value: (r: any) => r.error_rate, lv: (r: any) => (r.error_rate >= 0.05 ? "crit" : r.error_rate > 0 ? "warn" : "ok"), fmt: (v: number) => pctOf(v, 1) } }];
  const cols = narrow ? all.filter((c) => ["host", "last", "error_rate"].includes(c.k)) : all;   // a phone: no sideways scroll
  return <Grid>
    <Panel w={12} title={t("ap_t_hosts")} sub={t("ap_window", { n: a.health?.window_min ?? 60 })} empty={!(a.hosts ?? []).length} emptyText={t("ap_no_hosts")}>
      <BarTable rows={a.hosts ?? []} rowKey={(r) => r.host} onRow={(r) => onHost(r.host)} cols={cols as any} /></Panel>
    {(a.service_hosts ?? []).length > 0 && <Panel w={12} title={t("ap_svc_hosts")} sub={t("ap_svc_hosts_hint")}>
      <BarTable rows={a.service_hosts} rowKey={(r) => r.host} onRow={(r) => onHost(r.host)} cols={cols.filter((c) => c.k !== "via" && c.k !== "level") as any} /></Panel>}
  </Grid>;
}

function Services({ a, range }: { a: any; range: number }) {
  const { t, lang } = useT(); const { can } = useAuth(); const narrow = useNarrow();
  const top = useQuery({ queryKey: ["app-topo", a.id, range], queryFn: () => get(`/apps/${a.id}/topology?minutes=${range}`), enabled: can("page.apm"), refetchInterval: 30000 });
  const rows: any[] = a.services ?? []; const g = top.data?.services;
  return <Grid>
    <Panel w={12} title={t("ap_map")} qs={[top]} empty={can("page.apm") && !g?.nodes?.length} emptyText={t("ap_map_none")}>
      {!can("page.apm") ? <div className="dim">{t("ap_need_perm", { p: "page.apm" })}</div> : g?.nodes?.length ? <Graph nodes={g.nodes} edges={g.edges} mode="traffic" /> : null}</Panel>
    <Panel w={12} title={t("ap_t_services")} sub={t("ap_window", { n: a.health?.window_min ?? 60 })} empty={!rows.length}>
      <BarTable rows={rows} rowKey={(r) => r.service} cols={[
        { k: "service", label: t("ap_col_service"), render: (r) => <span className="row" style={{ gap: 6, flexWrap: "nowrap" }}>
          {can("page.apm") && r.calls ? <Link className="mono" to={`/services/${encodeURIComponent(r.service)}`} onClick={(e) => e.stopPropagation()}>{r.service}</Link> : <span className="mono">{r.service}</span>}
          <span className={`badge ${r.member ? "green" : ""}`} style={{ textTransform: "none" }}>{r.member ? t("ap_member") : t("ap_on_host")}</span></span> },
        ...(narrow ? [] : [{ k: "rps", label: t("ap_rate"), num: true, render: (r: any) => num(r.calls ? r.rps * 60 : null, lang) },
          { k: "p95", label: "p95", num: true, render: (r: any) => ms(r.p95_ms) },
          { k: "lines", label: t("ap_lines"), num: true, render: (r: any) => num(r.lines, lang) }]),
        { k: "err", label: t("ap_err_rate"), bar: { value: (r) => r.call_error_rate ?? r.error_rate, lv: (r) => (((r.call_error_rate ?? r.error_rate) ?? 0) >= 0.05 ? "crit" : ((r.call_error_rate ?? r.error_rate) ?? 0) > 0 ? "warn" : "ok"), fmt: (v) => pctOf(v, 1) } }]} /></Panel>
    {(a.namespaces ?? []).length > 0 && <Panel w={12} title={t("ap_namespaces")}>
      <div className="stack" style={{ gap: 6 }}>{a.namespaces.map((n: any) => <div key={n.namespace} className="row" style={{ gap: 6 }}><b className="mono">{n.namespace}</b>
        {n.services.map((x: string) => <span key={x} className="chip mono">{x}</span>)}</div>)}</div></Panel>}
  </Grid>;
}

