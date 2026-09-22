import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Activity, AlertTriangle, Gauge, Server } from "lucide-react";
import { api, get, post } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Badge, Btn, Card, Empty, Kpi, Modal, Table, Spark, hhmm } from "../components/ui";

const SEV = ["CRITICAL", "ERROR", "WARN", "INFO", "DEBUG"] as const;
const SEV_COLOR: Record<string, string> = { CRITICAL: "#f87171", ERROR: "#fb923c", WARN: "#fbbf24", INFO: "#60a5fa", DEBUG: "#64748b" };
const THR: Record<string, number> = { cpu: 85, gpu: 95, memory: 90, disk: 90 };
const PALETTE = ["#2dd4bf", "#60a5fa", "#a78bfa", "#fbbf24", "#f87171", "#34d399", "#fb923c", "#8b98ad"];
const TIP = { contentStyle: { background: "#0b1220", border: "1px solid #334155", borderRadius: 10 } };
/** services come as {name: n} or as [[name, n], …] depending on the endpoint */
function topServices(v: any, n = 8): [string, number][] {
  const pairs: [string, number][] = Array.isArray(v) ? v.map((x: any) => (Array.isArray(x) ? [x[0], x[1]] : [x.service ?? x.name, x.n ?? x.count ?? x.events])) : Object.entries(v ?? {}) as any;
  return pairs.sort((a, b) => b[1] - a[1]).slice(0, n);
}
const pct = (v: any, d = 2) => (v == null ? "-" : `${(v * 100).toFixed(d)}%`);

type Kind = "rate" | "errors" | "availability" | "sla" | "budget" | "p95" | "cpu" | "memory" | "disk" | "gpu" | "files";

