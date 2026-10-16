import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, del, get, patch, post, put } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Badge, Btn, Card, Confirm, Empty, Err, Field, Modal, Table, Tabs, fmtTs } from "../components/ui";
import DiscoveredHosts from "../components/DiscoveredHosts";
import { TeamChip, TeamPick } from "../components/TeamPick";
import { EnrollKeyCard } from "./Teams";
import { McpKeysPanel } from "./Mcp";
import { InstallCmds } from "../components/InstallCmd";

type Tab = "agents" | "discovered" | "mcp";

export default function Connections() {
  const { t } = useT(); const [sp] = useSearchParams(); const [tab, setTab] = useState<Tab>((sp.get("tab") as Tab) || "agents");
  return <div className="stack"><h2>🔌 {t("nav_conn")}</h2>
    <Tabs value={tab} onChange={setTab} tabs={[{ k: "agents", label: t("conn_agents") }, { k: "discovered", label: t("conn_discovered") }, { k: "mcp", label: t("mcp_tab") }]} />
    {tab === "agents" && <Agents />}{tab === "discovered" && <Discovered />}{tab === "mcp" && <McpKeysPanel />}</div>;
}

function Agents() {
  const { t } = useT(); const { can, user, teamsWith } = useAuth(); const qc = useQueryClient(); const [name, setName] = useState(""); const [env, setEnv] = useState("prod"); const [tok, setTok] = useState<any>(null);
  const [team, setTeam] = useState<number | null>(null);
  const wide = user?.all !== false;                                // the fleet key is installation-wide; a team member uses the team's own key
  const list = useQuery({ queryKey: ["agents"], queryFn: () => get("/agents"), refetchInterval: 15000 });
  const key = useQuery({ queryKey: ["enroll-key"], queryFn: () => get("/agents/enroll-key"), enabled: can("act.connect") && wide });
  const inv = () => { qc.invalidateQueries({ queryKey: ["agents"] }); qc.invalidateQueries({ queryKey: ["enroll-key"] }); };
  const enroll = useMutation({ mutationFn: () => post("/agents", { name, env, team_id: team }), onSuccess: (r: any) => { setTok(r); setName(""); inv(); } });
  const act = useMutation({ mutationFn: (p: { id: number; op: string }) => (p.op === "delete" ? del(`/agents/${p.id}`) : post(`/agents/${p.id}/${p.op}`)), onSuccess: (r: any, p) => { if (p.op === "rotate") setTok({ agent: { name: "" }, token: r.token }); inv(); } });
  const rotateKey = useMutation({ mutationFn: () => post("/agents/enroll-key/rotate"), onSuccess: inv });
  const upd = useMutation({ mutationFn: (p: { id: number; op: string; version?: string }) => post(`/agents/${p.id}/update/${p.op}`, p.op === "pin" ? { version: p.version ?? "" } : undefined), onSuccess: inv });
  const editable = can("act.connect");
  return <div className="stack">
    {editable && <div className="grid k2">
      <Card title={t("agent_enroll")}><div className="row"><input className="input" placeholder={t("name")} value={name} onChange={(e) => setName(e.target.value)} /><input className="input" style={{ width: 120 }} value={env} onChange={(e) => setEnv(e.target.value)} />
        <TeamPick perm="act.connect" value={team} onChange={setTeam} style={{ width: 170 }} /><Btn kind="primary" onClick={() => enroll.mutate()} disabled={!name}>{t("add")}</Btn></div><Err e={enroll.error} /></Card>
      {!wide ? <div className="stack">{teamsWith("act.connect").map((id) => <EnrollKeyCard key={id} tid={id} title={(user?.teams ?? []).find((m) => m.id === id)?.name} />)}</div> :
      <Card title={t("agent_key")}><div className="row"><span className="mono">{key.data?.key || "-"}</span><Btn sm onClick={() => rotateKey.mutate()}>{t("agent_key_rotate")}</Btn></div>
        <div style={{ marginTop: 8 }}><InstallCmds auth={`--enroll-key ${key.data?.key || "wk_…"} --env prod`} /></div></Card>}</div>}
    <AgentUpdates editable={editable && wide} />
    <Card><Table cols={[{ k: "name", label: t("name"), render: (r) => <div><b>{r.name}</b>{(r.team || wide) && <div><TeamChip name={r.team} /></div>}</div> }, { k: "env", label: t("env") }, { k: "site", label: "site" }, { k: "status", label: t("status"), render: (r) => <Badge v={r.status} /> },
      { k: "last_seen", label: t("agent_last"), render: (r) => <span className="muted small">{fmtTs(r.last_seen)} {r.last_ip}</span> }, { k: "events", label: t("agent_events"), num: true },
      { k: "update", label: t("agu_version"), render: (r) => <AgentVersion r={r} editable={editable} onOp={(op, version) => upd.mutate({ id: r.id, op, version })} /> },
      { k: "x", label: "", render: (r) => editable ? <div className="row">{r.status === "active" ? <Btn sm onClick={() => act.mutate({ id: r.id, op: "revoke" })}>{t("agent_revoke")}</Btn> : <Btn sm onClick={() => act.mutate({ id: r.id, op: "reactivate" })}>{t("agent_reactivate")}</Btn>}
        <Btn sm onClick={() => act.mutate({ id: r.id, op: "rotate" })}>{t("agent_rotate")}</Btn><Confirm onConfirm={() => act.mutate({ id: r.id, op: "delete" })}>{t("delete")}</Confirm></div> : null }]} rows={list.data ?? []} /></Card>
    {tok && <Modal title={t("agent_enroll")} onClose={() => setTok(null)}><p className="muted">{t("agent_token_once")}</p><pre className="code">{tok.token}</pre>
      <div className="small muted">{t("agent_install")}</div><InstallCmds auth={`--token ${tok.token} --env ${env}`} /></Modal>}
  </div>;
}

