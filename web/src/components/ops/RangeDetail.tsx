import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { get } from "../../api";
import { useT } from "../../i18n";
import { Empty, Modal } from "../ui";
import { fmtN, fmtPct, fmtTime } from "./util";

/** What is behind the four headline numbers, for the chosen range and scope: volume by service, errors by service / host with the
 *  newest error lines, availability per bar with the error budget and the message groups that lower it, latency per service with the
 *  slowest lines. Opened from a headline tile; one tab per tile. */
export type DTab = "volume" | "errors" | "avail" | "latency";
const TT = { contentStyle: { background: "var(--bg2)", border: "1px solid var(--line2)", borderRadius: 10, fontSize: 12 } };
const AX = { tick: { fill: "var(--dim)", fontSize: 11 }, axisLine: false, tickLine: false } as const;

export function RangeDetail({ rq, span, tab0, onClose, onLogs, onHost }: { rq: string; span: number; tab0: DTab; onClose: () => void; onLogs: (f: any) => void; onHost?: (h: string) => void }) {
  const { t, lang } = useT(); const [tab, setTab] = useState<DTab>(tab0);
  const q = useQuery({ queryKey: ["range-detail", rq], queryFn: () => get(`/live/range/detail?${rq}`), refetchInterval: rq.includes("last=") ? 15000 : false });
  const d = q.data;
  const bars = (d?.per_minute ?? []).map((x: any) => ({ ...x, t: Date.parse(x.minute) / 1000 }));
  const svc: any[] = d?.services ?? [];
  return <Modal wide title={t("rd_title")} onClose={onClose}>
    <div className="seg" style={{ marginBottom: 14 }}>{(["volume", "errors", "avail", "latency"] as DTab[]).map((k) => <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{t(`rd_${k}`)}</button>)}</div>
    {d?.source === "history" && <div className="dim small" style={{ marginBottom: 8 }}>{t("rd_hist_note")}</div>}
    {d?.sample > 1 && <div className="dim small" style={{ marginBottom: 8 }}>{t("lx_sample", { k: d.sample })}</div>}
    {!d ? <Empty>{t("loading")}</Empty> : <>
      {tab === "volume" && <div className="stack">
        <div style={{ height: 200 }}><ResponsiveContainer><BarChart data={bars}><CartesianGrid vertical={false} stroke="var(--line)" /><XAxis dataKey="t" tickFormatter={(v) => fmtTime(v, span)} {...AX} minTickGap={40} /><YAxis tickFormatter={(v) => fmtN(v, lang)} {...AX} width={44} />
          <Tooltip {...TT} labelFormatter={(v) => fmtTime(v as number, span + 2000)} formatter={(v: any) => [fmtN(v, lang), t("ops_k_lines")]} /><Bar dataKey="total" fill="var(--accent2)" isAnimationActive={false} /></BarChart></ResponsiveContainer></div>
        <SvcTable rows={svc} onLogs={onLogs} />
      </div>}
      {tab === "errors" && <div className="stack">
        <div className="rd-two">
          <div><h4>{t("od_err_by_service")}</h4>{!d.by_service.length ? <Empty>{t("rd_no_errors")}</Empty> :
            <div style={{ height: Math.max(120, Math.min(10, d.by_service.length) * 28) }}><ResponsiveContainer><BarChart layout="vertical" data={d.by_service.slice(0, 10).map((x: any) => ({ s: x[0], n: x[1] }))}>
              <XAxis type="number" {...AX} tickFormatter={(v) => fmtN(v, lang)} /><YAxis type="category" dataKey="s" width={150} {...AX} tickFormatter={(v) => (String(v).length > 22 ? `…${String(v).slice(-21)}` : v)} /><Tooltip {...TT} /><Bar dataKey="n" fill="var(--red)" isAnimationActive={false} /></BarChart></ResponsiveContainer></div>}</div>
          <div><h4>{t("od_err_by_host")}</h4><div className="rd-list">{d.by_host.slice(0, 12).map((x: any) => <div key={x[0]} className={`row between small ${onHost ? "rd-host" : ""}`} onClick={() => onHost?.(x[0])}><span>{x[0]}</span><b>{fmtN(x[1], lang)}</b></div>)}</div></div>
        </div>
        <div className="row between"><h4>{t("rd_recent_err")}</h4><button className="btn sm" onClick={() => onLogs({ tab: "raw", sev: "ERROR" })}>{t("rd_to_logs")}</button></div>
        <div className="lx-raw" style={{ maxHeight: 300 }}>{d.recent.map((l: any, i: number) => <div key={i} className="lx-l"><span className="mono dim">{fmtTime(l.ts, span)}</span><span className={`sev-dot ${l.severity}`} />
          <span className="lx-host small" onClick={() => onHost?.(l.host)}>{l.host}</span><span className="lx-svc small">{l.service}</span><span className="mono lx-msg">{l.message}</span></div>)}</div>
      </div>}
      {tab === "avail" && <div className="stack">
        <div className="rd-kpis">
          <div><span className="dim small">{t("od_availability")}</span><b>{fmtPct(d.availability, 3)}</b><span className="dim small">{t("od_target")} {fmtPct(d.slo?.availability, 1)} · SLA {fmtPct(d.sla?.availability, 1)}</span></div>
          <div><span className="dim small">{t("od_budget")}</span><b style={{ color: (d.error_budget ?? 1) > 0.25 ? undefined : "var(--red)" }}>{d.error_budget != null ? fmtPct(d.error_budget, 0) : "–"}</b><span className="dim small">{fmtN(d.errors, lang)} / {fmtN(d.allowed_errors, lang)} {t("od_allowed")}</span></div>
          <div><span className="dim small">{t("od_breach_min")}</span><b>{d.breach_minutes}</b><span className="dim small">{t("rd_bars", { m: d.step_min })}</span></div>
          <div><span className="dim small">{t("ops_k_lines")}</span><b>{fmtN(d.total, lang)}</b><span className="dim small">{fmtN(d.errors, lang)} ERROR+</span></div>
        </div>
        <div className="rd-two">
          <div><h4>{t("od_minute_avail")}</h4><div style={{ height: 200 }}><ResponsiveContainer><LineChart data={bars}><CartesianGrid vertical={false} stroke="var(--line)" /><XAxis dataKey="t" tickFormatter={(v) => fmtTime(v, span)} {...AX} minTickGap={40} />
            <YAxis domain={[0, 1]} tickFormatter={(v) => `%${Math.round(v * 100)}`} {...AX} width={44} /><Tooltip {...TT} labelFormatter={(v) => fmtTime(v as number, span + 2000)} formatter={(v: any) => [fmtPct(v, 2), t("od_availability")]} />
            <ReferenceLine y={d.slo?.availability} stroke="var(--red)" strokeDasharray="4 4" /><Line dataKey="availability" stroke="var(--accent2)" dot={false} strokeWidth={2} isAnimationActive={false} /></LineChart></ResponsiveContainer></div></div>
          <div><h4>{t("od_burn")}</h4><div style={{ height: 200 }}><ResponsiveContainer><LineChart data={bars}><CartesianGrid vertical={false} stroke="var(--line)" /><XAxis dataKey="t" tickFormatter={(v) => fmtTime(v, span)} {...AX} minTickGap={40} />
            <YAxis domain={[0, 1]} tickFormatter={(v) => `%${Math.round(v * 100)}`} {...AX} width={44} /><Tooltip {...TT} labelFormatter={(v) => fmtTime(v as number, span + 2000)} formatter={(v: any) => [fmtPct(v, 0), t("od_budget")]} />
            <Line dataKey="budget_left" stroke="var(--accent)" dot={false} strokeWidth={2} isAnimationActive={false} /></LineChart></ResponsiveContainer></div></div>
        </div>
        <h4>{t("od_what_lowers")}</h4>
        {!d.templates.length ? <Empty>{d.source === "history" ? t("lx_hist_only_long") : t("rd_no_errors")}</Empty> :
          <div className="lx-list">{d.templates.map((x: any, i: number) => <div key={i} className="lx-g"><div className="lx-gh" style={{ gridTemplateColumns: "14px minmax(0,1fr) 150px 80px 120px" }} onClick={() => onLogs({ tab: "groups", sev: "ERROR", service: x.services[0] === "-" ? "" : x.services[0] })}>
            <span className={`sev-dot ${x.severity}`} /><span className="mono lx-tpl">{x.template}</span><span className="lx-svc small">{x.services.join(", ")}</span><span className="lx-n"><b>{fmtN(x.count, lang)}</b></span><span className="lx-h small">{x.hosts.length} {t("lx_hosts")}</span></div></div>)}</div>}
      </div>}
      {tab === "latency" && <div className="stack">
        <div className="rd-kpis"><div><span className="dim small">p95</span><b>{d.p95_ms != null ? `${Math.round(d.p95_ms)} ms` : "–"}</b><span className="dim small">{t("rd_p95_src")}</span></div></div>
        {!d.latency.length ? <Empty>{t("rd_no_latency")}</Empty> : <div className="rd-two">
          <div><h4>{t("od_p95")}</h4><div style={{ height: Math.max(140, Math.min(10, d.latency.length) * 28) }}><ResponsiveContainer><BarChart layout="vertical" data={d.latency.slice(0, 10)}>
            <XAxis type="number" {...AX} /><YAxis type="category" dataKey="service" width={150} {...AX} tickFormatter={(v) => (String(v).length > 22 ? `…${String(v).slice(-21)}` : v)} /><Tooltip {...TT} formatter={(v: any) => [`${Math.round(v)} ms`, "p95"]} /><Bar dataKey="p95" fill="var(--amber)" isAnimationActive={false} /></BarChart></ResponsiveContainer></div></div>
          <div><h4>{t("od_slowest")}</h4><div className="rd-list">{d.slowest.map((x: any, i: number) => <div key={i} className="small" style={{ padding: "4px 0", borderBottom: "1px solid var(--line)" }}>
            <b>{Math.round(x.ms)} ms</b> <span className="dim">· {x.service} · {x.host} · {fmtTime(x.ts, span)}</span><div className="mono dim" style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{x.message}</div></div>)}</div></div>
        </div>}
      </div>}
    </>}
  </Modal>;
}

