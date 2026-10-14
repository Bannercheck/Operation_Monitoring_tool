import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { get } from "../../api";
import { useT } from "../../i18n";
import { Card, Empty, Kpi } from "../../components/ui";
import { fmtMs } from "../apm/common";

/** One user session, step by step: app start, screens / pages, taps and clicks, the network calls they made, errors and
 *  crashes, each with its duration and, when the backend logged the same trace id, a link to the full waterfall. */
const ICON: Record<string, string> = { start: "🚀", view: "📄", action: "👆", http: "🌐", error: "⚠️", crash: "💥", user: "👤", mobile: "📱" };

export default function Session() {
  const { t } = useT(); const { id = "" } = useParams();
  const q = useQuery({ queryKey: ["rum-session", id], queryFn: () => get(`/rum/sessions/${encodeURIComponent(id)}`), refetchInterval: 20000 });
  const s = q.data;
  if (q.isError) return <Empty>{t("rum_session_missing")}</Empty>;
  if (!s) return <div className="muted">…</div>;
  const t0 = s.steps.length ? Date.parse(s.steps[0].ts) : 0;
  const maxDur = Math.max(1, ...s.steps.map((x: any) => x.duration_ms || 0));
  return <div className="stack">
    <div><div className="dim small"><Link to="/rum?tab=sessions">{t("rum_tab_sessions")}</Link> /</div><h2>{s.app} <span className="dim small mono">{s.session_id}</span></h2></div>
    <div className="grid k4">
      <Kpi value={fmtMs(s.duration_ms)} label={t("rum_col_dur")} sub={`${s.started.replace("T", " ").slice(0, 19)}`} />
      <Kpi value={`${s.views + s.actions}`} label={t("rum_col_steps")} accent="#60a5fa" sub={`${s.views} ${t("rum_col_views").toLowerCase()} · ${s.actions} ${t("rum_actions")} · ${s.requests} ${t("rum_calls")}`} />
      <Kpi value={s.crashes ? `${s.crashes} 💥` : `${s.errors}`} label={s.crashes ? t("rum_crash") : t("apm_errors")} accent="#f87171" sub={s.crashes || s.errors ? t("apm_has_err") : t("apm_no_err")} />
      <Kpi value={s.platform} label={t("rum_col_device")} accent="#a78bfa" sub={[s.version, s.os, s.browser, s.model, s.net, s.carrier].filter(Boolean).join(" · ")} />
    </div>
    <Card title={t("rum_timeline")} right={s.user_ref ? <Link className="btn sm" to={`/rum?tab=sessions&user_ref=${s.user_ref}`}>{t("rum_user_sessions")}</Link> : null}>
      {!s.steps.length ? <Empty>{t("apm_none")}</Empty> : <div className="tl">
        {s.steps.map((x: any, i: number) => <div key={i} className={`tl-row ${x.error ? "err" : ""} ${x.step === "http" ? "sub" : ""}`}>
          <span className="tl-t mono">+{fmtMs(Date.parse(x.ts) - t0)}</span>
          <span className="tl-i">{ICON[x.step] ?? "•"}</span>
          <div className="tl-n"><span className={x.step === "http" ? "mono small" : ""}>{x.name}</span>{x.status ? <span className={`badge ${x.status >= 500 || x.status === 0 ? "red" : x.status >= 400 ? "amber" : ""}`} style={{ marginLeft: 6 }}>{x.status}</span> : null}
            {Object.keys(x.vitals || {}).length > 0 && <div className="dim small">{Object.entries(x.vitals).map(([k, v]: any) => `${k.toUpperCase()} ${k === "cls" ? v : fmtMs(v)}`).join(" · ")}</div>}
            {x.stack && <pre className="code small" style={{ marginTop: 4, maxHeight: 120, overflow: "auto" }}>{x.stack}</pre>}
            {(x.step === "error" || x.step === "crash") && x.message && !x.stack && <div className="dim small mono">{x.message}</div>}</div>
          <div className="tl-d">{x.duration_ms > 0 && <><div className="durbar"><div style={{ width: `${Math.max(2, (x.duration_ms / maxDur) * 100)}%`, background: x.error ? "var(--red)" : "var(--accent2)" }} /></div><span className="mono small">{fmtMs(x.duration_ms)}</span></>}</div>
          <div className="tl-b">{x.trace_id && x.backend ? <Link to={`/traces/${x.trace_id}`} className="small">🧵 {t("rum_backend", { n: x.backend.services })}{x.backend.error ? " ⚠" : ""}</Link> : x.trace_id && x.step !== "error" ? <Link to={`/traces/${x.trace_id}`} className="dim small">🧵</Link> : null}</div>
        </div>)}
      </div>}
    </Card>
    <div className="dim small">{t("rum_kvkk")}</div>
  </div>;
}
