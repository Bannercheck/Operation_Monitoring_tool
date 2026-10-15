import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { del, get, patch, post } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Badge, Btn, Card, Chip, Confirm, Empty, Err, Field, Modal, Table, fmtTs } from "../components/ui";
import { TeamChip, TeamPick } from "../components/TeamPick";

/** Teams a key may be bound to: every team for an installation admin, else the teams the user manages. */
function useKeyTeams(): { id: number; name: string }[] {
  const { user, can } = useAuth();
  const admin = !!user && user.all !== false && can("page.teams");
  const all = useQuery({ queryKey: ["teams"], queryFn: () => get("/teams"), enabled: admin, staleTime: 60000 });
  if (admin) return ((all.data as any)?.teams ?? []).map((x: any) => ({ id: x.id, name: x.name }));
  return (user?.teams ?? []).filter((m: any) => m.manage).map((m: any) => ({ id: m.id, name: m.name }));
}

/** Ayarlar › Bağlantılar › MCP: named keys (account or team scope, read-only unless "yazma"), the tool catalogue, the audit. */
export function McpKeysPanel() {
  const { t, lang } = useT(); const qc = useQueryClient(); const teams = useKeyTeams();
  const [f, setF] = useState<any>({ name: "", kind: "user", team_id: null, write: false, note: "" }); const [tok, setTok] = useState<any>(null);
  const keys = useQuery({ queryKey: ["mcp-keys"], queryFn: () => get("/mcp/keys") });
  const cat = useQuery({ queryKey: ["mcp-tools"], queryFn: () => get("/mcp/tools"), staleTime: 300000 });
  const audit = useQuery({ queryKey: ["mcp-audit"], queryFn: () => get("/mcp/audit?n=100"), refetchInterval: 15000 });
  const inv = () => { qc.invalidateQueries({ queryKey: ["mcp-keys"] }); qc.invalidateQueries({ queryKey: ["mcp-audit"] }); };
  const create = useMutation({ mutationFn: () => post("/mcp/keys", { ...f, team_id: f.kind === "team" ? f.team_id : null }), onSuccess: (r: any) => { setTok(r); setF({ ...f, name: "", note: "" }); inv(); } });
  const revoke = useMutation({ mutationFn: (id: number) => del(`/mcp/keys/${id}`), onSuccess: inv });
  const url = `${window.location.origin}/api/mcp`;
  const tools: any[] = (cat.data as any)?.tools ?? [];
  return <div className="stack">
    <div className="muted small">{t("mcp_keys_intro")}</div>
    <Card title={`🔑 ${t("mcp_key_new")}`}><div className="row" style={{ flexWrap: "wrap" }}>
      <input className="input" placeholder={t("mcp_key_name")} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} style={{ width: 200 }} />
      <select className="input" value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })} style={{ width: 170 }}>
        <option value="user">{t("mcp_kind_user")}</option>{teams.length > 0 && <option value="team">{t("mcp_kind_team")}</option>}</select>
      {f.kind === "team" && <select className="input" value={f.team_id ?? ""} onChange={(e) => setF({ ...f, team_id: e.target.value ? Number(e.target.value) : null })} style={{ width: 170 }}>
        <option value="">{t("tmr_team")}…</option>{teams.map((x) => <option key={x.id} value={x.id}>👥 {x.name}</option>)}</select>}
      <label className="check"><input type="checkbox" checked={f.write} onChange={(e) => setF({ ...f, write: e.target.checked })} /> {t("mcp_write")}</label>
      <Btn kind="primary" disabled={!f.name || (f.kind === "team" && !f.team_id) || create.isPending} onClick={() => create.mutate()}>{t("add")}</Btn></div>
      <div className="dim small" style={{ marginTop: 6 }}>{f.write ? t("mcp_write_warn") : t("mcp_read_note")}</div><Err e={create.error} /></Card>
    <Card title={t("mcp_keys")}>{!(keys.data as any[])?.length ? <Empty>{t("mcp_keys_none")}</Empty> : <Table cols={[
      { k: "name", label: t("name"), render: (r) => <div><b>{r.name}</b> <span className="mono dim small">{r.prefix}…</span></div> },
      { k: "kind", label: t("mcp_bound"), render: (r) => r.kind === "team" ? <TeamChip name={r.team} /> : <span className="small">👤 {r.email}</span> },
      { k: "write", label: t("mcp_access"), render: (r) => <Badge v={r.write ? "pending" : "resolved"} label={r.write ? t("mcp_rw") : t("mcp_ro")} /> },
      { k: "last_used", label: t("mcp_last_used"), render: (r) => <span className="muted small">{fmtTs(r.last_used)} · {r.calls ?? 0} {t("mcp_calls")}</span> },
      { k: "created_at", label: t("mcp_created"), render: (r) => <span className="muted small">{fmtTs(r.created_at)} · {r.created_by}</span> },
      { k: "x", label: "", render: (r) => r.active ? <Confirm onConfirm={() => revoke.mutate(r.id)}>{t("mcp_revoke")}</Confirm> : <span className="dim small">{t("mcp_revoked")} {fmtTs(r.revoked_at)}</span> }]}
      rows={(keys.data as any[]) ?? []} />}<Err e={revoke.error} /></Card>
    <details className="card"><summary style={{ cursor: "pointer", fontWeight: 600 }}>🧰 {t("mcp_tools")} ({tools.length})</summary>
      <Table cols={[{ k: "name", label: t("name"), render: (r) => <span className="mono">{r.name}</span> }, { k: "tr", label: t("mcp_what"), render: (r) => lang === "tr" ? r.tr : r.en },
        { k: "perm", label: t("mcp_perm"), render: (r) => <span className="mono small">{r.perm || "-"}</span> },
        { k: "write", label: t("mcp_access"), render: (r) => <Badge v={r.write ? "pending" : "resolved"} label={r.write ? t("mcp_rw") : t("mcp_ro")} /> }]} rows={tools.map((x) => ({ id: x.name, ...x }))} />
      <pre className="code" style={{ marginTop: 8 }}>{`{"mcpServers": {"watchover": {"url": "${url}", "headers": {"Authorization": "Bearer wom_…"}}}}`}</pre></details>
    <Card title={`📜 ${t("mcp_audit")}`}><Table cols={[
      { k: "ts", label: t("mcp_when"), render: (r) => <span className="muted small">{fmtTs(r.ts)}</span> },
      { k: "direction", label: "", render: (r) => <span title={r.direction}>{r.direction === "out" ? "⬆" : "⬇"}</span> },
      { k: "principal", label: t("mcp_who"), render: (r) => <span className="small">{r.key_name || r.principal}<div className="dim">{r.scope}</div></span> },
      { k: "tool", label: t("mcp_tool"), render: (r) => <span className="mono small">{r.tool}<div className="dim">{r.args}</div></span> },
      { k: "ok", label: t("status"), render: (r) => r.ok ? <Badge v="resolved" label={`${r.items} · ${r.ms} ms`} /> : <Badge v="critical" label={r.error || "error"} /> }]}
      rows={(audit.data as any[]) ?? []} empty={t("mcp_audit_none")} /></Card>
    {tok && <Modal title={t("mcp_key_once")} onClose={() => setTok(null)}><p className="muted">{t("mcp_key_once_help")}</p><pre className="code">{tok.token}</pre>
      <pre className="code">{JSON.stringify({ mcpServers: { watchover: { url, headers: { Authorization: `Bearer ${tok.token}` } } } }, null, 1)}</pre></Modal>}
  </div>;
}

