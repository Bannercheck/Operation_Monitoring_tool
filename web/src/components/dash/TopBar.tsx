/** The dashboard bar (after OpenObserve / Grafana): the built-in dashboards as tabs, auto refresh, refresh now, a share link (the URL
 *  holds dashboard, range, variables and refresh) and fullscreen; and the variables row (environment, pool, servers, service). */
import { useEffect, useState, type ReactNode, type RefObject } from "react";
import { AppWindow, Check, LayoutDashboard, Link2, Maximize, Minimize, RotateCw, ScrollText, Search, Server, Users, X } from "lucide-react";
import { useT } from "../../i18n";
import { fmtN, fmtPct, type useOpsState } from "../ops/util";
import { DASHES, REFRESH, type DashId } from "./ctx";

const ICON: Record<DashId, any> = { overview: LayoutDashboard, infra: Server, app: AppWindow, logs: ScrollText, ux: Users };

export function DashTabs({ value, onChange }: { value: DashId; onChange: (d: DashId) => void }) {
  const { t } = useT();
  return <nav className="dtabs" role="tablist" aria-label={t("db_dashboards")}>{DASHES.map((d) => { const I = ICON[d];
    return <button key={d} role="tab" aria-selected={value === d} className={value === d ? "on" : ""} onClick={() => onChange(d)}><I size={15} />{t("db_d_" + d)}</button>; })}</nav>;
}

export function BarTools({ every, setEvery, onRefresh, fetching, root }: { every: number; setEvery: (s: number) => void; onRefresh: () => void; fetching: boolean; root: RefObject<HTMLElement> }) {
  const { t } = useT(); const [copied, setCopied] = useState(false); const [fs, setFs] = useState(false);
  useEffect(() => { const f = () => setFs(!!document.fullscreenElement); document.addEventListener("fullscreenchange", f); return () => document.removeEventListener("fullscreenchange", f); }, []);
  const share = async () => {
    const url = window.location.href;
    try { await navigator.clipboard.writeText(url); } catch { const ta = document.createElement("textarea"); ta.value = url; document.body.appendChild(ta); ta.select(); try { document.execCommand("copy"); } catch { /* nothing to copy into */ } ta.remove(); }
    setCopied(true); setTimeout(() => setCopied(false), 1800);
  };
  const full = () => { if (document.fullscreenElement) document.exitFullscreen?.(); else root.current?.requestFullscreen?.().catch(() => undefined); };
  const lbl = (s: number) => (!s ? t("db_off") : s < 60 ? `${s} ${t("db_sec")}` : `${s / 60} ${t("tr_min")}`);
  return <div className="dtools">
    <label className="dt-rf" title={t("db_auto")}><RotateCw size={14} className={fetching ? "spin" : ""} /><span className="sr">{t("db_auto")}</span>
      <select value={every} onChange={(e) => setEvery(Number(e.target.value))} aria-label={t("db_auto")}>{REFRESH.map((s) => <option key={s} value={s}>{lbl(s)}</option>)}</select></label>
    <button className="dt-b" onClick={onRefresh} title={t("db_refresh_all")} aria-label={t("db_refresh_all")}><RotateCw size={15} /></button>
    <button className={`dt-b ${copied ? "ok" : ""}`} onClick={share} title={t("db_share")} aria-label={t("db_share")}>{copied ? <Check size={15} /> : <Link2 size={15} />}</button>
    <button className="dt-b" onClick={full} title={fs ? t("db_full_exit") : t("db_fullscreen")} aria-label={t("db_fullscreen")}>{fs ? <Minimize size={15} /> : <Maximize size={15} />}</button>
    {copied && <span className="dt-toast" role="status">{t("db_copied")}</span>}
  </div>;
}

function Var({ label, children, wide }: { label: string; children: ReactNode; wide?: boolean }) {
  return <div className={`var ${wide ? "wide" : ""}`}><span className="var-l">{label}</span><div className="var-v">{children}</div></div>;
}

function VarSel({ label, value, options, onChange, all }: { label: string; value: string; options: string[]; onChange: (v: string) => void; all: string }) {
  const opts = value && !options.includes(value) ? [value, ...options] : options;
  return <Var label={label}><select value={value} onChange={(e) => onChange(e.target.value)} aria-label={label}><option value="">{all}</option>{opts.map((o) => <option key={o} value={o}>{o}</option>)}</select></Var>;
}

