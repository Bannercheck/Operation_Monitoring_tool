import { useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { get } from "../../api";
import { useT } from "../../i18n";
import { Workflow } from "lucide-react";
import { Card, Empty, Kpi, Tabs, Hint } from "../../components/ui";
import { useAuth } from "../../auth";
import { AppIntegrationsView } from "./AppIntegrations";
import { CAT_COLOR, ConfLegend, type Sel, TechDrawer, TechLabel, confK, fmtTs, parseSel, sameSel, selStr, stackName } from "./IntegTech";

export { CAT_COLOR };

/** Integration map per host: who connects to this server, what runs on it (processes, listening ports, its technology stack,
 *  services seen in traces) and every system it integrates with, grouped by kind (database, messaging, API, SAP, identity, mail,
 *  file transfer), each with the technology it runs on (protocol · server product / framework, e.g. "REST API · FastAPI") and how
 *  sure that is. Built from the agents' TCP connections, so it shows integrations no log or document mentions. Every node, line
 *  and row opens a detail drawer (?ix=…, so a drawer can be linked to). */
const W = 940, LW = 244, RW = 244, CW = 272, ROW = 54, NH = 44;
const cut = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
/** Trims a label to a width in SVG user units, measured with the page font (falls back to a character estimate). */
let ctx2d: CanvasRenderingContext2D | null | undefined, fam = "sans-serif";
const NL = 12.5, NS = 11;                                  // .integ .topo-nl / .topo-ns font sizes
function pen(size: number, weight: number) {
  if (ctx2d === undefined) { try { ctx2d = document.createElement("canvas").getContext("2d"); fam = getComputedStyle(document.body).fontFamily || fam; } catch { ctx2d = null; } }
  if (ctx2d) ctx2d.font = `${weight} ${size}px ${fam}`;
  return ctx2d;
}
const textW = (s: string, size = NS, weight = 400) => { const c = pen(size, weight); return c ? c.measureText(s).width : s.length * size * 0.56; };
function fit(s: string, px: number, size = NS, weight = 400): string {
  const c = pen(size, weight);
  if (!c) return cut(s, Math.floor(px / (size * 0.56)));
  if (c.measureText(s).width <= px) return s;
  let lo = 0, hi = s.length;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (c.measureText(s.slice(0, mid) + "…").width <= px) lo = mid; else hi = mid - 1; }
  return s.slice(0, lo).replace(/[\s·,:;|/–-]+$/u, "") + "…";           // never end on a dangling separator ("name ·…")
}
/** The nodes a side has room for: when there are more, the busiest of every kind first (so no kind vanishes from the map), then
 *  the busiest of the rest; the original order (grouped by kind) is kept. The table below lists them all. */
function pickTop(xs: any[], cap: number): any[] {
  if (xs.length <= cap) return xs;
  const busy = [...xs].sort((a, b) => (b.connections ?? 0) - (a.connections ?? 0)); const keep = new Set<any>(), kinds = new Set<string>();
  for (const x of busy) if (keep.size < cap && !kinds.has(x.category)) { kinds.add(x.category); keep.add(x); }
  for (const x of busy) if (keep.size < cap) keep.add(x);
  return xs.filter((x) => keep.has(x));
}
const onKey = (f: () => void) => (e: KeyboardEvent) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); f(); } };

/** Stack chips of the centre box, wrapped into at most three rows. */
function chipRows(items: { label: string; key: string; conf: string }[], maxW: number, maxRows = 3) {
  const out: { label: string; key: string; conf: string; x: number; row: number; w: number }[] = []; let x = 0, row = 0;
  for (const it of items) {
    const label = fit(it.label, maxW - 24), w = 24 + textW(label);
    if (x > 0 && x + w > maxW) { row += 1; x = 0; }
    if (row >= maxRows) break;
    out.push({ ...it, label, x, row, w }); x += w + 5;
  }
  return { chips: out, rows: out.length ? out[out.length - 1].row + 1 : 0 };
}

