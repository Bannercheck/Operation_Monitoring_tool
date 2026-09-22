import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, del, get, post, put } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Btn, Card, Collapsible, Confirm, Err, Field, Kpi, Table, Tabs, fmtTs } from "../components/ui";
import { useSearchParams } from "react-router-dom";
import Notify from "./Notify";
import Readme from "./Readme";

export default function System() {
  const { t } = useT(); const qc = useQueryClient(); const { can } = useAuth();
  const st = useQuery({ queryKey: ["sys-status"], queryFn: () => get("/system/status"), refetchInterval: 15000 });
  const snaps = useQuery({ queryKey: ["snapshots"], queryFn: () => get("/system/snapshots") });
  const rel = useQuery({ queryKey: ["releases"], queryFn: () => get("/system/releases") });
  const inv = () => { qc.invalidateQueries({ queryKey: ["snapshots"] }); qc.invalidateQueries({ queryKey: ["sys-status"] }); };
  const snap = useMutation({ mutationFn: () => post("/system/snapshots", { reason: "manual", code: true, db: true }), onSuccess: inv });
  const rmSnap = useMutation({ mutationFn: (id: string) => del(`/system/snapshots/${id}`), onSuccess: inv });
  const vacuum = useMutation({ mutationFn: () => post("/system/vacuum"), onSuccess: inv });
  const rollback = useMutation({ mutationFn: (id: string) => post(`/system/snapshots/${id}/rollback`), onSuccess: inv });
  const restart = useMutation({ mutationFn: () => post("/system/restart") });
  const upd = useQuery({ queryKey: ["sys-update"], queryFn: () => get("/system/update") });
  const s = st.data; const v = s?.version ?? {}; const db = s?.database ?? {};
  const [sp, setSp] = useSearchParams(); const tab = (sp.get("tab") as "status" | "notify" | "readme") || "status";
  const setTab = (k: string) => setSp(k === "status" ? {} : { tab: k });
  const tabs = [{ k: "status", label: t("sys_tab_status") }, ...(can("sys.notify") ? [{ k: "notify", label: `🔔 ${t("sys_tab_notify")}` }] : []), ...(can("page.readme") ? [{ k: "readme", label: `📖 ${t("sys_tab_readme")}` }] : [])];
  if (tab === "notify") return <div className="stack"><div className="row between"><h2>🛠 {t("sys_title")}</h2></div><Tabs value={tab} onChange={setTab} tabs={tabs as any} /><Notify /></div>;
  if (tab === "readme") return <div className="stack"><div className="row between"><h2>🛠 {t("sys_title")}</h2></div><Tabs value={tab} onChange={setTab} tabs={tabs as any} /><Readme /></div>;
  return <div className="stack">
    <div className="row between"><h2>🛠 {t("sys_title")}</h2><span className="chip">Watchover v{v.version} · {v.git} · {v.docker ? "🐳" : v.platform}</span></div>
    <Tabs value={tab} onChange={setTab} tabs={tabs as any} />
    <div className="grid k4">
      <Kpi value={`v${v.version ?? "-"}`} label={t("sys_version")} sub={v.git} />
      <Kpi value={db.size_mb != null ? `${db.size_mb} MB` : "-"} label={t("sys_db")} accent="#a78bfa" sub={`${db.backend} · ${Object.keys(db.tables ?? {}).length} ${t("sys_tables")}`} />
      <Kpi value={s ? `${Math.floor(s.uptime_s / 3600)}h ${Math.floor((s.uptime_s % 3600) / 60)}m` : "-"} label={t("sys_uptime")} accent="#60a5fa" sub={`${s?.datasets ?? 0} ${t("ds_title").toLowerCase()}`} />
      <Kpi value={s?.live?.received?.toLocaleString() ?? 0} label={t("ops_events15")} accent="#2dd4bf" sub={`${s?.live?.agents ?? 0} ${t("ops_agents")} · ${s?.anomalies?.open ?? 0} ${t("ops_anom_open")}`} />
    </div>
    <div className="grid k2 top">
      <Collapsible id="sys-services" title={t("sys_services")}><div className="stack small">{Object.entries(s?.services ?? {}).map(([k, val]: any) => <div key={k} className="row"><span className="chip"><span className="d" style={{ background: val ? "#34d399" : "#64748b" }} />{k}</span><span className="muted">{String(val)}</span></div>)}
        <div className="row"><span className="chip"><span className="d" style={{ background: s?.live?.receiver ? "#34d399" : "#64748b" }} />receiver</span><span className="chip"><span className="d" style={{ background: s?.live?.simulator ? "#fbbf24" : "#64748b" }} />simulator</span></div>
        <div className="mono dim">{db.url}</div></div>
        <div className="row" style={{ marginTop: 10 }}><Btn sm onClick={() => vacuum.mutate()}>{t("sys_vacuum")}</Btn></div><Err e={vacuum.error} /></Collapsible>
      <Capacity c={s?.capacity} />
      <DbTables tables={db.tables ?? {}} />
    </div>
    <ParserCoverage />
    <Collapsible id="sys-snapshots" title={t("sys_snapshots")} right={<Btn sm kind="primary" onClick={() => snap.mutate()} disabled={snap.isPending}>📸 {t("sys_snap_new")}</Btn>}>
      <Table cols={[{ k: "id", label: "ID", render: (r) => <b className="mono">{r.id}</b> }, { k: "version", label: t("sys_version"), render: (r) => `v${r.version} · ${r.git}` }, { k: "ts", label: t("time"), render: (r) => fmtTs(r.ts) }, { k: "reason", label: t("note") }, { k: "size", label: "MB", num: true, render: (r) => (r.size / 1e6).toFixed(1) }, { k: "dbs", label: t("sys_db"), render: (r) => (r.dbs ?? []).join(", ") },
        { k: "x", label: "", render: (r) => <div className="row"><Confirm kind="" onConfirm={() => rollback.mutate(r.id)}>↩ {t("sys_rollback")}</Confirm><Confirm onConfirm={() => rmSnap.mutate(r.id)}>{t("delete")}</Confirm></div> }]} rows={snaps.data ?? []} /><Err e={snap.error || rollback.error} />{rollback.isSuccess && <div className="ok">{t("sys_restarting")}</div>}</Collapsible>
    <div className="grid k2 top">
      <Collapsible id="sys-update" title={`⬆ ${t("sys_update")}`} right={<Confirm kind="" onConfirm={() => restart.mutate()}>{t("sys_restart")}</Confirm>}><p className="muted small" style={{ marginTop: 0 }}>{upd.data?.docker ? t("sys_update_docker") : ""}</p><pre className="code">{(upd.data?.commands ?? []).join("\n")}</pre>{restart.isSuccess && <div className="ok">{t("sys_restarting")}</div>}</Collapsible>
      {can("sys.maint") && <Settings />}
    </div>
    {can("sys.maint") && <Maintenance docker={!!upd.data?.docker} />}
    {can("sys.auth") && <SignInProviders />}
    {!!rel.data?.length && <Collapsible id="sys-releases" title={t("sys_releases")} defaultOpen={false}><div className="stack small">{rel.data.slice(0, 6).map((r: any) => <div key={r.version}><b>v{r.version}</b> <span className="muted">{r.date} · {r.kind}</span><ul style={{ margin: "2px 0 0", paddingLeft: 18 }}>{(r.lines ?? []).map((n: string, i: number) => <li key={i}>{n}</li>)}</ul></div>)}</div></Collapsible>}
  </div>;
}


