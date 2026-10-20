import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Route as RouteIcon, Plus, Trash2, Wand2 } from "lucide-react";
import { del, get, post, put } from "../../api";
import { useAuth } from "../../auth";
import { useT } from "../../i18n";
import { Badge, Btn, Card, Empty, Err, Field, Kpi, Modal, Tabs, Hint } from "../../components/ui";
import { RANGES, pct } from "./common";

/** Business flows: one business key (order, request, application number) followed across every system that logs it. Steps are
 *  message patterns; the funnel shows how many keys reached each step, where they wait too long (stuck) and where they failed. */
const secs = (s?: number | null) => (s === null || s === undefined ? "-" : s < 90 ? `${Math.round(s)} sn` : s < 5400 ? `${Math.round(s / 60)} dk` : `${(s / 3600).toFixed(1)} sa`);

export default function Flows() {
  const { t } = useT(); const { can } = useAuth(); const qc = useQueryClient();
  const [minutes, setMinutes] = useState(1440); const [sel, setSel] = useState<number | null>(null);
  const [state, setState] = useState<"stuck" | "error" | "done">("stuck"); const [key, setKey] = useState<string | null>(null); const [edit, setEdit] = useState<any>(null);
  const q = useQuery({ queryKey: ["flows", minutes], queryFn: () => get(`/flows?minutes=${minutes}`), refetchInterval: 15000 });
  const flows: any[] = q.data ?? [];
  useEffect(() => { if (flows.length && !flows.some((f) => f.id === sel)) setSel(flows[0].id); }, [flows, sel]);
  const f = flows.find((x) => x.id === sel);
  const items = useQuery({ queryKey: ["flow-items", sel, state, minutes], queryFn: () => get(`/flows/${sel}/items?state=${state}&minutes=${minutes}&limit=100`), enabled: !!sel, refetchInterval: 15000 });
  const one = useQuery({ queryKey: ["flow-item", sel, key], queryFn: () => get(`/flows/${sel}/items/${encodeURIComponent(key!)}`), enabled: !!sel && !!key });
  const rm = useMutation({ mutationFn: (id: number) => del(`/flows/${id}`), onSuccess: () => { setSel(null); qc.invalidateQueries({ queryKey: ["flows"] }); } });
  const first = f?.funnel?.[0]?.reached || 1;
  return <div className="stack">
    <div className="row between"><div><h2><RouteIcon size={20} style={{ verticalAlign: -3 }} /> {t("bf_title")}<Hint text={t("bf_sub")} howto="flows" /></h2></div>
      <div className="row"><select value={minutes} onChange={(e) => setMinutes(+e.target.value)}>{RANGES.map((m) => <option key={m} value={m}>{m < 60 ? `${m} dk` : m < 1440 ? `${m / 60} sa` : `${m / 1440} gün`}</option>)}</select>
        {can("act.anomaly") && <Btn kind="primary" onClick={() => setEdit({ name: "", key_regex: "", steps: [{ name: "", match: "", service: "" }, { name: "", match: "", service: "" }], hash_keys: false, enabled: true })}><Plus size={14} /> {t("bf_new")}</Btn>}</div></div>
    {!flows.length ? <Card><Empty>{t("bf_empty")}</Empty></Card> : <>
      <div className="row wrap" style={{ gap: 8 }}>{flows.map((x) => <button key={x.id} className={`btn ${x.id === sel ? "primary" : ""}`} onClick={() => { setSel(x.id); setKey(null); }}>
        {x.name} <span className="dim small">· {x.items} · {pct(x.completion, 1)}</span>{x.stuck ? <span className="badge amber" style={{ marginLeft: 6 }}>{x.stuck}</span> : null}</button>)}</div>
      {f && <>
        <div className="grid k4">
          <Kpi value={f.items.toLocaleString()} label={t("bf_k_items")} sub={`${t("bf_key")}: ${f.key_regex}`} />
          <Kpi value={pct(f.completion, 1)} label={t("bf_k_done")} accent="#34d399" sub={`${f.completed.toLocaleString()} ${t("bf_completed")}`} />
          <Kpi value={f.stuck} label={t("bf_k_stuck")} accent="#fbbf24" sub={t("bf_k_stuck_sub")} />
          <Kpi value={f.errors} label={t("bf_k_err")} accent="#f87171" sub={t("bf_k_err_sub")} />
        </div>
        <Card title={t("bf_funnel")} right={can("act.anomaly") && <><Btn sm onClick={() => setEdit({ ...f, steps: f.steps })}>{t("edit")}</Btn><Btn sm kind="danger" onClick={() => rm.mutate(f.id)}><Trash2 size={13} /></Btn></>}>
          <div className="stack" style={{ gap: 6 }}>{f.funnel.map((s: any, i: number) => <div key={i} className="funnel-row">
            <div className="small" style={{ width: 170 }}><b>{i + 1}. {s.name}</b><div className="dim">{s.from_prev !== null ? `${pct(s.from_prev, 1)} ${t("bf_from_prev")}` : t("bf_start")}</div></div>
            <div style={{ flex: 1 }}><div className="durbar" style={{ height: 22 }}><div style={{ width: `${Math.max(1, (s.reached / first) * 100)}%`, background: "var(--accent)", height: "100%", borderRadius: 6 }} /></div></div>
            <div className="num small" style={{ width: 90 }}><b>{s.reached.toLocaleString()}</b></div>
            <div className="small" style={{ width: 230 }}>{s.stuck ? <span className="badge amber">{s.stuck} {t("bf_stuck")}</span> : null} {s.errors ? <span className="badge red">{s.errors} {t("bf_failed")}</span> : null}
              {s.to_next_p50_s !== null && <span className="dim"> → p50 {secs(s.to_next_p50_s)} · p95 {secs(s.to_next_p95_s)}</span>}</div>
          </div>)}</div>
          <div className="dim small" style={{ marginTop: 8 }}>{t("bf_stuck_rule")}</div>
        </Card>
        <Card>
          <Tabs value={state} onChange={(k) => setState(k)} tabs={[{ k: "stuck", label: t("bf_tab_stuck") }, { k: "error", label: t("bf_tab_err") }, { k: "done", label: t("bf_tab_done") }]} />
          {!(items.data ?? []).length ? <Empty>{t("bf_no_items")}</Empty> : <div className="table-wrap"><table className="table"><thead><tr><th>{t("bf_key")}</th><th>{t("bf_last_step")}</th><th className="right">{t("bf_waiting")}</th><th>{t("bf_where")}</th><th>{t("bf_failure")}</th></tr></thead>
            <tbody>{(items.data ?? []).map((r: any) => <tr key={r.key} className="clickable" onClick={() => setKey(r.key)}>
              <td className="mono"><b>{r.key}</b><div className="dim small">{r.started.slice(5, 16).replace("T", " ")}</div></td>
              <td>{r.last_step || "-"} <span className="dim small">({r.steps_done}/{f.funnel.length})</span></td><td className="num">{r.stuck ? <b style={{ color: "var(--amber)" }}>{secs(r.waiting_s)}</b> : secs(r.waiting_s)}</td>
              <td className="small">{r.last_service}<div className="dim">{r.last_host}</div></td>
              <td className="small">{r.failure ? <><Badge v="ERROR" label={r.failure.at} /> <span className="dim">{r.failure.message.slice(0, 90)}</span></> : "-"}</td></tr>)}</tbody></table></div>}
        </Card></>}
    </>}
    {key && one.data && <Modal title={`${one.data.flow} · ${key}`} onClose={() => setKey(null)} wide>
      <div className="bf-tl">{one.data.steps.map((s: any) => <div key={s.index} className={`bf-row ${s.missing ? "dim" : ""}`}>
        <div className="mono small" style={{ width: 150 }}>{s.ts ? s.ts.slice(0, 19).replace("T", " ") : "—"}</div>
        <div style={{ flex: 1 }}><b>{s.index + 1}. {s.name}</b> {s.missing ? <span className="badge">{t("bf_not_yet")}</span> : <span className="dim small">· {s.service} @ {s.host}</span>}
          {s.message && <div className="small mono dim">{s.message}</div>}</div></div>)}
        {one.data.failure && <div className="bf-row"><div className="mono small" style={{ width: 150 }}>{one.data.failure.ts.slice(0, 19).replace("T", " ")}</div>
          <div style={{ flex: 1 }}><Badge v="ERROR" label={`${t("bf_failed_at")}: ${one.data.failure.at}`} /> <span className="dim small">{one.data.failure.service} @ {one.data.failure.host}</span>
            <div className="small mono">{one.data.failure.message}</div></div></div>}</div>
    </Modal>}
    {edit && <Editor init={edit} onClose={() => setEdit(null)} onSaved={(id) => { setEdit(null); setSel(id); qc.invalidateQueries({ queryKey: ["flows"] }); }} />}
  </div>;
}