const SRC0 = { name: "", url: "", transport: "http", auth: "bearer", header_name: "", secret: "", headers: "", timeout_s: 15, max_kb: 1024, verify_tls: true, assist_tools: "", team_id: null as number | null };
const PULL0 = { name: "", tool: "", arguments: "{}", target: "logs", source_name: "", env: "prod", mapping: "{}", interval_s: 300, max_rows: 1000 };

/** Ayarlar › Veri kaynakları › MCP kaynakları: external MCP servers, their tools, and pulls ingested as logs, inventory or tickets. */
export function McpSourcesPanel() {
  const { t } = useT(); const { can } = useAuth(); const qc = useQueryClient(); const editable = can("act.sources");
  const [add, setAdd] = useState(false); const [f, setF] = useState<any>(SRC0); const [msg, setMsg] = useState<any>(null);
  const list = useQuery({ queryKey: ["mcp-sources"], queryFn: () => get("/mcp/sources"), refetchInterval: 10000 });
  const inv = () => qc.invalidateQueries({ queryKey: ["mcp-sources"] });
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const save = useMutation({ mutationFn: () => post("/mcp/sources", { ...f, timeout_s: Number(f.timeout_s), max_kb: Number(f.max_kb) }), onSuccess: () => { setAdd(false); setF(SRC0); inv(); } });
  const op = useMutation({ mutationFn: (p: { path: string; m: string; body?: any }) => (p.m === "del" ? del(p.path) : p.m === "patch" ? patch(p.path, p.body) : post(p.path, p.body)),
    onSuccess: (r: any) => { if (r && (r.ok !== undefined)) setMsg(r); inv(); } });
  const rows: any[] = (list.data as any)?.sources ?? [];
  return <div className="stack">
    <div className="muted small">{t("mcp_src_intro")}</div>
    {editable && !add && <div className="row"><Btn kind="primary" onClick={() => setAdd(true)}>➕ {t("mcp_src_add")}</Btn></div>}
    {add && <Card title={`➕ ${t("mcp_src_add")}`} right={<Btn sm onClick={() => setAdd(false)}>✕</Btn>}><div className="stack">
      <div className="grid k3"><Field label={t("name")}><input className="input" value={f.name} onChange={set("name")} placeholder="confluence-mcp" /></Field>
        <Field label={t("src_url")} hint={t("mcp_src_url_help")}><input className="input" value={f.url} onChange={set("url")} placeholder="https://mcp.example.com/mcp" /></Field>
        <Field label={t("mcp_transport")}><select className="input" value={f.transport} onChange={set("transport")}><option value="http">Streamable HTTP</option><option value="sse">HTTP + SSE</option></select></Field></div>
      <div className="grid k4"><Field label={t("src_auth")}><select className="input" value={f.auth} onChange={set("auth")}>
          <option value="none">{t("src_auth_none")}</option><option value="bearer">Bearer token</option><option value="header">{t("mcp_auth_header")}</option></select></Field>
        {f.auth === "header" && <Field label={t("mcp_header_name")}><input className="input" value={f.header_name} onChange={set("header_name")} placeholder="X-API-Key" /></Field>}
        <Field label={t("src_secret")} hint={t("src_secret_help")}><input className="input" type="password" value={f.secret} onChange={set("secret")} disabled={f.auth === "none"} /></Field>
        <Field label={t("tmr_team")}><TeamPick perm="act.sources" value={f.team_id} onChange={(v) => setF({ ...f, team_id: v })} /></Field></div>
      <details><summary className="muted small" style={{ cursor: "pointer" }}>{t("src_advanced")}</summary><div className="grid k4" style={{ marginTop: 8 }}>
        <Field label={t("mcp_timeout")}><input className="input" type="number" min={1} max={120} value={f.timeout_s} onChange={set("timeout_s")} /></Field>
        <Field label={t("mcp_max_kb")}><input className="input" type="number" min={16} max={16384} value={f.max_kb} onChange={set("max_kb")} /></Field>
        <Field label={t("src_headers")}><textarea className="input mono" style={{ minHeight: 56 }} value={f.headers} onChange={set("headers")} placeholder="X-Tenant: ops" /></Field>
        <label className="check" style={{ marginTop: 22 }}><input type="checkbox" checked={f.verify_tls} onChange={set("verify_tls")} /> {t("src_verify_tls")}</label></div></details>
      <Field label={t("mcp_assist_tools")} hint={t("mcp_assist_help")}><input className="input mono" value={f.assist_tools} onChange={set("assist_tools")} placeholder="search_docs, get_runbook" /></Field>
      <Err e={save.error} /><div className="row"><Btn kind="primary" disabled={!f.name || !f.url} onClick={() => save.mutate()}>💾 {t("src_save")}</Btn></div></div></Card>}
    {msg && <div className={msg.ok === false ? "err" : "ok"}>{msg.ok === false ? msg.message : msg.rows !== undefined ? `${msg.rows} ${t("mcp_rows")} · ${msg.skipped ?? 0} ${t("mcp_skipped")} · ${msg.ms} ms` : `${(msg.tools ?? []).length} ${t("mcp_tools_n")}`}</div>}
    <Err e={op.error} />
    {!rows.length ? <Card><Empty>{t("mcp_src_none")}</Empty></Card> : rows.map((s) => <McpSourceCard key={s.id} s={s} editable={editable} op={(path, m, body) => op.mutate({ path, m, body })} />)}
  </div>;
}

