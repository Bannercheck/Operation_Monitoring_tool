import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { get } from "../../api";
import { useT } from "../../i18n";
import { Card, Empty, Kpi, Tabs } from "../../components/ui";
import { ScopeBar, TIP, fmtMs, pct, rate, svcColor, useScope } from "./common";

/** One service (SigNoz service overview): latency percentiles, request rate and error rate over time, then its key
 *  operations, the database and external calls it makes, and its slowest traces. */
export default function ServiceDetail() {
  const { t } = useT(); const sc = useScope(); const { name = "" } = useParams();
  const [tab, setTab] = useState<"ops" | "db" | "ext" | "slow">("ops");
  const q = useQuery({ queryKey: ["apm-service", name, sc.qs()], queryFn: () => get(`/apm/services/${encodeURIComponent(name)}?${sc.qs()}`), refetchInterval: sc.live ? 15000 : false });
  const d = q.data; const s = d?.summary ?? {};
  const series = (d?.series ?? []).map((x: any) => ({ ...x, m: x.t.slice(11, 16), errp: +(x.error_rate * 100).toFixed(2) }));
  const opTable = (rows: any[]) => !rows?.length ? <Empty>{t("apm_none")}</Empty> :
    <div className="table-wrap"><table className="table"><thead><tr><th>{t("apm_col_op")}</th><th className="right">{t("apm_calls")}</th><th className="right">P50</th><th className="right">P95</th><th className="right">P99</th><th className="right">{t("apm_col_err")}</th></tr></thead>
      <tbody>{rows.map((r: any) => <tr key={r.operation}><td className="mono"><Link to={`/traces?service=${encodeURIComponent(name)}&operation=${encodeURIComponent(r.operation)}${sc.link().replace("?", "&")}`}>{r.operation}</Link></td>
        <td className="num">{r.calls.toLocaleString()}</td><td className="num">{fmtMs(r.p50_ms)}</td><td className="num">{fmtMs(r.p95_ms)}</td><td className="num">{fmtMs(r.p99_ms)}</td>
        <td className="num" style={{ color: r.error_rate > 0 ? "var(--red)" : undefined }}>{pct(r.error_rate)}</td></tr>)}</tbody></table></div>;
  const chart = (title: string, body: any) => <Card title={title}><div style={{ height: 190 }}><ResponsiveContainer>{body}</ResponsiveContainer></div></Card>;
  const axis = [<CartesianGrid key="g" stroke="rgba(148,163,184,0.12)" vertical={false} />, <XAxis key="x" dataKey="m" stroke="#64748b" fontSize={11} minTickGap={24} />];
  return <div className="stack">
    <div className="row between"><div><div className="dim small"><Link to={`/services${sc.link()}`}>{t("apm_services")}</Link> /</div>
      <h2 className="row" style={{ gap: 8 }}><span className="dot" style={{ background: svcColor(name), width: 12, height: 12 }} />{name}</h2></div><ScopeBar /></div>
    <div className="grid k4">
      <Kpi value={fmtMs(s.p99_ms)} label="P99" accent="#fbbf24" sub={`P50 ${fmtMs(s.p50_ms)} · P95 ${fmtMs(s.p95_ms)}`} />
      <Kpi value={rate(s.rps)} label={t("apm_k_rps")} accent="#60a5fa" sub={`${(s.calls ?? 0).toLocaleString()} ${t("apm_calls")}`} />
      <Kpi value={pct(s.error_rate)} label={t("apm_k_err")} accent="#f87171" sub={`${s.errors ?? 0} ${t("apm_errors")}`} />
      <Kpi value={s.apdex ?? "-"} label="Apdex" accent="#2dd4bf" sub={t("apm_apdex_sub")} />
    </div>
    <div className="grid k3">
      {chart(t("apm_ch_latency"), <LineChart data={series}>{axis}<YAxis stroke="#64748b" fontSize={11} width={44} /><Tooltip {...TIP} formatter={(v: any) => fmtMs(v)} />
        <Line type="monotone" dataKey="p50" name="P50" stroke="#2dd4bf" dot={false} strokeWidth={1.6} /><Line type="monotone" dataKey="p95" name="P95" stroke="#fbbf24" dot={false} strokeWidth={1.6} /><Line type="monotone" dataKey="p99" name="P99" stroke="#f87171" dot={false} strokeWidth={1.6} /></LineChart>)}
      {chart(t("apm_ch_rate"), <BarChart data={series}>{axis}<YAxis stroke="#64748b" fontSize={11} width={34} /><Tooltip {...TIP} /><Bar dataKey="n" name={t("apm_calls")} fill="#60a5fa" /></BarChart>)}
      {chart(t("apm_ch_err"), <LineChart data={series}>{axis}<YAxis stroke="#64748b" fontSize={11} width={34} unit="%" /><Tooltip {...TIP} /><Line type="monotone" dataKey="errp" name="%" stroke="#f87171" dot={false} strokeWidth={1.6} /></LineChart>)}
    </div>
    <Card>
      <Tabs value={tab} onChange={setTab} tabs={[{ k: "ops", label: `${t("apm_tab_ops")} · ${d?.operations?.length ?? 0}` }, { k: "db", label: `${t("apm_tab_db")} · ${d?.db_calls?.length ?? 0}` },
        { k: "ext", label: `${t("apm_tab_ext")} · ${d?.external_calls?.length ?? 0}` }, { k: "slow", label: `${t("apm_tab_slow")} · ${d?.slow_traces?.length ?? 0}` }]} />
      <div style={{ marginTop: 10 }}>
        {tab === "ops" && opTable(d?.operations)}{tab === "db" && opTable(d?.db_calls)}{tab === "ext" && opTable(d?.external_calls)}
        {tab === "slow" && (!d?.slow_traces?.length ? <Empty>{t("apm_no_traces")}</Empty> : <div className="table-wrap"><table className="table"><thead><tr><th>{t("apm_col_start")}</th><th>{t("apm_col_root")}</th><th className="right">{t("apm_col_dur")}</th><th className="right">{t("apm_col_spans")}</th></tr></thead>
          <tbody>{d.slow_traces.map((x: any) => <tr key={x.trace_id}><td className="mono">{x.start.replace("T", " ").slice(0, 19)}</td><td><Link to={`/traces/${encodeURIComponent(x.trace_id)}${sc.link()}`}>{x.root_service} · {x.root_operation}</Link></td>
            <td className="num"><b style={{ color: x.error ? "var(--red)" : undefined }}>{fmtMs(x.duration_ms)}</b></td><td className="num">{x.spans}</td></tr>)}</tbody></table></div>)}
      </div>
    </Card>
  </div>;
}