function Node({ x, y, w, col, dashed, l1, l2, port, tech, on, title, onClick }: { x: number; y: number; w: number; col: string; dashed?: boolean; l1: string; l2: string; port: string | number; tech?: any; on: boolean;
  title: string; onClick: () => void }) {
  const k = confK(tech?.confidence); const id = `ic${Math.round(x)}-${Math.round(y)}`;
  return <g className={`in-n ${on ? "on" : ""}`} onClick={onClick} onKeyDown={onKey(onClick)} role="button" tabIndex={0} aria-label={title}>
    <title>{title}</title>
    <clipPath id={id}><rect x={x + 4} y={y - NH / 2} width={w - 10} height={NH} /></clipPath>
    <rect className="nbox" x={x} y={y - NH / 2} width={w} height={NH} rx={8} stroke={col} strokeDasharray={dashed ? "4 3" : undefined} />
    <g clipPath={`url(#${id})`}>
      <text x={x + 11} y={y - 4} className="topo-nl">{fit(l1, w - 22, NL, 600)}</text>
      <circle className={`cm ${k}`} cx={x + 15} cy={y + 10} r={3.6} />
      <text x={x + 24} y={y + 14} className={`topo-ns tl2 ${k}`}>{fit(l2, w - 36 - textW(` · :${port}`))} · :{port}</text>
    </g>
  </g>;
}

function Edge({ d, col, width, dashed, on, title, onClick }: { d: string; col: string; width: number; dashed?: boolean; on: boolean; title: string; onClick: () => void }) {
  return <g className={`in-e ${on ? "on" : ""}`} onClick={onClick}><title>{title}</title>
    <path d={d} className="in-ev" fill="none" stroke={col} strokeWidth={width} strokeDasharray={dashed ? "5 4" : undefined} />
    <path d={d} className="in-hit" fill="none" stroke="transparent" strokeWidth={Math.max(12, width + 8)} /></g>;
}

/** Centre box geometry: host, addresses, processes, stack chips, listening ports (with their technology), services. */
function boxLayout(m: any, w: number) {
  const stack = (m.stack ?? []).map((s: any) => ({ label: stackName(s), key: s.key ?? s.name, conf: s.confidence }));
  const { chips, rows } = chipRows(stack, w - 24);
  const listens: any[] = m.listens.slice(0, 6), svcs: string[] = m.services.slice(0, 4);
  const chipTop = 66, listTop = chipTop + (rows ? rows * 22 + 12 : 4);
  const svcTop = listens.length ? listTop + listens.length * 17 + 2 : listTop;
  const last = svcs.length ? svcTop + (svcs.length - 1) * 16 : listens.length ? listTop + (listens.length - 1) * 17 : rows ? chipTop + rows * 22 - 8 : 58;
  const h = last + 14;
  return { chips, chipTop, listens, listTop, svcs, svcTop, h };
}

