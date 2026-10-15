import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MoreHorizontal, Plus, X } from "lucide-react";
import { ApiError, del, get, patch, post, put } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Btn, Card, Confirm, Empty, Err, Field, Modal } from "../components/ui";
import { PermGrid, type Catalogue, type PermCol } from "../components/PermGrid";

/** Teams, their roles and their hosts. A team owns hosts by rules (any rule matching is enough) and by explicit assignment; each
 *  member has one team role (three ready-made roles come with every new team) whose ticks apply only on the team's hosts.
 *  Installation admins (page.teams) see every team here; a team manager edits the same editor on the "Ekibim" page. */
type Rule = { field: string; op: string; value: string; origin?: string };
type Role = { id: number; key: string; label: string; description?: string; perms: string[]; template: string; members: number; locked: boolean };
export type Team = { id: number; name: string; description: string; rules: Rule[]; hosts: string[]; matched: string[]; host_meta: { host: string; origin: string; added_by: string; added_at: string }[];
  members: string[]; memberships: { email: string; name: string; role_id: number | null; role_label: string; global_role?: string; wider?: string[] }[]; roles: Role[];
  default_role_id: number | null; owned: Record<string, number>; can_manage: boolean; admin: boolean; own_perms: string[]; conflicts?: { host: string; teams: string[] }[] };

const roleCounts = (tm: Team) => {
  const by = (k: string) => tm.memberships.filter((m) => tm.roles.find((r) => r.id === m.role_id)?.template === k).length;
  return { m: by("manager"), o: by("operator"), v: by("viewer") };
};

export function TeamsPanel() {
  const { t } = useT(); const qc = useQueryClient();
  const q = useQuery({ queryKey: ["teams"], queryFn: () => get("/teams") });
  const [edit, setEdit] = useState<number | null>(null); const [add, setAdd] = useState(false); const [blocked, setBlocked] = useState<{ id: number; owned: Record<string, number> } | null>(null);
  const rm = useMutation({ mutationFn: (p: { id: number; transfer?: string }) => del(`/teams/${p.id}${p.transfer ? `?transfer=${p.transfer}` : ""}`),
    onSuccess: () => { setBlocked(null); qc.invalidateQueries({ queryKey: ["teams"] }); },
    onError: (e: any, p) => { if (e instanceof ApiError && e.status === 409) get<Team>(`/teams/${p.id}`).then((tm) => setBlocked({ id: p.id, owned: tm.owned })); } });
  const teams: Team[] = q.data?.teams ?? [];
  return <div className="stack">
    <Card title={t("tm_title")} right={<Btn kind="primary" sm onClick={() => setAdd(true)}>{t("tm_add")}</Btn>}>
      <div className="muted small" style={{ marginBottom: 8 }}>{t("tm_lead")}</div>
      {!teams.length ? <Empty>{t("tm_none")}</Empty> :
        <div className="table-wrap"><table className="table"><thead><tr><th>{t("tm_name")}</th><th>{t("tm_rules")}</th><th className="right">{t("tm_hosts")}</th><th>{t("tmr_tab_roles")}</th><th></th></tr></thead>
          <tbody>{teams.map((tm) => { const n = roleCounts(tm); return <tr key={tm.id}>
            <td><b>{tm.name}</b>{tm.description ? <div className="dim small">{tm.description}</div> : null}</td>
            <td className="small mono">{tm.rules.map((r, i) => <div key={i}>{t(`tm_f_${r.field}`)} {t(`tm_op_${r.op}`)} {r.value}</div>)}{tm.hosts.length ? <div>+ {tm.hosts.length} {t("tm_explicit")}</div> : null}</td>
            <td className="num" title={tm.matched.join(", ")}>{tm.matched.length}</td>
            <td className="small"><span className="chip">{t("tmr_roles_count", n)}</span>{tm.memberships.length > n.m + n.o + n.v ? <span className="dim"> +{tm.memberships.length - n.m - n.o - n.v}</span> : null}</td>
            <td><div className="row" style={{ gap: 4, flexWrap: "nowrap" }}><Btn sm onClick={() => setEdit(tm.id)}>{t("edit")}</Btn><Confirm onConfirm={() => rm.mutate({ id: tm.id })}>🗑</Confirm></div></td>
          </tr>; })}</tbody></table></div>}
      {blocked && <div className="err" style={{ marginTop: 10 }}>{t("tmr_owned_block", { list: Object.entries(blocked.owned).filter(([, v]) => v).map(([k, v]) => `${k} ${v}`).join(", ") })}
        <div className="row" style={{ marginTop: 6 }}><Btn sm kind="danger" onClick={() => rm.mutate({ id: blocked.id, transfer: "install" })}>{t("tmr_transfer_install")}</Btn><Btn sm onClick={() => setBlocked(null)}>✕</Btn></div></div>}
    </Card>
    <div className="dim small">{t("tm_hint")} {t("tmr_kb_shared")}</div>
    {add && <NewTeam teams={teams} onClose={(id) => { setAdd(false); qc.invalidateQueries({ queryKey: ["teams"] }); if (id) setEdit(id); }} />}
    {edit != null && <Modal title={`${teams.find((x) => x.id === edit)?.name ?? ""} · ${t("edit")}`} onClose={() => setEdit(null)} wide><TeamEditor tid={edit} /></Modal>}
  </div>;
}

