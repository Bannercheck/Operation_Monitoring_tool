import { useEffect, useRef, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { ArrowLeft, LogOut, Menu, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { HubTabs, inHub, useNav, type Hub } from "./HubTabs";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Btn, Modal, Field, Err, LangTheme } from "./ui";
import { NotifyBell } from "./NotifyBell";
import { TokenMeter } from "./TokenMeter";
import { ChatWidget } from "./ChatWidget";
import { Wordmark } from "./Logo";
import { get, post } from "../api";
import { useQuery } from "@tanstack/react-query";

export default function Layout() {
  const { user, logout, can, reload } = useAuth();
  const { t } = useT();
  const { pathname } = useLocation();
  const mainRef = useRef<HTMLElement>(null);
  useEffect(() => { mainRef.current?.scrollTo({ top: 0 }); }, [pathname]);   // the content column is the scroller: a new page starts at its top
  const [collapsed, setCollapsed] = useState(() => { try { return localStorage.getItem("wo_side") === "1"; } catch { return false; } });
  const [open, setOpen] = useState(false);
  const [pw, setPw] = useState(!!user?.must_change);
  const { hubs, settings } = useNav();          // one flat SigNoz-style list; the model and the per-page tab row live in HubTabs
  const item = (h: Hub) => { const on = inHub(h, pathname); const more = h.tabs.length > 1 ? `: ${h.tabs.map((x) => t(x.k)).join(" · ")}` : "";
    return <Link key={h.id} to={h.tabs[0].to} className={`tile ${on ? "active" : ""}`} aria-current={on ? "page" : undefined} onClick={() => setOpen(false)} title={t(h.k) + more}><h.icon size={17} /><span>{t(h.k)}</span></Link>; };
  const toggle = () => { setCollapsed((c) => { try { localStorage.setItem("wo_side", c ? "0" : "1"); } catch { /* ignore */ } return !c; }); };
  return (
    <div className="shell">
      {open && <div className="side-veil" onClick={() => setOpen(false)} />}
      <aside className={`side ${collapsed ? "collapsed" : ""} ${open ? "open" : ""}`}>
        <button className="collapse" onClick={toggle} title={collapsed ? t("expand") : t("collapse")}>{collapsed ? <PanelLeftOpen size={14} /> : <PanelLeftClose size={14} />}</button>
        <div className="brand"><Wordmark size={40} /></div>
        <nav className="nav-scroll nav-flat">{hubs.map(item)}</nav>
        {settings.tabs.length > 0 && <div className="nav-bottom nav-flat">{item(settings)}</div>}
        <VersionTag />
        <div className="user"><div className="av">{(user?.name || user?.email || "?")[0].toUpperCase()}</div><div className="info"><b>{user?.name || user?.email}</b><span className="small muted">{user?.role === "member" && user.teams?.length ? user.teams.map((m) => m.role_label).join(", ") : user?.role}</span></div>
          <Btn kind="ghost" sm onClick={logout} title={t("signout")}><LogOut size={14} /></Btn></div>
      </aside>
      <main className="main" ref={mainRef}>
        <div className="topbar">
          <Btn kind="ghost menu-btn" sm onClick={() => setOpen((o) => !o)}><Menu size={18} /></Btn>
          <BackButton />
          <HubTabs />
          <div className="grow" />
          <ScopeBadge />
          {(can("page.assist") || can("page.llm")) && <TokenMeter />}
          {can("page.anom") && <NotifyBell />}
          <LangTheme />
        </div>
        <Outlet />
      </main>
      {can("page.assist") && <ChatWidget />}
      {pw && <ChangePassword onDone={async () => { setPw(false); await reload(); }} />}
    </div>
  );
}

/** A team member sees only the team's hosts: say so in the top bar, so nobody mistakes the view for the whole estate. One team:
 *  "EA · Operatör · 12 sunucu"; several: "EA, İK · 18 sunucu". A click lists every membership (team, role, hosts, manage link). */
function ScopeBadge() {
  const { t } = useT(); const { user } = useAuth(); const [open, setOpen] = useState(false);
  const q = useQuery({ queryKey: ["scope-mine"], queryFn: () => get("/teams/mine"), staleTime: 60000, enabled: !!user && user.all === false });
  if (!user || user.all !== false) return null;
  const ms = user.teams ?? [];
  if (!ms.length) return <span className="badge amber" style={{ textTransform: "none" }} title={t("tm_scope_hint")}>👥 {t("tm_scope_none")} · {t("tmr_hosts_n", { n: 0 })}</span>;
  const hosts = q.data?.hosts ?? Math.max(...ms.map((m) => m.hosts));
  const text = ms.length === 1 ? `${ms[0].name} · ${ms[0].role_label} · ${t("tmr_hosts_n", { n: hosts })}` : `${ms.map((m) => m.name).join(", ")} · ${t("tmr_hosts_n", { n: hosts })}`;
  return <span className="scope-wrap">
    <button className="badge blue scope-badge" onClick={() => setOpen((o) => !o)} title={t("tm_scope_hint")}>👥 {text}</button>
    {open && <><div className="scope-veil" onClick={() => setOpen(false)} /><div className="scope-pop card">
      {ms.map((m) => <div key={m.id} className="scope-row"><b>{m.name}</b><span className="muted">{m.role_label}</span><span className="dim">{t("tmr_hosts_n", { n: m.hosts })}</span>
        {m.manage && <NavLink to="/team" onClick={() => setOpen(false)} className="small">{t("tmr_manage")}</NavLink>}</div>)}
      <div className="dim small" style={{ marginTop: 6 }}>{t("tm_scope_hint")}</div></div></>}
  </span>;
}

function ChangePassword({ onDone }: { onDone: () => void }) {
  const { t } = useT();
  const [cur, setCur] = useState(""); const [nw, setNw] = useState(""); const [err, setErr] = useState<any>(null);
  const submit = async (e: React.FormEvent) => { e.preventDefault(); setErr(null); try { await post("/auth/password", { current: cur, new: nw }); onDone(); } catch (x) { setErr(x); } };
  return (
    <Modal title={t("pw_change_title")} onClose={() => {}}>
      <form className="stack" onSubmit={submit}><p className="muted">{t("pw_change_lead")}</p>
        <Field label={t("pw_current")}><input className="input" type="password" value={cur} onChange={(e) => setCur(e.target.value)} required /></Field>
        <Field label={t("pw_new")}><input className="input" type="password" value={nw} onChange={(e) => setNw(e.target.value)} required minLength={10} /></Field>
        <Err e={err} /><div className="row"><Btn kind="primary" type="submit">{t("save")}</Btn></div></form>
    </Modal>
  );
}

/** The running version, and a one-click reload when the server was updated while this tab stayed open (the tab would otherwise
 *  keep showing the previous build). */
function VersionTag() {
  const { t } = useT(); const first = useRef<string | null>(null);
  const q = useQuery({ queryKey: ["health-version"], queryFn: () => get("/health"), refetchInterval: 60000 });
  const v: string | undefined = q.data?.version;
  if (v && first.current === null) first.current = v;
  if (v && first.current && v !== first.current) return <button className="btn sm primary ver-new" onClick={() => window.location.reload()}>{t("ver_new", { v })}</button>;
  return v ? <div className="ver dim small" title={t("ver_tip")}>Watchover v{v}</div> : null;
}

/** "Geri" on every page: the previous screen inside the app (browser history), or, when the page was opened directly (a shared
 *  link, a new tab), its parent: a detail page goes to its list (/services/x -> /services), a list goes to the dashboards. */
function BackButton() {
  const { t } = useT(); const nav = useNavigate(); const { pathname } = useLocation();
  const idx = (window.history.state as any)?.idx ?? 0;
  if (pathname === "/ops" && idx === 0) return null;
  const back = () => {
    if (idx > 0) { nav(-1); return; }
    const parts = pathname.split("/").filter(Boolean);
    nav(parts.length > 1 ? `/${parts[0]}` : "/ops");
  };
  return <Btn kind="ghost back-btn" sm onClick={back} title={t("nav_back")}><ArrowLeft size={16} /><span className="back-l">{t("nav_back")}</span></Btn>;
}
