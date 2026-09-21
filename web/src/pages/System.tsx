import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { del, get, post } from "../api";
import { useT } from "../i18n";
import { Btn, Card, Confirm, Err, Kpi, Table, fmtTs } from "../components/ui";

export default function System() {
  const { t } = useT(); const qc = useQueryClient();
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
    {!!rel.data?.length && <Card title={t("sys_releases")}><div className="stack small">{rel.data.slice(0, 6).map((r: any) => <div key={r.version}><b>v{r.version}</b> <span className="muted">{r.date} · {r.kind}</span><ul style={{ margin: "2px 0 0", paddingLeft: 18 }}>{(r.lines ?? []).map((n: string, i: number) => <li key={i}>{n}</li>)}</ul></div>)}</div></Card>}
  </div>;
}
