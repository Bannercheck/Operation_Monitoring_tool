import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { del, get, patch, post } from "../api";
import { useT } from "../i18n";
import { Badge, Btn, Card, Confirm, Err, Field, Modal, Table, hhmm } from "../components/ui";

const STATUSES = ["open", "in_progress", "done", "suppressed"]; const PRIOS = ["P1", "P2", "P3", "P4"];

export default function Actions() {
  const { t } = useT(); const qc = useQueryClient(); const [add, setAdd] = useState(false);
  const list = useQuery({ queryKey: ["actions"], queryFn: () => get("/actions") });
  const inv = () => qc.invalidateQueries({ queryKey: ["actions"] });
  const upd = useMutation({ mutationFn: (p: { id: number; body: any }) => patch(`/actions/${p.id}`, p.body), onSuccess: inv });
  const rm = useMutation({ mutationFn: (id: number) => del(`/actions/${id}`), onSuccess: inv });
  const rows = (list.data ?? []).slice().sort((a: any, b: any) => Number(a.status === "done") - Number(b.status === "done") || a.priority.localeCompare(b.priority));
  return (
    <div className="stack">
      <div className="row between"><h2>{t("act_title")}</h2><Btn kind="primary" onClick={() => setAdd(true)}>{t("act_new")}</Btn></div>
      <Card><Table cols={[
        { k: "priority", label: t("act_priority"), render: (r) => <select className="input" style={{ width: 80, padding: "4px 8px" }} value={r.priority} onChange={(e) => upd.mutate({ id: r.id, body: { priority: e.target.value } })}>{PRIOS.map((p) => <option key={p}>{p}</option>)}</select> },
        { k: "title", label: t("act_title_f"), render: (r) => <div><b>{r.title}</b><div className="muted small">{r.recommendation?.slice(0, 140)}</div></div> },
        { k: "incident_id", label: t("act_incident"), render: (r) => <span className="mono">{r.incident_id}</span> }, { k: "owner", label: t("owner") },
        { k: "status", label: t("status"), render: (r) => <select className="input" style={{ width: 150, padding: "4px 8px" }} value={r.status} onChange={(e) => upd.mutate({ id: r.id, body: { status: e.target.value } })}>{STATUSES.map((s) => <option key={s} value={s}>{t("st_" + s)}</option>)}</select> },
        { k: "updated_at", label: t("time"), render: (r) => <span className="muted small">{r.updated_at?.slice(0, 10)} {hhmm(r.updated_at)}</span> },
        { k: "x", label: "", render: (r) => <Confirm onConfirm={() => rm.mutate(r.id)}>{t("delete")}</Confirm> }]} rows={rows} /></Card>
      <Err e={upd.error || rm.error} />
      {add && <NewAction onClose={() => { setAdd(false); inv(); }} />}
    </div>
  );
}

function NewAction({ onClose }: { onClose: () => void }) {
  const { t } = useT(); const [f, setF] = useState({ incident_id: "", title: "", priority: "P2", owner: "", recommendation: "" }); const [err, setErr] = useState<any>(null);
  const submit = async (e: React.FormEvent) => { e.preventDefault(); try { await post("/actions", f); onClose(); } catch (x) { setErr(x); } };
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  return <Modal title={t("act_new")} onClose={onClose}><form className="stack" onSubmit={submit}>
    <div className="grid k2"><Field label={t("act_incident")}><input className="input" value={f.incident_id} onChange={set("incident_id")} required /></Field><Field label={t("act_priority")}><select className="input" value={f.priority} onChange={set("priority")}>{PRIOS.map((p) => <option key={p}>{p}</option>)}</select></Field></div>
    <Field label={t("act_title_f")}><input className="input" value={f.title} onChange={set("title")} required /></Field><Field label={t("owner")}><input className="input" value={f.owner} onChange={set("owner")} /></Field>
    <Field label={t("act_rec")}><textarea className="input" value={f.recommendation} onChange={set("recommendation")} /></Field><Err e={err} /><div className="row"><Btn kind="primary" type="submit">{t("save")}</Btn></div></form></Modal>;
}