function NewTeam({ teams, onClose }: { teams: Team[]; onClose: (id?: number) => void }) {
  const { t } = useT(); const [f, setF] = useState({ name: "", description: "", copy: "" }); const [err, setErr] = useState<any>(null);
  const submit = async (e: React.FormEvent) => { e.preventDefault(); try { const r = await post<Team>("/teams", { name: f.name, description: f.description, rules: [], hosts: [], members: [], copy_roles_from: f.copy ? Number(f.copy) : null }); onClose(r.id); } catch (x) { setErr(x); } };
  return <Modal title={t("tm_add")} onClose={() => onClose()}><form className="stack" onSubmit={submit}>
    <Field label={t("tm_name")}><input className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} required placeholder="EA" autoFocus /></Field>
    <Field label={t("tm_desc")}><input className="input" value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
    <Field label={t("tmr_role_copy")} hint={t("tmr_lead")}><select className="input" value={f.copy} onChange={(e) => setF({ ...f, copy: e.target.value })}>
      <option value="">{t("tmr_roles_tpl")}</option>{teams.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}</select></Field>
    <Err e={err} /><div className="row"><Btn kind="primary" type="submit">{t("save")}</Btn></div></form></Modal>;
}

/** The team editor: Genel · Sunucular · Roller · Üyeler. Installation admins edit everything; a team manager edits members, roles
 *  (within their own permissions) and the team's own hosts, with rules read-only. */
export function TeamEditor({ tid }: { tid: number }) {
  const { t } = useT();
  const q = useQuery({ queryKey: ["team", tid], queryFn: () => get<Team>(`/teams/${tid}`) });
  const cat = useQuery({ queryKey: ["perm-catalogue"], queryFn: () => get<Catalogue>("/teams/permissions"), staleTime: 300000 });
  const [part, setPart] = useState<"general" | "hosts" | "roles" | "members">("roles");
  if (q.error) return <Err e={q.error} />;
  if (!q.data || !cat.data) return <div className="muted">…</div>;
  const tm = q.data;
  return <div className="stack">
    <div className="row between"><div className="seg">{(["general", "hosts", "roles", "members"] as const).map((k) => <button key={k} className={part === k ? "on" : ""} onClick={() => setPart(k)}>
      {t(`tmr_tab_${k}`)}{k === "hosts" ? ` · ${tm.matched.length}` : k === "members" ? ` · ${tm.memberships.length}` : k === "roles" ? ` · ${tm.roles.length}` : ""}</button>)}</div>
      <span className="dim small">{tm.name}</span></div>
    {part === "general" && <General tm={tm} />}
    {part === "hosts" && <Hosts tm={tm} />}
    {part === "roles" && <Roles tm={tm} cat={cat.data} />}
    {part === "members" && <Members tm={tm} />}
  </div>;
}