export default function Ops() {
  const { t, lang } = useT(); const { can } = useAuth(); const qc = useQueryClient();
  const [env, setEnv] = useState(""); const [hostPick, setHostPick] = useState<string[]>([]); const [detail, setDetail] = useState<Kind | null>(null); const [win, setWin] = useState(60);
  const scope = new URLSearchParams(); if (env) scope.set("env", env); if (hostPick.length) scope.set("host", hostPick.join(","));
  const sq = scope.toString() ? `&${scope}` : "";
  const stats = useQuery({ queryKey: ["live-stats", sq], queryFn: () => get(`/live/stats?window=15${sq}`), refetchInterval: 5000 });
  const slo = useQuery({ queryKey: ["live-slo", sq], queryFn: () => get(`/live/slo?window=15${sq}`), refetchInterval: 5000 });
  const metrics = useQuery({ queryKey: ["live-metrics", sq], queryFn: () => get(`/live/metrics?window=15${sq}`), refetchInterval: 10000 });
  const hosts = useQuery({ queryKey: ["live-hosts"], queryFn: () => get("/live/hosts"), refetchInterval: 10000 });
  const events = useQuery({ queryKey: ["live-events", sq], queryFn: () => get(`/live/events?n=25${sq}`), refetchInterval: 5000 });
  const files = useQuery({ queryKey: ["live-files", sq], queryFn: () => get(`/live/files?window=60${sq}`), refetchInterval: 15000 });
  const an = useQuery({ queryKey: ["an-stats"], queryFn: () => get("/anomalies/stats"), refetchInterval: 15000 });
  const sys = useQuery({ queryKey: ["sys-status-lite"], queryFn: () => get("/system/status"), enabled: can("sys.status"), refetchInterval: 15000 });
  const tickets = useQuery({ queryKey: ["ops-tickets"], queryFn: () => get("/itsm/tickets"), enabled: can("page.itsm"), refetchInterval: 60000 });
  const s = stats.data, sl = slo.data, m = metrics.data;
  const simOn = !!sys.data?.live?.simulator;
  const series = useMemo(() => { const perMin: Record<string, any> = {}; for (const r of s?.rows ?? []) { const k = hhmm(r.minute); perMin[k] = perMin[k] || { minute: k }; perMin[k][r.severity] = r.events; } return Object.values(perMin); }, [s]);
  const empty = !s?.total;
  const hostNames = Object.keys(hosts.data?.hosts ?? {}).filter((h) => !env || hosts.data.hosts[h] === env);
  const toggleSim = async () => { await post("/live/simulate", { on: !simOn }); qc.invalidateQueries(); };
  const report = async () => { const html = await api<string>(`/live/report?window=${win}&lang=${lang}${sq}`, { text: true }); const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([html], { type: "text/html" })); a.download = `watchover-slo-${win}m.html`; a.click(); };
  const pick = (k: Kind) => setDetail((d) => (d === k ? null : k));
  const metricSeries = (k: string) => { const rows = (m?.per_minute ?? []).filter((r: any) => r.metric === k); const byMin: Record<string, number[]> = {}; for (const r of rows) (byMin[hhmm(r.minute)] ||= []).push(r.value); return Object.values(byMin).map((v) => v.reduce((a, b) => a + b, 0) / v.length).slice(-15); };
  const badFiles = (files.data ?? []).filter((f: any) => f.errors > 0).length;
  const clickable = (k: Kind) => ({ onClick: () => pick(k), style: { cursor: "pointer", outline: detail === k ? "2px solid rgba(45,212,191,0.6)" : undefined, borderRadius: 14 } });
  return (
    <div className="stack">
      <div className="row between"><div><h2>{t("ops_title")}</h2><span className="muted small">{(hosts.data?.agents ?? []).join(", ") || "-"}</span></div>
        <div className="row">{can("act.report") && <><select className="input" style={{ width: 120 }} value={win} onChange={(e) => setWin(Number(e.target.value))}>{[15, 60, 240, 1440, 10080, 43200].map((w) => <option key={w} value={w}>{w < 60 ? `${w} min` : w < 1440 ? `${w / 60} h` : `${w / 1440} d`}</option>)}</select><Btn onClick={report}>📄 {t("ops_report")}</Btn></>}
          {can("act.connect") && <Link className="btn" to="/connections">🔗 {t("ops_connect")}</Link>}
          {can("act.sim") && <Btn kind={simOn ? "primary" : ""} onClick={toggleSim} title={t("ops_sim_help")}>🧪 {t("ops_sim")} {simOn ? "ON" : "OFF"}</Btn>}
          {can("page.data") && <Btn onClick={async () => { await post("/live/analyze"); }} disabled={empty}>{t("ops_analyze")}</Btn>}</div></div>
      {/* scope: environment and hosts; environment summary chips */}
      <div className="row">
        <select className="input" style={{ width: 170 }} value={env} onChange={(e) => { setEnv(e.target.value); setHostPick([]); }}><option value="">{t("ops_scope_env")}: {t("ops_all")}</option>{Object.keys(hosts.data?.environments ?? {}).map((e) => <option key={e} value={e}>{e}</option>)}</select>
        <select className="input" style={{ width: 200 }} value="" onChange={(e) => { const h = e.target.value; if (h && !hostPick.includes(h)) setHostPick([...hostPick, h]); }} title={t("ops_scope_host")}><option value="">{t("ops_scope_host")}: {hostPick.length ? hostPick.length : t("ops_all")}</option>{hostNames.filter((h) => !hostPick.includes(h)).map((h) => <option key={h} value={h}>{h}</option>)}</select>
        {hostPick.map((h) => <span key={h} className="chip" style={{ cursor: "pointer" }} onClick={() => setHostPick(hostPick.filter((x) => x !== h))}><span className="d" style={{ background: "#60a5fa" }} />{h} ✕</span>)}
        {hostPick.length > 1 && <span className="muted small">{t("ops_multi", { n: hostPick.length })}</span>}
        {(env || hostPick.length > 0) && <Btn sm onClick={() => { setEnv(""); setHostPick([]); }}>✕ {t("ops_scope_reset")}</Btn>}
        <div className="grow" />
        <div className="row">{(hosts.data?.summary ?? []).map((e: any) => <span key={e.environment} className="chip" onClick={() => setEnv(e.environment === env ? "" : e.environment)} style={{ cursor: "pointer" }} title={`${t("ops_avail")} ${e.availability != null ? `${(e.availability * 100).toFixed(2)}%` : "-"}`}><span className="d" style={{ background: e.errors ? "#f87171" : "#34d399" }} />{e.environment} · {e.hosts} {t("host").toLowerCase()} · {e.events ?? 0} / {e.errors ?? 0}</span>)}</div>
      </div>
      <div className="grid k4">
        <div {...clickable("rate")}><Kpi value={(s?.total ?? 0).toLocaleString()} label={t("ops_events15")} sub={`${s?.recent ?? 0} / min · ${t("ops_detail")} ▾`} icon={<Activity size={18} color="#2dd4bf" />} /></div>
        <div {...clickable("errors")}><Kpi value={s?.errors ?? 0} label={t("ops_errors")} sub={`${topServices(s?.services).length} ${t("service").toLowerCase()} · ${t("ops_detail")} ▾`} accent="#f87171" icon={<AlertTriangle size={18} color="#f87171" />} /></div>
        <div {...clickable("availability")}><Kpi value={sl?.availability != null ? pct(sl.availability) : "-"} label={t("ops_avail")} sub={`${t("od_target")} ${pct(sl?.slo?.availability, 1)} · ${t("od_budget")} ${sl?.error_budget != null ? pct(sl.error_budget, 0) : "-"} · ${t("ops_detail")} ▾`} accent="#60a5fa" icon={<Gauge size={18} color="#60a5fa" />} /></div>
        <div {...clickable("p95")}><Kpi value={sl?.p95 != null ? `${sl.p95} ms` : "-"} label={t("ops_p95")} sub={`${Object.keys(hosts.data?.hosts ?? {}).length} ${t("ops_hosts")} · ${an.data?.open ?? 0} ${t("ops_anom_open")} · ${t("ops_detail")} ▾`} accent="#a78bfa" icon={<Server size={18} color="#a78bfa" />} /></div>
      </div>
      {/* infrastructure tiles: click for the per-host detail */}
      {!!m?.summary && <div className="grid tiles" style={{ gridTemplateColumns: "repeat(5, minmax(0, 1fr))" }}>
        {["cpu", "memory", "disk", "gpu"].map((k, i) => { const sm = m.summary[k] ?? {}; const worst = sm.max != null && sm.max >= THR[k]; return <div key={k} {...clickable(k as Kind)}><div className="card kpi" style={{ ["--acc" as any]: worst ? "#f87171" : PALETTE[i], padding: "10px 12px" }}><div className="lb">{k}</div><div className="row between"><div className="v" style={{ fontSize: 19 }}>{sm.avg != null ? `${Math.round(sm.avg)}%` : "-"}</div><div style={{ width: 120 }}><Spark data={metricSeries(k)} color={worst ? "#f87171" : PALETTE[i]} /></div></div><div className="sub">{sm.max != null ? `${t("od_max")} ${Math.round(sm.max)}% · ${sm.worst ?? ""} · ${sm.hosts} ${t("host").toLowerCase()}` : "-"}</div></div></div>; })}
        <div {...clickable("files")}><div className="card kpi" style={{ ["--acc" as any]: badFiles ? "#fb923c" : "#8b98ad", padding: "10px 12px" }}><div className="lb">{t("od_files")}</div><div className="v" style={{ fontSize: 19 }}>{files.data?.length ? `${badFiles}/${files.data.length}` : "-"}</div><div className="sub">{t("ops_files_tile")} · {t("ops_detail")} ▾</div></div></div>
      </div>}
      {detail && <DetailPanel kind={detail} sq={sq} env={env} host={hostPick.join(", ")} sl={sl} m={m} s={s} files={files.data ?? []} onClose={() => setDetail(null)} />}
      {empty ? <Empty>{t("ops_no_data")}</Empty> : (
        <div className="grid k2">
          <Card title={t("ops_per_min")}>
            <div style={{ height: 220 }}><ResponsiveContainer><AreaChart data={series}><CartesianGrid stroke="rgba(148,163,184,0.12)" vertical={false} /><XAxis dataKey="minute" stroke="#64748b" fontSize={11} /><YAxis stroke="#64748b" fontSize={11} width={34} />
              <Tooltip {...TIP} />{SEV.map((k) => <Area key={k} type="monotone" dataKey={k} stackId="1" stroke={SEV_COLOR[k]} fill={SEV_COLOR[k]} fillOpacity={0.35} />)}</AreaChart></ResponsiveContainer></div>
          </Card>
          <Card title={t("ops_top_services")}><Table cols={[{ k: "service", label: t("service") }, { k: "n", label: t("count"), num: true }]} rows={topServices(s?.services).map(([service, n]) => ({ id: service, service, n }))} /></Card>
          <Card title={t("ops_metrics")}>
            {m?.per_host && Object.keys(m.per_host).length ? <Table cols={[{ k: "host", label: t("host") }, { k: "env", label: t("env") }, ...["cpu", "memory", "disk", "gpu"].map((k) => ({ k, label: k, num: true, render: (r: any) => (r[k] == null ? "-" : <span style={{ color: r[k] >= THR[k] ? "#f87171" : undefined }}>{Math.round(r[k])}%</span>) }))]}
              rows={Object.entries(m.per_host).map(([host, v]: any) => ({ id: host, host, ...v }))} /> : <Empty />}
            {!!m?.breaches?.length && <div className="row" style={{ marginTop: 8 }}>{m.breaches.map((b: any, i: number) => <Badge key={i} v="critical" label={`${b.host} ${b.metric} ${Math.round(b.value)}% · ${t("ops_breach")}`} />)}</div>}
          </Card>
          <Card title={t("ops_recent")}>
            <Table cols={[{ k: "timestamp", label: t("time"), render: (r) => <span className="mono">{r.timestamp.slice(11, 19)}</span> }, { k: "severity", label: t("severity"), render: (r) => <Badge v={r.severity} /> },
              { k: "host", label: t("host") }, { k: "service", label: t("service") }, { k: "message", label: t("message"), render: (r) => <span className="mono">{r.message.slice(0, 90)}</span> }]} rows={(events.data ?? []).slice().reverse().map((e: any, i: number) => ({ id: i, ...e }))} />
          </Card>
        </div>)}
      {!!files.data?.length && <Card title={t("ops_files")} right={<Btn sm onClick={() => pick("files")}>{t("ops_detail")}</Btn>}><Table cols={[{ k: "file", label: t("ds_files"), render: (r) => <b>{r.file}</b> }, { k: "host", label: t("host") }, { k: "agent", label: t("ops_agents") }, { k: "total", label: t("ds_events"), num: true }, { k: "errors", label: "ERROR+", num: true }, { k: "last_error", label: t("time"), render: (r) => <span className="muted small">{String(r.last_error ?? r.last ?? "").slice(11, 19)}</span> }]} rows={files.data.map((f: any, i: number) => ({ id: i, ...f }))} /></Card>}
      {can("page.itsm") && !!tickets.data?.tickets?.length && <Card title={t("ops_tickets")} right={<Link className="btn sm" to="/itsm">ITSM →</Link>}>
        <Table cols={[{ k: "id", label: "ID", render: (r) => <b>{r.id}</b> }, { k: "priority", label: t("act_priority"), render: (r) => <Badge v={r.priority} /> }, { k: "status", label: t("status") }, { k: "title", label: t("act_title_f") }, { k: "service", label: t("service") }, { k: "relevance", label: t("itsm_rel"), num: true, render: (r) => r.relevance.toFixed(2) }, { k: "related", label: t("itsm_related"), render: (r) => <span className="small">{(r.related ?? []).join(", ")}</span> }]}
          rows={tickets.data.tickets.slice().sort((a: any, b: any) => b.relevance - a.relevance).slice(0, 5)} /></Card>}
    </div>
  );
}

