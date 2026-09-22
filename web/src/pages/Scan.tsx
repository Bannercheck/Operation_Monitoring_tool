import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { get, post } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Badge, Btn, Card, Empty, Err, Kpi, Modal, Table, Tabs, fmtTs } from "../components/ui";

const SEV: Record<string, string> = { critical: "red", high: "red", medium: "amber", low: "blue" };

function SevBadge({ s }: { s: string }) { const { t } = useT(); return <span className={`badge ${SEV[s] ?? ""}`}>{t(`scan_sev_${s}`) || s}</span>; }

export default function Scan() {
  const { t } = useT(); const { can } = useAuth(); const qc = useQueryClient();
  const [tab, setTab] = useState<"results" | "catalog">("results");
  const [probe, setProbe] = useState(false); const [open, setOpen] = useState<any>(null);
  const stats = useQuery({ queryKey: ["scan-stats"], queryFn: () => get("/scan/stats"), refetchInterval: 20000 });
  const rows = useQuery({ queryKey: ["scan-results"], queryFn: () => get("/scan/results") });
  const cat = useQuery({ queryKey: ["scan-catalog"], queryFn: () => get("/scan/catalog"), enabled: tab === "catalog" });
  const inv = () => { qc.invalidateQueries({ queryKey: ["scan-results"] }); qc.invalidateQueries({ queryKey: ["scan-stats"] }); };
  const scanAll = useMutation({ mutationFn: () => post("/scan/all", { probe }), onSuccess: inv });
  const editable = can("act.scan"); const s = stats.data; const data: any[] = rows.data ?? [];
  return <div className="stack">
    <div className="row between"><div><h2>🛡 {t("scan_title")}</h2><span className="muted small">{t("scan_sub")}</span></div>
      {editable && <div className="row">
        <label className="chip" title={t("scan_probe_hint")}><input type="checkbox" checked={probe} onChange={(e) => setProbe(e.target.checked)} /> {t("scan_probe")}</label>
        <Btn kind="primary" sm onClick={() => scanAll.mutate()} disabled={scanAll.isPending}>{scanAll.isPending ? `⏳ ${t("scan_running")}` : `🔍 ${t("scan_run_all")}`}</Btn></div>}</div>
    {probe && editable && <div className="muted small">⚠️ {t("scan_probe_warn")}</div>}
    <Err e={scanAll.error} />
    {s && <div className="grid k4">
      <Kpi value={s.hosts_scanned ?? 0} label={t("scan_k_hosts")} sub={s.last_run ? `${t("scan_last")} ${fmtTs(s.last_run.ts)}` : t("scan_never")} />
      <Kpi value={s.critical ?? 0} label={t("scan_sev_critical")} accent="#f87171" sub={`${s.high ?? 0} ${t("scan_sev_high")}`} />
      <Kpi value={s.exposed ?? 0} label={t("scan_k_exposed")} accent="#fb923c" sub={t("scan_k_exposed_sub")} />
      <Kpi value={s.catalog ?? 0} label={t("scan_k_catalog")} accent="#a78bfa" sub={t("scan_k_catalog_sub")} /></div>}
    <Tabs value={tab} onChange={setTab} tabs={[{ k: "results", label: `${t("scan_tab_results")} · ${data.length}` }, { k: "catalog", label: t("scan_tab_catalog") }]} />
    {tab === "results" && (!data.length ? <Empty>{t("scan_none")}</Empty> : <Card><Table cols={[
      { k: "hostname", label: t("inv_f_hostname"), render: (r) => <b style={{ cursor: "pointer" }} onClick={() => setOpen(r)}>{r.hostname}</b> },
      { k: "ip", label: "IP" },
      { k: "worst", label: t("scan_worst"), render: (r) => r.worst === "none" ? <span className="badge green">{t("scan_clean")}</span> : <SevBadge s={r.worst} /> },
      { k: "critical", label: t("scan_sev_critical"), num: true }, { k: "high", label: t("scan_sev_high"), num: true },
      { k: "medium", label: t("scan_sev_medium"), num: true }, { k: "low", label: t("scan_sev_low"), num: true },
      { k: "n", label: t("scan_findings"), num: true, render: (r) => r.findings.length },
      { k: "scanned_at", label: t("scan_when"), render: (r) => <span className="muted small">{fmtTs(r.scanned_at)}{r.probed ? " · 🌐" : ""}</span> },
    ]} rows={data.map((r) => ({ id: r.hostname, ...r }))} /></Card>)}
    {tab === "catalog" && <Card title={`${t("scan_tab_catalog")} · ${cat.data?.count ?? 0}`}><Table cols={[
      { k: "id", label: "ID", render: (r) => <b className="mono">{r.id}</b> }, { k: "product", label: t("scan_product") },
      { k: "severity", label: t("scan_severity"), render: (r) => <SevBadge s={r.severity} /> }, { k: "cvss", label: "CVSS", num: true, render: (r) => r.cvss ?? "-" },
      { k: "lt", label: t("scan_fixed"), render: (r) => `< ${r.lt}` }, { k: "title", label: t("scan_desc") },
    ]} rows={(cat.data?.advisories ?? []).map((r: any, i: number) => ({ id: i, ...r }))} /></Card>}
    {open && <Modal title={`🛡 ${open.hostname}`} onClose={() => setOpen(null)} wide>
      {open.open_ports?.length > 0 && <div className="stack" style={{ marginBottom: 10 }}><b>{t("scan_open_ports")}</b>
        <div className="chips">{open.open_ports.map((p: any) => <span key={p.port} className="chip">{p.port}/{p.service}{p.banner ? ` · ${p.banner}` : ""}</span>)}</div></div>}
      {!open.findings.length ? <span className="badge green">{t("scan_clean")}</span> : <Table cols={[
        { k: "severity", label: t("scan_severity"), render: (r) => <SevBadge s={r.severity} /> },
        { k: "id", label: "ID", render: (r) => <b className="mono">{r.id}</b> },
        { k: "title", label: t("scan_desc"), render: (r) => <div>{r.title}{r.confidence === "unconfirmed" && <span className="badge amber" style={{ marginLeft: 6 }}>{t("scan_unconfirmed")}</span>}<div className="muted small">{r.version && r.version !== "?" ? `${r.product} ${r.version} → ${r.fixed ?? ""}` : ""} {r.fix ? `· ${r.fix}` : ""}</div></div> },
      ]} rows={open.findings.map((f: any, i: number) => ({ id: i, ...f }))} />}
    </Modal>}
  </div>;
}
