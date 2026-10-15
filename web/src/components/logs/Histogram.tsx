import { useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, Cell, ReferenceArea, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useT } from "../../i18n";
import { fmtN } from "../ops/util";
import { SLOT, TipCard } from "../ops/LineChartCard";
import { LV_COLOR } from "./lib";

/** Matching lines over the range, stacked by level (or by service / host / file / pool: the reference's "Create chart"), local
 *  time ticks. Drag across bars to zoom into that span; a click on one bar zooms to it. Buckets the bounded scan did not reach
 *  are drawn faint ("not scanned"). */
const AX = { tick: { fill: "var(--dim)", fontSize: 10.5, fontFamily: "var(--mono)" }, axisLine: false, tickLine: false } as const;

function tick(v: number, step: number, span: number): string {
  const d = new Date(v * 1000); const p = (x: number) => String(x).padStart(2, "0");
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
  if (step < 60) return `${hm}:${p(d.getSeconds())}`;
  return span > 86400 ? `${p(d.getDate())}.${p(d.getMonth() + 1)} ${hm}` : hm;
}
export const keyColor = (k: string, i: number, by: string) => (by === "level" ? LV_COLOR[k] ?? (k === "ERROR+" ? "var(--red)" : "var(--sev-info)") : k === "_other" ? "var(--dim)" : SLOT[i % SLOT.length]);

export function Histogram({ data, onZoom, height, note }: { data: any; onZoom: (s: Date, e: Date) => void; height: number; note?: string }) {
  const { t, lang } = useT(); const [a, setA] = useState<number | null>(null); const [b, setB] = useState<number | null>(null);
  const by: string = data?.by ?? "level"; const keys: string[] = data?.keys ?? []; const step: number = data?.step_s ?? 60;
  const span = data?.range ? (new Date(data.range.end).getTime() - new Date(data.range.start).getTime()) / 1000 : 900;
  // keys may hold dots ("Microsoft.Dynamics.AX"), which recharts reads as a path: chart on k0, k1, …
  const rows = useMemo(() => (data?.buckets ?? []).map((x: any) => ({ t: x.t, total: x.total, scanned: x.scanned !== false, ...Object.fromEntries(keys.map((k, i) => [`k${i}`, x.v?.[k] ?? 0])) })), [data]);  // eslint-disable-line
  const label = (k: string) => (by === "level" && ["CRITICAL", "ERROR", "WARN", "INFO", "DEBUG"].includes(k) ? t(`lg_lv_${k}`) : k === "_other" ? t("lg_other") : k === "OTHER" ? t("lg_other") : k);
  const end = () => {
    if (a !== null && (b === null || b === a)) onZoom(new Date(a * 1000), new Date((a + step) * 1000));
    else if (a !== null && b !== null) { const lo = Math.min(a, b), hi = Math.max(a, b) + step; onZoom(new Date(lo * 1000), new Date(hi * 1000)); }
    setA(null); setB(null);
  };
  if (!rows.length || note) return <div className="lg-hist lc-empty" style={{ height }}>{note || (data ? t("lg_hist_empty") : "…")}</div>;
  const order = keys.map((k, i) => ({ k, i })).reverse();   // the first key (CRITICAL, or the busiest value) on top
  // the bounded scan stops at a frontier: the older bars hold nothing because nobody looked, not because nothing matched; hatch them
  const un = rows.filter((r: any) => !r.scanned); const unLo = un.length ? un[0].t : null; const unHi = un.length ? un[un.length - 1].t : null;
  return <div className="lg-hist" style={{ height, userSelect: "none" }}>
    <ResponsiveContainer><BarChart data={rows} barCategoryGap={1} margin={{ top: 4, right: 6, left: 0, bottom: 0 }}
      onMouseDown={(e: any) => e?.activeLabel !== undefined && setA(Number(e.activeLabel))} onMouseMove={(e: any) => a !== null && e?.activeLabel !== undefined && setB(Number(e.activeLabel))}
      onMouseUp={end} onMouseLeave={() => { setA(null); setB(null); }}>
      <defs><pattern id="lg-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="6" style={{ stroke: "var(--dim)", strokeWidth: 1.5, strokeOpacity: 0.35 }} /></pattern></defs>
      <CartesianGrid vertical={false} stroke="var(--line)" />
      <XAxis dataKey="t" tickFormatter={(v) => tick(v, step, span)} {...AX} minTickGap={44} />
      {unLo !== null && <ReferenceArea x1={unLo} x2={unHi} fill="url(#lg-hatch)" fillOpacity={1} label={{ value: t("lg_not_scanned"), position: "insideTop", fill: "var(--dim)", fontSize: 11 }} />}
      <YAxis tickFormatter={(v) => fmtN(v, lang)} {...AX} width={46} allowDecimals={false} />
      <Tooltip cursor={{ fill: "rgba(148,163,184,0.08)" }} isAnimationActive={false} wrapperStyle={{ outline: "none", zIndex: 5 }} content={(p: any) => {
        const d = p.payload?.[0]?.payload; if (!p.active || !d) return null;
        const r = keys.map((k, i) => ({ color: keyColor(k, i, by), label: label(k), value: fmtN(d[`k${i}`], lang) })).filter((x, i) => d[`k${i}`] > 0 || keys.length <= 5);
        return <TipCard ts={d.t} rows={r} plain foot={`${t("lg_total")}: ${fmtN(d.total, lang)} · ${tick(d.t, step < 60 ? 1 : 30, 0)} → ${tick(d.t + step, step < 60 ? 1 : 30, 0)}${d.scanned ? "" : ` · ${t("lg_not_scanned")}`}`} />; }} />
      {order.map(({ k, i }, j) => <Bar key={k} dataKey={`k${i}`} stackId="a" fill={keyColor(k, i, by)} isAnimationActive={false} radius={j === order.length - 1 ? [2, 2, 0, 0] : undefined}>
        {rows.map((r: any) => <Cell key={r.t} fillOpacity={r.scanned ? (by === "level" && k === "INFO" ? 0.62 : 0.92) : 0.25} />)}</Bar>)}
      {a !== null && b !== null && <ReferenceArea x1={Math.min(a, b)} x2={Math.max(a, b)} fill="var(--accent)" fillOpacity={0.15} stroke="var(--accent)" strokeOpacity={0.6} />}
    </BarChart></ResponsiveContainer>
  </div>;
}

export function Legend({ data }: { data: any }) {
  const { t } = useT(); const by: string = data?.by ?? "level"; const keys: string[] = data?.keys ?? [];
  if (!keys.length) return null;
  return <div className="lg-leg">{keys.map((k, i) => <span key={k}><i style={{ background: keyColor(k, i, by) }} />{by === "level" && ["CRITICAL", "ERROR", "WARN", "INFO", "DEBUG"].includes(k) ? t(`lg_lv_${k}`) : k === "_other" || k === "OTHER" ? t("lg_other") : k}</span>)}</div>;
}
