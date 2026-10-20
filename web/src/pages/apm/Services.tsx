import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Layers } from "lucide-react";
import { get } from "../../api";
import { useT } from "../../i18n";
import { Card, Empty, Kpi, Spark, Hint } from "../../components/ui";
import { ScopeBar, fmtMs, pct, rate, svcColor, useScope } from "./common";

/** Services (SigNoz "Services"): every service seen in the logs with its RED numbers (rate, errors, duration percentiles) and
 *  Apdex, worst first; computed for 100 % of requests from per-minute rollups, whether or not the full trace was sampled. */
export default function Services() {
  const { t } = useT(); const sc = useScope();
  const q = useQuery({ queryKey: ["apm-services", sc.qs()], queryFn: () => get(`/apm/services?${sc.qs()}`), refetchInterval: sc.live ? 15000 : false });
  const rows: any[] = q.data?.services ?? [];
  const envs = [...new Set(rows.flatMap((r) => r.envs ?? []))] as string[];
  const calls = rows.reduce((a, r) => a + r.calls, 0), errs = rows.reduce((a, r) => a + r.errors, 0);
  const worst = rows.reduce((a: any, r) => (!a || (r.p99_ms ?? 0) > (a.p99_ms ?? 0) ? r : a), null);
  return <div className="stack">
    <div className="row between"><div><h2><Layers size={20} style={{ verticalAlign: -3 }} /> {t("apm_services")}<Hint text={t("apm_services_sub")} /></h2></div><ScopeBar envs={envs} /></div>
    <div className="grid k4">
      <Kpi value={rows.length} label={t("apm_k_services")} sub={t("apm_k_services_sub")} />
      <Kpi value={rate(rows.reduce((a, r) => a + r.rps, 0))} label={t("apm_k_rps")} accent="#60a5fa" sub={`${calls.toLocaleString()} ${t("apm_calls")}`} />
      <Kpi value={pct(calls ? errs / calls : 0)} label={t("apm_k_err")} accent="#f87171" sub={`${errs.toLocaleString()} ${t("apm_errors")}`} />
      <Kpi value={fmtMs(worst?.p99_ms)} label={t("apm_k_p99")} accent="#fbbf24" sub={worst?.service ?? "-"} />
    </div>
    <Card>
      {!rows.length ? <Empty>{t("apm_no_services")}</Empty> :
        <div className="table-wrap"><table className="table"><thead><tr><th>{t("apm_col_service")}</th><th className="right">P50</th><th className="right">P95</th><th className="right">P99</th>
          <th className="right">{t("apm_col_err")}</th><th className="right">{t("apm_col_rps")}</th><th className="right">Apdex</th><th style={{ width: 160 }}>{t("apm_col_trend")}</th></tr></thead>
          <tbody>{rows.map((r) => <tr key={r.service}>
            <td><Link to={`/services/${encodeURIComponent(r.service)}${sc.link()}`} className="row" style={{ gap: 8, display: "inline-flex" }}><span className="dot" style={{ background: svcColor(r.service) }} /><b>{r.service}</b></Link>
              <div className="dim small">{r.operations} {t("apm_ops")}{r.envs?.length ? ` · ${r.envs.join(", ")}` : ""}</div></td>
            <td className="num">{fmtMs(r.p50_ms)}</td><td className="num">{fmtMs(r.p95_ms)}</td><td className="num"><b>{fmtMs(r.p99_ms)}</b></td>
            <td className="num" style={{ color: r.error_rate >= 0.05 ? "var(--red)" : r.error_rate > 0 ? "var(--amber)" : undefined }}>{pct(r.error_rate)}</td>
            <td className="num">{rate(r.rps)}</td>
            <td className="num" style={{ color: r.apdex === null ? undefined : r.apdex < 0.7 ? "var(--red)" : r.apdex < 0.85 ? "var(--amber)" : "var(--green)" }}>{r.apdex ?? "-"}</td>
            <td><div style={{ height: 28 }}><Spark data={((s) => (s.length === 1 ? [s[0], s[0]] : s))((r.series ?? []).map((x: any) => x.n))} color={svcColor(r.service)} /></div></td>
          </tr>)}</tbody></table></div>}
    </Card>
    <div className="dim small">{t("apm_services_foot")}</div>
  </div>;
}
