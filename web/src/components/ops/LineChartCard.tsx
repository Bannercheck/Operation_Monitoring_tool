import { useId, useMemo } from "react";
import { Area, CartesianGrid, ComposedChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useT } from "../../i18n";
import { fmtTime } from "./util";

/** A small time-series panel in one look across the screen (latency, volume, sessions, per-host lines): monospace title and axes,
 *  each line over a soft gradient fading to nothing, dots only at both ends so the shape reads first, a dashed crosshair and one
 *  tooltip card listing every series at that moment. Clicking a bar calls onPick: recharts tooltips cannot be clicked, so the
 *  tooltip's footer is only the hint and the click itself goes through the chart. */
export type LSeries = { key: string; label: string; color: string };
export const SLOT = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)"];
const AX = { tick: { fill: "var(--dim)", fontSize: 10.5, fontFamily: "var(--mono)" }, axisLine: false, tickLine: false } as const;

/** live ranges: the bar of the minute still filling is left out (as on the timeline), it would read as a sudden drop */
export function settled<T extends { t: number }>(data: T[], stepMin: number, live: boolean): T[] {
  return live && data.length && (data[data.length - 1].t + stepMin * 60) * 1000 > Date.now() ? data.slice(0, -1) : data;
}

export function tipHead(ts: number, lang: string): string {
  const d = new Date(ts * 1000); const loc = lang === "tr" ? "tr-TR" : "en-US";
  return `${d.toLocaleDateString(loc, { day: "numeric", month: "short", year: "numeric" }).toLocaleUpperCase(loc)} — ${d.toLocaleTimeString(loc, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })}`;
}

/** the tooltip card shared by every chart on the operations screen (also the timeline's bars) */
export function TipCard({ ts, rows, foot, plain }: { ts: number; rows: { color: string; label: string; value: string }[]; foot?: string; plain?: boolean }) {
  const { lang } = useT();
  return <div className="lc-tip"><div className="lc-tip-h">{tipHead(ts, lang)}</div>
    {rows.map((r) => <div key={r.label} className="lc-tip-r"><i style={{ background: r.color }} /><span>{r.label}</span><b>{r.value}</b></div>)}
    {foot && <><div className="lc-tip-d" /><div className={`lc-tip-f ${plain ? "plain" : ""}`}>{foot}</div></>}</div>;
}

export function LineChartCard({ title, sub, series, data, valueFormat, span, onPick, pickLabel, height = 170, fill = 0.26, empty }: {
  title: string; sub?: string; series: LSeries[]; data: any[]; valueFormat: (v: number) => string; span: number; onPick?: (t: number) => void; pickLabel?: string; height?: number; fill?: number; empty?: string }) {
  const { t } = useT(); const gid = useId().replace(/[^a-zA-Z0-9]/g, "");
  // first / last point that carries a value, per series: the only places a dot is drawn
  const ends = useMemo(() => Object.fromEntries(series.map((s) => { const idx = data.map((d, i) => (d[s.key] == null ? -1 : i)).filter((i) => i >= 0); return [s.key, [idx[0], idx[idx.length - 1]]]; })), [data, series]);
  const has = series.some((s) => ends[s.key][0] !== undefined);
  const dot = (s: LSeries) => (p: any) => (p.index === ends[s.key][0] || p.index === ends[s.key][1]) && p.cy != null ? <circle key={`${s.key}${p.index}`} cx={p.cx} cy={p.cy} r={3} fill={s.color} /> : <g key={`${s.key}${p.index}`} />;
  return <div className="lc">
    <div className="lc-h"><span className="lc-t">{title}</span>{sub && <span className="lc-s">{sub}</span>}</div>
    {!has ? <div className="lc-empty" style={{ height }}>{empty ?? t("lc_nodata")}</div> :
      <div style={{ height, cursor: onPick ? "pointer" : undefined }}><ResponsiveContainer><ComposedChart data={data} margin={{ top: 8, right: 18, left: 0, bottom: 0 }} onClick={(e: any) => onPick && e?.activeLabel != null && onPick(Number(e.activeLabel))}>
        <defs>{series.map((s) => <linearGradient key={s.key} id={`${gid}-${s.key.replace(/[^a-zA-Z0-9]/g, "")}`} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor={s.color} stopOpacity={fill} /><stop offset="100%" stopColor={s.color} stopOpacity={0} /></linearGradient>)}</defs>
        <CartesianGrid vertical={false} stroke="var(--line)" />
        <XAxis dataKey="t" tickFormatter={(v) => fmtTime(v, span)} {...AX} minTickGap={36} />
        <YAxis tickFormatter={(v) => valueFormat(v)} {...AX} width={54} />
        <Tooltip cursor={{ stroke: "var(--line2)", strokeDasharray: "4 4" }} isAnimationActive={false} wrapperStyle={{ outline: "none", zIndex: 5 }}
          content={(p: any) => { const row = p.active ? data.find((d) => d.t === Number(p.label)) : null;   // by time, not payload: a series null at this bar still gets its row ("–"); recharts hides the card when all are null
            return row ? <TipCard ts={row.t} rows={series.map((s) => ({ color: s.color, label: s.label, value: row[s.key] == null ? "–" : valueFormat(row[s.key]) }))} foot={onPick ? pickLabel : undefined} /> : null; }} />
        {series.map((s) => <Area key={s.key} type="monotone" dataKey={s.key} name={s.label} stroke={s.color} strokeWidth={2} fill={`url(#${gid}-${s.key.replace(/[^a-zA-Z0-9]/g, "")})`} isAnimationActive={false}
          dot={dot(s)} activeDot={{ r: 4.5, fill: "var(--lc-ring-in)", stroke: "var(--lc-ring)", strokeWidth: 2 }} />)}
      </ComposedChart></ResponsiveContainer></div>}
    {series.length > 1 && <div className="lc-leg">{series.map((s) => <span key={s.key}><i style={{ background: s.color }} />{s.label}</span>)}</div>}
  </div>;
}
