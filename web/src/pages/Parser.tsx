import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { del, get, patch, post } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Badge, Btn, Card, Chip, Confirm, Err, Field, Modal, Table, Tabs } from "../components/ui";

/** Parser v2 step 3: grok library (built-in packs + user patterns), a test box, and the lines a dataset could not parse. */
type Tab = "lib" | "test" | "unparsed" | "templates" | "profiles" | "base";
const ROLES = ["timestamp", "severity", "host", "service", "message", "status"];
const EMPTY = { name: "", pattern: "", family: "", description: "", roles: {} as Record<string, string>, enabled: true };

export default function ParserPage() {
  const { t } = useT(); const { can } = useAuth(); const qc = useQueryClient(); const [sp] = useSearchParams();
  const [tab, setTab] = useState<Tab>((sp.get("tab") as Tab) || "lib"); const [q, setQ] = useState(""); const [edit, setEdit] = useState<any>(null);
  const [sample, setSample] = useState(sp.get("sample") ?? ""); const [pattern, setPattern] = useState(sp.get("pattern") ?? "%{TIMESTAMP_ISO8601:timestamp} %{LOGLEVEL:severity} %{GREEDYDATA:message}"); const [res, setRes] = useState<any>(null);
  const list = useQuery({ queryKey: ["grok"], queryFn: () => get("/grok") });
  const base = useQuery({ queryKey: ["grok-base"], queryFn: () => get("/grok/base"), enabled: tab === "base" });
  const inv = () => qc.invalidateQueries({ queryKey: ["grok"] });
  const toggle = useMutation({ mutationFn: (p: any) => patch(`/grok/${p.id}`, { enabled: !p.enabled }), onSuccess: inv });
  const rm = useMutation({ mutationFn: (id: string) => del(`/grok/${id}`), onSuccess: inv });
  const run = async () => { setRes(await post("/grok/test", { pattern, sample })); };
  const editable = can("act.parser");
  const rows: any[] = (list.data ?? []).filter((r: any) => !q || r.name.toLowerCase().includes(q.toLowerCase()) || r.family.includes(q.toLowerCase()) || (r.description ?? "").toLowerCase().includes(q.toLowerCase()));
  const users = (list.data ?? []).filter((r: any) => !r.builtin).length;
  return <div className="stack">
    <div><h2>🧩 {t("nav_parser")}</h2><div className="muted small">{t("gk_tag")}</div></div>
    <div className="chips"><Chip color="#60a5fa">{(list.data ?? []).length - users} {t("gk_builtin")}</Chip><Chip color="#2dd4bf">{users} {t("gk_user")}</Chip></div>
    <div className="muted small">{t("gk_intro")}</div>
    <Tabs value={tab} onChange={setTab} tabs={[{ k: "lib", label: t("gk_tab_lib") }, { k: "test", label: t("gk_tab_test") }, { k: "unparsed", label: t("gk_tab_unparsed") }, { k: "templates", label: `🧠 ${t("gk_tab_templates")}` }, { k: "profiles", label: t("gk_tab_profiles") }, { k: "base", label: t("gk_tab_base") }]} />
    {tab === "lib" && <Card right={editable ? <Btn kind="primary" sm onClick={() => setEdit({ ...EMPTY })}>➕ {t("gk_add")}</Btn> : undefined}>
      <div className="row" style={{ marginBottom: 8 }}><input className="input grow" placeholder={t("gk_filter_pat")} value={q} onChange={(e) => setQ(e.target.value)} /></div>
      <Table cols={[{ k: "name", label: t("name"), render: (r) => <span><b className="mono">{r.name}</b>{!r.builtin && <span className="badge blue" style={{ marginLeft: 6 }}>{t("gk_user_one")}</span>}<div className="muted small">{r.description}</div></span> },
        { k: "family", label: t("gk_family"), render: (r) => <Badge v={r.family} /> }, { k: "roles", label: t("gk_roles"), render: (r) => <span className="small mono">{Object.entries(r.roles ?? {}).map(([k, v]) => `${k}←${v}`).join(" ")}</span> },
        { k: "corpus", label: t("gk_corpus"), render: (r) => <span className="muted small">{r.corpus.files} {t("file").toLowerCase()} · {r.corpus.lines} {t("gk_lines")}</span> },
        { k: "enabled", label: t("src_enabled"), render: (r) => <input type="checkbox" checked={!!r.enabled} disabled={!editable} onChange={() => toggle.mutate(r)} /> },
        { k: "x", label: "", render: (r) => <div className="row"><Btn sm onClick={() => { setPattern(r.pattern); setTab("test"); }}>🧪</Btn>{editable && (r.builtin ? <Btn sm onClick={() => setEdit({ ...r, name: r.name + "-custom", builtin: false, id: undefined })}>📋</Btn> : <><Btn sm onClick={() => setEdit(r)}>✏️</Btn><Confirm onConfirm={() => rm.mutate(r.id)}>🗑</Confirm></>)}</div> }]} rows={rows.map((r) => ({ ...r, id: r.id }))} />
      <Err e={toggle.error || rm.error} /></Card>}
    {tab === "test" && <Card title={t("gk_tab_test")}>
      <div className="stack"><Field label={t("gk_pattern")}><textarea className="input mono" style={{ minHeight: 56 }} value={pattern} onChange={(e) => setPattern(e.target.value)} /></Field>
        <Field label={t("gk_sample")}><textarea className="input mono" style={{ minHeight: 120 }} value={sample} onChange={(e) => setSample(e.target.value)} placeholder="2026-09-22 09:15:03 ERROR payment-api gateway timeout order=88213" /></Field>
        <div className="row"><Btn kind="primary" onClick={run} disabled={!pattern || !sample}>▶ {t("gk_run")}</Btn>{res?.ok && editable && <Btn onClick={() => setEdit({ ...EMPTY, pattern, roles: Object.fromEntries(ROLES.filter((r) => res.fields.includes(r)).map((r) => [r, r])) })}>💾 {t("gk_save_as")}</Btn>}</div>
        {res && !res.ok && <div className="err">{res.error}</div>}
        {res?.ok && <><div className={res.matched === res.total ? "ok" : "err"}>{t("gk_matched", { m: res.matched, n: res.total })} · {t("gk_fields")}: {res.fields.join(", ") || "-"}</div>
          <Table cols={[{ k: "line", label: t("gk_line"), render: (r) => <span className="mono small" style={{ color: r.fields ? undefined : "var(--red)" }}>{r.line.slice(0, 140)}</span> }, { k: "fields", label: t("gk_fields"), render: (r) => r.fields ? <span className="small">{Object.entries(r.fields).map(([k, v]) => <span key={k} className="chip" style={{ marginRight: 4 }}><b>{k}</b>={String(v).slice(0, 40)}</span>)}</span> : <span className="badge red">{t("gk_nomatch")}</span> }]} rows={res.lines.map((r: any, i: number) => ({ id: i, ...r }))} /></>}
      </div></Card>}
    {tab === "unparsed" && <Unparsed onUse={(s: string) => { setSample(s); setTab("test"); }} />}
    {tab === "templates" && <Templates editable={editable} onUse={(p: string, s: string) => { setPattern(p); setSample(s); setTab("test"); }} />}
    {tab === "profiles" && <Profiles editable={editable} />}
    {tab === "base" && <Card title={t("gk_tab_base")}><div className="muted small" style={{ marginBottom: 8 }}>{t("gk_base_hint")}</div>
      <Table cols={[{ k: "name", label: t("name"), render: (r) => <span className="mono">%{"{"}{r.name}{"}"}</span> }, { k: "pattern", label: t("gk_pattern"), render: (r) => <span className="mono small dim">{r.pattern.slice(0, 120)}</span> }]} rows={(base.data ?? []).map((r: any) => ({ id: r.name, ...r }))} /></Card>}
    {edit && <PatternForm rec={edit} onClose={() => { setEdit(null); inv(); }} />}
  </div>;
}

