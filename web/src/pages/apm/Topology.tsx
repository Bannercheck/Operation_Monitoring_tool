import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Network } from "lucide-react";
import { get } from "../../api";
import { useT } from "../../i18n";
import { Card, Empty, Kpi, Tabs } from "../../components/ui";
import { FailureMap } from "../Map";
import { ScopeBar, fmtMs, pct, rate, svcColor, useScope } from "./common";

/** Service map (SigNoz "Service Map"): who calls whom, measured. Traffic edges come from traces (solid: the logs named the
 *  parent; dashed: inferred from timing), network edges from the agents' TCP connections, and the error map stays as a tab. */
type N = { id: string; kind: string; [k: string]: any };
type E = { source: string; target: string; [k: string]: any };

function layout(nodes: N[], edges: E[]) {
  const out: Record<string, string[]> = {}, indeg: Record<string, number> = {};
  nodes.forEach((n) => { out[n.id] = []; indeg[n.id] = 0; });
  edges.forEach((e) => { if (out[e.source] && indeg[e.target] !== undefined) { out[e.source].push(e.target); indeg[e.target]++; } });
  const rank: Record<string, number> = {};
  nodes.forEach((n) => { rank[n.id] = 0; });
  for (let it = 0; it < nodes.length; it++) {                // longest path from the entry points; bounded so cycles cannot loop
    let moved = false;
    edges.forEach((e) => { if (rank[e.source] !== undefined && rank[e.target] !== undefined && rank[e.target] < rank[e.source] + 1 && rank[e.source] + 1 < nodes.length) { rank[e.target] = rank[e.source] + 1; moved = true; } });
    if (!moved) break;
  }
  nodes.forEach((n) => { if (n.kind === "app") rank[n.id] = 0; if (n.kind === "db") rank[n.id] = Math.max(rank[n.id], 1); });
  const cols: Record<number, N[]> = {};
  nodes.forEach((n) => { (cols[rank[n.id]] ||= []).push(n); });
  const order = { app: 0, service: 1, host: 1, db: 2, external: 3 } as Record<string, number>;
  const pos: Record<string, { x: number; y: number }> = {};
  const ranks = Object.keys(cols).map(Number).sort((a, b) => a - b);
  const colW = 230, rowH = 92;
  let maxRows = 1;
  ranks.forEach((r, ci) => {
    const col = cols[r].sort((a, b) => (order[a.kind] ?? 1) - (order[b.kind] ?? 1) || a.id.localeCompare(b.id));
    maxRows = Math.max(maxRows, col.length);
    col.forEach((n, i) => { pos[n.id] = { x: 90 + ci * colW, y: 60 + i * rowH }; });
  });
  ranks.forEach((r) => { const col = cols[r]; const off = ((maxRows - col.length) * rowH) / 2; col.forEach((n) => { pos[n.id].y += off; }); });
  return { pos, width: 90 * 2 + Math.max(0, ranks.length - 1) * colW, height: 60 * 2 + (maxRows - 1) * rowH };
}

const health = (er?: number | null) => (er === null || er === undefined ? "#64748b" : er >= 0.05 ? "#f87171" : er > 0 ? "#fbbf24" : "#34d399");

