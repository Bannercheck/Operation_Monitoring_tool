import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowUpRight, Pause, Play } from "lucide-react";
import { useT } from "../../i18n";
import { Card, Empty } from "../ui";
import { LV_COLOR, lg, qv, tsLocal } from "../logs/lib";

const MAX_ROWS = 200, BUF_MAX = 500;

/** The bottom of the server detail: this server's lines as a live tail (the log explorer's /logs/tail, polled from the last line id,
 *  newest on top). Scrolling into the list or "Duraklat" pauses it; new lines then wait behind a "N yeni satır" pill. A line opens
 *  in the log explorer with its detail. */
export function HostTail({ host, query, title }: { host: string; query?: string; title?: string }) {
  const { t } = useT();
  const [lvl, setLvl] = useState<"all" | "warn" | "error">("all");
  const [rows, setRows] = useState<any[]>([]); const [buf, setBuf] = useState<any[]>([]);
  const [userPause, setUserPause] = useState(false); const [scrolled, setScrolled] = useState(false); const [err, setErr] = useState("");
  const paused = userPause || scrolled; const pausedRef = useRef(paused); pausedRef.current = paused;
  const listRef = useRef<HTMLDivElement>(null);
  const base = query ?? `host:${qv(host)}`;           // an application passes its app:<name> query (watchover.applications)
  const q = `${base}${lvl === "warn" ? " level>=warn" : lvl === "error" ? " level>=error" : ""}`;
  useEffect(() => {
    let stop = false; let timer: any; let after: number | undefined;
    setRows([]); setBuf([]); setErr("");
    const poll = async () => {
      if (stop) return;
      if (document.visibilityState === "hidden") { timer = setTimeout(poll, 1500); return; }
      try {
        const r: any = await lg("/tail", { q, after, limit: after === undefined ? 50 : 200, cols: "service" });
        if (stop) return;
        after = r.after; setErr("");
        const ls: any[] = r.lines ?? [];
        if (ls.length) {
          if (pausedRef.current) setBuf((b) => [...ls, ...b].slice(0, BUF_MAX));
          else setRows((x) => [...ls, ...x].slice(0, MAX_ROWS));
        }
        timer = setTimeout(poll, Math.max(1000, r.poll_ms || 2000));
      } catch (e: any) {
        if (stop) return;
        if (e?.status === 429) { timer = setTimeout(poll, Math.max(300, e.retry_ms ?? 1000)); return; }
        setErr(e?.tr || e?.message || String(e)); timer = setTimeout(poll, 5000);
      }
    };
    poll();
    return () => { stop = true; clearTimeout(timer); };
  }, [q]);
  const flush = () => { if (!buf.length) return; setRows((x) => [...buf, ...x].slice(0, MAX_ROWS)); setBuf([]); };
  useEffect(() => { if (!paused) flush(); }, [paused]);  // eslint-disable-line react-hooks/exhaustive-deps
  const top = () => { listRef.current?.scrollTo({ top: 0 }); setScrolled(false); setUserPause(false); };
  const explorer = `/logs?q=${encodeURIComponent(q)}&live=1`;
  const right = <div className="row" style={{ gap: 6 }}>
    <div className="seg">{(["all", "warn", "error"] as const).map((k) => <button key={k} className={lvl === k ? "on" : ""} onClick={() => setLvl(k)}>{t(`ht_${k}`)}</button>)}</div>
    <button className="btn sm" onClick={() => (paused ? top() : setUserPause(true))}>{paused ? <Play size={13} /> : <Pause size={13} />} {paused ? t("ht_resume") : t("ht_pause")}</button>
    <Link className="btn sm" to={explorer}>{t("lg_open_in_explorer")} <ArrowUpRight size={13} /></Link>
  </div>;
  return <Card title={<span>{title ?? t("ht_title")} <span className={`ht-state ${paused ? "off" : ""}`}>{paused ? t("ht_paused") : t("ht_live")}</span></span>} right={right}>
    {buf.length > 0 && <button className="ht-pill" onClick={top}>{t("ht_new", { n: buf.length >= BUF_MAX ? `${BUF_MAX}+` : buf.length })}</button>}
    {err ? <Empty>{err}</Empty> : !rows.length ? <Empty>{t("ht_wait")}</Empty> :
      <div ref={listRef} className="ht-list" onScroll={(e) => setScrolled((e.target as HTMLDivElement).scrollTop > 8)}>
        {rows.map((l) => <Link key={l.id} className="ht-row" to={`/logs?q=${encodeURIComponent(base)}&line=${l.id}`} title={l.message}>
          <span className="mono dim">{tsLocal(l.ts, { date: false })}</span>
          <span className="ht-lv" style={{ color: LV_COLOR[l.level] ?? "var(--muted)" }}>{l.level}</span>
          <span className="small ht-svc">{l.service || l.cols?.service || ""}</span>
          <span className="mono ht-msg">{l.message}</span>
        </Link>)}
      </div>}
  </Card>;
}
