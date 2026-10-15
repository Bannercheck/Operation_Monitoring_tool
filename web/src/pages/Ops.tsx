import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useIsFetching, useQuery, useQueryClient } from "@tanstack/react-query";
import { MoreHorizontal } from "lucide-react";
import { api, get, post } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Modal } from "../components/ui";
import { TimeRange } from "../components/ops/TimeRange";
import { Timeline } from "../components/ops/Timeline";
import { FleetViews } from "../components/ops/FleetViews";
import { HostDetail, ScopeCharts } from "../components/ops/HostDetail";
import { RangeDetail, type DTab } from "../components/ops/RangeDetail";
import { Attention, buildAttention } from "../components/ops/Attention";
import { LogExplorer } from "../components/ops/LogExplorer";
import { DetailPanel, type Kind } from "../components/ops/Details";
import { delta, fmtN, fmtPct, hostState, rangeLabel, useOpsState } from "../components/ops/util";
import { opsToQuery } from "../components/logs/lib";
import { Grid, NowCtx, Panel, Stat, seriesCsv } from "../components/dash/kit";
import { DashCtx, useDashParams, type Dash } from "../components/dash/ctx";
import { BarTools, DashTabs, Vars } from "../components/dash/TopBar";
import { AlarmsPanel, GaugesPanel, HealthPanel } from "../components/dash/Overview";
import { InfraDash } from "../components/dash/Infra";
import { AppDash } from "../components/dash/AppDash";
import { LogsDash } from "../components/dash/LogsDash";
import { UxDash } from "../components/dash/UxDash";

/** Live operations as a dashboard wall (after OpManager, Grafana and OpenObserve): built-in dashboards as tabs - Genel bakış (the
 *  home: health in one sentence, stat panels, fleet health, resources and recent alarms, then the timeline, what needs a look, the
 *  fleet map and the logs), Altyapı, Uygulama, Loglar, Kullanıcı deneyimi - over one time range (quick, custom, dragged on a chart)
 *  and one set of variables (environment, pool, servers, service). Range, variables, dashboard and auto refresh live in the URL, so
 *  a view is a link; every panel refreshes, goes fullscreen, opens its explorer and downloads its data. */
