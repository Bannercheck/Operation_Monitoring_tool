import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderOpen, Pencil, Trash2 } from "lucide-react";
import { useT } from "../../i18n";
import { Btn, Confirm, Field, Modal } from "../ui";
import { TeamPick } from "../TeamPick";
import { rangeLabel, type Range } from "../ops/util";
import { errText, lg, toErr } from "./lib";

/** Save the current search (query, optionally the range, columns, grouping; optionally shared: a shared search shares the query,
 *  never data, each viewer runs it under their own access). */
export function SaveDialog({ q, range, cols, group, onClose, onSaved }: { q: string; range: Range; cols: string[]; group: boolean; onClose: () => void; onSaved: (name: string) => void }) {
  const { t, lang } = useT(); const qc = useQueryClient();
  const [name, setName] = useState(""); const [withRange, setWithRange] = useState(false); const [shared, setShared] = useState(false);
  const save = useMutation({ mutationFn: () => lg("/saved", {}, { method: "POST", body: { name: name.trim(), query: q, columns: cols, group_similar: group, shared,
    range: !withRange ? "" : "last" in range ? `last=${range.last}` : `start=${range.start}&end=${range.end}` } }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["lg-saved"] }); onSaved(name.trim()); } });
  const e = save.error ? toErr(save.error) : null;
  return <Modal title={t("lg_save_title")} onClose={onClose}>
    <form className="stack" onSubmit={(ev) => { ev.preventDefault(); if (name.trim()) save.mutate(); }}>
      <Field label={t("lg_save_name")}><input className="input" autoFocus value={name} onChange={(ev) => setName(ev.target.value)} maxLength={120} required /></Field>
      <div className="dim small mono lg-qpreview">{q || t("lg_all_lines")}</div>
      <label className="check"><input type="checkbox" checked={withRange} onChange={(ev) => setWithRange(ev.target.checked)} /><span>{t("lg_save_range")} <span className="dim">({rangeLabel(range, lang)})</span></span></label>
      <label className="check"><input type="checkbox" checked={shared} onChange={(ev) => setShared(ev.target.checked)} /><span>{t("lg_save_share")}</span></label>
      {e && <div className="err">{e.code === "duplicate_name" ? t("lg_dup_name") : errText(e, lang)}</div>}
      <div className="row"><Btn kind="primary" type="submit" disabled={!name.trim() || save.isPending}>{t("lg_save")}</Btn><Btn type="button" onClick={onClose}>{t("cancel")}</Btn></div>
    </form>
  </Modal>;
}

/** the saved-searches menu: own searches first, then the shared ones with their owner; open, rename, delete (own) */
export function SavedMenu({ onOpen }: { onOpen: (s: any) => void }) {
  const { t } = useT(); const qc = useQueryClient(); const [open, setOpen] = useState(false); const [edit, setEdit] = useState<number | null>(null); const [nm, setNm] = useState("");
  const box = useRef<HTMLDivElement>(null);
  const list = useQuery({ queryKey: ["lg-saved"], queryFn: () => lg("/saved"), enabled: open, staleTime: 15000 });
  useEffect(() => { if (!open) return; const h = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); }; document.addEventListener("mousedown", h); return () => document.removeEventListener("mousedown", h); }, [open]);
  const inv = () => qc.invalidateQueries({ queryKey: ["lg-saved"] });
  const items: any[] = (list.data?.items ?? []).slice().sort((a: any, b: any) => Number(b.mine) - Number(a.mine) || a.name.localeCompare(b.name));
  return <div className="lg-saved" ref={box}>
    <button className="btn" onClick={() => setOpen(!open)} title={t("lg_saved")} aria-label={t("lg_saved")}><FolderOpen size={15} /><span className="lg-bl">{t("lg_saved")}</span></button>
    {open && <div className="lg-savedpop card">
      {list.isLoading ? <div className="dim small" style={{ padding: 8 }}>{t("loading")}</div> : !items.length ? <div className="dim small" style={{ padding: 8 }}>{t("lg_saved_none")}</div> :
        items.map((s) => <div key={s.id} className="lg-si">
          {edit === s.id ? <form className="row" style={{ flex: 1, gap: 6 }} onSubmit={async (e) => { e.preventDefault(); try { await lg(`/saved/${s.id}`, {}, { method: "PUT", body: { name: nm.trim() } }); setEdit(null); inv(); } catch { /* duplicate: keep editing */ } }}>
            <input className="input sm" value={nm} onChange={(e) => setNm(e.target.value)} autoFocus style={{ flex: 1 }} /><Btn kind="primary" sm type="submit">{t("save")}</Btn></form> :
            <button className="lg-si-b" onClick={() => { setOpen(false); onOpen(s); }}>
              <b>{s.name}</b><span className="mono dim small lg-si-q">{s.query || t("lg_all_lines")}</span>
              {!s.mine && <span className="dim small">{t("lg_shared_by", { o: s.owner })}</span>}{s.mine && s.shared && <span className="dim small">{t("lg_shared")}</span>}</button>}
          {s.mine && edit !== s.id && <span className="row" style={{ gap: 2 }}><button className="icon-btn" title={t("lg_rename")} onClick={() => { setEdit(s.id); setNm(s.name); }}><Pencil size={12} /></button>
            <Confirm onConfirm={async () => { await lg(`/saved/${s.id}`, {}, { method: "DELETE" }); inv(); }}><Trash2 size={12} /></Confirm></span>}
        </div>)}
    </div>}
  </div>;
}

