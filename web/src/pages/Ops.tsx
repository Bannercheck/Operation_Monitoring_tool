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
import { HostMap } from "../components/ops/HostMap";
import { Attention, buildAttention } from "../components/ops/Attention";
import { LogExplorer } from "../components/ops/LogExplorer";
import { DetailPanel, type Kind } from "../components/ops/Details";
import { delta, fmtN, fmtPct, hostState, rangeLabel, useOpsState } from "../components/ops/util";

/** Live operations, built to be read in five seconds: one sentence on health, four numbers, the timeline, what needs a look,
 *  the fleet at a glance and the logs. One time range (quick, custom, or dragged on the timeline) and one scope (environment,
 *  pool, hosts) drive every card and live in the URL, so a view can be shared as a link. */
export default function Ops() {
  const { t, lang } = useT(); const { can } = useAuth(); const qc = useQueryClient(); const nav = useNavigate();
  const st = useOpsState(); const rq = st.rq();
  const refresh = st.live ? 10000 : false;
  const sum = useQuery({ queryKey: ["range-sum", rq], queryFn: () => get(`/live/range/summary?${rq}`), refetchInterval: st.live ? 5000 : false });
  const fleetQ = useQuery({ queryKey: ["range-fleet", rq.replace(/&?(pool|host)=[^&]*/g, "")], queryFn: () => get(`/live/range/fleet?${rq.replace(/&?(pool|host)=[^&]*/g, "")}`), refetchInterval: refresh });
  const errG = useQuery({ queryKey: ["range-groups-err", rq], queryFn: () => get(`/live/range/groups?${rq}&severity=ERROR&limit=40`), refetchInterval: refresh });
  const anom = useQuery({ queryKey: ["ops-anoms"], queryFn: () => get("/anomalies?status=open"), refetchInterval: 30000 });
  const tickets = useQuery({ queryKey: ["ops-tickets"], queryFn: () => get("/itsm/tickets"), enabled: can("page.itsm"), refetchInterval: 60000 });
  const sys = useQuery({ queryKey: ["sys-status-lite"], queryFn: () => get("/system/status"), enabled: can("sys.status"), refetchInterval: 15000 });
  const hostsQ = useQuery({ queryKey: ["live-hosts"], queryFn: () => get("/live/hosts"), refetchInterval: 30000 });
  const metrics = useQuery({ queryKey: ["live-metrics", rq], queryFn: () => get(`/live/metrics?window=15${scopeQs(st)}`), refetchInterval: refresh });
  const [detail, setDetail] = useState<Kind | null>(null); const [focus, setFocus] = useState<any>(null); const [menu, setMenu] = useState(false);

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
  const goLogs = (f: any) => { setFocus({ ...f, n: Date.now() }); document.getElementById("ops-logs")?.scrollIntoView({ behavior: "smooth", block: "start" }); };
  const items = useMemo(() => buildAttention(t, lang, fleet, errG.data?.groups ?? [], (anom.data ?? []).filter((a: any) => !a.host || !st.hosts.length || st.hosts.includes(a.host)), (tickets.data?.tickets ?? []).slice().sort((a: any, b: any) => b.relevance - a.relevance),
    { pool: st.setPool, hosts: (hs) => { st.clearScope(); setTimeout(() => hs.forEach((h) => st.toggleHost(h)), 0); }, group: (g) => goLogs({ tab: "groups", sev: "ERROR", q: "", service: g.service === "-" ? "" : g.service }) }), [fleet, errG.data, anom.data, tickets.data, lang]);  // eslint-disable-line
  const nCrit = items.filter((i) => i.level === 3).length; const crit = nCrit > 0;
  const health = !s ? "" : !s.total ? t("ops_h_nodata") : !items.length ? t("ops_h_ok") : crit ? t("ops_h_crit", { n: items.length, c: nCrit }) : t("ops_h_warn", { n: items.length });
  const simOn = !!sys.data?.live?.simulator;
  const services: string[] = (s?.services ?? []).map((x: any) => x[0]);
  const slo = 0.999;
  // live: the rate of the last complete bar; a fixed range: its average
  const bars = s?.series ?? []; const lastFull = st.live && bars.length > 1 ? bars[bars.length - 2] : null;
  const rateNow = lastFull ? lastFull.total / (s?.step_min || 1) : s?.per_min;
  const dTotal = delta(s?.per_min, s?.prev?.per_min); const dErr = delta(s?.error_rate, s?.prev?.error_rate);
  const report = async () => { const w = "last" in st.range ? st.range.last : Math.max(1, Math.round((new Date(st.range.end).getTime() - new Date(st.range.start).getTime()) / 60000)); const html = await api<string>(`/live/report?window=${w}&lang=${lang}${scopeQs(st)}`, { text: true }); const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([html], { type: "text/html" })); a.download = `watchover-slo-${w}m.html`; a.click(); };
  const hasMetrics = (metrics.data?.samples ?? 0) > 0;

  return <div className="ops">
    {/* 1. one line on health + the time range; sticky while scrolling */}
    <div ref={sentinel} style={{ height: 1, marginBottom: -15 }} />
    <div className={`ops-head ${stuck ? "stuck" : ""}`}>
      <div className="ops-title">
        <h2>{t("ops_title")}</h2>
        {health && <span className={`ops-health ${!s?.total ? "" : !items.length ? "ok" : crit ? "crit" : "warn"}`}><i />{health}</span>}
      </div>
      <TimeRange range={st.range} live={st.live} setLast={st.setLast} setAbs={st.setAbs} />
      <div className="ops-menu">
        <button className="btn" onClick={() => setMenu(!menu)} aria-label={t("ops_more")}><MoreHorizontal size={16} /></button>
        {menu && <div className="menu card" onMouseLeave={() => setMenu(false)}>
          {can("act.report") && <button onClick={() => { setMenu(false); report(); }}>📄 {t("ops_report")}</button>}
          {can("act.connect") && <Link to="/connections">🔗 {t("ops_connect")}</Link>}
          {can("act.sim") && <button onClick={async () => { setMenu(false); await post("/live/simulate", { on: !simOn }); qc.invalidateQueries(); }}>🧪 {t("ops_sim")} {simOn ? "ON → OFF" : "OFF → ON"}</button>}
          {can("page.data") && <button onClick={async () => { setMenu(false); await post("/live/analyze"); nav("/datasets"); }}>🗂 {t("ops_analyze")}</button>}
          <button onClick={() => { setMenu(false); setDetail("files"); }}>📁 {t("od_files")}</button>
        </div>}
      </div>
    </div>

    {/* 2. scope: environment, pool and hosts as chips; one search field adds hosts or pools */}
    <Scope st={st} hosts={Object.keys(hostsQ.data?.hosts ?? {})} pools={(fleet?.pools ?? []).map((p: any) => p.pool)} envs={Object.keys(hostsQ.data?.environments ?? {})} />

    {/* 3. four numbers */}
    <div className="ops-kpis">
      <Tile label={st.live ? t("ops_k_volume_now") : t("ops_k_volume")} value={`${fmtN(rateNow, lang)}`} unit={`/${t("tr_min")}`} sub={<>{fmtN(s?.total, lang)} {t("ops_k_lines")} {dTotal.text && <span className="dim">· {dTotal.text}</span>}</>}
        spark={(s?.series ?? []).map((x: any) => x.total)} color="var(--accent2)" onClick={() => goLogs({ tab: "groups", sev: "INFO" })} />
      <Tile label={t("ops_k_err")} value={fmtPct(s?.error_rate, 2)} tone={s?.error_rate >= 0.05 ? "crit" : s?.error_rate >= 0.01 ? "warn" : "ok"}
        sub={<>{fmtN(s?.errors, lang)} {t("ops_k_errlines")} {dErr.text && <span className={dErr.dir === "up" ? "bad" : "good"}>· {dErr.text}</span>}</>}
        spark={(s?.series ?? []).map((x: any) => x.errors)} color="var(--red)" onClick={() => goLogs({ tab: "groups", sev: "ERROR" })} />
      <Tile label={t("ops_k_avail")} value={fmtPct(s?.availability, 2)} tone={s?.availability == null ? "" : s.availability >= slo ? "ok" : s.availability >= 0.99 ? "warn" : "crit"}
        sub={<>{t("ops_k_target")} %99,9 · p95 {s?.p95_ms != null ? `${Math.round(s.p95_ms)} ms` : "–"}</>} onClick={() => st.live && setDetail("availability")} />
      <Tile label={t("ops_k_fleet")} value={<>{counts.ok}<span className="dim" style={{ fontSize: 15 }}> / {hostsAll.length}</span></>} tone={counts.crit || counts.silent ? "warn" : "ok"}
        sub={<FleetBar c={counts} t={t} />} onClick={() => document.getElementById("ops-map")?.scrollIntoView({ behavior: "smooth" })} />
    </div>

    {/* 4. timeline: drag to zoom */}
    <Card title={<span>{t("ops_timeline")} <span className="dim small">· {rangeLabel(st.range, lang)} · {t("ops_step", { m: s?.step_min ?? 1 })}{s?.source === "history" ? ` · ${t("ops_src_hist")}` : ""}</span></span>}
      right={<span className="dim small">{t("ops_drag")}</span>}>
      <Timeline series={s?.series ?? []} stepMin={s?.step_min ?? 1} spanMin={span} onSelect={(a, b) => st.setAbs(a, b)} />
    </Card>

    {/* 5. what needs a look, and the fleet */}
    <div className="ops-two">
      <Card title={t("ops_attention")}><Attention items={items} /></Card>
      <div id="ops-map"><Card title={<span>{t("ops_map")} <span className="dim small">· {mapHosts.length} {t("hm_hosts")} · {(fleetAll?.pools ?? []).length} pool</span></span>}>
        {!mapHosts.length ? <div className="empty">{t("fl_empty")}</div> : <HostMap pools={fleetAll?.pools ?? []} sel={st.hosts} pool={st.pool} onHost={st.toggleHost} onPool={st.setPool} />}
      </Card></div>
    </div>

    {hasMetrics && <InfraStrip m={metrics.data} onOpen={(k) => setDetail(k)} />}

    {/* 6. logs */}
    <div id="ops-logs"><Card title={t("ops_logs")}>
      <LogExplorer rq={rq} live={st.live} spanMin={span} services={services} focus={focus} onHost={(h) => { if (!st.hosts.includes(h)) st.toggleHost(h); }} />
    </Card></div>

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

function Scope({ st, hosts, pools, envs }: { st: ReturnType<typeof useOpsState>; hosts: string[]; pools: string[]; envs: string[] }) {
  const { t } = useT(); const [q, setQ] = useState(""); const [open, setOpen] = useState(false);
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
  </div>;
}

function InfraStrip({ m, onOpen }: { m: any; onOpen: (k: Kind) => void }) {
  const { t } = useT(); const THR: Record<string, number> = { cpu: 85, gpu: 95, memory: 90, disk: 90 };
  return <div className="ops-infra">{(["cpu", "memory", "disk", "gpu"] as const).map((k) => { const x = m.summary?.[k] ?? {}; if (x.avg == null) return null; const bad = x.max >= THR[k];
    return <button key={k} className={`tile2 slim ${bad ? "crit" : ""}`} onClick={() => onOpen(k)}><div className="t2-l">{k}</div><div className="t2-v" style={{ fontSize: 18 }}>%{Math.round(x.avg)}</div>
      <div className="t2-s">{t("od_max")} %{Math.round(x.max)} · {x.worst}</div></button>; })}</div>;
}
