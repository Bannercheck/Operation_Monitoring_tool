import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { del, get, post, put } from "../api";
import { useT } from "../i18n";
import { Btn, Card, Confirm, Empty, Err, Field, Kpi, Table, Tabs } from "../components/ui";
import { useSearchParams } from "react-router-dom";
import { ParserPanel } from "./Parser";

export default function Llm() {
  const { t } = useT(); const qc = useQueryClient(); const [sp] = useSearchParams(); const [tab, setTab] = useState<"conn" | "models" | "quality" | "parser">((sp.get("tab") as any) || "conn");
  const cfg = useQuery({ queryKey: ["llm"], queryFn: () => get("/llm") });
  const [f, setF] = useState<any>(null); const [msg, setMsg] = useState<any>(null); const [models, setModels] = useState<string[]>([]);
  useEffect(() => { if (cfg.data && !f) setF(cfg.data); }, [cfg.data, f]);
  const save = useMutation({ mutationFn: () => put("/llm", { llm_provider: f.llm_provider, llm_base: f.llm_base, llm_model: f.llm_model, llm_key: f.llm_key, llm_embed: f.llm_embed }), onSuccess: (r: any) => { setF(r); setMsg({ ok: true, info: t("saved") }); qc.invalidateQueries({ queryKey: ["llm"] }); } });
  const test = useMutation({ mutationFn: () => post("/llm/test"), onSuccess: (r: any) => setMsg(r) });
  const list = useMutation({ mutationFn: () => get("/llm/models"), onSuccess: (r: any) => setModels(r.models) });
  const st = cfg.data?.stats ?? {};
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  return <div className="stack">
    <div className="row between"><div><h2>🧠 {t("llm_title")}</h2><span className="muted small">{t("llm_sub")}</span></div>
      <div className="row"><span className="chip"><span className="d" style={{ background: cfg.data?.enabled ? "#2dd4bf" : "#64748b" }} />{cfg.data?.kind} · {cfg.data?.llm_model || "—"}</span><span className="chip">{st.calls ?? 0} {t("llm_calls")}</span>
        <span className="chip">{t("llm_success")} {st.success != null ? `${Math.round(st.success * 100)}%` : "-"}</span><span className="chip">{t("llm_ground")} {st.grounding_rate != null ? `${Math.round(st.grounding_rate * 100)}%` : "-"}</span></div></div>
    <Tabs value={tab} onChange={setTab} tabs={[{ k: "conn", label: t("llm_tab_conn") }, { k: "models", label: t("llm_tab_models") }, { k: "quality", label: t("llm_tab_quality") }, { k: "parser", label: `🧩 ${t("llm_tab_parser")}` }]} />
    {tab === "conn" && f && <Card><div className="grid k2">
      <Field label={t("llm_provider")}><select className="input" value={f.llm_provider} onChange={set("llm_provider")}>{(cfg.data?.providers ?? []).map((p: string) => <option key={p}>{p}</option>)}</select></Field>
      <Field label={t("llm_base")}><input className="input" value={f.llm_base} onChange={set("llm_base")} placeholder="http://localhost:11434 · https://api.openai.com/v1" /></Field>
      <Field label={t("llm_model")}><div className="row"><input className="input grow" value={f.llm_model} onChange={set("llm_model")} placeholder="qwen2.5:7b-instruct" list="llm-models" /><datalist id="llm-models">{models.map((m) => <option key={m} value={m} />)}</datalist><Btn sm onClick={() => list.mutate()}>{t("llm_list")}</Btn></div></Field>
      <Field label={t("llm_key")}><input className="input" type="password" value={f.llm_key} onChange={set("llm_key")} autoComplete="off" /></Field>
      <Field label={t("llm_embed")}><input className="input" value={f.llm_embed} onChange={set("llm_embed")} placeholder="bge-m3" /></Field></div>
      <div className="row" style={{ marginTop: 12 }}><Btn kind="primary" onClick={() => save.mutate()}>{t("save")}</Btn><Btn onClick={() => test.mutate()} disabled={test.isPending}>{t("llm_test")}</Btn></div>
      {msg && <div className={msg.ok ? "ok" : "err"} style={{ marginTop: 8 }}>{msg.info}</div>}<Err e={save.error || test.error || list.error} /></Card>}
    {tab === "models" && <OllamaModels />}
    {tab === "quality" && <Quality />}
    {tab === "parser" && <ParserPanel embedded />}
  </div>;
}

