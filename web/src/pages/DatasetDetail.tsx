import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api, get, post } from "../api";
import { useT } from "../i18n";
import { Badge, Btn, Card, Empty, Err, Field, Kpi, Modal, Table, Tabs, hhmm } from "../components/ui";

type Tab = "overview" | "incidents" | "signals" | "noise" | "search" | "actions";

export default function DatasetDetail() {
  const { key = "" } = useParams(); const { t } = useT();
  const [tab, setTab] = useState<Tab>("overview"); const [inc, setInc] = useState<string | null>(null);
  const d = useQuery({ queryKey: ["dataset", key], queryFn: () => get(`/datasets/${key}`) });
  const incs = useQuery({ queryKey: ["incidents", key], queryFn: () => get(`/datasets/${key}/incidents`) });
  const sigs = useQuery({ queryKey: ["signals", key], queryFn: () => get(`/datasets/${key}/signals?limit=200`), enabled: tab === "signals" });
  const noise = useQuery({ queryKey: ["noise", key], queryFn: () => get(`/datasets/${key}/noise`), enabled: tab === "noise" });
  const acts = useQuery({ queryKey: ["ds-actions", key], queryFn: () => get(`/datasets/${key}/actions`), enabled: tab === "actions" });
  if (d.isError) return <Empty>{String((d.error as any)?.message)}</Empty>;
  const f = d.data?.funnel ?? {}; const p = d.data?.profile ?? {};
  return (
    <div className="stack">
      <div className="row between"><div><Link to="/datasets" className="muted small">← {t("ds_title")}</Link><h2>{d.data?.name ?? "…"}</h2><span className="muted small mono">{d.data?.stamp?.result ? `result ${d.data.stamp.result.slice(0, 10)} · input ${d.data.stamp.input?.slice(0, 10)}` : ""}</span></div>
        <div className="row"><a className="btn sm" href={`/api/datasets/${key}/export?fmt=csv`} onClick={(e) => { e.preventDefault(); download(`/datasets/${key}/export?fmt=csv`, `${d.data?.name}.csv`); }}>CSV</a></div></div>
      <div className="grid k4">
        <Kpi value={(f.raw_events ?? 0).toLocaleString()} label={t("funnel_raw")} />
        <Kpi value={f.meaningful_signals ?? 0} label={t("funnel_signals")} accent="#60a5fa" sub={`${f.fingerprints ?? 0} fp`} />
        <Kpi value={f.incidents ?? 0} label={t("funnel_incidents")} accent="#f87171" />
        <Kpi value={f.reduction ? `${f.reduction}×` : "-"} label={t("funnel_reduction")} accent="#a78bfa" />
      </div>
      <Tabs value={tab} onChange={setTab} tabs={[{ k: "overview", label: t("tab_overview") }, { k: "incidents", label: `${t("tab_incidents")} · ${incs.data?.length ?? ""}` }, { k: "signals", label: t("tab_signals") }, { k: "noise", label: t("tab_noise") }, { k: "search", label: t("tab_search") }, { k: "actions", label: t("tab_actions_ds") }]} />
      {tab === "actions" && <Card><Table cols={[{ k: "priority", label: t("act_priority"), render: (r) => <Badge v={r.priority} /> }, { k: "incident_id", label: t("act_incident"), render: (r) => <span className="mono">{r.incident_id}</span> }, { k: "title", label: t("act_title_f"), render: (r) => <div><b>{r.title}</b><div className="muted small">{r.recommendation?.slice(0, 120)}</div></div> }, { k: "owner", label: t("owner") }, { k: "status", label: t("status"), render: (r) => <Badge v={r.status} label={t("st_" + r.status)} /> }]} rows={acts.data ?? []} /></Card>}
      {tab === "search" && <SearchTab dataset={key} />}
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
      {tab === "noise" && noise.data && <div className="stack">
        <div className="grid k2"><Kpi value={(noise.data.eliminated ?? 0).toLocaleString()} label={t("noise_eliminated")} accent="#64748b" sub={`${(noise.data.on_cards ?? 0).toLocaleString()} ${t("noise_kept")} · ${noise.data.total ?? 0} ${t("ds_events")}`} />
          <Card title={t("noise_rules")}><Table cols={[{ k: "rule", label: t("name") }, { k: "n", label: t("count"), num: true }]} rows={Object.entries(noise.data.totals ?? {}).map(([rule, n]) => ({ id: rule, rule, n }))} /></Card></div>
        {noise.data.heat && <Card title={t("noise_heat")}><HeatMap heat={noise.data.heat} /></Card>}
        {!!noise.data.rows?.length && <Card title={t("tab_noise")}><Table cols={[{ k: "signal", label: "ID" }, { k: "reason", label: t("noise_reason") }, { k: "severity", label: t("severity"), render: (r) => <Badge v={r.severity} /> }, { k: "count", label: t("count"), num: true }, { k: "services", label: t("service") }, { k: "first", label: t("time"), render: (r) => `${hhmm(r.first)}–${hhmm(r.last)}` }, { k: "template", label: t("sig_template"), render: (r) => <span className="mono">{r.template.slice(0, 100)}</span> }]} rows={noise.data.rows.slice().sort((a: any, b: any) => b.count - a.count).map((r: any) => ({ id: r.signal, ...r }))} /></Card>}
        {!!noise.data.demoted?.length && <Card title={t("noise_low_groups")}><Table cols={[{ k: "id", label: "ID" }, { k: "severity", label: t("severity"), render: (r) => <Badge v={r.severity} /> }, { k: "count", label: t("count"), num: true }, { k: "services", label: t("service") }, { k: "first", label: t("time"), render: (r) => `${hhmm(r.first)}–${hhmm(r.last)}` }, { k: "title", label: t("sig_template") }]} rows={noise.data.demoted} /></Card>}</div>}
      {inc && <IncidentModal dataset={key} iid={inc} onClose={() => setInc(null)} />}
    </div>
  );
}