const PROVIDERS = [
  { name: "google", label: "Google", flag: "auth_google", fields: [["google_client_id", "sso_client_id"], ["google_client_secret", "sso_client_secret"]] },
  { name: "microsoft", label: "Microsoft", flag: "auth_microsoft", fields: [["ms_tenant", "sso_tenant"], ["ms_client_id", "sso_client_id"], ["ms_client_secret", "sso_client_secret"]] },
  { name: "oidc", label: "OIDC", flag: "auth_oidc", fields: [["oidc_issuer", "sso_issuer"], ["oidc_client_id", "sso_client_id"], ["oidc_client_secret", "sso_client_secret"]] },
];

function SignInProviders() {
  const { t } = useT(); const qc = useQueryClient();
  const q = useQuery({ queryKey: ["sys-auth"], queryFn: () => get("/system/auth") });
  const [f, setF] = useState<any>(null); const [saved, setSaved] = useState(false);
  useEffect(() => { if (q.data && !f) setF({ ...q.data, auth_self_register: q.data.auth_self_register ?? true }); }, [q.data, f]);
  const save = useMutation({ mutationFn: () => put("/system/auth", f), onSuccess: () => { setSaved(true); qc.invalidateQueries({ queryKey: ["sys-auth"] }); qc.invalidateQueries({ queryKey: ["providers"] }); setTimeout(() => setSaved(false), 2500); } });
  if (!f) return null;
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  return <Collapsible id="sys-sso" defaultOpen={false} title={`🔐 ${t("sso_title")}`} right={<Btn sm kind="primary" onClick={() => save.mutate()} disabled={save.isPending}>{t("save")}</Btn>}>
    <p className="muted small" style={{ marginTop: 0 }}>{t("sso_lead")}</p>
    <div className="grid k2">{PROVIDERS.map((p) => (
      <div key={p.name} className="card tight" style={{ boxShadow: "none" }}>
        <label className="check" style={{ padding: 0, marginBottom: 6 }}><input type="checkbox" checked={!!f[p.flag]} onChange={set(p.flag)} /><b>{p.label}</b> <span className="muted small">{t("sso_enabled")}</span></label>
        <div className="stack" style={{ gap: 6 }}>{p.fields.map(([k, lab]) => <Field key={k} label={t(lab)}>{k === "apple_private_key" ? <textarea className="input mono" style={{ minHeight: 70 }} value={f[k] ?? ""} onChange={set(k)} /> : <input className="input" type={k.endsWith("secret") ? "password" : "text"} value={f[k] ?? ""} onChange={set(k)} autoComplete="off" />}</Field>)}
          <div className="dim small mono">{t("sso_callback")}: {f.callbacks?.[p.name]}</div></div>
      </div>))}</div>
    <div className="grid k2" style={{ marginTop: 10 }}>
      <Field label={t("sso_public_host")}><input className="input" value={f.public_host ?? ""} onChange={set("public_host")} placeholder="https://watchover.sirket.com" /></Field>
      <Field label={t("sso_domains")}><input className="input" value={f.auth_domains ?? ""} onChange={set("auth_domains")} placeholder="sirket.com, grup.com" /></Field>
    </div>
    <label className="check"><input type="checkbox" checked={!!f.auth_self_register} onChange={set("auth_self_register")} /> {t("sso_self_register")}</label>
    <label className="check"><input type="checkbox" checked={!!f.mfa_email} onChange={set("mfa_email")} /> {t("sso_mfa")}</label>
    {saved && <div className="ok">{t("saved")}</div>}<Err e={save.error} />
  </Collapsible>;
}

