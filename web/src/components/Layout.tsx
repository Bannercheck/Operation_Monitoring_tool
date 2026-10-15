import { useRef, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { AlertTriangle, BellRing, BookOpen, Bot, Brain, Bug, Route, ChevronDown, ChevronRight, ClipboardList, Database, FileText, GitBranch, Layers, LayoutDashboard, LogOut, Menu, Network, PanelLeftClose, PanelLeftOpen, Plug, Radar, Server, ShieldAlert, Settings2, Smartphone, Ticket, Users, Workflow } from "lucide-react";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Btn, Modal, Field, Err, LangTheme } from "./ui";
import { NotifyBell } from "./NotifyBell";
import { TokenMeter } from "./TokenMeter";
import { ChatWidget } from "./ChatWidget";
import { Wordmark } from "./Logo";
import { get, post } from "../api";
import { useQuery } from "@tanstack/react-query";

/** SigNoz-style navigation: a home row, then signal-oriented sections (APM, signals, response, intelligence, security), each
 *  collapsible and remembered; settings and administration are pinned to the bottom, as in SigNoz. Items only show with their
 *  permission; `unless` hides an item for users who reach the same page another way (System holds Alerts and README). */
type Item = { to: string; k: string; icon: any; perm: string; unless?: string };
type Section = { id: string; group?: string; items: Item[] };
export const NAV: Section[] = [
  { id: "home", items: [{ to: "/ops", k: "nav_home", icon: LayoutDashboard, perm: "page.ops" }] },
  { id: "apm", group: "nav_group_apm", items: [
    { to: "/services", k: "nav_services", icon: Layers, perm: "page.apm" }, { to: "/traces", k: "nav_traces", icon: GitBranch, perm: "page.apm" },
    { to: "/rum", k: "nav_rum", icon: Smartphone, perm: "page.rum" }, { to: "/map", k: "nav_map", icon: Network, perm: "page.map" },
    { to: "/integrations", k: "nav_integ", icon: Workflow, perm: "page.apm" }, { to: "/flows", k: "nav_flows", icon: Route, perm: "page.apm" },
    { to: "/exceptions", k: "nav_exc", icon: Bug, perm: "page.apm" } ] },
  { id: "signals", group: "nav_group_signals", items: [
    { to: "/datasets", k: "nav_data", icon: Database, perm: "page.data" }, { to: "/inventory", k: "nav_inv", icon: Server, perm: "page.inv" },
    { to: "/anomalies", k: "nav_anom", icon: AlertTriangle, perm: "page.ops" } ] },
  { id: "respond", group: "nav_group_respond", items: [
    { to: "/actions", k: "nav_actions", icon: ClipboardList, perm: "page.data" }, { to: "/playbook", k: "nav_pb", icon: BookOpen, perm: "page.pb" },
    { to: "/itsm", k: "nav_itsm", icon: Ticket, perm: "page.itsm" }, { to: "/alerts", k: "nav_ntf", icon: BellRing, perm: "sys.notify", unless: "sys.status" } ] },
  { id: "intel", group: "nav_group_intel", items: [
    { to: "/knowledge", k: "nav_kb", icon: Brain, perm: "page.assist" }, { to: "/llm", k: "nav_llm", icon: Bot, perm: "page.llm" } ] },
  { id: "security", group: "nav_group_security", items: [{ to: "/scan", k: "nav_scan", icon: ShieldAlert, perm: "page.scan" }] },
];
export const NAV_SETTINGS: Item[] = [
  { to: "/connections", k: "nav_conn", icon: Plug, perm: "page.conn" }, { to: "/sources", k: "nav_sources", icon: Radar, perm: "page.src" },
  { to: "/users", k: "nav_users", icon: Users, perm: "page.users" }, { to: "/system", k: "nav_sys", icon: Settings2, perm: "sys.status" },
  { to: "/readme", k: "nav_readme", icon: FileText, perm: "page.readme", unless: "sys.status" },
];

