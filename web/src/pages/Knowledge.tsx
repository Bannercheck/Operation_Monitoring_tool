import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, del, get, post } from "../api";
import { useT } from "../i18n";
import { Btn, Card, Confirm, Empty, Err, Field, Table, Tabs } from "../components/ui";

export default function Knowledge() {
  const { t } = useT(); const qc = useQueryClient(); const [tab, setTab] = useState<"search" | "rules">("search"); const [q, setQ] = useState(""); const [note, setNote] = useState({ title: "", text: "" }); const [rule, setRule] = useState({ kind: "owner", key: "", value: "", reason: "" });
  const stats = useQuery({ queryKey: ["kb-stats"], queryFn: () => get("/knowledge/stats") });
  const res = useQuery({ queryKey: ["kb-search", q], queryFn: () => (q ? get(`/knowledge/search?q=${encodeURIComponent(q)}&k=10`) : get("/knowledge/lessons?limit=40")) });
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
    <div className="row between"><h2>🧠 {t("kb_title")}</h2><span className="muted small">{st ? Object.entries(st.lessons ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ") : ""}</span></div>
    <Tabs value={tab} onChange={setTab} tabs={[{ k: "search", label: t("search") }, { k: "rules", label: `${t("kb_rules")} · ${st?.rules?.proposed ?? 0}` }]} />
    {tab === "search" && <div className="grid k2" style={{ gridTemplateColumns: "2fr 1fr" }}>
      <div className="stack"><input className="input" placeholder={t("kb_search")} value={q} onChange={(e) => setQ(e.target.value)} />
        {!res.data?.length ? <Empty /> : res.data.map((l: any) => <Card key={l.id} className="tight"><div className="svc"><div className="h"><span className="chip">{l.kind}</span> {l.title}{l.score != null && <span className="dim small">· {Number(l.score).toFixed(2)}</span>}<div className="grow" /><Confirm onConfirm={() => rmLesson.mutate(l.id)}>{t("delete")}</Confirm></div>
          <div className="v" style={{ whiteSpace: "pre-wrap" }}>{String(l.text).slice(0, 600)}</div>{l.dataset && <div className="dim small">{l.dataset} · {l.incident_id} · ×{l.occurrences}</div>}</div></Card>)}</div>
      <Card title={t("kb_note_add")}><div className="stack"><Field label={t("kb_title_f")}><input className="input" value={note.title} onChange={(e) => setNote({ ...note, title: e.target.value })} /></Field><Field label={t("kb_text")}><textarea className="input" value={note.text} onChange={(e) => setNote({ ...note, text: e.target.value })} /></Field>
        <Btn kind="primary" onClick={() => addNote.mutate()} disabled={!note.title || !note.text}>{t("add")}</Btn><Err e={addNote.error} />
        <label className="btn">📄 {t("kb_doc_upload")}<input type="file" accept=".txt,.md,.markdown,.log" style={{ display: "none" }} onChange={(e) => upload(e.target.files)} /></label></div></Card></div>}
    {tab === "rules" && <div className="stack">
      <Card title={t("kb_propose")}><div className="row"><select className="input" style={{ width: 170 }} value={rule.kind} onChange={(e) => setRule({ ...rule, kind: e.target.value })}>{["owner", "cause_rank", "noise_type", "noise_template", "dependency", "recommendation"].map((k) => <option key={k}>{k}</option>)}</select>
        <input className="input" style={{ width: 200 }} placeholder={t("kb_key")} value={rule.key} onChange={(e) => setRule({ ...rule, key: e.target.value })} /><input className="input" style={{ width: 220 }} placeholder={t("kb_value")} value={rule.value} onChange={(e) => setRule({ ...rule, value: e.target.value })} /><input className="input grow" placeholder={t("note")} value={rule.reason} onChange={(e) => setRule({ ...rule, reason: e.target.value })} />
        <Btn kind="primary" onClick={() => propose.mutate()} disabled={!rule.key || !rule.value}>{t("kb_propose")}</Btn><Btn onClick={() => proposeLlm.mutate()} disabled={proposeLlm.isPending}>✨ {t("kb_propose_llm")}</Btn></div><Err e={propose.error || decide.error || proposeLlm.error} /></Card>
      <Card><Table cols={[{ k: "status", label: t("status"), render: (r) => <span className={`badge ${r.status === "approved" ? "green" : r.status === "rejected" ? "red" : "amber"}`}>{r.status}</span> }, { k: "kind", label: t("kb_kind") }, { k: "key", label: t("kb_key"), render: (r) => <span className="mono">{r.key}</span> }, { k: "value", label: t("kb_value") }, { k: "reason", label: t("note") }, { k: "source", label: "src" },
        { k: "x", label: "", render: (r) => <div className="row">{r.status === "proposed" && <><Btn sm kind="primary" onClick={() => decide.mutate({ id: r.id, approve: true })}>{t("kb_approve")}</Btn><Btn sm onClick={() => decide.mutate({ id: r.id, approve: false })}>{t("kb_reject")}</Btn></>}<Confirm onConfirm={() => rmRule.mutate(r.id)}>{t("delete")}</Confirm></div> }]} rows={rules.data ?? []} /></Card></div>}
  </div>;
}