function HostBox({ m, w, L, sel, onSel }: { m: any; w: number; L: ReturnType<typeof boxLayout>; sel: Sel | null; onSel: (s: Sel) => void }) {
  const { t } = useT(); const nav = useNavigate();
  const host = () => onSel({ dir: "host", key: "" });
  return <>
    <g className={`in-host ${sel?.dir === "host" ? "on" : ""}`} onClick={host} onKeyDown={onKey(host)} role="button" tabIndex={0}>
      <title>{`${m.host} · ${t("itc_stack")}`}</title>
      <rect className="hbox" width={w} height={L.h} rx={12} />
      <text x={w / 2} y={24} textAnchor="middle" className="topo-nl" style={{ fontSize: 14 }}>{m.app ? "▣" : "🖥"} {fit(m.host, w - 40, 14, 600)}</text>
      <text x={w / 2} y={40} textAnchor="middle" className="topo-ns">{fit(m.app ? `${t("integ_members")}: ${(m.members ?? []).map((x: any) => x.host).join(", ") || "-"}` : m.ips.join(", ") || t("integ_no_ip"), w - 24)}</text>
      <text x={12} y={58} className="topo-ns">{fit(`${t("integ_procs")}: ${m.processes.slice(0, 5).join(", ") || "-"}`, w - 24)}</text>
    </g>
    {L.chips.map((c, i) => { const y = L.chipTop + c.row * 22, open = () => onSel({ dir: "host", key: c.key });
      return <g key={"c" + i} className="in-chip" onClick={open} onKeyDown={onKey(open)} role="button" tabIndex={0}>
        <title>{`${c.label} · ${t(`itc_conf_${confK(c.conf)}`)}`}</title>
        <rect className={`schip ${confK(c.conf)}`} x={12 + c.x} y={y} width={c.w} height={18} rx={9} />
        <circle className={`cm ${confK(c.conf)}`} cx={12 + c.x + 9} cy={y + 9} r={3} />
        <text x={12 + c.x + 16} y={y + 13} className="topo-ns schip-t">{c.label}</text></g>; })}
    {L.listens.map((l, i) => { const s: Sel = { dir: "listen", peer: "", port: String(l.port) }; const k = confK(l.tech?.confidence); const y = L.listTop + i * 17;
      return <g key={"p" + i} className={`in-row ${sameSel(sel, s) ? "on" : ""}`} onClick={() => onSel(s)} onKeyDown={onKey(() => onSel(s))} role="button" tabIndex={0}>
        <title>{`:${l.port} · ${l.tech?.label ?? l.protocol} · ${l.process} · ${l.peers} ${t("topo_conn")}`}</title>
        <rect x={6} y={y - 12} width={w - 12} height={16} rx={4} className="in-row-bg" />
        <circle className={`cm ${k}`} cx={16} cy={y - 4} r={3} />
        <text x={24} y={y} className="topo-ns">{fit(`⇠ :${l.port} ${l.tech?.label ?? l.protocol}${k === "olasi" ? " ?" : ""} · ${l.peers}`, w - 36)}</text></g>; })}
    {L.svcs.map((s, i) => <text key={"s" + i} x={14} y={L.svcTop + i * 16} className="topo-ns in-svc" onClick={() => nav(`/services/${encodeURIComponent(s)}`)}>◆ {fit(s, w - 34)}</text>)}
  </>;
}

type DProps = { m: any; sel: Sel | null; onSel: (s: Sel) => void };
const lab = (x: any) => (x.tech ? `${x.tech.label}${confK(x.tech.confidence) === "olasi" ? " ?" : ""}` : x.protocol);
/** A peer's address to show under its name: a named target's IP, a caller's known address (nothing when the name is the IP). */
export const ipOf = (x: any): string => {
  if (x.target !== undefined) return x.known ? x.ip ?? "" : "";
  const ip = (x.ips ?? [])[0] ?? "";
  return ip && ip !== x.source ? ip : "";
};
const appTag = (x: any) => (x.apps?.length ? ` ▣ ${x.apps[0]}` : "");
function useNodeText() {
  const { t } = useT();
  const conf = (x: any) => (x.tech ? t(`itc_conf_${confK(x.tech.confidence)}`) : "");
  const extra = (x: any) => [ipOf(x) && `IP ${ipOf(x)}`, x.apps?.length && `▣ ${x.apps.join(", ")}`, x.via?.length && `${t("integ_via")}: ${x.via.join(", ")}`].filter(Boolean).join("\n");
  return {
    inTitle: (m: any, c: any) => `${c.source} → ${m.host}:${c.port}\n${lab(c)} (${conf(c)})\n${c.connections} ${t("topo_conn")} · ${c.processes.join(", ")}${extra(c) ? "\n" + extra(c) : ""}`,
    outTitle: (m: any, o: any) => `${m.host} → ${o.target}:${o.port}\n${lab(o)} (${conf(o)})\n${o.connections} ${t("topo_conn")} · ${o.processes.join(", ")}${extra(o) ? "\n" + extra(o) : ""}`,
    inL1: (c: any) => `${c.source}${appTag(c)}${c.tech?.client?.name ? ` · ${c.tech.client.name}` : c.processes.length ? ` · ${c.processes[0]}` : ""}`,
    outL1: (o: any) => `${t(`integ_cat_${o.category}`)} · ${o.target}${appTag(o)}`,
    l2: (x: any) => (ipOf(x) ? `${ipOf(x)} · ${lab(x)}` : lab(x)),
    more: (n: number) => `+${n} ${t("itc_more")}`,
  };
}
const edgeW = (n: number) => 1.4 + Math.log1p(n) * 0.9;
const thinW = (n: number) => 1.2 + Math.log1p(n) * 0.25;     // many lines side by side (comb, phone bus): traffic shown more quietly
const CAT_KEYS = Object.keys(CAT_COLOR);