function HeatMap({ heat }: { heat: any }) {
  const max = Math.max(1, ...heat.cells.map((c: any) => c.n));
  const cell: Record<string, any> = {}; for (const c of heat.cells) cell[`${c.service}|${c.bucket}`] = c;
  return <div className="table-wrap"><table className="table" style={{ fontSize: 11 }}><thead><tr><th></th>{heat.buckets.map((b: string) => <th key={b}>{b}</th>)}</tr></thead>
    <tbody>{heat.services.slice(0, 30).map((svc: string) => <tr key={svc}><td className="mono">{svc}</td>{heat.buckets.map((b: string) => { const c = cell[`${svc}|${b}`]; const a = c ? 0.15 + 0.85 * (c.n / max) : 0; return <td key={b} title={c ? `${c.n}` : ""} style={{ padding: 0, height: 22, background: c ? `rgba(251, 146, 60, ${a})` : "transparent", outline: c?.hot ? "2px solid #2dd4bf" : undefined, textAlign: "center" }}>{c ? c.n : ""}</td>; })}</tr>)}</tbody></table></div>;
}

function SearchTab({ dataset }: { dataset: string }) {
  const { t } = useT(); const [q, setQ] = useState(""); const [regex, setRegex] = useState(false); const [sev, setSev] = useState("");
  const r = useQuery({ queryKey: ["search", dataset, q, regex, sev], queryFn: () => get(`/datasets/${dataset}/search?q=${encodeURIComponent(q)}&regex=${regex}&severity=${sev}&limit=300`) });
  return <Card><div className="row"><input className="input grow" placeholder={t("search_help")} value={q} onChange={(e) => setQ(e.target.value)} /><select className="input" style={{ width: 130 }} value={sev} onChange={(e) => setSev(e.target.value)}><option value="">{t("severity")}: {t("all")}</option>{["CRITICAL", "ERROR", "WARN", "INFO", "DEBUG"].map((x) => <option key={x}>{x}</option>)}</select><label className="check"><input type="checkbox" checked={regex} onChange={(e) => setRegex(e.target.checked)} /> {t("search_regex")}</label><span className="muted small">{r.data?.total ?? 0} {t("search_total")}</span></div>
    <div style={{ marginTop: 8 }}><Table cols={[{ k: "timestamp", label: t("time"), render: (x) => <span className="mono">{x.timestamp.slice(0, 19).replace("T", " ")}</span> }, { k: "severity", label: t("severity"), render: (x) => <Badge v={x.severity} /> }, { k: "service", label: t("service") }, { k: "host", label: t("host") }, { k: "source", label: t("ds_files"), render: (x) => <span className="dim small">{x.source}:{x.line_no}</span> }, { k: "message", label: t("message"), render: (x) => <span className="mono">{x.message}</span> }]} rows={(r.data?.rows ?? []).map((x: any) => ({ id: x.ref, ...x }))} /></div></Card>;
}

