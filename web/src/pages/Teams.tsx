import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { del, get, post, put } from "../api";
import { useT } from "../i18n";
import { Btn, Card, Confirm, Empty, Err, Field, Modal } from "../components/ui";

/** Teams and their hosts. A team owns hosts by rules (any rule matching is enough) and by explicit assignment; its members
 *  then see only those hosts, and the services / apps that run on them or match a service rule, everywhere in Watchover. */
type Rule = { field: string; op: string; value: string };
const EMPTY = { name: "", description: "", rules: [{ field: "host", op: "glob", value: "" }] as Rule[], hosts: [] as string[], members: [] as string[] };

export function TeamsPanel() {
  const { t } = useT(); const qc = useQueryClient();
  const q = useQuery({ queryKey: ["teams"], queryFn: () => get("/teams") });
  const [edit, setEdit] = useState<any>(null);
  const rm = useMutation({ mutationFn: (id: number) => del(`/teams/${id}`), onSuccess: () => qc.invalidateQueries({ queryKey: ["teams"] }) });
  const teams: any[] = q.data?.teams ?? [];
  return <div className="stack">
    <Card title={t("tm_title")} right={<Btn kind="primary" sm onClick={() => setEdit({ ...EMPTY })}>{t("tm_add")}</Btn>}>
      <div className="muted small" style={{ marginBottom: 8 }}>{t("tm_lead")}</div>
      {!teams.length ? <Empty>{t("tm_none")}</Empty> :
        <div className="table-wrap"><table className="table"><thead><tr><th>{t("tm_name")}</th><th>{t("tm_rules")}</th><th className="right">{t("tm_hosts")}</th><th>{t("tm_members")}</th><th></th></tr></thead>
          <tbody>{teams.map((tm) => <tr key={tm.id}>
            <td><b>{tm.name}</b>{tm.description ? <div className="dim small">{tm.description}</div> : null}</td>
            <td className="small mono">{tm.rules.map((r: Rule, i: number) => <div key={i}>{t(`tm_f_${r.field}`)} {t(`tm_op_${r.op}`)} {r.value}</div>)}{tm.hosts.length ? <div>+ {tm.hosts.length} {t("tm_explicit")}</div> : null}</td>
            <td className="num" title={tm.matched.join(", ")}>{tm.matched.length}</td>
            <td className="small">{tm.members.join(", ") || <span className="dim">-</span>}</td>
            <td><div className="row" style={{ gap: 4, flexWrap: "nowrap" }}><Btn sm onClick={() => setEdit({ ...tm })}>{t("edit")}</Btn><Confirm onConfirm={() => rm.mutate(tm.id)}>🗑</Confirm></div></td>
          </tr>)}</tbody></table></div>}
    </Card>
    <div className="dim small">{t("tm_hint")}</div>
    {edit && <Editor team={edit} fields={q.data?.fields ?? []} ops={q.data?.ops ?? []} onClose={() => setEdit(null)} />}
  </div>;
}

function Editor({ team, fields, ops, onClose }: { team: any; fields: string[]; ops: string[]; onClose: () => void }) {
  const { t } = useT(); const qc = useQueryClient();
  const [f, setF] = useState({ name: team.name, description: team.description ?? "", rules: (team.rules ?? []) as Rule[], hosts: (team.hosts ?? []).join(", "), members: (team.members ?? []) as string[] });
  const users = useQuery({ queryKey: ["users"], queryFn: () => get("/users") });
  const body = () => ({ name: f.name, description: f.description, rules: f.rules.filter((r) => r.value.trim()), hosts: f.hosts.split(/[\s,]+/).filter(Boolean), members: f.members });
  const [preview, setPreview] = useState<string[] | null>(null); const [pErr, setPErr] = useState<any>(null);
  useEffect(() => { const id = setTimeout(() => { post("/teams/preview", body()).then((r: any) => { setPreview(r.hosts); setPErr(null); }, (e) => setPErr(e)); }, 400); return () => clearTimeout(id); }, [JSON.stringify(f)]);
  const save = useMutation({ mutationFn: () => (team.id ? put(`/teams/${team.id}`, body()) : post("/teams", body())), onSuccess: () => { qc.invalidateQueries({ queryKey: ["teams"] }); onClose(); } });
  const setRule = (i: number, k: keyof Rule, v: string) => setF({ ...f, rules: f.rules.map((r, n) => (n === i ? { ...r, [k]: v } : r)) });
  return <Modal title={team.id ? `${t("edit")} · ${team.name}` : t("tm_add")} onClose={onClose} wide>
    <form className="stack" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <div className="row" style={{ gap: 8 }}><Field label={t("tm_name")}><input className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} required placeholder="EA" /></Field>
        <Field label={t("tm_desc")}><input className="input" style={{ width: 360 }} value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field></div>
      <div><div className="muted small" style={{ marginBottom: 4 }}>{t("tm_rules")} · {t("tm_rules_any")}</div>
        {f.rules.map((r, i) => <div key={i} className="row" style={{ gap: 6, marginBottom: 6 }}>
          <select className="input" style={{ width: 190 }} value={r.field} onChange={(e) => setRule(i, "field", e.target.value)}>{fields.map((x) => <option key={x} value={x}>{t(`tm_f_${x}`)}</option>)}</select>
          <select className="input" style={{ width: 150 }} value={r.op} onChange={(e) => setRule(i, "op", e.target.value)}>{ops.map((x) => <option key={x} value={x}>{t(`tm_op_${x}`)}</option>)}</select>
          <input className="input grow" value={r.value} onChange={(e) => setRule(i, "value", e.target.value)} placeholder={r.field === "ip" ? "10.20.0.0/16" : r.field === "host" ? "ea-*" : ""} />
          <Btn sm type="button" onClick={() => setF({ ...f, rules: f.rules.filter((_, n) => n !== i) })}>✕</Btn></div>)}
        <Btn sm type="button" onClick={() => setF({ ...f, rules: [...f.rules, { field: "host", op: "glob", value: "" }] })}>+ {t("tm_rule_add")}</Btn></div>
      <Field label={t("tm_explicit_hosts")}><input className="input" value={f.hosts} onChange={(e) => setF({ ...f, hosts: e.target.value })} placeholder="sap-pr1, erp-db-02" /></Field>
      <Field label={t("tm_members")}><div className="row" style={{ gap: 6, flexWrap: "wrap" }}>{(users.data ?? []).map((u: any) => <label key={u.email} className="chip" style={{ cursor: "pointer" }}>
        <input type="checkbox" checked={f.members.includes(u.email)} onChange={(e) => setF({ ...f, members: e.target.checked ? [...f.members, u.email] : f.members.filter((m) => m !== u.email) })} /> {u.email}</label>)}</div></Field>
      <Card className="tight"><div className="small"><b>{t("tm_preview")}</b> · {preview ? `${preview.length} ${t("tm_hosts").toLowerCase()}` : "…"}</div>
        {pErr ? <Err e={pErr} /> : <div className="small mono" style={{ marginTop: 4, maxHeight: 120, overflow: "auto" }}>{(preview ?? []).join(", ") || <span className="dim">{t("tm_preview_none")}</span>}</div>}</Card>
      <Err e={save.error} /><div className="row"><Btn kind="primary" type="submit" disabled={save.isPending}>{t("save")}</Btn></div>
    </form>
  </Modal>;
}