function DetailPanel({ kind, sq, env, host, sl, m, s, files, onClose }: { kind: Kind; sq: string; env: string; host: string; sl: any; m: any; s: any; files: any[]; onClose: () => void }) {
  const { t } = useT();
  const det = useQuery({ queryKey: ["slo-detail", sq], queryFn: () => get(`/live/slo?window=15&detail=true${sq}`), refetchInterval: 10000, enabled: ["availability", "sla", "budget", "p95", "errors"].includes(kind) });
  const disc = useQuery({ queryKey: ["discoveries"], queryFn: () => get("/live/discoveries"), enabled: kind === "files" });
  const [file, setFile] = useState<any>(null);
  const d = det.data;
  const scopeLabel = [env, host].filter(Boolean).join(" · ") || t("ops_all");
  const title = t("od_" + kind);
  const metric = ["cpu", "memory", "disk", "gpu"].includes(kind);
  // per-host time series for one metric
  const metricRows = (m?.per_minute ?? []).filter((r: any) => r.metric === kind);
  const byMin: Record<string, any> = {}; const hostsSeen: string[] = [];
  for (const r of metricRows) { const k = hhmm(r.minute); byMin[k] = byMin[k] || { minute: k }; byMin[k][r.host] = r.value; if (!hostsSeen.includes(r.host)) hostsSeen.push(r.host); }
  const perHost = hostsSeen.map((h) => { const v = metricRows.filter((r: any) => r.host === h).map((r: any) => r.value); const mx = Math.max(...v); return { id: h, host: h, env: metricRows.find((r: any) => r.host === h)?.env, latest: v[v.length - 1], avg: v.reduce((a: number, b: number) => a + b, 0) / v.length, max: mx, threshold: THR[kind], status: mx >= THR[kind] ? "breach" : mx >= THR[kind] - 15 ? "warn" : "ok" }; }).sort((a, b) => b.max - a.max);
  // rate by service
  const rateRows = useMemo(() => { const out: Record<string, any> = {}; for (const r of s?.rows ?? []) { const k = hhmm(r.minute); out[k] = out[k] || { minute: k, total: 0 }; out[k].total += r.events; } return Object.values(out); }, [s]);
  const perMinute = (d?.per_minute ?? []).map((r: any) => ({ ...r, minute: hhmm(r.minute) }));
  return <Card title={<span>{title} <span className="muted small">· {t("od_window")} · {scopeLabel}</span></span>} right={<Btn sm kind="ghost" onClick={onClose}>✕</Btn>}>
    {metric && (!hostsSeen.length ? <Empty /> : <div className="stack">
      <div style={{ height: 240 }}><ResponsiveContainer><LineChart data={Object.values(byMin)}><CartesianGrid stroke="rgba(148,163,184,0.12)" vertical={false} /><XAxis dataKey="minute" stroke="#64748b" fontSize={11} /><YAxis stroke="#64748b" fontSize={11} width={34} domain={[0, 100]} /><Tooltip {...TIP} /><ReferenceLine y={THR[kind]} stroke="#f87171" strokeDasharray="4 4" />
        {hostsSeen.slice(0, 12).map((h, i) => <Line key={h} type="monotone" dataKey={h} stroke={PALETTE[i % PALETTE.length]} dot={false} strokeWidth={1.6} />)}</LineChart></ResponsiveContainer></div>
      <Table cols={[{ k: "host", label: t("host") }, { k: "env", label: t("env") }, { k: "latest", label: t("od_latest"), num: true, render: (r) => `${Math.round(r.latest)}%` }, { k: "avg", label: t("od_avg"), num: true, render: (r) => `${r.avg.toFixed(1)}%` }, { k: "max", label: t("od_max"), num: true, render: (r) => `${Math.round(r.max)}%` }, { k: "threshold", label: t("od_threshold"), num: true, render: (r) => `${r.threshold}%` }, { k: "status", label: t("status"), render: (r) => <Badge v={r.status === "breach" ? "critical" : r.status === "warn" ? "high" : "active"} label={r.status} /> }]} rows={perHost} />
      {!!m?.breaches?.filter((b: any) => b.metric === kind).length && <div><b>{t("od_breaches")}</b> · {m.breaches.filter((b: any) => b.metric === kind).slice(0, 8).map((b: any) => `${b.host} ${Math.round(b.value)}%`).join(", ")}</div>}</div>)}
    {kind === "rate" && <div className="grid k2">
      <div style={{ height: 220 }}><ResponsiveContainer><AreaChart data={rateRows}><CartesianGrid stroke="rgba(148,163,184,0.12)" vertical={false} /><XAxis dataKey="minute" stroke="#64748b" fontSize={11} /><YAxis stroke="#64748b" fontSize={11} width={34} /><Tooltip {...TIP} /><Area type="monotone" dataKey="total" stroke="#2dd4bf" fill="#2dd4bf" fillOpacity={0.3} /></AreaChart></ResponsiveContainer></div>
      <div style={{ height: 220 }}><ResponsiveContainer><BarChart layout="vertical" data={topServices(s?.services).map(([service, n]) => ({ service, n }))}><XAxis type="number" stroke="#64748b" fontSize={11} /><YAxis type="category" dataKey="service" width={130} stroke="#64748b" fontSize={11} /><Tooltip {...TIP} /><Bar dataKey="n" fill="#2dd4bf" /></BarChart></ResponsiveContainer></div></div>}
    {kind === "errors" && <Table cols={[{ k: "ts", label: t("time"), render: (r) => <span className="mono">{String(r.ts).slice(11, 19)}</span> }, { k: "severity", label: t("severity"), render: (r) => <Badge v={r.severity} /> }, { k: "service", label: t("service") }, { k: "host", label: t("host") }, { k: "message", label: t("message"), render: (r) => <span className="mono">{r.message.slice(0, 120)}</span> }]} rows={(d?.recent ?? []).map((r: any, i: number) => ({ id: i, ...r }))} />}
    {["availability", "sla", "budget"].includes(kind) && d && <div className="stack">
      <div className="grid k4">
        <Kpi value={pct(sl?.availability)} label={t("od_availability")} sub={`${t("od_target")} ${pct(sl?.slo?.availability, 1)}`} accent="#60a5fa" /><Kpi value={pct(sl?.sla?.availability, 1)} label={t("od_sla")} sub={sl?.availability != null && sl?.sla?.availability != null && sl.availability < sl.sla.availability ? "breach" : "ok"} accent="#a78bfa" />
        <Kpi value={sl?.error_budget != null ? pct(sl.error_budget, 0) : "-"} label={t("od_budget")} sub={`${d.errors} / ${d.allowed_errors} ${t("od_allowed")}`} accent={(sl?.error_budget ?? 1) > 0.25 ? "#2dd4bf" : "#f87171"} /><Kpi value={d.breach_minutes} label={t("od_breach_min")} accent="#fbbf24" sub={`${d.total} ${t("ds_events")}`} /></div>
      <div className="grid k2">
        <Card title={kind === "budget" ? t("od_burn") : t("od_minute_avail")}><div style={{ height: 200 }}><ResponsiveContainer><LineChart data={perMinute}><CartesianGrid stroke="rgba(148,163,184,0.12)" vertical={false} /><XAxis dataKey="minute" stroke="#64748b" fontSize={11} /><YAxis stroke="#64748b" fontSize={11} width={44} domain={[0, 1]} tickFormatter={(v) => `${Math.round(v * 100)}%`} /><Tooltip {...TIP} formatter={(v: any) => pct(v)} />
          <Line type="monotone" dataKey={kind === "budget" ? "budget_left" : "availability"} stroke={kind === "budget" ? "#2dd4bf" : "#60a5fa"} dot={false} strokeWidth={2} />{kind !== "budget" && sl?.slo?.availability && <ReferenceLine y={sl.slo.availability} stroke="#f87171" strokeDasharray="4 4" />}</LineChart></ResponsiveContainer></div></Card>
        <Card title={t("od_err_by_service")}><div style={{ height: 200 }}><ResponsiveContainer><BarChart layout="vertical" data={(d.by_service ?? []).slice(0, 8).map((x: any) => ({ service: x[0], errors: x[1] }))}><XAxis type="number" stroke="#64748b" fontSize={11} /><YAxis type="category" dataKey="service" width={130} stroke="#64748b" fontSize={11} /><Tooltip {...TIP} /><Bar dataKey="errors" fill="#f87171" /></BarChart></ResponsiveContainer></div>
          {!!d.by_host?.length && <div className="small muted">{t("od_err_by_host")} · {d.by_host.slice(0, 8).map((x: any) => `${x[0]} (${x[1]})`).join(", ")}</div>}</Card></div>
      <Card title={t("od_what_lowers")}><Table cols={[{ k: "severity", label: t("severity"), render: (r) => <Badge v={r.severity ?? "ERROR"} /> }, { k: "template", label: t("sig_template"), render: (r) => <span className="mono">{String(r.template ?? r[0] ?? "").slice(0, 110)}</span> }, { k: "count", label: t("count"), num: true, render: (r) => r.count ?? r[1] }, { k: "services", label: t("service"), render: (r) => Array.isArray(r.services) ? r.services.join(", ") : r.services ?? "" }]} rows={(d.templates ?? []).map((x: any, i: number) => ({ id: i, ...(Array.isArray(x) ? { template: x[0], count: x[1] } : x) }))} /></Card></div>}
    {kind === "p95" && d && <div className="grid k2">
      <Card title={t("od_p95")}><div style={{ height: 220 }}><ResponsiveContainer><BarChart layout="vertical" data={(d.latency ?? []).slice(0, 10)}><XAxis type="number" stroke="#64748b" fontSize={11} /><YAxis type="category" dataKey="service" width={130} stroke="#64748b" fontSize={11} /><Tooltip {...TIP} /><Bar dataKey="p95" fill="#fb923c" /></BarChart></ResponsiveContainer></div>
        <Table cols={[{ k: "service", label: t("service") }, { k: "p95", label: "p95 ms", num: true }, { k: "n", label: t("od_samples"), num: true }]} rows={(d.latency ?? []).map((x: any) => ({ id: x.service, ...x }))} /></Card>
      <Card title={t("od_slowest")}><Table cols={[{ k: "ts", label: t("time"), render: (r) => <span className="mono">{String(r.ts).slice(11, 19)}</span> }, { k: "ms", label: "ms", num: true }, { k: "service", label: t("service") }, { k: "host", label: t("host") }, { k: "message", label: t("message"), render: (r) => <span className="mono">{String(r.message).slice(0, 90)}</span> }]} rows={(d.slowest ?? []).map((x: any, i: number) => ({ id: i, ...x }))} /></Card></div>}
    {kind === "files" && <div className="stack">
      {!!disc.data && Object.keys(disc.data).length > 0 && <Card title={t("od_discovery")}>{Object.entries(disc.data).map(([agent, apps]: any) => <div key={agent} className="small"><b>{agent}</b> · {apps.map((a: any) => `${a.app} (${a.files.length})`).join(" · ")}<details><summary className="muted">files</summary><pre className="code">{apps.flatMap((a: any) => a.files).join("\n")}</pre></details></div>)}</Card>}
      <Table cols={[{ k: "file", label: t("ds_files"), render: (r) => <a href="#" onClick={(e) => { e.preventDefault(); setFile(r); }}><b>{r.file}</b></a> }, { k: "host", label: t("host") }, { k: "agent", label: t("ops_agents") }, { k: "total", label: t("ds_events"), num: true }, { k: "errors", label: "ERROR+", num: true }, { k: "services", label: t("service"), render: (r) => (r.services ?? []).join(", ") }, { k: "last_msg", label: t("message"), render: (r) => <span className="mono small">{String(r.last_msg ?? "").slice(0, 80)}</span> }]} rows={files.map((f: any, i: number) => ({ id: i, ...f }))} />
      {file && <TailModal f={file} onClose={() => setFile(null)} />}</div>}
  </Card>;
}

