import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { del, get, patch, post, put } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Badge, Btn, Card, Confirm, Err, Field, Modal, Table, Tabs, fmtTs } from "../components/ui";
import { PermGrid, type Catalogue } from "../components/PermGrid";
import { TeamsPanel } from "./Teams";

type UTeam = { id: number; name: string; role_id: number | null; role_label: string };

export default function Users() {
  const { t } = useT(); const { user: me, can } = useAuth(); const qc = useQueryClient(); const [tab, setTab] = useState<"list" | "roles" | "teams" | "events">("list"); const [add, setAdd] = useState(false); const [pw, setPw] = useState<any>(null);
  const [teamF, setTeamF] = useState("");
  const users = useQuery({ queryKey: ["users"], queryFn: () => get("/users") });
  const roles = useQuery({ queryKey: ["roles"], queryFn: () => get("/roles") });
  const teams = useQuery({ queryKey: ["teams"], queryFn: () => get("/teams"), enabled: can("page.teams") });
  const events = useQuery({ queryKey: ["user-events"], queryFn: () => get("/users/events?n=100"), enabled: tab === "events" });
  const inv = () => { qc.invalidateQueries({ queryKey: ["users"] }); qc.invalidateQueries({ queryKey: ["roles"] }); qc.invalidateQueries({ queryKey: ["teams"] }); };
  const upd = useMutation({ mutationFn: (p: { id: number; body: any }) => patch(`/users/${p.id}`, p.body), onSuccess: inv });
  const unlock = useMutation({ mutationFn: (id: number) => post(`/users/${id}/unlock`), onSuccess: inv });
  const rm = useMutation({ mutationFn: (id: number) => del(`/users/${id}`), onSuccess: inv });
  const roleRows: any[] = roles.data?.roles ?? [];
  const names = roleRows.map((r: any) => r.name);
  const permsOf = useMemo(() => Object.fromEntries(roleRows.map((r: any) => [r.name, new Set<string>(r.perms)])), [roles.data]);   // eslint-disable-line react-hooks/exhaustive-deps
  const teamPerms = useMemo(() => {     // team id -> role id -> perms (for the "wider" hint)
    const out: Record<number, Record<number, string[]>> = {};
    for (const tm of teams.data?.teams ?? []) out[tm.id] = Object.fromEntries(tm.roles.map((r: any) => [r.id, r.perms]));
    return out;
  }, [teams.data]);
  const anyTeam = (teams.data?.teams ?? []).length > 0;
  const wide = (r: any) => r.role === "admin" || permsOf[r.role]?.has("scope.all");
  const teamKeys = useMemo(() => new Set<string>((roles.data?.sections ?? []).flatMap((s: any) => s.items.filter((i: any) => i.team).map((i: any) => i.key))), [roles.data]);
  const widerList = (r: any): string[] => {    // team-assignable permissions the installation role grants and none of the user's team roles has
    if (!r.teams?.length || wide(r) || !teams.data) return [];
    const g = permsOf[r.role] ?? new Set<string>();
    const tp = new Set<string>(r.teams.flatMap((x: UTeam) => teamPerms[x.id]?.[x.role_id ?? -1] ?? []));
    return [...g].filter((p) => teamKeys.has(p) && !tp.has(p)).sort();
  };
  const rows = (users.data ?? []).filter((r: any) => !teamF || (r.teams ?? []).some((x: UTeam) => String(x.id) === teamF));
  return <div className="stack">
    <div className="row between"><h2>👥 {t("us_title")}</h2><Btn kind="primary" onClick={() => setAdd(true)}>{t("us_add")}</Btn></div>
    <Tabs value={tab} onChange={setTab} tabs={[{ k: "list", label: `${t("nav_users")} · ${users.data?.length ?? 0}` }, { k: "roles", label: t("us_global_roles") }, ...(can("page.teams") ? [{ k: "teams" as const, label: t("tm_tab") }] : []), { k: "events", label: t("us_events") }]} />
    {tab === "teams" && <TeamsPanel />}
    {tab === "list" && <Card right={anyTeam ? <select className="input" style={{ width: 200, padding: "4px 8px" }} value={teamF} onChange={(e) => setTeamF(e.target.value)}>
        <option value="">{t("tmr_all_teams")}</option>{(teams.data?.teams ?? []).map((x: any) => <option key={x.id} value={x.id}>{x.name}</option>)}</select> : undefined}>
      <Table cols={[{ k: "email", label: t("email"), render: (r) => <div><b>{r.name || r.email}</b><div className="muted small">{r.email} · {r.provider}</div></div> },
      { k: "role", label: t("tmr_global_role"), render: (r) => <div className="row" style={{ gap: 6, flexWrap: "nowrap" }}><select className="input" style={{ width: 130, padding: "4px 8px" }} value={r.role} onChange={(e) => upd.mutate({ id: r.id, body: { role: e.target.value } })}>{names.map((n: string) => <option key={n} value={n}>{roleRows.find((x: any) => x.name === n)?.label || n}</option>)}</select>
        {widerList(r).length > 0 && <span className="badge amber" style={{ textTransform: "none" }} title={t("tmr_wider_tip", { list: widerList(r).join(", ") })}>{t("tmr_wider")}</span>}</div> },
      { k: "teams", label: t("us_teams_col"), render: (r) => <div className="row" style={{ gap: 4 }}>{(r.teams ?? []).map((x: UTeam) => <span key={x.id} className="chip small">{x.name} · {x.role_label}</span>)}
        {anyTeam && !(r.teams ?? []).length && !wide(r) && r.status === "active" && <span className="badge amber" style={{ textTransform: "none" }} title={t("tmr_no_team_tip")}>{t("tmr_no_team")}</span>}</div> },
      { k: "status", label: t("status"), render: (r) => <Badge v={r.status} /> }, { k: "last_login", label: t("us_last"), render: (r) => <span className="muted small">{fmtTs(r.last_login)}</span> },
      { k: "x", label: "", render: (r) => <div className="row"><Btn sm onClick={() => setPw(r)}>{t("us_pw_reset")}</Btn><Btn sm onClick={() => unlock.mutate(r.id)}>{t("us_unlock")}</Btn>
        {r.status === "active" ? <Btn sm onClick={() => upd.mutate({ id: r.id, body: { status: "disabled" } })}>{t("us_disable")}</Btn> : <Btn sm onClick={() => upd.mutate({ id: r.id, body: { status: "active" } })}>{t("us_enable")}</Btn>}
        {r.id !== me?.id && <Confirm onConfirm={() => rm.mutate(r.id)}>{t("delete")}</Confirm>}</div> }]} rows={rows} /><Err e={upd.error || rm.error || unlock.error} /></Card>}
    {tab === "roles" && roles.data && <RolesMatrix data={roles.data} onChange={inv} />}
    {tab === "events" && <Card><Table cols={[{ k: "ts", label: t("time"), render: (r) => fmtTs(r.ts) }, { k: "email", label: t("email") }, { k: "event", label: "event" }, { k: "ok", label: t("status"), render: (r) => (r.ok ? "✓" : "✗") }, { k: "detail", label: t("note") }]} rows={events.data ?? []} /></Card>}
    {add && <NewUser roles={roleRows} teams={teams.data?.teams ?? []} onClose={() => { setAdd(false); inv(); }} />}
    {pw && <SetPassword u={pw} onClose={() => setPw(null)} />}
  </div>;
}