/** Services in the range: lines, lines per minute, errors, error rate, hosts, p95; click a row for its logs. */
export function SvcTable({ rows, onLogs }: { rows: any[]; onLogs: (f: any) => void }) {
  const { t, lang } = useT(); const [sort, setSort] = useState<"errors" | "lines" | "rate" | "p95">("errors");
  const r = rows.slice().sort((a, b) => sort === "lines" ? b.lines - a.lines : sort === "rate" ? b.error_rate - a.error_rate : sort === "p95" ? (b.p95_ms ?? -1) - (a.p95_ms ?? -1) : b.errors - a.errors || b.lines - a.lines);
  const th = (k: typeof sort, label: string) => <th className="right clickable" onClick={() => setSort(k)}>{label}{sort === k ? " ▾" : ""}</th>;
  if (!rows.length) return <Empty>{t("lx_empty")}</Empty>;
  return <div className="table-wrap" style={{ maxHeight: 420 }}><table className="table"><thead><tr><th>{t("service")}</th>{th("lines", t("ops_k_lines"))}<th className="right">{t("fl_per_min")}</th>{th("errors", "ERROR+")}{th("rate", t("fl_err_rate"))}<th className="right">{t("fl_hosts")}</th>{th("p95", "p95")}</tr></thead>
    <tbody>{r.map((x) => <tr key={x.service} className="clickable" onClick={() => onLogs({ tab: "groups", sev: "INFO", service: x.service })}>
      <td><b>{x.service}</b></td><td className="num">{fmtN(x.lines, lang)}</td><td className="num">{fmtN(x.per_min, lang)}</td><td className="num">{fmtN(x.errors, lang)}</td>
      <td className="num" style={{ color: x.error_rate >= 0.05 ? "var(--red)" : x.error_rate >= 0.005 ? "var(--amber)" : undefined }}>{fmtPct(x.error_rate, 2)}</td><td className="num">{x.hosts ?? "–"}</td>
      <td className="num">{x.p95_ms != null ? `${Math.round(x.p95_ms)} ms` : "–"}</td></tr>)}</tbody></table></div>;
}