function PatternForm({ rec, onClose }: { rec: any; onClose: () => void }) {
  const { t } = useT(); const [f, setF] = useState<any>({ ...rec }); const [err, setErr] = useState<any>(null);
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  const setRole = (r: string) => (e: any) => setF({ ...f, roles: { ...f.roles, [r]: e.target.value } });
  const save = async (e: React.FormEvent) => { e.preventDefault(); setErr(null); const body = { name: f.name, pattern: f.pattern, family: f.family, description: f.description, roles: Object.fromEntries(Object.entries(f.roles ?? {}).filter(([, v]) => v)), enabled: f.enabled ?? true };
    try { if (f.id) await patch(`/grok/${f.id}`, body); else await post("/grok", body); onClose(); } catch (x) { setErr(x); } };
  return <Modal wide title={f.id ? `✏️ ${f.name}` : `➕ ${t("gk_add")}`} onClose={onClose}><form className="stack" onSubmit={save}>
    <div className="grid k3"><Field label={t("name")}><input className="input mono" value={f.name} onChange={set("name")} required placeholder="MY_APP_LINE" /></Field><Field label={t("gk_family")} hint={t("gk_family_hint")}><input className="input" value={f.family} onChange={set("family")} placeholder="my-app" /></Field><Field label={t("gk_desc")}><input className="input" value={f.description ?? ""} onChange={set("description")} /></Field></div>
    <Field label={t("gk_pattern")} hint={t("gk_pattern_hint")}><textarea className="input mono" style={{ minHeight: 70 }} value={f.pattern} onChange={set("pattern")} required /></Field>
    <div className="muted small">{t("gk_roles_hint")}</div>
    <div className="grid k3">{ROLES.map((r) => <Field key={r} label={r}><input className="input mono" value={f.roles?.[r] ?? ""} onChange={setRole(r)} placeholder={r} /></Field>)}</div>
    <Err e={err} /><div className="row"><Btn kind="primary" type="submit">💾 {t("save")}</Btn></div></form></Modal>;
}

