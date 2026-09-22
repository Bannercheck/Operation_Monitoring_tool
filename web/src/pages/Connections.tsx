import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { SourcesPanel } from "./Sources";
import { InventoryPanel } from "./Inventory";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, del, get, patch, post, put } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Badge, Btn, Card, Confirm, Empty, Err, Field, Modal, Table, Tabs, fmtTs } from "../components/ui";

type Tab = "agents" | "sources" | "inventory" | "discovered";

export default function Connections() {
  const { t } = useT(); const [sp] = useSearchParams(); const [tab, setTab] = useState<Tab>((sp.get("tab") as Tab) || "agents");
  return <div className="stack"><h2>🔌 {t("nav_conn")}</h2>
    <Tabs value={tab} onChange={setTab} tabs={[{ k: "agents", label: t("conn_agents") }, { k: "sources", label: t("conn_sources") }, { k: "inventory", label: t("conn_inv") }, { k: "discovered", label: t("conn_discovered") }]} />
    {tab === "agents" && <Agents />}{tab === "sources" && <SourcesPanel />}{tab === "inventory" && <InventoryPanel />}{tab === "discovered" && <Discovered />}</div>;
}

function Agents() {
  const { t } = useT(); const { can } = useAuth(); const qc = useQueryClient(); const [name, setName] = useState(""); const [env, setEnv] = useState("prod"); const [tok, setTok] = useState<any>(null);
  const list = useQuery({ queryKey: ["agents"], queryFn: () => get("/agents"), refetchInterval: 15000 });
  const key = useQuery({ queryKey: ["enroll-key"], queryFn: () => get("/agents/enroll-key"), enabled: can("act.connect") });
  const inv = () => { qc.invalidateQueries({ queryKey: ["agents"] }); qc.invalidateQueries({ queryKey: ["enroll-key"] }); };
  const enroll = useMutation({ mutationFn: () => post("/agents", { name, env }), onSuccess: (r: any) => { setTok(r); setName(""); inv(); } });
  const act = useMutation({ mutationFn: (p: { id: number; op: string }) => (p.op === "delete" ? del(`/agents/${p.id}`) : post(`/agents/${p.id}/${p.op}`)), onSuccess: (r: any, p) => { if (p.op === "rotate") setTok({ agent: { name: "" }, token: r.token }); inv(); } });
  const rotateKey = useMutation({ mutationFn: () => post("/agents/enroll-key/rotate"), onSuccess: inv });
  const host = window.location.host.replace(/:\d+$/, "");
  const editable = can("act.connect");
  return <div className="stack">
    {editable && <div className="grid k2">
      <Card title={t("agent_enroll")}><div className="row"><input className="input" placeholder={t("name")} value={name} onChange={(e) => setName(e.target.value)} /><input className="input" style={{ width: 120 }} value={env} onChange={(e) => setEnv(e.target.value)} /><Btn kind="primary" onClick={() => enroll.mutate()} disabled={!name}>{t("add")}</Btn></div><Err e={enroll.error} /></Card>
      <Card title={t("agent_key")}><div className="row"><span className="mono">{key.data?.key || "-"}</span><Btn sm onClick={() => rotateKey.mutate()}>{t("agent_key_rotate")}</Btn></div>
        <pre className="code" style={{ marginTop: 8 }}>{`curl -fsSL http://${host}:8600/agent/install.sh | sudo bash -s -- --url http://${host}:8600/ingest --enroll-key ${key.data?.key || "wk_…"} --env prod`}</pre></Card></div>}
    <Card><Table cols={[{ k: "name", label: t("name"), render: (r) => <b>{r.name}</b> }, { k: "env", label: t("env") }, { k: "site", label: "site" }, { k: "status", label: t("status"), render: (r) => <Badge v={r.status} /> },
      { k: "last_seen", label: t("agent_last"), render: (r) => <span className="muted small">{fmtTs(r.last_seen)} {r.last_ip}</span> }, { k: "events", label: t("agent_events"), num: true },
      { k: "x", label: "", render: (r) => editable ? <div className="row">{r.status === "active" ? <Btn sm onClick={() => act.mutate({ id: r.id, op: "revoke" })}>{t("agent_revoke")}</Btn> : <Btn sm onClick={() => act.mutate({ id: r.id, op: "reactivate" })}>{t("agent_reactivate")}</Btn>}
        <Btn sm onClick={() => act.mutate({ id: r.id, op: "rotate" })}>{t("agent_rotate")}</Btn><Confirm onConfirm={() => act.mutate({ id: r.id, op: "delete" })}>{t("delete")}</Confirm></div> : null }]} rows={list.data ?? []} /></Card>
    {tok && <Modal title={t("agent_enroll")} onClose={() => setTok(null)}><p className="muted">{t("agent_token_once")}</p><pre className="code">{tok.token}</pre>
      <div className="small muted">{t("agent_install")}</div><pre className="code">{`curl -fsSL http://${host}:8600/agent/install.sh | sudo bash -s -- --url http://${host}:8600/ingest --token ${tok.token} --env ${env}`}</pre></Modal>}
  </div>;
}

function Discovered() {
  const { t } = useT(); const { can } = useAuth(); const qc = useQueryClient();
  const q = useQuery({ queryKey: ["discovered"], queryFn: () => get("/inventory/discovered"), refetchInterval: 15000 });
  const add = useMutation({ mutationFn: (r: any) => put("/inventory", { hostname: r.hostname, env: r.env ?? "", ip: r.ip ?? "", dc: r.dc ?? "" }), onSuccess: () => { qc.invalidateQueries({ queryKey: ["discovered"] }); qc.invalidateQueries({ queryKey: ["inventory"] }); } });
  const rows = (q.data ?? []).map((r: any, i: number) => ({ id: i, ...r }));
  return <Card><Table cols={[{ k: "hostname", label: t("host"), render: (r) => <b>{r.hostname}</b> }, { k: "env", label: t("env") }, { k: "ip", label: "IP" }, { k: "dc", label: t("inv_dc") }, { k: "source", label: "" },
    { k: "x", label: "", render: (r) => can("act.inventory") ? <Btn sm onClick={() => add.mutate(r)}>{t("inv_add_from")}</Btn> : null }]} rows={rows} /><Err e={add.error} /></Card>;
}