function useTeamSaved(tid: number) {
  const qc = useQueryClient(); const { reload } = useAuth();
  return () => { qc.invalidateQueries({ queryKey: ["team", tid] }); qc.invalidateQueries({ queryKey: ["teams"] }); qc.invalidateQueries({ queryKey: ["users"] }); qc.invalidateQueries({ queryKey: ["my-teams"] }); reload(); };
}

function General({ tm }: { tm: Team }) {
  const { t } = useT(); const done = useTeamSaved(tm.id);
  const [f, setF] = useState({ name: tm.name, description: tm.description });
  const save = useMutation({ mutationFn: () => put(`/teams/${tm.id}`, { name: f.name, description: f.description, rules: tm.rules, hosts: tm.hosts, members: tm.members }), onSuccess: done });
  const owned = Object.entries(tm.owned).filter(([, v]) => v);
  return <div className="stack">
    <div className="grid k2"><Field label={t("tm_name")}><input className="input" value={f.name} disabled={!tm.admin} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
      <Field label={t("tm_desc")}><input className="input" value={f.description} disabled={!tm.admin} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field></div>
    <div className="small"><span className="muted">{t("tmr_records")}: </span>{owned.length ? owned.map(([k, v]) => <span key={k} className="chip" style={{ marginRight: 4 }}>{k} {v}</span>) : <span className="dim">-</span>}</div>
    {!!tm.conflicts?.length && <div className="err">{t("tmr_conflicts", { list: tm.conflicts.map((c) => `${c.host} (${c.teams.join(", ")})`).join("; ") })}</div>}
    <div className="dim small">{t("tmr_kb_shared")}</div>
    <Err e={save.error} />{tm.admin && <div className="row"><Btn kind="primary" onClick={() => save.mutate()} disabled={save.isPending}>{t("save")}</Btn></div>}
  </div>;
}

