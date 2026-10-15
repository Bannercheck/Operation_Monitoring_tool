/** The dashboards' building blocks: a Panel (title, last refresh, refresh, ⋮ menu with fullscreen / explore / CSV, skeleton and
 *  empty state) on a 12-column grid, and the marks drawn inside it - stat (big number over a sparkline), donut, gauge with its
 *  threshold band, time chart (lines, areas, stacked bars) with a legend that carries each series' last and average value, and a
 *  table whose value column is an inline bar. Series colours are the validated slots --s1..--s8 (one hue per entity, never per
 *  rank); status colours --st-* are reserved for state and always come with a text label. */
import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { Bar, CartesianGrid, ComposedChart, Area, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Clock, Compass, Download, Inbox, Maximize2, Minimize2, MoreVertical, RotateCw } from "lucide-react";
import { useT } from "../../i18n";
import { useAuth } from "../../auth";
import { TipCard } from "../ops/LineChartCard";
import { fmtTime } from "../ops/util";

export const NowCtx = createContext<number>(0);
export const SER = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)", "var(--s7)", "var(--s8)"];
export const ST = { ok: "var(--st-ok)", warn: "var(--st-warn)", serious: "var(--st-serious)", crit: "var(--st-crit)", none: "var(--st-none)" } as const;
export type Lv = keyof typeof ST;
export const lvOf = (v: number | null | undefined, warn?: number | null, crit?: number | null): Lv =>
  v == null ? "none" : crit != null && v >= crit ? "crit" : warn != null && v >= warn ? "warn" : "ok";

type Q = { isLoading?: boolean; isFetching?: boolean; dataUpdatedAt?: number; refetch?: () => unknown };
export type Csv = { name: string; cols: string[]; rows: (string | number | null | undefined)[][] };

