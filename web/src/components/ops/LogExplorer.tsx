import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Pause, Play, Search } from "lucide-react";
import { get } from "../../api";
import { useT } from "../../i18n";
import { Empty, Spark } from "../ui";
import { TailModal } from "./Details";
import { fmtN, fmtTime } from "./util";

/** The logs of the chosen range and scope. "Grouped" folds identical lines (numbers, ids, IPs masked) into one row per message
 *  and service; "Raw" lists the lines newest first and pages further back. Both follow the same severity / text / service filter. */
export function LogExplorer({ rq, live, spanMin, services, focus, onHost }: { rq: string; live: boolean; spanMin: number; services: string[]; focus: any; onHost: (h: string) => void }) {
  const { t, lang } = useT();
  const [tab, setTab] = useState<"groups" | "raw" | "files">("groups"); const [tail, setTail] = useState<any>(null); const [sev, setSev] = useState("WARN"); const [q, setQ] = useState(""); const [qq, setQq] = useState(""); const [svc, setSvc] = useState("");
  const [open, setOpen] = useState<number | null>(null); const [paused, setPaused] = useState(false); const [pages, setPages] = useState<any[]>([]); const [show, setShow] = useState(25);
  useEffect(() => { const h = setTimeout(() => setQq(q), 350); return () => clearTimeout(h); }, [q]);
  useEffect(() => { if (focus) { setTab(focus.tab ?? "groups"); if (focus.sev) setSev(focus.sev); if (focus.q !== undefined) { setQ(focus.q); setQq(focus.q); } if (focus.service !== undefined) setSvc(focus.service); } }, [focus]);
  const f = `${rq}&severity=${sev}&q=${encodeURIComponent(qq)}&service=${encodeURIComponent(svc)}`;
  useEffect(() => { setPages([]); setShow(25); }, [f, tab]);
  const refresh = live && !paused ? 10000 : false;
  const g = useQuery({ queryKey: ["range-groups", f], queryFn: () => get(`/live/range/groups?${f}&limit=150`), enabled: tab === "groups", refetchInterval: refresh });
  const r = useQuery({ queryKey: ["range-logs", f], queryFn: () => get(`/live/range/logs?${f}&limit=200`), enabled: tab === "raw", refetchInterval: refresh });
  const scopeOnly = rq.split("&").filter((x) => /^(env|pool|host)=/.test(x)).join("&");
  const files = useQuery({ queryKey: ["live-files", scopeOnly, spanMin], queryFn: () => get(`/live/files?window=${Math.min(Math.max(spanMin, 5), 60)}${scopeOnly ? `&${scopeOnly}` : ""}`), enabled: tab === "files", refetchInterval: refresh });
  const more = async () => { const last = pages.length ? pages[pages.length - 1] : r.data; if (!last?.next) return; const nx = await get(`/live/range/logs?${f}&limit=200&before=${encodeURIComponent(last.next)}`); setPages([...pages, nx]); };
  const lines = [...(r.data?.lines ?? []), ...pages.flatMap((p) => p.lines)];
  const nextCursor = pages.length ? pages[pages.length - 1].next : r.data?.next;
  const rows: any[] = g.data?.groups ?? [];
  const src = tab === "groups" ? g.data : null;
  return <div>
    <div className="lx-bar">
      <div className="seg"><button className={tab === "groups" ? "on" : ""} onClick={() => setTab("groups")}>{t("lx_grouped")}</button><button className={tab === "raw" ? "on" : ""} onClick={() => setTab("raw")}>{t("lx_raw")}</button><button className={tab === "files" ? "on" : ""} onClick={() => setTab("files")}>{t("lx_files")}</button></div>
      <div className="seg">{[["ERROR", t("lx_err")], ["WARN", t("lx_warn")], ["INFO", t("lx_all")]].map(([k, l]) => <button key={k} className={sev === k ? "on" : ""} onClick={() => setSev(k)}>{l}</button>)}</div>
      <div className="lx-search"><Search size={14} /><input placeholder={t("lx_search")} value={q} onChange={(e) => setQ(e.target.value)} /></div>
      <select className="input sm" value={svc} onChange={(e) => setSvc(e.target.value)} style={{ maxWidth: 200 }}><option value="">{t("lx_all_services")}</option>{services.map((s) => <option key={s} value={s}>{s}</option>)}</select>
      {live && <button className="btn sm" onClick={() => setPaused(!paused)} title={t("lx_pause_tip")}>{paused ? <Play size={13} /> : <Pause size={13} />} {paused ? t("lx_resume") : t("lx_pause")}</button>}
      <span className="grow" />
      <span className="dim small">{tab === "groups" ? (g.data ? t("lx_fold", { n: fmtN(g.data.total, lang), g: fmtN(g.data.distinct, lang) }) : "") : r.data ? t("lx_lines", { n: lines.length }) : ""}
        {src?.sample > 1 ? ` · ${t("lx_sample", { k: src.sample })}` : ""}{src?.source === "history" ? ` · ${t("lx_hist_only")}` : ""}</span>
    </div>
    {tab === "files" ? (!(files.data ?? []).length ? <Empty>{files.isLoading ? t("loading") : t("lx_no_files")}</Empty> :
      <div className="table-wrap" style={{ maxHeight: 520 }}><table className="table"><thead><tr><th>{t("ds_files")}</th><th>{t("host")}</th><th>{t("ops_agents")}</th><th className="right">{t("ds_events")}</th><th className="right">ERROR+</th><th>{t("service")}</th><th>{t("message")}</th></tr></thead>
        <tbody>{(files.data ?? []).slice().sort((a: any, b: any) => b.errors - a.errors || b.total - a.total).map((x: any, i: number) => <tr key={i} className="clickable" onClick={() => setTail(x)}>
          <td><b className="mono small">{x.file}</b></td><td className="small">{x.host}</td><td className="small">{x.agent}</td><td className="num">{fmtN(x.total, lang)}</td><td className="num" style={{ color: x.errors ? "var(--red)" : undefined }}>{fmtN(x.errors, lang)}</td>
          <td className="small">{(x.services ?? []).slice(0, 3).join(", ")}</td><td className="mono small dim" style={{ maxWidth: 360, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{x.last_msg}</td></tr>)}</tbody></table>
        <div className="dim small" style={{ padding: 8 }}>{t("lx_files_note")}</div></div>) :
    tab === "groups" ? (!rows.length ? <Empty>{g.isLoading ? t("loading") : src?.source === "history" ? t("lx_hist_only_long") : t("lx_empty")}</Empty> :
      <><div className="lx-list">{rows.slice(0, show).map((x) => <div key={x.id} className={`lx-g ${open === x.id ? "open" : ""}`}>
        <div className="lx-gh" onClick={() => setOpen(open === x.id ? null : x.id)}>
          <span className={`sev-dot ${x.severity}`} title={x.severity} />
          <div className="lx-tpl"><span className="mono">{x.template}</span>{x.new && <span className="tag new">{t("lx_new")}</span>}</div>
          <span className="lx-svc small">{x.service}</span>
          <span className="lx-n"><b>{fmtN(x.count, lang)}</b></span>
          <span className="lx-h small">{x.host_count} {t("lx_hosts")}</span>
          <span className="lx-sp"><Spark data={x.trend?.length > 1 ? x.trend : [x.trend?.[0] ?? 0, x.trend?.[0] ?? 0]} color={x.severity === "ERROR" || x.severity === "CRITICAL" ? "var(--red)" : x.severity === "WARN" ? "var(--amber)" : "var(--accent2)"} /></span>
        </div>
        {open === x.id && <div className="lx-gd">
          <div className="small dim">{t("lx_latest")} · {fmtTime(x.last, spanMin + 2000)} · {x.ref}</div>
          <pre className="lx-pre">{x.sample}</pre>
          <div className="small dim" style={{ margin: "6px 0 4px" }}>{t("lx_where")}</div>
          <div className="row wrap" style={{ gap: 6 }}>{x.hosts.map((h: any) => <button key={h.host} className="chip" onClick={() => onHost(h.host)}>{h.host} · {fmtN(h.count, lang)}</button>)}</div>
          {x.pools?.length > 0 && <div className="small dim" style={{ marginTop: 6 }}>Pool: {x.pools.map((p: any) => `${p.pool} (${fmtN(p.count, lang)})`).join(" · ")}</div>}
          <button className="btn sm" style={{ marginTop: 8 }} onClick={() => { const words = x.sample.split(/\s+/).filter((w: string) => w.length > 3 && !/\d/.test(w)).slice(0, 3).join(" "); setTab("raw"); setSvc(x.service === "-" ? "" : x.service); setQ(words); setQq(words); }}>{t("lx_show_raw")}</button>
        </div>}
      </div>)}</div>
      {rows.length > show && <div style={{ textAlign: "center", padding: 10 }}><button className="btn sm" onClick={() => setShow(show + 50)}>{t("lx_more_groups", { n: rows.length - show })}</button></div>}</>) :
      (!lines.length ? <Empty>{r.isLoading ? t("loading") : t("lx_empty")}</Empty> :
        <div className="lx-raw">{lines.map((l: any, i: number) => <div key={i} className={`lx-l ${open === -i - 1 ? "open" : ""}`} onClick={() => setOpen(open === -i - 1 ? null : -i - 1)}>
          <span className="mono dim">{fmtTime(l.ts, spanMin + (spanMin > 1440 ? 0 : 0))}:{String(new Date(l.ts).getSeconds()).padStart(2, "0")}</span>
          <span className={`sev-dot ${l.severity}`} title={l.severity} />
          <span className="lx-host small" onClick={(e) => { e.stopPropagation(); onHost(l.host); }}>{l.host}</span>
          <span className="lx-svc small">{l.service}</span>
          <span className="mono lx-msg">{l.message}</span>
        </div>)}
          {nextCursor && <div style={{ textAlign: "center", padding: 10 }}><button className="btn sm" onClick={more}>{t("lx_older")}</button></div>}</div>)}
    {tail && <TailModal f={tail} onClose={() => setTail(null)} />}
  </div>;
}