/** Wide screens: callers on the left, the host in the middle, its integrations on the right (an empty side is cropped away). */
function Diagram({ m, sel, onSel }: DProps) {
  const { t } = useT(); const tx = useNodeText();
  const left: any[] = pickTop(m.inbound, 14), right: any[] = pickTop(m.outbound, 18);
  const L = boxLayout(m, CW);
  const H = Math.max(200, 30 + Math.max(left.length, right.length, 2) * ROW, L.h + 30), cx = W / 2, cy = H / 2;
  const bx = cx - CW / 2, bxr = cx + CW / 2, by = cy - L.h / 2;
  const ly = (i: number) => (H - left.length * ROW) / 2 + i * ROW + ROW / 2;
  const ry = (i: number) => (H - right.length * ROW) / 2 + i * ROW + ROW / 2;
  const endY = (i: number, n: number) => cy + (n > 1 ? (i / (n - 1) - 0.5) * Math.min(L.h - 36, n * 12) : 0);
  const xl = 8 + LW, xr = W - 8 - RW;
  /** A side with many nodes: curves squeezed into the narrow gap would pile up into one tangle, so its lines leave the box from
   *  its top / bottom edge like a comb and turn once towards their node (the outermost node nearest the centre: no two lines
   *  cross and each stays apart enough to be clicked). Nodes level with the box keep a short curve into its side. */
  const R = 8, dense = (n: number) => n > 8;
  const comb = (ys: number[], side: 1 | -1) => {
    const xs: Record<number, number> = {}, span = CW / 2 - 26;
    const up = ys.map((y, i) => [y, i]).filter(([y]) => y + R + 2 < by), down = ys.map((y, i) => [y, i]).filter(([y]) => y - R - 2 > by + L.h).reverse();
    for (const g of [up, down]) { const st = g.length > 1 ? Math.min(16, span / (g.length - 1)) : 0; g.forEach(([, i], k) => { xs[i] = cx + side * (14 + k * st); }); }
    return xs;
  };
  const clampY = (y: number) => Math.min(by + L.h - 16, Math.max(by + 16, y));
  const lc = dense(left.length) ? comb(left.map((_, i) => ly(i)), -1) : null, rc = dense(right.length) ? comb(right.map((_, i) => ry(i)), 1) : null;
  const lPath = (i: number, y: number) => { const x = lc?.[i];
    if (lc && x != null) return y < by ? `M ${xl} ${y} H ${x - R} Q ${x} ${y} ${x} ${y + R} V ${by}` : `M ${xl} ${y} H ${x - R} Q ${x} ${y} ${x} ${y - R} V ${by + L.h}`;
    const ey = lc ? clampY(y) : endY(i, left.length); return `M ${xl} ${y} C ${xl + 50} ${y}, ${bx - 50} ${ey}, ${bx} ${ey}`; };
  const rPath = (i: number, y: number) => { const x = rc?.[i];
    if (rc && x != null) return y < by ? `M ${x} ${by} V ${y + R} Q ${x} ${y} ${x + R} ${y} H ${xr}` : `M ${x} ${by + L.h} V ${y - R} Q ${x} ${y} ${x + R} ${y} H ${xr}`;
    const ey = rc ? clampY(y) : endY(i, right.length); return `M ${bxr} ${ey} C ${bxr + 50} ${ey}, ${xr - 50} ${y}, ${xr} ${y}`; };
  const x0 = left.length || m.inbound.length ? 0 : bx - 40, x1 = right.length || m.outbound.length ? W : bxr + 40;
  return <div className="integ-scroll"><svg viewBox={`${x0} 0 ${x1 - x0} ${H}`} className="topo integ wide" style={{ maxWidth: Math.round((x1 - x0) * 1.08) }} role="img" aria-label={`${t("integ_map")} · ${m.host}`}>
    {left.map((c, i) => { const y = ly(i), col = CAT_COLOR[c.category] ?? "#94a3b8"; const s: Sel = { dir: "in", peer: c.source, port: String(c.port) };
      const on = sameSel(sel, s), title = tx.inTitle(m, c);
      return <g key={"l" + i}>
        <Edge d={lPath(i, y)} col={col} width={(lc ? thinW : edgeW)(c.connections)} on={on} title={title} onClick={() => onSel(s)} />
        <Node x={8} y={y} w={LW} col={col} on={on} title={title} onClick={() => onSel(s)} tech={c.tech} l1={tx.inL1(c)} l2={tx.l2(c)} port={c.port} /></g>; })}
    {right.map((o, i) => { const y = ry(i), col = CAT_COLOR[o.category] ?? "#94a3b8"; const s: Sel = { dir: "out", peer: o.target, port: String(o.port) };
      const on = sameSel(sel, s), title = tx.outTitle(m, o);
      return <g key={"r" + i}>
        <Edge d={rPath(i, y)} col={col} width={(rc ? thinW : edgeW)(o.connections)} dashed={!o.known} on={on} title={title} onClick={() => onSel(s)} />
        <Node x={xr} y={y} w={RW} col={col} dashed={!o.known} on={on} title={title} onClick={() => onSel(s)} tech={o.tech} l1={tx.outL1(o)} l2={tx.l2(o)} port={o.port} /></g>; })}
    <g transform={`translate(${bx},${by})`}><HostBox m={m} w={CW} L={L} sel={sel} onSel={onSel} /></g>
    {m.inbound.length > left.length && <text x={12} y={H - 6} className="topo-ns">{tx.more(m.inbound.length - left.length)}</text>}
    {m.outbound.length > right.length && <text x={W - 12} y={H - 6} textAnchor="end" className="topo-ns">{tx.more(m.outbound.length - right.length)}</text>}
  </svg></div>;
}