export function downloadCsv({ name, cols, rows }: Csv) {
  const esc = (v: any) => { const s = v == null ? "" : String(v); return /[",\n;]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  const body = [cols, ...rows].map((r) => r.map(esc).join(",")).join("\n");
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob(["﻿" + body], { type: "text/csv;charset=utf-8" }));
  a.download = `${name.replace(/[^\w.-]+/g, "-")}.csv`; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

/** time-series rows -> CSV (one column per series) */
export const seriesCsv = (name: string, data: any[], series: { key: string; label: string }[]): Csv =>
  ({ name, cols: ["time", ...series.map((s) => s.label)], rows: data.map((d) => [new Date(d.t * 1000).toISOString(), ...series.map((s) => d[s.key])]) });

export function ago(t: (k: string, kw?: any) => string, now: number, ts: number): string {
  if (!ts) return ""; const s = Math.max(0, Math.round((now - ts) / 1000));
  return s < 15 ? t("db_now") : s < 60 ? t("db_ago_s", { n: s }) : s < 3600 ? t("db_ago_m", { n: Math.round(s / 60) }) : t("db_ago_h", { n: Math.round(s / 3600) });
}

/** The permission each explorer page needs: "Keşfet" only shows when the page will open for this user (the routes themselves are
 *  not guarded, a page without its permission renders empty with 403s). */
const PAGE_PERM: Record<string, string[]> = { inventory: ["page.inv"], anomalies: ["page.anom"], thresholds: ["page.thr"], services: ["page.apm"], traces: ["page.apm"],
  exceptions: ["page.apm"], rum: ["page.rum"], logs: ["page.logs"], datasets: ["page.data"], map: ["page.map"], alerts: ["sys.notify", "act.alerts"] };
export function useCanOpen() {
  const { can } = useAuth();
  return (to?: string) => { const need = to ? PAGE_PERM[to.split(/[/?#]/)[1] ?? ""] : undefined; return !need || need.some(can); };
}

/** One panel on the grid. `w`: columns of 12 (tablet: wide panels take both columns; phone: one). `qs`: the queries it shows - their
 *  loading state, last refresh and refetch. `explore`: the page that holds the same data with the same filter. */
export function Panel({ title, sub, w = 4, qs = [], explore, csv, empty, emptyText, h, children, className = "", id, actions, tall }: {
  title: ReactNode; sub?: ReactNode; w?: number; qs?: Q[]; explore?: { to?: string; go?: () => void; label?: string }; csv?: () => Csv;
  empty?: boolean; emptyText?: string; h?: number; children?: ReactNode; className?: string; id?: string; actions?: ReactNode; tall?: boolean }) {
  const { t } = useT(); const now = useContext(NowCtx) || Date.now(); const nav = useNavigate(); const canOpen = useCanOpen();
  if (explore && !explore.go && !canOpen(explore.to)) explore = undefined;
  const [full, setFull] = useState(false); const [menu, setMenu] = useState(false); const box = useRef<HTMLDivElement>(null);
  useEffect(() => { if (!full) return; const k = (e: KeyboardEvent) => e.key === "Escape" && setFull(false); window.addEventListener("keydown", k); document.body.classList.add("pn-lock");
    return () => { window.removeEventListener("keydown", k); document.body.classList.remove("pn-lock"); }; }, [full]);
  useEffect(() => { if (!menu) return; const f = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setMenu(false); }; document.addEventListener("mousedown", f); return () => document.removeEventListener("mousedown", f); }, [menu]);
  const loading = qs.some((q) => q.isLoading); const fetching = qs.some((q) => q.isFetching && !q.isLoading);
  const upd = Math.max(0, ...qs.map((q) => q.dataUpdatedAt || 0));
  return <>
    {full && <div className="pn-bg" onMouseDown={() => setFull(false)} />}
    <section id={id} className={`pn w${w} ${tall ? "tall" : ""} ${full ? "full" : ""} ${className}`} style={h ? ({ "--ph": `${h}px` } as any) : undefined}>
      <header className="pn-h">
        <div className="pn-t"><span className="pn-tt">{title}</span>{sub && <span className="pn-s">{sub}</span>}</div>
        <div className="pn-a" ref={box}>
          {actions}
          {upd > 0 && <span className="pn-ago" title={new Date(upd).toLocaleTimeString()}><Clock size={11} />{ago(t, now, upd)}</span>}
          {qs.length > 0 && <button className="pn-i" onClick={() => qs.forEach((q) => q.refetch?.())} title={t("db_refresh")} aria-label={t("db_refresh")}><RotateCw size={13} className={fetching ? "spin" : ""} /></button>}
          <button className={`pn-i ${menu ? "on" : ""}`} onClick={() => setMenu(!menu)} title={t("db_menu")} aria-label={t("db_menu")}><MoreVertical size={14} /></button>
          {menu && <div className="pn-menu card" role="menu">
            <button role="menuitem" onClick={() => { setMenu(false); setFull(!full); }}>{full ? <Minimize2 size={14} /> : <Maximize2 size={14} />}{full ? t("db_full_exit") : t("db_full")}</button>
            {explore && <button role="menuitem" onClick={() => { setMenu(false); setFull(false); if (explore.go) explore.go(); else if (explore.to) nav(explore.to); }}><Compass size={14} />{t("db_explore")}{explore.label ? <span className="dim"> → {explore.label}</span> : null}</button>}
            {csv && <button role="menuitem" onClick={() => { setMenu(false); downloadCsv(csv()); }}><Download size={14} />{t("db_csv")}</button>}
          </div>}
        </div>
      </header>
      <div className="pn-body">{loading ? <Skel /> : empty ? <div className="pn-empty"><Inbox size={20} /><span>{emptyText ?? t("lc_nodata")}</span></div> : children}</div>
    </section>
  </>;
}

export const Skel = () => <div className="skel" aria-busy="true"><i style={{ width: "62%" }} /><i style={{ width: "88%" }} /><i style={{ width: "74%" }} /><i className="blk" /></div>;
export const Grid = ({ children }: { children: ReactNode }) => <div className="pg">{children}</div>;
export const Section = ({ children }: { children: ReactNode }) => <div className="pg-sec">{children}</div>;

/** Stat panel body: the number first, its change against the previous range, a line of context, a sparkline wash underneath. */
export function Stat({ value, unit, tone = "", delta, sub, spark, color = "var(--s1)", onClick }: {
  value: ReactNode; unit?: string; tone?: string; delta?: { text: string; good?: boolean | null }; sub?: ReactNode; spark?: (number | null)[]; color?: string; onClick?: () => void }) {
  return <button className={`stat ${tone ? "tone-" + tone : ""}`} onClick={onClick} disabled={!onClick}>
    <div className="stat-v">{value}{unit && <span className="stat-u">{unit}</span>}</div>
    <div className="stat-s">{delta?.text ? <span className={`stat-d ${delta.good == null ? "" : delta.good ? "good" : "bad"}`}>{delta.text}</span> : null}{sub}</div>
    {spark && spark.filter((x) => x != null).length > 1 && <SparkArea data={spark} color={color} />}
  </button>;
}

export function SparkArea({ data, color }: { data: (number | null)[]; color: string }) {
  const w = 300, h = 44; const vals = data.map((v) => (v == null ? null : v)); const max = Math.max(...(vals.filter((v) => v != null) as number[]), 1e-9);
  const pts = vals.map((v, i) => [(i / Math.max(vals.length - 1, 1)) * w, v == null ? null : h - 3 - (v / max) * (h - 8)] as const).filter((p) => p[1] != null) as [number, number][];
  if (pts.length < 2) return null;
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  return <svg className="stat-sp" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" aria-hidden><path d={`${line} L${pts[pts.length - 1][0]},${h} L${pts[0][0]},${h} Z`} fill={color} opacity={0.12} />
    <path d={line} fill="none" stroke={color} strokeWidth={2} vectorEffect="non-scaling-stroke" strokeLinejoin="round" strokeLinecap="round" /></svg>;
}

/** Donut with the total (or any node) in the middle and the parts as a clickable legend with their counts; 2px gaps between parts. */
export function Donut({ parts, center, label, size = 136, thick = 14, onPick, sel }: {
  parts: { key: string; label: string; value: number; color: string }[]; center: ReactNode; label?: ReactNode; size?: number; thick?: number; onPick?: (k: string) => void; sel?: string | null }) {
  const { lang } = useT(); const r = (size - thick) / 2 - 2; const C = 2 * Math.PI * r; const tot = parts.reduce((a, p) => a + p.value, 0); const gap = parts.filter((p) => p.value).length > 1 ? 2.5 : 0;
  let acc = 0;
  return <div className="dn">
    <div className="dn-c" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img">
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--line)" strokeWidth={thick} />
        {tot > 0 && parts.map((p) => { if (!p.value) return null; const len = (p.value / tot) * C; const off = acc; acc += len;
          return <circle key={p.key} className={`dn-p ${sel && sel !== p.key ? "dim" : ""}`} cx={size / 2} cy={size / 2} r={r} fill="none" stroke={p.color} strokeWidth={thick}
            strokeDasharray={`${Math.max(0.5, len - gap)} ${C - Math.max(0.5, len - gap)}`} strokeDashoffset={-off} transform={`rotate(-90 ${size / 2} ${size / 2})`}
            style={{ cursor: onPick ? "pointer" : undefined }} onClick={() => onPick?.(p.key)}><title>{`${p.label}: ${p.value}`}</title></circle>; })}
      </svg>
      <div className="dn-m">{center}{label && <span>{label}</span>}</div>
    </div>
    <div className="dn-l">{parts.map((p) => <button key={p.key} className={`dn-li ${sel === p.key ? "on" : ""}`} disabled={!onPick} onClick={() => onPick?.(p.key)}>
      <i style={{ background: p.color }} /><span>{p.label}</span><b>{p.value.toLocaleString(lang === "tr" ? "tr-TR" : "en-US")}</b><em>{tot ? `%${Math.round((p.value / tot) * 100)}` : ""}</em></button>)}</div>
  </div>;
}

const rad = (a: number) => ((a - 90) * Math.PI) / 180;
const pt = (cx: number, cy: number, r: number, a: number) => [cx + r * Math.cos(rad(a)), cy + r * Math.sin(rad(a))];
function arc(cx: number, cy: number, r: number, a0: number, a1: number) {
  const [x0, y0] = pt(cx, cy, r, a0); const [x1, y1] = pt(cx, cy, r, a1);
  return `M${x0.toFixed(2)},${y0.toFixed(2)} A${r},${r} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${x1.toFixed(2)},${y1.toFixed(2)}`;
}

/** 240° gauge: the value arc coloured by its level, a thin threshold band outside it (normal / warning / critical), the value and a
 *  worded state underneath so the colour never carries the meaning alone. */
export function Gauge({ value, warn, crit, max = 100, title, sub, onClick, size = 150 }: {
  value: number | null | undefined; warn?: number | null; crit?: number | null; max?: number; title: string; sub?: ReactNode; onClick?: () => void; size?: number }) {
  const { t } = useT(); const cx = size / 2, cy = size / 2 + 6, r = size / 2 - 16; const A0 = -120, A1 = 120; const span = A1 - A0;
  const at = (v: number) => A0 + (Math.max(0, Math.min(max, v)) / max) * span; const lv = lvOf(value, warn, crit);
  const bands: [number, number, string][] = [];
  if (warn != null) bands.push([0, warn, ST.ok]); if (warn != null || crit != null) bands.push([warn ?? 0, crit ?? max, ST.warn]); if (crit != null) bands.push([crit, max, ST.crit]);
  return <button className="gg" onClick={onClick} disabled={!onClick} aria-label={`${title} ${value == null ? "–" : Math.round(value)}%`}>
    <svg width="100%" height={size * 0.82} viewBox={`0 0 ${size} ${size * 0.82}`} preserveAspectRatio="xMidYMid meet">
      <path d={arc(cx, cy, r, A0, A1)} fill="none" stroke="var(--line)" strokeWidth={11} strokeLinecap="round" />
      {bands.map(([a, b, c]) => b > a ? <path key={a} d={arc(cx, cy, r + 10, at(a) + (a ? 1 : 0), at(b) - (b < max ? 1 : 0))} fill="none" stroke={c} strokeWidth={3} opacity={0.85} /> : null)}
      {value != null && value > 0 && <path d={arc(cx, cy, r, A0, Math.max(A0 + 1, at(value)))} fill="none" stroke={ST[lv]} strokeWidth={11} strokeLinecap="round" />}
      <text x={cx} y={cy + 2} textAnchor="middle" className="gg-v">{value == null ? "–" : `%${Math.round(value)}`}</text>
      <text x={cx} y={cy + 20} textAnchor="middle" className="gg-t">{title}</text>
    </svg>
    <div className="gg-s"><span className={`lvl lv-${lv}`}><i />{t("db_lv_" + lv)}</span>{sub}</div>
  </button>;
}

export type Ser = { key: string; label: string; color: string };
const AX = { tick: { fill: "var(--dim)", fontSize: 10.5 }, axisLine: false, tickLine: false } as const;

/** Time chart: lines, areas (stacked or not) or stacked columns, one y-axis, crosshair tooltip with every series at that moment,
 *  and a legend (right, or below on narrow panels) listing each series with its last and average value; hovering a legend row
 *  brings that series forward, clicking it calls onLegend (e.g. open the server) or else hides / shows it. */
export function TimeChart({ data, series, fmt, span, kind = "line", stacked, onPick, pickLabel, onLegend, yMax, legend = "right", avg = true, h }: {
  data: any[]; series: Ser[]; fmt: (v: number) => string; span: number; kind?: "line" | "area" | "bar"; stacked?: boolean; onPick?: (t: number) => void; pickLabel?: string;
  onLegend?: (key: string) => void; yMax?: number; legend?: "right" | "bottom" | "none"; avg?: boolean; h?: number }) {
  const { t } = useT(); const [hide, setHide] = useState<string[]>([]); const [hot, setHot] = useState<string | null>(null);
  const stats = useMemo(() => Object.fromEntries(series.map((s) => { const v = data.map((d) => d[s.key]).filter((x) => x != null) as number[];
    return [s.key, { last: v.length ? v[v.length - 1] : null, avg: v.length ? v.reduce((a, b) => a + b, 0) / v.length : null }]; })), [data, series]);
  const shown = series.filter((s) => !hide.includes(s.key));
  const lone = (key: string) => (p: any) => { const i = p.index; const v = (j: number) => data[j]?.[key]; return v(i) != null && v(i - 1) == null && v(i + 1) == null && p.cy != null
    ? <circle key={`${key}${i}`} cx={p.cx} cy={p.cy} r={3} fill={p.stroke} /> : <g key={`${key}${i}`} />; };
  const op = (k: string) => (hot && hot !== k ? 0.25 : 1);
  return <div className={`tc lg-${legend}`}>
    <div className="pn-ch" style={h ? { height: h } : undefined}><ResponsiveContainer>
      <ComposedChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }} barCategoryGap="18%" onClick={(e: any) => onPick && e?.activeLabel != null && onPick(Number(e.activeLabel))} style={{ cursor: onPick ? "pointer" : undefined }}>
        <CartesianGrid vertical={false} stroke="var(--line)" />
        <XAxis dataKey="t" tickFormatter={(v) => fmtTime(v, span)} {...AX} minTickGap={40} />
        <YAxis tickFormatter={(v) => fmt(v)} {...AX} width={48} domain={[0, yMax ?? "auto"]} />
        <Tooltip cursor={kind === "bar" ? { fill: "var(--line)" } : { stroke: "var(--line2)" }} isAnimationActive={false} wrapperStyle={{ outline: "none", zIndex: 5 }}
          content={(p: any) => { const row = p.active ? data.find((d) => d.t === Number(p.label)) : null;
            return row ? <TipCard ts={row.t} rows={shown.map((s) => ({ color: s.color, label: s.label, value: row[s.key] == null ? "–" : fmt(row[s.key]) }))} foot={onPick ? pickLabel : undefined} /> : null; }} />
        {shown.map((s, i) => kind === "bar"
          ? <Bar key={s.key} dataKey={s.key} name={s.label} stackId={stacked ? "a" : undefined} fill={s.color} fillOpacity={op(s.key)} stroke="var(--pn-surface)" strokeWidth={1} maxBarSize={24}
              radius={!stacked || i === shown.length - 1 ? [4, 4, 0, 0] : 0} isAnimationActive={false} />
          : kind === "area"
            ? <Area key={s.key} type="monotone" dataKey={s.key} name={s.label} stackId={stacked ? "a" : undefined} stroke={s.color} strokeOpacity={op(s.key)} strokeWidth={stacked ? 1.5 : 2}
                fill={s.color} fillOpacity={(stacked ? 0.28 : 0.1) * op(s.key)} dot={stacked ? false : lone(s.key)} activeDot={{ r: 4, stroke: "var(--pn-surface)", strokeWidth: 2 }} isAnimationActive={false} connectNulls={stacked} />
            : <Line key={s.key} type="monotone" dataKey={s.key} name={s.label} stroke={s.color} strokeOpacity={op(s.key)} strokeWidth={hot === s.key ? 2.5 : 2} dot={lone(s.key)}
                activeDot={{ r: 4, stroke: "var(--pn-surface)", strokeWidth: 2 }} isAnimationActive={false} />)}
      </ComposedChart></ResponsiveContainer></div>
    {legend !== "none" && series.length > 1 && <div className="tc-lg">
      {avg && <div className="tc-lh"><span /><span>{t("db_last")}</span><span>{t("db_avg")}</span></div>}
      {series.map((s) => { const off = hide.includes(s.key);
        return <button key={s.key} className={`tc-li ${off ? "off" : ""}`} onMouseEnter={() => setHot(s.key)} onMouseLeave={() => setHot(null)} title={onLegend ? t("db_open") : t("db_toggle")}
          onClick={() => (onLegend ? onLegend(s.key) : setHide(off ? hide.filter((x) => x !== s.key) : [...hide, s.key]))}>
          <span className="tc-n"><i style={{ background: s.color }} />{s.label}</span>
          {avg && <><b>{stats[s.key].last == null ? "–" : fmt(stats[s.key].last!)}</b><em>{stats[s.key].avg == null ? "–" : fmt(stats[s.key].avg!)}</em></>}
        </button>; })}
    </div>}
  </div>;
}

/** Table with an inline bar for the value column (the "devices by CPU" lists): every row clickable when onRow is given. */
export type Col = { k: string; label: ReactNode; num?: boolean; render?: (r: any) => ReactNode; bar?: { value: (r: any) => number | null; max?: number; lv?: (r: any) => Lv; color?: string; fmt: (v: number) => string } };
export function BarTable({ rows, cols, onRow, rowKey = (r: any, i: number) => String(i), sel }: { rows: any[]; cols: Col[]; onRow?: (r: any) => void; rowKey?: (r: any, i: number) => string; sel?: string }) {
  const tpl = cols.map((c, i) => (c.bar ? "minmax(70px, 1.1fr)" : c.num ? "minmax(40px, auto)" : i === 0 ? "minmax(72px, 1.5fr)" : "minmax(0, 1fr)")).join(" ");
  const maxes = Object.fromEntries(cols.filter((c) => c.bar).map((c) => [c.k, c.bar!.max ?? Math.max(1e-9, ...rows.map((r) => c.bar!.value(r) ?? 0))]));
  return <div className="bt" role="table">
    <div className="bt-r bt-h" role="row" style={{ gridTemplateColumns: tpl }}>
      {cols.map((c) => <span key={c.k} className={c.num || c.bar ? "num" : ""} role="columnheader">{c.label}</span>)}</div>
    {rows.map((r, i) => <div key={rowKey(r, i)} role="row" className={`bt-r ${onRow ? "click" : ""} ${sel && sel === rowKey(r, i) ? "on" : ""}`} onClick={() => onRow?.(r)}
      style={{ gridTemplateColumns: tpl }}>
      {cols.map((c) => { if (c.bar) { const v = c.bar.value(r); const lv = c.bar.lv?.(r);
          return <span key={c.k} className="bt-bar"><span className="bt-tr"><i style={{ width: `${v == null ? 0 : Math.max(2, Math.min(100, (v / maxes[c.k]) * 100))}%`, background: lv ? ST[lv] : c.bar.color ?? "var(--s1)" }} /></span><b>{v == null ? "–" : c.bar.fmt(v)}</b></span>; }
        return <span key={c.k} className={c.num ? "num" : "txt"} title={typeof r[c.k] === "string" ? r[c.k] : undefined}>{c.render ? c.render(r) : r[c.k] ?? "–"}</span>; })}
    </div>)}
  </div>;
}
