import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, del, get, post } from "../api";
import { useT } from "../i18n";
import { Badge, Btn, Card, Confirm, Empty, Err, Field, Modal, Table, fmtTs } from "../components/ui";

export default function Datasets() {
  const { t } = useT(); const qc = useQueryClient();
  const [drag, setDrag] = useState(false); const [combine, setCombine] = useState(true); const [err, setErr] = useState<any>(null); const [cmp, setCmp] = useState<string[]>([]); const [fetchOpen, setFetchOpen] = useState(""); const [mapping, setMapping] = useState("");
  const samples = useQuery({ queryKey: ["samples"], queryFn: () => get("/datasets/samples") });
  const loadSample = useMutation({ mutationFn: (name: string) => post(`/datasets/samples/${encodeURIComponent(name)}`), onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }) });
  const fileRef = useRef<HTMLInputElement>(null);
  const list = useQuery({ queryKey: ["datasets"], queryFn: () => get("/datasets") });
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: () => get("/datasets/jobs"), refetchInterval: (q) => ((q.state.data as any[])?.some((j) => j.state === "running") ? 1000 : 5000) });
  const running = (jobs.data ?? []).filter((j: any) => j.state === "running").length;
  useEffect(() => { qc.invalidateQueries({ queryKey: ["datasets"] }); }, [running, qc]);
  const upload = async (files: FileList | File[]) => {
    const fd = new FormData(); Array.from(files).forEach((f) => fd.append("files", f));
    setErr(null);
    if (mapping.trim()) fd.append("mapping", mapping);
    try { await api(`/datasets?combine=${combine}`, { method: "POST", form: fd }); qc.invalidateQueries({ queryKey: ["jobs"] }); } catch (e) { setErr(e); }
  };
  const demo = useMutation({ mutationFn: () => post("/datasets/demo"), onSuccess: () => qc.invalidateQueries({ queryKey: ["datasets"] }) });
  const remove = useMutation({ mutationFn: (k: string) => del(`/datasets/${k}`), onSuccess: () => qc.invalidateQueries({ queryKey: ["datasets"] }) });
  return (
    <div className="stack">
      <div className="row between"><h2>{t("ds_title")}</h2><div className="row">{(list.data?.length ?? 0) >= 2 && <Btn onClick={() => setCmp([list.data[0].id, list.data[1].id])}>{t("ds_compare")}</Btn>}<Btn onClick={() => demo.mutate()} disabled={demo.isPending}>{t("ds_demo")}</Btn></div></div>
      <Card title={t("ds_upload")}>
        <div className={`drop ${drag ? "on" : ""}`} onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)} onDrop={(e) => { e.preventDefault(); setDrag(false); upload(e.dataTransfer.files); }} onClick={() => fileRef.current?.click()}>
          📂 {t("ds_drop")}<input ref={fileRef} type="file" multiple style={{ display: "none" }} onChange={(e) => e.target.files && upload(e.target.files)} /></div>
        <label className="check" style={{ marginTop: 8 }}><input type="checkbox" checked={combine} onChange={(e) => setCombine(e.target.checked)} /> {t("ds_combine")}</label>
        <details><summary className="muted small">{t("ds_mapping")}</summary><textarea className="input mono" style={{ minHeight: 60 }} placeholder='{"timestamp": "ts", "message": "msg", "severity": "level"}' value={mapping} onChange={(e) => setMapping(e.target.value)} /></details>
        <div className="row" style={{ marginTop: 8 }}><span className="muted small">{t("ds_fetch")}:</span><Btn sm onClick={() => setFetchOpen("http")}>🌐 {t("ds_fetch_http")}</Btn><Btn sm onClick={() => setFetchOpen("mcp")}>🧩 {t("ds_fetch_mcp")}</Btn><Btn sm onClick={() => setFetchOpen("live")}>📡 {t("ds_fetch_live")}</Btn><Btn sm onClick={() => setFetchOpen("source")}>🔎 {t("ds_fetch_source")}</Btn>{(samples.data ?? []).map((x: any) => <Btn key={x.name} sm onClick={() => loadSample.mutate(x.name)}>{t("ds_samples")}: {x.name} <span className="dim">({(x.size / 1e6).toFixed(1)} MB)</span></Btn>)}</div>
        <Err e={err || demo.error} />
      </Card>
      {!!jobs.data?.length && <Card title={t("ds_jobs")}>
        <div className="stack">{jobs.data.map((j: any) => (
          <div key={j.id} className="stack" style={{ gap: 4 }}><div className="row between"><span><b>{j.name}</b> <span className="muted small">{(j.size / 1e6).toFixed(1)} MB · {j.state === "running" ? t("ds_stage_" + j.stage) : j.state === "done" ? t("ds_done") : t("ds_failed")}{j.file && ` · ${j.file}`}</span></span>
            <span className="mono">{j.pct}% · {j.seconds}{t("ds_elapsed")}</span></div><div className="progress"><div style={{ width: `${j.pct}%` }} /></div>{j.error && <div className="err">{j.error}</div>}</div>))}</div></Card>}
      <Card title={t("ds_loaded")}>
        {!list.data?.length ? <Empty /> : <Table cols={[
          { k: "name", label: t("name"), render: (r) => <Link to={`/datasets/${r.id}`}><b>{r.name}</b></Link> },
          { k: "events", label: t("ds_events"), num: true, render: (r) => r.events.toLocaleString() }, { k: "signals", label: t("ds_signals"), num: true }, { k: "incidents", label: t("ds_incidents"), num: true },
          { k: "elapsed", label: t("time"), num: true, render: (r) => `${r.elapsed}${t("ds_elapsed")}` }, { k: "loaded_at", label: "", render: (r) => <span className="muted small">{fmtTs(r.loaded_at)} · {r.stamp?.result?.slice(0, 10)}</span> },
          { k: "x", label: "", render: (r) => <div className="row"><Link className="btn sm" to={`/datasets/${r.id}`}>{t("ds_open")}</Link><Confirm onConfirm={() => remove.mutate(r.id)}>{t("delete")}</Confirm></div> }]} rows={list.data} />}
      </Card>
      {cmp.length === 2 && <CompareModal list={list.data ?? []} a={cmp[0]} b={cmp[1]} onPick={setCmp} onClose={() => setCmp([])} />}
      {fetchOpen && <FetchModal kind={fetchOpen} onClose={() => { setFetchOpen(""); qc.invalidateQueries({ queryKey: ["jobs"] }); }} />}
    </div>
  );
}

