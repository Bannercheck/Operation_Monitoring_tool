import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Gauge, Plus, Trash2 } from "lucide-react";
import { del, get, post, put } from "../api";
import { useAuth } from "../auth";
import { TeamChip, TeamPick } from "../components/TeamPick";
import { useT } from "../i18n";
import { Btn, Card, Empty, Err, Field, Modal, Hint } from "../components/ui";

/** Per-server thresholds: every server with its current values against the levels that apply to it (and where each level comes
 *  from: this host, a name pattern, its pool, its environment, everyone, or System › Settings), the policies themselves with a live
 *  preview while editing, and the breaches open right now. Alerts go out by e-mail, SMS or webhook (Teams / Slack / JSON). */
const UNIT: Record<string, string> = { cpu: "%", memory: "%", disk: "%", gpu: "%", error_rate: "%", errors_per_min: "/dk", p95_ms: "ms", silent_min: "dk" };
const COLS = ["cpu", "memory", "disk", "gpu", "error_rate", "errors_per_min", "p95_ms", "silent_min"];
const fmt = (m: string, v: any) => (v === null || v === undefined ? "–" : `${Number(v).toLocaleString("tr-TR", { maximumFractionDigits: m === "error_rate" ? 2 : 1 })}${UNIT[m] === "%" ? "%" : UNIT[m] === "ms" ? " ms" : ""}`);
const EMPTY = { name: "", scope: "host", scope_value: "", metric: "cpu", warn: 80 as number | null, crit: 90 as number | null, for_min: 3, channels: "email", targets: "", cooldown_min: 30, notify_resolved: true, enabled: true, note: "" };