function useFolded(key: string): [Record<string, boolean>, (id: string) => void] {
  const [f, setF] = useState<Record<string, boolean>>(() => { try { return JSON.parse(localStorage.getItem(key) || "{}"); } catch { return {}; } });
  const flip = (id: string) => setF((o) => { const n = { ...o, [id]: !o[id] }; try { localStorage.setItem(key, JSON.stringify(n)); } catch { /* ignore */ } return n; });
  return [f, flip];
}

export default function Layout() {
  const { user, logout, can, reload } = useAuth();
  const { t } = useT();
  const [collapsed, setCollapsed] = useState(() => { try { return localStorage.getItem("wo_side") === "1"; } catch { return false; } });
  const [open, setOpen] = useState(false);
  const [pw, setPw] = useState(!!user?.must_change);
  const [folded, fold] = useFolded("wo_nav_fold");
  const visible = (i: Item) => can(i.perm) && !(i.unless && can(i.unless));
  const settings = NAV_SETTINGS.filter(visible);
  const link = (i: Item) => <NavLink key={i.to} to={i.to} className={({ isActive }) => `tile ${isActive ? "active" : ""}`} onClick={() => setOpen(false)} title={t(i.k)}><i.icon size={15} /><span>{t(i.k)}</span></NavLink>;
  const toggle = () => { setCollapsed((c) => { try { localStorage.setItem("wo_side", c ? "0" : "1"); } catch { /* ignore */ } return !c; }); };
  return (
    <div className="shell">
      <aside className={`side ${collapsed ? "collapsed" : ""} ${open ? "open" : ""}`}>
        <button className="collapse" onClick={toggle} title={collapsed ? t("expand") : t("collapse")}>{collapsed ? <PanelLeftOpen size={14} /> : <PanelLeftClose size={14} />}</button>
        <div className="brand"><Wordmark size={40} /></div>
        <nav className="nav-scroll">
          {NAV.map((g) => { const items = g.items.filter(visible); if (!items.length) return null; const shut = !!g.group && !!folded[g.id] && !collapsed;
            return <div key={g.id} className="nav-sec">
              {g.group && <button className="group" onClick={() => fold(g.id)} title={t(g.group)}>{shut ? <ChevronRight size={11} /> : <ChevronDown size={11} />}<span>{t(g.group)}</span></button>}
              {!shut && items.map(link)}
            </div>; })}
        </nav>
        {settings.length > 0 && <div className="nav-sec nav-bottom">
          <button className="group" onClick={() => fold("settings")} title={t("nav_group_settings")}>{folded.settings && !collapsed ? <ChevronRight size={11} /> : <ChevronDown size={11} />}<span>{t("nav_group_settings")}</span></button>
          {!(folded.settings && !collapsed) && settings.map(link)}
        </div>}
        <VersionTag />
        <div className="user"><div className="av">{(user?.name || user?.email || "?")[0].toUpperCase()}</div><div className="info"><b>{user?.name || user?.email}</b><span className="small muted">{user?.role}</span></div>
          <Btn kind="ghost" sm onClick={logout} title={t("signout")}><LogOut size={14} /></Btn></div>
      </aside>
      <main className="main">
        <div className="topbar">
          <Btn kind="ghost menu-btn" sm onClick={() => setOpen((o) => !o)}><Menu size={18} /></Btn>
          <div className="grow" />
          <ScopeBadge />
          {(can("page.assist") || can("page.llm")) && <TokenMeter />}
          {can("page.ops") && <NotifyBell />}
          <LangTheme />
        </div>
        <Outlet />
      </main>
      {can("page.assist") && <ChatWidget />}
      {pw && <ChangePassword onDone={async () => { setPw(false); await reload(); }} />}
    </div>
  );
}

/** A team member sees only the team's hosts: say so in the top bar, so nobody mistakes the view for the whole estate. */
function ScopeBadge() {
  const { t } = useT();
  const q = useQuery({ queryKey: ["scope-mine"], queryFn: () => get("/teams/mine"), staleTime: 60000 });
  if (!q.data || q.data.all) return null;
  return <span className="badge blue" style={{ textTransform: "none" }} title={t("tm_scope_hint")}>👥 {q.data.teams.join(", ") || t("tm_scope_none")} · {q.data.hosts ?? 0} {t("tm_hosts").toLowerCase()}</span>;
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
