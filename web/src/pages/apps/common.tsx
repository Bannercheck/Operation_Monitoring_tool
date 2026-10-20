import { useEffect, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { X } from "lucide-react";
import { del, get, post, put } from "../../api";
import { useT } from "../../i18n";
import { Btn, Confirm, Err, Field, Modal } from "../../components/ui";
import { fmtMs } from "../apm/common";

/** Shared bits of the applications pages (watchover.applications, /api/apps): status and criticality badges, number formats, the
 *  membership chip input and the create / edit dialog. */
export const LV: Record<string, string> = { ok: "ok", warn: "warn", crit: "crit", nodata: "none" };
export const CRITS = ["critical", "high", "normal", "low"] as const;
const CRIT_CLS: Record<string, string> = { critical: "red", high: "amber", normal: "blue", low: "" };
export const KINDS = ["hosts", "inv_apps", "pools", "services", "namespaces"] as const;

export function StatusBadge({ s }: { s?: string | null }) {
  const { t } = useT(); const k = s || "nodata";
  return <span className={`lvl lv-${LV[k] ?? "none"} ap-st`}><i />{t(`ap_st_${k}`)}</span>;
}
export function CritBadge({ v }: { v: string }) {
  const { t } = useT();
  return <span className={`badge ${CRIT_CLS[v] ?? ""}`} style={{ textTransform: "none" }}>{t(`ap_crit_${v}`)}</span>;
}
export const pctOf = (v?: number | null, d = 2) => (v == null ? "–" : `%${(v * 100).toFixed(d)}`);
export const num = (v?: number | null, lang = "tr") => (v == null ? "–" : v >= 100 || Number.isInteger(v) ? Math.round(v).toLocaleString(lang === "tr" ? "tr-TR" : "en-US") : v >= 10 ? v.toFixed(0) : v.toFixed(1));
export const ms = (v?: number | null) => fmtMs(v);
export function sloText(t: (k: string, kw?: any) => string, s: any): { main: string; sub: string; tone: string } {
  if (!s) return { main: "–", sub: "", tone: "" };
  const availOk = s.availability == null || s.target_availability == null || s.availability * 100 >= s.target_availability;
  const p95Bad = s.p95_ms != null && s.target_p95_ms != null && s.p95_ms > s.target_p95_ms;
  const main = s.state === "breach" && availOk && p95Bad ? `p95 ${fmtMs(s.p95_ms)}` : s.availability == null ? "–" : `%${(s.availability * 100).toFixed(2)}`;
  const tgt = [s.target_availability != null ? `%${s.target_availability}` : "", s.target_p95_ms != null ? `p95 ≤ ${fmtMs(s.target_p95_ms)}` : ""].filter(Boolean).join(" · ");
  const sub = `${t(`ap_slo_${s.state}`)}${tgt ? ` · ${t("ap_target", { v: tgt })}` : ""}${s.budget_left != null ? ` · ${t("ap_budget", { v: Math.round(s.budget_left * 100) })}` : ""}`;
  return { main, sub, tone: s.state === "breach" ? "crit" : s.state === "ok" ? "ok" : "" };
}
export const qlink = (q: string, extra = "") => `/logs?q=${encodeURIComponent(q)}${extra}`;

/** True on a phone-width screen: the wide tables drop their secondary columns instead of scrolling sideways. */
export function useNarrow(px = 640): boolean {
  const q = `(max-width: ${px}px)`;
  const [n, setN] = useState(() => typeof window !== "undefined" && window.matchMedia(q).matches);
  useEffect(() => { const m = window.matchMedia(q); const f = () => setN(m.matches); m.addEventListener("change", f); return () => m.removeEventListener("change", f); }, [q]);
  return n;
}

/** Values as removable chips, an input with suggestions (datalist): Enter or comma adds, Backspace on empty removes the last. */
export function ChipInput({ values, onChange, options = [], placeholder, id }: { values: string[]; onChange: (v: string[]) => void; options?: string[]; placeholder?: string; id: string }) {
  const [draft, setDraft] = useState("");
  const add = (raw: string) => { const vs = raw.split(",").map((x) => x.trim()).filter(Boolean); if (vs.length) onChange([...new Set([...values, ...vs])]); setDraft(""); };
  return <div className="ap-chips">
    {values.map((v) => <span key={v} className="chip on ap-chip"><span className="mono">{v}</span><button type="button" aria-label="✕" onClick={() => onChange(values.filter((x) => x !== v))}><X size={12} /></button></span>)}
    <input className="ap-chip-in" list={`dl-${id}`} value={draft} placeholder={placeholder} onChange={(e) => { const v = e.target.value; if (v.endsWith(",")) add(v); else setDraft(v); }}
      onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); add(draft); } else if (e.key === "Backspace" && !draft && values.length) onChange(values.slice(0, -1)); }}
      onBlur={() => draft.trim() && add(draft)} />
    <datalist id={`dl-${id}`}>{options.filter((o) => !values.includes(o)).slice(0, 400).map((o) => <option key={o} value={o} />)}</datalist>
  </div>;
}

