import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { get } from "../../api";
import { useT } from "../../i18n";
import { Badge, Btn, Card, Empty, Kpi, Modal, Table, hhmm } from "../ui";
import { useAuth } from "../../auth";

/** The detail behind a headline number (availability, latency, infrastructure metrics, log files) for the live window,
 *  opened from the operations screen. */
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

export type Kind = "rate" | "errors" | "availability" | "sla" | "budget" | "p95" | "cpu" | "memory" | "disk" | "gpu" | "files";

export function DetailPanel({ kind, sq, env, host, sl, m, s, files, onClose }: { kind: Kind; sq: string; env: string; host: string; sl: any; m: any; s: any; files: any[]; onClose: () => void }) {
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
