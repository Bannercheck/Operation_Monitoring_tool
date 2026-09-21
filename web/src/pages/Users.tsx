import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { del, get, patch, post, put } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Badge, Btn, Card, Confirm, Err, Field, Modal, Table, Tabs, fmtTs } from "../components/ui";

export default function Users() {
  const { t } = useT(); const { user: me } = useAuth(); const qc = useQueryClient(); const [tab, setTab] = useState<"list" | "roles" | "events">("list"); const [add, setAdd] = useState(false); const [pw, setPw] = useState<any>(null);
  const users = useQuery({ queryKey: ["users"], queryFn: () => get("/users") });
  const roles = useQuery({ queryKey: ["roles"], queryFn: () => get("/roles") });
  const events = useQuery({ queryKey: ["user-events"], queryFn: () => get("/users/events?n=100"), enabled: tab === "events" });
  const inv = () => { qc.invalidateQueries({ queryKey: ["users"] }); qc.invalidateQueries({ queryKey: ["roles"] }); };
  const upd = useMutation({ mutationFn: (p: { id: number; body: any }) => patch(`/users/${p.id}`, p.body), onSuccess: inv });
  const unlock = useMutation({ mutationFn: (id: number) => post(`/users/${id}/unlock`), onSuccess: inv });
  const rm = useMutation({ mutationFn: (id: number) => del(`/users/${id}`), onSuccess: inv });
  const names = (roles.data?.roles ?? []).map((r: any) => r.name);
  return <div className="stack">
    <div className="row between"><h2>👥 {t("us_title")}</h2><Btn kind="primary" onClick={() => setAdd(true)}>{t("us_add")}</Btn></div>
    <Tabs value={tab} onChange={setTab} tabs={[{ k: "list", label: `${t("nav_users")} · ${users.data?.length ?? 0}` }, { k: "roles", label: t("us_roles") }, { k: "events", label: t("us_events") }]} />
    {tab === "list" && <Card><Table cols={[{ k: "email", label: t("email"), render: (r) => <div><b>{r.name || r.email}</b><div className="muted small">{r.email} · {r.provider}</div></div> },
      { k: "role", label: t("us_role"), render: (r) => <select className="input" style={{ width: 130, padding: "4px 8px" }} value={r.role} onChange={(e) => upd.mutate({ id: r.id, body: { role: e.target.value } })}>{names.map((n: string) => <option key={n}>{n}</option>)}</select> },
      { k: "status", label: t("status"), render: (r) => <Badge v={r.status} /> }, { k: "last_login", label: t("us_last"), render: (r) => <span className="muted small">{fmtTs(r.last_login)}</span> },
      { k: "x", label: "", render: (r) => <div className="row"><Btn sm onClick={() => setPw(r)}>{t("us_pw_reset")}</Btn><Btn sm onClick={() => unlock.mutate(r.id)}>{t("us_unlock")}</Btn>
        {r.status === "active" ? <Btn sm onClick={() => upd.mutate({ id: r.id, body: { status: "disabled" } })}>{t("us_disable")}</Btn> : <Btn sm onClick={() => upd.mutate({ id: r.id, body: { status: "active" } })}>{t("us_enable")}</Btn>}
        {r.id !== me?.id && <Confirm onConfirm={() => rm.mutate(r.id)}>{t("delete")}</Confirm>}</div> }]} rows={users.data ?? []} /><Err e={upd.error || rm.error || unlock.error} /></Card>}
    {tab === "roles" && roles.data && <RolesMatrix data={roles.data} onChange={inv} />}
    {tab === "events" && <Card><Table cols={[{ k: "ts", label: t("time"), render: (r) => fmtTs(r.ts) }, { k: "email", label: t("email") }, { k: "event", label: "event" }, { k: "ok", label: t("status"), render: (r) => (r.ok ? "✓" : "✗") }, { k: "detail", label: t("note") }]} rows={events.data ?? []} /></Card>}
    {add && <NewUser roles={names} onClose={() => { setAdd(false); inv(); }} />}
    {pw && <SetPassword u={pw} onClose={() => setPw(null)} />}
  </div>;
}