/** Alert from this query: "matching lines in the last N minutes ≥ / < threshold", previewed on the last 24 hours as the numbers
 *  change; channels, recipients, cooldown and severity as on the Alerts page. */
export function AlertDialog({ q, onClose, onSaved }: { q: string; onClose: () => void; onSaved: () => void }) {
  const { t, lang } = useT();
  const [f, setF] = useState({ name: "", window_min: 5, op: ">=", threshold: 10, severity: "high", targets: "", cooldown_min: 30 });
  const [ch, setCh] = useState<Record<string, boolean>>({ email: true, sms: false, webhook: false }); const [team, setTeam] = useState<number | null>(null);
  const [pv, setPv] = useState(f);
  useEffect(() => { const h = setTimeout(() => setPv(f), 400); return () => clearTimeout(h); }, [f]);
  const prev = useQuery({ queryKey: ["lg-alert-pv", q, pv.window_min, pv.op, pv.threshold], queryFn: () => lg("/alerts/preview", { q, window_min: pv.window_min, op: pv.op, threshold: pv.threshold, last: 1440 }),
    enabled: !!q.trim() && pv.window_min >= 1 && pv.window_min <= 60, retry: false, staleTime: 30000 });
  const save = useMutation({ mutationFn: () => lg("/alerts", {}, { method: "POST", body: { ...f, name: f.name.trim(), query: q, threshold: Number(f.threshold), window_min: Number(f.window_min), cooldown_min: Number(f.cooldown_min),
    channels: Object.keys(ch).filter((k) => ch[k]).join(",") || "email", team_id: team } }), onSuccess: onSaved });
  const e = save.error ? toErr(save.error) : null;
  const set = (k: string, v: any) => setF((x) => ({ ...x, [k]: v }));
  return <Modal title={t("lg_alert_title")} onClose={onClose}>
    <form className="stack" onSubmit={(ev) => { ev.preventDefault(); if (f.name.trim()) save.mutate(); }}>
      <div className="dim small mono lg-qpreview">{q || t("lg_all_lines")}</div>
      <Field label={t("lg_save_name")}><input className="input" autoFocus value={f.name} onChange={(ev) => set("name", ev.target.value)} maxLength={120} required /></Field>
      <div className="lg-cond">
        <span>{t("lg_alert_last")}</span><input className="input sm" type="number" min={1} max={60} value={f.window_min} onChange={(ev) => set("window_min", Number(ev.target.value))} style={{ width: 64 }} />
        <span>{t("lg_alert_minutes")}</span>
        <select className="input sm" value={f.op} onChange={(ev) => set("op", ev.target.value)} style={{ width: 72 }}><option value=">=">≥</option><option value="<">&lt;</option></select>
        <input className="input sm" type="number" min={0} value={f.threshold} onChange={(ev) => set("threshold", Number(ev.target.value))} style={{ width: 80 }} />
      </div>
      <div className="dim small">{!q.trim() ? t("lg_alert_need_q") : prev.isLoading ? t("loading") : prev.error ? errText(toErr(prev.error), lang) :
        prev.data ? <>{t("lg_alert_preview", { n: prev.data.would_fire })}{prev.data.exact === false ? ` (${t("lg_estimate")})` : ""}</> : null}</div>
      <div className="row" style={{ alignItems: "flex-end" }}>
        <Field label={t("severity")}><select className="input" value={f.severity} onChange={(ev) => set("severity", ev.target.value)} style={{ width: 140 }}>{["critical", "high", "medium", "low"].map((s) => <option key={s} value={s}>{t(`lg_sev_${s}`)}</option>)}</select></Field>
        <Field label={t("ntf_cooldown")}><input className="input" type="number" min={0} value={f.cooldown_min} onChange={(ev) => set("cooldown_min", Number(ev.target.value))} style={{ width: 110 }} /></Field>
        <TeamPick perm="act.logalert" value={team} onChange={setTeam} style={{ width: 200 }} />
      </div>
      <Field label={t("ntf_channels")}><div className="row">{[["email", t("lg_ch_email")], ["sms", "SMS"], ["webhook", "Webhook"]].map(([k, l]) => <label key={k} className="check"><input type="checkbox" checked={!!ch[k]} onChange={(ev) => setCh({ ...ch, [k]: ev.target.checked })} /><span>{l}</span></label>)}</div></Field>
      <Field label={t("ntf_targets")} hint={t("lg_targets_hint")}><input className="input" value={f.targets} onChange={(ev) => set("targets", ev.target.value)} placeholder="ops, @dba" /></Field>
      {e && <div className="err">{errText(e, lang)}</div>}
      <div className="row"><Btn kind="primary" type="submit" disabled={!f.name.trim() || !q.trim() || save.isPending}>{t("lg_save")}</Btn><Btn type="button" onClick={onClose}>{t("cancel")}</Btn>
        <span className="grow" /><Link className="small" to="/alerts">{t("lg_alert_rules")} →</Link></div>
    </form>
  </Modal>;
}