export function Toggles({ items, on, onChange }: { items: { k: string; label: ReactNode }[]; on: string[]; onChange: (v: string[]) => void }) {
  const { t } = useT();
  if (!items.length) return <span className="dim small">{t("ap_none_avail")}</span>;
  return <div className="row" style={{ gap: 6 }}>{items.map((x) => <button type="button" key={x.k} className={`chip ${on.includes(x.k) ? "on" : ""}`}
    onClick={() => onChange(on.includes(x.k) ? on.filter((y) => y !== x.k) : [...on, x.k])}>{x.label}</button>)}</div>;
}

/** Create or edit an application: name, description, owning team, criticality, tags, SLO targets and the membership rules. */
export function AppEditor({ app, onClose, onSaved }: { app?: any; onClose: () => void; onSaved?: (a: any) => void }) {
  const { t } = useT(); const qc = useQueryClient(); const nav = useNavigate();
  const cat = useQuery({ queryKey: ["apps-catalog"], queryFn: () => get("/apps/catalog"), staleTime: 30000 });
  const c = cat.data ?? {};
  const [f, setF] = useState<any>(() => ({ name: app?.name ?? "", description: app?.description ?? "", criticality: app?.criticality ?? "normal",
    tags: (app?.tags ?? []).join(", "), slo_availability: app?.slo_availability ?? "", slo_p95_ms: app?.slo_p95_ms ?? "",
    team_id: app ? (app.team_id ?? "") : undefined, members: { hosts: [], inv_apps: [], pools: [], services: [], namespaces: [], rum_apps: [], flows: [], ...(app?.members ?? {}) } }));
  const [err, setErr] = useState<any>(null); const [busy, setBusy] = useState(false);
  const teams: any[] = c.teams ?? [];
  const team = f.team_id !== undefined ? f.team_id : (c.install ? "" : teams[0]?.id ?? "");
  const setM = (k: string, v: any) => setF({ ...f, members: { ...f.members, [k]: v } });
  const save = async () => {
    setErr(null); setBusy(true);
    const body = { name: f.name.trim(), description: f.description, criticality: f.criticality, tags: f.tags.split(",").map((x: string) => x.trim()).filter(Boolean),
      slo_availability: f.slo_availability === "" ? null : Number(f.slo_availability), slo_p95_ms: f.slo_p95_ms === "" ? null : Number(f.slo_p95_ms),
      team_id: team === "" ? null : Number(team), members: f.members };
    try {
      const r = app ? await put(`/apps/${app.id}`, body) : await post("/apps", body);
      qc.invalidateQueries({ queryKey: ["apps"] }); qc.invalidateQueries({ queryKey: ["app"] }); onSaved?.(r); onClose();
    } catch (e) { setErr(e); } finally { setBusy(false); }
  };
  const remove = async () => { try { const r: any = await del(`/apps/${app.id}`); qc.invalidateQueries({ queryKey: ["apps"] }); onClose(); nav("/apps", { state: { msg: t("ap_deleted", { n: r?.policies_disabled ?? 0 }) } }); } catch (e) { setErr(e); } };
  const opts: Record<string, string[]> = { hosts: c.hosts ?? [], inv_apps: c.inv_apps ?? [], pools: c.pools ?? [], services: c.services ?? [], namespaces: c.namespaces ?? [] };
  return <Modal wide title={app ? t("ap_ed_edit") : t("ap_ed_new")} onClose={onClose}>
    <div className="stack">
      <div className="ap-form">
        <Field label={t("ap_name")}><input className="input" value={f.name} maxLength={80} onChange={(e) => setF({ ...f, name: e.target.value })} autoFocus /></Field>
        <Field label={t("ap_team")}><select className="input" value={team} onChange={(e) => setF({ ...f, team_id: e.target.value })} disabled={!c.install && teams.length < 2 && !!app}>
          {(c.install || (app && app.team_id == null)) && <option value="">{t("ap_install")}</option>}
          {teams.map((x: any) => <option key={x.id} value={x.id}>{x.name}</option>)}
          {app?.team_id != null && !teams.some((x: any) => x.id === app.team_id) && <option value={app.team_id}>{app.team}</option>}</select></Field>
        <Field label={t("ap_criticality")}><select className="input" value={f.criticality} onChange={(e) => setF({ ...f, criticality: e.target.value })}>
          {CRITS.map((k) => <option key={k} value={k}>{t(`ap_crit_${k}`)}</option>)}</select></Field>
        <Field label={t("ap_tags")}><input className="input" value={f.tags} onChange={(e) => setF({ ...f, tags: e.target.value })} /></Field>
        <Field label={t("ap_slo_avail")}><input className="input" type="number" step="0.01" min="0" max="100" value={f.slo_availability} placeholder="99.5" onChange={(e) => setF({ ...f, slo_availability: e.target.value })} /></Field>
        <Field label={t("ap_slo_p95")}><input className="input" type="number" min="1" value={f.slo_p95_ms} placeholder="800" onChange={(e) => setF({ ...f, slo_p95_ms: e.target.value })} /></Field>
      </div>
      <Field label={t("ap_desc")}><textarea className="input" rows={2} maxLength={500} value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
      <div><h3 style={{ margin: "4px 0" }}>{t("ap_members")}</h3><div className="dim small">{t("ap_members_hint")}</div></div>
      <div className="ap-form">
        {KINDS.map((k) => <Field key={k} label={t(`ap_m_${k}`)}><ChipInput id={`${k}`} values={f.members[k] ?? []} onChange={(v) => setM(k, v)} options={opts[k]} placeholder={t("ap_add_value")} /></Field>)}
      </div>
      <Field label={t("ap_m_rum_apps")}><Toggles items={(c.rum_apps ?? []).map((x: string) => ({ k: x, label: x }))} on={f.members.rum_apps ?? []} onChange={(v) => setM("rum_apps", v)} /></Field>
      <Field label={t("ap_m_flows")}><Toggles items={(c.flows ?? []).map((x: any) => ({ k: String(x.id), label: x.name }))} on={(f.members.flows ?? []).map(String)}
        onChange={(v) => setM("flows", v.map(Number))} /></Field>
      <Err e={err} />
      <div className="row between">
        <div>{app?.editable && <Confirm onConfirm={remove}>{t("ap_delete")}</Confirm>}</div>
        <div className="row"><Btn kind="ghost" onClick={onClose}>{t("cancel")}</Btn><Btn kind="primary" disabled={busy || !f.name.trim()} onClick={save}>{t("save")}</Btn></div>
      </div>
    </div>
  </Modal>;
}
