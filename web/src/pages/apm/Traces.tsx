import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { GitBranch } from "lucide-react";
import { get } from "../../api";
import { useT } from "../../i18n";
import { Btn, Card, Empty } from "../../components/ui";
import { LinkBadge, ScopeBar, fmtMs, svcColor, useScope } from "./common";

/** Trace explorer (SigNoz "Traces"): search by service, operation, minimum duration, failures or any text / trace id; each row is
 *  one request across tiers with how its spans were joined (shared id, session) and a duration bar on a common scale. */
export default function Traces() {
  const { t } = useT(); const sc = useScope(); const [sp, setSp] = useSearchParams();
  const [f, setF] = useState({ service: sp.get("service") || "", operation: sp.get("operation") || "", min_ms: sp.get("min_ms") || "", errors: sp.get("errors") === "1", q: sp.get("q") || "",
    session: sp.get("session") || "", user_ref: sp.get("user_ref") || "", order: sp.get("order") || "recent" });
  const [applied, setApplied] = useState(f);
  const svcs = useQuery({ queryKey: ["apm-services", sc.qs()], queryFn: () => get(`/apm/services?${sc.qs()}`) });
  const q = useQuery({ queryKey: ["traces", sc.qs(), applied], queryFn: () => get(`/traces?${sc.qs({ ...applied, errors: applied.errors ? "true" : "", limit: 100 })}`), refetchInterval: sc.live ? 20000 : false });
  const rows: any[] = q.data ?? [];
  const max = Math.max(1, ...rows.map((r) => r.duration_ms));
  const apply = (e?: React.FormEvent) => { e?.preventDefault(); setApplied(f); const n = new URLSearchParams(sp); for (const [k, v] of Object.entries(f)) { if (v && v !== "recent") n.set(k, v === true ? "1" : String(v)); else n.delete(k); } setSp(n, { replace: true }); };
  return <div className="stack">
    <div className="row between"><div><h2><GitBranch size={20} style={{ verticalAlign: -3 }} /> {t("apm_traces")}</h2><span className="muted small">{t("apm_traces_sub")}</span></div><ScopeBar /></div>
    <Card>
      <form className="row" style={{ gap: 8 }} onSubmit={apply}>
        <select className="input" style={{ width: 190 }} value={f.service} onChange={(e) => setF({ ...f, service: e.target.value })}><option value="">{t("apm_all_services")}</option>{(svcs.data?.services ?? []).map((s: any) => <option key={s.service} value={s.service}>{s.service}</option>)}</select>
        <input className="input" style={{ width: 220 }} placeholder={t("apm_f_op")} value={f.operation} onChange={(e) => setF({ ...f, operation: e.target.value })} />
        <input className="input" style={{ width: 120 }} type="number" min={0} placeholder={t("apm_f_min")} value={f.min_ms} onChange={(e) => setF({ ...f, min_ms: e.target.value })} />
        <input className="input grow" style={{ minWidth: 200 }} placeholder={t("apm_f_q")} value={f.q} onChange={(e) => setF({ ...f, q: e.target.value })} />
        <select className="input" style={{ width: 150 }} value={f.order} onChange={(e) => setF({ ...f, order: e.target.value })}><option value="recent">{t("apm_o_recent")}</option><option value="duration">{t("apm_o_slow")}</option></select>
        <label className="row small" style={{ gap: 4 }}><input type="checkbox" checked={f.errors} onChange={(e) => setF({ ...f, errors: e.target.checked })} />{t("apm_f_err")}</label>
        <Btn kind="primary" type="submit">{t("apm_search")}</Btn>
      </form>
      {(applied.session || applied.user_ref) && <div className="dim small" style={{ marginTop: 6 }}>{applied.session ? `${t("apm_session")}: ${applied.session}` : ""} {applied.user_ref ? `${t("apm_user")}: ${applied.user_ref}` : ""}</div>}
    </Card>
    <Card>
      {!rows.length ? <Empty>{t("apm_no_traces")}</Empty> :
        <div className="table-wrap"><table className="table"><thead><tr><th>{t("apm_col_start")}</th><th>{t("apm_col_root")}</th><th style={{ width: "26%" }}>{t("apm_col_dur")}</th><th className="right">{t("apm_col_spans")}</th><th>{t("apm_col_services")}</th><th>{t("apm_col_link")}</th></tr></thead>
          <tbody>{rows.map((r) => <tr key={r.trace_id}>
            <td className="mono">{r.start.replace("T", " ").slice(0, 19)}</td>
            <td><Link to={`/traces/${encodeURIComponent(r.trace_id)}${sc.link()}`}><b>{r.root_service}</b> · <span className="mono">{r.root_operation}</span></Link>{r.error ? <span className="badge red" style={{ marginLeft: 6 }}>{t("apm_err")}</span> : null}</td>
            <td><div className="row" style={{ gap: 8, flexWrap: "nowrap" }}><div className="durbar"><div style={{ width: `${Math.max(2, (r.duration_ms / max) * 100)}%`, background: r.error ? "var(--red)" : "var(--accent2)" }} /></div><span className="mono small">{fmtMs(r.duration_ms)}</span></div></td>
            <td className="num">{r.spans}</td>
            <td><div className="row" style={{ gap: 4 }}>{r.services.slice(0, 5).map((s: string) => <span key={s} className="dot" title={s} style={{ background: svcColor(s) }} />)}<span className="dim small">{r.services.length}</span></div></td>
            <td>{r.links.map((l: string) => <LinkBadge key={l} link={l} />)}</td>
          </tr>)}</tbody></table></div>}
    </Card>
  </div>;
}