function Settings() {
  const { t } = useT(); const qc = useQueryClient();
  const q = useQuery({ queryKey: ["sys-settings"], queryFn: () => get("/system/settings") });
  const [f, setF] = useState<any>(null); const [saved, setSaved] = useState(false);
  useEffect(() => { if (q.data && !f) setF(q.data); }, [q.data, f]);
  const save = useMutation({ mutationFn: () => put("/system/settings", { ...f, live_retention_h: Number(f.live_retention_h), live_window_max_events: Number(f.live_window_max_events), live_ring: Number(f.live_ring), live_spool_mb: Number(f.live_spool_mb), history_retention_days: Number(f.history_retention_days), learn_min: Number(f.learn_min), live_port: Number(f.live_port), thr_cpu: Number(f.thr_cpu), thr_memory: Number(f.thr_memory), thr_disk: Number(f.thr_disk), thr_gpu: Number(f.thr_gpu) }), onSuccess: () => { setSaved(true); qc.invalidateQueries({ queryKey: ["sys-settings"] }); qc.invalidateQueries({ queryKey: ["sys-status"] }); setTimeout(() => setSaved(false), 2500); } });
  if (!f) return null;
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  return <Collapsible id="sys-settings" title={`⚙ ${t("sys_settings")}`} right={<Btn sm kind="primary" onClick={() => save.mutate()}>{t("save")}</Btn>}><div className="stack">
    <label className="check"><input type="checkbox" checked={!!f.self_monitor} onChange={set("self_monitor")} /> {t("set_self_monitor")}</label>
    <label className="check"><input type="checkbox" checked={!!f.alerts_on} onChange={set("alerts_on")} /> {t("set_alerts_on")}</label>
    <div className="muted small">{t("set_thr_lead")}</div>
    <div className="grid k4">{(["cpu", "memory", "disk", "gpu"] as const).map((k) => <Field key={k} label={`${t("set_thr")} ${k.toUpperCase()} %`}><input className="input" type="number" min={1} max={100} value={f[`thr_${k}`] ?? ""} onChange={set(`thr_${k}`)} /></Field>)}</div>
    <div className="muted small">{t("set_cap_lead")}</div>
    <div className="grid k4"><Field label={t("set_live_retention_h")} hint={t("set_live_retention_hint")}><input className="input" type="number" min={1} value={f.live_retention_h ?? 48} onChange={set("live_retention_h")} /></Field><Field label={t("set_history_retention_days")}><input className="input" type="number" min={7} value={f.history_retention_days ?? 400} onChange={set("history_retention_days")} /></Field><Field label={t("set_live_ring")} hint={t("set_live_ring_hint")}><input className="input" type="number" min={1000} step={1000} value={f.live_ring ?? 50000} onChange={set("live_ring")} /></Field><Field label={t("set_live_spool_mb")}><input className="input" type="number" min={8} value={f.live_spool_mb ?? 64} onChange={set("live_spool_mb")} /></Field></div>
    <div className="grid k2"><Field label={t("set_learn_min")}><input className="input" type="number" value={f.learn_min} onChange={set("learn_min")} /></Field><Field label={t("set_live_port")}><input className="input" type="number" value={f.live_port} onChange={set("live_port")} /></Field>
      <Field label={t("set_live_key")}><input className="input" type="password" value={f.live_key ?? ""} onChange={set("live_key")} autoComplete="off" /></Field><Field label={t("set_public_host")}><input className="input" value={f.public_host ?? ""} onChange={set("public_host")} /></Field>
      <Field label={t("set_workspace")}><input className="input" value={f.workspace ?? ""} onChange={set("workspace")} /></Field><Field label={t("set_lang")}><select className="input" value={f.lang} onChange={set("lang")}><option value="tr">Türkçe</option><option value="en">English</option></select></Field></div>
    {saved && <div className="ok">{t("saved")}</div>}<Err e={save.error} /></div></Collapsible>;
}