function Hosts({ tm }: { tm: Team }) {
  const { t } = useT(); const { canIn } = useAuth(); const done = useTeamSaved(tm.id);
  const fq = useQuery({ queryKey: ["teams"], queryFn: () => get("/teams"), enabled: tm.admin });
  const [rules, setRules] = useState<Rule[]>(tm.rules.length ? tm.rules : [{ field: "host", op: "glob", value: "" }]);
  const [preview, setPreview] = useState<string[] | null>(null); const [pErr, setPErr] = useState<any>(null);
  const [addH, setAddH] = useState(""); const [rej, setRej] = useState<{ host: string; reason: string }[]>([]);
  const clean = () => rules.filter((r) => r.value.trim());
  useEffect(() => { if (!tm.admin) return; const id = setTimeout(() => { post("/teams/preview", { name: tm.name, rules: clean(), hosts: tm.hosts }).then((r: any) => { setPreview(r.hosts); setPErr(null); }, (e) => setPErr(e)); }, 400); return () => clearTimeout(id); }, [JSON.stringify(rules), tm.admin, tm.hosts.join()]);   // eslint-disable-line react-hooks/exhaustive-deps
  const saveRules = useMutation({ mutationFn: () => put(`/teams/${tm.id}`, { name: tm.name, description: tm.description, rules: clean(), hosts: tm.hosts, members: tm.members }), onSuccess: done });
  const addHosts = useMutation({ mutationFn: (hosts: string[]) => post(`/teams/${tm.id}/hosts`, { hosts, move: false }), onSuccess: (r: any) => { setRej(r.rejected ?? []); setAddH(""); done(); } });
  const rmHost = useMutation({ mutationFn: (h: string) => del(`/teams/${tm.id}/hosts/${encodeURIComponent(h)}`), onSuccess: done });
  const unowned = useQuery({ queryKey: ["teams-unowned", tm.matched.length], queryFn: () => get("/teams/unowned"), enabled: tm.admin });
  const [showUn, setShowUn] = useState(false);
  const setRule = (i: number, k: keyof Rule, v: string) => setRules(rules.map((r, n) => (n === i ? { ...r, [k]: v } : r)));
  const fields: string[] = fq.data?.fields ?? []; const ops: string[] = fq.data?.ops ?? [];
  return <div className="stack">
    <Card className="tight" title={t("tm_rules")}>
      {tm.admin ? <div><div className="muted small" style={{ marginBottom: 4 }}>{t("tm_rules_any")}</div>
        {rules.map((r, i) => <div key={i} className="row tm-rule">
          <select className="input" value={r.field} onChange={(e) => setRule(i, "field", e.target.value)}>{fields.map((x) => <option key={x} value={x}>{t(`tm_f_${x}`)}</option>)}</select>
          <select className="input" value={r.op} onChange={(e) => setRule(i, "op", e.target.value)}>{ops.map((x) => <option key={x} value={x}>{t(`tm_op_${x}`)}</option>)}</select>
          <input className="input grow" value={r.value} onChange={(e) => setRule(i, "value", e.target.value)} placeholder={r.field === "ip" ? "10.20.0.0/16" : r.field === "host" ? "ea-*" : ""} />
          <Btn sm type="button" onClick={() => setRules(rules.filter((_, n) => n !== i))}><X size={13} /></Btn></div>)}
        <div className="row"><Btn sm type="button" onClick={() => setRules([...rules, { field: "host", op: "glob", value: "" }])}>+ {t("tm_rule_add")}</Btn><div className="grow" />
          <Btn sm kind="primary" onClick={() => saveRules.mutate()} disabled={saveRules.isPending}>{t("save")}</Btn></div><Err e={saveRules.error} /></div>
        : <div className="small mono">{tm.rules.length ? tm.rules.map((r, i) => <div key={i}>{t(`tm_f_${r.field}`)} {t(`tm_op_${r.op}`)} {r.value}</div>) : <span className="dim">-</span>}<div className="dim small" style={{ marginTop: 4, fontFamily: "var(--font)" }}>{t("tmr_rules_ro")}</div></div>}
    </Card>
    <Card className="tight" title={t("tmr_owned_hosts")}>
      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>{tm.host_meta.length ? tm.host_meta.map((h) => <span key={h.host} className="chip" title={[h.added_by, h.added_at.slice(0, 16).replace("T", " ")].filter(Boolean).join(" · ")}>
        <span className="mono">{h.host}</span><span className="dim small">{t(`tmr_origin_${h.origin}`)}</span>
        <button className="chip-x" onClick={() => rmHost.mutate(h.host)} title={t("tmr_remove")}><X size={11} /></button></span>) : <span className="dim small">-</span>}</div>
      <form className="row" style={{ marginTop: 8 }} onSubmit={(e) => { e.preventDefault(); const hs = addH.split(/[\s,]+/).filter(Boolean); if (hs.length) addHosts.mutate(hs); }}>
        <input className="input grow" value={addH} onChange={(e) => setAddH(e.target.value)} placeholder="sap-pr1, erp-db-02" /><Btn sm type="submit">{t("tmr_host_add")}</Btn></form>
      {rej.length > 0 && <div className="small" style={{ marginTop: 6 }}>{rej.map((r) => <div key={r.host}><span className="mono">{r.host}</span> · <span className="badge amber" style={{ textTransform: "none" }}>{t(`tmr_rej_${r.reason}`)}</span></div>)}</div>}
      <Err e={addHosts.error || rmHost.error} />
    </Card>
    <Card className="tight"><div className="small"><b>{tm.admin ? t("tm_preview") : t("tmr_matched")}</b> · {t("tmr_hosts_n", { n: (tm.admin && preview ? preview : tm.matched).length })}</div>
      {pErr ? <Err e={pErr} /> : <div className="small mono" style={{ marginTop: 4, maxHeight: 120, overflow: "auto" }}>{((tm.admin && preview) || tm.matched).join(", ") || <span className="dim">{t("tm_preview_none")}</span>}</div>}</Card>
    {tm.admin && <Card className="tight"><button className="linklike" onClick={() => setShowUn(!showUn)}>{showUn ? "▾" : "▸"} {t("tmr_unowned")} ({unowned.data?.length ?? 0})</button>
      {showUn && <div className="stack" style={{ gap: 4, maxHeight: 220, overflow: "auto", marginTop: 6 }}>{(unowned.data ?? []).map((u: any) => <div key={u.host} className="row between small">
        <span><span className="mono">{u.host}</span> <span className="dim">{[u.env, u.application, u.ip].filter(Boolean).join(" · ")}</span></span>
        <Btn sm onClick={() => addHosts.mutate([u.host])}>{t("tmr_assign")}</Btn></div>)}{!unowned.data?.length && <span className="dim small">-</span>}</div>}</Card>}
    {canIn(tm.id, "act.connect") && <EnrollKey tid={tm.id} />}
  </div>;
}

