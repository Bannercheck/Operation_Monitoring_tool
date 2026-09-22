import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { MessageSquare, X } from "lucide-react";
import { get, post } from "../api";
import { useT } from "../i18n";
import { Btn, Err } from "./ui";

type Msg = { role: "user" | "assistant"; content: string; meta?: any; rated?: string };

/** "Ask Watchover" as a chat bubble in the bottom-right corner of every page; the /assist page stays for the full view.
 *  History is shared with that page through sessionStorage. */
export function ChatWidget() {
  const { t, lang } = useT(); const [open, setOpen] = useState(false); const [q, setQ] = useState(""); const [dataset, setDataset] = useState("");
  const [hist, setHist] = useState<Msg[]>(() => { try { return JSON.parse(sessionStorage.getItem("wo_chat") || "[]"); } catch { return []; } });
  const ds = useQuery({ queryKey: ["datasets"], queryFn: () => get("/datasets"), enabled: open });
  const llm = useQuery({ queryKey: ["llm"], queryFn: () => get("/llm"), enabled: open, staleTime: 60000 });
  const endRef = useRef<HTMLDivElement>(null); const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => { try { sessionStorage.setItem("wo_chat", JSON.stringify(hist.slice(-40))); } catch { /* ignore */ } endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [hist, open]);
  useEffect(() => { if (open) setTimeout(() => inputRef.current?.focus(), 50); }, [open]);
  const ask = useMutation({ mutationFn: (question: string) => post("/assist/ask", { question, history: hist.map((m) => ({ role: m.role, content: m.content })), dataset: dataset || (ds.data?.[0]?.id ?? null), lang }),
    onSuccess: (r: any) => setHist((h) => [...h, { role: "assistant", content: r.text, meta: r }]) });
  const send = (question: string) => { if (!question.trim()) return; setHist((h) => [...h, { role: "user", content: question }]); setQ(""); ask.mutate(question); };
  const rate = async (i: number, verdict: string) => { const m = hist[i]; await post("/assist/rate", { question: hist[i - 1]?.content ?? "", answer: m.content, verdict }); setHist((h) => h.map((x, n) => (n === i ? { ...x, rated: verdict } : x))); };
  return <>
    {!open && <button className="chat-fab" onClick={() => setOpen(true)} title={t("as_title")}><MessageSquare size={20} /><span>{t("as_open")}</span></button>}
    {open && <div className="chat-panel">
      <div className="chat-head">
        <div><b>💬 {t("as_title")}</b><div className="dim small">{llm.data?.enabled ? `LLM: ${llm.data.llm_model}` : t("as_via_ctx")}</div></div>
        <div className="row" style={{ gap: 4, flexWrap: "nowrap" }}>
          <select className="input" style={{ width: 120, padding: "4px 6px", fontSize: 12 }} value={dataset} onChange={(e) => setDataset(e.target.value)}><option value="">{ds.data?.[0]?.name ?? t("as_dataset")}</option>{(ds.data ?? []).map((x: any) => <option key={x.id} value={x.id}>{x.name}</option>)}<option value="live">{t("map_live")}</option></select>
          <Link className="btn sm" to="/assist" title={t("as_full")} onClick={() => setOpen(false)}>⤢</Link>
          {hist.length > 0 && <Btn sm onClick={() => setHist([])} title={t("as_clear")}>🗑</Btn>}
          <Btn sm onClick={() => setOpen(false)}><X size={14} /></Btn>
        </div>
      </div>
      <div className="chat-body">
        {!hist.length && <div className="chat-msg bot"><div>{t("as_hello")}</div><div className="row" style={{ marginTop: 8, flexWrap: "wrap" }}>{["as_ex1", "as_ex2", "as_ex3"].map((k) => <Btn key={k} sm onClick={() => send(t(k))}>{t(k)}</Btn>)}</div></div>}
        {hist.map((m, i) => <div key={i} className={`chat-msg ${m.role === "user" ? "me" : "bot"}`}>
          <div style={{ whiteSpace: "pre-wrap" }}>{m.content}</div>
          {m.role === "assistant" && <div className="row between" style={{ marginTop: 6 }}><span className="dim small">{m.meta?.used_llm ? t("as_via_llm", { m: m.meta.model }) : t("as_via_ctx")}{m.meta?.sources?.length ? ` · 📎 ${m.meta.sources.length}` : ""}</span>
            {!m.rated ? <span className="row" style={{ gap: 2 }}><Btn sm onClick={() => rate(i, "up")}>👍</Btn><Btn sm onClick={() => rate(i, "down")}>👎</Btn></span> : <span className="dim small">{m.rated === "up" ? "👍" : "👎"}</span>}</div>}
        </div>)}
        {ask.isPending && <div className="chat-msg bot dim">{t("as_thinking")}</div>}
        <Err e={ask.error} /><div ref={endRef} />
      </div>
      <form className="chat-input" onSubmit={(e) => { e.preventDefault(); send(q); }}><input ref={inputRef} className="input grow" placeholder={t("as_input")} value={q} onChange={(e) => setQ(e.target.value)} /><Btn kind="primary" sm type="submit" disabled={ask.isPending}>➤</Btn></form>
    </div>}
  </>;
}