function OllamaModels() {
  const { t } = useT(); const qc = useQueryClient(); const [custom, setCustom] = useState("");
  const o = useQuery({ queryKey: ["ollama"], queryFn: () => get("/llm/ollama"), refetchInterval: (q) => ((q.state.data as any)?.pulls?.some((p: any) => p.state === "running") ? 2000 : 15000) });
  const inv = () => { qc.invalidateQueries({ queryKey: ["ollama"] }); qc.invalidateQueries({ queryKey: ["llm"] }); };
  const use = useMutation({ mutationFn: (name: string) => put("/llm", /embed|bge|minilm|e5-/.test(name) ? { llm_embed: name, llm_base: o.data.base, llm_provider: "ollama" } : { llm_model: name, llm_base: o.data.base, llm_provider: "ollama" }), onSuccess: inv });
  const pull = useMutation({ mutationFn: (name: string) => post("/llm/ollama/pull", { name }), onSuccess: inv });
  const rm = useMutation({ mutationFn: (name: string) => del(`/llm/ollama/${encodeURIComponent(name)}`), onSuccess: inv });
  const d = o.data; if (!d) return null;
  if (!d.base) return <Empty>{t("llm_none")}</Empty>;
  const norm = (n: string) => n.replace(/:latest$/, "");          // Ollama reports "bge-m3:latest", the recommendation says "bge-m3"
  const have = new Set((d.installed ?? []).map((m: any) => norm(m.name))); const running = new Set((d.running ?? []).map((m: any) => m.name));
  return <div className="stack">
    <div className="grid k3"><Kpi value={d.installed.length} label={t("llm_installed")} accent="#60a5fa" sub={`${d.installed.reduce((a: number, m: any) => a + (m.size_gb || 0), 0).toFixed(1)} GB · ${d.base}`} /><Kpi value={d.running.length} label={t("llm_loaded")} sub={d.running.map((m: any) => m.name).join(", ") || "-"} /><Kpi value={(d.pulls ?? []).filter((p: any) => p.state === "running").length} label={t("llm_pull")} accent="#a78bfa" /></div>
    {!!d.pulls?.length && <Card>{d.pulls.map((p: any) => <div key={p.name} className="stack" style={{ gap: 4 }}><div className="row between"><b>{p.name}</b><span className="mono">{p.state} {p.pct}% {p.status}</span></div><div className="progress"><div style={{ width: `${p.pct}%` }} /></div>{p.error && <div className="err">{p.error}</div>}</div>)}</Card>}
    <Card title={t("llm_installed")}><Table cols={[{ k: "name", label: t("name"), render: (r) => <span><b>{r.name}</b> {running.has(r.name) && <span className="badge green">{t("llm_loaded")}</span>}</span> }, { k: "size_gb", label: "GB", num: true }, { k: "params", label: "params", render: (r) => r.params || r.family }, { k: "quant", label: "quant" },
      { k: "x", label: "", render: (r) => <div className="row"><Btn sm onClick={() => use.mutate(r.name)}>{t("llm_use")}</Btn><Confirm onConfirm={() => rm.mutate(r.name)}>{t("delete")}</Confirm></div> }]} rows={d.installed} /></Card>
    <Card title={t("llm_recommended")}><Table cols={[{ k: "name", label: t("name"), render: (r) => <b>{r.name}</b> }, { k: "role", label: "", render: (r) => t("llm_role_" + r.role) }, { k: "note", label: t("note") }, { k: "x", label: "", render: (r) => have.has(norm(r.name)) ? <span className="badge green">{t("llm_have")}</span> : <Btn sm onClick={() => pull.mutate(r.name)}>⬇ {t("llm_pull")}</Btn> }]} rows={d.recommended.map((r: any) => ({ id: r.name, ...r }))} />
      <div className="row" style={{ marginTop: 8 }}><input className="input" style={{ width: 260 }} placeholder={t("llm_pull_custom")} value={custom} onChange={(e) => setCustom(e.target.value)} /><Btn sm onClick={() => pull.mutate(custom)} disabled={!custom}>⬇ {t("llm_pull")}</Btn></div></Card>
    <Err e={use.error || pull.error || rm.error} />
  </div>;
}

function Quality() {
  const { t } = useT(); const q = useQuery({ queryKey: ["llm-quality"], queryFn: () => get("/llm/quality") });
  const ds = useQuery({ queryKey: ["datasets"], queryFn: () => get("/datasets") });
  const bench = useMutation({ mutationFn: () => post(`/llm/benchmark?dataset=${ds.data?.[0]?.id ?? ""}`), onSuccess: () => q.refetch() });
  const s = q.data?.stats ?? {};
  return <div className="stack">
    <div className="grid k4"><Kpi value={s.calls ?? 0} label={t("llm_calls")} /><Kpi value={s.success != null ? `${Math.round(s.success * 100)}%` : "-"} label={t("llm_success")} accent="#2dd4bf" /><Kpi value={s.grounding_rate != null ? `${Math.round(s.grounding_rate * 100)}%` : "-"} label={t("llm_ground")} accent="#60a5fa" /><Kpi value={s.latency_ms ?? s.p50_ms ?? "-"} label="ms" accent="#a78bfa" /></div>
    <div className="row"><Btn onClick={() => bench.mutate()} disabled={bench.isPending || !ds.data?.length}>{t("llm_benchmark")}</Btn></div><Err e={bench.error} />
    <Card title={t("llm_tab_quality")}><Table cols={[{ k: "ts", label: t("time"), render: (r) => r.ts?.slice(0, 16).replace("T", " ") }, { k: "model", label: t("llm_model") }, { k: "dataset", label: t("as_dataset") }, { k: "n", label: "n", num: true }, { k: "correct", label: "✓", num: true }, { k: "cited", label: "cite", num: true }, { k: "grounded", label: t("llm_ground"), num: true }, { k: "latency_ms", label: "ms", num: true }]} rows={q.data?.evals ?? []} /></Card>
    <Card title={t("llm_calls")}><Table cols={[{ k: "ts", label: t("time"), render: (r) => r.ts?.slice(11, 19) }, { k: "provider", label: t("llm_provider") }, { k: "model", label: t("llm_model") }, { k: "kind", label: "kind" }, { k: "ok", label: "ok", render: (r) => (r.ok ? "✓" : "✗") }, { k: "latency_ms", label: "ms", num: true }, { k: "citations", label: "cite", num: true }, { k: "error", label: t("error"), render: (r) => <span className="muted small">{r.error?.slice(0, 60)}</span> }]} rows={(q.data?.calls ?? []).slice(0, 60)} /></Card>
  </div>;
}