function Unparsed({ onUse }: { onUse: (s: string) => void }) {
  const { t } = useT(); const [key, setKey] = useState("");
  const ds = useQuery({ queryKey: ["datasets"], queryFn: () => get("/datasets") });
  const u = useQuery({ queryKey: ["unparsed", key], queryFn: () => get(`/grok/unparsed/${key}`), enabled: !!key });
  const d = u.data;
  return <Card title={t("gk_tab_unparsed")}>
    <div className="muted small" style={{ marginBottom: 8 }}>{t("gk_unparsed_hint")}</div>
    <div className="row" style={{ marginBottom: 8 }}><select className="input" value={key} onChange={(e) => setKey(e.target.value)}><option value="">{t("gk_pick_ds")}</option>{(ds.data ?? []).map((x: any) => <option key={x.id} value={x.id}>{x.name}</option>)}</select>
      {d && <span className="muted small">{d.unparsed.toLocaleString()} / {d.total.toLocaleString()} {t("ds_events")} · {d.groups.length} {t("gk_groups")}</span>}</div>
    {d && !d.groups.length && <div className="ok">{t("gk_all_parsed")}</div>}
    {d && d.groups.length > 0 && <Table cols={[{ k: "count", label: t("count"), num: true }, { k: "source", label: t("file") }, { k: "parser", label: t("sys_cov_format") }, { k: "sample", label: t("gk_sample"), render: (r) => <span className="mono small">{r.sample.slice(0, 160)}</span> },
      { k: "flags", label: "", render: (r) => <span className="small">{r.no_ts ? <span className="badge amber">{t("gk_no_ts")}</span> : null} {r.no_level ? <span className="badge">{t("gk_no_level")}</span> : null}</span> },
      { k: "x", label: "", render: (r) => <Btn sm onClick={() => onUse(r.sample)}>🧪 {t("gk_use")}</Btn> }]} rows={d.groups.map((g: any, i: number) => ({ id: i, ...g }))} />}
  </Card>;
}

