import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { get } from "../../api";
import { useT } from "../../i18n";
import { HostMap } from "./HostMap";
import { SvcTable } from "./RangeDetail";
import { fmtN, fmtPct, hostState } from "./util";
import { LineChartCard, SLOT, settled } from "./LineChartCard";
import { SessionsView } from "./Sessions";

/** The fleet card: the map by default, and the same numbers as sortable tables (hosts, pools, services), per-host lines over time
 *  and the client sessions one click away. */
export type View = "map" | "hosts" | "pools" | "services" | "charts" | "sessions";
export function FleetViews({ rq, pools, sel, pool, onHost, onPool, onLogs, span, onZoom }: { rq: string; pools: any[]; sel: string[]; pool: string; onHost: (h: string) => void; onPool: (p: string) => void; onLogs: (f: any) => void; span: number; onZoom: (t: number, stepMin: number) => void }) {
  const { t, lang } = useT(); const [view, setView] = useState<View>("map"); const [f, setF] = useState(""); const [sort, setSort] = useState("errors");
  const det = useQuery({ queryKey: ["range-detail", rq], queryFn: () => get(`/live/range/detail?${rq}`), enabled: view === "services", refetchInterval: rq.includes("last=") ? 15000 : false });
  const services: any[] = det.data?.services ?? [];
  // sessions per host for the hosts table: like the table itself, the whole fleet, not the scope
  const rqAll = rq.replace(/&?(pool|host)=[^&]*/g, "");
  const ses = useQuery({ queryKey: ["range-sessions-byhost", rqAll], queryFn: () => get(`/live/range/sessions?${rqAll}&limit=1`), enabled: view === "hosts", refetchInterval: rq.includes("last=") ? 30000 : false });
  const sesBy: Record<string, number> = Object.fromEntries((ses.data?.by_host ?? []).map((h: any) => [h.host, h.sessions]));
  const fl = f.toLowerCase();
  const hosts = pools.flatMap((p) => p.hosts).filter((h: any) => !fl || h.host.toLowerCase().includes(fl) || h.pool.toLowerCase().includes(fl))
    .sort((a: any, b: any) => sort === "silent" ? Number(b.silent) - Number(a.silent) || (b.last_seen_s ?? 0) - (a.last_seen_s ?? 0) : sort === "events" ? b.events - a.events : sort === "rate" ? b.error_rate - a.error_rate : sort === "host" ? a.host.localeCompare(b.host) : b.errors - a.errors || b.events - a.events);
  const prow = pools.filter((p) => !fl || p.pool.toLowerCase().includes(fl));
  const th = (k: string, label: string, right = true) => <th className={`${right ? "right " : ""}clickable`} onClick={() => setSort(k)}>{label}{sort === k ? " ▾" : ""}</th>;
  return <div>
    <div className="row between" style={{ marginBottom: 10, gap: 8, flexWrap: "wrap" }}>
      <div className="seg fv-seg">{(["map", "hosts", "pools", "services", "charts", "sessions"] as View[]).map((k) => <button key={k} className={view === k ? "on" : ""} onClick={() => setView(k)}>{t(`fv_${k}`)}</button>)}</div>
      {(view === "hosts" || view === "pools") && <input className="input sm" style={{ width: 170 }} placeholder={t("hm_find")} value={f} onChange={(e) => setF(e.target.value)} />}
    </div>
    {view === "map" && <HostMap pools={pools} sel={sel} pool={pool} onHost={onHost} onPool={onPool} />}
    {view === "hosts" && <div className="table-wrap" style={{ maxHeight: 440 }}><table className="table fv-hosts"><thead><tr>{th("host", t("host"), false)}<th>Pool</th>{th("events", t("fl_per_min"))}<th className="right">WARN</th>{th("errors", "ERROR+")}{th("rate", t("fl_err_rate"))}<th className="right">{t("fl_sessions")}</th>{th("silent", t("fl_last"), false)}<th>{t("service")}</th></tr></thead>
      <tbody>{hosts.slice(0, 500).map((h: any) => <tr key={h.host} className="clickable" onClick={() => onHost(h.host)} style={sel.includes(h.host) ? { outline: "1px solid var(--accent)" } : undefined}>
        <td style={{ whiteSpace: "nowrap" }}><span className={`hm-c ${hostState(h)}`} style={{ width: 10, height: 10, verticalAlign: -1 }} /> <b>{h.host}</b> <span className="dim small">{h.env}</span></td><td className="small" style={{ whiteSpace: "nowrap" }}>{h.pool}</td>
        <td className="num">{fmtN(h.per_min, lang)}</td><td className="num">{h.warnings || "–"}</td><td className="num">{fmtN(h.errors, lang)}</td>
        <td className="num" style={{ color: h.error_rate >= 0.05 ? "var(--red)" : h.error_rate >= 0.005 ? "var(--amber)" : undefined }}>{fmtPct(h.error_rate, 2)}</td><td className="num">{sesBy[h.host] ? fmtN(sesBy[h.host], lang) : "–"}</td>
        <td className="small" style={{ color: h.silent ? "var(--amber)" : undefined }}>{h.last_seen_s == null ? "–" : h.last_seen_s < 60 ? `${h.last_seen_s} sn` : `${Math.round(h.last_seen_s / 60)} dk`}{h.silent ? ` · ${t("fl_silent")}` : ""}</td><td className="small">{h.service}</td></tr>)}</tbody></table>
      {hosts.length > 500 && <div className="dim small">{t("fl_more", { n: hosts.length - 500 })}</div>}</div>}
    {view === "pools" && <div className="table-wrap" style={{ maxHeight: 440 }}><table className="table"><thead><tr><th>Pool</th><th className="right">{t("fl_hosts")}</th><th className="right">{t("fl_per_min")}</th><th className="right">ERROR+</th><th className="right">{t("fl_err_rate")}</th><th>{t("fl_attention")}</th><th /></tr></thead>
      <tbody>{prow.map((p) => <tr key={p.pool} className="clickable" onClick={() => onPool(pool === p.pool ? "" : p.pool)} style={pool === p.pool ? { outline: "1px solid var(--accent)" } : undefined}>
        <td><b>{p.pool}</b> <span className="dim small">{p.envs.join(", ")}</span></td><td className="num">{p.host_count}{p.silent ? <span style={{ color: "var(--amber)" }}> · {p.silent} {t("fl_silent")}</span> : null}</td>
        <td className="num">{fmtN(p.per_min, lang)}</td><td className="num">{fmtN(p.errors, lang)}</td><td className="num" style={{ color: p.error_rate >= 0.05 ? "var(--red)" : p.error_rate >= 0.005 ? "var(--amber)" : undefined }}>{fmtPct(p.error_rate, 2)}</td>
        <td className="small">{p.worst && <span>{t("fl_worst")}: <b>{p.worst}</b></span>}{p.outliers?.length ? <span className="chip" title={p.outliers.join(", ")} style={{ color: "var(--amber)", marginLeft: 6 }}>{t("fl_outlier_n", { n: p.outliers.length })}</span> : null}</td>
        <td><button className="btn sm" onClick={(e) => { e.stopPropagation(); onPool(pool === p.pool ? "" : p.pool); }}>{pool === p.pool ? "✕" : t("fl_focus")}</button></td></tr>)}</tbody></table></div>}
    {view === "services" && <SvcTable rows={services} onLogs={onLogs} />}
    {view === "charts" && <HostCharts rq={rq} span={span} onZoom={onZoom} />}
    {view === "sessions" && <SessionsView rq={rq} span={span} onZoom={onZoom} onHost={onHost} />}
  </div>;
}