function Graph({ nodes, edges, mode }: { nodes: N[]; edges: E[]; mode: "traffic" | "network" }) {
  const { t } = useT(); const nav = useNavigate(); const sc = useScope();
  const [hover, setHover] = useState<string | null>(null);
  const { pos, width, height } = useMemo(() => layout(nodes, edges), [nodes, edges]);
  const maxCalls = Math.max(1, ...edges.map((e) => e.calls ?? e.connections ?? 1));
  const near = (id: string) => !hover || hover === id || edges.some((e) => (e.source === hover && e.target === id) || (e.target === hover && e.source === id));
  return <div style={{ overflow: "auto" }}><svg width={Math.max(width, 600)} height={Math.max(height, 220)} className="topo">
    <defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#64748b" /></marker></defs>
    {edges.map((e, i) => { const a = pos[e.source], b = pos[e.target]; if (!a || !b) return null;
      const w = 1.2 + 4 * Math.log1p(e.calls ?? e.connections ?? 1) / Math.log1p(maxCalls);
      const x1 = a.x + 34, x2 = b.x - 38, mx = (x1 + x2) / 2;
      const d = b.x > a.x ? `M ${x1} ${a.y} C ${mx} ${a.y}, ${mx} ${b.y}, ${x2} ${b.y}` : `M ${a.x} ${a.y + 30} C ${a.x} ${a.y + 90}, ${b.x} ${b.y + 90}, ${b.x} ${b.y + 34}`;
      const col = mode === "network" ? "#60a5fa" : health(e.error_rate);
      const dim = hover && !(hover === e.source || hover === e.target);
      const label = mode === "network" ? `${e.protocol} · ${e.connections} ${t("topo_conn")}` : `${e.calls} ${t("apm_calls")} · P95 ${fmtMs(e.p95_ms)}${e.errors ? ` · ${pct(e.error_rate, 1)} ${t("apm_err")}` : ""}`;
      return <g key={i} opacity={dim ? 0.15 : 1}><path d={d} fill="none" stroke={col} strokeWidth={w} strokeOpacity={0.75} strokeDasharray={e.evidence === "explicit" ? undefined : "6 5"} markerEnd="url(#arr)">
        <title>{`${e.source} → ${e.target}\n${label}\n${t(`topo_ev_${e.evidence}`)}`}</title></path>
        {!hover || hover === e.source || hover === e.target ? <text x={(x1 + x2) / 2} y={(a.y + b.y) / 2 - 6} className="topo-el">{label}</text> : null}</g>; })}
    {nodes.map((n) => { const p = pos[n.id]; if (!p) return null; const col = n.kind === "db" ? "#a78bfa" : n.kind === "app" ? "#60a5fa" : health(n.error_rate);
      const r = n.kind === "service" ? 14 + Math.min(16, 3 * Math.log1p(n.calls ?? 0)) : 22;
      const click = () => { if (mode === "network") nav(`/integrations?host=${encodeURIComponent(n.id)}`); else if (n.kind === "service" || n.kind === "app") nav(`/services/${encodeURIComponent(n.id)}${sc.link()}`); };
      return <g key={n.id} transform={`translate(${p.x},${p.y})`} opacity={near(n.id) ? 1 : 0.2} onMouseEnter={() => setHover(n.id)} onMouseLeave={() => setHover(null)} onClick={click} style={{ cursor: "pointer" }}>
        {n.kind === "db" ? <><rect x={-24} y={-20} width={48} height={40} rx={10} fill="#1e1b4b" stroke={col} strokeWidth={2} /><text y={6} textAnchor="middle" fontSize={16}>🛢</text></>
          : n.kind === "app" ? <><rect x={-20} y={-24} width={40} height={48} rx={9} fill="#0c1e36" stroke={col} strokeWidth={2} /><text y={6} textAnchor="middle" fontSize={16}>📱</text></>
          : n.kind === "host" ? <><rect x={-26} y={-18} width={52} height={36} rx={6} fill="#0b1220" stroke={n.agent ? "#2dd4bf" : "#64748b"} strokeWidth={2} strokeDasharray={n.agent ? undefined : "4 3"} /><text y={6} textAnchor="middle" fontSize={15}>🖥</text></>
          : <><circle r={r} fill={svcColor(n.id)} fillOpacity={0.18} stroke={col} strokeWidth={3} /><circle r={4} fill={col} /></>}
        <text y={(n.kind === "service" ? r : 26) + 16} textAnchor="middle" className="topo-nl">{n.id.length > 26 ? n.id.slice(0, 25) + "…" : n.id}</text>
        {n.kind === "service" && n.calls ? <text y={(r) + 30} textAnchor="middle" className="topo-ns">{rate(n.rps)}/s · P95 {fmtMs(n.p95_ms)}{n.error_rate ? ` · ${pct(n.error_rate, 1)}` : ""}</text> : null}
        {n.kind === "host" && n.processes?.length ? <text y={42} textAnchor="middle" className="topo-ns">{n.processes.slice(0, 3).join(", ")}</text> : null}
        <title>{n.id}</title>
      </g>; })}
  </svg></div>;
}

export default function ServiceMap() {
  const { t } = useT(); const sc = useScope(); const [sp, setSp] = useSearchParams();
  const tab = (sp.get("view") || "traffic") as "traffic" | "network" | "errors";
  const q = useQuery({ queryKey: ["topology", sc.qs()], queryFn: () => get(`/apm/topology?${sc.qs()}`), refetchInterval: sc.live ? 20000 : false, enabled: tab === "traffic" || tab === "network" });
  const s = q.data?.services; const n = q.data?.network;
  const setTab = (k: string) => { const x = new URLSearchParams(sp); if (k === "traffic") x.delete("view"); else x.set("view", k); setSp(x, { replace: true }); };
  return <div className="stack">
    <div className="row between"><div><h2><Network size={20} style={{ verticalAlign: -3 }} /> {t("map_title")}</h2><span className="muted small">{t(`topo_sub_${tab}`)}</span></div>{(tab === "traffic" || tab === "network") && <ScopeBar />}</div>
    <Tabs value={tab} onChange={setTab} tabs={[{ k: "traffic", label: t("topo_tab_traffic") }, { k: "network", label: t("topo_tab_network") }, { k: "errors", label: t("topo_tab_errors") }]} />
    {tab === "traffic" && <>
      <div className="grid k4">
        <Kpi value={s?.nodes?.filter((x: N) => x.kind === "service").length ?? 0} label={t("apm_k_services")} sub={`${s?.nodes?.filter((x: N) => x.kind === "app").length ?? 0} ${t("topo_apps")} · ${s?.nodes?.filter((x: N) => x.kind === "db").length ?? 0} ${t("topo_dbs")}`} />
        <Kpi value={s?.edges?.length ?? 0} label={t("topo_edges")} accent="#60a5fa" sub={`${s?.traces ?? 0} ${t("topo_from_traces")}`} />
        <Kpi value={s?.edges?.filter((e: E) => e.evidence === "explicit").length ?? 0} label={t("topo_explicit")} accent="#2dd4bf" sub={t("topo_explicit_sub")} />
        <Kpi value={s?.edges?.filter((e: E) => e.evidence !== "explicit").length ?? 0} label={t("topo_inferred")} accent="#fbbf24" sub={t("topo_inferred_sub")} />
      </div>
      <Card>{!s?.nodes?.length ? <Empty>{t("topo_none")}</Empty> : <Graph nodes={s.nodes} edges={s.edges} mode="traffic" />}</Card>
      <div className="dim small">{t("topo_legend")}</div>
    </>}
    {tab === "network" && <>
      <Card>{!n?.nodes?.length ? <Empty>{t("topo_no_flows")}</Empty> : <Graph nodes={n.nodes} edges={n.edges} mode="network" />}</Card>
      <div className="dim small">{t("topo_net_legend")}</div>
    </>}
    {tab === "errors" && <FailureMap />}
  </div>;
}