export default function Thresholds() {
  const { t } = useT(); const { can, user, teamsWith } = useAuth(); const qc = useQueryClient(); const [sp, setSp] = useSearchParams();
  const tab = (sp.get("tab") || "hosts") as "hosts" | "policies" | "open";
  const [edit, setEdit] = useState<any>(null); const [f, setF] = useState(sp.get("host") || "");
  const ov = useQuery({ queryKey: ["thr-ov"], queryFn: () => get("/thresholds/overview"), refetchInterval: 30000 });
  const pol = useQuery({ queryKey: ["thr-pol"], queryFn: () => get("/thresholds") });
  const open = useQuery({ queryKey: ["thr-open"], queryFn: () => get("/thresholds/state"), refetchInterval: 30000 });
  const rm = useMutation({ mutationFn: (id: number) => del(`/thresholds/${id}`), onSuccess: () => qc.invalidateQueries({ queryKey: ["thr-pol"] }) });
  const editable = (can("sys.notify") && user?.all !== false) || teamsWith("act.thresholds").length > 0;   // installation-wide policies, or the team's own
  const hosts: any[] = (ov.data?.hosts ?? []).filter((h: any) => !f || h.host.toLowerCase().includes(f.toLowerCase()) || h.pool.toLowerCase().includes(f.toLowerCase()));
  const nBad = (ov.data?.hosts ?? []).filter((h: any) => h.worst !== "ok").length;
  const setTab = (k: string) => { const n = new URLSearchParams(sp); n.set("tab", k); setSp(n, { replace: true }); };
  return <div className="stack">
    <div className="row between"><div><h2><Gauge size={20} style={{ verticalAlign: -3 }} /> {t("thr_title")}<Hint text={t("thr_sub")} howto="thresholds" /></h2></div>
      {editable && <Btn kind="primary" onClick={() => setEdit({ ...EMPTY })}><Plus size={14} /> {t("thr_new")}</Btn>}</div>
    <div className="seg">{([["hosts", `${t("thr_tab_hosts")} · ${ov.data?.hosts?.length ?? 0}`], ["policies", `${t("thr_tab_policies")} · ${pol.data?.length ?? 0}`], ["open", `${t("thr_tab_open")} · ${open.data?.length ?? 0}`]] as const).map(([k, l]) =>
      <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{l}</button>)}</div>
    {tab === "hosts" && <Card title={<span>{t("thr_hosts_title")} {nBad ? <span className="badge red" style={{ marginLeft: 6 }}>{nBad} {t("thr_beyond")}</span> : null}</span>}
      right={<input className="input sm" style={{ width: 200 }} placeholder={t("hm_find")} value={f} onChange={(e) => setF(e.target.value)} />}>
      {!hosts.length ? <Empty>{t("thr_no_hosts")}</Empty> :
        <div className="table-wrap" style={{ maxHeight: 620 }}><table className="table thr-table"><thead><tr><th>{t("host")}</th>{COLS.map((m) => <th key={m} className="right">{t(`thr_m_${m}`)}</th>)}<th /></tr></thead>
          <tbody>{hosts.slice(0, 400).map((h: any) => <tr key={h.host}>
            <td><b>{h.host}</b><div className="dim small">{h.pool} · {h.env}</div></td>
            {COLS.map((m) => { const x = h.metrics[m]; const lim = [x.warn != null ? `${t("thr_warn")} ${fmt(m, x.warn)}` : "", x.crit != null ? `${t("thr_crit")} ${fmt(m, x.crit)}` : ""].filter(Boolean).join(" · ");
              return <td key={m} className={`num thr-cell ${x.level ?? ""}`} title={x.source ? `${lim}\n${t("thr_from")}: ${t(`thr_scope_${x.source.scope}`)} ${x.source.value}${x.source.name ? ` (${x.source.name})` : ""}` : t("thr_no_level")}>
                {fmt(m, x.value)}{x.source && <div className="thr-lim">{x.warn != null ? fmt(m, x.warn) : ""}{x.warn != null && x.crit != null ? " / " : ""}{x.crit != null ? fmt(m, x.crit) : ""}</div>}</td>; })}
            <td>{editable && <Btn sm onClick={() => setEdit({ ...EMPTY, scope: "host", scope_value: h.host })}>{t("thr_set")}</Btn>}</td></tr>)}</tbody></table>
          <div className="dim small" style={{ padding: 8 }}>{t("thr_hosts_foot")}</div></div>}
    </Card>}
    {tab === "policies" && <Card>{!(pol.data ?? []).length ? <Empty>{t("thr_no_policies")}</Empty> :
      <div className="table-wrap"><table className="table"><thead><tr><th>{t("name")}</th><th>{t("thr_scope")}</th><th>{t("thr_metric")}</th><th className="right">{t("thr_warn")}</th><th className="right">{t("thr_crit")}</th><th className="right">{t("thr_for")}</th><th>{t("ntf_channels")}</th><th>{t("ntf_targets")}</th><th>{t("tmr_team")}</th><th /></tr></thead>
        <tbody>{(pol.data ?? []).map((p: any) => <tr key={p.id} style={p.enabled ? undefined : { opacity: 0.5 }}>
          <td><b>{p.name}</b>{p.note && <div className="dim small">{p.note}</div>}</td><td>{t(`thr_scope_${p.scope}`)} <span className="mono small">{p.scope_value}</span></td><td>{t(`thr_m_${p.metric}`)}</td>
          <td className="num">{fmt(p.metric, p.warn)}</td><td className="num">{fmt(p.metric, p.crit)}</td><td className="num">{p.for_min} {t("tr_min")}</td><td className="small">{p.channels || "–"}</td><td className="small">{p.targets || "–"}</td><td><TeamChip name={p.team} /></td>
          <td>{p.editable !== false && editable && <div className="row"><Btn sm onClick={() => setEdit({ ...p, notify_resolved: !!p.notify_resolved, enabled: !!p.enabled })}>{t("edit")}</Btn><Btn sm kind="danger" onClick={() => rm.mutate(p.id)}><Trash2 size={13} /></Btn></div>}</td></tr>)}</tbody></table></div>}
      <div className="dim small" style={{ marginTop: 8 }}>{t("thr_order")}</div></Card>}
    {tab === "open" && <Card>{!(open.data ?? []).length ? <Empty>{t("thr_none_open")}</Empty> :
      <div className="table-wrap"><table className="table"><thead><tr><th>{t("host")}</th><th>{t("thr_metric")}</th><th>{t("status")}</th><th className="right">{t("thr_value")}</th><th className="right">{t("thr_level")}</th><th>{t("thr_since")}</th><th>{t("thr_notified")}</th></tr></thead>
        <tbody>{(open.data ?? []).map((s: any) => <tr key={s.host + s.metric}><td><b>{s.host}</b></td><td>{t(`thr_m_${s.metric}`)}</td><td><span className={`badge ${s.level === "crit" ? "red" : "amber"}`} lang="en">{s.level === "crit" ? t("thr_crit") : t("thr_warn")}</span></td>
          <td className="num">{fmt(s.metric, s.value)}</td><td className="num">{fmt(s.metric, s.limit_value)}</td><td className="small mono">{String(s.since).slice(0, 16).replace("T", " ")}</td><td className="small">{s.notified_at ? String(s.notified_at).slice(11, 16) : t("thr_waiting")}</td></tr>)}</tbody></table></div>}</Card>}
    {edit && <Editor init={edit} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); qc.invalidateQueries({ queryKey: ["thr-pol"] }); qc.invalidateQueries({ queryKey: ["thr-ov"] }); }} />}
  </div>;
}

