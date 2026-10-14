import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Boxes, Layers3 } from "lucide-react";
import { get } from "../api";
import { useT } from "../i18n";
import { Badge, Card, Empty, Spark, Tabs } from "./ui";

/** Fleet view for 200+ servers: pools (inventory cluster, else the host name without its number) with every host's volume,
 *  errors and silence, and the deduplicated log groups (one row per message template and service). Both read the per-minute
 *  rollups kept at ingest, so they stay fast at 100k+ lines a minute. */
const rateCol = (r: number) => (r >= 0.05 ? "var(--red)" : r > 0.005 ? "var(--amber)" : undefined);

export function FleetCard({ env, pool, hosts, onPool, onHost }: { env: string; pool: string; hosts: string[]; onPool: (p: string) => void; onHost: (h: string) => void }) {
  const { t } = useT(); const [tab, setTab] = useState<"pool" | "host">("pool"); const [open, setOpen] = useState<string | null>(null); const [f, setF] = useState(""); const [sort, setSort] = useState<"errors" | "events" | "silent">("errors");
  const q = useQuery({ queryKey: ["live-pools", env], queryFn: () => get(`/live/pools?window=15${env ? `&env=${encodeURIComponent(env)}` : ""}`), refetchInterval: 10000 });
  const pools: any[] = q.data?.pools ?? [];
  const fl = f.toLowerCase();
  const hostRows = pools.flatMap((p) => p.hosts).filter((h: any) => !fl || h.host.toLowerCase().includes(fl) || h.pool.toLowerCase().includes(fl))
    .sort((a: any, b: any) => (sort === "silent" ? Number(b.silent) - Number(a.silent) || b.last_seen_s - a.last_seen_s : sort === "events" ? b.events - a.events : b.errors - a.errors || b.events - a.events));
  const prow = pools.filter((p) => !fl || p.pool.toLowerCase().includes(fl) || p.hosts.some((h: any) => h.host.toLowerCase().includes(fl)));
  return <Card title={<span><Boxes size={16} style={{ verticalAlign: -3 }} /> {t("fl_title")} <span className="muted small">· {q.data?.hosts ?? 0} {t("fl_hosts")} · {pools.length} pool{q.data?.silent ? <> · <b style={{ color: "var(--amber)" }}>{q.data.silent} {t("fl_silent")}</b></> : null}</span></span>}
    right={<><input className="input" style={{ width: 170 }} placeholder={t("fl_filter")} value={f} onChange={(e) => setF(e.target.value)} />
      {tab === "host" && <select className="input" style={{ width: 130 }} value={sort} onChange={(e) => setSort(e.target.value as any)}><option value="errors">{t("fl_sort_err")}</option><option value="events">{t("fl_sort_vol")}</option><option value="silent">{t("fl_sort_silent")}</option></select>}</>}>
    <Tabs value={tab} onChange={setTab} tabs={[{ k: "pool", label: `Pool (${pools.length})` }, { k: "host", label: `${t("fl_tab_host")} (${q.data?.hosts ?? 0})` }]} />
    {!pools.length ? <Empty>{t("fl_empty")}</Empty> : tab === "pool" ?
      <div className="table-wrap" style={{ maxHeight: 420 }}><table className="table"><thead><tr><th>Pool</th><th className="right">{t("fl_hosts")}</th><th className="right">{t("fl_per_min")}</th><th className="right">ERROR+</th><th className="right">{t("fl_err_rate")}</th><th>{t("fl_attention")}</th></tr></thead>
        <tbody>{prow.map((p) => [<tr key={p.pool} className="clickable" onClick={() => setOpen(open === p.pool ? null : p.pool)} style={pool === p.pool ? { outline: "1px solid var(--accent)" } : undefined}>
          <td><b>{p.pool}</b> <span className="dim small">{p.envs.join(", ")}</span></td><td className="num">{p.host_count}{p.silent ? <span style={{ color: "var(--amber)" }}> · {p.silent}⏸</span> : null}</td>
          <td className="num">{p.per_min.toLocaleString()}</td><td className="num">{p.errors.toLocaleString()}</td><td className="num" style={{ color: rateCol(p.error_rate) }}>{(p.error_rate * 100).toFixed(2)}%</td>
          <td className="small">{p.worst && <span>{t("fl_worst")}: <b>{p.worst}</b> </span>}{p.outliers.length ? <span className="chip" title={p.outliers.join(", ")} style={{ color: "var(--amber)" }}>{t("fl_outlier_n", { n: p.outliers.length })}</span> : null}
            <button className="btn sm" style={{ marginLeft: 6 }} onClick={(e) => { e.stopPropagation(); onPool(pool === p.pool ? "" : p.pool); }}>{pool === p.pool ? "✕" : t("fl_focus")}</button></td></tr>,
          open === p.pool && <tr key={`${p.pool}-h`}><td colSpan={6}><div className="fleet-grid">{p.hosts.map((h: any) => <HostTile key={h.host} h={h} on={hosts.includes(h.host)} onClick={() => onHost(h.host)} />)}</div></td></tr>])}</tbody></table></div> :
      <div className="table-wrap" style={{ maxHeight: 420 }}><table className="table"><thead><tr><th>{t("host")}</th><th>Pool</th><th className="right">{t("fl_per_min")}</th><th className="right">WARN</th><th className="right">ERROR+</th><th className="right">{t("fl_err_rate")}</th><th>{t("fl_last")}</th><th>{t("service")}</th></tr></thead>
        <tbody>{hostRows.slice(0, 400).map((h: any) => <tr key={h.host} className="clickable" onClick={() => onHost(h.host)} style={hosts.includes(h.host) ? { outline: "1px solid var(--accent)" } : undefined}>
          <td><span className="dot" style={{ background: h.silent ? "#64748b" : h.errors ? (h.error_rate >= 0.05 ? "#f87171" : "#fbbf24") : "#34d399" }} /> <b>{h.host}</b> <span className="dim small">{h.env}</span></td><td className="small">{h.pool}</td>
          <td className="num">{h.per_min.toLocaleString()}</td><td className="num">{h.warnings}</td><td className="num">{h.errors}</td><td className="num" style={{ color: rateCol(h.error_rate) }}>{(h.error_rate * 100).toFixed(2)}%</td>
          <td className="small" style={{ color: h.silent ? "var(--amber)" : undefined }}>{h.last_seen_s < 60 ? `${h.last_seen_s} sn` : `${Math.round(h.last_seen_s / 60)} dk`}{h.silent ? ` · ${t("fl_silent")}` : ""}</td><td className="small">{h.service}</td></tr>)}</tbody></table>
        {hostRows.length > 400 && <div className="dim small">{t("fl_more", { n: hostRows.length - 400 })}</div>}</div>}
    <div className="dim small" style={{ marginTop: 6 }}>{t("fl_foot")}</div>
  </Card>;
}

