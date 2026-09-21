import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Flame, Radar, Sparkles, VolumeX, Waves } from "lucide-react";
import { get, post } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Btn, Card, Empty, Err, Field, Kpi, Spark, fmtTs, hhmm } from "../components/ui";

const ICON: Record<string, any> = { errors: Flame, rate: Waves, silence: VolumeX, pattern: Sparkles, metric: Radar };
const COLOR: Record<string, string> = { open: "#f87171", ack: "#fbbf24", resolved: "#2dd4bf", ignored: "#64748b" };

export function anomalyTitle(t: (k: string, kw?: any) => string, a: any): string {
  const v = Math.round(a.observed), b = Math.round(a.baseline);
  if (a.kind === "errors" || a.kind === "rate") return t(`an_t_${a.kind}`, { host: a.host, v, b });
  if (a.kind === "metric") return t("an_t_metric", { host: a.host, metric: a.metric, v, b });
  if (a.kind === "silence") return t("an_t_silence", { host: a.host, v });
  return t("an_t_pattern", { sev: a.metric || "", v, tpl: (a.key.split(":").slice(1).join(":") || "").slice(0, 90) });
}

export default function Anomalies() {
  const { t } = useT(); const { can } = useAuth(); const qc = useQueryClient();
  const [status, setStatus] = useState("active");
  const stats = useQuery({ queryKey: ["an-stats"], queryFn: () => get("/anomalies/stats"), refetchInterval: 15000 });
  const list = useQuery({ queryKey: ["anomalies", status], queryFn: () => get(`/anomalies?status=${status}`), refetchInterval: 15000 });
  const scan = useMutation({ mutationFn: () => post("/anomalies/scan"), onSuccess: () => qc.invalidateQueries() });
  const st = stats.data; const editable = can("act.anomaly");
  return (
    <div className="stack">
      <div className="row between"><div><h2>🧭 {t("an_title")}</h2><span className="muted small">{t("an_sub")}</span></div>{editable && <Btn onClick={() => scan.mutate()} disabled={scan.isPending}>🔍 {t("an_scan_now")}</Btn>}</div>
      <div className="grid k4">
        <Kpi value={st?.open ?? 0} label={t("an_open")} accent="#f87171" sub={Object.entries(st?.kinds ?? {}).map(([k, n]) => `${t("an_kind_" + k)} ${n}`).join(" · ") || "-"} />
        <Kpi value={st?.ack ?? 0} label={t("an_ack")} accent="#fbbf24" />
        <Kpi value={st?.resolved_today ?? 0} label={t("an_resolved_today")} accent="#2dd4bf" sub={`${st?.resolved ?? 0} ${t("an_st_resolved").toLowerCase()}`} />
        <Kpi value={st?.runs ?? 0} label={t("an_scans")} accent="#a78bfa" sub={hhmm(st?.last_run)} />
      </div>
      <div className="row"><select className="input" style={{ width: 260 }} value={status} onChange={(e) => setStatus(e.target.value)}>{["active", "open", "ack", "resolved", "ignored", "all"].map((k) => <option key={k} value={k}>{t("an_st_" + k)}</option>)}</select></div>
      <Err e={scan.error} />
      {!list.data?.length ? <Empty>{t("an_none")}</Empty> : list.data.map((a: any) => <AnomalyCard key={a.id} a={a} editable={editable} />)}
    </div>
  );
}

function AnomalyCard({ a, editable }: { a: any; editable: boolean }) {
  const { t } = useT(); const qc = useQueryClient();
  const [open, setOpen] = useState(a.status === "ack");
  const [owner, setOwner] = useState(a.owner); const [note, setNote] = useState(a.note);
  const inv = () => qc.invalidateQueries({ queryKey: ["anomalies"] }).then(() => qc.invalidateQueries({ queryKey: ["an-stats"] }));
  const setStatus = useMutation({ mutationFn: (s: string) => post(`/anomalies/${a.id}/status`, { status: s, owner, note }), onSuccess: inv });
  const step = useMutation({ mutationFn: (p: { id: string; done: boolean }) => post(`/anomalies/${a.id}/steps/${p.id}`, { done: p.done }), onSuccess: inv });
  const action = useMutation({ mutationFn: () => post(`/anomalies/${a.id}/action`), onSuccess: inv });
  const Icon = ICON[a.kind] ?? Radar;
  const numeric = ["errors", "rate", "metric"].includes(a.kind);
  return (
    <Card className="tight">
      <div className="svc" onClick={() => setOpen((o) => !o)} style={{ cursor: "pointer" }}>
        <div className="h"><span className="chip"><span className="d" style={{ background: COLOR[a.status] }} /></span><Icon size={16} /> {anomalyTitle(t, a)} <span className="chip">{t("an_kind_" + a.kind)}</span> <span className="muted">· {a.env}</span>
          {a.cleared_at && ["open", "ack"].includes(a.status) && <span className="badge green">{t("an_recovered")}</span>}{!!a.action_id && <span className="badge blue">{t("an_linked", { id: a.action_id })}</span>}{a.owner && <span className="muted">· 👤 {a.owner}</span>}</div>
        <div className="v">{numeric ? <>{t("an_observed")} <b>{Math.round(a.observed)}</b> · {t("an_baseline")} {Math.round(a.baseline)} · </> : <>{t("an_observed")} <b>{Math.round(a.observed)}</b> · </>}{t("an_score")} {a.score} · {t("an_hits")} {a.hits} · {a.done}/{a.steps.length} {t("an_steps").toLowerCase()} · {fmtTs(a.first_seen)} → {hhmm(a.last_seen)}</div>
      </div>
      {open && (
        <div className="stack" style={{ marginTop: 10 }}>
          <div className="muted small">{a.detail}</div>
          {numeric && !!a.series?.length && <div><Spark data={a.series} /><div className="dim small">{t("an_series")}</div></div>}
          <div>{a.steps.map((s: any) => (
            <label key={s.id} className={`check ${s.done ? "done" : ""}`}><input type="checkbox" checked={!!s.done} disabled={!editable} onChange={(e) => step.mutate({ id: s.id, done: e.target.checked })} /><span>{t("an_step_" + s.id)}{s.ts && <span className="dim"> · {hhmm(s.ts)}</span>}</span></label>))}</div>
          <div className="grid k2"><Field label={t("owner")}><input className="input" value={owner} onChange={(e) => setOwner(e.target.value)} disabled={!editable} /></Field><Field label={t("note")}><input className="input" value={note} onChange={(e) => setNote(e.target.value)} disabled={!editable} /></Field></div>
          {editable && <div className="row">
            <Btn sm onClick={() => setStatus.mutate(a.status)}>{t("save")}</Btn>
            {a.status === "open" && <Btn sm onClick={() => setStatus.mutate("ack")}>{t("an_btn_ack")}</Btn>}
            {["open", "ack"].includes(a.status) && <Btn sm kind="primary" onClick={() => setStatus.mutate("resolved")}>{t("an_btn_resolve")}</Btn>}
            {["open", "ack"].includes(a.status) && <Btn sm onClick={() => setStatus.mutate("ignored")}>{t("an_btn_ignore")}</Btn>}
            {["resolved", "ignored"].includes(a.status) && <Btn sm onClick={() => setStatus.mutate("open")}>{t("an_btn_reopen")}</Btn>}
            {!a.action_id && ["open", "ack"].includes(a.status) && <Btn sm onClick={() => action.mutate()}>{t("an_btn_action")}</Btn>}
          </div>}
          <Err e={setStatus.error || step.error || action.error} />
        </div>)}
    </Card>
  );
}