function McpSourceCard({ s, editable, op }: { s: any; editable: boolean; op: (path: string, m: string, body?: any) => void }) {
  const { t } = useT(); const [p, setP] = useState<any>(null); const [err, setErr] = useState<string>("");
  const tools: any[] = s.tools?.tools ?? []; const resources: any[] = s.tools?.resources ?? [];
  const dot = !s.enabled ? "#64748b" : s.last_err ? "#f87171" : s.last_ok ? "#2dd4bf" : "#fbbf24";
  const savePull = () => {
    let args: any, mapping: any;
    try { args = JSON.parse(p.arguments || "{}"); mapping = JSON.parse(p.mapping || "{}"); } catch { setErr(t("mcp_json_bad")); return; }
    setErr(""); op(`/mcp/sources/${s.id}/pulls`, "post", { ...p, arguments: args, mapping, interval_s: Number(p.interval_s), max_rows: Number(p.max_rows) }); setP(null);
  };
  return <Card title={<span><span className="mcp-dot" style={{ background: dot }} /><b>{s.name}</b> <span className="mono dim small">{s.transport} · {s.url.slice(0, 60)}</span> <TeamChip name={s.team} /></span>}
    right={editable ? <div className="row"><Btn sm onClick={() => op(`/mcp/sources/${s.id}/discover`, "post")}>🔎 {t("mcp_discover")}</Btn>
      <Btn sm onClick={() => op(`/mcp/sources/${s.id}`, "patch", { enabled: !s.enabled })}>{s.enabled ? "⏸" : "▶"}</Btn>
      <Confirm onConfirm={() => op(`/mcp/sources/${s.id}`, "del")}>{t("delete")}</Confirm></div> : null}>
    <div className="stack">
      {s.last_err && <div className="small" style={{ color: "var(--red)" }}>⚠ {s.last_err.slice(0, 200)}</div>}
      <div className="chips mcp-tools">{tools.length ? tools.map((x) => <Chip key={x.name} color={x.annotations?.readOnlyHint ? "#2dd4bf" : x.annotations?.destructiveHint ? "#f87171" : "#60a5fa"}>
        <span className="mono" title={x.description}>{x.name}</span></Chip>) : <span className="dim small">{t("mcp_not_discovered")}</span>}
        {resources.length > 0 && <Chip color="#a78bfa">{resources.length} {t("mcp_resources")}</Chip>}
        {s.assist_tools && <Chip color="#fbbf24">🤖 {s.assist_tools}</Chip>}</div>
      <Table cols={[
        { k: "name", label: t("mcp_pull"), render: (r) => <div><b>{r.name}</b> <span className="mono small dim">{r.tool}({Object.keys(r.arguments ?? {}).join(", ")})</span></div> },
        { k: "target", label: t("mcp_target"), render: (r) => <span className="small">{t("mcp_target_" + r.target)}{r.target === "logs" ? ` · ${r.source_name}` : ""}</span> },
        { k: "interval_s", label: t("src_interval"), render: (r) => <span className="muted small">{r.interval_s}s · ≤{r.max_rows}</span> },
        { k: "status", label: t("status"), render: (r) => <div><Badge v={r.status === "ok" ? "resolved" : r.status === "error" ? "critical" : "medium"} label={r.status} />
          <span className="muted small"> {fmtTs(r.last_run)} · {r.last_rows} / {r.total_rows} {t("mcp_rows")}{r.last_skipped ? ` · ${r.last_skipped} ${t("mcp_skipped")}` : ""} · {r.last_ms} ms</span>
          {r.last_err && <div className="small" style={{ color: "var(--red)" }}>{r.last_err.slice(0, 160)}</div>}</div> },
        { k: "x", label: "", render: (r) => editable ? <div className="row"><Btn sm onClick={() => op(`/mcp/pulls/${r.id}/run`, "post")}>▶ {t("mcp_run")}</Btn>
          <Btn sm onClick={() => op(`/mcp/pulls/${r.id}`, "patch", { enabled: !r.enabled })}>{r.enabled ? "⏸" : "▶"}</Btn><Confirm onConfirm={() => op(`/mcp/pulls/${r.id}`, "del")}>{t("delete")}</Confirm></div> : null }]}
        rows={s.pulls ?? []} empty={t("mcp_pulls_none")} />
      {editable && !p && <div className="row"><Btn sm onClick={() => setP({ ...PULL0, tool: tools[0]?.name ?? "" })}>➕ {t("mcp_pull_add")}</Btn></div>}
      {p && <div className="stack mcp-pull-form">
        <div className="grid k4"><Field label={t("name")}><input className="input" value={p.name} onChange={(e) => setP({ ...p, name: e.target.value })} /></Field>
          <Field label={t("mcp_tool")}>{tools.length ? <select className="input" value={p.tool} onChange={(e) => setP({ ...p, tool: e.target.value })}>{tools.map((x) => <option key={x.name}>{x.name}</option>)}</select>
            : <input className="input mono" value={p.tool} onChange={(e) => setP({ ...p, tool: e.target.value })} />}</Field>
          <Field label={t("mcp_target")}><select className="input" value={p.target} onChange={(e) => setP({ ...p, target: e.target.value })}>{["logs", "inventory", "itsm"].map((x) => <option key={x} value={x}>{t("mcp_target_" + x)}</option>)}</select></Field>
          <Field label={t("src_interval")}><input className="input" type="number" min={30} value={p.interval_s} onChange={(e) => setP({ ...p, interval_s: e.target.value })} /></Field></div>
        <div className="grid k2"><Field label={t("mcp_args")} hint={t("mcp_args_help")}><textarea className="input mono" style={{ minHeight: 56 }} value={p.arguments} onChange={(e) => setP({ ...p, arguments: e.target.value })} /></Field>
          <Field label={t("mcp_mapping")} hint={t("mcp_mapping_help_" + p.target)}><textarea className="input mono" style={{ minHeight: 56 }} value={p.mapping} onChange={(e) => setP({ ...p, mapping: e.target.value })} /></Field></div>
        {p.target === "logs" && <div className="grid k3"><Field label={t("mcp_source_name")}><input className="input mono" value={p.source_name} onChange={(e) => setP({ ...p, source_name: e.target.value })} placeholder={`mcp-${s.name}`} /></Field>
          <Field label={t("env")}><input className="input" value={p.env} onChange={(e) => setP({ ...p, env: e.target.value })} /></Field>
          <Field label={t("mcp_max_rows")}><input className="input" type="number" min={1} max={10000} value={p.max_rows} onChange={(e) => setP({ ...p, max_rows: e.target.value })} /></Field></div>}
        {err && <div className="err">{err}</div>}
        <div className="row"><Btn kind="primary" disabled={!p.name || !p.tool} onClick={savePull}>💾 {t("src_save")}</Btn><Btn onClick={() => setP(null)}>✕</Btn></div></div>}
    </div></Card>;
}