function CompareModal({ list, a, b, onPick, onClose }: { list: any[]; a: string; b: string; onPick: (x: string[]) => void; onClose: () => void }) {
  const { t } = useT();
  const r = useQuery({ queryKey: ["compare", a, b], queryFn: () => get(`/datasets/compare?a=${a}&b=${b}`) });
  const d = r.data;
  return <Modal wide title={t("ds_compare")} onClose={onClose}><div className="stack">
    <div className="row"><select className="input" value={a} onChange={(e) => onPick([e.target.value, b])}>{list.map((x) => <option key={x.id} value={x.id}>A · {x.name}</option>)}</select><select className="input" value={b} onChange={(e) => onPick([a, e.target.value])}>{list.map((x) => <option key={x.id} value={x.id}>B · {x.name}</option>)}</select></div>
    {d && <><Table cols={[{ k: "metric", label: t("cmp_metric") }, { k: "a", label: "A", num: true }, { k: "b", label: "B", num: true }, { k: "delta", label: "Δ", num: true, render: (x) => <span style={{ color: x.delta > 0 ? "#f87171" : x.delta < 0 ? "#34d399" : undefined }}>{x.delta ?? "-"}{x.delta_pct != null ? ` (${x.delta_pct}%)` : ""}</span> }]} rows={d.kpis.map((x: any) => ({ id: x.metric, ...x }))} />
      <Card title={t("cmp_shared")}><Table cols={[{ k: "severity", label: t("severity"), render: (x) => <Badge v={x.severity} /> }, { k: "template", label: t("sig_template"), render: (x) => <span className="mono">{x.template.slice(0, 100)}</span> }, { k: "count_a", label: "A", num: true }, { k: "count_b", label: "B", num: true }, { k: "delta", label: "Δ", num: true }]} rows={d.shared.slice(0, 30).map((x: any) => ({ id: x.template, ...x }))} /></Card>
      <div className="grid k2"><Card title={t("cmp_only_a")}><Table cols={[{ k: "severity", label: "", render: (x) => <Badge v={x.severity} /> }, { k: "template", label: t("sig_template"), render: (x) => <span className="mono">{x.template.slice(0, 80)}</span> }, { k: "count", label: t("count"), num: true }]} rows={d.only_a.slice(0, 20).map((x: any) => ({ id: x.template, ...x }))} /></Card>
        <Card title={t("cmp_only_b")}><Table cols={[{ k: "severity", label: "", render: (x) => <Badge v={x.severity} /> }, { k: "template", label: t("sig_template"), render: (x) => <span className="mono">{x.template.slice(0, 80)}</span> }, { k: "count", label: t("count"), num: true }]} rows={d.only_b.slice(0, 20).map((x: any) => ({ id: x.template, ...x }))} /></Card></div></>}
  </div></Modal>;
}