function HostTile({ h, on, onClick }: { h: any; on: boolean; onClick: () => void }) {
  const c = h.silent ? "#64748b" : h.errors ? (h.error_rate >= 0.05 ? "#f87171" : "#fbbf24") : "#34d399";
  return <div className="fleet-tile" style={{ borderColor: on ? "var(--accent)" : c }} onClick={onClick} title={`${h.host} · ${h.events} / ${h.errors}`}>
    <div className="row between"><b className="small">{h.host}</b><span className="dot" style={{ background: c }} /></div>
    <div className="dim small">{h.per_min}/dk · {h.errors} err</div></div>;
}

export function GroupsCard({ sq, onHost }: { sq: string; onHost: (h: string) => void }) {
  const { t } = useT(); const [sev, setSev] = useState("WARN"); const [q, setQ] = useState(""); const [open, setOpen] = useState<number | null>(null); const [win, setWin] = useState(15);
  const g = useQuery({ queryKey: ["live-groups", sq, sev, q, win], queryFn: () => get(`/live/groups?window=${win}&severity=${sev}&q=${encodeURIComponent(q)}&limit=100${sq}`), refetchInterval: 10000 });
  const rows: any[] = g.data?.groups ?? [];
  return <Card title={<span><Layers3 size={16} style={{ verticalAlign: -3 }} /> {t("gr_title")} <span className="muted small">· {(g.data?.total ?? 0).toLocaleString()} {t("gr_lines")} → {(g.data?.distinct ?? 0).toLocaleString()} {t("gr_groups")}</span></span>}
    right={<><input className="input" style={{ width: 180 }} placeholder={t("gr_search")} value={q} onChange={(e) => setQ(e.target.value)} />
      <select className="input" style={{ width: 110 }} value={sev} onChange={(e) => setSev(e.target.value)}><option value="ERROR">ERROR+</option><option value="WARN">WARN+</option><option value="INFO">{t("gr_all")}</option></select>
      <select className="input" style={{ width: 90 }} value={win} onChange={(e) => setWin(+e.target.value)}>{[5, 15, 30, 60].map((w) => <option key={w} value={w}>{w} dk</option>)}</select></>}>
    {!rows.length ? <Empty>{t("gr_empty")}</Empty> :
      <div className="table-wrap" style={{ maxHeight: 520 }}><table className="table"><thead><tr><th style={{ width: 90 }}>{t("severity")}</th><th>{t("gr_template")}</th><th>{t("service")}</th><th className="right">{t("gr_count")}</th><th>{t("gr_hosts")}</th><th>{t("fl_last")}</th><th style={{ width: 120 }}>{t("apm_col_trend")}</th></tr></thead>
        <tbody>{rows.map((r) => [<tr key={r.id} className="clickable" onClick={() => setOpen(open === r.id ? null : r.id)}>
          <td><Badge v={r.severity} /></td><td className="mono small">{r.template.slice(0, 140)}</td><td className="small">{r.service}</td><td className="num"><b>{r.count.toLocaleString()}</b></td>
          <td className="small" style={{ whiteSpace: "nowrap" }} title={r.pools.map((p: any) => `${p.pool} (${p.count})`).join(", ")}><b>{r.host_count}</b> {t("fl_hosts")} · {r.pools.length} pool</td><td className="small mono">{r.last.slice(11, 19)}</td>
          <td><div style={{ height: 24 }}><Spark data={r.trend.length === 1 ? [r.trend[0], r.trend[0]] : r.trend} color={r.severity === "ERROR" || r.severity === "CRITICAL" ? "#f87171" : r.severity === "WARN" ? "#fbbf24" : "#60a5fa"} /></div></td></tr>,
          open === r.id && <tr key={`${r.id}-d`}><td colSpan={7}><div className="small dim">{t("gr_sample")} · {r.ref} · {t("gr_first")} {r.first.slice(11, 19)}</div><div className="mono small" style={{ whiteSpace: "pre-wrap", margin: "4px 0 8px" }}>{r.sample}</div>
            <div className="row wrap" style={{ gap: 6 }}>{r.hosts.map((h: any) => <span key={h.host} className="chip" style={{ cursor: "pointer" }} onClick={() => onHost(h.host)}>{h.host} · {h.count}</span>)}</div></td></tr>])}</tbody></table></div>}
    <div className="dim small" style={{ marginTop: 6 }}>{t("gr_foot")}</div>
  </Card>;
}