type Metric = "lines" | "errors" | "error_rate" | "p95" | "sessions";
/** the busiest hosts in scope as one line each, for the chosen measure; a host keeps its colour while the measure changes, so
 *  switching from volume to p95 shows at once whether the same server leads */
function HostCharts({ rq, span, onZoom }: { rq: string; span: number; onZoom: (t: number, stepMin: number) => void }) {
  const { t, lang } = useT(); const [m, setM] = useState<Metric>("lines"); const slots = useRef<Record<string, number>>({});
  const r = useQuery({ queryKey: ["range-hostseries", rq, m], queryFn: () => get(`/live/range/hostseries?${rq}&metric=${m}&top=6`), refetchInterval: rq.includes("last=") ? 15000 : false });
  const hosts: string[] = (r.data?.hosts ?? []).map((h: any) => h.host);
  // a remembered slot only while no other host on screen holds it (a host that left and came back may find it taken)
  const keep: Record<string, number> = {}; const used = new Set<number>();
  for (const h of hosts) { const s = slots.current[h]; if (s !== undefined && !used.has(s)) { keep[h] = s; used.add(s); } }
  for (const h of hosts) if (!(h in keep)) { const free = [0, 1, 2, 3, 4, 5].find((i) => !used.has(i)) ?? 0; keep[h] = free; used.add(free); }
  slots.current = { ...slots.current, ...keep };
  const fmt = (v: number) => m === "error_rate" ? fmtPct(v, 2) : m === "p95" ? `${Math.round(v)} ms` : fmtN(v, lang);
  const data = settled(r.data?.series ?? [], r.data?.step_min ?? 1, rq.includes("last=")).map((x: any) => { const o: any = { t: x.t }; hosts.forEach((h, i) => { o[`h${i}`] = x[h]; }); return o; });
  return <div>
    <div className="row between" style={{ marginBottom: 8, gap: 8, flexWrap: "wrap" }}><div className="seg sm fv-seg">{(["lines", "errors", "error_rate", "p95", "sessions"] as Metric[]).map((k) => <button key={k} className={m === k ? "on" : ""} onClick={() => setM(k)}>{t(`hs_m_${k}`)}</button>)}</div>
      <span className="dim small">{t("hs_top", { n: hosts.length })}</span></div>
    <LineChartCard title={t(`hs_m_${m}`)} height={250} span={span} fill={0.1} data={data} series={hosts.map((h, i) => ({ key: `h${i}`, label: h, color: SLOT[keep[h]] }))}
      valueFormat={fmt} onPick={(x) => onZoom(x, r.data?.step_min ?? 1)} pickLabel={t("lc_zoom")} empty={!r.data ? t("loading") : r.data.source === "history" && m === "sessions" ? t("ses_hist") : undefined} />
  </div>;
}