export function EnrollKeyCard({ tid, title }: { tid: number; title?: string }) { return <EnrollKey tid={tid} title={title} />; }

function EnrollKey({ tid, title }: { tid: number; title?: string }) {
  const { t } = useT(); const qc = useQueryClient();
  const q = useQuery({ queryKey: ["team-key", tid], queryFn: () => get(`/teams/${tid}/enroll-key`) });
  const rot = useMutation({ mutationFn: () => post(`/teams/${tid}/enroll-key/rotate`), onSuccess: () => qc.invalidateQueries({ queryKey: ["team-key", tid] }) });
  const off = useMutation({ mutationFn: () => del(`/teams/${tid}/enroll-key`), onSuccess: () => qc.invalidateQueries({ queryKey: ["team-key", tid] }) });
  const [show, setShow] = useState(false);
  const key: string = q.data?.key ?? ""; const host = typeof window !== "undefined" ? window.location.hostname : "watchover";
  const hq = useQuery({ queryKey: ["api-health"], queryFn: () => get("/health"), staleTime: 300000 });
  const base = ((hq.data as any)?.ingest as string) || `http://${host}:8600`;   // OpenShift: the ingest Route (WATCHOVER_INGEST_URL)
  const cmd = `curl -fsSL ${base}/agent/install.sh | sudo bash -s -- --url ${base}/ingest --enroll-key ${key} --env prod`;
  return <Card className="tight" title={title ? `${t("tmr_enroll")} · ${title}` : t("tmr_enroll")}>
    <div className="dim small" style={{ marginBottom: 6 }}>{t("tmr_enroll_hint")}</div>
    {key ? <div className="stack" style={{ gap: 6 }}>
      <div className="row"><code className="mono grow" onClick={() => setShow(!show)} style={{ cursor: "pointer" }}>{show ? key : key.slice(0, 6) + "••••••••••••"}</code>
        <Btn sm onClick={() => navigator.clipboard?.writeText(key)}>{t("tmr_copy")}</Btn><Btn sm onClick={() => rot.mutate()}>{t("tmr_key_rotate")}</Btn><Confirm onConfirm={() => off.mutate()}>{t("tmr_key_disable")}</Confirm></div>
      {show && <div className="small"><span className="muted">{t("tmr_install_cmd")}: </span><code className="mono">{cmd}</code></div>}</div>
      : <div className="row"><span className="dim small">{t("tmr_key_off")}</span><Btn sm onClick={() => rot.mutate()}>{t("tmr_key_make")}</Btn></div>}
    <Err e={q.error || rot.error || off.error} />
  </Card>;
}

