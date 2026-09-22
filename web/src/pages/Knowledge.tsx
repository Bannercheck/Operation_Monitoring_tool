import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, del, get, post } from "../api";
import { useT } from "../i18n";
import { Btn, Card, Confirm, Empty, Err, Field, Kpi, Table, Tabs, fmtTs } from "../components/ui";

const KINDS = ["", "pattern", "anomaly", "resolution", "feedback", "note", "doc", "chat"];

export default function Knowledge() {
  const { t } = useT(); const qc = useQueryClient(); const [tab, setTab] = useState<"search" | "rules">("search"); const [q, setQ] = useState(""); const [kind, setKind] = useState(""); const [note, setNote] = useState({ title: "", text: "" }); const [rule, setRule] = useState({ kind: "owner", key: "", value: "", reason: "" });
  const stats = useQuery({ queryKey: ["kb-stats"], queryFn: () => get("/memory/stats"), refetchInterval: 10000 });
  const res = useQuery({ queryKey: ["kb-search", q, kind], queryFn: () => get(`/memory/search?q=${encodeURIComponent(q)}&k=${q ? 12 : 40}&kinds=${kind}`) });
  const tl = useQuery({ queryKey: ["mem-timeline"], queryFn: () => get("/memory/timeline?days=10") });
  const learn = useMutation({ mutationFn: () => post("/memory/learn"), onSuccess: () => inv() });
  const reindex = useMutation({ mutationFn: () => post("/memory/reindex"), onSuccess: () => inv() });
  const rules = useQuery({ queryKey: ["kb-rules"], queryFn: () => get("/knowledge/rules"), enabled: tab === "rules" });
  const inv = () => { qc.invalidateQueries({ queryKey: ["kb-search"] }); qc.invalidateQueries({ queryKey: ["kb-rules"] }); qc.invalidateQueries({ queryKey: ["kb-stats"] }); };
  const addNote = useMutation({ mutationFn: () => post("/knowledge/notes", note), onSuccess: () => { setNote({ title: "", text: "" }); inv(); } });
  const rmLesson = useMutation({ mutationFn: (id: number) => del(`/knowledge/lessons/${id}`), onSuccess: inv });
  const propose = useMutation({ mutationFn: () => post("/knowledge/rules", rule), onSuccess: () => { setRule({ ...rule, key: "", value: "", reason: "" }); inv(); } });
  const decide = useMutation({ mutationFn: (p: { id: number; approve: boolean }) => post(`/knowledge/rules/${p.id}/decide`, { approve: p.approve }), onSuccess: inv });
  const rmRule = useMutation({ mutationFn: (id: number) => del(`/knowledge/rules/${id}`), onSuccess: inv });
  const proposeLlm = useMutation({ mutationFn: () => post("/assist/propose-rules"), onSuccess: inv });
  const upload = async (files: FileList | null) => { if (!files?.length) return; const fd = new FormData(); fd.append("file", files[0]); await api("/knowledge/docs/upload", { method: "POST", form: fd }); inv(); };
  const st = stats.data;
  return <div className="stack">
    <div className="row between"><div><h2>🧠 {t("kb_title")}</h2><span className="muted small">{t("mem_sub")}</span></div>
      <div className="row"><Btn sm onClick={() => learn.mutate()} disabled={learn.isPending}>⚡ {t("mem_learn_now")}</Btn><Btn sm onClick={() => reindex.mutate()} disabled={reindex.isPending || st?.reindex?.running}>{st?.reindex?.running ? `⏳ ${st.reindex.done} / ${st.reindex.done + st.reindex.remaining}` : `🔁 ${t("mem_reindex")}`}</Btn></div></div>
    {st && <div className="grid k4">
      <Kpi value={st.total ?? 0} label={t("mem_total")} sub={Object.entries(st.lessons ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "-"} />
      <Kpi value={`${st.indexed_pct ?? 100}%`} label={t("mem_indexed")} sub={st.embedder ? st.index_model : t("mem_hash")} accent={st.embedder ? "#2dd4bf" : "#f59e0b"} />
      <Kpi value={st.last_learn ? fmtTs(st.last_learn.ts) : "-"} label={t("mem_last_learn")} sub={st.last_learn ? `${st.last_learn.events} ${t("ds_events")} · ${st.last_learn.incidents} incident · +${st.last_learn.lessons}` : `${t("mem_every")} ${st.learn_min} ${t("mem_min")}`} accent="#60a5fa" />
      <Kpi value={st.rules?.approved ?? 0} label={t("mem_rules_ok")} sub={`${st.rules?.proposed ?? 0} ${t("mem_rules_pending")}`} accent="#a78bfa" />
    </div>}
    {learn.data && <div className="muted small">⚡ {learn.data.events} {t("ds_events")} · {learn.data.incidents} incident · +{learn.data.lessons} {t("mem_lessons")} {learn.data.note && `· ${learn.data.note}`}</div>}
    {st?.reindex?.error && <div className="err">{st.reindex.error}</div>}
    <Tabs value={tab} onChange={setTab} tabs={[{ k: "search", label: t("search") }, { k: "rules", label: `${t("kb_rules")} · ${st?.rules?.proposed ?? 0}` }]} />
    {tab === "search" && <div className="grid k2" style={{ gridTemplateColumns: "2fr 1fr" }}>
      <div className="stack"><div className="row"><input className="input grow" placeholder={t("kb_search")} value={q} onChange={(e) => setQ(e.target.value)} />
          <select className="input" style={{ width: 150 }} value={kind} onChange={(e) => setKind(e.target.value)}>{KINDS.map((k) => <option key={k} value={k}>{k ? t(`mem_k_${k}`) : t("mem_all_kinds")}</option>)}</select></div>
        {!res.data?.length ? <Empty /> : res.data.map((l: any) => <Card key={l.id} className="tight"><div className="svc"><div className="h"><span className={`badge ${l.kind === "anomaly" ? "amber" : l.kind === "resolution" ? "green" : l.kind === "pattern" ? "blue" : ""}`}>{t(`mem_k_${l.kind}`) || l.kind}</span> {l.title}{l.score != null && <span className="dim small">· {Number(l.score).toFixed(2)}</span>}<div className="grow" /><Confirm onConfirm={() => rmLesson.mutate(l.id)}>{t("delete")}</Confirm></div>
          <div className="v" style={{ whiteSpace: "pre-wrap" }}>{String(l.text).slice(0, 600)}</div><div className="dim small">{l.dataset ? `${l.dataset} · ${l.incident_id} · ` : ""}×{l.occurrences} · {fmtTs(l.updated_at)}</div></div></Card>)}</div>
      <div className="stack"><Card title={t("mem_timeline")}>{!tl.data?.length ? <Empty /> : <Table cols={[{ k: "day", label: t("mem_day") }, { k: "total", label: t("mem_total"), num: true }, { k: "pattern", label: t("mem_k_pattern"), num: true, render: (r) => r.pattern ?? 0 }, { k: "anomaly", label: t("mem_k_anomaly"), num: true, render: (r) => r.anomaly ?? 0 }, { k: "resolution", label: t("mem_k_resolution"), num: true, render: (r) => r.resolution ?? 0 }]} rows={(tl.data ?? []).map((r: any) => ({ id: r.day, ...r }))} />}</Card>
      <Card title={t("kb_note_add")}><div className="stack"><Field label={t("kb_title_f")}><input className="input" value={note.title} onChange={(e) => setNote({ ...note, title: e.target.value })} /></Field><Field label={t("kb_text")}><textarea className="input" value={note.text} onChange={(e) => setNote({ ...note, text: e.target.value })} /></Field>
        <Btn kind="primary" onClick={() => addNote.mutate()} disabled={!note.title || !note.text}>{t("add")}</Btn><Err e={addNote.error} />
        <label className="btn">📄 {t("kb_doc_upload")}<input type="file" accept=".txt,.md,.markdown,.log" style={{ display: "none" }} onChange={(e) => upload(e.target.files)} /></label></div></Card></div></div>}
    {tab === "rules" && <div className="stack">
      <Card title={t("kb_propose")}><div className="row"><select className="input" style={{ width: 170 }} value={rule.kind} onChange={(e) => setRule({ ...rule, kind: e.target.value })}>{["owner", "cause_rank", "noise_type", "noise_template", "dependency", "recommendation"].map((k) => <option key={k}>{k}</option>)}</select>
        <input className="input" style={{ width: 200 }} placeholder={t("kb_key")} value={rule.key} onChange={(e) => setRule({ ...rule, key: e.target.value })} /><input className="input" style={{ width: 220 }} placeholder={t("kb_value")} value={rule.value} onChange={(e) => setRule({ ...rule, value: e.target.value })} /><input className="input grow" placeholder={t("note")} value={rule.reason} onChange={(e) => setRule({ ...rule, reason: e.target.value })} />
        <Btn kind="primary" onClick={() => propose.mutate()} disabled={!rule.key || !rule.value}>{t("kb_propose")}</Btn><Btn onClick={() => proposeLlm.mutate()} disabled={proposeLlm.isPending}>✨ {t("kb_propose_llm")}</Btn></div><Err e={propose.error || decide.error || proposeLlm.error} /></Card>
      <Card><Table cols={[{ k: "status", label: t("status"), render: (r) => <span className={`badge ${r.status === "approved" ? "green" : r.status === "rejected" ? "red" : "amber"}`}>{r.status}</span> }, { k: "kind", label: t("kb_kind") }, { k: "key", label: t("kb_key"), render: (r) => <span className="mono">{r.key}</span> }, { k: "value", label: t("kb_value") }, { k: "reason", label: t("note") }, { k: "source", label: "src" },
        { k: "x", label: "", render: (r) => <div className="row">{r.status === "proposed" && <><Btn sm kind="primary" onClick={() => decide.mutate({ id: r.id, approve: true })}>{t("kb_approve")}</Btn><Btn sm onClick={() => decide.mutate({ id: r.id, approve: false })}>{t("kb_reject")}</Btn></>}<Confirm onConfirm={() => rmRule.mutate(r.id)}>{t("delete")}</Confirm></div> }]} rows={rules.data ?? []} /></Card></div>}
  </div>;
}
