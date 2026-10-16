import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ArrowUpRight, Radio, ScrollText, TriangleAlert } from "lucide-react";
import { get, post } from "../../api";
import { useAuth } from "../../auth";
import { useT } from "../../i18n";
import { Badge, Btn, Card, Empty, Err, Field, fmtTs } from "../../components/ui";
import { HostTail } from "../../components/ops/HostTail";
import { LV, ms, num, pctOf, qlink } from "./common";

/** The detail tabs that reuse other pages' data: logs (explorer links and a live tail of app:<name>), traces, user experience, business
 *  flows, alarms and thresholds, events. Each needs the permission of the page it borrows from and says so when it is missing. */
function NeedPerm({ p }: { p: string }) {
  const { t } = useT();
  return <Empty>{t("ap_need_perm", { p })}</Empty>;
}

export function LogsTab({ a }: { a: any }) {
  const { t } = useT(); const { can } = useAuth();
  if (!can("page.logs")) return <NeedPerm p="page.logs" />;
  return <div className="stack">
    <Card>
      <div className="stack" style={{ gap: 8 }}>
        <div className="dim small">{t("ap_logs_lead")}</div>
        <code className="ap-q mono">{a.log_query}</code>
        <div className="row" style={{ gap: 8 }}>
          <Link className="btn sm primary" to={qlink(a.log_query)}><ScrollText size={14} /> {t("ap_logs_open")}</Link>
          <Link className="btn sm" to={qlink(a.log_query, "&live=1")}><Radio size={14} /> {t("ap_logs_live")}</Link>
          <Link className="btn sm" to={qlink(`${a.log_query} level>=error`)}><TriangleAlert size={14} /> {t("ap_logs_errors")}</Link>
        </div>
      </div>
    </Card>
    <HostTail host={a.name} query={a.log_query} title={t("ap_tail_title")} />
  </div>;
}

export function TracesTab({ a, range }: { a: any; range: number }) {
  const { t } = useT(); const { can } = useAuth();
  const [errors, setErrors] = useState(false); const [order, setOrder] = useState<"recent" | "duration">("recent");
  const q = useQuery({ queryKey: ["app-traces", a.id, range, errors, order], queryFn: () => get(`/apps/${a.id}/traces?minutes=${range}&errors=${errors}&order=${order}&limit=60`),
    enabled: can("page.apm"), refetchInterval: 30000 });
  if (!can("page.apm")) return <NeedPerm p="page.apm" />;
  const rows: any[] = q.data?.traces ?? [];
  return <Card title={t("ap_t_traces")} right={<div className="row" style={{ gap: 6 }}>
    <div className="seg sm"><button className={order === "recent" ? "on" : ""} onClick={() => setOrder("recent")}>{t("apm_o_recent")}</button>
      <button className={order === "duration" ? "on" : ""} onClick={() => setOrder("duration")}>{t("apm_o_slow")}</button></div>
    <label className="row small" style={{ gap: 4 }}><input type="checkbox" checked={errors} onChange={(e) => setErrors(e.target.checked)} />{t("apm_f_err")}</label></div>}>
    <Err e={q.error} />
    {q.isLoading ? <div className="muted">{t("loading")}</div> : !rows.length ? <Empty>{t("ap_traces_none")}</Empty> :
      <div className="table-wrap"><table className="table ap-table"><thead><tr><th>{t("apm_col_start")}</th><th>{t("apm_col_root")}</th><th className="right">{t("apm_col_dur")}</th>
        <th className="right">{t("apm_col_spans")}</th><th>{t("apm_col_services")}</th></tr></thead>
        <tbody>{rows.map((r) => <tr key={r.trace_id}>
          <td className="small">{fmtTs(r.start)}</td>
          <td><Link to={`/traces/${encodeURIComponent(r.trace_id)}`}><b>{r.root_service}</b> <span className="mono small">{r.root_operation}</span></Link>
            {r.error ? <span className="badge red" style={{ marginLeft: 6 }}>ERR</span> : null}</td>
          <td className="num">{ms(r.duration_ms)}</td><td className="num">{r.spans}</td>
          <td className="small dim">{(r.services ?? []).join(", ")}</td></tr>)}</tbody></table></div>}
  </Card>;
}