export default function Ops() {
  const { t, lang } = useT(); const { can, user } = useAuth(); const qc = useQueryClient(); const nav = useNavigate();
  const st = useOpsState(); const rq = st.rq(); const dp = useDashParams();
  const every = dp.every(st.live); const ms: number | false = every ? every * 1000 : false;
  const sum = useQuery({ queryKey: ["range-sum", rq], queryFn: () => get(`/live/range/summary?${rq}`), refetchInterval: ms });
  const fleetQ = useQuery({ queryKey: ["range-fleet", rq.replace(/&?(pool|host)=[^&]*/g, "")], queryFn: () => get(`/live/range/fleet?${rq.replace(/&?(pool|host)=[^&]*/g, "")}`), refetchInterval: ms });
  const errG = useQuery({ queryKey: ["range-groups-err", rq], queryFn: () => get(`/live/range/groups?${rq}&severity=ERROR&limit=40`), refetchInterval: ms });
  const anom = useQuery({ queryKey: ["ops-anoms"], queryFn: () => get("/anomalies?status=open"), refetchInterval: 30000, enabled: can("page.anom") });
  const sys = useQuery({ queryKey: ["sys-status-lite"], queryFn: () => get("/system/status"), enabled: can("sys.status"), refetchInterval: 15000 });
  const hostsQ = useQuery({ queryKey: ["live-hosts"], queryFn: () => get("/live/hosts"), refetchInterval: 30000 });
  const metrics = useQuery({ queryKey: ["live-metrics", rq], queryFn: () => get(`/live/metrics?window=15${scopeQs(st)}`), refetchInterval: ms ? Math.max(ms, 10000) : false });
  const lat = useQuery({ queryKey: ["range-lat", rq], queryFn: () => get(`/live/range/latency?${rq}`), refetchInterval: ms, enabled: dp.d === "overview" });
  const [detail, setDetail] = useState<Kind | null>(null); const [dtab, setDtab] = useState<DTab | null>(null); const [focus, setFocus] = useState<any>(null); const [menu, setMenu] = useState(false);
  const root = useRef<HTMLDivElement>(null); const fetching = useIsFetching() > 0;
  const [now, setNow] = useState(Date.now()); useEffect(() => { const i = setInterval(() => setNow(Date.now()), 5000); return () => clearInterval(i); }, []);

  const raw = sum.data; const fleetAll = fleetQ.data;
  // live: the bar of the minute still filling is left out, it would read as a sudden drop
  const s = useMemo(() => { if (!raw || !st.live || !raw.series?.length) return raw; const lastT = raw.series[raw.series.length - 1].t; return (lastT + raw.step_min * 60) * 1000 > Date.now() ? { ...raw, series: raw.series.slice(0, -1) } : raw; }, [raw, st.live]);
  // the map shows the whole fleet; the numbers and the attention list follow the scope (pool / hosts)
  const fleet = useMemo(() => { if (!fleetAll || (!st.pool && !st.hosts.length)) return fleetAll;
    const pools = (fleetAll.pools ?? []).filter((p: any) => !st.pool || p.pool === st.pool).map((p: any) => ({ ...p, hosts: st.hosts.length ? p.hosts.filter((h: any) => st.hosts.includes(h.host)) : p.hosts })).filter((p: any) => p.hosts.length);
    return { ...fleetAll, pools }; }, [fleetAll, st.pool, st.hosts.join(",")]);  // eslint-disable-line
  const span = s?.minutes ?? ("last" in st.range ? st.range.last : 60);
  const hostsAll = (fleet?.pools ?? []).flatMap((p: any) => p.hosts);
  const mapHosts = (fleetAll?.pools ?? []).flatMap((p: any) => p.hosts);
  const sentinel = useRef<HTMLDivElement>(null); const [stuck, setStuck] = useState(false);
  useEffect(() => { const el = sentinel.current; if (!el) return; const io = new IntersectionObserver(([e]) => setStuck(!e.isIntersecting)); io.observe(el); return () => io.disconnect(); }, []);
  const counts = { ok: 0, warn: 0, crit: 0, silent: 0 } as Record<string, number>;
  for (const h of hostsAll) counts[hostState(h)]++;
  const zoom = (x: number, stepMin: number) => st.setAbs(new Date(x * 1000), new Date((x + stepMin * 60) * 1000));
  const [hd, setHd] = useState<string | null>(null);                       // the server whose detail is open (any click on a server)
  const base = rq.split("&").filter((x) => !/^(pool|host)=/.test(x)).join("&");
  // "to the logs" anywhere on this screen (panels, attention, fleet, server and range details) opens the log explorer with the same
  // range, scope, level, service and text as query text; without the explorer permission it scrolls to the compact card as before
  const goLogs = (f: any) => {
    if (can("page.logs")) {
      const p = new URLSearchParams(rq); const o = new URLSearchParams();
      if (p.get("start")) { o.set("start", p.get("start")!); o.set("end", p.get("end") || new Date().toISOString()); } else o.set("last", p.get("last") || "15");
      const qt = opsToQuery({ env: p.get("env") || "", pool: f.host ? "" : p.get("pool") || "", hosts: f.host ? [f.host] : (p.get("host") || "").split(",").filter(Boolean),
        sev: f.sev, service: f.service, text: f.q });
      if (qt) o.set("q", qt); if (f.tab === "groups") o.set("group", "1");
      nav(`/logs?${o}`); return;
    }
    setFocus({ ...f, n: Date.now() }); document.getElementById("ops-logs")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  const anomsInScope = (anom.data ?? []).filter((a: any) => !a.host || !st.hosts.length || st.hosts.includes(a.host));
  const items = useMemo(() => buildAttention(t, lang, fleet, errG.data?.groups ?? [], anomsInScope, metrics.data?.breaches ?? [],
    { pool: st.setPool, hosts: (hs) => { st.clearScope(); setTimeout(() => hs.forEach((h) => st.toggleHost(h)), 0); }, group: (g) => goLogs({ tab: "groups", sev: "ERROR", q: "", service: g.service === "-" ? "" : g.service }) }), [fleet, errG.data, anom.data, metrics.data, lang]);  // eslint-disable-line
  const nCrit = items.filter((i) => i.level === 3).length; const crit = nCrit > 0;
  const health = !s ? "" : !s.total ? t("ops_h_nodata") : !items.length ? t("ops_h_ok") : crit ? t("ops_h_crit", { n: items.length, c: nCrit }) : t("ops_h_warn", { n: items.length });
  const simOn = !!sys.data?.live?.simulator;
  const apiHealth = useQuery({ queryKey: ["api-health"], queryFn: () => get("/health"), staleTime: 300000 });
  const demoMode = (apiHealth.data as any)?.demo === true;          // production installs (WATCHOVER_DEMO=0) have no simulator at all
  const services: string[] = (s?.services ?? []).map((x: any) => x[0]);
  const slo = 0.999;
  // live: the rate of the last complete bar; a fixed range: its average
  const bars = s?.series ?? []; const lastFull = st.live && bars.length > 1 ? bars[bars.length - 2] : null;
  const rateNow = lastFull ? lastFull.total / (s?.step_min || 1) : s?.per_min;
  const dTotal = delta(s?.per_min, s?.prev?.per_min); const dErr = delta(s?.error_rate, s?.prev?.error_rate); const dP95 = delta(s?.p95_ms, s?.prev?.p95_ms);
  const report = async () => { const w = "last" in st.range ? st.range.last : Math.max(1, Math.round((new Date(st.range.end).getTime() - new Date(st.range.start).getTime()) / 60000));
    const when = "last" in st.range ? `window=${w}` : `start=${encodeURIComponent(st.range.start)}&end=${encodeURIComponent(st.range.end)}`;
    const html = await api<string>(`/live/report?${when}&lang=${lang}${scopeQs(st)}`, { text: true }); const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([html], { type: "text/html" })); a.download = `watchover-slo-${w}m.html`; a.click(); };
  const scroll = (id: string) => document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  const minutes = s?.minutes ?? span;
  const ctx: Dash = { st, rq, sq: scopeQs(st), span, live: st.live, ms, svc: dp.svc, minutes, setDash: dp.setD,
    openHost: setHd, openRange: (k) => setDtab(k), openMetric: (k) => setDetail(k), goLogs };
  const latRows = (lat.data?.series ?? []) as any[];
  const logsPanel = <Panel w={12} id="ops-logs" title={t("ops_logs")} sub={rangeLabel(st.range, lang)} explore={can("page.logs") ? { go: () => goLogs({}), label: t("db_x_logs") } : undefined}>
    <LogExplorer rq={rq} live={st.live} spanMin={span} services={services} focus={focus} onHost={setHd} />
  </Panel>;

  return <div className="ops dash" ref={root}>
    {/* 1. one line on health, the time range and the bar's tools (auto refresh, refresh, share, fullscreen, more); sticky */}
    <div ref={sentinel} style={{ height: 1, marginBottom: -15 }} />
    <div className={`ops-head ${stuck ? "stuck" : ""}`}>
      <div className="ops-title">
        <h2>{t("ops_title")}</h2>
        {health && <button className={`ops-health ${!s?.total ? "" : !items.length ? "ok" : crit ? "crit" : "warn"}`} onClick={() => { if (dp.d !== "overview") dp.setD("overview"); setTimeout(() => document.getElementById("ops-att")?.scrollIntoView({ behavior: "smooth", block: "center" }), 60); }}><i />{health}</button>}
      </div>
      <TimeRange range={st.range} live={st.live} setLast={st.setLast} setAbs={st.setAbs} />
      <BarTools every={every} setEvery={dp.setRefresh} onRefresh={() => qc.invalidateQueries()} fetching={fetching} root={root} />
      <div className="ops-menu">
        <button className="btn" onClick={() => setMenu(!menu)} aria-label={t("ops_more")}><MoreHorizontal size={16} /></button>
        {menu && <div className="menu card" onMouseLeave={() => setMenu(false)}>
          {can("act.report") && <button onClick={() => { setMenu(false); report(); }}>📄 {t("ops_report")}</button>}
          {can("act.connect") && <Link to="/connections">🔗 {t("ops_connect")}</Link>}
          {demoMode && can("act.sim") && user?.all !== false && <button onClick={async () => { setMenu(false); await post("/live/simulate", { on: !simOn }); qc.invalidateQueries(); }}>🧪 {t("ops_sim")} {simOn ? "ON → OFF" : "OFF → ON"}</button>}
          {can("page.data") && <button onClick={async () => { setMenu(false); await post("/live/analyze"); nav("/datasets"); }}>🗂 {t("ops_analyze")}</button>}
          <button onClick={() => { setMenu(false); setDetail("files"); }}>📁 {t("od_files")}</button>
        </div>}
      </div>
    </div>

    <DashTabs value={dp.d} onChange={dp.setD} />

    {/* 2. variables: environment, pool, servers (search: also an environment or a pool), service where it applies */}
    <Vars st={st} hosts={Object.keys(hostsQ.data?.hosts ?? {})} pools={(fleetAll?.pools ?? []).map((p: any) => p.pool)} envs={Object.keys(hostsQ.data?.environments ?? {})} envSum={envSummary(fleetAll)}
      services={services} svc={dp.svc} setSvc={dp.setSvc} showSvc={dp.d === "app" || dp.d === "logs"} />

    <NowCtx.Provider value={now}><DashCtx.Provider value={ctx}>
      {dp.d === "overview" && <Grid>
        {/* 3. stat panels: the number, its change against the previous range, a sparkline */}
        <Panel w={2} title={st.live ? t("ops_k_volume_now") : t("ops_k_volume")} qs={[sum]} explore={can("page.logs") ? { go: () => goLogs({}), label: t("db_x_logs") } : undefined}
          csv={() => seriesCsv("watchover-volume", bars, [{ key: "total", label: "lines" }, { key: "errors", label: "errors" }])}>
          <Stat value={fmtN(rateNow, lang)} unit={`/${t("tr_min")}`} delta={{ text: dTotal.text, good: null }} sub={<span>{fmtN(s?.total, lang)} {t("ops_k_lines")}</span>}
            spark={bars.map((x: any) => x.total)} color="var(--s1)" onClick={() => setDtab("volume")} />
        </Panel>
        <Panel w={2} title={t("ops_k_err")} qs={[sum]} explore={can("page.logs") ? { go: () => goLogs({ sev: "ERROR" }), label: t("db_x_logs") } : undefined}>
          <Stat value={fmtPct(s?.error_rate, 2)} tone={s?.error_rate >= 0.05 ? "crit" : s?.error_rate >= 0.01 ? "warn" : "ok"} delta={{ text: dErr.text, good: dErr.dir ? dErr.dir === "down" : null }}
            sub={<span>{fmtN(s?.errors, lang)} {t("ops_k_errlines")}</span>} spark={bars.map((x: any) => x.errors)} color="var(--st-crit)" onClick={() => setDtab("errors")} />
        </Panel>
        <Panel w={2} title={t("ops_k_avail")} qs={[sum]} explore={{ to: "/services", label: t("db_x_services") }}>
          <Stat value={fmtPct(s?.availability, 2)} tone={s?.availability == null ? "" : s.availability >= slo ? "ok" : s.availability >= 0.99 ? "warn" : "crit"}
            sub={<span>{t("ops_k_target")} %99,9</span>} spark={bars.map((x: any) => (x.total ? 1 - x.errors / x.total : null))} color="var(--st-ok)" onClick={() => setDtab("avail")} />
        </Panel>
        <Panel w={2} title={t("db_p95")} qs={[sum, lat]} explore={{ to: "/traces", label: t("db_x_traces") }}>
          <Stat value={s?.p95_ms != null ? `${Math.round(s.p95_ms)}` : "–"} unit={s?.p95_ms != null ? " ms" : undefined} delta={{ text: dP95.text, good: dP95.dir ? dP95.dir === "down" : null }}
            sub={<span>p50 {latRows.length && latRows[latRows.length - 1]?.p50 != null ? `${Math.round(latRows[latRows.length - 1].p50)} ms` : "–"}</span>} spark={latRows.map((x) => x.p90)} color="var(--lc-p90)" onClick={() => setDtab("latency")} />
        </Panel>
        <Panel w={2} title={t("ops_k_fleet")} qs={[fleetQ]} explore={{ to: "/inventory", label: t("db_x_inv") }}>
          <Stat value={<>{counts.ok}<span className="stat-u"> / {hostsAll.length}</span></>} tone={counts.crit || counts.silent ? "warn" : "ok"} sub={<FleetBar c={counts} t={t} />} onClick={() => scroll("ops-map")} />
        </Panel>
        <Panel w={2} title={t("ops_attention")} qs={[errG, metrics]} explore={{ to: "/anomalies", label: t("db_x_anom") }}>
          <Stat value={items.length} tone={crit ? "crit" : items.length ? "warn" : "ok"} sub={<span>{nCrit ? <b className="bad">{nCrit} {t("db_lv_crit").toLowerCase()}</b> : t("ops_h_ok")}</span>} onClick={() => scroll("ops-att")} />
        </Panel>

        {/* 4. fleet health, resources, recent alarms */}
        <HealthPanel hosts={hostsAll} qs={[fleetQ]} />
        <GaugesPanel />
        <AlarmsPanel id="ops-alarms" />

        {/* 5. timeline: drag to zoom */}
        <Panel w={12} title={t("ops_timeline")} sub={<>{rangeLabel(st.range, lang)} · {t("ops_step", { m: s?.step_min ?? 1 })}{s?.source === "history" ? ` · ${t("ops_src_hist")}` : ""} · {t("ops_drag")}</>}
          qs={[sum]} explore={can("page.logs") ? { go: () => goLogs({}), label: t("db_x_logs") } : undefined} csv={() => seriesCsv("watchover-timeline", bars, [{ key: "total", label: "lines" }, { key: "warn", label: "warn" }, { key: "errors", label: "errors" }])}>
          <div className="pn-tl"><Timeline series={bars} stepMin={s?.step_min ?? 1} spanMin={span} onSelect={(a, b) => st.setAbs(a, b)} /></div>
        </Panel>

        {/* 5b. only with a pool or hosts in scope: latency, volume and active sessions of just that scope */}
        {(st.pool || st.hosts.length > 0) && <div className="pn w12 pn-plain"><ScopeCharts rq={rq} title={st.hosts.length ? st.hosts.slice(0, 3).join(", ") + (st.hosts.length > 3 ? ` +${st.hosts.length - 3}` : "") : st.pool} series={bars} step={s?.step_min ?? 1} span={span} onZoom={zoom} /></div>}

        {/* 6. what needs a look, and the fleet */}
        <Panel w={5} id="ops-att" title={t("ops_attention")} sub={items.length ? `${items.length}` : undefined} qs={[errG, anom]} explore={{ to: "/anomalies", label: t("db_x_anom") }}
          csv={() => ({ name: "watchover-attention", cols: ["level", "title", "detail"], rows: items.map((i: any) => [i.level, i.title, i.sub ?? i.detail ?? ""]) })}>
          <Attention items={items} />
        </Panel>
        <Panel w={7} id="ops-map" title={t("ops_map")} sub={`${mapHosts.length} ${t("hm_hosts")} · ${(fleetAll?.pools ?? []).length} pool`} qs={[fleetQ]} explore={{ to: "/inventory", label: t("db_x_inv") }}
          empty={!mapHosts.length && !fleetQ.isLoading} emptyText={t("fl_empty")}
          csv={() => ({ name: "watchover-fleet", cols: ["host", "pool", "env", "lines", "errors", "error_rate", "silent"], rows: mapHosts.map((h: any) => [h.host, h.pool, h.env, h.events, h.errors, h.error_rate, h.silent]) })}>
          <FleetViews rq={rq} pools={fleetAll?.pools ?? []} sel={st.hosts} pool={st.pool} onHost={setHd} onPool={st.setPool} onLogs={goLogs} span={span} onZoom={zoom} />
        </Panel>

        {/* 7. logs */}
        {logsPanel}
      </Grid>}
      {dp.d === "infra" && <InfraDash />}
      {dp.d === "app" && <AppDash />}
      {dp.d === "logs" && <LogsDash logs={logsPanel} />}
      {dp.d === "ux" && <UxDash />}
    </DashCtx.Provider></NowCtx.Provider>

    {dtab && <RangeDetail rq={rq} span={span} tab0={dtab} onClose={() => setDtab(null)} onLogs={(f) => { setDtab(null); goLogs(f); }} onHost={(h) => { setDtab(null); setHd(h); }} />}
    {hd && <HostDetail host={hd} base={base} row={mapHosts.find((h: any) => h.host === hd)} span={span} focused={st.hosts.includes(hd)} onClose={() => setHd(null)}
      onFocus={st.toggleHost} onLogs={goLogs} onZoom={zoom} />}
    {detail && <Modal wide title={t("od_" + detail)} onClose={() => setDetail(null)}>
      <DetailPanel kind={detail} sq={scopeQs(st)} env={st.env} host={st.hosts.join(", ")} sl={null} m={metrics.data} s={null} files={[]} onClose={() => setDetail(null)} />
    </Modal>}
  </div>;
}