async function download(path: string, name: string) {
  const text = await api<string>(path, { text: true });
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text])); a.download = name; a.click();
}

function IncidentModal({ dataset, iid, onClose }: { dataset: string; iid: string; onClose: () => void }) {
  const { t } = useT(); const qc = useQueryClient(); const [pm, setPm] = useState<string | null>(null); const [prompt, setPrompt] = useState<string | null>(null); const [llm, setLlm] = useState<any>(null);
  const [fb, setFb] = useState({ verdict: "up", correct: "__keep__", comment: "" }); const [fbMsg, setFbMsg] = useState(""); const [custom, setCustom] = useState({ title: "", priority: "P2", owner: "" });
  const q = useQuery({ queryKey: ["incident", dataset, iid], queryFn: () => get(`/datasets/${dataset}/incidents/${iid}`) });
  const related = useQuery({ queryKey: ["related", dataset, iid], queryFn: () => get(`/datasets/${dataset}/incidents/${iid}/related`) });
  const inv = () => { qc.invalidateQueries({ queryKey: ["incident", dataset, iid] }); qc.invalidateQueries({ queryKey: ["actions"] }); qc.invalidateQueries({ queryKey: ["ds-actions", dataset] }); };
  const mkAction = useMutation({ mutationFn: (p: { title: string; priority: string; owner: string; rec: string }) => post("/actions", { incident_id: iid, title: p.title.slice(0, 120), priority: p.priority, owner: p.owner, recommendation: p.rec, evidence: q.data?.root_cause_signal ?? "" }), onSuccess: inv });
  const feedback = useMutation({ mutationFn: () => post(`/datasets/${dataset}/incidents/${iid}/feedback`, fb), onSuccess: (r: any) => { setFbMsg(t("fb_saved", { n: r.proposals })); qc.invalidateQueries({ queryKey: ["kb-rules"] }); } });
  const explain = useMutation({ mutationFn: () => post(`/datasets/${dataset}/incidents/${iid}/explain`), onSuccess: (r: any) => setLlm(r) });
  const i = q.data;
  const rk = i?.recovery?.kind ?? "unknown"; const rcol: Record<string, string> = { restart: "#2dd4bf", self_healed: "#2dd4bf", stopped: "#fbbf24", ongoing: "#f87171" };
  return (
    <Modal wide title={<span><Badge v={i?.severity ?? "low"} /> {iid} · {i?.title}</span>} onClose={onClose}>
      {!i ? <div className="muted">{t("loading")}</div> : (
        <div className="stack">
          <div className="card" style={{ borderTop: `3px solid ${rcol[rk] ?? "#8b98ad"}` }}><div className="stack small">
            <div><b>{t("fc_what")}</b> · {i.title}. {(i.signals ?? []).reduce((a: number, s: any) => a + (s.count || 0), 0).toLocaleString()} {t("ds_events")}, {i.signal_ids.length} {t("ds_signals")}. <b>{t("fc_chain")}:</b> <span className="mono">{[i.root_cause, ...(i.signals ?? []).filter((s: any) => s.id !== i.root_cause.id).slice(0, 4)].map((s: any) => `${s.id} ${s.template.slice(0, 40)}`).join(" → ")}</span></div>
            <div><b>{t("fc_why")}</b> · <span className="mono">{i.root_cause.id}</span> "{i.root_cause.template}" — {i.reason_text}</div>
            <div><b>{t("fc_where")}</b> · {t("env")}: {Object.entries(i.origin?.environments ?? {}).map(([e, n]) => `${e} (${n})`).join(", ") || "-"} · {t("service").toLowerCase()}: {i.affected_services.join(", ")} · {t("host").toLowerCase()}: {i.affected_hosts.join(", ")}</div>
            <div><b>{t("fc_when")}</b> · {i.started_at.slice(0, 10)} <span className="mono">{i.started_at.slice(11, 19)} → {i.ended_at.slice(11, 19)}</span>{i.timing?.duration_s != null && ` · ${Math.round(i.timing.duration_s / 60)} min`}</div>
            <div><b>{t("fc_resolved")}</b> · <span style={{ color: rcol[rk] ?? "#8b98ad", fontWeight: 700 }}>{rk}</span> {i.recovery?.recovered_at?.slice(11, 19)} <span className="mono">{i.recovery?.evidence}</span> {i.recovery?.what?.slice(0, 120)}</div>
            <div><b>{t("fc_todo")}</b> <span className="muted">· {t("fc_owner")}: {i.actions?.[0]?.owner || "-"}</span><ul style={{ margin: "2px 0 0", paddingLeft: 18 }}>{(i.recommendations ?? []).map((r: string, n: number) => <li key={n}>{r} <Btn sm kind="ghost" onClick={() => mkAction.mutate({ title: r, priority: i.severity === "critical" ? "P1" : "P2", owner: i.actions?.[0]?.owner || "", rec: r })}>+ {t("create_action")}</Btn></li>)}</ul></div>
            {!!i.root_cause_alternatives?.length && <div><b>{t("fc_alts")}</b><ul style={{ margin: "2px 0 0", paddingLeft: 18 }}>{i.root_cause_alternatives.slice(0, 3).map((a: any, n: number) => <li key={n}><span className="mono">{a.signal}</span> "{String(a.template ?? "").slice(0, 70)}" ({(a.services ?? []).slice(0, 2).join(", ")}) · {t("inc_score")} {Number(a.score).toFixed(1)}</li>)}</ul></div>}
            {i.playbook && (i.playbook.datasets?.length > 1 || i.playbook.occurrences > 1) && <div><b>📚 {t("pb_seen_before")}</b> {t("pb_times", { n: i.playbook.occurrences, d: i.playbook.datasets.length })} · {i.playbook.datasets.slice(0, 3).join(", ")}{i.playbook.resolution && ` · ${i.playbook.resolution.slice(0, 140)}`}</div>}
            <div className="row"><Link className="btn sm" to={`/map?dataset=${dataset}&incident=${iid}`}>🕸 {t("fc_map")}</Link></div></div></div>
          <Card title={t("inc_root")}><div className="stack small"><div className="mono">{i.root_cause.template}</div><div className="muted">{i.root_cause_reason}</div><div><b>{t("inc_why")}</b> {i.reason_text}</div></div></Card>
          <Card title={t("inc_narrative")}><p style={{ margin: 0 }}>{i.narrative_text}</p></Card>
          <div className="grid k2">
            <Card title={t("inc_factors")}><Table cols={[{ k: "name", label: t("name") }, { k: "value", label: "" }, { k: "contribution", label: "+", num: true, render: (r) => Number(r.contribution).toFixed(2) }]} rows={(i.factors ?? []).map((f: any, n: number) => ({ id: n, ...f }))} /></Card>
            <Card title={t("inc_affected")}><div className="stack small"><div><b>{t("service")}:</b> {i.affected_services.join(", ")}</div><div><b>{t("host")}:</b> {i.affected_hosts.join(", ")}</div><div><b>{t("time")}:</b> {i.started_at.slice(11, 19)} → {i.ended_at.slice(11, 19)}</div>
              {i.recovery?.kind && <div><b>{t("inc_recovery")}:</b> {i.recovery.kind} {i.recovery.recovered_at?.slice(11, 19)} {i.recovery.evidence}</div>}</div></Card>
          </div>
          <Card title={t("inc_timeline")}><div className="tl">{(i.timeline ?? []).slice(0, 30).map((e: any, n: number) => <div key={n} className="e small"><span className="mono">{String(e.time ?? e.ts ?? "").slice(11, 19)}</span> {e.event ?? e.text ?? e.template ?? JSON.stringify(e)}</div>)}</div></Card>
          <Card title={t("tab_actions")}><Table cols={[{ k: "title", label: t("act_title_f") }, { k: "priority", label: t("act_priority"), render: (r) => <Badge v={r.priority} /> }, { k: "status", label: t("status"), render: (r) => <Badge v={r.status} label={t("st_" + r.status)} /> }, { k: "owner", label: t("owner") }]} rows={i.actions ?? []} />
            <details style={{ marginTop: 8 }}><summary className="muted small">{t("custom_action")}</summary><div className="row" style={{ marginTop: 6 }}><input className="input" style={{ width: 280 }} placeholder={t("act_title_f")} value={custom.title} onChange={(e) => setCustom({ ...custom, title: e.target.value })} /><select className="input" style={{ width: 80 }} value={custom.priority} onChange={(e) => setCustom({ ...custom, priority: e.target.value })}>{["P1", "P2", "P3", "P4"].map((p) => <option key={p}>{p}</option>)}</select><input className="input" style={{ width: 160 }} placeholder={t("owner")} value={custom.owner} onChange={(e) => setCustom({ ...custom, owner: e.target.value })} /><Btn sm kind="primary" disabled={!custom.title} onClick={() => mkAction.mutate({ title: custom.title, priority: custom.priority, owner: custom.owner, rec: custom.title })}>{t("create_action")}</Btn></div></details><Err e={mkAction.error} /></Card>
          <div className="grid k2">
            <Card title={`🧠 ${t("kb_related")}`}>{!related.data?.length ? <div className="muted small">{t("kb_related_none")}</div> : related.data.map((l: any) => <div key={l.id} className="card tight" style={{ marginBottom: 6 }}><span className="chip">{l.kind}</span> <span className="muted small">×{l.occurrences ?? 1} · {Number(l.score ?? 0).toFixed(2)}</span><div><b>{String(l.title).slice(0, 110)}</b></div><div className="muted small">{String(l.text).slice(0, 260)}</div></div>)}</Card>
            <Card title={`✍️ ${t("fb_title")}`}><div className="stack">
              <div className="row"><label className="check"><input type="radio" checked={fb.verdict === "up"} onChange={() => setFb({ ...fb, verdict: "up" })} /> 👍 {t("fb_up")}</label><label className="check"><input type="radio" checked={fb.verdict === "down"} onChange={() => setFb({ ...fb, verdict: "down" })} /> 👎 {t("fb_down")}</label></div>
              <Field label={t("fb_correct")}><select className="input" value={fb.correct} onChange={(e) => setFb({ ...fb, correct: e.target.value })}><option value="__keep__">{t("fb_keep")}</option>{(i.root_cause_alternatives ?? []).slice(0, 4).map((a: any, n: number) => <option key={n} value={`alt${n}`}>{String(a.template ?? "").slice(0, 80)} ({(a.services ?? []).join(", ")})</option>)}<option value="__noise__">{t("fb_noise")}</option><option value="__other__">{t("fb_other")}</option></select></Field>
              <Field label={t("fb_comment")}><textarea className="input" style={{ minHeight: 60 }} value={fb.comment} onChange={(e) => setFb({ ...fb, comment: e.target.value })} /></Field>
              <div className="row"><Btn sm kind="primary" onClick={() => feedback.mutate()} disabled={feedback.isPending}>{t("fb_send")}</Btn>{fbMsg && <span className="ok">{fbMsg}</span>}</div><Err e={feedback.error} /></div></Card>
          </div>
          <div className="row"><span className="muted small">{t("inc_evidence")}: {(i.evidence ?? []).slice(0, 8).join(" · ")}</span></div>
          <div className="row"><Btn sm onClick={async () => setPm(await api<string>(`/datasets/${dataset}/incidents/${iid}/postmortem`, { text: true }))}>{t("inc_postmortem")}</Btn>
            <Btn sm onClick={async () => setPrompt(await api<string>(`/datasets/${dataset}/incidents/${iid}/prompt`, { text: true }))}>{t("llm_prompt")}</Btn><Btn sm onClick={() => explain.mutate()} disabled={explain.isPending}>✨ {t("llm_explain")}</Btn></div>
          <Err e={explain.error} />
          {llm && <Card title={`${t("llm_answer")} · ${llm.model}`}><div style={{ whiteSpace: "pre-wrap" }}>{llm.text}</div></Card>}
          {prompt && <pre className="code">{prompt}</pre>}
          {pm && <pre className="code">{pm}</pre>}
        </div>)}
    </Modal>
  );
}
