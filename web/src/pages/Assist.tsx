import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { get, post } from "../api";
import { useT } from "../i18n";
import { Btn, Card, Err } from "../components/ui";

type Msg = { role: "user" | "assistant"; content: string; meta?: any; rated?: string };

export default function Assist() {
  const { t, lang } = useT();
  const ds = useQuery({ queryKey: ["datasets"], queryFn: () => get("/datasets") });
  const llm = useQuery({ queryKey: ["llm"], queryFn: () => get("/llm") });
  const [dataset, setDataset] = useState(""); const [q, setQ] = useState(""); const [hist, setHist] = useState<Msg[]>(() => { try { return JSON.parse(sessionStorage.getItem("wo_chat") || "[]"); } catch { return []; } });
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => { try { sessionStorage.setItem("wo_chat", JSON.stringify(hist.slice(-40))); } catch { /* ignore */ } endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [hist]);
  const ask = useMutation({ mutationFn: (question: string) => post("/assist/ask", { question, history: hist.map((m) => ({ role: m.role, content: m.content })), dataset: dataset || (ds.data?.[0]?.id ?? null), lang }),
    onSuccess: (r: any) => setHist((h) => [...h, { role: "assistant", content: r.text, meta: r }]) });
  const send = (question: string) => { if (!question.trim()) return; setHist((h) => [...h, { role: "user", content: question }]); setQ(""); ask.mutate(question); };
  const rate = async (i: number, verdict: string) => { const m = hist[i]; const qq = hist[i - 1]?.content ?? ""; await post("/assist/rate", { question: qq, answer: m.content, verdict }); setHist((h) => h.map((x, n) => (n === i ? { ...x, rated: verdict } : x))); };
  const save = async (i: number) => { await post("/assist/save", { question: hist[i - 1]?.content ?? "", answer: hist[i].content }); };
  return <div className="stack">
    <div className="row between"><div><h2>💬 {t("as_title")}</h2><span className="muted small">{t("as_sub")}</span></div>
      <div className="row"><span className="chip"><span className="d" style={{ background: llm.data?.enabled ? "#2dd4bf" : "#64748b" }} />LLM: {llm.data?.enabled ? llm.data.llm_model : "—"}</span>
        <select className="input" style={{ width: 220 }} value={dataset} onChange={(e) => setDataset(e.target.value)}><option value="">{t("as_dataset")}: {ds.data?.[0]?.name ?? "—"}</option>{(ds.data ?? []).map((x: any) => <option key={x.id} value={x.id}>{x.name}</option>)}<option value="live">{t("map_live")}</option></select>
        {hist.length > 0 && <Btn sm onClick={() => setHist([])}>{t("as_clear")}</Btn>}</div></div>
    {!hist.length && <Card><p style={{ margin: 0 }}>{t("as_hello")}</p><div className="row" style={{ marginTop: 10 }}>{["as_ex1", "as_ex2", "as_ex3"].map((k) => <Btn key={k} sm onClick={() => send(t(k))}>{t(k)}</Btn>)}</div></Card>}
    <div className="stack">{hist.map((m, i) => (
      <div key={i} className="card" style={{ marginLeft: m.role === "user" ? "12%" : 0, marginRight: m.role === "user" ? 0 : "12%", background: m.role === "user" ? "rgba(45,212,191,0.08)" : undefined }}>
        <div style={{ whiteSpace: "pre-wrap" }}>{m.content}</div>
        {m.role === "assistant" && <div className="stack small" style={{ marginTop: 8 }}>
          <div className="muted">{m.meta?.used_llm ? t("as_via_llm", { m: m.meta.model }) : t("as_via_ctx")}{m.meta?.error && <span className="badge amber" style={{ marginLeft: 6 }}>{String(m.meta.error).slice(0, 80)}</span>}</div>
          {!!m.meta?.sources?.length && <details><summary className="muted">📎 {t("as_sources")} · {m.meta.sources.length}</summary>{m.meta.sources.map((s: any, n: number) => <div key={n} className="card tight" style={{ marginTop: 6 }}><b>{s.title?.slice(0, 100)}</b><div className="muted">{String(s.text).slice(0, 300)}</div></div>)}</details>}
          {m.meta?.context && <details><summary className="muted">🔍 {t("as_context")}</summary><pre className="code">{String(m.meta.context).slice(0, 6000)}</pre></details>}
          <div className="row"><Btn sm onClick={() => save(i)}>{t("as_save")}</Btn>{!m.rated ? <><Btn sm onClick={() => rate(i, "up")}>👍</Btn><Btn sm onClick={() => rate(i, "down")}>👎</Btn></> : <span className="muted">{m.rated === "up" ? "👍" : "👎"}</span>}</div></div>}
      </div>))}{ask.isPending && <div className="muted">{t("as_thinking")}</div>}<div ref={endRef} /></div>
    <Err e={ask.error} />
    <form className="row" onSubmit={(e) => { e.preventDefault(); send(q); }}><input className="input grow" placeholder={t("as_input")} value={q} onChange={(e) => setQ(e.target.value)} /><Btn kind="primary" type="submit" disabled={ask.isPending}>{t("as_send")}</Btn></form>
  </div>;
}
