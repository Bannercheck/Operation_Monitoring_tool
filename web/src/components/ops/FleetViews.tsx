import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { get } from "../../api";
import { useT } from "../../i18n";
import { HostMap } from "./HostMap";
import { SvcTable } from "./RangeDetail";
import { fmtN, fmtPct, hostState } from "./util";

/** The fleet card: the map by default, and the same numbers as sortable tables (hosts, pools, services) one click away. */
export type View = "map" | "hosts" | "pools" | "services";
export function FleetViews({ rq, pools, sel, pool, onHost, onPool, onLogs }: { rq: string; pools: any[]; sel: string[]; pool: string; onHost: (h: string) => void; onPool: (p: string) => void; onLogs: (f: any) => void }) {
  const { t, lang } = useT(); const [view, setView] = useState<View>("map"); const [f, setF] = useState(""); const [sort, setSort] = useState("errors");
  const det = useQuery({ queryKey: ["range-detail", rq], queryFn: () => get(`/live/range/detail?${rq}`), enabled: view === "services", refetchInterval: rq.includes("last=") ? 15000 : false });
  const services: any[] = det.data?.services ?? [];
  const fl = f.toLowerCase();
  const hosts = pools.flatMap((p) => p.hosts).filter((h: any) => !fl || h.host.toLowerCase().includes(fl) || h.pool.toLowerCase().includes(fl))
    .sort((a: any, b: any) => sort === "silent" ? Number(b.silent) - Number(a.silent) || (b.last_seen_s ?? 0) - (a.last_seen_s ?? 0) : sort === "events" ? b.events - a.events : sort === "rate" ? b.error_rate - a.error_rate : sort === "host" ? a.host.localeCompare(b.host) : b.errors - a.errors || b.events - a.events);
  const prow = pools.filter((p) => !fl || p.pool.toLowerCase().includes(fl));
  const th = (k: string, label: string, right = true) => <th className={`${right ? "right " : ""}clickable`} onClick={() => setSort(k)}>{label}{sort === k ? " ▾" : ""}</th>;
  return <div>
    <div className="row between" style={{ marginBottom: 10, gap: 8, flexWrap: "wrap" }}>
      <div className="seg">{(["map", "hosts", "pools", "services"] as View[]).map((k) => <button key={k} className={view === k ? "on" : ""} onClick={() => setView(k)}>{t(`fv_${k}`)}</button>)}</div>
      {view !== "map" && view !== "services" && <input className="input sm" style={{ width: 170 }} placeholder={t("hm_find")} value={f} onChange={(e) => setF(e.target.value)} />}
    </div>
    {view === "map" && <HostMap pools={pools} sel={sel} pool={pool} onHost={onHost} onPool={onPool} />}
    {view === "hosts" && <div className="table-wrap" style={{ maxHeight: 440 }}><table className="table"><thead><tr>{th("host", t("host"), false)}<th>Pool</th>{th("events", t("fl_per_min"))}<th className="right">WARN</th>{th("errors", "ERROR+")}{th("rate", t("fl_err_rate"))}{th("silent", t("fl_last"), false)}<th>{t("service")}</th></tr></thead>
      <tbody>{hosts.slice(0, 500).map((h: any) => <tr key={h.host} className="clickable" onClick={() => onHost(h.host)} style={sel.includes(h.host) ? { outline: "1px solid var(--accent)" } : undefined}>
        <td><span className={`hm-c ${hostState(h)}`} style={{ width: 10, height: 10, verticalAlign: -1 }} /> <b>{h.host}</b> <span className="dim small">{h.env}</span></td><td className="small">{h.pool}</td>
        <td className="num">{fmtN(h.per_min, lang)}</td><td className="num">{h.warnings || "–"}</td><td className="num">{fmtN(h.errors, lang)}</td>
        <td className="num" style={{ color: h.error_rate >= 0.05 ? "var(--red)" : h.error_rate >= 0.005 ? "var(--amber)" : undefined }}>{fmtPct(h.error_rate, 2)}</td>
        <td className="small" style={{ color: h.silent ? "var(--amber)" : undefined }}>{h.last_seen_s == null ? "–" : h.last_seen_s < 60 ? `${h.last_seen_s} sn` : `${Math.round(h.last_seen_s / 60)} dk`}{h.silent ? ` · ${t("fl_silent")}` : ""}</td><td className="small">{h.service}</td></tr>)}</tbody></table>
      {hosts.length > 500 && <div className="dim small">{t("fl_more", { n: hosts.length - 500 })}</div>}</div>}
    {view === "pools" && <div className="table-wrap" style={{ maxHeight: 440 }}><table className="table"><thead><tr><th>Pool</th><th className="right">{t("fl_hosts")}</th><th className="right">{t("fl_per_min")}</th><th className="right">ERROR+</th><th className="right">{t("fl_err_rate")}</th><th>{t("fl_attention")}</th><th /></tr></thead>
      <tbody>{prow.map((p) => <tr key={p.pool} style={pool === p.pool ? { outline: "1px solid var(--accent)" } : undefined}>
        <td><b>{p.pool}</b> <span className="dim small">{p.envs.join(", ")}</span></td><td className="num">{p.host_count}{p.silent ? <span style={{ color: "var(--amber)" }}> · {p.silent} {t("fl_silent")}</span> : null}</td>
        <td className="num">{fmtN(p.per_min, lang)}</td><td className="num">{fmtN(p.errors, lang)}</td><td className="num" style={{ color: p.error_rate >= 0.05 ? "var(--red)" : p.error_rate >= 0.005 ? "var(--amber)" : undefined }}>{fmtPct(p.error_rate, 2)}</td>
        <td className="small">{p.worst && <span>{t("fl_worst")}: <b>{p.worst}</b></span>}{p.outliers?.length ? <span className="chip" title={p.outliers.join(", ")} style={{ color: "var(--amber)", marginLeft: 6 }}>{t("fl_outlier_n", { n: p.outliers.length })}</span> : null}</td>
        <td><button className="btn sm" onClick={() => onPool(pool === p.pool ? "" : p.pool)}>{pool === p.pool ? "✕" : t("fl_focus")}</button></td></tr>)}</tbody></table></div>}
    {view === "services" && <SvcTable rows={services} onLogs={onLogs} />}
  </div>;
}