function FetchModal({ kind: k0, onClose }: { kind?: string; onClose: () => void }) {
  const { t } = useT(); const [f, setF] = useState({ kind: k0 ?? "http", url: "", headers: "", method: "GET", body: "", path: "", tool: "", arguments: "{}", source: "", minutes: 60, limit: 20000 }); const [tools, setTools] = useState<string[]>([]); const [err, setErr] = useState<any>(null); const [busy, setBusy] = useState(false);
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  const srcs = useQuery({ queryKey: ["sources"], queryFn: () => get("/sources"), enabled: f.kind === "source" });
  const live = useQuery({ queryKey: ["live-stats-mini"], queryFn: () => get("/live/stats?window=60"), enabled: f.kind === "live" });
  const go = async () => { setErr(null); setBusy(true); try {
    if (f.kind === "live") await post("/live/analyze");
    else if (f.kind === "source") await post("/datasets/from-source", { id: Number(f.source), minutes: Number(f.minutes), limit: Number(f.limit) });
    else await post("/datasets/fetch", { ...f, arguments: JSON.parse(f.arguments || "{}") });
    onClose(); } catch (x) { setErr(x); } finally { setBusy(false); } };
  const listTools = async () => { setErr(null); try { setTools(await post("/datasets/mcp-tools", { ...f, arguments: {} })); } catch (x) { setErr(x); } };
  const KINDS = [["http", "🌐", t("ds_fetch_http"), "REST / JSON / text"], ["mcp", "🧩", t("ds_fetch_mcp"), "tools/call"], ["live", "📡", t("ds_fetch_live"), t("ops_analyze")], ["source", "🔎", t("ds_fetch_source"), "Elasticsearch · Loki · Splunk · Graylog"]];
  const ready = f.kind === "live" ? true : f.kind === "source" ? !!f.source : !!f.url;
  return <Modal wide title={t("ds_fetch")} onClose={onClose}><div className="stack">
    <div className="kinds">{KINDS.map(([k, ic, lb, d]) => <button key={k} className={f.kind === k ? "on" : ""} onClick={() => setF({ ...f, kind: k })}><div className="kt">{ic} {lb}</div><div className="kd">{d}</div></button>)}</div>
    {(f.kind === "http" || f.kind === "mcp") && <>
      <Field label={t("ds_fetch_url")}><input className="input" value={f.url} onChange={set("url")} placeholder={f.kind === "http" ? "https://api.example.com/alerts" : "http://localhost:8765/mcp"} /></Field>
      {f.kind === "mcp" && <div className="muted small">{t("ds_fetch_mcp_hint")}</div>}
      <Field label={t("ds_fetch_headers")}><textarea className="input" style={{ minHeight: 56 }} value={f.headers} onChange={set("headers")} placeholder="Authorization: Bearer …" /></Field>
      {f.kind === "http" ? <div className="grid k2"><Field label="Method"><select className="input" value={f.method} onChange={set("method")}><option>GET</option><option>POST</option></select></Field><Field label={t("ds_fetch_path")}><input className="input" value={f.path} onChange={set("path")} placeholder="data.events" /></Field>{f.method === "POST" && <Field label="Body"><textarea className="input" value={f.body} onChange={set("body")} /></Field>}</div>
        : <div className="grid k2"><Field label={t("ds_fetch_tool")}><div className="row"><input className="input grow" value={f.tool} onChange={set("tool")} list="mcp-tools" /><datalist id="mcp-tools">{tools.map((x) => <option key={x} value={x} />)}</datalist><Btn sm onClick={listTools}>{t("ds_fetch_tools")}</Btn></div></Field><Field label="Arguments (JSON)"><input className="input mono" value={f.arguments} onChange={set("arguments")} /></Field></div>}</>}
    {f.kind === "live" && <div className="stack"><div className="muted small">{t("ds_fetch_live_hint")}</div>{live.data && <div className="chips"><span className="chip">{(live.data.received ?? 0).toLocaleString()} {t("ds_events")}</span></div>}</div>}
    {f.kind === "source" && <div className="stack"><div className="muted small">{t("ds_fetch_source_hint")}</div>
      {srcs.data && !srcs.data.length ? <div className="row"><span className="muted small">{t("ds_fetch_nosrc")}</span><Link className="btn sm" to="/sources">{t("ds_fetch_define")}</Link></div>
        : <div className="grid k3"><Field label={t("ds_fetch_pick")}><select className="input" value={f.source} onChange={set("source")}><option value="">-</option>{(srcs.data ?? []).map((x: any) => <option key={x.id} value={x.id}>{x.name} · {x.kind}</option>)}</select></Field>
          <Field label={t("ds_fetch_minutes")}><input className="input" type="number" min={1} max={10080} value={f.minutes} onChange={set("minutes")} /></Field><Field label={t("ds_fetch_limit")}><input className="input" type="number" min={1} max={200000} value={f.limit} onChange={set("limit")} /></Field></div>}</div>}
    <Err e={err} /><div className="row"><Btn kind="primary" onClick={go} disabled={!ready || busy}>{t("ds_fetch_go")}</Btn></div></div></Modal>;
}
