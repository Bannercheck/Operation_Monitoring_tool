import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { del, get, post, put } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Btn, Card, Confirm, Err, Field, Kpi, Table, fmtTs } from "../components/ui";

export default function System() {
  const { t } = useT(); const qc = useQueryClient(); const { can } = useAuth();
  const st = useQuery({ queryKey: ["sys-status"], queryFn: () => get("/system/status"), refetchInterval: 15000 });
  const snaps = useQuery({ queryKey: ["snapshots"], queryFn: () => get("/system/snapshots") });
  const rel = useQuery({ queryKey: ["releases"], queryFn: () => get("/system/releases") });
  const inv = () => { qc.invalidateQueries({ queryKey: ["snapshots"] }); qc.invalidateQueries({ queryKey: ["sys-status"] }); };
  const snap = useMutation({ mutationFn: () => post("/system/snapshots", { reason: "manual", code: true, db: true }), onSuccess: inv });
  const rmSnap = useMutation({ mutationFn: (id: string) => del(`/system/snapshots/${id}`), onSuccess: inv });
  const vacuum = useMutation({ mutationFn: () => post("/system/vacuum"), onSuccess: inv });
  const s = st.data; const v = s?.version ?? {}; const db = s?.database ?? {};
  return <div className="stack">
    <div className="row between"><h2>🛠 {t("sys_title")}</h2><span className="chip">Watchover v{v.version} · {v.git} · {v.docker ? "🐳" : v.platform}</span></div>
    <div className="grid k4">
      <Kpi value={`v${v.version ?? "-"}`} label={t("sys_version")} sub={v.git} />
      <Kpi value={db.size_mb != null ? `${db.size_mb} MB` : "-"} label={t("sys_db")} accent="#a78bfa" sub={`${db.backend} · ${Object.keys(db.tables ?? {}).length} ${t("sys_tables")}`} />
      <Kpi value={s ? `${Math.floor(s.uptime_s / 3600)}h ${Math.floor((s.uptime_s % 3600) / 60)}m` : "-"} label={t("sys_uptime")} accent="#60a5fa" sub={`${s?.datasets ?? 0} ${t("ds_title").toLowerCase()}`} />
      <Kpi value={s?.live?.received?.toLocaleString() ?? 0} label={t("ops_events15")} accent="#2dd4bf" sub={`${s?.live?.agents ?? 0} ${t("ops_agents")} · ${s?.anomalies?.open ?? 0} ${t("ops_anom_open")}`} />
    </div>
    <div className="grid k2">
      <Card title={t("sys_services")}><div className="stack small">{Object.entries(s?.services ?? {}).map(([k, val]: any) => <div key={k} className="row"><span className="chip"><span className="d" style={{ background: val ? "#34d399" : "#64748b" }} />{k}</span><span className="muted">{String(val)}</span></div>)}
        <div className="row"><span className="chip"><span className="d" style={{ background: s?.live?.receiver ? "#34d399" : "#64748b" }} />receiver</span><span className="chip"><span className="d" style={{ background: s?.live?.simulator ? "#fbbf24" : "#64748b" }} />simulator</span></div>
        <div className="mono dim">{db.url}</div></div>
        <div className="row" style={{ marginTop: 10 }}><Btn sm onClick={() => vacuum.mutate()}>{t("sys_vacuum")}</Btn></div><Err e={vacuum.error} /></Card>
      <Card title={t("sys_db")}><Table cols={[{ k: "table", label: t("sys_tables") }, { k: "n", label: t("count"), num: true }]} rows={Object.entries(db.tables ?? {}).map(([table, n]) => ({ id: table, table, n }))} /></Card>
    </div>
    <Card title={t("sys_snapshots")} right={<Btn sm kind="primary" onClick={() => snap.mutate()} disabled={snap.isPending}>📸 {t("sys_snap_new")}</Btn>}>
      <Table cols={[{ k: "id", label: "ID", render: (r) => <b className="mono">{r.id}</b> }, { k: "version", label: t("sys_version"), render: (r) => `v${r.version} · ${r.git}` }, { k: "ts", label: t("time"), render: (r) => fmtTs(r.ts) }, { k: "reason", label: t("note") }, { k: "size", label: "MB", num: true, render: (r) => (r.size / 1e6).toFixed(1) }, { k: "dbs", label: t("sys_db"), render: (r) => (r.dbs ?? []).join(", ") },
        { k: "x", label: "", render: (r) => <Confirm onConfirm={() => rmSnap.mutate(r.id)}>{t("delete")}</Confirm> }]} rows={snaps.data ?? []} /><Err e={snap.error} /></Card>
    {can("sys.auth") && <SignInProviders />}
    {!!rel.data?.length && <Card title={t("sys_releases")}><div className="stack small">{rel.data.slice(0, 6).map((r: any) => <div key={r.version}><b>v{r.version}</b> <span className="muted">{r.date} · {r.kind}</span><ul style={{ margin: "2px 0 0", paddingLeft: 18 }}>{(r.lines ?? []).map((n: string, i: number) => <li key={i}>{n}</li>)}</ul></div>)}</div></Card>}
  </div>;
}


const PROVIDERS = [
  { name: "google", label: "Google", flag: "auth_google", fields: [["google_client_id", "sso_client_id"], ["google_client_secret", "sso_client_secret"]] },
  { name: "microsoft", label: "Microsoft", flag: "auth_microsoft", fields: [["ms_tenant", "sso_tenant"], ["ms_client_id", "sso_client_id"], ["ms_client_secret", "sso_client_secret"]] },
  { name: "apple", label: "Apple", flag: "auth_apple", fields: [["apple_client_id", "sso_client_id"], ["apple_team_id", "sso_team"], ["apple_key_id", "sso_key_id"], ["apple_private_key", "sso_p8"]] },
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
  return <Card title={`🔐 ${t("sso_title")}`} right={<Btn sm kind="primary" onClick={() => save.mutate()} disabled={save.isPending}>{t("save")}</Btn>}>
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
  </Card>;
}