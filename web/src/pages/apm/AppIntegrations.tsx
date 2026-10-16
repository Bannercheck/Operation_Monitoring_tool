import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { get } from "../../api";
import { useT } from "../../i18n";
import { Card, Empty, Kpi } from "../../components/ui";
import { CAT_COLOR, MapView, ipOf } from "./Integrations";
import { ConfLegend, type Sel, TechLabel, fmtTs, selStr } from "./IntegTech";

/** Integrations of one application: the flows of its member servers (explicit, inventory application, pool, and the servers its
 *  services ran on) folded into one map. Callers and targets outside the application are its integrations, named or known only by
 *  their IP, each with the member server it goes through ("via") and the applications the peer belongs to; flows between members
 *  are internal. A line or row opens on that member server in the server view, with the same detail drawer. */
export function AppIntegrationsView() {
  const { t } = useT(); const [sp, setSp] = useSearchParams(); const [q, setQ] = useState("");
  const minutes = Number(sp.get("minutes") || 1440);
  const apps = useQuery({ queryKey: ["integ-apps"], queryFn: () => get("/apps?minutes=60"), refetchInterval: 60000 });
  const list: any[] = (apps.data as any)?.apps ?? [];
  const id = Number(sp.get("app") || list[0]?.id || 0);
  const pick = (aid: number) => { const n = new URLSearchParams(sp); n.set("mode", "app"); n.set("app", String(aid)); setSp(n, { replace: true }); };
  const toHost = (host: string, s?: Sel) => {
    const n = new URLSearchParams(); if (sp.get("minutes")) n.set("minutes", sp.get("minutes")!);
    n.set("host", host); if (s) n.set("ix", selStr(s)); setSp(n, { replace: true });
  };
  const m = useQuery({ queryKey: ["integ-app", id, minutes], queryFn: () => get(`/apps/${id}/integrations?minutes=${minutes}`), enabled: !!id, refetchInterval: 30000 });
  const d: any = m.data;
  const ql = q.trim().toLowerCase();
  const shown = list.filter((a) => !ql || a.name.toLowerCase().includes(ql) || (a.tags ?? []).some((x: string) => x.toLowerCase().includes(ql)));
  if (!list.length) return <Card><Empty>{apps.isLoading ? t("loading") : t("integ_no_apps")}</Empty></Card>;
  const onSel = (s: Sel | null) => {
    if (!s || !d || s.dir === "host") return;
    const hit = s.dir === "out" ? d.outbound.find((o: any) => o.target === s.peer && String(o.port) === s.port)
      : s.dir === "in" ? d.inbound.find((c: any) => c.source === s.peer && String(c.port) === s.port) : d.listens.find((l: any) => String(l.port) === s.port);
    const via = hit?.via?.[0] ?? hit?.host;
    if (via) toHost(via, s);
  };
  const hostBtn = (h: string, s?: Sel) => <a key={h} href="#" onClick={(e) => { e.preventDefault(); toHost(h, s); }} className="mono" style={{ marginRight: 6 }}>{h}</a>;
  const peer = (name: string, x: any) => <><b className={x.known === false ? "mono" : ""}>{name}</b>
    {ipOf(x) ? <div className="dim small mono">{ipOf(x)}</div> : x.known === false ? <div className="dim small">{t("integ_unknown")}</div> : null}
    {!!x.apps?.length && <div className="small">{x.apps.map((a: string) => <span key={a} className="chip" style={{ marginRight: 4 }}>▣ {a}</span>)}</div>}</>;
  return <div className="grid integ-grid">
    <Card title={t("integ_apps")}>
      <input className="input" placeholder={t("integ_app_search")} value={q} onChange={(e) => setQ(e.target.value)} style={{ marginBottom: 8 }} />
      <div className="stack integ-hosts" style={{ gap: 2 }}>{shown.map((a) => <button key={a.id} className={`hostrow ${a.id === id ? "on" : ""}`} onClick={() => pick(a.id)}>
        <div style={{ minWidth: 0 }}><b>▣ {a.name}</b><div className="dim small">{a.criticality}{a.envs?.length ? ` · ${a.envs.join(", ")}` : ""}{a.team ? ` · ${a.team}` : ""}</div></div></button>)}
        {!shown.length && <div className="dim small" style={{ padding: 8 }}>{t("none")}</div>}</div>
    </Card>
    <div className="stack" style={{ minWidth: 0 }}>
      {!d ? <Card><Empty>{m.isError ? String((m.error as any)?.message ?? "") : t("loading")}</Empty></Card> : <>
        <div className="grid k4">
          <Kpi value={d.inbound.length} label={t("integ_in")} sub={t("integ_app_in_sub")} accent="#60a5fa" />
          <Kpi value={d.outbound.length} label={t("integ_out")} sub={Object.entries(d.categories).map(([k, v]) => `${t(`integ_cat_${k}`)} ${v}`).join(" · ") || "-"} accent="#2dd4bf" />
          <Kpi value={d.internal.length} label={t("integ_internal")} sub={t("integ_internal_sub")} accent="#a78bfa" />
          <Kpi value={d.members.length} label={t("integ_members")} sub={d.members.map((x: any) => x.host).slice(0, 3).join(", ") || t("integ_no_members")} accent="#fbbf24" />
        </div>
        <Card title={`${t("integ_map")} · ▣ ${d.host}`} right={<span className="dim small">{t("integ_app_click")}</span>}>
          {!d.members.length ? <Empty>{t("integ_no_members")}</Empty> : !d.inbound.length && !d.outbound.length && !d.listens.length ? <Empty>{t("integ_none_app")}</Empty>
            : <MapView m={d} sel={null} onSel={onSel} />}
          <ConfLegend />
        </Card>
        <Card title={t("integ_members")}>{!d.members.length ? <Empty>{t("integ_no_members")}</Empty> :
          <div className="table-wrap"><table className="table"><thead><tr><th>{t("integ_hosts")}</th><th>IP</th><th className="right">{t("integ_in")}</th><th className="right">{t("integ_out")}</th><th className="right">{t("integ_listens")}</th></tr></thead>
            <tbody>{d.members.map((x: any) => <tr key={x.host}><td>{hostBtn(x.host)}</td><td className="mono small">{x.ips.join(", ") || <span className="dim">{t("integ_no_ip")}</span>}</td>
              <td className="num">{x.inbound}</td><td className="num">{x.outbound}</td><td className="num">{x.listens}</td></tr>)}</tbody></table></div>}
          {d.truncated > 0 && <div className="dim small">{t("integ_truncated", { n: d.truncated })}</div>}</Card>
        <Card title={t("integ_out")}>{!d.outbound.length ? <Empty>{t("integ_none_app")}</Empty> :
          <div className="table-wrap"><table className="table integ-t"><thead><tr><th>{t("integ_kind")}</th><th>{t("integ_target")}</th><th>{t("itc_tech")}</th><th>{t("integ_proto")}</th><th>{t("integ_via")}</th><th className="right">{t("topo_conn")}</th><th>{t("integ_last")}</th></tr></thead>
            <tbody>{d.outbound.map((o: any, i: number) => { const s: Sel = { dir: "out", peer: o.target, port: String(o.port) };
              return <tr key={i} className="trow" onClick={() => onSel(s)}>
                <td><span className="badge icat" style={{ ["--c" as any]: CAT_COLOR[o.category] ?? "#94a3b8" }}>{t(`integ_cat_${o.category}`)}</span></td>
                <td className="tgt">{peer(o.target, o)}</td><td><TechLabel tech={o.tech} /></td><td className="mono">{o.protocol}:{o.port}</td>
                <td className="small" onClick={(e) => e.stopPropagation()}>{o.via.map((h: string) => hostBtn(h, s))}</td><td className="num">{o.connections}</td><td className="small nowrap">{fmtTs(o.last_seen)}</td></tr>; })}</tbody></table></div>}</Card>
        <Card title={t("integ_in")}>{!d.inbound.length ? <Empty>{t("integ_none_app")}</Empty> :
          <div className="table-wrap"><table className="table integ-t"><thead><tr><th>{t("integ_source")}</th><th>{t("itc_tech")}</th><th>{t("integ_proto")}</th><th>{t("integ_via")}</th><th className="right">{t("topo_conn")}</th><th>{t("integ_last")}</th></tr></thead>
            <tbody>{d.inbound.map((c: any, i: number) => { const s: Sel = { dir: "in", peer: c.source, port: String(c.port) };
              return <tr key={i} className="trow" onClick={() => onSel(s)}><td className="tgt">{peer(c.source, c)}</td><td><TechLabel tech={c.tech} /></td><td className="mono">{c.protocol}:{c.port}</td>
                <td className="small" onClick={(e) => e.stopPropagation()}>{c.via.map((h: string) => hostBtn(h, s))}</td><td className="num">{c.connections}</td><td className="small">{fmtTs(c.last_seen)}</td></tr>; })}</tbody></table></div>}</Card>
        {d.internal.length > 0 && <Card title={t("integ_internal")}>
          <div className="table-wrap"><table className="table integ-t"><thead><tr><th>{t("integ_source")}</th><th>{t("integ_target")}</th><th>{t("itc_tech")}</th><th>{t("integ_proto")}</th><th className="right">{t("topo_conn")}</th></tr></thead>
            <tbody>{d.internal.map((e: any, i: number) => <tr key={i}><td>{hostBtn(e.source, { dir: "out", peer: e.target, port: String(e.port) })}</td><td className="mono">{e.target}</td>
              <td><TechLabel tech={e.tech} /></td><td className="mono">{e.protocol}:{e.port}</td><td className="num">{e.connections}</td></tr>)}</tbody></table></div></Card>}
        <div className="dim small">{t("integ_app_hint")}</div>
      </>}
    </div>
  </div>;
}
