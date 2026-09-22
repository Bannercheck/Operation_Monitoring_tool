import { useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { Activity, AlertTriangle, BellRing, BookOpen, Bot, Brain, ClipboardList, Database, FileText, LogOut, Menu, MessageSquare, Network, PanelLeftClose, PanelLeftOpen, Plug, Radar, Server, Settings2, Ticket, Users } from "lucide-react";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Btn, Modal, Field, Err, LangTheme } from "./ui";
import { NotifyBell } from "./NotifyBell";
import { ChatWidget } from "./ChatWidget";
import { Wordmark } from "./Logo";
import { post } from "../api";

const NAV = [
  { group: "nav_group_ops", items: [
    { to: "/ops", k: "nav_ops", icon: Activity, perm: "page.ops" }, { to: "/anomalies", k: "nav_anom", icon: AlertTriangle, perm: "page.ops" },
    { to: "/datasets", k: "nav_data", icon: Database, perm: "page.data" }, { to: "/map", k: "nav_map", icon: Network, perm: "page.map" }, { to: "/actions", k: "nav_actions", icon: ClipboardList, perm: "page.data" },
    { to: "/playbook", k: "nav_pb", icon: BookOpen, perm: "page.pb" }, { to: "/knowledge", k: "nav_kb", icon: Brain, perm: "page.assist" }, { to: "/itsm", k: "nav_itsm", icon: Ticket, perm: "page.itsm" } ] },
  { group: "nav_group_admin", items: [
    { to: "/connections", k: "nav_conn", icon: Plug, perm: "page.conn" }, { to: "/sources", k: "nav_sources", icon: Radar, perm: "page.src" }, { to: "/inventory", k: "nav_inv", icon: Server, perm: "page.inv" }, { to: "/llm", k: "nav_llm", icon: Bot, perm: "page.llm" },
    { to: "/users", k: "nav_users", icon: Users, perm: "page.users" }, { to: "/system", k: "nav_sys", icon: Settings2, perm: "sys.status" },
    { to: "/alerts", k: "nav_ntf", icon: BellRing, perm: "sys.notify", unless: "sys.status" }, { to: "/readme", k: "nav_readme", icon: FileText, perm: "page.readme", unless: "sys.status" } ] },
];

export default function Layout() {
  const { user, logout, can, reload } = useAuth();
  const { t } = useT();
  const [collapsed, setCollapsed] = useState(() => { try { return localStorage.getItem("wo_side") === "1"; } catch { return false; } });
  const [open, setOpen] = useState(false);
  const [pw, setPw] = useState(!!user?.must_change);
  const toggle = () => { setCollapsed((c) => { try { localStorage.setItem("wo_side", c ? "0" : "1"); } catch { /* ignore */ } return !c; }); };
  return (
    <div className="shell">
      <aside className={`side ${collapsed ? "collapsed" : ""} ${open ? "open" : ""}`}>
        <button className="collapse" onClick={toggle} title={collapsed ? t("expand") : t("collapse")}>{collapsed ? <PanelLeftOpen size={14} /> : <PanelLeftClose size={14} />}</button>
        <div className="brand"><Wordmark size={40} /></div>
        {NAV.map((g) => (
          <div key={g.group}><div className="group">{t(g.group)}</div>
            <div className="stack" style={{ gap: 6 }}>{g.items.filter((i: any) => can(i.perm) && !(i.unless && can(i.unless))).map((i) => (
              <NavLink key={i.to} to={i.to} className={({ isActive }) => `tile ${isActive ? "active" : ""}`} onClick={() => setOpen(false)} title={t(i.k)}><i.icon size={16} /><span>{t(i.k)}</span></NavLink>))}</div>
          </div>))}
        <div className="spacer" />
        <div className="user"><div className="av">{(user?.name || user?.email || "?")[0].toUpperCase()}</div><div className="info"><b>{user?.name || user?.email}</b><span className="small muted">{user?.role}</span></div>
          <Btn kind="ghost" sm onClick={logout} title={t("signout")}><LogOut size={14} /></Btn></div>
      </aside>
      <main className="main">
        <div className="topbar">
          <Btn kind="ghost menu-btn" sm onClick={() => setOpen((o) => !o)}><Menu size={18} /></Btn>
          <div className="grow" />
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
