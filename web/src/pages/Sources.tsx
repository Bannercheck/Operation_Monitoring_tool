import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { del, get, patch, post } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Btn, Card, Chip, Confirm, Err, Field, Table, fmtTs } from "../components/ui";

/** Pull sources: Elasticsearch / OpenSearch, Grafana Loki, Splunk, Graylog, HTTP. Same page as the Streamlit "Kaynaklar": intro,
 *  catalogue (what each platform needs, which logs come through), add form with test-before-save and advanced fields, live list. */
const ENVS = ["", "prod", "staging", "test", "dev", "qa", "dr"];
const EMPTY = { name: "", kind: "elasticsearch", url: "", selector: "", auth: "none", user: "", secret: "", interval: 30, env: "prod", site: "", enabled: true, verify_tls: true, headers: "", ts_field: "", lookback_min: 15 };

export default function SourcesPage() {
  const { t } = useT();
  return <div className="stack"><div><h2>🔎 {t("src_page")}</h2><div className="muted small">{t("src_page_tag")}</div></div><SourcesPanel /></div>;
}

export function SourcesPanel() {
  const { t } = useT(); const { can } = useAuth(); const qc = useQueryClient(); const [msg, setMsg] = useState<any>(null); const [add, setAdd] = useState(false);
  const list = useQuery({ queryKey: ["sources"], queryFn: () => get("/sources"), refetchInterval: 10000 });
  const kinds = useQuery({ queryKey: ["source-kinds"], queryFn: () => get("/sources/kinds") });
  const inv = () => qc.invalidateQueries({ queryKey: ["sources"] });
  const op = useMutation({ mutationFn: (p: { id: number; op: string; body?: any }) => (p.op === "delete" ? del(`/sources/${p.id}`) : p.op === "patch" ? patch(`/sources/${p.id}`, p.body) : post(`/sources/${p.id}/${p.op}`)),
    onSuccess: (r: any, p) => { if (p.op === "test" || p.op === "poll") setMsg(r); inv(); } });
  const editable = can("act.sources"); const rows: any[] = list.data ?? []; const K: Record<string, any> = kinds.data ?? {};
  const on = rows.filter((r) => r.enabled).length, errs = rows.filter((r) => r.last_err).length, ev = rows.reduce((a, r) => a + (r.events ?? 0), 0);
  const fresh = (r: any) => !!r.last_ok && Date.now() - new Date(r.last_ok).getTime() < Math.max(r.interval * 3, 120) * 1000;
  return <div className="stack">
    <div className="chips"><Chip color="#60a5fa">{rows.length} {t("src_n")}</Chip><Chip color="#2dd4bf">{on} {t("src_active")}</Chip><Chip color={errs ? "#f87171" : "#64748b"}>{errs} {t("src_err")}</Chip><Chip color="#a78bfa">{ev.toLocaleString()} {t("ds_events")}</Chip></div>
    <div className="muted small">{t("src_intro")}</div>
    <details className="card" open={!rows.length}><summary style={{ cursor: "pointer", fontWeight: 600 }}>📚 {t("src_catalog")}</summary>
      <div className="grid k3" style={{ marginTop: 10 }}>{Object.entries(K).map(([k, m]: any) => <div key={k} className="card cat"><div className="cat-t">{m.icon} {m.label} <span className="mono dim">:{m.port}</span></div>
        <div className="cat-b"><b>{t("src_needs")}</b><br />{t("src_needs_" + k)}</div><div className="cat-b"><b>{t("src_logs")}</b><br />{t("src_logs_" + k)}</div></div>)}</div>
      <div className="muted small" style={{ marginTop: 8 }}>{t("src_catalog_note")}</div></details>
    {editable && !add && <div className="row"><Btn kind="primary" onClick={() => setAdd(true)}>➕ {t("src_add")}</Btn></div>}
    {add && <NewSource kinds={K} onClose={() => { setAdd(false); inv(); }} />}
    {msg && <div className={msg.ok === false ? "err" : "ok"}>{msg.message ?? `${msg.events} ${t("ds_events")}`}</div>}
    {msg?.sample?.length > 0 && <Card><Table cols={["ts", "level", "host", "service", "msg"].map((k) => ({ k, label: k, render: (r: any) => <span className={k === "msg" ? "mono" : ""}>{String(r[k] ?? "")}</span> }))} rows={msg.sample.map((r: any, i: number) => ({ id: i, ...r }))} /></Card>}
    <Card title={t("src_list")}>{!rows.length ? <div className="muted small">{t("src_none")}</div> : <Table cols={[
      { k: "name", label: t("name"), render: (r) => { const dot = !r.enabled ? "#64748b" : r.last_err ? "#f87171" : fresh(r) ? "#2dd4bf" : "#fbbf24"; const st = !r.enabled ? t("src_off") : r.last_err ? t("src_failing") : fresh(r) ? t("ag_online") : t("ag_waiting");
        return <span><span style={{ display: "inline-block", width: 8, height: 8, borderRadius: 4, background: dot, marginRight: 6, boxShadow: `0 0 0 3px ${dot}33` }} /><b>{r.name}</b> <span className="muted small">· {st}</span>{r.last_err && <div className="small" style={{ color: "var(--red)" }}>⚠ {r.last_err.slice(0, 160)}</div>}</span>; } },
      { k: "kind", label: t("src_kind"), render: (r) => <span>{K[r.kind]?.icon} {K[r.kind]?.label ?? r.kind}</span> },
      { k: "url", label: t("src_url"), render: (r) => <span className="mono small">{r.url.slice(0, 48)} <span className="dim">{r.selector.slice(0, 30)}</span></span> },
      { k: "interval", label: t("src_interval"), render: (r) => <span className="muted small">{r.interval}s · {r.env || "-"}{r.site ? ` · ${r.site}` : ""}</span> },
      { k: "last_ok", label: t("status"), render: (r) => <span className="muted small">{r.last_ok ? fmtTs(r.last_ok) : "-"} · {(r.events ?? 0).toLocaleString()} {t("ds_events")} · {r.polls ?? 0} {t("src_polls")}</span> },
      { k: "x", label: "", render: (r) => editable ? <div className="row"><Btn sm title={t("src_toggle")} onClick={() => op.mutate({ id: r.id, op: "patch", body: { enabled: !r.enabled, last_err: "" } })}>{r.enabled ? "⏸" : "▶"}</Btn><Btn sm onClick={() => op.mutate({ id: r.id, op: "test" })}>🧪 {t("src_test")}</Btn><Btn sm onClick={() => op.mutate({ id: r.id, op: "poll" })}>{t("src_poll")}</Btn><Confirm onConfirm={() => op.mutate({ id: r.id, op: "delete" })}>🗑</Confirm></div> : null }]} rows={rows} />}</Card>
    <Err e={op.error} />
  </div>;
}