/** Installation roles: the list on the left, one permission column on the right grouped like the sidebar (team.manage is a team
 *  role permission only, so it is not offered here). */
function RolesMatrix({ data, onChange }: { data: any; onChange: () => void }) {
  const { t } = useT(); const [sel, setSel] = useState<string>(data.roles[0]?.name); const [perms, setPerms] = useState<string[]>(data.roles[0]?.perms ?? []); const [newName, setNewName] = useState("");
  const role = data.roles.find((r: any) => r.name === sel);
  const pick = (name: string) => { setSel(name); setPerms(data.roles.find((r: any) => r.name === name)?.perms ?? []); };
  const save = useMutation({ mutationFn: () => put(`/roles/${sel}`, { perms, label: role?.label ?? sel }), onSuccess: onChange });
  const reset = useMutation({ mutationFn: () => post(`/roles/${sel}/reset`), onSuccess: (r: any) => { if (r?.perms) setPerms(r.perms); onChange(); } });
  const rm = useMutation({ mutationFn: () => del(`/roles/${sel}`), onSuccess: () => { pick(data.roles[0].name); onChange(); } });
  const create = useMutation({ mutationFn: () => put(`/roles/${newName}`, { perms: [], label: newName }), onSuccess: () => { setNewName(""); onChange(); } });
  const cat: Catalogue = { sections: data.sections ?? [] };
  return <div className="grid k2 roles-grid">
    <Card><div className="stack" style={{ gap: 6 }}>{data.roles.map((r: any) => <button key={r.name} className={`role-pick ${r.name === sel ? "on" : ""}`} onClick={() => pick(r.name)}><b>{r.label || r.name}</b>{!!r.builtin && <span className="dim small"> · builtin</span>}<span className="dim small"> · {r.perms.length}</span></button>)}
      <div className="row" style={{ marginTop: 8 }}><input className="input" placeholder={t("name")} value={newName} onChange={(e) => setNewName(e.target.value)} /><Btn sm onClick={() => create.mutate()} disabled={!newName}>{t("add")}</Btn></div></div></Card>
    <Card title={role?.label || sel} right={<><Btn sm kind="primary" onClick={() => save.mutate()} disabled={sel === "admin"}>{t("save")}</Btn>{!!role?.builtin && <Btn sm onClick={() => reset.mutate()}>reset</Btn>}{!role?.builtin && <Confirm onConfirm={() => rm.mutate()}>{t("delete")}</Confirm>}</>}>
      <div className="muted small" style={{ marginBottom: 8 }}>{t("us_global_lead")}</div>
      <PermGrid cat={cat} team={false} hide={["team.manage"]} cols={[{ id: sel, label: role?.label || sel, perms: sel === "admin" ? cat.sections.flatMap((s) => s.items.map((i) => i.key)) : perms, locked: sel === "admin" }]}
        onChange={(_id, p) => setPerms(p)} />
      <Err e={save.error || rm.error} /></Card></div>;
}