export function RumTab({ a, range }: { a: any; range: number }) {
  const { t } = useT(); const { can } = useAuth();
  if (!can("page.rum")) return <NeedPerm p="page.rum" />;
  if (!(a.rum_apps ?? []).length) return <Empty>{t("ap_rum_none")}</Empty>;
  return <div className="ap-cards2">{a.rum_apps.map((r: any) => <RumCard key={r.name} name={r.name} exists={r.exists} minutes={Math.max(range, 60)} />)}</div>;
}

function RumCard({ name, exists, minutes }: { name: string; exists: boolean; minutes: number }) {
  const { t, lang } = useT();
  const q = useQuery({ queryKey: ["app-rum", name, minutes], queryFn: () => get(`/rum/overview?app=${encodeURIComponent(name)}&minutes=${minutes}`), enabled: exists, refetchInterval: 60000 });
  const rows: any[] = q.data?.apps ?? []; const vit = q.data?.vitals?.[name] ?? {};
  return <Card title={<span className="mono">{name}</span>} right={exists ? <Link className="btn sm" to={`/rum?app=${encodeURIComponent(name)}`}>{t("ap_t_rum")} <ArrowUpRight size={13} /></Link> : null}>
    {!exists ? <Empty>{t("ap_rum_missing")}</Empty> : <Err e={q.error} />}
    {exists && (rows.length ? rows.map((r) => <div key={r.platform} className="hd-kpis" style={{ marginBottom: 8 }}>
      <div><span>{r.platform}</span><b>{num(r.sessions, lang)}<small> {t("ap_sessions").toLowerCase()}</small></b></div>
      <div><span>{t("ap_users")}</span><b>{num(r.users, lang)}</b></div>
      <div><span>{t("ap_crash_free")}</span><b className={(r.crash_free ?? 1) < 0.99 ? "bad" : ""}>{pctOf(r.crash_free, 1)}</b></div>
      <div><span>{t("ap_error_free")}</span><b className={(r.error_free ?? 1) < 0.95 ? "bad" : ""}>{pctOf(r.error_free, 1)}</b></div>
    </div>) : !q.isLoading && <Empty>{t("apm_none")}</Empty>)}
    {Object.keys(vit).length > 0 && <div className="row" style={{ gap: 6 }}>{Object.entries(vit).map(([k, v]: any) => <span key={k} className={`chip`} title={`n=${v.n}`}>
      <span className={`lvl lv-${v.grade === "good" ? "ok" : v.grade === "poor" ? "crit" : "warn"}`}><i /></span>{k} p75 {k === "cls" ? v.p75?.toFixed?.(3) : ms(v.p75)}</span>)}</div>}
  </Card>;
}

export function FlowsTab({ a, range }: { a: any; range: number }) {
  const { t } = useT(); const { can } = useAuth();
  if (!can("page.apm")) return <NeedPerm p="page.apm" />;
  if (!(a.flows ?? []).length) return <Empty>{t("ap_flows_none")}</Empty>;
  return <div className="stack">{a.flows.map((f: any) => <FlowCard key={f.id} f={f} minutes={Math.max(range, 60)} />)}</div>;
}

