import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { get } from "../../api";
import { useT } from "../../i18n";
import { LineChartCard, settled } from "./LineChartCard";
import { fmtN } from "./util";

/** Client sessions seen in the server logs (application, SSO, or a user / client address when no session id is logged), for the
 *  range and scope of the operations screen. Every id is a pseudonym made at ingest (KVKK): the table can say "this user had 3
 *  errors on prd-web-02" without anyone reading who that user is. Web / mobile sessions of the user sensors sit on the RUM page. */
const KIND: Record<string, string> = { app: "", sso: "violet", client: "", rum: "blue" };
const STEPS = [15, 60, 360, 1440, 10080];

/** the trace search only takes "the last N minutes": pick the smallest of its choices that still reaches the session's start */
function linkOf(s: any): string {
  if (!s.link || !s.link.startsWith("/traces")) return s.link;
  const back = (Date.now() - Date.parse(s.first)) / 60000; const m = STEPS.find((x) => x >= back) ?? 10080;
  return `${s.link}&minutes=${m}`;
}
/** pseudonyms are opaque: 10 hex characters are plenty to tell rows apart on screen, the full one is in the tooltip and the search */
const short = (x: string) => (x && x.length > 12 ? x.slice(0, 12) : x);
const dur = (s: number, lang: string) => { const [sn, dk, sa] = lang === "tr" ? ["sn", "dk", "sa"] : ["s", "min", "h"]; return s < 60 ? `${s} ${sn}` : s < 3600 ? `${Math.round(s / 60)} ${dk}` : `${(s / 3600).toFixed(1)} ${sa}`; };
const hm = (iso: string, day: boolean) => { const d = new Date(iso); const p = (x: number) => String(x).padStart(2, "0"); return `${day ? `${p(d.getDate())}.${p(d.getMonth() + 1)} ` : ""}${p(d.getHours())}:${p(d.getMinutes())}`; };

/** compact (inside the fleet card): services move into the hosts cell's tooltip and the duration joins the start time */
export function SessionTable({ rows, day = false, max = 420, loading = false, compact = false }: { rows: any[]; day?: boolean; max?: number; loading?: boolean; compact?: boolean }) {
  const { t, lang } = useT();
  if (loading) return <div className="empty">{t("loading")}</div>;
  if (!rows.length) return <div className="empty">{t("ses_none")}</div>;
  return <div className="table-wrap" style={{ maxHeight: max }}><table className="table ses-t"><thead><tr><th>{t("ses_col_id")}</th><th>{t("ses_col_who")}</th><th>{t("ses_col_hosts")}</th>{!compact && <th>{t("ses_col_svc")}</th>}
    <th>{t(compact ? "ses_col_start" : "ses_col_when")}</th>{!compact && <th className="right">{t("ses_col_dur")}</th>}<th className="right">{t("ses_col_lines")}</th><th className="right">{t("ses_col_err")}</th></tr></thead>
    <tbody>{rows.map((s) => <tr key={s.id}>
      <td className="nw"><span className={`badge ${KIND[s.kind] ?? ""}`}>{t(`ses_k_${s.kind}`)}</span> {s.link ? <Link className="mono small" to={linkOf(s)} title={`${s.id} · ${t(s.kind === "rum" ? "ses_to_rum" : "ses_to_traces")}`}>{short(s.id)}</Link> : <span className="mono small dim" title={s.id}>{short(s.id)}</span>}</td>
      <td className="mono small nw" title={[s.user, s.client].filter(Boolean).join(" · ")}>{s.user ? short(s.user) : <span className="dim">{short(s.client) || "–"}</span>}</td>
      <td className="small nw" title={`${s.hosts.join(", ")}${compact && s.services.length ? `\n${s.services.join(", ")}` : ""}`}>{s.hosts[0]}{s.hosts.length > 1 ? <span className="dim"> +{s.hosts.length - 1}</span> : null}</td>
      {!compact && <td className="small nw" title={s.services.join(", ")}>{s.services[0] ?? "–"}{s.services.length > 1 ? <span className="dim"> +{s.services.length - 1}</span> : null}</td>}
      {compact ? <td className="mono small nw" title={`${hm(s.first, day)}–${hm(s.last, false)}`}>{hm(s.first, day)} <span className="dim">· {dur(s.duration_s, lang)}</span></td>
        : <><td className="mono small nw">{hm(s.first, day)}–{hm(s.last, false)}</td><td className="num nw">{dur(s.duration_s, lang)}</td></>}<td className="num nw">{fmtN(s.lines, lang)}</td>
      <td className="num" style={{ color: s.errors ? "var(--red)" : undefined }}>{s.errors ? fmtN(s.errors, lang) : "–"}</td></tr>)}</tbody></table></div>;
}

