import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { get } from "../api";
import { useT } from "../i18n";
import { Empty, Kpi } from "../components/ui";

export default function MapPage() {
  const { t, lang } = useT();
  const ds = useQuery({ queryKey: ["datasets"], queryFn: () => get("/datasets") });
  const [sp] = useSearchParams();
  const [src, setSrc] = useState<string>(sp.get("dataset") ?? ""); const [inc, setInc] = useState(sp.get("incident") ?? ""); const [env, setEnv] = useState(""); const [host, setHost] = useState(""); const [hosts, setHosts] = useState(true);
  const source = src || ds.data?.[0]?.id || "live";
  const q = new URLSearchParams({ dataset: source, lang, hosts: String(hosts) }); if (inc) q.set("incident", inc); if (env) q.set("env", env); if (host) q.set("host", host);
  const m = useQuery({ queryKey: ["map", q.toString()], queryFn: () => get(`/map?${q}`), refetchInterval: source === "live" ? 10000 : false });
  const d = m.data;
  return <div className="stack">
    <div><h2>🕸 {t("map_title")}</h2><span className="muted small">{t("map_sub")}</span></div>
    <div className="row">
      <select className="input" style={{ width: 240 }} value={source} onChange={(e) => { setSrc(e.target.value); setInc(""); setEnv(""); setHost(""); }}>{(ds.data ?? []).map((x: any) => <option key={x.id} value={x.id}>{x.name}</option>)}<option value="live">{t("map_live")}</option></select>
      <select className="input" style={{ width: 300 }} value={inc} onChange={(e) => setInc(e.target.value)}><option value="">{t("map_all_inc")}</option>{(d?.incidents ?? []).map((i: any) => <option key={i.id} value={i.id}>{i.id} · {i.title}</option>)}</select>
      <select className="input" style={{ width: 140 }} value={env} onChange={(e) => setEnv(e.target.value)}><option value="">{t("env")}: {t("all")}</option>{(d?.envs ?? []).map((x: string) => <option key={x}>{x}</option>)}</select>
      <select className="input" style={{ width: 180 }} value={host} onChange={(e) => setHost(e.target.value)}><option value="">{t("host")}: {t("all")}</option>{(d?.host_list ?? []).map((x: string) => <option key={x}>{x}</option>)}</select>
      <label className="check"><input type="checkbox" checked={hosts} onChange={(e) => setHosts(e.target.checked)} /> {t("map_hosts_toggle")}</label></div>
    {!d || d.empty ? <Empty>{t("map_empty")}</Empty> : <>
      <div className="grid k4">
        <Kpi value={d.nodes} label={t("map_k_services")} accent="#60a5fa" sub={`${d.hosts} ${t("host").toLowerCase()}`} />
        <Kpi value={d.deps} label={t("map_k_deps")} accent={d.deps ? "#f87171" : "#8b98ad"} sub={`${d.dep_events} ${t("ds_events")} · ${d.corr} ${t("map_k_corr")}`} />
        <Kpi value={(d.root ?? []).length || "—"} label={t("map_k_root")} accent="#f87171" sub={<span title={(d.root ?? []).join(", ")}>{(d.root ?? []).slice(0, 3).join(", ")}{(d.root ?? []).length > 3 ? ` +${d.root.length - 3}` : ""}{(d.incidents_hit ?? []).length ? ` · ${d.incidents_hit.slice(0, 3).join(", ")}` : ""}</span>} />
        <Kpi value={d.errors_total} label={t("map_k_errors")} accent="#fb923c" sub={`${d.affected} ${t("map_k_affected")}`} />
      </div>
      <div className="card" style={{ padding: 6 }}><iframe title="map" srcDoc={d.html} style={{ width: "100%", height: 660, border: 0, borderRadius: 10, background: "transparent" }} /></div></>}
  </div>;
}