function Editor({ init, onClose, onSaved }: { init: any; onClose: () => void; onSaved: (id: number) => void }) {
  const { t } = useT();
  const [d, setD] = useState<any>({ ...init, steps: init.steps.map((s: any) => ({ name: s.name, match: s.match, service: s.service ?? "" })) });
  const [sugg, setSugg] = useState<any[] | null>(null);
  const save = useMutation({ mutationFn: () => (d.id ? put(`/flows/${d.id}`, d) : post("/flows", d)), onSuccess: (r: any) => onSaved(r.id) });
  const sg = useMutation({ mutationFn: () => post("/flows/suggest", { key_regex: d.key_regex, minutes: 1440 }), onSuccess: (r: any) => setSugg(r.steps) });
  const st = (i: number, k: string, v: string) => setD({ ...d, steps: d.steps.map((s: any, j: number) => (j === i ? { ...s, [k]: v } : s)) });
  return <Modal title={d.id ? t("bf_edit") : t("bf_new")} onClose={onClose} wide>
    <div className="stack">
      <Field label={t("bf_f_name")}><input value={d.name} onChange={(e) => setD({ ...d, name: e.target.value })} placeholder="Sipariş → Fatura" /></Field>
      <Field label={t("bf_f_key")} hint={t("bf_f_key_hint")}><div className="row"><input className="mono" style={{ flex: 1 }} value={d.key_regex} onChange={(e) => setD({ ...d, key_regex: e.target.value })} placeholder="sipariş\s+(\d{7})" />
        <Btn onClick={() => sg.mutate()} disabled={!d.key_regex}><Wand2 size={14} /> {t("bf_suggest")}</Btn></div></Field>
      {sugg && <div className="card" style={{ padding: 10 }}>{!sugg.length ? <span className="dim small">{t("bf_sugg_none")}</span> : <>
        <div className="small dim" style={{ marginBottom: 6 }}>{t("bf_sugg_lead")}</div>
        {sugg.map((s, i) => <div key={i} className="row between small" style={{ padding: "3px 0" }}><span className="mono">{s.template.slice(0, 90)}</span><span className="dim">{s.service} · {s.keys}</span></div>)}
        <Btn sm kind="primary" onClick={() => { setD({ ...d, steps: sugg.map((s) => ({ name: s.name, match: s.match, service: "" })) }); setSugg(null); }}>{t("bf_sugg_use")}</Btn></>}</div>}
      <div className="stack" style={{ gap: 6 }}>{d.steps.map((s: any, i: number) => <div key={i} className="row" style={{ gap: 6 }}>
        <span className="dim" style={{ width: 18 }}>{i + 1}.</span>
        <input style={{ width: 160 }} value={s.name} placeholder={t("bf_f_step")} onChange={(e) => st(i, "name", e.target.value)} />
        <input className="mono" style={{ flex: 1 }} value={s.match} placeholder={t("bf_f_match")} onChange={(e) => st(i, "match", e.target.value)} />
        <input style={{ width: 140 }} value={s.service} placeholder={t("bf_f_service")} onChange={(e) => st(i, "service", e.target.value)} />
        <Btn sm onClick={() => setD({ ...d, steps: d.steps.filter((_: any, j: number) => j !== i) })} disabled={d.steps.length <= 2}><Trash2 size={13} /></Btn></div>)}
        <Btn sm onClick={() => setD({ ...d, steps: [...d.steps, { name: "", match: "", service: "" }] })}><Plus size={13} /> {t("bf_add_step")}</Btn></div>
      <label className="row small"><input type="checkbox" checked={d.hash_keys} onChange={(e) => setD({ ...d, hash_keys: e.target.checked })} /> {t("bf_f_hash")}</label>
      {save.error && <Err e={save.error} />}
      <div className="row"><Btn kind="primary" onClick={() => save.mutate()} disabled={!d.name || !d.key_regex}>{t("save")}</Btn><Btn onClick={onClose}>{t("cancel")}</Btn></div>
    </div></Modal>;
}