function NewSource({ kinds, onClose }: { kinds: Record<string, any>; onClose: () => void }) {
  const { t } = useT(); const [f, setF] = useState<any>(EMPTY); const [err, setErr] = useState<any>(null); const [res, setRes] = useState<any>(null); const [busy, setBusy] = useState(false);
  const k = kinds[f.kind] ?? {}; const set = (key: string) => (e: any) => setF({ ...f, [key]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const body = () => ({ ...f, interval: Number(f.interval), lookback_min: Number(f.lookback_min) });
  const test = async () => { setErr(null); setBusy(true); try { setRes(await post("/sources/test", body())); } catch (x) { setErr(x); } finally { setBusy(false); } };
  const save = async (e: React.FormEvent) => { e.preventDefault(); setErr(null); try { await post("/sources", body()); onClose(); } catch (x) { setErr(x); } };
  const auths: string[] = k.auth ?? ["none", "basic", "bearer"];
  return <Card title={`➕ ${t("src_add")}`} right={<Btn sm onClick={onClose}>✕</Btn>}><form className="stack" onSubmit={save}>
    <div className="grid k3"><Field label={t("src_kind")}><select className="input" value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value, auth: (kinds[e.target.value]?.auth ?? ["none"])[0] })}>{Object.entries(kinds).map(([key, v]: any) => <option key={key} value={key}>{v.icon} {v.label}</option>)}</select></Field>
      <Field label={t("src_name")}><input className="input" value={f.name} onChange={set("name")} placeholder={`${f.kind}-prod`} required /></Field>
      <Field label={t("src_url")} hint={t("src_url_help")}><input className="input" value={f.url} onChange={set("url")} placeholder={k.path_hint ?? (k.port ? `https://host:${k.port}` : "")} required /></Field></div>
    <Field label={k.selector ?? t("src_selector")} hint={t("src_sel_help_" + f.kind)}><input className="input" value={f.selector} onChange={set("selector")} placeholder={k.selector_ex ?? ""} /></Field>
    <div className="grid k4"><Field label={t("src_auth")}><select className="input" value={f.auth} onChange={set("auth")}>{auths.map((a) => <option key={a} value={a}>{t("src_auth_" + a)}</option>)}</select></Field>
      <Field label={t("src_user")}><input className="input" value={f.user} onChange={set("user")} disabled={f.auth !== "basic"} /></Field>
      <Field label={t("src_secret")} hint={t("src_secret_help")}><input className="input" type="password" value={f.secret} onChange={set("secret")} disabled={f.auth === "none"} /></Field>
      <Field label={t("src_interval")}><input className="input" type="number" min={5} max={3600} value={f.interval} onChange={set("interval")} /></Field></div>
    <div className="grid k3"><Field label={t("env")}><select className="input" value={f.env} onChange={set("env")}>{ENVS.map((e) => <option key={e} value={e}>{e || "-"}</option>)}</select></Field>
      <Field label={t("ag_site")}><input className="input" value={f.site} onChange={set("site")} placeholder="IST-DC1" /></Field>
      <label className="check" style={{ marginTop: 22 }}><input type="checkbox" checked={f.verify_tls} onChange={set("verify_tls")} /> {t("src_verify_tls")}</label></div>
    <details><summary className="muted small" style={{ cursor: "pointer" }}>{t("src_advanced")}</summary><div className="grid k3" style={{ marginTop: 8 }}>
      <Field label={t("src_lookback")}><input className="input" type="number" min={1} max={1440} value={f.lookback_min} onChange={set("lookback_min")} /></Field>
      <Field label={t("src_ts_field")}><input className="input" value={f.ts_field} onChange={set("ts_field")} placeholder="@timestamp" /></Field>
      <Field label={t("src_headers")}><textarea className="input mono" style={{ minHeight: 56 }} value={f.headers} onChange={set("headers")} placeholder="X-Scope-OrgID: tenant-1" /></Field></div></details>
    {res && <div className={res.ok ? "ok" : "err"}>{res.message}</div>}
    {res?.sample?.length > 0 && <Table cols={["ts", "level", "host", "service", "msg"].map((c) => ({ k: c, label: c, render: (r: any) => <span className={c === "msg" ? "mono" : ""}>{String(r[c] ?? "")}</span> }))} rows={res.sample.map((r: any, i: number) => ({ id: i, ...r }))} />}
    <Err e={err} />
    <div className="row"><Btn type="button" onClick={test} disabled={busy || !f.url}>🧪 {t("src_test")}</Btn><Btn kind="primary" type="submit" disabled={!f.url || !f.name}>💾 {t("src_save")}</Btn></div>
  </form></Card>;
}
