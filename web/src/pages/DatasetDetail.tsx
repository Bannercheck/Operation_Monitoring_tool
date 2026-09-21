import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, get } from "../api";
import { useT } from "../i18n";
import { Badge, Btn, Card, Empty, Kpi, Modal, Table, Tabs } from "../components/ui";

type Tab = "overview" | "incidents" | "signals" | "noise";

export default function DatasetDetail() {
  const { key = "" } = useParams(); const { t } = useT();
  const [tab, setTab] = useState<Tab>("overview"); const [inc, setInc] = useState<string | null>(null);
  const d = useQuery({ queryKey: ["dataset", key], queryFn: () => get(`/datasets/${key}`) });
  const incs = useQuery({ queryKey: ["incidents", key], queryFn: () => get(`/datasets/${key}/incidents`) });
  const sigs = useQuery({ queryKey: ["signals", key], queryFn: () => get(`/datasets/${key}/signals?limit=200`), enabled: tab === "signals" });
  const noise = useQuery({ queryKey: ["noise", key], queryFn: () => get(`/datasets/${key}/noise`), enabled: tab === "noise" });
  if (d.isError) return <Empty>{String((d.error as any)?.message)}</Empty>;
  const f = d.data?.funnel ?? {}; const p = d.data?.profile ?? {};
  return (
    <div className="stack">
      <div className="row between"><div><Link to="/datasets" className="muted small">← {t("ds_title")}</Link><h2>{d.data?.name ?? "…"}</h2><span className="muted small mono">{d.data?.stamp?.result ? `result ${d.data.stamp.result.slice(0, 10)} · input ${d.data.stamp.input?.slice(0, 10)}` : ""}</span></div>
        <div className="row"><a className="btn sm" href={`/api/datasets/${key}/export?fmt=csv`} onClick={(e) => { e.preventDefault(); download(`/datasets/${key}/export?fmt=csv`, `${d.data?.name}.csv`); }}>CSV</a></div></div>
      <div className="grid k4">
        <Kpi value={(f.raw_events ?? 0).toLocaleString()} label={t("funnel_raw")} />
        <Kpi value={f.signals ?? 0} label={t("funnel_signals")} accent="#60a5fa" sub={`${f.fingerprints ?? f.signals ?? 0} fp`} />
        <Kpi value={f.incidents ?? 0} label={t("funnel_incidents")} accent="#f87171" />
        <Kpi value={f.reduction ? `${f.reduction}×` : "-"} label={t("funnel_reduction")} accent="#a78bfa" />
      </div>
      <Tabs value={tab} onChange={setTab} tabs={[{ k: "overview", label: t("tab_overview") }, { k: "incidents", label: `${t("tab_incidents")} · ${incs.data?.length ?? ""}` }, { k: "signals", label: t("tab_signals") }, { k: "noise", label: t("tab_noise") }]} />
      {tab === "overview" && <div className="grid k2">
        <Card title={t("ds_files")}><Table cols={[{ k: "file", label: t("name") }, { k: "format", label: t("ds_format") }, { k: "records", label: t("ds_records"), num: true }]} rows={(d.data?.files ?? []).map((x: any, i: number) => ({ id: i, ...x }))} /></Card>
        <Card title={t("tab_overview")}><div className="stack small">
          <div><b>{t("time")}:</b> {p.time_range ? `${p.time_range.start} → ${p.time_range.end}` : "-"}</div>
          <div><b>{t("service")}:</b> {(p.services ?? []).slice(0, 12).join(", ")}</div><div><b>{t("host")}:</b> {(p.hosts ?? []).slice(0, 12).join(", ")}</div>
          {p.severity && <div className="row">{Object.entries(p.severity).map(([k, v]: any) => <span key={k} className="chip"><Badge v={k} /> {v}</span>)}</div>}</div></Card>
      </div>}
      {tab === "incidents" && (!incs.data?.length ? <Empty /> : incs.data.map((i: any) => (
        <Card key={i.id} className="tight"><div className="svc" style={{ cursor: "pointer" }} onClick={() => setInc(i.id)}>
          <div className="h"><Badge v={i.severity} /> <b>{i.id}</b> {i.title} <span className="muted">· {t("inc_score")} {i.score}</span>{i.recovery?.kind && <span className="chip">{t("inc_recovery")}: {i.recovery.kind}</span>}</div>
          <div className="v">{i.narrative_text?.slice(0, 220)}</div></div></Card>)))}
      {tab === "signals" && <Card><Table cols={[{ k: "id", label: "ID" }, { k: "severity", label: t("severity"), render: (r) => <Badge v={r.severity} /> }, { k: "template", label: t("sig_template"), render: (r) => <span className="mono">{r.template.slice(0, 110)}</span> },
        { k: "count", label: t("count"), num: true }, { k: "burst", label: t("sig_burst"), num: true }, { k: "peak/min", label: t("sig_peak"), num: true }, { k: "services", label: t("service") }, { k: "onset", label: t("sig_onset") }]} rows={sigs.data ?? []} /></Card>}
      {tab === "noise" && noise.data && <div className="grid k2">
        <Kpi value={(noise.data.eliminated ?? 0).toLocaleString()} label={t("noise_eliminated")} accent="#64748b" sub={`${(noise.data.kept ?? 0).toLocaleString()} ${t("noise_kept")}`} />
        <Card title={t("noise_rules")}><Table cols={[{ k: "rule", label: t("name") }, { k: "n", label: t("count"), num: true }]} rows={Object.entries(noise.data.by_rule ?? noise.data.reasons ?? {}).map(([rule, n]) => ({ id: rule, rule, n }))} /></Card></div>}
      {inc && <IncidentModal dataset={key} iid={inc} onClose={() => setInc(null)} />}
    </div>
  );
}

