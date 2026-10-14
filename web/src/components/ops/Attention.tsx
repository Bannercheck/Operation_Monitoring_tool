import { Link } from "react-router-dom";
import { AlertOctagon, AlertTriangle, BellOff, CheckCircle2, Sparkles, Ticket, TrendingUp } from "lucide-react";
import { useT } from "../../i18n";
import { fmtN, fmtPct } from "./util";

export type Item = { level: 3 | 2 | 1; icon: any; title: string; sub?: string; act?: { label: string; run?: () => void; to?: string } };

/** The few things worth a look right now, worst first; when there are none, the card says so plainly. */
export function buildAttention(t: any, lang: string, fleet: any, groups: any[], anomalies: any[], tickets: any[], go: { pool: (p: string) => void; hosts: (h: string[]) => void; group: (g: any) => void }): Item[] {
  const out: Item[] = [];
  const allHosts = (fleet?.pools ?? []).flatMap((p: any) => p.hosts);
  const silent = allHosts.filter((h: any) => h.silent);
  if (silent.length) out.push({ level: 3, icon: BellOff, title: t("at_silent", { n: silent.length }), sub: silent.slice(0, 4).map((h: any) => h.host).join(", ") + (silent.length > 4 ? " …" : ""), act: { label: t("at_show"), run: () => go.hosts(silent.map((h: any) => h.host)) } });
  for (const p of (fleet?.pools ?? []).filter((p: any) => p.events >= 50 && p.error_rate >= 0.05).slice(0, 3))
    out.push({ level: 3, icon: AlertOctagon, title: t("at_pool_err", { p: p.pool, r: fmtPct(p.error_rate, 1) }), sub: t("at_pool_err_sub", { n: fmtN(p.errors, lang), w: p.worst ?? "-" }), act: { label: t("at_focus"), run: () => go.pool(p.pool) } });
  for (const g of groups.filter((g) => g.new && (g.severity === "ERROR" || g.severity === "CRITICAL")).slice(0, 2))
    out.push({ level: 2, icon: Sparkles, title: t("at_new_err", { s: g.service }), sub: g.template.slice(0, 110), act: { label: t("at_open"), run: () => go.group(g) } });
  for (const g of groups.filter((g) => !g.new && (g.severity === "ERROR" || g.severity === "CRITICAL") && rising(g.trend)).slice(0, 2))
    out.push({ level: 2, icon: TrendingUp, title: t("at_rising", { s: g.service, n: g.host_count }), sub: g.template.slice(0, 110), act: { label: t("at_open"), run: () => go.group(g) } });
  for (const a of anomalies.slice(0, 3)) out.push({ level: a.severity === "critical" ? 3 : 2, icon: AlertTriangle, title: a.title, sub: a.detail?.slice(0, 110), act: { label: t("at_anomaly"), to: "/anomalies" } });
  for (const p of (fleet?.pools ?? []).filter((p: any) => p.outliers?.length).slice(0, 2))
    out.push({ level: 1, icon: AlertTriangle, title: t("at_outlier", { p: p.pool, n: p.outliers.length }), sub: p.outliers.slice(0, 4).join(", "), act: { label: t("at_show"), run: () => go.hosts(p.outliers) } });
  for (const k of tickets.slice(0, 2)) out.push({ level: 1, icon: Ticket, title: `${k.id} · ${k.title}`, sub: `${k.priority} · ${k.status}`, act: { label: "ITSM", to: "/itsm" } });
  return out.sort((a, b) => b.level - a.level).slice(0, 8);
}

function rising(tr: number[] = []): boolean {
  if (tr.length < 6) return false;
  const k = Math.floor(tr.length / 3); const recent = tr.slice(-k).reduce((a, b) => a + b, 0) / k; const before = tr.slice(0, -k).reduce((a, b) => a + b, 0) / (tr.length - k);
  return recent >= 10 && recent >= 3 * Math.max(before, 0.5);
}

export function Attention({ items }: { items: Item[] }) {
  const { t } = useT();
  if (!items.length) return <div className="at-ok"><CheckCircle2 size={28} color="var(--green)" /><div><b>{t("at_none")}</b><div className="muted small">{t("at_none_sub")}</div></div></div>;
  return <div className="at">{items.map((it, i) => <div key={i} className={`at-i l${it.level}`}>
    <it.icon size={17} className="at-ic" />
    <div className="at-b"><div className="at-t">{it.title}</div>{it.sub && <div className="at-s">{it.sub}</div>}</div>
    {it.act && (it.act.to ? <Link className="btn sm" to={it.act.to}>{it.act.label}</Link> : <button className="btn sm" onClick={it.act.run}>{it.act.label}</button>)}
  </div>)}</div>;
}