/** Variables: environment and pool as dropdowns, servers as a search that also accepts an environment or a pool (chips), the service
 *  where the dashboard uses it; the environments' health chips on the right (a click toggles that environment). */
export function Vars({ st, hosts, pools, envs, envSum, services, svc, setSvc, showSvc }: { st: ReturnType<typeof useOpsState>; hosts: string[]; pools: string[]; envs: string[];
  envSum: { env: string; hosts: number; events: number; errors: number }[]; services: string[]; svc: string; setSvc: (v: string) => void; showSvc: boolean }) {
  const { t, lang } = useT(); const [q, setQ] = useState(""); const [open, setOpen] = useState(false);
  const ql = q.toLowerCase();
  const sugg = !ql ? [] : [...envs.filter((e) => e.toLowerCase().includes(ql)).map((v) => ({ k: "env", v })), ...pools.filter((p) => p.toLowerCase().includes(ql)).map((v) => ({ k: "pool", v })),
    ...hosts.filter((h) => h.toLowerCase().includes(ql) && !st.hosts.includes(h)).map((v) => ({ k: "host", v }))].slice(0, 12);
  const pick = (s: { k: string; v: string }) => { if (s.k === "env") st.setEnv(s.v); else if (s.k === "pool") st.setPool(s.v); else st.toggleHost(s.v); setQ(""); setOpen(false); };
  const any = st.env || st.pool || st.hosts.length || (showSvc && svc);
  return <div className="ops-scope vars">
    <VarSel label={t("sc_env")} value={st.env} options={envs} onChange={st.setEnv} all={t("db_all")} />
    <VarSel label="Pool" value={st.pool} options={pools} onChange={st.setPool} all={t("db_all")} />
    <Var label={t("db_var_hosts")} wide>
      <div className="sc-in"><Search size={14} />
        {st.hosts.map((h) => <span key={h} className="chip on">{h}<button onClick={() => st.toggleHost(h)} aria-label={`${h} ✕`}><X size={12} /></button></span>)}
        <input placeholder={st.hosts.length ? "" : t("sc_ph")} value={q} onChange={(e) => { setQ(e.target.value); setOpen(true); }} onFocus={() => setOpen(true)} onBlur={() => setTimeout(() => setOpen(false), 150)}
          onKeyDown={(e) => { if (e.key === "Enter" && sugg[0]) pick(sugg[0]); if (e.key === "Backspace" && !q && st.hosts.length) st.toggleHost(st.hosts[st.hosts.length - 1]); }} />
        {open && sugg.length > 0 && <div className="sc-pop card">{sugg.map((s) => <button key={s.k + s.v} onMouseDown={() => pick(s)}><span className="dim small">{t("sc_" + s.k)}</span> {s.v}</button>)}</div>}
      </div>
    </Var>
    {showSvc && <VarSel label={t("db_svc")} value={svc} options={services} onChange={setSvc} all={t("db_all")} />}
    {any ? <button className="btn sm ghost var-clear" onClick={() => { st.clearScope(); if (showSvc) setSvc(""); }}>{t("sc_clear")}</button> : <span className="dim small var-all">{t("sc_all")}</span>}
    <span className="grow" />
    {envSum.length > 0 && <div className="env-chips">{envSum.map((e) => { const r = e.events ? e.errors / e.events : 0;
      return <button key={e.env} className={`chip ${st.env === e.env ? "on" : ""}`} onClick={() => st.setEnv(st.env === e.env ? "" : e.env)} title={`${t("ops_k_avail")} ${e.events ? fmtPct(1 - r, 2) : "–"} · ${fmtN(e.events, lang)} ${t("ops_k_lines")} · ${fmtN(e.errors, lang)} ERROR+`}>
        <span className="d" style={{ background: !e.events ? "var(--st-none)" : r >= 0.05 ? "var(--st-crit)" : r >= 0.005 ? "var(--st-warn)" : "var(--st-ok)" }} />{e.env} · {e.hosts} {t("hm_hosts")}</button>; })}</div>}
  </div>;
}
