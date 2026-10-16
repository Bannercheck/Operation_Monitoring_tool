import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation } from "react-router-dom";
import { AlertTriangle, Boxes, Compass, Plus, Server } from "lucide-react";
import { get, post } from "../../api";
import { useT } from "../../i18n";
import { Btn, Empty, Err, Kpi, Modal, Tabs } from "../../components/ui";
import { SparkArea, ST } from "../../components/dash/kit";
import { AppEditor, CritBadge, LV, StatusBadge, ms, num, pctOf, sloText } from "./common";

/** "Uygulamalar": every business application the reader may see as a card with its health (status and why, request rate, error rate,
 *  p95, SLO, hosts reporting, open alerts, a traffic sparkline), filters by team / environment / criticality / text, discovery of new
 *  applications and the create dialog. */
export default function Apps() {
  const { t, lang } = useT(); const loc = useLocation();
  const [minutes, setMinutes] = useState(60); const [q, setQ] = useState(""); const [team, setTeam] = useState(""); const [env, setEnv] = useState("");
  const [crit, setCrit] = useState(""); const [bad, setBad] = useState(false); const [edit, setEdit] = useState(false); const [disc, setDisc] = useState(false);
  const [msg, setMsg] = useState<string | null>((loc.state as any)?.msg ?? null);
  useEffect(() => { if (!msg) return; const h = setTimeout(() => setMsg(null), 5000); return () => clearTimeout(h); }, [msg]);
  const list = useQuery({ queryKey: ["apps", minutes], queryFn: () => get(`/apps?minutes=${minutes}`), refetchInterval: 15000 });
  const all: any[] = list.data?.apps ?? [];
  const rows = useMemo(() => all.filter((a) => (!q || `${a.name} ${a.description} ${(a.tags ?? []).join(" ")}`.toLowerCase().includes(q.toLowerCase()))
    && (!team || String(a.team_id ?? "") === team) && (!env || (a.envs ?? []).includes(env)) && (!crit || a.criticality === crit)
    && (!bad || ["crit", "warn"].includes(a.health?.status))), [all, q, team, env, crit, bad]);
  const n = (s: string) => all.filter((a) => (a.health?.status ?? "nodata") === s).length;
  return <div className="stack">
    <div className="row between">
      <div><h2><Boxes size={20} style={{ verticalAlign: -3 }} /> {t("ap_title")}</h2><span className="muted small">{t("ap_sub")}</span></div>
      <div className="row" style={{ gap: 8 }}>
        <div className="seg">{[15, 60].map((m) => <button key={m} className={minutes === m ? "on" : ""} onClick={() => setMinutes(m)}>{m < 60 ? `${m} dk` : "1 sa"}</button>)}</div>
        <Btn sm onClick={() => setDisc(true)}><Compass size={14} /> {t("ap_discover")}</Btn>
        {list.data?.can_create && <Btn sm kind="primary" onClick={() => setEdit(true)}><Plus size={14} /> {t("ap_new")}</Btn>}
      </div>
    </div>
    {msg && <div className="card tight small">{msg}</div>}
    <div className="grid k4 ap-kpis">
      <Kpi value={all.length} label={t("ap_k_apps")} sub={t("ap_k_apps_sub")} icon={<Boxes size={16} />} />
      <Kpi value={n("crit")} label={t("ap_k_crit")} accent={ST.crit} sub={t("ap_window", { n: minutes })} />
      <Kpi value={n("warn")} label={t("ap_k_warn")} accent={ST.warn} sub={t("ap_window", { n: minutes })} />
      <Kpi value={n("ok")} label={t("ap_k_ok")} accent={ST.ok} sub={t("ap_k_nodata_sub", { n: n("nodata") })} />
    </div>
    <div className="ap-filters">
      <input className="input" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("ap_search")} />
      {(list.data?.teams ?? []).length > 0 && <select className="input" value={team} onChange={(e) => setTeam(e.target.value)}><option value="">{t("ap_all_teams")}</option>
        {(list.data?.teams ?? []).map((x: any) => <option key={x.id} value={x.id}>{x.name}</option>)}</select>}
      <select className="input" value={env} onChange={(e) => setEnv(e.target.value)}><option value="">{t("ap_all_envs")}</option>
        {(list.data?.envs ?? []).map((x: string) => <option key={x} value={x}>{x}</option>)}</select>
      <select className="input" value={crit} onChange={(e) => setCrit(e.target.value)}><option value="">{t("ap_all_crit")}</option>
        {["critical", "high", "normal", "low"].map((k) => <option key={k} value={k}>{t(`ap_crit_${k}`)}</option>)}</select>
      <div className="seg"><button className={!bad ? "on" : ""} onClick={() => setBad(false)}>{t("ap_all")}</button><button className={bad ? "on" : ""} onClick={() => setBad(true)}>{t("ap_only_problems")}</button></div>
    </div>
    <Err e={list.error} />
    {list.isLoading ? <div className="muted">{t("loading")}</div> : !all.length ? <Empty>{t("ap_empty")}<div className="row" style={{ justifyContent: "center", marginTop: 12 }}>
      <Btn sm onClick={() => setDisc(true)}><Compass size={14} /> {t("ap_discover")}</Btn>{list.data?.can_create && <Btn sm kind="primary" onClick={() => setEdit(true)}><Plus size={14} /> {t("ap_new")}</Btn>}</div></Empty>
      : !rows.length ? <Empty>{t("ap_none_match")}</Empty>
      : <div className="ap-grid">{rows.map((a) => <AppCard key={a.id} a={a} lang={lang} />)}</div>}
    {edit && <AppEditor onClose={() => setEdit(false)} />}
    {disc && <Discovery apps={all} onClose={() => setDisc(false)} />}
  </div>;
}