async function download(path: string, name: string) {
  const text = await api<string>(path, { text: true });
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text])); a.download = name; a.click();
}

function IncidentModal({ dataset, iid, onClose }: { dataset: string; iid: string; onClose: () => void }) {
  const { t } = useT(); const [pm, setPm] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["incident", dataset, iid], queryFn: () => get(`/datasets/${dataset}/incidents/${iid}`) });
  const i = q.data;
  return (
    <Modal wide title={<span><Badge v={i?.severity ?? "low"} /> {iid} · {i?.title}</span>} onClose={onClose}>
      {!i ? <div className="muted">{t("loading")}</div> : (
        <div className="stack">
          <Card title={t("inc_root")}><div className="stack small"><div className="mono">{i.root_cause.template}</div><div className="muted">{i.root_cause_reason}</div><div><b>{t("inc_why")}</b> {i.reason_text}</div>
            {!!i.root_cause_alternatives?.length && <div className="muted">alt: {i.root_cause_alternatives.map((a: any) => `${a.signal} (${a.score})`).join(", ")}</div>}</div></Card>
          <Card title={t("inc_narrative")}><p style={{ margin: 0 }}>{i.narrative_text}</p></Card>
          <div className="grid k2">
            <Card title={t("inc_factors")}><Table cols={[{ k: "name", label: t("name") }, { k: "value", label: "" }, { k: "contribution", label: "+", num: true, render: (r) => Number(r.contribution).toFixed(2) }]} rows={(i.factors ?? []).map((f: any, n: number) => ({ id: n, ...f }))} /></Card>
            <Card title={t("inc_affected")}><div className="stack small"><div><b>{t("service")}:</b> {i.affected_services.join(", ")}</div><div><b>{t("host")}:</b> {i.affected_hosts.join(", ")}</div><div><b>{t("time")}:</b> {i.started_at.slice(11, 19)} → {i.ended_at.slice(11, 19)}</div>
              {i.recovery?.kind && <div><b>{t("inc_recovery")}:</b> {i.recovery.kind} {i.recovery.recovered_at?.slice(11, 19)} {i.recovery.evidence}</div>}</div></Card>
          </div>
          <Card title={t("inc_timeline")}><div className="tl">{(i.timeline ?? []).slice(0, 30).map((e: any, n: number) => <div key={n} className="e small"><span className="mono">{String(e.time ?? e.ts ?? "").slice(11, 19)}</span> {e.event ?? e.text ?? e.template ?? JSON.stringify(e)}</div>)}</div></Card>
          <Card title={t("inc_recs")}><ul style={{ margin: 0, paddingLeft: 18 }}>{(i.recommendations ?? []).map((r: string, n: number) => <li key={n}>{r}</li>)}</ul></Card>
          <Card title={t("tab_actions")}><Table cols={[{ k: "title", label: t("act_title_f") }, { k: "priority", label: t("act_priority"), render: (r) => <Badge v={r.priority} /> }, { k: "status", label: t("status"), render: (r) => <Badge v={r.status} label={t("st_" + r.status)} /> }, { k: "owner", label: t("owner") }]} rows={i.actions ?? []} /></Card>
          <div className="row"><span className="muted small">{t("inc_evidence")}: {(i.evidence ?? []).slice(0, 8).join(" · ")}</span></div>
          <div className="row"><Btn sm onClick={async () => setPm(await api<string>(`/datasets/${dataset}/incidents/${iid}/postmortem`, { text: true }))}>{t("inc_postmortem")}</Btn></div>
          {pm && <pre className="code">{pm}</pre>}
        </div>)}
    </Modal>
  );
}
