import { useEffect, useRef, type ReactNode } from "react";
import { Lock } from "lucide-react";
import { useT } from "../i18n";

/** The permission catalogue as a matrix: rows grouped like the sidebar (Ana sayfa, APM, Sinyaller, Müdahale, Zekâ, Güvenlik,
 *  Ayarlar), one column per role. Ticking an action ticks the view it needs; unticking a view unticks the actions that need it
 *  (the server applies the same closure). Used by the team editor (several team roles) and by Users › Kurulum rolleri (one
 *  installation role). */
export type CatItem = { key: string; kind: "view" | "action" | "manage"; team: boolean; requires: string | null; tr: string; en: string };
export type Catalogue = { sections: { id: string; tr: string; en: string; items: CatItem[] }[] };
export type PermCol = { id: string | number; label: ReactNode; sub?: ReactNode; perms: string[]; locked?: boolean; lockTip?: string; dirty?: boolean; menu?: ReactNode };

function TriBox({ state, disabled, onChange, title }: { state: "on" | "off" | "some"; disabled?: boolean; onChange: (on: boolean) => void; title?: string }) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { if (ref.current) ref.current.indeterminate = state === "some"; }, [state]);
  return <input ref={ref} type="checkbox" checked={state === "on"} disabled={disabled} title={title} onChange={(e) => onChange(e.target.checked)} />;
}

/** Apply one tick to a permission set with the requires-closure in both directions. */
export function tick(cat: Catalogue, perms: string[], key: string, on: boolean): string[] {
  const items = cat.sections.flatMap((s) => s.items);
  const req = Object.fromEntries(items.filter((i) => i.requires).map((i) => [i.key, i.requires as string]));
  const p = new Set(perms);
  if (on) { p.add(key); if (req[key]) p.add(req[key]); }
  else { p.delete(key); for (const [a, v] of Object.entries(req)) if (v === key) p.delete(a); }
  return [...p];
}

export function PermGrid({ cat, cols, team, onChange, grantable, hide = [] }: { cat: Catalogue; cols: PermCol[]; team: boolean; onChange: (colId: string | number, perms: string[]) => void;
  grantable?: string[]; hide?: string[] }) {
  const { t, lang } = useT();
  const label = (i: { tr: string; en: string }) => (lang === "tr" ? i.tr : i.en);
  const can = (i: CatItem, c: PermCol) => !c.locked && (!grantable || grantable.includes(i.key) || c.perms.includes(i.key));
  return <div className="table-wrap perm-wrap"><table className="table perm-matrix">
    <thead><tr><th className="perm-first" />{cols.map((c) => <th key={c.id} className={`perm-col ${c.dirty ? "dirty" : ""}`}>
      <div className="perm-colh"><span className="perm-coll" title={c.locked ? c.lockTip : undefined}>{c.locked && <Lock size={11} />} {c.label}</span>{c.menu}</div>
      {c.sub && <div className="dim small perm-sub">{c.sub}</div>}</th>)}</tr></thead>
    <tbody>{cat.sections.map((s) => {
      const items = s.items.filter((i) => !hide.includes(i.key) && (team ? i.team : true));
      const installOnly = team ? s.items.filter((i) => !i.team) : [];
      if (!items.length && !installOnly.length) return null;
      return [<tr key={s.id} className="perm-sec"><td className="perm-first">{label(s)}</td>{cols.map((c) => {
          const n = items.filter((i) => c.perms.includes(i.key)).length;
          const free = items.filter((i) => can(i, c));
          return <td key={c.id} className="perm-cell"><label className="perm-all" title={t("tmr_all")}>
            <TriBox state={n === 0 ? "off" : n === items.length ? "on" : "some"} disabled={!free.length}
              onChange={(on) => { let p = c.perms; for (const i of free) p = tick(cat, p, i.key, on); onChange(c.id, p); }} />
            <span className="dim">{n}/{items.length}</span></label></td>; })}</tr>,
        ...items.map((i) => <tr key={i.key}><td className="perm-first"><span>{label(i)}</span> <span className="perm-kind">{t(`tmr_kind_${i.kind}`)}</span></td>
          {cols.map((c) => { const ok = can(i, c);
            return <td key={c.id} className="perm-cell"><input type="checkbox" checked={c.perms.includes(i.key)} disabled={!ok}
              title={!ok ? (c.locked ? c.lockTip : t("tmr_no_grant")) : undefined} onChange={(e) => onChange(c.id, tick(cat, c.perms, i.key, e.target.checked))} /></td>; })}</tr>),
        ...(installOnly.length ? [<tr key={`${s.id}-io`} className="perm-io"><td className="perm-first dim small" colSpan={cols.length + 1}>
          {t("tmr_install_only", { list: installOnly.map(label).join(", ") })}</td></tr>] : [])];
    })}</tbody></table></div>;
}