function AppCard({ a, lang }: { a: any; lang: string }) {
  const { t } = useT(); const h = a.health ?? {}; const rq = h.requests ?? {}; const lg = h.logs ?? {};
  const traced = (rq.calls ?? 0) > 0; const slo = sloText(t, h.slo); const hs = h.hosts ?? {}; const al = h.alerts ?? {};
  const alerts = (al.crit ?? 0) + (al.warn ?? 0) + (al.anomalies ?? 0);
  return <Link to={`/apps/${a.id}`} className={`card ap-card st-${LV[h.status ?? "nodata"]}`}>
    <div className="ap-card-h">
      <div className="ap-card-t"><b title={a.name}>{a.name}</b><StatusBadge s={h.status} /></div>
      <div className="row" style={{ gap: 6 }}><CritBadge v={a.criticality} />{a.team ? <span className="chip">{a.team}</span> : <span className="chip dim">{t("ap_install")}</span>}
        {(a.envs ?? []).slice(0, 3).map((e: string) => <span key={e} className="chip">{e}</span>)}</div>
    </div>
    {a.description && <div className="dim small ap-desc">{a.description}</div>}
    <div className="ap-m">
      <div><span>{traced ? t("ap_rate") : t("ap_lines_min")}</span><b>{num(traced ? rq.per_min : lg.per_min, lang)}</b></div>
      <div><span>{t("ap_err_rate")}</span><b className={((traced ? rq.error_rate : lg.error_rate) ?? 0) >= 0.05 ? "bad" : ""}>{pctOf(traced ? rq.error_rate : lg.error_rate)}</b></div>
      <div><span>{t("ap_p95")}</span><b>{ms(rq.p95_ms ?? lg.p95_ms)}</b></div>
      <div title={slo.sub}><span>{t("ap_slo")}</span><b className={slo.tone === "crit" ? "bad" : ""}>{slo.main}</b></div>
    </div>
    <div className="ap-spark"><SparkArea data={h.spark ?? []} color="var(--s1)" /></div>
    <div className="ap-card-f small">
      <span><Server size={12} /> {hs.total ? t("ap_hosts_rep", { r: hs.reporting ?? 0, n: hs.total }) : t("ap_no_hosts")}</span>
      <span className={alerts ? "bad" : "dim"}><AlertTriangle size={12} /> {alerts ? t("ap_alerts_n", { n: alerts }) : t("ap_no_alerts")}</span>
    </div>
    {(h.reasons ?? []).length > 0 && <div className="ap-reasons">{h.reasons.slice(0, 3).map((r: string) => <span key={r} className="badge amber" style={{ textTransform: "none" }}>{t(`ap_r_${r}`)}</span>)}</div>}
  </Link>;
}

/** Discovery: proposals (create, create under another name, add explicitly to the existing one, dismiss) and the hosts / services no
 *  application covers (pick some, add them to an application). */