function RolesMatrix({ data, onChange }: { data: any; onChange: () => void }) {
  const { t, lang } = useT(); const [sel, setSel] = useState<string>(data.roles[0]?.name); const [perms, setPerms] = useState<string[]>(data.roles[0]?.perms ?? []); const [newName, setNewName] = useState("");
  const role = data.roles.find((r: any) => r.name === sel);
  const pick = (name: string) => { setSel(name); setPerms(data.roles.find((r: any) => r.name === name)?.perms ?? []); };
  const save = useMutation({ mutationFn: () => put(`/roles/${sel}`, { perms, label: role?.label ?? sel }), onSuccess: onChange });
  const reset = useMutation({ mutationFn: () => post(`/roles/${sel}/reset`), onSuccess: onChange });
  const rm = useMutation({ mutationFn: () => del(`/roles/${sel}`), onSuccess: () => { pick(data.roles[0].name); onChange(); } });
  const create = useMutation({ mutationFn: () => put(`/roles/${newName}`, { perms: [], label: newName }), onSuccess: () => { setNewName(""); onChange(); } });
  const groups: Record<string, string[]> = {};
  for (const [k, v] of Object.entries<any>(data.permissions)) (groups[v.group] ||= []).push(k);
  return <div className="grid k2" style={{ gridTemplateColumns: "260px 1fr" }}>
    <Card><div className="stack" style={{ gap: 6 }}>{data.roles.map((r: any) => <button key={r.name} className={`tile ${r.name === sel ? "active" : ""}`} style={{ display: "flex", padding: "8px 10px", borderRadius: 10, border: "1px solid var(--line)", background: r.name === sel ? "rgba(45,212,191,0.15)" : "transparent", color: "inherit", cursor: "pointer" }} onClick={() => pick(r.name)}><b>{r.label || r.name}</b>{!!r.builtin && <span className="dim small"> · builtin</span>}</button>)}
      <div className="row" style={{ marginTop: 8 }}><input className="input" placeholder={t("name")} value={newName} onChange={(e) => setNewName(e.target.value)} /><Btn sm onClick={() => create.mutate()} disabled={!newName}>{t("add")}</Btn></div></div></Card>
    <Card title={role?.label || sel} right={<><Btn sm kind="primary" onClick={() => save.mutate()} disabled={sel === "admin"}>{t("save")}</Btn>{!!role?.builtin && <Btn sm onClick={() => reset.mutate()}>reset</Btn>}{!role?.builtin && <Confirm onConfirm={() => rm.mutate()}>{t("delete")}</Confirm>}</>}>
      <div className="grid k3">{Object.entries(groups).map(([g, keys]) => <div key={g}><h4>{g}</h4>{keys.map((k) => <label key={k} className="check"><input type="checkbox" checked={sel === "admin" || perms.includes(k)} disabled={sel === "admin"} onChange={(e) => setPerms(e.target.checked ? [...perms, k] : perms.filter((x) => x !== k))} /><span className="small">{lang === "tr" ? data.permissions[k].tr : data.permissions[k].en}</span></label>)}</div>)}</div>
      <Err e={save.error || rm.error} /></Card></div>;
}

function NewUser({ roles, onClose }: { roles: string[]; onClose: () => void }) {
  const { t } = useT(); const [f, setF] = useState({ email: "", password: "", name: "", role: "operator" }); const [err, setErr] = useState<any>(null);
  const submit = async (e: React.FormEvent) => { e.preventDefault(); try { await post("/users", f); onClose(); } catch (x) { setErr(x); } };
  return <Modal title={t("us_add")} onClose={onClose}><form className="stack" onSubmit={submit}>
    <Field label={t("email")}><input className="input" type="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} required /></Field><Field label={t("name")}><input className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
    <div className="grid k2"><Field label={t("password")}><input className="input" type="password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} required minLength={10} /></Field><Field label={t("us_role")}><select className="input" value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>{roles.map((r) => <option key={r}>{r}</option>)}</select></Field></div>
    <Err e={err} /><div className="row"><Btn kind="primary" type="submit">{t("save")}</Btn></div></form></Modal>;
}

function SetPassword({ u, onClose }: { u: any; onClose: () => void }) {
  const { t } = useT(); const [pw, setPw] = useState(""); const [err, setErr] = useState<any>(null);
  const submit = async (e: React.FormEvent) => { e.preventDefault(); try { await post(`/users/${u.id}/password`, { password: pw }); onClose(); } catch (x) { setErr(x); } };
  return <Modal title={`${t("us_pw_reset")} · ${u.email}`} onClose={onClose}><form className="stack" onSubmit={submit}><Field label={t("pw_new")}><input className="input" type="password" value={pw} onChange={(e) => setPw(e.target.value)} required minLength={10} /></Field><Err e={err} /><div className="row"><Btn kind="primary" type="submit">{t("save")}</Btn></div></form></Modal>;
}
