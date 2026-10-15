import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { get, post } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Btn } from "./ui";

type TFn = (k: string, kw?: Record<string, any>) => string;

export const sigLabel = (t: TFn, s: string) => { const k = `an_sig_${s}`; const v = t(k); return v === k ? s : v; };
export const signedPct = (n: any) => (n == null ? "" : `${n > 0 ? "+" : ""}${n}%`);
const reasonText = (t: TFn, r: any) => t(r.code === "weekly" ? `an_lr_weekly_${r.need}` : `an_lr_${r.code}`, r);
const why = (t: TFn, s: any) => (s.reasons ?? []).map((r: any) => reasonText(t, r)).join(" · ");

/** one signal in a few words: why it is still learning, or done (a level shift that came with errors / alerts says so) */
export function sigStatus(t: TFn, s: any): string {
  if (!s.learning) return s.shift?.incident ? t("an_lr_incident", { pct: signedPct(s.shift.pct) }) : t("an_lr_ok");
  if (s.note?.code === "shift") return t("an_lr_shift", { pct: signedPct(s.note.pct) });
  if (s.note?.code === "manual") return t("an_lr_manual");
  const r = s.reasons?.[0];
  return r ? reasonText(t, r) : `%${Math.round(s.pct)}`;
}

/** "✓ CPU, bellek tamam · hata oranı haftalık döngüyü bekliyor": learned signals in one chip, then each one still learning */
export function LearnLine({ h }: { h: any }) {
  const { t } = useT();
  const sigs: any[] = h.signals ?? [];
  const done = sigs.filter((s) => !s.learning && !s.shift?.incident);
  const rest = sigs.filter((s) => s.learning || s.shift?.incident);
  return <span className="lrn-line small">
    {!!done.length && <span className="lrn-ok">✓ {done.map((s) => sigLabel(t, s.signal)).join(", ")} {t("an_lr_ok")}</span>}
    {rest.map((s) => <span key={s.signal} className={`lrn-sig${s.learning ? "" : " warn"}`} title={why(t, s)}>{sigLabel(t, s.signal)} {sigStatus(t, s)}</span>)}
  </span>;
}

/** every signal with its bar and the operator's two actions: end its learning / send it back to learning */
export function SignalList({ h, editable }: { h: any; editable: boolean }) {
  const { t } = useT(); const qc = useQueryClient();
  const act = useMutation({ mutationFn: (p: { path: string; signal: string }) => post(`/anomalies/learning/${p.path}`, { host: h.host, env: h.env, signal: p.signal }),
    onSuccess: () => qc.invalidateQueries() });
  return <div className="lrn-list">{(h.signals ?? []).map((s: any) => <div key={s.signal} className="lrn-item">
    <b>{sigLabel(t, s.signal)}</b>
    <div className="durbar"><div style={{ width: `${s.pct}%`, background: s.learning ? "var(--accent)" : "#2dd4bf" }} /></div>
    <span className="small" title={why(t, s)}>{sigStatus(t, s)}{!s.learning && ["cap", "kept", "history", "operator"].includes(s.note?.code) ? ` · ${t("an_lr_note_" + s.note.code, { d: s.note.days })}` : ""}</span>
    {editable && (s.learning ? <Btn sm disabled={act.isPending} onClick={() => act.mutate({ path: "graduate", signal: s.signal })}>{t("an_lr_end_sig")}</Btn>
      : <Btn sm disabled={act.isPending} onClick={() => act.mutate({ path: "relearn", signal: s.signal })}>{t("an_lr_relearn")}</Btn>)}
  </div>)}</div>;
}

/** server detail: one compact line (nothing for a host the tracker has not seen); a click opens the per-signal list */
export function HostLearning({ host, env }: { host: string; env?: string }) {
  const { t } = useT(); const { can } = useAuth(); const [open, setOpen] = useState(false);
  const q = useQuery({ queryKey: ["host-learning", host, env ?? ""], enabled: can("page.anom"), refetchInterval: 60000,
    queryFn: () => get(`/anomalies/learning?host=${encodeURIComponent(host)}&env=${encodeURIComponent(env ?? "")}`) });
  const h = q.data;
  if (!h?.known) return null;
  const sigs: any[] = h.signals ?? [];
  const calm = !h.learning && !sigs.some((s) => s.shift || s.note?.code === "shift");
  return <div className="lrn-host">
    <button className="lrn-head" onClick={() => setOpen((o) => !o)} disabled={!sigs.length}>
      <span>🎓</span>{calm ? <span className="dim small">{sigs.length ? t("an_lr_host_ok", { n: sigs.length }) : t("an_learn_done")}</span> : <>
        <div className="durbar" style={{ width: 110 }}><div style={{ width: `${h.pct}%`, background: "var(--accent)" }} /></div>
        <span className="small">%{Math.round(h.pct)}</span>{sigs.length > 0 && <LearnLine h={h} />}</>}
    </button>
    {open && sigs.length > 0 && <SignalList h={h} editable={can("act.anomaly")} />}
  </div>;
}
