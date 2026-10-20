import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Bug } from "lucide-react";
import { get } from "../../api";
import { useT } from "../../i18n";
import { Card, Empty, Kpi, Spark, Hint } from "../../components/ui";
import { svcColor } from "./common";

/** Exceptions: every error line of the live window grouped by what failed (exception class, ORA / SQL code, HTTP 5xx, timeout,
 *  refused connection) and the service that logged it; one row per group with hosts, trend and the latest masked sample. */
export default function Exceptions() {
  const { t } = useT(); const [minutes, setMinutes] = useState(60); const [open, setOpen] = useState<string | null>(null); const [qtext, setQ] = useState("");
  const q = useQuery({ queryKey: ["exceptions", minutes], queryFn: () => get(`/apm/exceptions?minutes=${minutes}`), refetchInterval: 15000 });
  const all: any[] = q.data?.groups ?? [];
  const rows = all.filter((r) => !qtext || `${r.type} ${r.service} ${r.sample}`.toLowerCase().includes(qtext.toLowerCase()));
  const total = all.reduce((a, r) => a + r.count, 0);
  return <div className="stack">
    <div className="row between"><div><h2><Bug size={20} style={{ verticalAlign: -3 }} /> {t("ex_title")}<Hint text={t("ex_sub")} howto="exceptions" /></h2></div>
      <div className="row"><input placeholder={t("ex_filter")} value={qtext} onChange={(e) => setQ(e.target.value)} />
        <select value={minutes} onChange={(e) => setMinutes(+e.target.value)}>{[15, 60, 360, 1440].map((m) => <option key={m} value={m}>{m < 60 ? `${m} dk` : `${m / 60} sa`}</option>)}</select></div></div>
    <div className="grid k4">
      <Kpi value={all.length} label={t("ex_k_groups")} sub={t("ex_k_groups_sub")} />
      <Kpi value={total.toLocaleString()} label={t("ex_k_total")} accent="#f87171" />
      <Kpi value={new Set(all.map((r) => r.service)).size} label={t("ex_k_services")} accent="#60a5fa" />
      <Kpi value={all[0]?.type ?? "-"} label={t("ex_k_top")} accent="#fbbf24" sub={all[0] ? `${all[0].count} · ${all[0].service}` : ""} />
    </div>
    <Card>{!rows.length ? <Empty>{t("ex_empty")}</Empty> :
      <div className="table-wrap"><table className="table"><thead><tr><th>{t("ex_col_type")}</th><th>{t("apm_col_service")}</th><th className="right">{t("ex_col_count")}</th><th>{t("ex_col_hosts")}</th><th>{t("ex_col_last")}</th><th style={{ width: 140 }}>{t("apm_col_trend")}</th></tr></thead>
        <tbody>{rows.map((r) => { const id = `${r.type}|${r.service}`; return [<tr key={id} className="clickable" onClick={() => setOpen(open === id ? null : id)}>
          <td><b className="mono">{r.type}</b>{r.critical ? <span className="badge red" style={{ marginLeft: 6 }}>CRITICAL {r.critical}</span> : null}<div className="dim small">{r.sample.slice(0, 110)}</div></td>
          <td><span className="dot" style={{ background: svcColor(r.service) }} /> {r.service}</td><td className="num"><b>{r.count.toLocaleString()}</b></td>
          <td className="small">{r.hosts.slice(0, 3).map((h: any) => h.host).join(", ")}{r.host_count > 3 ? ` +${r.host_count - 3}` : ""}</td>
          <td className="small mono">{r.last.slice(11, 19)}<div className="dim">{t("ex_first")} {r.first.slice(11, 19)}</div></td>
          <td><div style={{ height: 26 }}><Spark data={r.trend} color="#f87171" /></div></td></tr>,
          open === id && <tr key={`${id}-d`}><td colSpan={6}><div className="small mono" style={{ whiteSpace: "pre-wrap" }}>{r.sample}</div><div className="dim small">{r.ref}</div>
            <div className="row wrap small" style={{ gap: 6, marginTop: 6 }}>{r.hosts.map((h: any) => <span key={h.host} className="chip">{h.host} · {h.count}</span>)}</div></td></tr>]; })}</tbody></table></div>}
    </Card>
    <div className="dim small">{t("ex_foot")}</div>
  </div>;
}