function FlowCard({ f, minutes }: { f: any; minutes: number }) {
  const { t, lang } = useT();
  const q = useQuery({ queryKey: ["app-flow", f.id, minutes], queryFn: () => get(`/flows/${f.id}/funnel?minutes=${minutes}`), refetchInterval: 60000 });
  const d = q.data; const steps: any[] = d?.steps ?? [];
  return <Card title={f.name} right={<Link className="btn sm" to="/flows">{t("ap_t_flows")} <ArrowUpRight size={13} /></Link>}>
    <Err e={q.error} />
    {d && <div className="dim small" style={{ marginBottom: 8 }}>{num(d.items, lang)} {t("ap_items")} · {t("ap_completion")} {pctOf(d.completion, 1)}</div>}
    <div className="stack" style={{ gap: 6 }}>{steps.map((s) => <div key={s.index} className="ap-step">
      <span className="small">{s.index + 1}. {s.name}</span>
      <span className="bt-tr"><i style={{ width: `${Math.max(2, (s.of_first ?? 0) * 100)}%`, background: "var(--s1)" }} /></span>
      <b className="small">{num(s.reached, lang)}</b>
      <span className="small">{s.stuck ? <span className="bad">{s.stuck} {t("ap_stuck")}</span> : null}{s.errors ? <span className="bad"> · {s.errors} ERR</span> : null}</span>
    </div>)}</div>
  </Card>;
}

const METRIC_KEYS = ["cpu", "memory", "disk", "error_rate", "p95_ms", "silent_min"];

export function AlarmsTab({ a }: { a: any }) {
  const { t } = useT(); const qc = useQueryClient();
  const q = useQuery({ queryKey: ["app-thr", a.id], queryFn: () => get(`/apps/${a.id}/thresholds`), refetchInterval: 30000 });
  const d = q.data ?? {}; const [f, setF] = useState({ metric: "cpu", warn: "80", crit: "90", for_min: "3" }); const [err, setErr] = useState<any>(null);
  const add = async () => {
    setErr(null);
    try {
      await post("/thresholds", { name: `${a.name} · ${f.metric}`, scope: "application", scope_value: a.name, metric: f.metric, warn: f.warn === "" ? null : Number(f.warn),
        crit: f.crit === "" ? null : Number(f.crit), for_min: Number(f.for_min || 0), channels: "email", targets: "" });
      qc.invalidateQueries({ queryKey: ["app-thr", a.id] });
    } catch (e) { setErr(e); }
  };
  return <div className="stack">
    <div className="dim small">{t("ap_prec")}</div>
    <Card title={t("ap_states")}>{!(d.states ?? []).length ? <Empty>{t("ap_states_none")}</Empty> :
      <div className="table-wrap"><table className="table ap-table"><thead><tr><th>{t("ap_col_host")}</th><th>{t("thr_metric")}</th><th>{t("ap_col_state")}</th>
        <th className="right">{t("thr_warn")} / {t("thr_crit")}</th><th>{t("fl_last")}</th></tr></thead>
        <tbody>{d.states.map((s: any) => <tr key={`${s.host}-${s.metric}`}><td className="mono">{s.host}</td><td>{t(`thr_m_${s.metric}`)}</td>
          <td><span className={`lvl lv-${LV[s.level] ?? "none"}`}><i />{t(`ap_st_${s.level}`)}</span></td>
          <td className="num">{s.value != null ? Math.round(s.value * 10) / 10 : "–"} / {s.limit_value ?? "–"}</td><td className="small">{fmtTs(s.since)}</td></tr>)}</tbody></table></div>}</Card>
    <Card title={t("ap_policies")} right={<Link className="btn sm" to="/thresholds">{t("ap_open_thr")} <ArrowUpRight size={13} /></Link>}>
      {!(d.policies ?? []).length ? <Empty>{t("ap_policies_none")}</Empty> :
        <div className="table-wrap"><table className="table ap-table"><thead><tr><th>{t("name")}</th><th>{t("thr_metric")}</th><th className="right">{t("thr_warn")}</th>
          <th className="right">{t("thr_crit")}</th><th className="right">{t("thr_for")}</th><th>{t("tmr_team")}</th><th>{t("status")}</th></tr></thead>
          <tbody>{d.policies.map((p: any) => <tr key={p.id}><td><b>{p.name}</b></td><td>{t(`thr_m_${p.metric}`)}</td><td className="num">{p.warn ?? "–"}</td><td className="num">{p.crit ?? "–"}</td>
            <td className="num">{p.for_min}</td><td className="small">{p.team || t("ap_install")}</td><td>{p.enabled ? <Badge v="active" /> : <Badge v="disabled" />}</td></tr>)}</tbody></table></div>}
      {d.can_add && <div className="ap-thr-add">
        <Field label={t("thr_metric")}><select className="input" value={f.metric} onChange={(e) => setF({ ...f, metric: e.target.value })}>
          {["cpu", "memory", "disk", "gpu", "error_rate", "errors_per_min", "p95_ms", "silent_min"].map((m) => <option key={m} value={m}>{t(`thr_m_${m}`)}</option>)}</select></Field>
        <Field label={t("thr_warn")}><input className="input" type="number" value={f.warn} onChange={(e) => setF({ ...f, warn: e.target.value })} /></Field>
        <Field label={t("thr_crit")}><input className="input" type="number" value={f.crit} onChange={(e) => setF({ ...f, crit: e.target.value })} /></Field>
        <Field label={t("thr_for")}><input className="input" type="number" min="0" value={f.for_min} onChange={(e) => setF({ ...f, for_min: e.target.value })} /></Field>
        <Btn kind="primary" onClick={add}>{t("ap_policy_add")}</Btn>
      </div>}
      <Err e={err} />
    </Card>
    <Card title={t("ap_levels")}>{!(d.overview ?? []).length ? <Empty>{t("ap_no_hosts")}</Empty> :
      <div className="table-wrap"><table className="table ap-table"><thead><tr><th>{t("ap_col_host")}</th>{METRIC_KEYS.map((m) => <th key={m} className="right">{t(`thr_m_${m}`)}</th>)}</tr></thead>
        <tbody>{d.overview.map((h: any) => <tr key={h.host}><td className="mono">{h.host}</td>{METRIC_KEYS.map((m) => { const x = h.metrics?.[m] ?? {};
          const src = x.source ? `${t(`thr_scope_${x.source.scope}`)}${x.source.value ? `: ${x.source.value}` : ""}${x.source.name ? ` (${x.source.name})` : ""}` : t("thr_no_level");
          return <td key={m} className={`num thr-cell ${x.level ?? ""}`} title={src}>{x.value != null ? Math.round(x.value * 10) / 10 : "–"}
            <div className="dim small">{x.warn ?? "–"}/{x.crit ?? "–"}{x.source?.scope === "application" ? " ◆" : ""}</div></td>; })}</tr>)}</tbody></table></div>}</Card>
    <Err e={q.error} />
  </div>;
}