function scopeQs(st: ReturnType<typeof useOpsState>): string {
  const p = new URLSearchParams(); if (st.env) p.set("env", st.env); if (st.pool) p.set("pool", st.pool); if (st.hosts.length) p.set("host", st.hosts.join(","));
  return p.toString() ? `&${p}` : "";
}

function FleetBar({ c, t }: { c: Record<string, number>; t: any }) {
  const n = c.ok + c.warn + c.crit + c.silent || 1;
  return <span className="fb"><span className="fb-bar">{(["ok", "warn", "crit", "silent"] as const).map((k) => c[k] ? <i key={k} className={k} style={{ width: `${(c[k] / n) * 100}%` }} /> : null)}</span>
    <span className="fb-l">{c.crit ? <b className="bad">{c.crit} {t("hm_crit").toLowerCase()}</b> : null}{c.warn ? <span> · {c.warn} {t("hm_warn").toLowerCase()}</span> : null}{c.silent ? <span> · {c.silent} {t("hm_silent").toLowerCase()}</span> : null}{!c.crit && !c.warn && !c.silent ? t("ops_k_allok") : null}</span></span>;
}

/** per environment: hosts, lines, errors in the range (the environment chips), from the fleet rows */
function envSummary(fleet: any): { env: string; hosts: number; events: number; errors: number }[] {
  const by: Record<string, { env: string; hosts: number; events: number; errors: number }> = {};
  for (const p of fleet?.pools ?? []) for (const h of p.hosts) { const e = (by[h.env] ||= { env: h.env, hosts: 0, events: 0, errors: 0 }); e.hosts++; e.events += h.events; e.errors += h.errors; }
  const order = ["prod", "staging", "test", "qa", "dev", "unknown"]; const rank = (e: string) => (order.indexOf(e) + 99) % 99;
  return Object.values(by).sort((a, b) => rank(a.env) - rank(b.env) || a.env.localeCompare(b.env));
}