function Roles({ tm, cat }: { tm: Team; cat: Catalogue }) {
  const { t } = useT(); const done = useTeamSaved(tm.id);
  const [draft, setDraft] = useState<Record<number, string[]>>({});
  const [menu, setMenu] = useState<number | null>(null); const [adding, setAdding] = useState(false); const [err, setErr] = useState<any>(null); const [saved, setSaved] = useState(false);
  useEffect(() => { setDraft({}); }, [tm]);
  const perms = (r: Role) => draft[r.id] ?? r.perms;
  const dirty = Object.keys(draft).map(Number).filter((id) => { const r = tm.roles.find((x) => x.id === id); return r && [...draft[id]].sort().join() !== [...r.perms].sort().join(); });
  const cols: PermCol[] = tm.roles.map((r) => ({ id: r.id, label: r.label, perms: perms(r), locked: r.locked, lockTip: t("tmr_locked"), dirty: dirty.includes(r.id),
    sub: <>{t("tmr_members_n", { n: r.members })}{r.id === tm.default_role_id ? ` · ${t("tmr_default")}` : ""}</>,
    menu: !r.locked ? <span className="perm-menu"><button className="icon-btn" title={t("tmr_menu")} onClick={() => setMenu(menu === r.id ? null : r.id)}><MoreHorizontal size={14} /></button>
      {menu === r.id && <RoleMenu tm={tm} r={r} onDone={() => { setMenu(null); done(); }} />}</span> : undefined }));
  const saveAll = async () => {
    setErr(null); setSaved(false);
    try { for (const id of dirty) { const r = tm.roles.find((x) => x.id === id)!; await put(`/teams/${tm.id}/roles/${id}`, { label: r.label, description: r.description ?? "", perms: draft[id] }); } setSaved(true); done(); }
    catch (x) { setErr(x); }
  };
  return <div className="stack">
    <div className="muted small">{t("tmr_lead")}</div>
    <PermGrid cat={cat} cols={cols} team grantable={tm.admin ? undefined : tm.own_perms} onChange={(id, p) => { setDraft({ ...draft, [id as number]: p }); setSaved(false); }} />
    {adding && <NewRole tm={tm} onClose={() => { setAdding(false); done(); }} />}
    <Err e={err} />
    <div className="row"><Btn kind="primary" onClick={saveAll} disabled={!dirty.length}>{t("save")}{dirty.length ? ` (${dirty.length})` : ""}</Btn>
      <Btn onClick={() => setAdding(true)}><Plus size={13} /> {t("tmr_role_add")}</Btn>{saved && <span className="dim small">{t("tmr_saved")}</span>}
      {dirty.length > 0 && <span className="dim small">● {t("tmr_unsaved")}</span>}</div>
  </div>;
}

/** A team manager hands out (assigns, makes default, moves members into) only roles within their own permissions; what a member already had may stay. */
const beyond = (tm: Team, perms: string[], keep: string[] = []) => !tm.admin && perms.some((p) => !tm.own_perms.includes(p) && !keep.includes(p));

