import { NavLink, useLocation } from "react-router-dom";
import { AlertTriangle, BellRing, BookOpen, Bot, Brain, Bug, ClipboardList, Database, FileText, Gauge, GitBranch, Layers, LayoutDashboard, LayoutGrid, MessageSquare, Network, Plug, Radar, Route, ScrollText, Server, Settings, Settings2, ShieldAlert, Siren, Smartphone, Sparkles, Ticket, UserCog, Users, Workflow } from "lucide-react";
import { useAuth } from "../auth";
import { useT } from "../i18n";

/** One flat, SigNoz-style menu. Each sidebar item is a hub of one or more pages; every page keeps its own route, so deep links keep
 *  working. A hub with several visible pages shows them as a slim tab row in the top bar (HubTabs). A tab shows with its permission
 *  (`any`: one of these is enough; `unless`: hidden for users who reach the same page another way, System holds Alerts and README).
 *  A hub shows when one of its tabs does, links to its first visible tab and is active on any of its routes and their detail pages. */
export type NavTab = { to: string; k: string; icon: any; perm: string; any?: string[]; unless?: string };
export type Hub = { id: string; k: string; icon: any; tabs: NavTab[] };

export const HUBS: Hub[] = [
  { id: "dash", k: "nav_dash", icon: LayoutGrid, tabs: [{ to: "/ops", k: "nav_home", icon: LayoutDashboard, perm: "page.ops" }] },
  { id: "services", k: "nav_services", icon: Layers, tabs: [
    { to: "/services", k: "nav_services", icon: Layers, perm: "page.apm" }, { to: "/flows", k: "nav_flows", icon: Route, perm: "page.apm" }] },
  { id: "traces", k: "nav_traces", icon: GitBranch, tabs: [{ to: "/traces", k: "nav_traces", icon: GitBranch, perm: "page.apm" }] },
  { id: "logs", k: "nav_logs", icon: ScrollText, tabs: [
    { to: "/logs", k: "lg_title", icon: ScrollText, perm: "page.logs" }, { to: "/datasets", k: "nav_data", icon: Database, perm: "page.data" }] },
  { id: "alerts", k: "nav_hub_alerts", icon: BellRing, tabs: [
    { to: "/anomalies", k: "nav_anom", icon: AlertTriangle, perm: "page.anom" }, { to: "/thresholds", k: "nav_thr", icon: Gauge, perm: "page.thr" },
    { to: "/alerts", k: "nav_tab_notify", icon: BellRing, perm: "sys.notify", any: ["sys.notify", "act.alerts"], unless: "sys.status" }] },
  { id: "respond", k: "nav_hub_respond", icon: Siren, tabs: [
    { to: "/actions", k: "nav_actions", icon: ClipboardList, perm: "page.actions" }, { to: "/playbook", k: "nav_pb", icon: BookOpen, perm: "page.pb" },
    { to: "/itsm", k: "nav_itsm", icon: Ticket, perm: "page.itsm" }] },
  { id: "exceptions", k: "nav_exc", icon: Bug, tabs: [{ to: "/exceptions", k: "nav_exc", icon: Bug, perm: "page.apm" }] },
  { id: "rum", k: "nav_rum", icon: Smartphone, tabs: [{ to: "/rum", k: "nav_rum", icon: Smartphone, perm: "page.rum" }] },
  { id: "map", k: "nav_map", icon: Network, tabs: [{ to: "/map", k: "nav_map", icon: Network, perm: "page.map" }] },
  { id: "integrations", k: "nav_integ", icon: Workflow, tabs: [{ to: "/integrations", k: "nav_integ", icon: Workflow, perm: "page.apm" }] },
  { id: "infra", k: "nav_hub_infra", icon: Server, tabs: [
    { to: "/inventory", k: "nav_inv", icon: Server, perm: "page.inv" }, { to: "/scan", k: "nav_scan", icon: ShieldAlert, perm: "page.scan" }] },
  { id: "intel", k: "nav_hub_intel", icon: Sparkles, tabs: [
    { to: "/knowledge", k: "nav_kb", icon: Brain, perm: "page.assist" }, { to: "/llm", k: "nav_llm", icon: Bot, perm: "page.llm" },
    { to: "/assist", k: "nav_assist", icon: MessageSquare, perm: "page.assist" }] },
];
/** Pinned to the bottom of the sidebar, as in SigNoz. */
export const SETTINGS_HUB: Hub = { id: "settings", k: "nav_hub_settings", icon: Settings, tabs: [
  { to: "/connections", k: "nav_conn", icon: Plug, perm: "page.conn" }, { to: "/sources", k: "nav_sources", icon: Radar, perm: "page.src" },
  { to: "/users", k: "nav_users", icon: Users, perm: "page.users" }, { to: "/team", k: "tmr_my_team", icon: UserCog, perm: "team.manage", unless: "page.teams" },
  { to: "/system", k: "nav_sys", icon: Settings2, perm: "sys.status" }, { to: "/readme", k: "nav_docs", icon: FileText, perm: "page.readme", unless: "sys.status" }] };

const onRoute = (path: string, to: string) => path === to || path.startsWith(to + "/");
export const inHub = (h: Hub, path: string) => h.tabs.some((x) => onRoute(path, x.to));

/** The hubs this user may see, each holding only its visible tabs. */
export function useNav(): { hubs: Hub[]; settings: Hub } {
  const { can } = useAuth();
  const show = (x: NavTab) => (x.any ? x.any.some(can) : can(x.perm)) && !(x.unless && can(x.unless));
  const pick = (h: Hub): Hub => ({ ...h, tabs: h.tabs.filter(show) });
  return { hubs: HUBS.map(pick).filter((h) => h.tabs.length > 0), settings: pick(SETTINGS_HUB) };
}

/** The tab row of the hub the current page belongs to; nothing when the hub has a single visible page. */
export function HubTabs() {
  const { t } = useT(); const { pathname } = useLocation(); const { hubs, settings } = useNav();
  const hub = [...hubs, settings].find((h) => inHub(h, pathname));
  if (!hub || hub.tabs.length < 2) return null;
  return <nav className="hubtabs" aria-label={t(hub.k)}>
    <span className="hub-name"><hub.icon size={15} />{t(hub.k)}</span>
    {hub.tabs.map((x) => <NavLink key={x.to} to={x.to} className={({ isActive }) => `hubtab ${isActive ? "on" : ""}`}><x.icon size={14} /><span>{t(x.k)}</span></NavLink>)}
  </nav>;
}