function NewUser({ roles, teams, onClose }: { roles: any[]; teams: any[]; onClose: () => void }) {
  const { t } = useT(); const [f, setF] = useState({ email: "", password: "", name: "", role: "operator", team: "", teamRole: "" }); const [err, setErr] = useState<any>(null);
  const team = teams.find((x) => String(x.id) === f.team);
  const pickTeam = (v: string) => { const tm = teams.find((x) => String(x.id) === v); setF({ ...f, team: v, role: v ? "member" : "operator", teamRole: tm ? String(tm.default_role_id ?? "") : "" }); };
  const submit = async (e: React.FormEvent) => { e.preventDefault(); try { await post("/users", { email: f.email, password: f.password, name: f.name, role: f.role, team_id: f.team ? Number(f.team) : null, team_role_id: f.teamRole ? Number(f.teamRole) : null }); onClose(); } catch (x) { setErr(x); } };
  return <Modal title={t("us_add")} onClose={onClose}><form className="stack" onSubmit={submit}>
    <Field label={t("email")}><input className="input" type="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} required /></Field><Field label={t("name")}><input className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
    <div className="grid k2"><Field label={t("password")}><input className="input" type="password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} required minLength={10} /></Field>
      <Field label={t("tmr_global_role")}><select className="input" value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })}>{roles.map((r: any) => <option key={r.name} value={r.name}>{r.label || r.name}</option>)}{!roles.some((r: any) => r.name === "member") && f.team && <option value="member">member</option>}</select></Field></div>
    {teams.length > 0 && <div className="grid k2"><Field label={t("us_team_opt")}><select className="input" value={f.team} onChange={(e) => pickTeam(e.target.value)}><option value="">-</option>{teams.map((x: any) => <option key={x.id} value={x.id}>{x.name}</option>)}</select></Field>
      <Field label={t("tmr_member_role")}><select className="input" value={f.teamRole} disabled={!team} onChange={(e) => setF({ ...f, teamRole: e.target.value })}>{(team?.roles ?? []).map((r: any) => <option key={r.id} value={r.id}>{r.label}</option>)}</select></Field></div>}
    <Err e={err} /><div className="row"><Btn kind="primary" type="submit">{t("save")}</Btn></div></form></Modal>;
}

function SetPassword({ u, onClose }: { u: any; onClose: () => void }) {
  const { t } = useT(); const [pw, setPw] = useState(""); const [err, setErr] = useState<any>(null);
  const submit = async (e: React.FormEvent) => { e.preventDefault(); try { await post(`/users/${u.id}/password`, { password: pw }); onClose(); } catch (x) { setErr(x); } };
  return <Modal title={`${t("us_pw_reset")} · ${u.email}`} onClose={onClose}><form className="stack" onSubmit={submit}><Field label={t("pw_new")}><input className="input" type="password" value={pw} onChange={(e) => setPw(e.target.value)} required minLength={10} /></Field><Err e={err} /><div className="row"><Btn kind="primary" type="submit">{t("save")}</Btn></div></form></Modal>;
}
