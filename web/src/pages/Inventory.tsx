import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, del, get, put } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Badge, Btn, Card, Chip, Confirm, Err, Field, Modal, Table, Tabs } from "../components/ui";

/** Inventory (CMDB-lite): the Streamlit page as a route of its own — hosts, full add / edit form, discovered hosts with criticality, CSV import / export. */
const FIELDS = ["hostname", "ip", "aliases", "env", "criticality", "dc", "rack", "vlan", "subnet", "os", "role", "application", "cluster", "owner", "storage", "vendor_model", "serial", "monitoring", "status", "tags", "notes"] as const;
const CRIT = ["critical", "high", "normal", "low"]; const STATUS = ["active", "maintenance", "decommissioned"]; const ENVS = ["", "prod", "staging", "test", "dev", "qa", "dr"];
const EMPTY: Record<string, string> = Object.fromEntries(FIELDS.map((k) => [k, k === "criticality" ? "normal" : k === "status" ? "active" : k === "env" ? "prod" : ""]));
type Tab = "list" | "add" | "disc" | "csv";

export default function InventoryPage() {
  const { t } = useT();
  return <div className="stack"><div><h2>🗄 {t("inv_page")}</h2><div className="muted small">{t("inv_page_tag")}</div></div><InventoryPanel /></div>;
}

