import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { get, post } from "../api";
import { useT } from "../i18n";
import { Btn, Card, Err, Field } from "../components/ui";

/** "Ask the LLM" box used inside the incident card and on the dataset's LLM review tab. */
export function LlmReview({ dataset, incident, compact }: { dataset: string; incident?: string; compact?: boolean }) {
  const { t } = useT(); const [scope, setScope] = useState(incident ? "incident" : "dataset"); const [query, setQuery] = useState(""); const [question, setQuestion] = useState(""); const [hist, setHist] = useState<any[]>([]);
  const llm = useQuery({ queryKey: ["llm-settings"], queryFn: () => get("/llm/settings"), staleTime: 60000 });
  const run = useMutation({ mutationFn: () => post(`/datasets/${dataset}/review`, { scope, incident: incident ?? "", query, question }), onSuccess: (r: any) => setHist((h) => [{ ...r, at: new Date().toLocaleTimeString(), question, query }, ...h].slice(0, 8)) });
  const enabled = llm.data?.enabled ?? llm.data?.configured ?? true; const model = llm.data?.model ?? "";
  const body = (txt: string) => txt.split("\n").map((ln, i) => { const b = ln.match(/^\*\*(.+?)\*\*:?\s*(.*)$/); return b ? <div key={i} style={{ marginTop: 8 }}><b>{b[1]}</b>{b[2] ? ` ${b[2]}` : ""}</div> : ln.trim().startsWith("-") || ln.trim().startsWith("•") ? <div key={i} style={{ paddingLeft: 14 }}>{ln.trim()}</div> : <div key={i}>{ln}</div>; });
  return <Card title={<span>🤖 {t("rv_title")}{model && <span className="muted small"> · {model}</span>}</span>}>
    {!compact && <div className="muted small" style={{ marginBottom: 8 }}>{t("rv_lead")}</div>}
    {!enabled && <div className="err">{t("rv_no_llm")} <Link className="btn sm" to="/llm">{t("rv_open_llm")}</Link></div>}
    <div className="row" style={{ alignItems: "flex-end", flexWrap: "wrap" }}>
      {!incident && <Field label={t("rv_scope")}><select className="input" value={scope} onChange={(e) => setScope(e.target.value)}><option value="dataset">{t("rv_scope_dataset")}</option><option value="lines">{t("rv_scope_lines")}</option></select></Field>}
      {scope === "lines" && <Field label={t("rv_query")}><input className="input" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="ERROR payment" /></Field>}
      <Field label={t("rv_question")}><input className="input" style={{ minWidth: 260 }} value={question} onChange={(e) => setQuestion(e.target.value)} /></Field>
      <Btn kind="primary" onClick={() => run.mutate()} disabled={run.isPending || !enabled || (scope === "lines" && !query)}>{run.isPending ? t("rv_running") : `✨ ${t("rv_run")}`}</Btn>
    </div>
    <Err e={run.error} />
    {hist.map((r, i) => <div key={i} className="card" style={{ marginTop: 10, borderLeft: "3px solid var(--accent)" }}>
      <div className="row between"><span className="muted small">{r.at} · {t("rv_model")}: {r.model} · {t("rv_scope")}: {r.scope}{r.query ? ` "${r.query}"` : ""} · {(r.evidence_chars / 1000).toFixed(1)}k {t("rv_evidence")}{r.grounding ? ` · ${t("rv_grounded")} ${Math.round((r.grounding.grounded ?? 0) * 100)}%` : ""}</span><Btn sm onClick={() => navigator.clipboard?.writeText(r.text)}>{t("rv_copy")}</Btn></div>
      <div className="small" style={{ marginTop: 6, lineHeight: 1.5 }}>{body(r.text)}</div></div>)}
  </Card>;
}