function Maintenance({ docker }: { docker: boolean }) {
  const { t } = useT(); const qc = useQueryClient(); const [msg, setMsg] = useState<any>(null);
  const run = async (fn: () => Promise<any>) => { try { setMsg({ ok: true, text: JSON.stringify(await fn()) }); qc.invalidateQueries(); } catch (e: any) { setMsg({ ok: false, text: e.message }); } };
  const deploy = async (files: FileList | null) => { if (!files?.length) return; const fd = new FormData(); fd.append("file", files[0]); await run(() => api("/system/deploy", { method: "POST", form: fd })); };
  return <Collapsible id="sys-maint" defaultOpen={false} title={`🧰 ${t("sys_maint")}`}><div className="row">
    <Confirm kind="" onConfirm={() => run(() => post("/live/clear"))}>📡 {t("sys_clear_live")}</Confirm>
    <Confirm kind="" onConfirm={() => run(() => post("/system/snapshots/prune?keep=10"))}>🗂 {t("sys_prune")}</Confirm>
    {!docker && <><label className="btn">📦 {t("sys_zip_deploy")}<input type="file" accept=".zip" style={{ display: "none" }} onChange={(e) => deploy(e.target.files)} /></label><Confirm kind="" onConfirm={() => run(() => post("/system/git-update"))}>⬇ {t("sys_git_update")}</Confirm></>}
    {msg && <span className={msg.ok ? "ok" : "err"}>{msg.text.slice(0, 200)}</span>}</div></Collapsible>;
}
/** Table list of the database: filter box, "only non-empty" switch, collapsed to the first rows until expanded. */
function DbTables({ tables }: { tables: Record<string, number> }) {
  const { t } = useT(); const [q, setQ] = useState(""); const [open, setOpen] = useState(false); const [nonEmpty, setNonEmpty] = useState(false);
  const all = Object.entries(tables).map(([table, n]) => ({ id: table, table, n: Number(n) || 0 })).sort((a, b) => b.n - a.n || a.table.localeCompare(b.table));
  const rows = all.filter((r) => (!q || r.table.includes(q.toLowerCase())) && (!nonEmpty || r.n > 0));
  const shown = open || q ? rows : rows.slice(0, 8);
  const total = all.reduce((a, r) => a + r.n, 0);
  return <Collapsible id="sys-tables" title={t("sys_db")} right={<span className="muted small">{all.length} {t("sys_tables")} · {total.toLocaleString()} {t("sys_rows")}</span>}>
    <div className="row" style={{ marginBottom: 8 }}><input className="input grow" placeholder={t("sys_tbl_filter")} value={q} onChange={(e) => setQ(e.target.value)} />
      <label className="check" style={{ padding: 0 }}><input type="checkbox" checked={nonEmpty} onChange={(e) => setNonEmpty(e.target.checked)} /> {t("sys_tbl_nonempty")}</label></div>
    <Table cols={[{ k: "table", label: t("sys_tables"), render: (r) => <span className="mono">{r.table}</span> }, { k: "n", label: t("count"), num: true, render: (r) => r.n.toLocaleString() }]} rows={shown} />
    {rows.length > 8 && !q && <div className="row" style={{ marginTop: 8 }}><Btn sm onClick={() => setOpen(!open)}>{open ? t("sys_tbl_less") : t("sys_tbl_more", { n: rows.length - 8 })}</Btn></div>}
  </Collapsible>;
}