function TailModal({ f, onClose }: { f: any; onClose: () => void }) {
  const { t } = useT(); const { can } = useAuth(); const [errors, setErrors] = useState(false);
  const q = useQuery({ queryKey: ["lines", f.agent, f.file, errors], queryFn: () => get(`/live/lines?agent=${encodeURIComponent(f.agent)}&source=${encodeURIComponent(f.file)}&n=400&errors=${errors}${f.host && f.host !== "-" ? `&host=${encodeURIComponent(f.host)}` : ""}`), refetchInterval: 5000 });
  const text = (q.data ?? []).map((l: any) => `${l.timestamp.slice(11, 19)} ${l.severity.padEnd(8)} ${l.message}`).join("\n");
  const download = () => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], { type: "text/plain" })); a.download = `${f.host}_${String(f.file).split("/").pop()}.log`; a.click(); };
  return <Modal wide title={<span>{f.host} · <span className="mono">{f.file}</span></span>} onClose={onClose}>
    <div className="row"><label className="check"><input type="checkbox" checked={errors} onChange={(e) => setErrors(e.target.checked)} /> {t("ops_errors_only")} · {q.data?.length ?? 0} {t("ops_lines")}</label>{can("act.export") && <Btn sm onClick={download}>⬇ {t("files_export")}</Btn>}</div>
    <pre className="code" style={{ maxHeight: 520 }}>{text}</pre></Modal>;
}