function Discovery({ apps, onClose }: { apps: any[]; onClose: () => void }) {
  const { t } = useT(); const qc = useQueryClient();
  const d = useQuery({ queryKey: ["apps-discovery"], queryFn: () => get("/apps/discovery") });
  const cat = useQuery({ queryKey: ["apps-catalog"], queryFn: () => get("/apps/catalog"), staleTime: 30000 });
  const [tab, setTab] = useState<"p" | "h" | "s">("p"); const [err, setErr] = useState<any>(null); const [names, setNames] = useState<Record<string, string>>({});
  const [team, setTeam] = useState<string>(""); const [sel, setSel] = useState<string[]>([]); const [into, setInto] = useState("");
  const props: any[] = d.data?.proposals ?? []; const hosts: any[] = d.data?.unmatched_hosts ?? []; const svcs: any[] = d.data?.unmatched_services ?? [];
  const teams: any[] = cat.data?.teams ?? []; const install = !!cat.data?.install; const canWrite = install || teams.length > 0;
  const teamVal = team !== "" ? team : install ? "" : String(teams[0]?.id ?? "");
  const done = () => { qc.invalidateQueries({ queryKey: ["apps"] }); qc.invalidateQueries({ queryKey: ["apps-discovery"] }); };
  const act = async (fn: () => Promise<any>) => { setErr(null); try { await fn(); done(); } catch (e) { setErr(e); } };
  const accept = (p: any, extra: any = {}) => act(() => post("/apps/discovery/accept", { key: p.key, team_id: teamVal === "" ? null : Number(teamVal), ...extra }));
  const editable = apps.filter((a) => a.editable);
  const assign = () => act(async () => { await post(`/apps/${into}/assign`, tab === "h" ? { hosts: sel } : { services: sel }); setSel([]); });
  const toggle = (k: string) => setSel((s) => (s.includes(k) ? s.filter((x) => x !== k) : [...s, k]));
  return <Modal wide title={<span className="row" style={{ gap: 8 }}><Compass size={16} /> {t("ap_d_title")}</span>} onClose={onClose}>
    <div className="stack">
      <div className="dim small">{t("ap_d_lead")}</div>
      <div className="row between">
        <Tabs value={tab} onChange={(k) => { setTab(k); setSel([]); }} tabs={[{ k: "p", label: t("ap_d_props", { n: props.length }) }, { k: "h", label: t("ap_d_hosts", { n: d.data?.counts?.hosts ?? 0 }) },
          { k: "s", label: t("ap_d_svcs", { n: d.data?.counts?.services ?? 0 }) }]} />
        {tab === "p" && canWrite && (install || teams.length > 1) && <select className="input" style={{ width: 200 }} value={teamVal} onChange={(e) => setTeam(e.target.value)}>
          {install && <option value="">{t("ap_install")}</option>}{teams.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}</select>}
      </div>
      <Err e={err} />
      {d.isLoading ? <div className="muted">{t("loading")}</div> : tab === "p" ? (!props.length ? <Empty>{t("ap_d_none")}</Empty> : <div className="stack" style={{ gap: 8 }}>
        {props.map((p) => <div key={p.key} className="card tight ap-prop">
          <div className="row between">
            <div className="row" style={{ gap: 8 }}><span className="badge blue" style={{ textTransform: "none" }}>{t(`ap_d_src_${p.source}`)}</span><b>{p.name}</b>
              <span className="dim small">{[p.host_count ? t("ap_d_hosts_n", { n: p.host_count }) : "", p.service_count ? t("ap_d_svcs_n", { n: p.service_count }) : ""].filter(Boolean).join(" · ")}</span></div>
            {canWrite && <div className="row" style={{ gap: 6 }}>
              {p.exists ? <>
                {editable.some((a) => a.id === p.exists) && <Btn sm onClick={() => accept(p, { into: p.exists })}>{t("ap_d_merge")}</Btn>}
                <input className="input" style={{ width: 170 }} placeholder={t("ap_d_rename")} value={names[p.key] ?? ""} onChange={(e) => setNames({ ...names, [p.key]: e.target.value })} />
                <Btn sm kind="primary" disabled={!(names[p.key] ?? "").trim()} onClick={() => accept(p, { name: names[p.key].trim() })}>{t("ap_d_create")}</Btn></>
                : <Btn sm kind="primary" onClick={() => accept(p)}>{t("ap_d_create")}</Btn>}
              <Btn sm kind="ghost" onClick={() => act(() => post("/apps/discovery/dismiss", { key: p.key }))}>{t("ap_d_dismiss")}</Btn></div>}
          </div>
          {p.exists && <div className="small" style={{ color: "var(--amber)", marginTop: 6 }}>{t("ap_d_exists")}</div>}
          <div className="ap-samples mono small">{[...p.hosts, ...p.services].slice(0, 12).join(" · ")}{p.host_count + p.service_count > 12 ? " …" : ""}</div>
        </div>)}</div>)
        : <div className="stack" style={{ gap: 8 }}>
          {(tab === "h" ? hosts : svcs).length === 0 ? <Empty>{t("ap_d_all_matched")}</Empty> : <>
            <div className="ap-pick">{(tab === "h" ? hosts : svcs).map((x: any) => { const k = tab === "h" ? x.host : x.service;
              return <label key={k} className={`ap-pick-r ${sel.includes(k) ? "on" : ""}`}><input type="checkbox" checked={sel.includes(k)} onChange={() => toggle(k)} />
                <span className="mono">{k}</span><span className="dim small">{tab === "h" ? [x.env, x.pool, x.application && `${t("ap_d_inv_app")}: ${x.application}`].filter(Boolean).join(" · ") : t("ap_d_hosts_n", { n: x.hosts })}</span></label>; })}</div>
            {editable.length > 0 && <div className="row">
              <span className="dim small">{t("ap_d_sel", { n: sel.length })}</span>
              <select className="input" style={{ width: 220 }} value={into} onChange={(e) => setInto(e.target.value)}><option value="">{t("ap_d_pick_app")}</option>
                {editable.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}</select>
              <Btn sm kind="primary" disabled={!into || !sel.length} onClick={assign}>{t("ap_d_assign")}</Btn></div>}
          </>}
        </div>}
    </div>
  </Modal>;
}
