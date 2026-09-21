import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Activity, AlertTriangle, Gauge, Server, Timer } from "lucide-react";
import { api, get, post } from "../api";
import { useState } from "react";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Badge, Btn, Card, Empty, Kpi, Modal, Table, hhmm } from "../components/ui";

const SEV = ["CRITICAL", "ERROR", "WARN", "INFO", "DEBUG"] as const;
/** services come as {name: n} or as [[name, n], …] depending on the endpoint */
function topServices(v: any): [string, number][] {
  const pairs: [string, number][] = Array.isArray(v) ? v.map((x: any) => (Array.isArray(x) ? [x[0], x[1]] : [x.service ?? x.name, x.n ?? x.count ?? x.events])) : Object.entries(v ?? {}) as any;
  return pairs.sort((a, b) => b[1] - a[1]).slice(0, 8);
}
const SEV_COLOR: Record<string, string> = { CRITICAL: "#f87171", ERROR: "#fb923c", WARN: "#fbbf24", INFO: "#60a5fa", DEBUG: "#64748b" };

export default function Ops() {
  const { t, lang } = useT(); const { can } = useAuth(); const qc = useQueryClient();
  const stats = useQuery({ queryKey: ["live-stats"], queryFn: () => get("/live/stats?window=15"), refetchInterval: 5000 });
  const slo = useQuery({ queryKey: ["live-slo"], queryFn: () => get("/live/slo?window=15"), refetchInterval: 5000 });
  const metrics = useQuery({ queryKey: ["live-metrics"], queryFn: () => get("/live/metrics?window=15"), refetchInterval: 10000 });
  const hosts = useQuery({ queryKey: ["live-hosts"], queryFn: () => get("/live/hosts"), refetchInterval: 10000 });
  const events = useQuery({ queryKey: ["live-events"], queryFn: () => get("/live/events?n=25"), refetchInterval: 5000 });
  const an = useQuery({ queryKey: ["an-stats"], queryFn: () => get("/anomalies/stats"), refetchInterval: 15000 });
  const sys = useQuery({ queryKey: ["sys-status-lite"], queryFn: () => get("/system/status"), enabled: can("sys.status"), refetchInterval: 15000 });
  const files = useQuery({ queryKey: ["live-files"], queryFn: () => get("/live/files?window=60"), refetchInterval: 15000 });
  const [tail, setTail] = useState<any>(null); const [win, setWin] = useState(60);
  const report = async () => { const html = await api<string>(`/live/report?window=${win}&lang=${lang}`, { text: true }); const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([html], { type: "text/html" })); a.download = `watchover-slo-${win}m.html`; a.click(); };
  const s = stats.data, sl = slo.data, m = metrics.data;
  const simOn = !!sys.data?.live?.simulator;
  const perMin: Record<string, any> = {};
  for (const r of s?.rows ?? []) { const k = hhmm(r.minute); perMin[k] = perMin[k] || { minute: k }; perMin[k][r.severity] = r.events; }
  const series = Object.values(perMin);
  const empty = !s?.total;
  const toggleSim = async () => { await post("/live/simulate", { on: !simOn }); qc.invalidateQueries(); };
  return (
    <div className="stack">
      <div className="row between"><div><h2>{t("ops_title")}</h2><span className="muted small">{(hosts.data?.agents ?? []).join(", ") || "-"}</span></div>
        <div className="row">{can("act.report") && <><select className="input" style={{ width: 120 }} value={win} onChange={(e) => setWin(Number(e.target.value))}>{[15, 60, 240, 1440, 10080, 43200].map((w) => <option key={w} value={w}>{w < 60 ? `${w} min` : w < 1440 ? `${w / 60} h` : `${w / 1440} d`}</option>)}</select><Btn onClick={report}>📄 {t("ops_report")}</Btn></>}
          {can("act.sim") && <Btn kind={simOn ? "primary" : ""} onClick={toggleSim} title={t("ops_sim_help")}>🧪 {t("ops_sim")} {simOn ? "ON" : "OFF"}</Btn>}
          {can("page.data") && <Btn onClick={async () => { await post("/live/analyze"); }} disabled={empty}>{t("ops_analyze")}</Btn>}</div></div>
      <div className="grid k4">
        <Kpi value={(s?.total ?? 0).toLocaleString()} label={t("ops_events15")} sub={`${s?.recent ?? 0} / min`} icon={<Activity size={18} color="#2dd4bf" />} />
        <Kpi value={s?.errors ?? 0} label={t("ops_errors")} sub={`${Object.keys(s?.services ?? {}).length} ${t("service").toLowerCase()}`} accent="#f87171" icon={<AlertTriangle size={18} color="#f87171" />} />
        <Kpi value={sl?.availability != null ? `${(sl.availability * 100).toFixed(2)}%` : "-"} label={t("ops_avail")} sub={sl?.p95 != null ? `${t("ops_p95")} ${sl.p95} ms` : " "} accent="#60a5fa" icon={<Gauge size={18} color="#60a5fa" />} />
        <Kpi value={Object.keys(hosts.data?.hosts ?? {}).length} label={t("ops_hosts")} sub={<Link to="/anomalies">{an.data?.open ?? 0} {t("ops_anom_open")}</Link>} accent="#a78bfa" icon={<Server size={18} color="#a78bfa" />} />
      </div>
      {empty ? <Empty>{t("ops_no_data")}</Empty> : (
        <div className="grid k2">
          <Card title={t("ops_per_min")}>
            <div style={{ height: 220 }}><ResponsiveContainer><AreaChart data={series}><CartesianGrid stroke="rgba(148,163,184,0.12)" vertical={false} /><XAxis dataKey="minute" stroke="#64748b" fontSize={11} /><YAxis stroke="#64748b" fontSize={11} width={34} />
              <Tooltip contentStyle={{ background: "#0b1220", border: "1px solid #334155", borderRadius: 10 }} />{SEV.map((k) => <Area key={k} type="monotone" dataKey={k} stackId="1" stroke={SEV_COLOR[k]} fill={SEV_COLOR[k]} fillOpacity={0.35} />)}</AreaChart></ResponsiveContainer></div>
          </Card>
          <Card title={t("ops_top_services")}>
            <Table cols={[{ k: "service", label: t("service") }, { k: "n", label: t("count"), num: true }]} rows={topServices(s?.services).map(([service, n]) => ({ id: service, service, n }))} />
          </Card>
          <Card title={t("ops_metrics")}>
            {m?.per_host && Object.keys(m.per_host).length ? <Table cols={[{ k: "host", label: t("host") }, { k: "env", label: t("env") }, ...["cpu", "memory", "disk", "gpu"].map((k) => ({ k, label: k, num: true, render: (r: any) => (r[k] == null ? "-" : `${Math.round(r[k])}%`) }))]}
              rows={Object.entries(m.per_host).map(([host, v]: any) => ({ id: host, host, ...v }))} /> : <Empty />}
            {!!m?.breaches?.length && <div className="row" style={{ marginTop: 8 }}>{m.breaches.map((b: any, i: number) => <Badge key={i} v="critical" label={`${b.host} ${b.metric} ${Math.round(b.value)}% · ${t("ops_breach")}`} />)}</div>}
          </Card>
          <Card title={t("ops_recent")}>
            <Table cols={[{ k: "timestamp", label: t("time"), render: (r) => <span className="mono">{r.timestamp.slice(11, 19)}</span> }, { k: "severity", label: t("severity"), render: (r) => <Badge v={r.severity} /> },
              { k: "host", label: t("host") }, { k: "service", label: t("service") }, { k: "message", label: t("message"), render: (r) => <span className="mono">{r.message.slice(0, 90)}</span> }]} rows={(events.data ?? []).slice().reverse().map((e: any, i: number) => ({ id: i, ...e }))} />
          </Card>
        </div>)}
      {!!files.data?.length && <Card title={t("ops_files")}><Table cols={[{ k: "source", label: t("ds_files"), render: (r) => <a href="#" onClick={(e) => { e.preventDefault(); setTail(r); }}><b>{r.source}</b></a> }, { k: "host", label: t("host") }, { k: "agent", label: t("ops_agents") }, { k: "events", label: t("ds_events"), num: true }, { k: "errors", label: "ERROR+", num: true }, { k: "last", label: t("time"), render: (r) => <span className="muted small">{String(r.last ?? "").slice(11, 19)}</span> }]} rows={files.data.map((f: any, i: number) => ({ id: i, ...f }))} /></Card>}
      {tail && <TailModal f={tail} onClose={() => setTail(null)} />}
    </div>
  );
}

function TailModal({ f, onClose }: { f: any; onClose: () => void }) {
  const { t } = useT(); const [errors, setErrors] = useState(false);
  const q = useQuery({ queryKey: ["lines", f.agent, f.source, errors], queryFn: () => get(`/live/lines?agent=${encodeURIComponent(f.agent)}&source=${encodeURIComponent(f.source)}&n=400&errors=${errors}`), refetchInterval: 5000 });
  return <Modal wide title={<span>{f.host} · <span className="mono">{f.source}</span></span>} onClose={onClose}>
    <label className="check"><input type="checkbox" checked={errors} onChange={(e) => setErrors(e.target.checked)} /> {t("ops_errors_only")} · {q.data?.length ?? 0} {t("ops_lines")}</label>
    <pre className="code" style={{ maxHeight: 520 }}>{(q.data ?? []).map((l: any) => `${l.timestamp.slice(11, 19)} ${l.severity.padEnd(8)} ${l.message}`).join("\n")}</pre></Modal>;
}