export function InventoryPanel() {
  const { t } = useT(); const { can } = useAuth(); const qc = useQueryClient(); const [tab, setTab] = useState<Tab>("list"); const [q, setQ] = useState(""); const [edit, setEdit] = useState<any>(null); const [msg, setMsg] = useState<any>(null);
  const list = useQuery({ queryKey: ["inventory", q], queryFn: () => get(`/inventory?q=${encodeURIComponent(q)}`) });
  const stats = useQuery({ queryKey: ["inv-stats"], queryFn: () => get("/inventory/stats"), refetchInterval: 15000 });
  const disc = useQuery({ queryKey: ["discovered"], queryFn: () => get("/inventory/discovered"), refetchInterval: 15000 });
  const inv = () => { qc.invalidateQueries({ queryKey: ["inventory"] }); qc.invalidateQueries({ queryKey: ["inv-stats"] }); qc.invalidateQueries({ queryKey: ["discovered"] }); };
  const rm = useMutation({ mutationFn: (h: string) => del(`/inventory/${encodeURIComponent(h)}`), onSuccess: inv });
  const promote = useMutation({ mutationFn: (r: any) => put("/inventory", { hostname: r.hostname, env: r.env ?? "", ip: r.ip ?? "", dc: r.dc ?? "", criticality: r.criticality ?? "normal", monitoring: r.source ?? "live" }), onSuccess: inv });
  const upload = async (files: FileList | null) => { if (!files?.length) return; const fd = new FormData(); fd.append("file", files[0]); try { setMsg(await api("/inventory/import", { method: "POST", form: fd })); inv(); } catch (x) { setMsg({ error: x }); } };
  const download = async (path: string, name: string) => { const txt = await api<string>(path, { text: true }); const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([txt], { type: "text/csv" })); a.download = name; a.click(); };
  const editable = can("act.inventory"); const s = stats.data; const rows: any[] = list.data ?? []; const found: any[] = disc.data ?? [];
  return <div className="stack">
    {s && <div className="chips"><Chip color="#60a5fa">{s.hosts} {t("inv_hosts")}</Chip><Chip color="#f87171">{s.critical} {t("inv_critical")}</Chip><Chip color="#a78bfa">{s.dcs} DC</Chip><Chip color={s.unmatched ? "#fbbf24" : "#2dd4bf"}>{s.matched}/{s.seen} {t("inv_matched")}</Chip></div>}
    <div className="muted small">{t("inv_intro")}</div>
    <Tabs value={tab} onChange={setTab} tabs={[{ k: "list", label: `${t("inv_tab_list")} · ${s?.hosts ?? rows.length}` }, { k: "add", label: t("inv_tab_add") }, { k: "disc", label: `${t("inv_tab_disc")} · ${found.length}` }, { k: "csv", label: t("inv_tab_csv") }]} />
    {tab === "list" && <Card>
      <div className="row" style={{ marginBottom: 8 }}><input className="input grow" placeholder={t("inv_search")} value={q} onChange={(e) => setQ(e.target.value)} /></div>
      {!rows.length ? <div className="muted small">{t("inv_none")}</div> : <Table cols={[
        { k: "hostname", label: t("inv_f_hostname"), render: (r) => <b style={{ cursor: editable ? "pointer" : undefined }} onClick={() => editable && setEdit(r)}>{r.hostname}</b> }, { k: "ip", label: "IP" }, { k: "env", label: t("inv_f_env") }, { k: "dc", label: t("inv_f_dc") },
        { k: "role", label: t("inv_f_role") }, { k: "application", label: t("inv_f_application") }, { k: "owner", label: t("inv_f_owner") }, { k: "criticality", label: t("inv_f_criticality"), render: (r) => <Badge v={r.criticality} /> },
        { k: "os", label: t("inv_f_os") }, { k: "status", label: t("inv_f_status"), render: (r) => <Badge v={r.status} /> }, { k: "monitoring", label: t("inv_f_monitoring") },
        { k: "x", label: "", render: (r) => editable ? <div className="row"><Btn sm onClick={() => setEdit(r)}>✏️</Btn><Confirm onConfirm={() => rm.mutate(r.hostname)}>🗑</Confirm></div> : null }]} rows={rows.map((r) => ({ id: r.hostname, ...r }))} />}
    </Card>}
    {tab === "add" && editable && <HostForm onDone={() => { inv(); setTab("list"); }} />}
    {tab === "disc" && <Card>
      <div className="muted small" style={{ marginBottom: 8 }}>{t("inv_disc_hint")}</div>
      {!found.length ? <div className="ok">{t("inv_disc_none")}</div> : <Table cols={[{ k: "hostname", label: t("inv_f_hostname"), render: (r) => <b>{r.hostname}</b> }, { k: "env", label: t("inv_f_env") }, { k: "ip", label: "IP" }, { k: "dc", label: t("inv_f_dc") }, { k: "source", label: t("inv_f_monitoring") },
        { k: "x", label: "", render: (r) => editable ? <DiscoveredRow r={r} onAdd={(crit) => promote.mutate({ ...r, criticality: crit })} /> : null }]} rows={found.map((r: any, i: number) => ({ id: i, ...r }))} />}
      <Err e={promote.error} /></Card>}
    {tab === "csv" && <div className="grid k2">
      <Card title={`⬆ ${t("inv_import")}`}><div className="muted small">{t("inv_import_hint")}</div>
        {editable && <div className="row" style={{ marginTop: 8 }}><label className="btn primary">{t("inv_import_go")}<input type="file" accept=".csv,.txt" style={{ display: "none" }} onChange={(e) => upload(e.target.files)} /></label><Btn onClick={() => download("/inventory/template", "watchover-envanter-sablon.csv")}>📄 {t("inv_template")}</Btn></div>}
        {msg?.imported != null && <div className="ok" style={{ marginTop: 8 }}>{t("inv_imported", { n: msg.imported })}</div>}{(msg?.errors ?? []).slice(0, 10).map((e: string, i: number) => <div key={i} className="err">{e}</div>)}<Err e={msg?.error} /></Card>
      <Card title={`⬇ ${t("inv_export")}`}><div className="muted small">{t("inv_export_hint")}</div><div className="row" style={{ marginTop: 8 }}><Btn onClick={() => download("/inventory/export", `watchover-envanter-${new Date().toISOString().slice(0, 10)}.csv`)}>📄 {t("inv_export_go")}</Btn></div></Card>
    </div>}
    {edit && <Modal wide title={t("inv_edit_title", { h: edit.hostname })} onClose={() => setEdit(null)}><HostForm rec={edit} onDone={() => { setEdit(null); inv(); }} /></Modal>}
  </div>;
}