function RoleMenu({ tm, r, onDone }: { tm: Team; r: Role; onDone: () => void }) {
  const { t } = useT(); const [mode, setMode] = useState<"" | "rename" | "delete">(""); const [label, setLabel] = useState(r.label); const [desc, setDesc] = useState(r.description ?? "");
  const [moveTo, setMoveTo] = useState<string>(""); const [err, setErr] = useState<any>(null);
  const run = async (fn: () => Promise<any>) => { setErr(null); try { await fn(); onDone(); } catch (x) { setErr(x); } };
  return <div className="perm-pop">
    {mode === "" && <div className="stack" style={{ gap: 2 }}>
      <button className="menu-i" onClick={() => setMode("rename")}>{t("tmr_role_rename")}</button>
      {r.template && <button className="menu-i" onClick={() => run(() => post(`/teams/${tm.id}/roles/${r.id}/reset`))}>{t("tmr_role_reset")}</button>}
      {r.id !== tm.default_role_id && !beyond(tm, r.perms) && <button className="menu-i" onClick={() => run(() => post(`/teams/${tm.id}/roles/${r.id}/default`))}>{t("tmr_role_default")}</button>}
      {tm.roles.length > 1 && <button className="menu-i danger" onClick={() => (r.members ? setMode("delete") : run(() => del(`/teams/${tm.id}/roles/${r.id}`)))}>{t("delete")}</button>}</div>}
    {mode === "rename" && <form className="stack" style={{ gap: 6 }} onSubmit={(e) => { e.preventDefault(); run(() => put(`/teams/${tm.id}/roles/${r.id}`, { label, description: desc, perms: r.perms })); }}>
      <input className="input" value={label} onChange={(e) => setLabel(e.target.value)} autoFocus /><input className="input" value={desc} placeholder={t("tmr_role_desc")} onChange={(e) => setDesc(e.target.value)} />
      <Btn sm kind="primary" type="submit">{t("save")}</Btn></form>}
    {mode === "delete" && <div className="stack" style={{ gap: 6 }}><span className="small">{t("tmr_role_move")}</span>
      <select className="input" value={moveTo} onChange={(e) => setMoveTo(e.target.value)}><option value="" />{tm.roles.filter((x) => x.id !== r.id && !x.locked && !beyond(tm, x.perms, r.perms)).map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}</select>
      <Btn sm kind="danger" disabled={!moveTo} onClick={() => run(() => del(`/teams/${tm.id}/roles/${r.id}?move_to=${moveTo}`))}>{t("delete")}</Btn></div>}
    <Err e={err} />
  </div>;
}

function NewRole({ tm, onClose }: { tm: Team; onClose: () => void }) {
  const { t } = useT(); const [label, setLabel] = useState(""); const [from, setFrom] = useState(""); const [err, setErr] = useState<any>(null);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const src = tm.roles.find((r) => String(r.id) === from);
    const perms = src ? src.perms.filter((p) => tm.admin || (tm.own_perms.includes(p) && p !== "team.manage")) : [];
    try { await post(`/teams/${tm.id}/roles`, { label, perms }); onClose(); } catch (x) { setErr(x); }
  };
  return <form className="row card tight" onSubmit={submit}><b className="small">{t("tmr_new_role")}</b>
    <input className="input" style={{ width: 220 }} value={label} onChange={(e) => setLabel(e.target.value)} placeholder={t("tmr_new_role_name")} required autoFocus />
    <span className="dim small">{t("tmr_start_from")}</span><select className="input" style={{ width: 200 }} value={from} onChange={(e) => setFrom(e.target.value)}><option value="">{t("tmr_start_empty")}</option>{tm.roles.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}</select>
    <Btn sm kind="primary" type="submit">{t("tmr_create")}</Btn><Btn sm type="button" onClick={onClose}>✕</Btn><Err e={err} /></form>;
}

