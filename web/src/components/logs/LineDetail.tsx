import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Check, Copy, Download, ExternalLink, Minus, Plus } from "lucide-react";
import { useT } from "../../i18n";
import { copyText, errText, fmtSkew, lg, LV_ALIAS, toErr, tsFull, tsLocal, tsUtc } from "./lib";

/** One line's detail, inline under its row: Detail (every field and attribute, each with filter / exclude / copy), Context (the
 *  lines before and after it on the same host and file, the anchor highlighted, matches of the current query marked), Raw (the
 *  scrubbed original line) and JSON (the whole answer). Clicking a context line opens its own detail in place. */
export type FilterFn = (op: "add" | "exclude", field: string, value: string) => void;

export function CopyBtn({ text, label }: { text: string; label?: boolean }) {
  const { t } = useT(); const [ok, setOk] = useState(false);
  useEffect(() => { if (!ok) return; const h = setTimeout(() => setOk(false), 1400); return () => clearTimeout(h); }, [ok]);
  return <button className={label ? "btn sm" : "lg-ab"} title={t("lg_copy")} onClick={async (e) => { e.stopPropagation(); setOk(await copyText(text)); }}>
    {ok ? <Check size={12} /> : <Copy size={12} />}{label && (ok ? t("lg_copied") : t("lg_copy"))}</button>;
}

function Val({ field, value, show, onFilter, children }: { field?: string; value: string; show?: ReactNode; onFilter: FilterFn; children?: ReactNode }) {
  const { t } = useT();
  return <span className="lg-val"><span className="lg-vt" title={value}>{show ?? value}</span>{children}
    <span className="lg-acts">{field && <><button className="lg-ab" title={t("lg_filter_by")} onClick={() => onFilter("add", field, value)}><Plus size={12} /></button>
      <button className="lg-ab" title={t("lg_exclude_val")} onClick={() => onFilter("exclude", field, value)}><Minus size={12} /></button></>}
      <CopyBtn text={value} /></span></span>;
}

export function LineDetail({ id, q, onFilter, onGroup, onHost }: { id: string; q: string; onFilter: FilterFn; onGroup: (key: string) => void; onHost: (h: string) => void }) {
  const { t, lang } = useT();
  const [cur, setCur] = useState(id); const [tab, setTab] = useState<"detail" | "context" | "raw" | "json">("detail");
  useEffect(() => { setCur(id); }, [id]);
  const d = useQuery({ queryKey: ["lg-line", cur], queryFn: () => lg(`/line/${cur}`), staleTime: 60000, retry: false });
  const x: any = d.data;
  const tabs: [typeof tab, string][] = [["detail", t("lg_t_detail")], ["context", t("lg_t_context")], ["raw", t("lg_t_raw")], ["json", t("lg_t_json")]];
  return <div className="lg-exp" onClick={(e) => e.stopPropagation()}>
    <div className="lg-exp-h">
      <div className="seg sm">{tabs.map(([k, l]) => <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{l}</button>)}</div>
      {cur !== id && <button className="linklike small" onClick={() => setCur(id)}><ArrowLeft size={12} style={{ verticalAlign: -2 }} /> {t("lg_back_line")}</button>}
      <span className="grow" />{x && <span className="dim small mono">#{x.id} · {x.source_of_detail === "ring" ? t("lg_from_ring") : t("lg_from_store")}</span>}
    </div>
    {d.isLoading ? <div className="dim small" style={{ padding: 8 }}>{t("loading")}</div> : d.error ? <div className="dim small" style={{ padding: 8 }}>{toErr(d.error).status === 404 ? t("lg_line_gone") : errText(toErr(d.error), lang)}</div> :
      tab === "detail" ? <Detail x={x} onFilter={onFilter} onGroup={onGroup} onHost={onHost} /> :
      tab === "context" ? <Context id={cur} q={q} anchor={x} onOpen={(lid) => { setCur(lid); setTab("detail"); }} /> :
      tab === "raw" ? <div className="lg-rawbox">
        {x.raw_state === "stored" || (x.raw && x.raw_state !== "absent") ? <><pre className="lx-pre lg-pre">{x.raw || x.message}</pre><div className="row" style={{ marginTop: 6 }}><CopyBtn text={x.raw || x.message} label /></div></> : null}
        {x.raw_state === "same" && <div className="dim small" style={{ marginTop: 6 }}>{t("lg_raw_same")}</div>}
        {x.raw_state === "absent" && <div className="dim small">{t("lg_raw_absent")}</div>}
      </div> :
      <div><pre className="lx-pre lg-pre lg-json">{JSON.stringify(x, null, 2)}</pre>
        <div className="row" style={{ marginTop: 6 }}><CopyBtn text={JSON.stringify(x, null, 2)} label />
          <button className="btn sm" onClick={() => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([JSON.stringify(x, null, 2)], { type: "application/json" })); a.download = `log-${x.id}.json`; a.click(); }}><Download size={12} /> {t("lg_download")}</button></div></div>}
  </div>;
}