function DiscoveredRow({ r, onAdd }: { r: any; onAdd: (crit: string) => void }) {
  const { t } = useT(); const [crit, setCrit] = useState("normal");
  return <div className="row"><select className="input" style={{ width: 120 }} value={crit} onChange={(e) => setCrit(e.target.value)}>{CRIT.map((c) => <option key={c}>{c}</option>)}</select><Btn sm kind="primary" onClick={() => onAdd(crit)}>➕ {t("inv_add")}</Btn></div>;
}

function HostForm({ rec, onDone }: { rec?: any; onDone: () => void }) {
  const { t } = useT(); const [f, setF] = useState<Record<string, string>>({ ...EMPTY, ...(rec ?? {}) }); const [err, setErr] = useState<any>(null);
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  const save = async (e: React.FormEvent) => { e.preventDefault(); setErr(null); try { await put("/inventory", { ...f, source: rec?.source || "manual" }); onDone(); } catch (x) { setErr(x); } };
  const rm = async () => { try { await del(`/inventory/${encodeURIComponent(rec.hostname)}`); onDone(); } catch (x) { setErr(x); } };
  const T = (k: string, extra: any = {}) => <Field key={k} label={t("inv_f_" + k)}><input className="input" value={f[k] ?? ""} onChange={set(k)} {...extra} /></Field>;
  return <Card title={rec ? undefined : `➕ ${t("inv_tab_add")}`}><form className="stack" onSubmit={save}>
    <div className="grid k4">{T("hostname", { required: true, disabled: !!rec, placeholder: "db-01" })}{T("ip", { placeholder: "10.20.1.15" })}{T("aliases", { placeholder: "db01.corp.local, oradb1", title: t("inv_aliases_help") })}
      <Field label={t("inv_f_env")}><select className="input" value={f.env} onChange={set("env")}>{ENVS.map((x) => <option key={x} value={x}>{x || "-"}</option>)}</select></Field></div>
    <div className="grid k4"><Field label={t("inv_f_criticality")}><select className="input" value={f.criticality} onChange={set("criticality")}>{CRIT.map((x) => <option key={x}>{x}</option>)}</select></Field>{T("dc", { placeholder: "IST-DC1" })}{T("rack", { placeholder: "R12" })}{T("vlan", { placeholder: "VLAN-120" })}</div>
    <div className="grid k4">{T("subnet", { placeholder: "10.20.1.0/24" })}{T("os", { placeholder: "RHEL 9" })}{T("role", { placeholder: t("inv_role_help") })}{T("application", { placeholder: "Oracle ERP" })}</div>
    <div className="grid k4">{T("cluster", { placeholder: "ora-rac-1" })}{T("owner", { placeholder: "DBA" })}{T("storage", { placeholder: "SAN LUN-042 2TB" })}{T("vendor_model", { placeholder: "Dell R760" })}</div>
    <div className="grid k4">{T("serial")}{T("monitoring", { placeholder: "agent:db-01 / es-prod" })}<Field label={t("inv_f_status")}><select className="input" value={f.status} onChange={set("status")}>{STATUS.map((x) => <option key={x}>{x}</option>)}</select></Field>{T("tags", { placeholder: "oracle, core" })}</div>
    <Field label={t("inv_f_notes")}><textarea className="input" style={{ minHeight: 56 }} value={f.notes ?? ""} onChange={set("notes")} /></Field>
    <Err e={err} /><div className="row"><Btn kind="primary" type="submit">💾 {t("inv_save")}</Btn>{rec && <Confirm onConfirm={rm}>🗑 {t("inv_delete")}</Confirm>}</div>
  </form></Card>;
}
