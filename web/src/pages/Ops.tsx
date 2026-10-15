import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { MoreHorizontal, Search, X } from "lucide-react";
import { api, get, post } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Card, Modal, Spark } from "../components/ui";
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

/** Live operations, built to be read in five seconds: one sentence on health, four numbers, the timeline, what needs a look,
 *  the fleet at a glance and the logs. One time range (quick, custom, or dragged on the timeline) and one scope (environment,
 *  pool, hosts) drive every card and live in the URL, so a view can be shared as a link. */
export default function Ops() {
  const { t, lang } = useT(); const { can, user } = useAuth(); const qc = useQueryClient(); const nav = useNavigate();
  const st = useOpsState(); const rq = st.rq();
  const refresh = st.live ? 10000 : false;
  const sum = useQuery({ queryKey: ["range-sum", rq], queryFn: () => get(`/live/range/summary?${rq}`), refetchInterval: st.live ? 5000 : false });
  const fleetQ = useQuery({ queryKey: ["range-fleet", rq.replace(/&?(pool|host)=[^&]*/g, "")], queryFn: () => get(`/live/range/fleet?${rq.replace(/&?(pool|host)=[^&]*/g, "")}`), refetchInterval: refresh });
  const errG = useQuery({ queryKey: ["range-groups-err", rq], queryFn: () => get(`/live/range/groups?${rq}&severity=ERROR&limit=40`), refetchInterval: refresh });
  const anom = useQuery({ queryKey: ["ops-anoms"], queryFn: () => get("/anomalies?status=open"), refetchInterval: 30000, enabled: can("page.anom") });
  const sys = useQuery({ queryKey: ["sys-status-lite"], queryFn: () => get("/system/status"), enabled: can("sys.status"), refetchInterval: 15000 });
  const hostsQ = useQuery({ queryKey: ["live-hosts"], queryFn: () => get("/live/hosts"), refetchInterval: 30000 });
  const metrics = useQuery({ queryKey: ["live-metrics", rq], queryFn: () => get(`/live/metrics?window=15${scopeQs(st)}`), refetchInterval: refresh });
  const [detail, setDetail] = useState<Kind | null>(null); const [dtab, setDtab] = useState<DTab | null>(null); const [focus, setFocus] = useState<any>(null); const [menu, setMenu] = useState(false);

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
  // "to the logs" anywhere on this screen (attention, fleet, server and range details) opens the log explorer with the same range,
  // scope, level, service and text as query text; without the explorer permission it scrolls to the compact card as before
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
  const services: string[] = (s?.services ?? []).map((x: any) => x[0]);
  const slo = 0.999;
  // live: the rate of the last complete bar; a fixed range: its average
  const bars = s?.series ?? []; const lastFull = st.live && bars.length > 1 ? bars[bars.length - 2] : null;
  const rateNow = lastFull ? lastFull.total / (s?.step_min || 1) : s?.per_min;
  const dTotal = delta(s?.per_min, s?.prev?.per_min); const dErr = delta(s?.error_rate, s?.prev?.error_rate);
  const report = async () => { const w = "last" in st.range ? st.range.last : Math.max(1, Math.round((new Date(st.range.end).getTime() - new Date(st.range.start).getTime()) / 60000));
    const when = "last" in st.range ? `window=${w}` : `start=${encodeURIComponent(st.range.start)}&end=${encodeURIComponent(st.range.end)}`;
    const html = await api<string>(`/live/report?${when}&lang=${lang}${scopeQs(st)}`, { text: true }); const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([html], { type: "text/html" })); a.download = `watchover-slo-${w}m.html`; a.click(); };
  const hasMetrics = (metrics.data?.samples ?? 0) > 0;

  return <div className="ops">
    {/* 1. one line on health + the time range; sticky while scrolling */}
    <div ref={sentinel} style={{ height: 1, marginBottom: -15 }} />
    <div className={`ops-head ${stuck ? "stuck" : ""}`}>
      <div className="ops-title">
        <h2>{t("ops_title")}</h2>
        {health && <button className={`ops-health ${!s?.total ? "" : !items.length ? "ok" : crit ? "crit" : "warn"}`} onClick={() => document.getElementById("ops-att")?.scrollIntoView({ behavior: "smooth", block: "center" })}><i />{health}</button>}
      </div>
      <TimeRange range={st.range} live={st.live} setLast={st.setLast} setAbs={st.setAbs} />
      <div className="ops-menu">
        <button className="btn" onClick={() => setMenu(!menu)} aria-label={t("ops_more")}><MoreHorizontal size={16} /></button>
        {menu && <div className="menu card" onMouseLeave={() => setMenu(false)}>
          {can("act.report") && <button onClick={() => { setMenu(false); report(); }}>📄 {t("ops_report")}</button>}
          {can("act.connect") && <Link to="/connections">🔗 {t("ops_connect")}</Link>}
          {can("act.sim") && user?.all !== false && <button onClick={async () => { setMenu(false); await post("/live/simulate", { on: !simOn }); qc.invalidateQueries(); }}>🧪 {t("ops_sim")} {simOn ? "ON → OFF" : "OFF → ON"}</button>}
          {can("page.data") && <button onClick={async () => { setMenu(false); await post("/live/analyze"); nav("/datasets"); }}>🗂 {t("ops_analyze")}</button>}
          <button onClick={() => { setMenu(false); setDetail("files"); }}>📁 {t("od_files")}</button>
        </div>}
      </div>
    </div>

    {/* 2. scope: environment, pool and hosts as chips; one search field adds hosts or pools */}
    <Scope st={st} hosts={Object.keys(hostsQ.data?.hosts ?? {})} pools={(fleetAll?.pools ?? []).map((p: any) => p.pool)} envs={Object.keys(hostsQ.data?.environments ?? {})} envSum={envSummary(fleetAll)} />

    {/* 3. four numbers */}
    <div className="ops-kpis">
      <Tile label={st.live ? t("ops_k_volume_now") : t("ops_k_volume")} value={`${fmtN(rateNow, lang)}`} unit={`/${t("tr_min")}`} sub={<>{fmtN(s?.total, lang)} {t("ops_k_lines")} {dTotal.text && <span className="dim">· {dTotal.text}</span>}</>}
        spark={(s?.series ?? []).map((x: any) => x.total)} color="var(--accent2)" onClick={() => setDtab("volume")} />
      <Tile label={t("ops_k_err")} value={fmtPct(s?.error_rate, 2)} tone={s?.error_rate >= 0.05 ? "crit" : s?.error_rate >= 0.01 ? "warn" : "ok"}
        sub={<>{fmtN(s?.errors, lang)} {t("ops_k_errlines")} {dErr.text && <span className={dErr.dir === "up" ? "bad" : "good"}>· {dErr.text}</span>}</>}
        spark={(s?.series ?? []).map((x: any) => x.errors)} color="var(--red)" onClick={() => setDtab("errors")} />
      <Tile label={t("ops_k_avail")} value={fmtPct(s?.availability, 2)} tone={s?.availability == null ? "" : s.availability >= slo ? "ok" : s.availability >= 0.99 ? "warn" : "crit"}
        sub={<>{t("ops_k_target")} %99,9 · <span className="t2-link" onClick={(e) => { e.stopPropagation(); setDtab("latency"); }}>p95 {s?.p95_ms != null ? `${Math.round(s.p95_ms)} ms` : "–"}</span></>} onClick={() => setDtab("avail")} />
      <Tile label={t("ops_k_fleet")} value={<>{counts.ok}<span className="dim" style={{ fontSize: 15 }}> / {hostsAll.length}</span></>} tone={counts.crit || counts.silent ? "warn" : "ok"}
        sub={<FleetBar c={counts} t={t} />} onClick={() => document.getElementById("ops-map")?.scrollIntoView({ behavior: "smooth" })} />
    </div>

    {/* 4. timeline: drag to zoom */}
    <Card title={<span>{t("ops_timeline")} <span className="dim small">· {rangeLabel(st.range, lang)} · {t("ops_step", { m: s?.step_min ?? 1 })}{s?.source === "history" ? ` · ${t("ops_src_hist")}` : ""}</span></span>}
      right={<span className="dim small">{t("ops_drag")}</span>}>
      <Timeline series={s?.series ?? []} stepMin={s?.step_min ?? 1} spanMin={span} onSelect={(a, b) => st.setAbs(a, b)} />
    </Card>

    {/* 4b. only with a pool or hosts in scope: latency, volume and active sessions of just that scope */}
    {(st.pool || st.hosts.length > 0) && <ScopeCharts rq={rq} title={st.hosts.length ? st.hosts.slice(0, 3).join(", ") + (st.hosts.length > 3 ? ` +${st.hosts.length - 3}` : "") : st.pool} series={s?.series ?? []} step={s?.step_min ?? 1} span={span} onZoom={zoom} />}

    {/* 5. what needs a look, and the fleet */}
    <div className="ops-two">
      <div id="ops-att"><Card title={t("ops_attention")}><Attention items={items} /></Card></div>
      <div id="ops-map"><Card title={<span>{t("ops_map")} <span className="dim small">· {mapHosts.length} {t("hm_hosts")} · {(fleetAll?.pools ?? []).length} pool</span></span>}>
        {!mapHosts.length ? <div className="empty">{t("fl_empty")}</div> : <FleetViews rq={rq} pools={fleetAll?.pools ?? []} sel={st.hosts} pool={st.pool} onHost={setHd} onPool={st.setPool} onLogs={goLogs} span={span} onZoom={zoom} />}
      </Card></div>
    </div>

    {hasMetrics && <InfraStrip m={metrics.data} onOpen={(k) => setDetail(k)} />}
    {dtab && <RangeDetail rq={rq} span={span} tab0={dtab} onClose={() => setDtab(null)} onLogs={(f) => { setDtab(null); goLogs(f); }} onHost={(h) => { setDtab(null); setHd(h); }} />}

    {/* 6. logs */}
    <div id="ops-logs"><Card title={t("ops_logs")}>
      <LogExplorer rq={rq} live={st.live} spanMin={span} services={services} focus={focus} onHost={setHd} />
    </Card></div>

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

