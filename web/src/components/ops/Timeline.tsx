import { useState } from "react";
import { Bar, BarChart, CartesianGrid, ReferenceArea, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { useT } from "../../i18n";
import { fmtN, fmtTime } from "./util";
import { TipCard } from "./LineChartCard";

/** Log volume over the chosen range, errors and warnings stacked on the rest. Drag across the bars to zoom into that span. */
export function Timeline({ series, stepMin, spanMin, onSelect, height = 170 }: { series: any[]; stepMin: number; spanMin: number; onSelect: (s: Date, e: Date) => void; height?: number }) {
  const { t, lang } = useT(); const [a, setA] = useState<number | null>(null); const [b, setB] = useState<number | null>(null);
  const data = series.map((x) => ({ t: x.t, other: Math.max(0, x.total - x.errors - (x.warn ?? 0)), warn: x.warn ?? 0, errors: x.errors, total: x.total }));
  const end = () => { if (a !== null && b !== null && a !== b) { const lo = Math.min(a, b), hi = Math.max(a, b) + stepMin * 60; onSelect(new Date(lo * 1000), new Date(hi * 1000)); } setA(null); setB(null); };
  return <div style={{ height, userSelect: "none" }}>
    <ResponsiveContainer><BarChart data={data} barCategoryGap={1} onMouseDown={(e: any) => e?.activeLabel !== undefined && setA(e.activeLabel)} onMouseMove={(e: any) => a !== null && e?.activeLabel !== undefined && setB(e.activeLabel)} onMouseUp={end} onMouseLeave={() => { setA(null); setB(null); }}>
      <CartesianGrid vertical={false} stroke="var(--line)" />
      <XAxis dataKey="t" tickFormatter={(v) => fmtTime(v, spanMin)} tick={{ fill: "var(--dim)", fontSize: 10.5, fontFamily: "var(--mono)" }} axisLine={false} tickLine={false} minTickGap={40} />
      <YAxis tickFormatter={(v) => fmtN(v, lang)} tick={{ fill: "var(--dim)", fontSize: 10.5, fontFamily: "var(--mono)" }} axisLine={false} tickLine={false} width={54} />
      <Tooltip cursor={{ fill: "rgba(148,163,184,0.08)" }} isAnimationActive={false} wrapperStyle={{ outline: "none", zIndex: 5 }} content={(p: any) => { const d = p.payload?.[0]?.payload; return p.active && d ? <TipCard ts={Number(p.label)}
        rows={[{ color: "var(--red)", label: t("tl_errors"), value: fmtN(d.errors, lang) }, { color: "var(--amber)", label: t("tl_warn"), value: fmtN(d.warn, lang) }, { color: "var(--sev-info)", label: t("tl_other"), value: fmtN(d.other, lang) }]}
        foot={`${t("tl_total")}: ${fmtN(d.total, lang)}`} plain /> : null; }} />
      <Bar dataKey="other" stackId="a" fill="var(--sev-info)" isAnimationActive={false} />
      <Bar dataKey="warn" stackId="a" fill="var(--amber)" isAnimationActive={false} />
      <Bar dataKey="errors" stackId="a" fill="var(--red)" radius={[2, 2, 0, 0]} isAnimationActive={false} />
      {a !== null && b !== null && <ReferenceArea x1={Math.min(a, b)} x2={Math.max(a, b)} fill="var(--accent)" fillOpacity={0.15} stroke="var(--accent)" strokeOpacity={0.6} />}
    </BarChart></ResponsiveContainer>
  </div>;
}