function Templates({ editable, onUse }: { editable: boolean; onUse: (pattern: string, sample: string) => void }) {
  const { t } = useT(); const qc = useQueryClient(); const [q, setQ] = useState("");
  const d = useQuery({ queryKey: ["drain", q], queryFn: () => get(`/grok/templates?q=${encodeURIComponent(q)}&limit=200`), refetchInterval: 30000 });
  const propose = useMutation({ mutationFn: (id: number) => post(`/grok/templates/${id}/propose`), onSuccess: (r: any) => onUse(r.pattern, r.sample) });
  const forget = useMutation({ mutationFn: (id: number) => del(`/grok/templates/${id}`), onSuccess: () => qc.invalidateQueries({ queryKey: ["drain"] }) });
  const st = d.data?.stats;
  return <Card title={`🧠 ${t("gk_tab_templates")}`} right={st && <span className="muted small">{st.clusters} {t("gk_tpl_clusters")} · {st.events.toLocaleString()} {t("ds_events")}</span>}>
    <div className="muted small" style={{ marginBottom: 8 }}>{t("gk_tpl_hint")}</div>
    <div className="row" style={{ marginBottom: 8 }}><input className="input grow" placeholder={t("gk_filter_tpl")} value={q} onChange={(e) => setQ(e.target.value)} /></div>
    <Table cols={[{ k: "count", label: t("count"), num: true, render: (r) => r.count.toLocaleString() }, { k: "template", label: t("gk_tpl_template"), render: (r) => <span className="mono small">{r.template.slice(0, 150)}</span> },
      { k: "vars", label: t("gk_tpl_vars"), num: true }, { k: "last_seen", label: t("time"), render: (r) => <span className="dim small">{(r.last_seen || "").slice(0, 16)}</span> },
      { k: "x", label: "", render: (r) => <div className="row"><Btn sm onClick={() => propose.mutate(r.id)}>🧪 {t("gk_tpl_propose")}</Btn>{editable && <Confirm onConfirm={() => forget.mutate(r.id)}>{t("gk_tpl_forget")}</Confirm>}</div> }]} rows={d.data?.rows ?? []} />
    <Err e={propose.error || forget.error} /></Card>;
}

function Profiles({ editable }: { editable: boolean }) {
  const { t } = useT(); const qc = useQueryClient(); const [edit, setEdit] = useState<any>(null);
  const d = useQuery({ queryKey: ["profiles"], queryFn: () => get("/grok/profiles") });
  const rm = useMutation({ mutationFn: (sig: string) => del(`/grok/profiles/${sig}`), onSuccess: () => qc.invalidateQueries({ queryKey: ["profiles"] }) });
  const save = useMutation({ mutationFn: (p: any) => patch(`/grok/profiles/${p.id}`, { mapping: p.mapping, name: p.name }), onSuccess: () => { setEdit(null); qc.invalidateQueries({ queryKey: ["profiles"] }); } });
  const rows: any[] = d.data ?? [];
  return <Card title={t("gk_tab_profiles")}>
    <div className="muted small" style={{ marginBottom: 8 }}>{t("gk_prof_hint")}</div>
    {!rows.length ? <div className="muted small">{t("gk_prof_none")}</div> : <Table cols={[{ k: "name", label: t("name"), render: (r) => <span><b>{r.name || r.id}</b><div className="dim small">{r.format} · <span className={`badge ${r.source === "user" ? "blue" : ""}`}>{r.source === "user" ? t("gk_prof_source_user") : t("gk_prof_source_auto")}</span> · {r.hits} {t("gk_prof_hits")}</div></span> },
      { k: "keys", label: t("gk_prof_keys"), render: (r) => <span className="mono small">{(r.keys ?? []).slice(0, 12).join(", ")}{(r.keys ?? []).length > 12 ? " …" : ""}</span> },
      { k: "mapping", label: t("gk_prof_roles"), render: (r) => <span className="small mono">{Object.entries(r.mapping ?? {}).map(([k, v]) => `${k}←${v}`).join(" ")}</span> },
      { k: "x", label: "", render: (r) => editable ? <div className="row"><Btn sm onClick={() => setEdit({ ...r, mapping: { ...r.mapping } })}>✏️</Btn><Confirm onConfirm={() => rm.mutate(r.id)}>🗑</Confirm></div> : null }]} rows={rows} />}
    {edit && <Modal title={`✏️ ${edit.name || edit.id}`} onClose={() => setEdit(null)}><div className="stack">
      <Field label={t("name")}><input className="input" value={edit.name ?? ""} onChange={(e) => setEdit({ ...edit, name: e.target.value })} /></Field>
      <div className="grid k2">{["timestamp", "severity", "service", "host", "message", "environment", "origin"].map((role) => <Field key={role} label={role}><select className="input" value={edit.mapping?.[role] ?? ""} onChange={(e) => setEdit({ ...edit, mapping: { ...edit.mapping, [role]: e.target.value } })}><option value="">-</option>{(edit.keys ?? []).map((k: string) => <option key={k} value={k}>{k}</option>)}</select></Field>)}</div>
      <Err e={save.error} /><div className="row"><Btn kind="primary" onClick={() => save.mutate(edit)}>💾 {t("save")}</Btn></div></div></Modal>}
    <Err e={rm.error} /></Card>;
}