function Detail({ x, onFilter, onGroup, onHost }: { x: any; onFilter: FilterFn; onGroup: (key: string) => void; onHost: (h: string) => void }) {
  const { t } = useT();
  const rows: [string, ReactNode][] = [];
  const skew = x.skew_s != null ? t("lg_skew", { d: fmtSkew(x.skew_s), dir: x.skew_s > 0 ? t("lg_behind") : t("lg_ahead") }) : "";
  rows.push([t("lg_k_time"), <span className="lg-val"><span className="lg-vt mono">{tsFull(x.ts)}</span><span className="dim small">({tsUtc(x.ts)})</span>
    {x.received && <span className="dim small">· {t("lg_received")} {tsLocal(x.received, { date: false })}</span>}{skew && <span className="badge amber" style={{ textTransform: "none" }}>⏱ {skew}</span>}</span>]);
  rows.push([t("lg_c_level"), <Val field="level" value={LV_ALIAS[x.level] ?? x.level} show={<span className={`lg-lvl ${x.level}`}>{t(`lg_lv_${x.level}`)}</span>} onFilter={onFilter} />]);
  rows.push([t("lg_c_service"), <Val field="service" value={x.service} onFilter={onFilter} />]);
  rows.push([t("lg_c_host"), <Val field="host" value={x.host} show={<button className="linklike mono" onClick={() => onHost(x.host)}>{x.host}</button>} onFilter={onFilter}>
    {x.host_link?.ops && <Link className="small lg-link" to={x.host_link.ops}>{t("lg_open_ops")} <ExternalLink size={11} /></Link>}</Val>]);
  rows.push([t("lg_c_env"), <Val field="env" value={x.env} onFilter={onFilter} />]);
  if (x.pool) rows.push([t("lg_c_pool"), <Val field="pool" value={x.pool} onFilter={onFilter} />]);
  rows.push([t("lg_k_file"), <Val field="source" value={x.source} show={<span className="mono">{x.source}{x.line_no ? `:${x.line_no}` : ""}</span>} onFilter={onFilter} />]);
  if (x.agent) rows.push([t("lg_c_agent"), <Val field="agent" value={x.agent} onFilter={onFilter} />]);
  if (x.kind) rows.push([t("lg_k_kind"), <Val field="kind" value={x.kind} onFilter={onFilter} />]);
  if (x.origin) rows.push([t("lg_k_origin"), <Val field="origin" value={x.origin} onFilter={onFilter} />]);
  if (x.parser) rows.push([t("lg_k_parser"), <Val value={x.parser} onFilter={onFilter} />]);
  if (x.trace?.id) rows.push([t("lg_c_trace"), <Val field="trace" value={x.trace.id} show={<span className="mono">{x.trace.id.slice(0, 16)}…</span>} onFilter={onFilter}>
    {x.trace.stored ? <Link className="small lg-link" to={x.trace.link}>{t("lg_open_trace")} →</Link> : <span className="dim small">{t("lg_trace_missing")}</span>}</Val>]);
  if (x.session?.id) rows.push([t("lg_c_session"), <Val field="session" value={x.session.id} show={<span className="mono">{x.session.id}</span>} onFilter={onFilter}>
    {x.session.link && <Link className="small lg-link" to={x.session.rum ? `/rum/sessions/${x.session.rum}` : x.session.link}>{t("lg_open_session")} →</Link>}</Val>]);
  if (x.session?.user) rows.push([t("lg_k_user"), <Val field="user" value={x.session.user} show={<span className="mono">{x.session.user}</span>} onFilter={onFilter} />]);
  if (x.session?.client) rows.push([t("lg_k_client"), <Val field="client" value={x.session.client} show={<span className="mono">{x.session.client}</span>} onFilter={onFilter} />]);
  const attrs = Object.entries(x.attrs ?? {}).filter(([k]) => !k.startsWith("_")).sort(([a], [b]) => a.localeCompare(b));
  return <div>
    <div className="lg-kv">{rows.map(([k, v], i) => <div key={i} className="lg-kv-r"><span className="lg-kl">{k}</span>{v}</div>)}
      {attrs.map(([k, v]) => <div key={k} className="lg-kv-r"><span className="lg-kl mono">@{k}</span><Val field={`@${k}`} value={typeof v === "string" ? v : JSON.stringify(v)} show={<span className="mono">{typeof v === "string" ? v : JSON.stringify(v)}</span>} onFilter={onFilter} /></div>)}
      {x.attrs_dropped > 0 && <div className="lg-kv-r"><span className="lg-kl" /><span className="dim small">{t("lg_attrs_dropped", { n: x.attrs_dropped })}</span></div>}
    </div>
    <div className="lg-msgh"><span className="lg-kl">{t("lg_c_message")}</span><span className="grow" /><CopyBtn text={x.message} label />
      {x.group?.key && <button className="btn sm" onClick={() => onGroup(x.group.key)} title={x.group.template}>{t("lg_same_group")}{x.group.count_1h ? ` · ${x.group.count_1h.toLocaleString()}` : ""}</button>}</div>
    <pre className="lx-pre lg-pre">{x.message}</pre>
    {x.message_truncated && <div className="dim small">{t("lg_truncated")}</div>}
  </div>;
}