/** Parser v2 step 1: coverage of the format corpus (samples/corpus) through the production pipeline, against the accepted baseline. */
function ParserCoverage() {
  const { t } = useT(); const [open, setOpen] = useState(false); const [q, setQ] = useState("");
  const cov = useQuery({ queryKey: ["coverage"], queryFn: () => get("/system/coverage"), staleTime: 600000 });
  const d = cov.data; if (!d) return null;
  const o = d.overall; const pc = (v: any) => (v == null ? "-" : `${v}%`);
  const color = (v: any) => (v == null ? "var(--muted)" : v >= 80 ? "var(--green)" : v >= 50 ? "var(--amber)" : "var(--red)");
  const rows = (d.files as any[]).filter((r) => !q || r.file.includes(q.toLowerCase()) || r.format.includes(q.toLowerCase()));
  return <Collapsible id="sys-coverage" title={`🧪 ${t("sys_cov")}`} right={<span className="muted small">{d.n} {t("sys_cov_files")} · {t("sys_cov_score")} <b style={{ color: color(o.score) }}>{o.score}</b></span>}>
    <div className="muted small" style={{ marginBottom: 8 }}>{t("sys_cov_lead")}</div>
    <div className="chips" style={{ marginBottom: 8 }}>{(["timestamp", "level", "host", "service"] as const).map((k) => <span key={k} className="chip"><span className="d" style={{ background: color(o[k]) }} />{t("sys_cov_" + k)} {pc(o[k])}</span>)}{d.regressions?.length > 0 && <span className="chip" style={{ color: "var(--red)" }}>⚠ {d.regressions.length} {t("sys_cov_regress")}</span>}</div>
    {d.noise?.length > 0 && <Table cols={[{ k: "dataset", label: t("sys_cov_dataset"), render: (r) => <span className="mono small">{r.dataset}</span> }, { k: "raw", label: t("ds_events"), num: true, render: (r) => r.raw.toLocaleString() }, { k: "signals", label: t("ds_signals"), num: true, render: (r) => `${r.signals} / ${r.fingerprints}` }, { k: "incidents", label: t("ds_incidents"), num: true },
      { k: "reduction", label: t("sys_cov_reduction"), num: true, render: (r) => <b style={{ color: "var(--accent)" }}>{r.reduction}×</b> }, { k: "eliminated_pct", label: t("sys_cov_elim"), num: true, render: (r) => `${r.eliminated_pct}%` }, { k: "noise_precision", label: t("sys_cov_precision"), num: true, render: (r) => r.noise_precision == null ? "-" : <span style={{ color: color(r.noise_precision) }}>{r.noise_precision}%</span> }]} rows={d.noise.map((r: any) => ({ id: r.dataset, ...r }))} />}
    {open && <div className="row" style={{ marginBottom: 8, marginTop: 8 }}><input className="input grow" placeholder={t("sys_tbl_filter")} value={q} onChange={(e) => setQ(e.target.value)} /></div>}
    {open && <Table cols={[{ k: "file", label: t("file"), render: (r) => <span className="mono small">{r.file}</span> }, { k: "format", label: t("sys_cov_format") }, { k: "events", label: t("ds_events"), num: true, render: (r) => `${r.events}/${r.lines}` },
      ...(["timestamp", "level", "host", "service"] as const).map((k) => ({ k, label: t("sys_cov_" + k), num: true, render: (r: any) => <span style={{ color: color(r[k]) }}>{pc(r[k])}</span> })),
      { k: "score", label: t("sys_cov_score"), num: true, render: (r) => <b style={{ color: color(r.score) }}>{r.score}</b> }, { k: "delta", label: "Δ", num: true, render: (r) => r.delta == null ? "-" : <span style={{ color: r.delta > 0 ? "var(--green)" : r.delta < 0 ? "var(--red)" : undefined }}>{r.delta > 0 ? "+" : ""}{r.delta}</span> }]} rows={rows.map((r) => ({ id: r.file, ...r }))} />}
    <div className="row" style={{ marginTop: 8 }}><Btn sm onClick={() => setOpen(!open)}>{open ? t("sys_tbl_less") : t("sys_cov_show", { n: d.n })}</Btn></div>
  </Collapsible>;
}