/** Phone width: the same map top to bottom (callers, the host, its integrations), each line running along a bus on the left. */
function VDiagram({ m, sel, onSel }: DProps) {
  const { t } = useT(); const tx = useNodeText();
  const VW = 380, X = 34, NW = VW - X - 6;
  const left: any[] = pickTop(m.inbound, 14), right: any[] = pickTop(m.outbound, 18);
  const L = boxLayout(m, NW);
  let y = 10;
  const inY = left.map((_, i) => y + i * ROW + NH / 2); y += left.length * ROW + (left.length ? 12 : 0);
  const top = y; y += L.h + 16;
  const outY = right.map((_, i) => y + i * ROW + NH / 2); y += right.length * ROW;
  const H = y + (m.inbound.length > left.length || m.outbound.length > right.length ? 16 : 0);
  // one bus lane per kind: lines of the same kind (same colour) merge into one trunk instead of a striped band of every colour
  const lane = (cat: string) => Math.max(0, CAT_KEYS.indexOf(cat)) % 5, bus = (cat: string) => 8 + lane(cat) * 5;
  return <svg viewBox={`0 0 ${VW} ${H}`} className="topo integ tall" role="img" aria-label={`${t("integ_map")} · ${m.host}`}>
    {left.map((c, i) => { const yy = inY[i], b = bus(c.category), col = CAT_COLOR[c.category] ?? "#94a3b8"; const s: Sel = { dir: "in", peer: c.source, port: String(c.port) };
      const on = sameSel(sel, s), title = tx.inTitle(m, c), ey = top + 18 + lane(c.category) * 6;
      return <g key={"l" + i}>
        <Edge d={`M ${X} ${yy} H ${b + 6} Q ${b} ${yy} ${b} ${yy + 6} V ${ey - 6} Q ${b} ${ey} ${b + 6} ${ey} H ${X}`} col={col} width={thinW(c.connections)} on={on} title={title} onClick={() => onSel(s)} />
        <Node x={X} y={yy} w={NW} col={col} on={on} title={title} onClick={() => onSel(s)} tech={c.tech} l1={tx.inL1(c)} l2={tx.l2(c)} port={c.port} /></g>; })}
    {right.map((o, i) => { const yy = outY[i], b = bus(o.category), col = CAT_COLOR[o.category] ?? "#94a3b8"; const s: Sel = { dir: "out", peer: o.target, port: String(o.port) };
      const on = sameSel(sel, s), title = tx.outTitle(m, o), sy = top + L.h - 18 - lane(o.category) * 6;
      return <g key={"r" + i}>
        <Edge d={`M ${X} ${sy} H ${b + 6} Q ${b} ${sy} ${b} ${sy + 6} V ${yy - 6} Q ${b} ${yy} ${b + 6} ${yy} H ${X}`} col={col} width={thinW(o.connections)} dashed={!o.known} on={on} title={title} onClick={() => onSel(s)} />
        <Node x={X} y={yy} w={NW} col={col} dashed={!o.known} on={on} title={title} onClick={() => onSel(s)} tech={o.tech} l1={tx.outL1(o)} l2={tx.l2(o)} port={o.port} /></g>; })}
    <g transform={`translate(${X},${top})`}><HostBox m={m} w={NW} L={L} sel={sel} onSel={onSel} /></g>
    {(m.inbound.length > left.length || m.outbound.length > right.length) && <text x={X} y={H - 4} className="topo-ns">{tx.more(m.inbound.length - left.length + m.outbound.length - right.length)}</text>}
  </svg>;
}