const UPD_COLOR: Record<string, string> = { current: "done", updating: "medium", failed: "open", rolled_back: "open", paused: "pending", waiting: "pending" };

/** One agent's build and self-update state (X-Agent-Version / X-Agent-Update), with pause / resume and "go back to this version". */
function AgentVersion({ r, editable, onOp }: { r: any; editable: boolean; onOp: (op: string, version?: string) => void }) {
  const { t } = useT(); const u = r.update || {}; const [pick, setPick] = useState(false);
  const versions = useQuery({ queryKey: ["agent-updates"], queryFn: () => get("/agents/updates"), enabled: pick });
  const st: string = u.state || "unknown";
  const tip = [u.error, u.target && u.target !== u.version ? `→ ${u.target}` : ""].filter(Boolean).join(" ");
  return <div className="stack" style={{ gap: 2 }}>
    <div className="row" style={{ gap: 6 }}><span className="mono small">{u.engine === "agent.py" ? "agent.py " : ""}{u.version || "-"}</span>
      <span title={tip}><Badge v={UPD_COLOR[st] ?? "disabled"} label={t("agu_st_" + st)} /></span>
      {u.pin && <span className="dim small">{t("agu_pinned", { v: u.pin })}</span>}</div>
    {u.error && (st === "failed" || st === "rolled_back" || st === "unsupported") && <div className="dim small" style={{ maxWidth: 260 }}>{u.error}</div>}
    {editable && r.status === "active" && <div className="row" style={{ gap: 4 }}>
      {u.paused ? <Btn sm onClick={() => onOp("resume")}>{t("agu_resume")}</Btn> : <Btn sm onClick={() => onOp("pause")}>{t("agu_pause")}</Btn>}
      {u.pin ? <Btn sm onClick={() => onOp("pin", "")}>{t("agu_unpin")}</Btn> : <Btn sm onClick={() => setPick(!pick)}>{t("agu_pin")}</Btn>}</div>}
    {pick && <div className="row" style={{ gap: 4 }} title={t("agu_pin_hint")}>
      <select className="input" style={{ width: 120 }} defaultValue="" onChange={(e) => { if (e.target.value) { onOp("pin", e.target.value); setPick(false); } }}>
        <option value="">…</option>{((versions.data as any)?.versions ?? []).map((v: any) => <option key={v.seq} value={v.version}>{v.version}</option>)}</select></div>}
  </div>;
}

/** The global self-update switch, the staged rollout share and the signed release (keyd) with its key fingerprint. */
function AgentUpdates({ editable }: { editable: boolean }) {
  const { t } = useT(); const qc = useQueryClient();
  const q = useQuery({ queryKey: ["agent-updates"], queryFn: () => get("/agents/updates"), refetchInterval: 60000 });
  const save = useMutation({ mutationFn: (b: any) => put("/agents/updates", b), onSuccess: () => { qc.invalidateQueries({ queryKey: ["agent-updates"] }); qc.invalidateQueries({ queryKey: ["agents"] }); } });
  const s: any = q.data;
  if (!s) return null;
  return <Card title={t("agu_title")}><div className="stack" style={{ gap: 6 }}>
    <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
      <label className="check"><input type="checkbox" checked={!!s.enabled} disabled={!editable} onChange={(e) => save.mutate({ enabled: e.target.checked })} /> {t("agu_switch")}</label>
      <label className="row small" style={{ gap: 6 }}>{t("agu_rollout")}
        <select className="input" style={{ width: 130 }} value={s.pct} disabled={!editable} onChange={(e) => save.mutate({ pct: Number(e.target.value) })}>
          {[5, 10, 25, 50, 100].map((p) => <option key={p} value={p}>{p === 100 ? t("agu_rollout_all") : `%${p}`}</option>)}</select></label>
      {s.signed ? <span className="small">{t("agu_release")}: <b className="mono">{s.version}</b> · {t("agu_key")}: <span className="mono">{s.fp}</span></span>
        : <span className="small err">{t("agu_unsigned")}{s.error ? ` (${s.error})` : ""}</span>}</div>
    <div className="dim small">{t("agu_switch_hint")}</div><Err e={save.error} /></div></Card>;
}

/** Discovered hosts: the shared pool view (one row per pool, bulk promote), same as Inventory → Discovered. */
function Discovered() {
  return <Card><DiscoveredHosts /></Card>;
}