function Capacity({ c }: { c: any }) {
  const { t } = useT();
  if (!c) return null;
  const gb = (b: number) => `${(b / 1024 ** 3).toFixed(1)} GB`; const mb = (b: number) => `${(b / 1024 ** 2).toFixed(0)} MB`;
  const ring = c.ring ?? {}; const db = c.db ?? {}; const disk = c.disk;
  const src = ring.covers_window ? t("cap_src_ring") : (db.enabled ? t("cap_src_db") : t("cap_src_short"));
  return <Collapsible id="sys-capacity" title={`📈 ${t("cap_title")}`} right={<span className={`badge ${c.warnings?.length ? "amber" : "green"}`}>{c.warnings?.length ? `${c.warnings.length} ${t("cap_warn")}` : t("cap_ok")}</span>}>
    <div className="muted small" style={{ marginBottom: 8 }}>{t("cap_hint")}</div>
    <div className="grid k4">
      <Kpi value={c.events_per_s ?? 0} label={t("cap_eps")} sub={`${t("cap_5m")} ${c.events_per_s_5m ?? 0}/s`} accent="#2dd4bf" />
      <Kpi value={`${ring.fill_pct ?? 0}%`} label={t("cap_ring")} sub={`${(ring.events ?? 0).toLocaleString()} / ${(ring.maxlen ?? 0).toLocaleString()} · ${ring.span_s ?? 0} s`} accent={ring.covers_window ? "#60a5fa" : "#f59e0b"} />
      <Kpi value={src} label={`${t("cap_window")} ${c.window_min} ${t("min")}`} sub={db.enabled ? `${t("cap_db")}: ${(db.events ?? 0).toLocaleString()} ${t("ds_events")} · ${gb(db.bytes ?? 0)} · ${c.retention_h} h` : t("cap_db_off")} accent={ring.covers_window ? "#2dd4bf" : "#a78bfa"} />
      <Kpi value={disk ? `${disk.free_pct}%` : "-"} label={t("cap_disk")} sub={disk ? `${gb(disk.free)} ${t("cap_free")} · spool ${mb(c.spool_bytes ?? 0)}` : "-"} accent={disk && disk.free_pct < 10 ? "#f87171" : "#a78bfa"} />
    </div>
    {!!c.warnings?.length && <ul className="small" style={{ marginTop: 8, color: "#f59e0b" }}>{c.warnings.map((w: any) => <li key={w.code}>{t(`cap_w_${w.code}`) || w.code}: {w.detail}</li>)}</ul>}
  </Collapsible>;
}