function Context({ id, q, anchor, onOpen }: { id: string; q: string; anchor: any; onOpen: (id: string) => void }) {
  const { t } = useT(); const [nb, setNb] = useState(20); const [na, setNa] = useState(20);
  const c = useQuery({ queryKey: ["lg-ctx", id, nb, na, q], queryFn: () => lg(`/context/${id}`, { before: nb, after: na, q }), staleTime: 30000, retry: false, placeholderData: (p: any) => p });
  const d: any = c.data;
  const row = (l: any, cls = "") => <button key={l.id} className={`lg-cl ${cls} ${l.match ? "match" : ""}`} onClick={() => cls !== "anchor" && onOpen(l.id)} title={l.match ? t("lg_ctx_match") : undefined}>
    <span className="mono dim">{tsLocal(l.ts, { date: false })}</span><span className={`lg-lvl ${l.level}`}>{t(`lg_lv_${l.level}`)}</span>
    <span className="mono dim">{l.line_no ?? ""}</span><span className="mono lg-cm">{l.message}</span></button>;
  if (c.isLoading) return <div className="dim small" style={{ padding: 8 }}>{t("loading")}</div>;
  if (c.error || !d) return <div className="dim small" style={{ padding: 8 }}>{t("lg_line_gone")}</div>;
  return <div className="lg-ctx">
    <div className="dim small" style={{ marginBottom: 4 }}>{t("lg_ctx_lead", { h: d.host, f: d.source, m: Math.round((d.window_s ?? 1800) / 60) })}</div>
    {d.more_before && nb < 200 && <button className="linklike small lg-ctx-more" onClick={() => setNb(Math.min(200, nb + 50))}>▲ {t("lg_ctx_more")}</button>}
    {(d.before ?? []).map((l: any) => row(l))}
    {row({ ...anchor, match: false }, "anchor")}
    {(d.after ?? []).map((l: any) => row(l))}
    {d.more_after && na < 200 && <button className="linklike small lg-ctx-more" onClick={() => setNa(Math.min(200, na + 50))}>▼ {t("lg_ctx_more")}</button>}
    {!d.before?.length && !d.after?.length && <div className="dim small">{t("lg_ctx_none")}</div>}
  </div>;
}