function Members({ tm }: { tm: Team }) {
  const { t } = useT(); const { user } = useAuth(); const done = useTeamSaved(tm.id);
  const users = useQuery({ queryKey: ["users"], queryFn: () => get("/users"), enabled: tm.admin });
  const roles = useQuery({ queryKey: ["roles"], queryFn: () => get("/roles"), enabled: tm.admin });
  const gLabel = (n?: string) => (roles.data?.roles ?? []).find((r: any) => r.name === n)?.label || n || "";
  const set = useMutation({ mutationFn: (p: { email: string; role_id: number | null }) => put(`/teams/${tm.id}/members/${encodeURIComponent(p.email)}`, { role_id: p.role_id }), onSuccess: done });
  const rm = useMutation({ mutationFn: (email: string) => del(`/teams/${tm.id}/members/${encodeURIComponent(email)}`), onSuccess: done });
  const mk = useMutation({ mutationFn: (id: number) => patch(`/users/${id}`, { role: "member" }), onSuccess: done });
  const giveable = (r: Role) => tm.admin || (!r.perms.includes("team.manage") && !beyond(tm, r.perms));
  const [add, setAdd] = useState({ email: "", role: String((tm.roles.find((r) => r.id === tm.default_role_id && giveable(r)) ?? tm.roles.find(giveable))?.id ?? "") });
  const idOf = useMemo(() => Object.fromEntries((users.data ?? []).map((u: any) => [u.email, u.id])), [users.data]);
  const manager = (rid: number | null) => !!tm.roles.find((r) => r.id === rid)?.perms.includes("team.manage");
  const free = (users.data ?? []).filter((u: any) => !tm.members.includes(u.email) && u.status === "active");
  return <div className="stack">
    <div className="table-wrap"><table className="table"><thead><tr><th>{t("tmr_tab_members")}</th><th>{t("tmr_member_role")}</th>{tm.admin && <th>{t("tmr_global_role")}</th>}<th /></tr></thead>
      <tbody>{tm.memberships.map((m) => { const self = m.email === user?.email; const lock = !tm.admin && (self || manager(m.role_id));
        return <tr key={m.email}>
          <td><b>{m.name}</b><div className="dim small">{m.email}</div></td>
          <td><select className="input" style={{ width: 200, padding: "4px 8px" }} value={m.role_id ?? ""} disabled={lock} title={lock ? t("tmr_locked") : undefined}
            onChange={(e) => set.mutate({ email: m.email, role_id: Number(e.target.value) })}>{tm.roles.map((r) => <option key={r.id} value={r.id} disabled={!tm.admin && (r.perms.includes("team.manage") || beyond(tm, r.perms, tm.roles.find((x) => x.id === m.role_id)?.perms))}>{r.label}</option>)}</select></td>
          {tm.admin && <td><div className="row" style={{ gap: 6 }}><span className="chip small">{gLabel(m.global_role)}</span>
            {!!m.wider?.length && <span className="badge amber" style={{ textTransform: "none" }} title={t("tmr_wider_tip", { list: m.wider.join(", ") })}>{t("tmr_wider")}</span>}
            {m.global_role && m.global_role !== "member" && m.global_role !== "admin" && idOf[m.email] && !self && <Btn sm onClick={() => mk.mutate(idOf[m.email])}>{t("tmr_make_member")}</Btn>}</div></td>}
          <td>{!lock && <Confirm onConfirm={() => rm.mutate(m.email)}>{t("tmr_remove")}</Confirm>}</td></tr>; })}</tbody></table>
      {!tm.memberships.length && <Empty />}</div>
    <form className="row" onSubmit={(e) => { e.preventDefault(); if (add.email) set.mutate({ email: add.email.trim(), role_id: add.role ? Number(add.role) : null }, { onSuccess: () => setAdd({ ...add, email: "" }) }); }}>
      {tm.admin ? <select className="input" style={{ width: 280 }} value={add.email} onChange={(e) => setAdd({ ...add, email: e.target.value })}><option value="">{t("tmr_pick_user")}</option>
        {free.map((u: any) => <option key={u.email} value={u.email}>{u.name ? `${u.name} · ${u.email}` : u.email}</option>)}</select>
        : <input className="input" style={{ width: 280 }} type="email" value={add.email} onChange={(e) => setAdd({ ...add, email: e.target.value })} placeholder={t("tmr_add_email")} />}
      <select className="input" style={{ width: 200 }} value={add.role} onChange={(e) => setAdd({ ...add, role: e.target.value })}>{tm.roles.filter(giveable).map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}</select>
      <Btn kind="primary" type="submit" disabled={!add.email}>{t("tmr_add_member")}</Btn></form>
    <Err e={set.error || rm.error || mk.error} />
  </div>;
}