/** Picks the layout by the card's width (not the window's), so the map also fits a narrow column. */
export function MapView(p: DProps) {
  const ref = useRef<HTMLDivElement>(null); const [w, setW] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current; if (!el) return;
    setW(el.clientWidth);
    const ro = new ResizeObserver(() => setW(el.clientWidth)); ro.observe(el); return () => ro.disconnect();
  }, []);
  return <div ref={ref} className="integ-map">{w === 0 ? null : w < 600 ? <VDiagram {...p} /> : <Diagram {...p} />}</div>;
}

export function IntegrationsView() {
  const { t } = useT(); const [sp, setSp] = useSearchParams();
  const [q, setQ] = useState(""); const minutes = Number(sp.get("minutes") || 1440);
  const hosts = useQuery({ queryKey: ["integ-hosts", minutes], queryFn: () => get(`/apm/hosts?minutes=${minutes}`), refetchInterval: 30000 });
  const list: any[] = hosts.data ?? [];
  const host = sp.get("host") || list.find((h) => h.agent)?.host || "";
  const sel = parseSel(sp.get("ix"));
  const pick = (h: string) => { const n = new URLSearchParams(sp); n.set("host", h); n.delete("ix"); setSp(n, { replace: true }); };
  const open = (s: Sel | null) => { const n = new URLSearchParams(sp); if (host) n.set("host", host); if (s) n.set("ix", selStr(s)); else n.delete("ix"); setSp(n, { replace: true }); };
  const m = useQuery({ queryKey: ["integ", host, minutes], queryFn: () => get(`/apm/hosts/${encodeURIComponent(host)}/integrations?minutes=${minutes}`), enabled: !!host, refetchInterval: 30000 });
  const d = m.data;
  const ql = q.trim().toLowerCase();
  const shown = useMemo(() => list.filter((h) => !ql || h.host.toLowerCase().includes(ql) || h.protocols.some((p: string) => p.includes(ql))
    || (h.tech ?? []).some((x: string) => x.toLowerCase().includes(ql))), [list, ql]);
  const names = useMemo(() => list.map((h) => h.host), [list]);
  if (!list.length) return <Card><Empty>{hosts.isLoading ? t("loading") : t("topo_no_flows")}</Empty></Card>;
  const row = (s: Sel) => ({ className: `trow ${sameSel(sel, s) ? "on" : ""}`, onClick: () => open(s), onKeyDown: onKey(() => open(s)), tabIndex: 0 });
  const hostLink = (h: string) => <a href="#" onClick={(e) => { e.preventDefault(); e.stopPropagation(); pick(h); }}><b>{h}</b></a>;
  return <div className="grid integ-grid">
    <Card title={t("integ_hosts")}>
      <input className="input" placeholder={t("itc_search")} title={t("itc_search_t")} value={q} onChange={(e) => setQ(e.target.value)} style={{ marginBottom: 8 }} />
      <div className="stack integ-hosts" style={{ gap: 2 }}>{shown.map((h) => <button key={h.host} className={`hostrow ${h.host === host ? "on" : ""}`} onClick={() => pick(h.host)}>
        <div style={{ minWidth: 0 }}><b>{h.agent ? "🖥" : "◌"} {h.host}</b>
          {h.ips?.length && h.ips[0] !== h.host ? <div className="dim small mono">{h.ips.slice(0, 3).join(", ")}</div> : null}
          {h.tech?.length ? <div className="htech small" title={h.tech.join(" · ")}>{h.tech.slice(0, 3).join(" · ")}</div> : null}
          <div className="dim small">⇠ {h.inbound} · ⇢ {h.outbound} · {h.protocols.slice(0, 4).join(", ")}</div></div></button>)}
        {!shown.length && <div className="dim small" style={{ padding: 8 }}>{t("none")}</div>}</div>
    </Card>
    <div className="stack" style={{ minWidth: 0 }}>
      {!d ? <Card><Empty>{m.isError ? String((m.error as any)?.message ?? "") : t("loading")}</Empty></Card> : <>
        <div className="grid k4">
          <Kpi value={d.inbound.length} label={t("integ_in")} sub={t("integ_in_sub")} accent="#60a5fa" />
          <Kpi value={d.outbound.length} label={t("integ_out")} sub={Object.entries(d.categories).map(([k, v]) => `${t(`integ_cat_${k}`)} ${v}`).join(" · ") || "-"} accent="#2dd4bf" />
          <Kpi value={d.listens.length} label={t("integ_listens")} sub={d.listens.map((l: any) => l.port).slice(0, 6).join(", ") || "-"} accent="#a78bfa" />
          <Kpi value={d.services.length} label={t("integ_services")} sub={d.services.slice(0, 3).join(", ") || t("integ_no_services")} accent="#fbbf24" />
        </div>
        <Card title={`${t("integ_map")} · ${d.host}`} right={<span className="dim small">{t("itc_click")}</span>}>
          {!d.inbound.length && !d.outbound.length && !d.listens.length ? <Empty>{t("integ_none")}</Empty> : <MapView m={d} sel={sel} onSel={open} />}
          {(d.stack ?? []).length > 0 && <div className="tstack-row"><span className="dim small">{t("itc_stack")}:</span>{d.stack.map((s: any, i: number) =>
            <button key={i} className="chip tstack-chip" onClick={() => open({ dir: "host", key: s.key ?? s.name })} title={t(`itc_layer_${s.layer}`)}>
              <span className={`tconf ${confK(s.confidence)} bare`}><i /></span>{stackName(s)}</button>)}</div>}
          <ConfLegend />
          {d.outbound.some((o: any) => !o.known) && <div className="tlegend tlegend-box"><span className="dim">{t("itc_leg_box")}</span>   {/* a dashed box is about the address, not the confidence */}
            <span className="tlg"><i className="lgbox" /><span className="dim">{t("itc_leg_known")}</span></span>
            <span className="tlg"><i className="lgbox dashed" /><span className="dim">{t("itc_leg_unknown")}</span></span></div>}
        </Card>
        <Card title={t("integ_out")}>{!d.outbound.length ? <Empty>{t("integ_none")}</Empty> :
          <div className="table-wrap"><table className="table integ-t"><thead><tr><th>{t("integ_kind")}</th><th>{t("integ_target")}</th><th>{t("itc_tech")}</th><th>{t("integ_proto")}</th><th>{t("integ_procs")}</th><th className="right">{t("topo_conn")}</th><th>{t("integ_last")}</th></tr></thead>
            <tbody>{d.outbound.map((o: any, i: number) => <tr key={i} {...row({ dir: "out", peer: o.target, port: String(o.port) })}>
              <td><span className="badge icat" style={{ ["--c" as any]: CAT_COLOR[o.category] ?? "#94a3b8" }}>{t(`integ_cat_${o.category}`)}</span></td>
              <td className="tgt">{o.known && names.includes(o.target) ? hostLink(o.target) : <b className={o.known ? "" : "mono"}>{o.target}</b>}{o.known ? <div className="dim small mono">{o.ip}</div> : <div className="dim small">{t("integ_unknown")}</div>}</td>
              <td><TechLabel tech={o.tech} /></td>
              <td className="mono">{o.protocol}:{o.port}</td><td className="small">{o.processes.join(", ")}</td><td className="num">{o.connections}</td><td className="small nowrap">{fmtTs(o.last_seen)}<div className="dim">{t("itc_first_short")} {fmtTs(o.first_seen)}</div></td></tr>)}</tbody></table></div>}</Card>
        <Card title={t("integ_in")}>{!d.inbound.length && !d.listens.length ? <Empty>{t("integ_none")}</Empty> :
          <div className="table-wrap"><table className="table integ-t"><thead><tr><th>{t("integ_source")}</th><th>{t("itc_tech")}</th><th>{t("integ_proto")}</th><th>{t("integ_procs")}</th><th className="right">{t("topo_conn")}</th><th>{t("integ_last")}</th></tr></thead>
            <tbody>{d.inbound.map((c: any, i: number) => <tr key={i} {...row({ dir: "in", peer: c.source, port: String(c.port) })}><td className="tgt">{names.includes(c.source) ? hostLink(c.source) : <b>{c.source}</b>}{ipOf(c) && <div className="dim small mono">{ipOf(c)}</div>}</td>
              <td><TechLabel tech={c.tech} /></td><td className="mono">{c.protocol}:{c.port}</td><td className="small">{c.processes.join(", ")}</td><td className="num">{c.connections}</td><td className="small">{fmtTs(c.last_seen)}</td></tr>)}
              {d.listens.map((l: any, i: number) => <tr key={"l" + i} {...row({ dir: "listen", peer: "", port: String(l.port) })}><td className="dim">{t("integ_listen_row")}</td>
                <td><TechLabel tech={l.tech} /></td><td className="mono">{l.protocol}:{l.port}</td><td className="small">{l.process}</td><td className="num">{l.peers}</td><td className="small">{fmtTs(l.last_seen)}</td></tr>)}</tbody></table></div>}</Card>
        {d.services.length > 0 && <div className="small">{t("integ_services")}: {d.services.map((s: string) => <Link key={s} to={`/services/${encodeURIComponent(s)}`} style={{ marginRight: 8 }}>{s}</Link>)}</div>}
        <div className="dim small">{t("integ_hint")} {t("itc_hint")}</div>
        {sel && <TechDrawer m={d} sel={sel} minutes={minutes} hosts={names} onClose={() => open(null)} onPick={pick} />}
      </>}
    </div>
  </div>;
}

/** The page behind the "Integrations" menu item: by server, or by application (its member servers folded into one map). */
export default function Integrations() {
  const { t } = useT(); const { can } = useAuth(); const [sp, setSp] = useSearchParams();
  const mode = can("page.apps") && sp.get("mode") === "app" ? "app" : "host";
  const setMode = (k: string) => { const n = new URLSearchParams(); if (sp.get("minutes")) n.set("minutes", sp.get("minutes")!); if (k === "app") n.set("mode", "app"); setSp(n, { replace: true }); };
  return <div className="stack">
    <div className="row between" style={{ flexWrap: "wrap" }}><div><h2><Workflow size={20} style={{ verticalAlign: -3 }} /> {t("nav_integ")}<Hint text={t("topo_sub_integrations")} /></h2></div>
      {can("page.apps") && <Tabs value={mode} onChange={setMode} tabs={[{ k: "host", label: `🖥 ${t("integ_by_host")}` }, { k: "app", label: `▣ ${t("integ_by_app")}` }]} />}</div>
    {mode === "app" ? <AppIntegrationsView /> : <IntegrationsView />}
  </div>;
}
