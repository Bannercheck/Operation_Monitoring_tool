import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Crosshair, Gauge, Server } from "lucide-react";
import { get } from "../../api";
import { useT } from "../../i18n";
import { Card, Empty, Modal } from "../ui";
import { LineChartCard, settled } from "./LineChartCard";
import { SvcTable } from "./RangeDetail";
import { SessionTable } from "./Sessions";
import { fmtN, fmtPct, fmtTime, hostState } from "./util";

/** what a pool or a few hosts look like on their own: p50 / p90 / p99 (a click opens the traces of those minutes), lines and ERROR+
 *  per bar (from the summary already loaded) and active client sessions */
export function ScopeCharts({ rq, title, series, step, span, onZoom }: { rq: string; title: string; series: any[]; step: number; span: number; onZoom: (t: number, stepMin: number) => void }) {
  const { t, lang } = useT(); const nav = useNavigate(); const live = rq.includes("last=");
  const lat = useQuery({ queryKey: ["range-latency", rq], queryFn: () => get(`/live/range/latency?${rq}`), refetchInterval: live ? 15000 : false });
  const ses = useQuery({ queryKey: ["range-sessions-n", rq], queryFn: () => get(`/live/range/sessions?${rq}&limit=1`), refetchInterval: live ? 15000 : false });
  const ms = (v: number) => `${v >= 10 || v === 0 ? Math.round(v) : v.toFixed(1)} ms`;
  const traces = (x: number) => { const back = (Date.now() - x * 1000) / 60000; const env = new URLSearchParams(rq).get("env"); nav(`/traces?minutes=${[15, 60, 360, 1440, 10080].find((m) => m >= back) ?? 10080}&order=duration${env ? `&env=${encodeURIComponent(env)}` : ""}`); };
  return <Card title={<span>{title} <span className="dim small">· {t("sd_detail")}</span></span>}>
    <div className="lc-row">
      <LineChartCard title={t("lc_latency")} span={span} data={settled(lat.data?.series ?? [], lat.data?.step_min ?? 1, live)} valueFormat={ms} onPick={traces} pickLabel={t("lc_traces")}
        series={[{ key: "p99", label: "p99", color: "var(--lc-p99)" }, { key: "p90", label: "p90", color: "var(--lc-p90)" }, { key: "p50", label: "p50", color: "var(--lc-p50)" }]} />
      <LineChartCard title={t("lc_volume")} span={span} data={series} valueFormat={(v) => fmtN(v, lang)} onPick={(x) => onZoom(x, step)} pickLabel={t("lc_zoom")}
        series={[{ key: "total", label: t("lc_lines"), color: "var(--s1)" }, { key: "errors", label: "ERROR+", color: "var(--red)" }]} />
      <LineChartCard title={t("lc_active")} sub={ses.data ? `${fmtN(ses.data.total, lang)} ${t("ses_total")}` : ""} span={span} data={settled(ses.data?.active_series ?? [], ses.data?.step_min ?? 1, live)} valueFormat={(v) => fmtN(v, lang)}
        onPick={(x) => onZoom(x, ses.data?.step_min ?? 1)} pickLabel={t("lc_zoom")} empty={!ses.data ? t("loading") : ses.data.source === "history" ? t("ses_hist") : undefined}
        series={[{ key: "n", label: t("lc_active"), color: "var(--s3)" }]} />
    </div>
  </Card>;
}


const UNIT: Record<string, string> = { cpu: "%", memory: "%", disk: "%", gpu: "%", error_rate: "%", errors_per_min: "/dk", p95_ms: "ms", silent_min: "dk" };

/** Everything about one server for the chosen range, from wherever it was clicked (map, tables, logs, sessions): its numbers, the same
 *  charts as a scoped screen, its threshold levels and current values, its most frequent errors, services, client sessions and newest
 *  lines. "Focus" narrows the whole screen to it; "Set level" opens the thresholds page for it. */