function Tile({ label, value, unit, sub, spark, color, tone = "", onClick }: { label: string; value: any; unit?: string; sub?: any; spark?: number[]; color?: string; tone?: string; onClick?: () => void }) {
  return <button className={`tile2 ${tone}`} onClick={onClick}>
    <div className="t2-l">{label}</div>
    <div className="t2-v">{value}{unit && <span className="t2-u">{unit}</span>}</div>
    <div className="t2-s">{sub}</div>
    {spark && spark.length > 1 && <div className="t2-sp"><Spark data={spark} color={color} /></div>}
  </button>;
}

function FleetBar({ c, t }: { c: Record<string, number>; t: any }) {
  const n = c.ok + c.warn + c.crit + c.silent || 1;
  return <span className="fb"><span className="fb-bar">{(["ok", "warn", "crit", "silent"] as const).map((k) => c[k] ? <i key={k} className={k} style={{ width: `${(c[k] / n) * 100}%` }} /> : null)}</span>
    <span className="fb-l">{c.crit ? <b className="bad">{c.crit} {t("hm_crit").toLowerCase()}</b> : null}{c.warn ? <span> · {c.warn} {t("hm_warn").toLowerCase()}</span> : null}{c.silent ? <span> · {c.silent} {t("hm_silent").toLowerCase()}</span> : null}{!c.crit && !c.warn && !c.silent ? t("ops_k_allok") : null}</span></span>;
}