function Editor({ init, onClose, onSaved }: { init: any; onClose: () => void; onSaved: () => void }) {
  const { t } = useT(); const [p, setP] = useState<any>(init); const [pv, setPv] = useState<any>(null); const [pvErr, setPvErr] = useState<any>(null);
  const meta = useQuery({ queryKey: ["thr-meta"], queryFn: () => get("/thresholds/meta") });
  const rec = useQuery({ queryKey: ["ntf-rec"], queryFn: () => get("/notify/recipients") });
  const grp = useQuery({ queryKey: ["ntf-grp"], queryFn: () => get("/notify/groups") });
  const body = useMemo(() => ({ ...p, warn: p.warn === "" || p.warn == null ? null : Number(p.warn), crit: p.crit === "" || p.crit == null ? null : Number(p.crit), for_min: Number(p.for_min || 0), cooldown_min: Number(p.cooldown_min || 30) }), [p]);
  useEffect(() => { const h = setTimeout(() => { post("/thresholds/preview", body).then((r) => { setPv(r); setPvErr(null); }).catch((e) => { setPv(null); setPvErr(e); }); }, 350); return () => clearTimeout(h); }, [body]);
  const save = useMutation({ mutationFn: () => (p.id ? put(`/thresholds/${p.id}`, body) : post("/thresholds", body)), onSuccess: onSaved });
  const set = (k: string) => (e: any) => setP({ ...p, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const chans = String(p.channels || "").split(",").filter(Boolean);
  const toggleCh = (c: string) => setP({ ...p, channels: (chans.includes(c) ? chans.filter((x) => x !== c) : [...chans, c]).join(",") });
  const names = [...(grp.data ?? []).map((g: any) => g.name), ...(rec.data ?? []).map((r: any) => r.name)];
  const unit = UNIT[p.metric] ?? "";
  const teamRow = p.team_id != null;
  return <Modal wide title={p.id ? t("thr_edit") : t("thr_new")} onClose={onClose}>
    <div className="thr-ed">
      <div className="stack">
        {!p.id && <TeamPick perm="act.thresholds" value={p.team_id ?? null} onChange={(v) => setP({ ...p, team_id: v, channels: v != null ? chans.filter((c) => c !== "webhook").join(",") : p.channels })} />}
        <div className="grid k2">
          <Field label={t("thr_metric")}><select className="input" value={p.metric} onChange={set("metric")}>{Object.keys(meta.data?.metrics ?? UNIT).map((m) => <option key={m} value={m}>{t(`thr_m_${m}`)}</option>)}</select></Field>
          <Field label={t("thr_scope")}><select className="input" value={p.scope} onChange={set("scope")}>{["host", "glob", "application", "pool", "env", "all"].map((s) => <option key={s} value={s}>{t(`thr_scope_${s}`)}</option>)}</select></Field>
        </div>
        {p.scope !== "all" && <Field label={t(`thr_value_${p.scope}`)} hint={t(`thr_hint_${p.scope}`)}><input className="input mono" list={p.scope === "application" ? "thr-apps" : undefined} value={p.scope_value} onChange={set("scope_value")} placeholder={p.scope === "glob" ? "prd-sap-*" : p.scope === "env" ? "prod" : p.scope === "pool" ? "prd-sap-app" : p.scope === "application" ? "SAP ERP" : "prd-sap-app-01"} />
          <datalist id="thr-apps">{(meta.data?.applications ?? []).map((n: string) => <option key={n} value={n} />)}</datalist></Field>}
        <div className="grid k3">
          <Field label={`${t("thr_warn")} (${unit})`}><input className="input" type="number" value={p.warn ?? ""} onChange={set("warn")} /></Field>
          <Field label={`${t("thr_crit")} (${unit})`}><input className="input" type="number" value={p.crit ?? ""} onChange={set("crit")} /></Field>
          <Field label={t("thr_for_label")} hint={t("thr_for_hint")}><input className="input" type="number" min={0} value={p.for_min} onChange={set("for_min")} /></Field>
        </div>
        <Field label={t("ntf_channels")}><div className="row" style={{ gap: 6 }}>{["email", "sms", "webhook"].filter((c) => !(teamRow && c === "webhook")).map((c) => <button key={c} type="button" className={`chip ${chans.includes(c) ? "on" : ""}`} onClick={() => toggleCh(c)}>{t(`thr_ch_${c}`)}</button>)}</div></Field>
        <Field label={t("ntf_targets")} hint={t("thr_targets_hint")}><input className="input" list="thr-names" value={p.targets} onChange={set("targets")} placeholder="noc, Ayşe Yılmaz" /><datalist id="thr-names">{names.map((n) => <option key={n} value={n} />)}</datalist></Field>
        <div className="grid k2">
          <Field label={t("ntf_cooldown")}><input className="input" type="number" min={1} value={p.cooldown_min} onChange={set("cooldown_min")} /></Field>
          <Field label={t("name")}><input className="input" value={p.name} onChange={set("name")} placeholder={t("thr_name_ph")} /></Field>
        </div>
        <label className="row small"><input type="checkbox" checked={!!p.notify_resolved} onChange={set("notify_resolved")} /> {t("thr_resolved")}</label>
        <label className="row small"><input type="checkbox" checked={!!p.enabled} onChange={set("enabled")} /> {t("src_enabled")}</label>
        <Err e={save.error} />
        <div className="row"><Btn kind="primary" onClick={() => save.mutate()}>{t("save")}</Btn><Btn onClick={onClose}>{t("cancel")}</Btn></div>
      </div>
      <div className="thr-pv">
        <h4>{t("thr_preview")}</h4>
        {pvErr ? <div className="err small">{String(pvErr?.message ?? pvErr)}</div> : !pv ? <div className="dim small">…</div> : <>
          <div className="small">{t("thr_pv_cover", { n: pv.covered, w: pv.wins })}</div>
          {pv.wins < pv.covered && <div className="dim small">{t("thr_pv_more_specific", { n: pv.covered - pv.wins })}</div>}
          <div className="small" style={{ marginTop: 8 }}><b>{pv.beyond.length ? t("thr_pv_beyond", { n: pv.beyond.length }) : t("thr_pv_none")}</b></div>
          <div className="thr-pv-list">{pv.beyond.map((b: any) => <div key={b.host} className="row between small"><span>{b.host}</span><span className={b.level === "crit" ? "bad" : ""} style={{ color: b.level === "warn" ? "var(--amber)" : undefined }}>{fmt(p.metric, b.value)}</span></div>)}</div>
          {pv.hosts.length > 0 && <div className="dim small" style={{ marginTop: 8 }}>{pv.hosts.slice(0, 12).join(", ")}{pv.hosts.length > 12 ? ` +${pv.hosts.length - 12}` : ""}</div>}
        </>}
      </div>
    </div>
  </Modal>;
}