export function HostDetail({ host, base, row, span, onClose, onFocus, focused, onLogs, onZoom }: { host: string; base: string; row?: any; span: number; onClose: () => void;
  onFocus: (h: string) => void; focused: boolean; onLogs: (f: any) => void; onZoom: (t: number, stepMin: number) => void }) {
  const { t, lang } = useT(); const nav = useNavigate();
  const rq = `${base}&host=${encodeURIComponent(host)}`; const live = base.includes("last=");
  const sum = useQuery({ queryKey: ["hd-sum", rq], queryFn: () => get(`/live/range/summary?${rq}`), refetchInterval: live ? 10000 : false });
  const thr = useQuery({ queryKey: ["thr-ov"], queryFn: () => get("/thresholds/overview"), refetchInterval: 30000 });
  const grp = useQuery({ queryKey: ["hd-grp", rq], queryFn: () => get(`/live/range/groups?${rq}&severity=ERROR&limit=6`), refetchInterval: live ? 15000 : false });
  const det = useQuery({ queryKey: ["range-detail", rq], queryFn: () => get(`/live/range/detail?${rq}`), refetchInterval: live ? 15000 : false });
  const ses = useQuery({ queryKey: ["hd-ses", rq], queryFn: () => get(`/live/range/sessions?${rq}&limit=8`), refetchInterval: live ? 15000 : false });
  const logs = useQuery({ queryKey: ["hd-logs", rq], queryFn: () => get(`/live/range/logs?${rq}&limit=15`), refetchInterval: live ? 10000 : false });
  const s = sum.data; const th = (thr.data?.hosts ?? []).find((h: any) => h.host === host);
  const state = row ? hostState(row) : "ok";
  const series = settled(s?.series ?? [], s?.step_min ?? 1, live);
  const go = (f: any) => { onClose(); if (!focused) onFocus(host); onLogs(f); };
  return <Modal wide title={<span className="row" style={{ gap: 8 }}><Server size={16} /> {host} <span className={`hm-c ${state}`} style={{ width: 10, height: 10 }} />
    <span className="dim small">{row?.pool ?? th?.pool ?? ""}{(row?.env ?? th?.env) ? ` · ${row?.env ?? th?.env}` : ""}</span></span>} onClose={onClose}>
    <div className="stack">
      <div className="row between wrap" style={{ gap: 8 }}>
        <div className="hd-kpis">
          <div><span>{t("ops_k_volume")}</span><b>{fmtN(s?.per_min, lang)}<small>/{t("tr_min")}</small></b></div>
          <div><span>{t("ops_k_err")}</span><b className={(s?.error_rate ?? 0) >= 0.05 ? "bad" : ""}>{fmtPct(s?.error_rate, 2)}</b></div>
          <div><span>p95</span><b>{s?.p95_ms != null ? `${Math.round(s.p95_ms)} ms` : "–"}</b></div>
          <div><span>{t("lc_active")}</span><b>{fmtN(ses.data?.total, lang)}</b></div>
          <div><span>{t("fl_last")}</span><b>{row?.last_seen_s == null ? "–" : row.last_seen_s < 60 ? `${row.last_seen_s} sn` : `${Math.round(row.last_seen_s / 60)} dk`}</b></div>
        </div>
        <div className="row" style={{ gap: 6 }}>
          <button className="btn sm primary" onClick={() => { onFocus(host); onClose(); }}><Crosshair size={13} /> {focused ? t("hd_unfocus") : t("hd_focus")}</button>
          <button className="btn sm" onClick={() => nav(`/thresholds?tab=hosts&host=${encodeURIComponent(host)}`)}><Gauge size={13} /> {t("thr_set")}</button>
        </div>
      </div>
      <ScopeCharts rq={rq} title={host} series={series} step={s?.step_min ?? 1} span={span} onZoom={(x, st) => { onClose(); onZoom(x, st); }} />
      <div className="rd-two">
        <Card title={t("hd_levels")}>{!th ? <Empty>{t("thr_no_hosts")}</Empty> :
          <div className="hd-thr">{Object.entries(th.metrics).filter(([, x]: any) => x.value != null || (x.source && x.source.scope !== "global")).map(([m, x]: any) => <div key={m} className={`hd-thr-r ${x.level === "warn" || x.level === "crit" ? x.level : ""}`}>
            <span>{t(`thr_m_${m}`)}</span><b>{x.value == null ? "–" : `${Number(x.value).toLocaleString("tr-TR", { maximumFractionDigits: 1 })}${UNIT[m] === "%" ? "%" : UNIT[m] === "ms" ? " ms" : ` ${UNIT[m]}`}`}</b>
            <span className="dim small">{x.warn != null || x.crit != null ? `${x.warn ?? "–"} / ${x.crit ?? "–"}${x.source ? ` · ${t(`thr_scope_${x.source.scope}`)}` : ""}` : t("thr_no_level")}</span></div>)}</div>}</Card>
        <Card title={t("hd_errors")}>{!(grp.data?.groups ?? []).length ? <Empty>{t("rd_no_errors")}</Empty> :
          <div className="stack" style={{ gap: 4 }}>{grp.data.groups.map((g: any) => <button key={g.id} className="hd-err" onClick={() => go({ tab: "groups", sev: "ERROR", service: g.service === "-" ? "" : g.service })}>
            <span className={`sev-dot ${g.severity}`} /><span className="mono small">{g.template}</span><b className="small">{fmtN(g.count, lang)}</b></button>)}</div>}</Card>
      </div>
      <Card title={t("fv_services")}><SvcTable rows={det.data?.services ?? []} onLogs={go} /></Card>
      <Card title={t("fv_sessions")}><SessionTable rows={ses.data?.sessions ?? []} max={260} compact loading={ses.isLoading} /></Card>
      <Card title={t("hd_lines")} right={<button className="btn sm" onClick={() => go({ tab: "raw", sev: "INFO" })}>{t("hd_all_lines")}</button>}>
        {!(logs.data?.lines ?? []).length ? <Empty>{t("lx_empty")}</Empty> : <div className="lx-raw" style={{ maxHeight: 300 }}>{logs.data.lines.map((l: any, i: number) =>
          <div key={i} className="lx-l" style={{ gridTemplateColumns: "70px 10px 130px minmax(0,1fr)" }}><span className="mono dim">{fmtTime(l.ts, span)}</span><span className={`sev-dot ${l.severity}`} /><span className="lx-svc small">{l.service}</span><span className="mono lx-msg">{l.message}</span></div>)}</div>}
      </Card>
    </div>
  </Modal>;
}
