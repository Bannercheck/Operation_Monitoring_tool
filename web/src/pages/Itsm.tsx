import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { get, put } from "../api";
import { useT } from "../i18n";
import { Badge, Btn, Card, Empty, Err, Field, Table } from "../components/ui";

export default function Itsm() {
  const { t } = useT(); const qc = useQueryClient();
  const cfg = useQuery({ queryKey: ["itsm-cfg"], queryFn: () => get("/itsm/config") });
  const ds = useQuery({ queryKey: ["datasets"], queryFn: () => get("/datasets") });
  const [f, setF] = useState<any>(null); const [sel, setSel] = useState<any>(null); const [refresh, setRefresh] = useState(0);
  useEffect(() => { if (cfg.data && !f) setF(cfg.data); }, [cfg.data, f]);
  const dataset = ds.data?.[0]?.id ?? "";
  const tk = useQuery({ queryKey: ["itsm-tickets", dataset, refresh], queryFn: () => get(`/itsm/tickets?dataset=${dataset}${refresh ? "&refresh=true" : ""}`), refetchInterval: 60000 });
  const save = useMutation({ mutationFn: () => put("/itsm/config", { system: f.system, base: f.base, user: f.user, password: f.password, token: f.token, query: f.query, path: f.path, mapping: f.mapping }), onSuccess: () => { qc.invalidateQueries({ queryKey: ["itsm-cfg"] }); setRefresh((r) => r + 1); } });
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  const rows = tk.data?.tickets ?? [];
  return <div className="stack">
    <div><h2>🎫 {t("itsm_title")}</h2><span className="muted small">{t("itsm_sub")}</span></div>
    <div className="grid k2" style={{ gridTemplateColumns: "1fr 2fr" }}>
      <Card title={t("itsm_system")}>{f && <div className="stack">
        <select className="input" value={f.system} onChange={set("system")}>{(cfg.data?.systems ?? []).map((s: string) => <option key={s} value={s}>{s === "demo" ? t("itsm_demo") : s}</option>)}</select>
        {f.system !== "demo" && <><Field label={t("itsm_base")}><input className="input" value={f.base} onChange={set("base")} /></Field><div className="grid k2"><Field label={t("src_user")}><input className="input" value={f.user} onChange={set("user")} /></Field><Field label={t("smtp_password")}><input className="input" type="password" value={f.password} onChange={set("password")} /></Field></div>
          <Field label="Token"><input className="input" type="password" value={f.token} onChange={set("token")} /></Field><Field label={t("itsm_query")}><input className="input" value={f.query} onChange={set("query")} /></Field>
          {f.system === "generic" && <><Field label={t("itsm_path")}><input className="input" value={f.path} onChange={set("path")} /></Field><Field label={t("itsm_mapping")}><textarea className="input" value={f.mapping} onChange={set("mapping")} placeholder='{"title": "subject", "created": "opened_at"}' /></Field></>}</>}
        <div className="row"><Btn kind="primary" onClick={() => save.mutate()}>{t("save")}</Btn><Btn onClick={() => setRefresh((r) => r + 1)}>{t("itsm_fetch")}</Btn></div><Err e={save.error || tk.error} /></div>}</Card>
      <Card title={`${t("itsm_title")} · ${rows.length}`}>{!rows.length ? <Empty>{t("itsm_none")}</Empty> : <Table cols={[
        { k: "id", label: "ID", render: (r) => <a href="#" onClick={(e) => { e.preventDefault(); setSel(r); }}><b>{r.id}</b></a> }, { k: "priority", label: t("act_priority"), render: (r) => <Badge v={r.priority} /> }, { k: "status", label: t("status") }, { k: "title", label: t("act_title_f") }, { k: "service", label: t("service") },
        { k: "created", label: t("itsm_opened"), render: (r) => r.created?.slice(5, 16).replace("T", " ") }, { k: "relevance", label: t("itsm_rel"), num: true, render: (r) => <span><span className="progress" style={{ width: 70, display: "inline-block", verticalAlign: "middle", marginRight: 6 }}><span style={{ display: "block", height: "100%", width: `${Math.round(r.relevance * 100)}%`, background: "linear-gradient(90deg,#2dd4bf,#60a5fa)" }} /></span>{r.relevance.toFixed(2)}</span> },
        { k: "related", label: t("itsm_related"), render: (r) => <span className="small">{(r.related ?? []).join(", ")}</span> }]} rows={rows} />}</Card>
    </div>
    {sel && <Card title={`${sel.id} · ${sel.title}`} right={<Btn sm kind="ghost" onClick={() => setSel(null)}>✕</Btn>}><div className="stack small"><div><Badge v={sel.priority} /> {sel.status} · {sel.service} · {sel.assignee}</div><div className="muted">{sel.description}</div>
      <div><b>{t("itsm_rel")}:</b> {sel.relevance} · <b>{t("itsm_related")}:</b> {(sel.related ?? []).join(", ") || "-"}</div><div><b>{t("itsm_why")}:</b><ul style={{ margin: "2px 0 0", paddingLeft: 18 }}>{(sel.reasons ?? []).map((r: string, i: number) => <li key={i}>{r}</li>)}</ul></div>{sel.url && <a href={sel.url} target="_blank" rel="noreferrer">{sel.url}</a>}</div></Card>}
  </div>;
}