/** the fleet card's "Sessions" view: active sessions per bar, the web / mobile line when no server scope is set, the busiest
 *  hosts, and the list with a search */
export function SessionsView({ rq, span, onZoom }: { rq: string; span: number; onZoom: (t: number, stepMin: number) => void }) {
  const { t, lang } = useT(); const [q, setQ] = useState("");
  const r = useQuery({ queryKey: ["range-sessions", rq, q], queryFn: () => get(`/live/range/sessions?${rq}&limit=200${q ? `&q=${encodeURIComponent(q)}` : ""}`), refetchInterval: rq.includes("last=") ? 15000 : false });
  const d = r.data; const hosts: any[] = (d?.by_host ?? []).slice(0, 8); const top = Math.max(1, ...hosts.map((h) => h.sessions));
  if (d?.source === "history") return <div className="empty">{t("ses_hist")}</div>;
  return <div className="stack">
    <div className="ses-top">
      <LineChartCard title={t("lc_active")} sub={d ? `${fmtN(d.total, lang)} ${t("ses_total")}${d.overflow ? ` · ${t("ses_overflow", { n: fmtN(d.overflow, lang) })}` : ""}` : ""} height={196} span={span}
        series={[{ key: "n", label: t("lc_active"), color: "var(--s1)" }]} data={settled(d?.active_series ?? [], d?.step_min ?? 1, rq.includes("last="))} valueFormat={(v) => fmtN(v, lang)} onPick={(x) => onZoom(x, d?.step_min ?? 1)} pickLabel={t("lc_zoom")} empty={d ? undefined : t("loading")} />
      <div className="ses-hosts"><div className="lc-t" style={{ marginBottom: 6 }}>{t("ses_by_host")}</div>
        {!hosts.length ? <div className="dim small">–</div> : hosts.map((h) => <div key={h.host} className="ses-hb" title={`${h.users} ${t("ses_users")} · ${h.errors} ${t("ses_col_err").toLowerCase()}`}>
          <span className="mono">{h.host}</span><b>{fmtN(h.sessions, lang)}</b><i style={{ width: `${(h.sessions / top) * 100}%` }} /></div>)}</div>
    </div>
    {d?.rum?.sessions > 0 && <div className="small muted">{t("ses_rum", { s: fmtN(d.rum.sessions, lang), u: fmtN(d.rum.users, lang), e: fmtN(d.rum.errors, lang) })} · <Link to="/rum?tab=sessions">{t("ses_to_rum_page")}</Link></div>}
    <input className="input sm" style={{ width: 260 }} placeholder={t("ses_find")} value={q} onChange={(e) => setQ(e.target.value)} />
    <SessionTable rows={d?.sessions ?? []} day={span > 1440} loading={!d} compact />
    <div className="dim small">{t("ses_kvkk")}</div>
  </div>;
}

/** the same server-side list for the RUM page's sessions tab (the page's range, no ops scope) */
export function ServerSessions({ minutes = 1440 }: { minutes?: number }) {
  const { t, lang } = useT(); const [q, setQ] = useState("");
  const r = useQuery({ queryKey: ["range-sessions", `last=${minutes}`, q], queryFn: () => get(`/live/range/sessions?last=${minutes}&limit=200${q ? `&q=${encodeURIComponent(q)}` : ""}`), refetchInterval: 20000 });
  const d = r.data;
  return <>
    <div className="row" style={{ gap: 8, marginBottom: 10 }}><input className="input sm" style={{ width: 240 }} placeholder={t("ses_find")} value={q} onChange={(e) => setQ(e.target.value)} />
      {d && <span className="dim small">{fmtN(d.total, lang)} {t("ses_total")}</span>}<span className="grow" /><span className="dim small">{t("ses_kvkk")}</span></div>
    {d?.source === "history" ? <div className="empty">{t("ses_hist")}</div> : <SessionTable rows={d?.sessions ?? []} day max={640} loading={!d} />}
  </>;
}
