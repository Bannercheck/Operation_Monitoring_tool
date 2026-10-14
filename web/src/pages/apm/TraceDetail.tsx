import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { get } from "../../api";
import { useT } from "../../i18n";
import { Badge, Card, Empty, Kpi } from "../../components/ui";
import { LinkBadge, fmtMs, svcColor, useScope } from "./common";

/** One trace as a waterfall: every span on a common time axis, indented under its parent. Parents given by the logs are solid;
 *  parents inferred from time containment are marked, and each span says how it joined the trace, so nothing inferred looks
 *  like a measured fact. A span opens its details (attributes, message, evidence line). */
export default function TraceDetail() {
  const { t } = useT(); const sc = useScope(); const { id = "" } = useParams();
  const [sel, setSel] = useState<any>(null);
  const q = useQuery({ queryKey: ["trace", id, sc.dataset], queryFn: () => get(`/traces/${encodeURIComponent(id)}${sc.dataset !== "live" ? `?dataset=${sc.dataset}` : ""}`) });
  const d = q.data;
  if (q.isError) return <Empty>{t("apm_trace_missing")}</Empty>;
  if (!d) return <div className="muted">…</div>;
  const total = Math.max(d.duration_ms, 0.001);
  const tot = d.services.reduce((a: number, [, v]: any) => a + v, 0) || 1;
  return <div className="stack">
    <div><div className="dim small"><Link to={`/traces${sc.link()}`}>{t("apm_traces")}</Link> /</div><h2 className="mono" style={{ fontSize: 18 }}>{d.trace_id}</h2></div>
    <div className="grid k4">
      <Kpi value={fmtMs(d.duration_ms)} label={t("apm_col_dur")} sub={d.start.replace("T", " ").slice(0, 19)} />
      <Kpi value={d.spans.length} label={t("apm_col_spans")} accent="#60a5fa" sub={`${d.services.length} ${t("apm_k_services").toLowerCase()}`} />
      <Kpi value={d.errors} label={t("apm_errors")} accent="#f87171" sub={d.errors ? t("apm_has_err") : t("apm_no_err")} />
      <Kpi value={Object.entries(d.links).map(([k, v]) => `${t(`apm_link_${k}`)} ${v}`).join(" · ")} label={t("apm_k_evidence")} accent="#2dd4bf" sub={t("apm_k_inferred", { n: d.inferred_parents })} />
    </div>
    <Card title={t("apm_time_by_svc")}>
      <div className="stackbar">{d.services.map(([s, v]: any) => <div key={s} title={`${s} · ${fmtMs(v)}`} style={{ width: `${(v / tot) * 100}%`, background: svcColor(s) }} />)}</div>
      <div className="row" style={{ gap: 12, marginTop: 6 }}>{d.services.map(([s, v]: any) => <span key={s} className="small"><span className="dot" style={{ background: svcColor(s) }} /> {s} <b>{fmtMs(v)}</b> <span className="dim">({Math.round((v / tot) * 100)}%)</span></span>)}</div>
    </Card>
    <div className="grid" style={{ gridTemplateColumns: sel ? "minmax(0, 1fr) 360px" : "1fr", alignItems: "start" }}>
      <Card title={t("apm_waterfall")}>
        <div className="wf">
          <div className="wf-axis"><span>0</span><span>{fmtMs(total / 2)}</span><span>{fmtMs(total)}</span></div>
          {d.spans.map((s: any) => <div key={s.key} className={`wf-row ${sel?.key === s.key ? "on" : ""} ${s.error ? "err" : ""}`} onClick={() => setSel(s)}>
            <div className="wf-name" style={{ paddingLeft: 6 + s.depth * 14 }}>
              <span className="dot" style={{ background: svcColor(s.service) }} /><b>{s.service}</b> <span className="mono dim">{s.operation}</span>
              {s.parent_inferred && <span className="wf-inf" title={t("apm_inferred_hint")}>≈</span>}
            </div>
            <div className="wf-track">
              {s.kind === "log" ? <div className="wf-mark" style={{ left: `${(s.offset_ms / total) * 100}%`, background: s.error ? "var(--red)" : svcColor(s.service) }} /> :
                <div className="wf-bar" style={{ left: `${(s.offset_ms / total) * 100}%`, width: `${Math.max(0.4, (s.duration_ms / total) * 100)}%`, background: s.error ? "var(--red)" : svcColor(s.service) }} />}
              <span className="wf-dur mono">{s.kind === "log" ? "•" : fmtMs(s.duration_ms)}</span>
            </div>
          </div>)}
        </div>
      </Card>
      {sel && <Card title={<span><span className="dot" style={{ background: svcColor(sel.service) }} /> {sel.service}</span>} right={<button className="btn sm" onClick={() => setSel(null)}>✕</button>}>
        <div className="stack small">
          <div className="mono">{sel.operation}</div>
          <div className="row" style={{ gap: 6 }}><Badge v={sel.kind} /><LinkBadge link={sel.link} inferred={sel.parent_inferred} />{sel.error ? <Badge v="ERROR" /> : null}</div>
          <table className="kv"><tbody>
            <tr><td>{t("apm_col_dur")}</td><td>{fmtMs(sel.duration_ms)} <span className="dim">({t("apm_self")} {fmtMs(sel.self_ms)})</span></td></tr>
            <tr><td>{t("apm_offset")}</td><td>+{fmtMs(sel.offset_ms)}</td></tr>
            {sel.status ? <tr><td>HTTP</td><td>{sel.status}</td></tr> : null}
            {sel.host && <tr><td>{t("apm_host")}</td><td>{sel.host}</td></tr>}{sel.env && <tr><td>env</td><td>{sel.env}</td></tr>}
            {sel.user_ref && <tr><td>{t("apm_user")}</td><td className="mono"><Link to={`/traces?user_ref=${sel.user_ref}${sc.link().replace("?", "&")}`}>{sel.user_ref}</Link></td></tr>}
            {sel.session_id && <tr><td>{t("apm_session")}</td><td className="mono"><Link to={`/traces?session=${encodeURIComponent(sel.session_id)}${sc.link().replace("?", "&")}`}>{sel.session_id}</Link></td></tr>}
            {Object.entries(sel.attrs || {}).map(([k, v]) => <tr key={k}><td>{k}</td><td className="mono">{String(v)}</td></tr>)}
            <tr><td>{t("apm_evidence")}</td><td className="mono">{sel.ref}</td></tr>
          </tbody></table>
          {sel.message && <div className="card tight mono" style={{ whiteSpace: "pre-wrap" }}>{sel.message}</div>}
          <div className="dim">{t(`apm_link_${sel.link}_hint`)}{sel.parent_inferred ? ` ${t("apm_inferred_hint")}` : ""}</div>
        </div>
      </Card>}
    </div>
  </div>;
}