/** per environment: hosts, lines, errors in the range (the old environment chips), from the fleet rows */
function envSummary(fleet: any): { env: string; hosts: number; events: number; errors: number }[] {
  const by: Record<string, { env: string; hosts: number; events: number; errors: number }> = {};
  for (const p of fleet?.pools ?? []) for (const h of p.hosts) { const e = (by[h.env] ||= { env: h.env, hosts: 0, events: 0, errors: 0 }); e.hosts++; e.events += h.events; e.errors += h.errors; }
  const order = ["prod", "staging", "test", "qa", "dev", "unknown"]; const rank = (e: string) => (order.indexOf(e) + 99) % 99;
  return Object.values(by).sort((a, b) => rank(a.env) - rank(b.env) || a.env.localeCompare(b.env));
}

function Scope({ st, hosts, pools, envs, envSum }: { st: ReturnType<typeof useOpsState>; hosts: string[]; pools: string[]; envs: string[]; envSum: { env: string; hosts: number; events: number; errors: number }[] }) {
  const { t, lang } = useT(); const [q, setQ] = useState(""); const [open, setOpen] = useState(false);
  const ql = q.toLowerCase();
  const sugg = !ql ? [] : [...envs.filter((e) => e.toLowerCase().includes(ql)).map((v) => ({ k: "env", v })), ...pools.filter((p) => p.toLowerCase().includes(ql)).map((v) => ({ k: "pool", v })),
    ...hosts.filter((h) => h.toLowerCase().includes(ql) && !st.hosts.includes(h)).map((v) => ({ k: "host", v }))].slice(0, 12);
  const pick = (s: { k: string; v: string }) => { if (s.k === "env") st.setEnv(s.v); else if (s.k === "pool") st.setPool(s.v); else st.toggleHost(s.v); setQ(""); setOpen(false); };
  const any = st.env || st.pool || st.hosts.length;
  return <div className="ops-scope">
    <div className="sc-in"><Search size={15} /><input placeholder={t("sc_ph")} value={q} onChange={(e) => { setQ(e.target.value); setOpen(true); }} onFocus={() => setOpen(true)} onBlur={() => setTimeout(() => setOpen(false), 150)}
      onKeyDown={(e) => { if (e.key === "Enter" && sugg[0]) pick(sugg[0]); }} />
      {open && sugg.length > 0 && <div className="sc-pop card">{sugg.map((s) => <button key={s.k + s.v} onMouseDown={() => pick(s)}><span className="dim small">{t("sc_" + s.k)}</span> {s.v}</button>)}</div>}</div>
    {!any && <span className="dim small">{t("sc_all")}</span>}
    {st.env && <span className="chip on">{t("sc_env")}: {st.env}<button onClick={() => st.setEnv("")}><X size={12} /></button></span>}
    {st.pool && <span className="chip on">pool: {st.pool}<button onClick={() => st.setPool("")}><X size={12} /></button></span>}
    {st.hosts.map((h) => <span key={h} className="chip on">{h}<button onClick={() => st.toggleHost(h)}><X size={12} /></button></span>)}
    {any ? <button className="btn sm ghost" onClick={st.clearScope}>{t("sc_clear")}</button> : null}
    <span className="grow" />
    {envSum.length > 0 && <div className="env-chips">{envSum.map((e) => { const r = e.events ? e.errors / e.events : 0;
      return <button key={e.env} className={`chip ${st.env === e.env ? "on" : ""}`} onClick={() => st.setEnv(st.env === e.env ? "" : e.env)} title={`${t("ops_k_avail")} ${e.events ? fmtPct(1 - r, 2) : "–"} · ${fmtN(e.events, lang)} ${t("ops_k_lines")} · ${fmtN(e.errors, lang)} ERROR+`}>
        <span className="d" style={{ background: !e.events ? "var(--dim)" : r >= 0.05 ? "var(--red)" : r >= 0.005 ? "var(--amber)" : "var(--green)" }} />{e.env} · {e.hosts} {t("hm_hosts")}</button>; })}</div>}
  </div>;
}

