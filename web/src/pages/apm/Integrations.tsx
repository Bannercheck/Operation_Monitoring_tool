import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { get } from "../../api";
import { useT } from "../../i18n";
import { Workflow } from "lucide-react";
import { Card, Empty, Kpi } from "../../components/ui";

/** Integration map per host: who connects to this server, what runs on it (processes, listening ports, services seen in traces)
 *  and every system it integrates with, grouped by kind (database, messaging, API, SAP, identity, mail, file transfer). Built
 *  from the agents' TCP connections, so it shows integrations no log or document mentions. */
export const CAT_COLOR: Record<string, string> = { db: "#a78bfa", cache: "#c084fc", messaging: "#fb923c", api: "#60a5fa", sap: "#2dd4bf", identity: "#facc15",
  mail: "#f472b6", file: "#34d399", observability: "#94a3b8", infra: "#64748b", remote: "#f87171", other: "#94a3b8" };
const fmtTs = (s?: string) => (s ? s.replace("T", " ").slice(5, 16) : "-");

function Diagram({ m }: { m: any }) {
  const { t } = useT();
  const left = m.inbound.slice(0, 14), right = m.outbound.slice(0, 18);
  const rowH = 46, H = Math.max(220, 40 + Math.max(left.length, right.length, 3) * rowH), W = 1000, cx = W / 2, cy = H / 2;
  const ly = (i: number) => (H - left.length * rowH) / 2 + i * rowH + rowH / 2;
  const ry = (i: number) => (H - right.length * rowH) / 2 + i * rowH + rowH / 2;
  const boxH = Math.min(H - 20, 84 + Math.max(0, m.listens.length) * 16 + Math.min(4, m.services.length) * 16);
  return <div><svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ maxHeight: H + 40 }} className="topo integ">
    {left.map((c: any, i: number) => { const y = ly(i); const col = CAT_COLOR[c.category] ?? "#94a3b8";
      return <g key={"l" + i}><path d={`M 200 ${y} C ${cx - 150} ${y}, ${cx - 150} ${cy}, ${cx - 120} ${cy}`} fill="none" stroke={col} strokeWidth={1.6 + Math.log1p(c.connections)} strokeOpacity={0.6} />
        <rect x={10} y={y - 17} width={190} height={34} rx={8} fill="#0b1220" stroke={col} />
        <text x={20} y={y - 2} className="topo-nl">{c.source.length > 22 ? c.source.slice(0, 21) + "…" : c.source}</text>
        <text x={20} y={y + 12} className="topo-ns">{c.processes.join(", ").slice(0, 26)} → {c.protocol}:{c.port}</text></g>; })}
    {right.map((o: any, i: number) => { const y = ry(i); const col = CAT_COLOR[o.category] ?? "#94a3b8";
      return <g key={"r" + i}><path d={`M ${cx + 120} ${cy} C ${cx + 150} ${cy}, ${cx + 150} ${y}, ${W - 250} ${y}`} fill="none" stroke={col} strokeWidth={1.6 + Math.log1p(o.connections)} strokeOpacity={0.6} strokeDasharray={o.known ? undefined : "5 4"} />
        <rect x={W - 250} y={y - 17} width={240} height={34} rx={8} fill="#0b1220" stroke={col} strokeDasharray={o.known ? undefined : "4 3"} />
        <text x={W - 240} y={y - 2} className="topo-nl">{t(`integ_cat_${o.category}`)} · {o.target.length > 18 ? o.target.slice(0, 17) + "…" : o.target}</text>
        <text x={W - 240} y={y + 12} className="topo-ns">{o.protocol}:{o.port} · {o.connections} {t("topo_conn")} · {o.processes.join(", ").slice(0, 18)}</text></g>; })}
    <g transform={`translate(${cx - 120},${cy - boxH / 2})`}>
      <rect width={240} height={boxH} rx={12} fill="#0f1b2e" stroke="#2dd4bf" strokeWidth={2} />
      <text x={120} y={24} textAnchor="middle" className="topo-nl" style={{ fontSize: 14 }}>🖥 {m.host}</text>
      <text x={120} y={40} textAnchor="middle" className="topo-ns">{m.ips.join(", ") || t("integ_no_ip")}</text>
      <text x={14} y={60} className="topo-ns">{t("integ_procs")}: {m.processes.slice(0, 5).join(", ") || "-"}</text>
      {m.listens.slice(0, 6).map((l: any, i: number) => <text key={i} x={14} y={78 + i * 16} className="topo-ns">⇠ :{l.port} {l.protocol} · {l.process} · {l.peers} {t("topo_conn")}</text>)}
      {m.services.slice(0, 4).map((s: string, i: number) => <text key={"s" + i} x={14} y={78 + Math.min(6, m.listens.length) * 16 + i * 16} className="topo-ns">◆ {s}</text>)}
    </g>
  </svg></div>;
}