export function EventsTab({ a, range }: { a: any; range: number }) {
  const { t } = useT(); const { can } = useAuth();
  const minutes = Math.max(range, 1440);
  const q = useQuery({ queryKey: ["app-events", a.id, minutes], queryFn: () => get(`/apps/${a.id}/events?minutes=${minutes}`), refetchInterval: 30000 });
  const rows: any[] = q.data?.events ?? [];
  return <Card title={t("ap_t_events")} right={can("page.anom") ? <Link className="btn sm" to="/anomalies">{t("ap_open_anoms")} <ArrowUpRight size={13} /></Link> : null}>
    <Err e={q.error} />
    {q.isLoading ? <div className="muted">{t("loading")}</div> : !rows.length ? <Empty>{t("ap_events_none")}</Empty> :
      <div className="table-wrap"><table className="table ap-table"><thead><tr><th>{t("status")}</th><th>{t("name")}</th><th>{t("ap_col_host")}</th><th>{t("ap_col_service")}</th>
        <th className="right">{t("ap_score")}</th><th>{t("fl_last")}</th></tr></thead>
        <tbody>{rows.map((r) => <tr key={r.id}><td><Badge v={r.status} /></td><td><b>{r.title}</b><div className="dim small">{r.kind}{r.metric ? ` · ${r.metric}` : ""}</div></td>
          <td className="mono small">{r.host || "–"}</td><td className="mono small">{r.service || "–"}</td><td className="num">{r.score != null ? Number(r.score).toFixed(1) : "–"}</td>
          <td className="small">{fmtTs(r.last_seen)}</td></tr>)}</tbody></table></div>}
  </Card>;
}