function InfraStrip({ m, onOpen }: { m: any; onOpen: (k: Kind) => void }) {
  const { t } = useT(); const THR: Record<string, number> = { cpu: 85, gpu: 95, memory: 90, disk: 90 };
  return <div className="ops-infra">{(["cpu", "memory", "disk", "gpu"] as const).map((k) => { const x = m.summary?.[k] ?? {}; if (x.avg == null) return null; const bad = x.max >= THR[k];
    const byMin: Record<string, number[]> = {}; for (const r of (m.per_minute ?? []).filter((r: any) => r.metric === k)) (byMin[r.minute] ||= []).push(r.value);
    const sp = Object.keys(byMin).sort().map((x) => byMin[x].reduce((a, b) => a + b, 0) / byMin[x].length).slice(-30);
    return <button key={k} className={`tile2 slim ${bad ? "crit" : ""}`} onClick={() => onOpen(k)}><div className="t2-l">{k}</div><div className="t2-v" style={{ fontSize: 18 }}>%{Math.round(x.avg)}</div>
      <div className="t2-s">{t("od_max")} %{Math.round(x.max)} · {x.worst}</div>{sp.length > 1 && <div className="t2-sp" style={{ bottom: 12, height: 26 }}><Spark data={sp} color={bad ? "var(--red)" : "var(--accent)"} /></div>}</button>; })}</div>;
}