export function IntegrationsView() {
  const { t } = useT(); const [sp, setSp] = useSearchParams();
  const [q, setQ] = useState(""); const minutes = Number(sp.get("minutes") || 1440);
  const hosts = useQuery({ queryKey: ["integ-hosts", minutes], queryFn: () => get(`/apm/hosts?minutes=${minutes}`), refetchInterval: 30000 });
  const list: any[] = hosts.data ?? [];
  const host = sp.get("host") || list.find((h) => h.agent)?.host || "";
  const pick = (h: string) => { const n = new URLSearchParams(sp); n.set("host", h); setSp(n, { replace: true }); };
  const m = useQuery({ queryKey: ["integ", host, minutes], queryFn: () => get(`/apm/hosts/${encodeURIComponent(host)}/integrations?minutes=${minutes}`), enabled: !!host, refetchInterval: 30000 });
  const d = m.data;
  const shown = useMemo(() => list.filter((h) => !q || h.host.toLowerCase().includes(q.toLowerCase()) || h.protocols.some((p: string) => p.includes(q.toLowerCase()))), [list, q]);
  if (!list.length) return <Card><Empty>{t("topo_no_flows")}</Empty></Card>;
  return <div className="grid" style={{ gridTemplateColumns: "260px minmax(0, 1fr)", alignItems: "start" }}>
    <Card title={t("integ_hosts")}>
      <input className="input" placeholder={t("integ_search")} value={q} onChange={(e) => setQ(e.target.value)} style={{ marginBottom: 8 }} />
      <div className="stack" style={{ gap: 2, maxHeight: 640, overflow: "auto" }}>{shown.map((h) => <button key={h.host} className={`hostrow ${h.host === host ? "on" : ""}`} onClick={() => pick(h.host)}>
        <div><b>{h.agent ? "🖥" : "◌"} {h.host}</b><div className="dim small">⇠ {h.inbound} · ⇢ {h.outbound} · {h.protocols.slice(0, 4).join(", ")}</div></div></button>)}</div>
    </Card>
    <div className="stack">
      {!d ? <Card><Empty>…</Empty></Card> : <>
        <div className="grid k4">
          <Kpi value={d.inbound.length} label={t("integ_in")} sub={t("integ_in_sub")} accent="#60a5fa" />
          <Kpi value={d.outbound.length} label={t("integ_out")} sub={Object.entries(d.categories).map(([k, v]) => `${t(`integ_cat_${k}`)} ${v}`).join(" · ") || "-"} accent="#2dd4bf" />
          <Kpi value={d.listens.length} label={t("integ_listens")} sub={d.listens.map((l: any) => l.port).slice(0, 6).join(", ") || "-"} accent="#a78bfa" />
          <Kpi value={d.services.length} label={t("integ_services")} sub={d.services.slice(0, 3).join(", ") || t("integ_no_services")} accent="#fbbf24" />
        </div>
        <Card title={`${t("integ_map")} · ${d.host}`}>{!d.inbound.length && !d.outbound.length && !d.listens.length ? <Empty>{t("integ_none")}</Empty> : <Diagram m={d} />}</Card>
        <Card title={t("integ_out")}>{!d.outbound.length ? <Empty>{t("integ_none")}</Empty> :
          <div className="table-wrap"><table className="table"><thead><tr><th>{t("integ_kind")}</th><th>{t("integ_target")}</th><th>{t("integ_proto")}</th><th>{t("integ_procs")}</th><th className="right">{t("topo_conn")}</th><th>{t("integ_first")}</th><th>{t("integ_last")}</th></tr></thead>
            <tbody>{d.outbound.map((o: any, i: number) => <tr key={i}><td><span className="badge" style={{ color: CAT_COLOR[o.category], textTransform: "none" }}>{t(`integ_cat_${o.category}`)}</span></td>
              <td>{o.known ? <a href="#" onClick={(e) => { e.preventDefault(); pick(o.target); }}><b>{o.target}</b></a> : <span className="mono">{o.target}</span>}{o.known ? <div className="dim small mono">{o.ip}</div> : <div className="dim small">{t("integ_unknown")}</div>}</td>
              <td className="mono">{o.protocol}:{o.port}</td><td className="small">{o.processes.join(", ")}</td><td className="num">{o.connections}</td><td className="small">{fmtTs(o.first_seen)}</td><td className="small">{fmtTs(o.last_seen)}</td></tr>)}</tbody></table></div>}</Card>
        <Card title={t("integ_in")}>{!d.inbound.length && !d.listens.length ? <Empty>{t("integ_none")}</Empty> :
          <div className="table-wrap"><table className="table"><thead><tr><th>{t("integ_source")}</th><th>{t("integ_proto")}</th><th>{t("integ_procs")}</th><th className="right">{t("topo_conn")}</th><th>{t("integ_last")}</th></tr></thead>
            <tbody>{d.inbound.map((c: any, i: number) => <tr key={i}><td><a href="#" onClick={(e) => { e.preventDefault(); pick(c.source); }}><b>{c.source}</b></a></td><td className="mono">{c.protocol}:{c.port}</td><td className="small">{c.processes.join(", ")}</td><td className="num">{c.connections}</td><td className="small">{fmtTs(c.last_seen)}</td></tr>)}
              {d.listens.map((l: any, i: number) => <tr key={"l" + i}><td className="dim">{t("integ_listen_row")}</td><td className="mono">{l.protocol}:{l.port}</td><td className="small">{l.process}</td><td className="num">{l.peers}</td><td className="small">{fmtTs(l.last_seen)}</td></tr>)}</tbody></table></div>}</Card>
        {d.services.length > 0 && <div className="small">{t("integ_services")}: {d.services.map((s: string) => <Link key={s} to={`/services/${encodeURIComponent(s)}`} style={{ marginRight: 8 }}>{s}</Link>)}</div>}
        <div className="dim small">{t("integ_hint")}</div>
      </>}
    </div>
  </div>;
}

/** The page behind the "Integrations" menu item. */
export default function Integrations() {
  const { t } = useT();
  return <div className="stack">
    <div><h2><Workflow size={20} style={{ verticalAlign: -3 }} /> {t("nav_integ")}</h2><span className="muted small">{t("topo_sub_integrations")}</span></div>
    <IntegrationsView />
  </div>;
}

