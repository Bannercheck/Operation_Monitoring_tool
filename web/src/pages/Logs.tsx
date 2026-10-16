import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Archive, BellPlus, Bookmark, RefreshCw, SlidersHorizontal, ZoomIn, ZoomOut } from "lucide-react";
import { get } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { TimeRange } from "../components/ops/TimeRange";
import { HostDetail } from "../components/ops/HostDetail";
import { QueryBar, type QueryBarHandle } from "../components/logs/QueryBar";
import { Facets, type FacetOp } from "../components/logs/Facets";
import { Histogram, Legend } from "../components/logs/Histogram";
import { ColumnMenu, GroupTable, LogTable, useMedia } from "../components/logs/LogTable";
import { LineDetail, type FilterFn } from "../components/logs/LineDetail";
import { TailStatus } from "../components/logs/TailStatus";
import { AlertDialog, SaveDialog, SavedMenu } from "../components/logs/Dialogs";
import { errText, FACETS, fullN, lg, qs, rangeKey, rangeParams, roundEst, toErr, tsLocal, useLogsState, type LgErr } from "../components/logs/lib";
import type { Range } from "../components/ops/util";

/** Log explorer (docs/design/logs.md section 6): one query string drives the facet sidebar, the result count, the per-level
 *  histogram and the table; Live tail follows new lines as they arrive (pausing when you scroll or open a row); every row opens in
 *  place with its fields, context, raw line and JSON. Query, range, columns, grouping and the open line live in the URL. */
const PAGE = 200; const MAX_ROWS = 1000; const BUF_MAX = 2000; const COLD_AUTO = 40;
const retry5xx = (n: number, e: any) => (e?.status ?? 0) >= 500 && n < 2;

export default function Logs() {
  const { t, lang } = useT(); const { can, user } = useAuth();
  const st = useLogsState();
  const cols = useMemo(() => ["time", "level", ...st.cols.filter((c) => !["time", "level", "message"].includes(c)), "message"], [st.cols.join(",")]);  // eslint-disable-line
  const colsParam = cols.filter((c) => c === "agent" || c.startsWith("@")).join(",");
  const qbar = useRef<QueryBarHandle>(null); const tableRef = useRef<HTMLDivElement>(null);
  const [drawer, setDrawer] = useState(false); const [dialog, setDialog] = useState<"save" | "alert" | null>(null);
  const [toast, setToast] = useState<ReactNode>(null); const [hd, setHd] = useState<string | null>(null);
  const [focusId, setFocusId] = useState(""); const [openGroup, setOpenGroup] = useState("");
  const phone = useMedia("(max-width: 640px)");
  useEffect(() => { if (!toast) return; const h = setTimeout(() => setToast(null), 6000); return () => clearTimeout(h); }, [toast]);

  const fields = useQuery({ queryKey: ["lg-fields"], queryFn: () => lg("/fields"), staleTime: 60000, retry: false });
  const rel = "last" in st.range;
  const viewRange: Range = st.tail ? { last: 15 } : st.range;   // live tail: the histogram and facets show the last 15 minutes
  const hist = useQuery({ queryKey: ["lg-hist", st.q, rangeKey(viewRange), st.by], queryFn: () => lg("/histogram", { q: st.q, ...rangeParams(viewRange), by: st.by }),
    refetchInterval: st.tail ? 10000 : rel ? 30000 : false, placeholderData: keepPreviousData, retry: retry5xx });
  const facets = useQuery({ queryKey: ["lg-facets", st.q, rangeKey(viewRange)], queryFn: () => lg("/facets", { q: st.q, ...rangeParams(viewRange) }),
    refetchInterval: st.tail ? 15000 : rel ? 30000 : false, placeholderData: keepPreviousData, retry: retry5xx });

  // ---- search: the first page pins the range; later pages send it back with the keyset cursor, so paging never drifts
  const sKey = ["lg-search", st.q, rangeKey(st.range), colsParam];
  const search = useQuery({ queryKey: sKey, queryFn: () => lg("/search", { q: st.q, ...rangeParams(st.range), limit: PAGE, cols: colsParam }), enabled: !st.tail && !st.group, staleTime: Infinity, refetchOnMount: "always", retry: retry5xx });
  const [pages, setPages] = useState<any[]>([]); const [loadingMore, setLoadingMore] = useState(false); const [moreErr, setMoreErr] = useState<LgErr | null>(null); const gen = useRef(0);
  useEffect(() => { gen.current++; setPages([]); setMoreErr(null); setLoadingMore(false); }, [sKey.join("|"), search.dataUpdatedAt]);  // eslint-disable-line
  const first: any = search.data; const lastPage: any = pages.length ? pages[pages.length - 1] : first;
  const more = async () => {
    if (!first || !lastPage?.next || loadingMore) return;
    const g = gen.current; setLoadingMore(true); setMoreErr(null);
    try { const r = await lg("/search", { q: st.q, start: first.range.start, end: first.range.end, limit: PAGE, cursor: lastPage.next, cols: colsParam }); if (g === gen.current) setPages((p) => [...p, r]); }
    catch (e) { if (g === gen.current) setMoreErr(toErr(e)); } finally { if (g === gen.current) setLoadingMore(false); }
  };
  const searchRows = useMemo(() => { const seen = new Set<string>(); const out: any[] = [];
    for (const p of [first, ...pages]) for (const l of p?.lines ?? []) if (!seen.has(l.id)) { seen.add(l.id); out.push(l); } return out; }, [first, pages]);
  const droppedTop = Math.max(0, searchRows.length - MAX_ROWS);
  const fullPage = (lastPage?.lines?.length ?? 0) >= PAGE;

  // ---- group similar
  const groups = useQuery({ queryKey: ["lg-groups", st.q, rangeKey(st.range)], queryFn: () => lg("/groups", { q: st.q, ...rangeParams(st.range), limit: 200 }), enabled: st.group, staleTime: Infinity, refetchOnMount: "always", retry: retry5xx });

  // ---- live tail: poll /logs/tail with the last cursor; paused (scrolled away, a row open, or Pause) buffers new lines
  const [tailRows, setTailRows] = useState<any[]>([]); const [tailInfo, setTailInfo] = useState<any>(null); const [tailErr, setTailErr] = useState<LgErr | null>(null);
  const [buf, setBuf] = useState<any[]>([]); const [fresh, setFresh] = useState<Set<string>>(new Set()); const [userPause, setUserPause] = useState(false); const [scrolled, setScrolled] = useState(false);
  const paused = st.tail && (userPause || scrolled || !!st.line);
  const pausedRef = useRef(paused); pausedRef.current = paused;
  useEffect(() => {
    if (!st.tail) return;
    let stop = false; let timer: any; let after: number | undefined;
    setTailRows([]); setBuf([]); setTailInfo(null); setTailErr(null); setScrolled(false);
    const poll = async () => {
      if (stop) return;
      if (document.visibilityState === "hidden") { timer = setTimeout(poll, 1500); return; }
      try {
        const r = await lg("/tail", { q: st.q, after, limit: after === undefined ? PAGE : 500, cols: colsParam });
        if (stop) return;
        after = r.after; setTailInfo(r); setTailErr(null);
        const ls: any[] = r.lines ?? [];
        if (ls.length) {
          if (pausedRef.current) setBuf((b) => [...ls, ...b].slice(0, BUF_MAX));
          else { setTailRows((rows) => [...ls, ...rows].slice(0, MAX_ROWS)); setFresh(new Set(ls.map((l) => l.id))); }
        }
        timer = setTimeout(poll, Math.max(1000, r.poll_ms || 2000));
      } catch (e) {
        if (stop) return; const er = toErr(e);
        if (er.status === 429) { timer = setTimeout(poll, Math.max(300, er.retry_ms ?? 1000)); return; }
        setTailErr(er); timer = setTimeout(poll, er.status === 400 ? 30000 : 5000);
      }
    };
    poll();
    return () => { stop = true; clearTimeout(timer); };
  }, [st.tail, st.q, colsParam]);
  const flush = () => { if (!buf.length) return; setTailRows((rows) => [...buf, ...rows].slice(0, MAX_ROWS)); setFresh(new Set(buf.slice(0, 300).map((l) => l.id))); setBuf([]); };
  useEffect(() => { if (st.tail && !paused && buf.length) flush(); }, [paused]);  // eslint-disable-line
  const backToTop = () => { tableRef.current?.scrollTo({ top: 0 }); setScrolled(false); setUserPause(false); if (st.line) st.setLine(null); flush(); };

  // ---- query rewriting (facet clicks, filter / exclude on any value): one implementation on the server
  const rewrite = async (op: FacetOp, field: string, value: string) => {
    try { const r = await lg("/rewrite", { q: st.q, op, field, value }); st.setQ(r.q); } catch (e) { setToast(<span className="bad">{errText(toErr(e), lang)}</span>); }
  };
  const onFilter: FilterFn = (op, field, value) => rewrite(op, field, value);
  const onGroupKey = async (key: string) => { try { const r = await lg("/rewrite", { q: st.q, op: "add", field: "group", value: key }); st.set({ q: r.q, group: null, line: null }, true); } catch (e) { setToast(errText(toErr(e), lang)); } };
  const resetFacets = async () => {
    let q = st.q;
    for (const f of FACETS) { const fx = facets.data?.facets?.[f]; for (const v of [...(fx?.selected ?? []), ...(fx?.excluded ?? [])]) { try { q = (await lg("/rewrite", { q, op: "remove", field: f, value: v })).q; } catch { /* keep going */ } } }
    st.setQ(q);
  };
  const nSel = FACETS.reduce((a, f) => a + (facets.data?.facets?.[f]?.selected?.length ?? 0) + (facets.data?.facets?.[f]?.excluded?.length ?? 0), 0);

  // ---- zoom: absolute ranges down to one second; leaving live tail
  const bounds = (): [number, number] => { const r = (st.tail ? hist.data?.range : first?.range ?? hist.data?.range); if (r) return [new Date(r.start).getTime(), new Date(r.end).getTime()];
    return "last" in st.range ? [Date.now() - st.range.last * 60000, Date.now()] : [new Date(st.range.start).getTime(), new Date(st.range.end).getTime()]; };
  const zoom = (f: number) => { const [s, e] = bounds(); const c = (s + e) / 2; const w = Math.max(1000, (e - s) * f); let ns = c - w / 2, ne = c + w / 2; if (ne > Date.now()) { ne = Date.now(); ns = ne - w; } st.setAbs(new Date(ns), new Date(ne)); };

  // ---- rows of the current mode, keyboard
  const rows = st.tail ? tailRows : searchRows.slice(droppedTop);
  const ids = st.group ? [] : rows.map((r) => r.id);
  const toggleLine = (id: string) => st.setLine(st.line === id ? null : id);
  // keep the focused row in view by scrolling the table only (scrollIntoView would also move the page and the clipped card)
  const reveal = (id: string) => { const root = tableRef.current; const el = root?.querySelector(`[data-id="${id}"]`) as HTMLElement | null; if (!root || !el) return;
    const head = 34, top = el.offsetTop, h = (el.firstElementChild as HTMLElement | null)?.offsetHeight ?? 29;
    if (top - head < root.scrollTop) root.scrollTop = top - head; else if (top + h > root.scrollTop + root.clientHeight) root.scrollTop = top + h - root.clientHeight; };
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (dialog || hd) return;
      const el = e.target as HTMLElement | null; const typing = !!el && (["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName) || el.isContentEditable);
      if (e.key === "/" && !typing) { e.preventDefault(); qbar.current?.focus(); return; }
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "Escape") { if (st.line) { e.preventDefault(); st.setLine(null); } return; }
      if ((e.key === "ArrowDown" || e.key === "ArrowUp") && ids.length) {
        e.preventDefault(); const i = ids.indexOf(focusId); const j = i < 0 ? 0 : e.key === "ArrowDown" ? Math.min(ids.length - 1, i + 1) : Math.max(0, i - 1);
        setFocusId(ids[j]); requestAnimationFrame(() => reveal(ids[j])); return;
      }
      if ((e.key === "Enter" || e.key === " ") && el?.tagName !== "BUTTON" && el?.tagName !== "A" && focusId && ids.includes(focusId)) { e.preventDefault(); toggleLine(focusId); }
    };
    window.addEventListener("keydown", h); return () => window.removeEventListener("keydown", h);
  });

  // infinite scroll: a full last page loads the next one when its end comes into view (a partial scan waits for "scan further")
  const sentinel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = sentinel.current; const root = tableRef.current; if (!el || !root || st.tail || st.group) return;
    const io = new IntersectionObserver(([en]) => { if (en.isIntersecting && fullPage && lastPage?.next && searchRows.length < MAX_ROWS + 2 * PAGE) more(); }, { root, rootMargin: "300px" });
    io.observe(el); return () => io.disconnect();
  });

  // ---- results line
  const err: LgErr | null = st.tail ? (tailErr?.status === 400 ? tailErr : null) : st.group ? (groups.error ? toErr(groups.error) : null) : search.error ? toErr(search.error) : null;
  const qErr = err && err.status === 400 ? err : hist.error && toErr(hist.error).status === 400 ? toErr(hist.error) : null;
  const warnings = (st.tail ? tailInfo?.query?.warnings : first?.query?.warnings) ?? [];
  const tot = first?.total; const histSum = (hist.data?.buckets ?? []).reduce((a: number, b: any) => a + (b.total ?? 0), 0);
  // the histogram of the same pinned range counted every line (a complete scan or the counters) while the list's first page stopped
  // counting early: show that exact number rather than an estimate that disagrees with the bars under it
  const histExact = !!hist.data?.exact && (hist.data?.sample ?? 1) === 1 && !!first?.range && hist.data?.range?.start === first.range.start && hist.data?.range?.end === first.range.end;
  const count = (() => {
    if (st.tail) return { text: t(hist.data?.exact === false ? "lg_results_est" : "lg_results", { n: fullN(hist.data?.exact === false ? roundEst(histSum) : histSum, lang) }), flag: t("lg_last15"), tip: "" };
    if (st.group) { const g = groups.data; if (!g) return null;
      const est = (g.sample ?? 1) > 1 || !!g.scaled;
      const text = g.exact ? t("lg_results", { n: fullN(g.total, lang) }) : est ? t("lg_results_est", { n: fullN(roundEst(g.total), lang) }) : t("lg_results_min", { n: fullN(g.total, lang) });
      return { text, flag: t("lg_patterns", { n: fullN(g.distinct, lang) }), tip: g.exact ? t("lg_exact_tip") : g.scaled ? t("lg_f_scaled") : (g.sample ?? 1) > 1 ? t("lg_sample", { k: g.sample }) : t("lg_min_tip") }; }
    if (!tot) return null;
    if (tot.source === "history") return { text: t("lg_results", { n: "–" }), flag: "", tip: "" };
    if (tot.exact) return { text: t("lg_results", { n: fullN(tot.value, lang) }), flag: t("lg_exact"), tip: tot.source === "counters" ? t("lg_exact_tip") : t("lg_exact_scan_tip") };
    if (histExact && !tot.below_cursor) return { text: t("lg_results", { n: fullN(histSum, lang) }), flag: t("lg_exact"), tip: t("lg_exact_scan_tip") };
    if (tot.estimated) return { text: t("lg_results_est", { n: fullN(roundEst(tot.value), lang) }), flag: t("lg_estimate"), tip: t("lg_est_tip", { s: fullN(tot.basis?.scanned, lang), m: fullN(tot.basis?.matched, lang), r: fullN(tot.basis?.range_lines, lang) }) };
    return { text: t("lg_results_min", { n: fullN(tot.value, lang) }), flag: t("lg_at_least"), tip: t("lg_min_tip") };
  })();
  // "n new lines · Refresh": the search list is a snapshot; buckets after its pinned end tell how much arrived since
  const newer = !st.tail && !st.group && rel && first?.range ? (hist.data?.buckets ?? []).filter((b: any) => b.t * 1000 >= new Date(first.range.end).getTime()).reduce((a: number, b: any) => a + b.total, 0) : 0;
  const refresh = () => { tableRef.current?.scrollTo({ top: 0 }); if (st.group) groups.refetch(); else search.refetch(); hist.refetch(); facets.refetch(); };
  const scan = lastPage?.scan; const incomplete = !st.tail && !st.group && scan && scan.complete === false && scan.stopped_by !== "limit" && !!lastPage?.next;
  const scope = user && user.all === false ? <ScopeChip /> : null;
  const busy = err?.status === 503 || (hist.error && toErr(hist.error).status === 503);

  // older than the hot window: lines come from the cold archive (logtiers); "expired" only past the archive's oldest day
  const coldRet = first?.retention?.cold;
  const retMsg = coldRet?.horizon ? t("lg_retention_cold", { d: tsLocal(coldRet.horizon, { date: true, ms: false }) }) : t("lg_retention", { h: Math.round(first?.retention?.hours ?? 48) });
  const fromCold = !st.tail && (st.group ? groups.data?.source === "cold" : pages.some((p: any) => p?.source === "cold") || first?.source === "cold");
  // a scan of the cold archive goes on by itself while the page is not full (each request keeps its 1.5 s budget): a progress bar
  // instead of a hundred "Scan further back" clicks; at most COLD_AUTO requests per search, the button stops it
  const [auto, setAuto] = useState(0);
  useEffect(() => { setAuto(0); }, [first]);
  useEffect(() => {
    if (fromCold && incomplete && !loadingMore && !moreErr && auto < COLD_AUTO && searchRows.length < PAGE) { setAuto((n) => n + 1); more(); }
  }, [fromCold, incomplete, loadingMore, moreErr, auto, searchRows.length]);   // eslint-disable-line react-hooks/exhaustive-deps
  const autoOn = fromCold && incomplete && auto > 0 && auto < COLD_AUTO && searchRows.length < PAGE;
  // the synchronous scan stops after COLD_AUTO requests of 1.5 s: months of archive are an audit search in the background (System › Depolama)
  const longHint = fromCold && incomplete && auto >= COLD_AUTO && first?.range ? <div className="small dim" style={{ marginTop: 6 }}>{t("lg_cold_long", { n: COLD_AUTO })}{" "}
    {can("sys.maint") ? <Link to={`/system?audit_q=${encodeURIComponent(st.q ?? "")}&audit_from=${String(first.range.start).slice(0, 10)}&audit_to=${String(first.range.end).slice(0, 10)}`}>{t("lg_cold_bg")}</Link>
      : <span>{t("lg_cold_bg")}</span>}</div> : null;
  const emptyMsg = (() => {
    if (st.tail) return tailInfo ? t("lg_tail_wait") : t("loading");
    if (st.group) return groups.isLoading ? t("loading") : groups.error ? errText(toErr(groups.error), lang) : groups.data?.source === "history" ? retMsg : t("lg_empty");
    if (search.isLoading) return t("loading");
    if (err) return err.status === 400 ? t("lg_fix_query") : errText(err, lang);
    if (first?.source === "history") return retMsg;
    if (incomplete) return t("lg_empty_partial");
    return t("lg_empty");
  })();
  const empty = <div className="lg-empty">{emptyMsg}{incomplete && !rows.length && <> {fromCold && <ColdProgress range={first.range} frontier={scan.frontier} />}
    {autoOn ? <button className="linklike" onClick={() => setAuto(COLD_AUTO)}>{t("lg_cold_stop")}</button>
      : <button className="linklike" onClick={more} disabled={loadingMore}>{loadingMore && fromCold ? t("lg_cold_scanning") : t("lg_scan_more")}</button>}</>}{longHint}</div>;
  const bottom = st.tail || !rows.length ? (st.tail && rows.length >= MAX_ROWS ? <div className="lg-endnote dim small">{t("lg_cap_note", { n: fullN(MAX_ROWS, lang) })}</div> : null) : <div className="lg-endnote">
    <div ref={sentinel} />
    {loadingMore ? <span className="dim small">{t("loading")}</span> : moreErr ? <span className="bad small">{errText(moreErr, lang)}</span> :
      lastPage?.next ? (incomplete && !fullPage ? <span className="small dim">{t("lg_scanned", { from: tsLocal(scan.frontier), to: tsLocal(first.range.end), n: fullN(scan.lines, lang) })} · <button className="linklike" onClick={more}>{t("lg_scan_more")}</button></span>
        : <button className="btn sm" onClick={more}>{t("lg_older")}</button>) : <span className="dim small">{t("lg_end")}</span>}
    {longHint}
  </div>;
  const top = !st.tail && droppedTop > 0 ? <div className="lg-endnote dim small">{t("lg_dropped_top", { n: fullN(droppedTop, lang) })} · <button className="linklike" onClick={refresh}>{t("lg_back_newest")}</button></div> : null;
  const pill = st.tail && paused && buf.length > 0 ? <button className="lg-pill" onClick={backToTop}>▲ {t("lg_tail_new", { n: fullN(buf.length, lang) })}</button> : null;
  const standalone = st.line && !rows.some((r) => r.id === st.line) && !st.group;

  return <div className="lg">
    <div className="lg-top">
      <h2>{t("lg_title")}</h2>
      <span className="grow" />
      <TimeRange range={st.range} live={rel} setLast={st.setLast} setAbs={st.setAbs} />
    </div>
    <QueryBar ref={qbar} value={st.q} onRun={(v) => (v === st.q ? refresh() : st.setQ(v))} error={qErr} warnings={warnings} range={viewRange} fields={fields.data?.fields ?? []}
      onEscape={() => { if (st.line) { st.setLine(null); return true; } return false; }}>
      <div className="lg-qbtns">
        <button className={`btn lg-live ${st.tail ? "on" : ""}`} onClick={() => st.setTail(!st.tail)} title={t("lg_live_tip")} aria-pressed={st.tail}><i className="lg-live-dot" /><span>{t("lg_live")}</span></button>
        <button className="btn" onClick={() => setDialog("save")} title={t("lg_save")} aria-label={t("lg_save")}><Bookmark size={15} /><span className="lg-bl">{t("lg_save")}</span></button>
        {can("act.logalert") && <button className="btn" onClick={() => setDialog("alert")} title={t("lg_alert")} aria-label={t("lg_alert")}><BellPlus size={15} /><span className="lg-bl">{t("lg_alert")}</span></button>}
        <SavedMenu onOpen={(s) => { const p: Record<string, string | null> = { q: s.query || null, group: s.group_similar ? "1" : null, live: null, line: null };
          if (s.range) { const r = new URLSearchParams(s.range); p.last = r.get("last"); p.start = r.get("start"); p.end = r.get("end"); }
          if (s.columns?.length) p.cols = s.columns.join(","); st.set(p, true); setToast(t("lg_opened", { n: s.name })); }} />
      </div>
    </QueryBar>

    <div className="lg-page">
      <aside className={`lg-side card ${drawer ? "open" : ""}`} aria-label={t("lg_filters")}>
        <Facets data={facets.data} loading={facets.isLoading} q={st.q} range={viewRange} onOp={(op, f, v) => { rewrite(op, f, v); }} onReset={resetFacets} onClose={drawer ? () => setDrawer(false) : undefined} note={qErr ? t("lg_fix_short") : undefined} />
      </aside>
      {drawer && <div className="lg-veil" onClick={() => setDrawer(false)} />}
      <div className="lg-main">
        <div className="card lg-res">
          <div className="lg-head">
            <button className="btn sm lg-fbtn" onClick={() => setDrawer(true)}><SlidersHorizontal size={13} /> {t("lg_filters")}{nSel ? ` (${nSel})` : ""}</button>
            {count ? <span className="lg-count" title={count.tip}>{count.text}</span> : <span className="lg-count dim">…</span>}
            {count?.flag && <span className="chip lg-flag" title={count.tip}>{count.flag}</span>}
            {fromCold && <span className="chip lg-cold" title={t("lg_cold_tip")}><Archive size={11} />{t("lg_cold")}</span>}
            {scope}
            <span className="grow" />
            {!st.tail && rel && <button className={`btn sm ${newer > 0 ? "lg-new" : ""}`} onClick={refresh} title={t("lg_refresh")}><RefreshCw size={12} />{newer > 0 ? <span>{t("lg_refresh_new", { n: fullN(newer, lang) })}</span> : <span className="lg-bl">{t("lg_refresh")}</span>}</button>}
            <label className="lg-by"><span className="lg-bl dim small">{t("lg_breakdown")}</span>
              <select className="input sm" value={st.by} onChange={(e) => st.setBy(e.target.value)} aria-label={t("lg_breakdown")}>
                {[["level", "lg_f_level"], ["service", "lg_f_service"], ["host", "lg_f_host"], ["source", "lg_f_source"], ["pool", "lg_f_pool"]].map(([k, l]) => <option key={k} value={k}>{t(l)}</option>)}</select></label>
            <button className="btn sm" onClick={() => zoom(2)} title={t("lg_zoom_out")}><ZoomOut size={13} /><span className="lg-bl">{t("lg_zoom_out")}</span></button>
            <button className="btn sm" onClick={() => zoom(0.5)} title={t("lg_zoom_in")}><ZoomIn size={13} /><span className="lg-bl">{t("lg_zoom_in")}</span></button>
          </div>
          <Histogram data={hist.data} onZoom={(s, e) => st.setAbs(s, e)} height={phone ? 90 : 120} note={qErr ? t("lg_fix_short") : undefined} />
          <div className="lg-histfoot"><Legend data={hist.data} /><span className="grow" />
            {hist.data?.sample > 1 && <span className="dim small">{t("lg_sample", { k: hist.data.sample })}</span>}
            {hist.data?.unsupported?.length > 0 && <span className="small" style={{ color: "var(--amber)" }}>{t("lg_hist_unsupported", { l: hist.data.unsupported.join(", ") })}</span>}
            {hist.data?.fc_over && <span className="small" style={{ color: "var(--amber)" }}>{t("lg_fc_over")}</span>}
            {busy && <span className="small" style={{ color: "var(--amber)" }}>{t("lg_busy")}</span>}
            {!phone && <span className="dim small">{t("lg_drag")}</span>}</div>
          {st.tail && <TailStatus info={tailInfo} paused={paused} buffered={buf.length} onPause={() => setUserPause(true)} onResume={backToTop} err={tailErr && tailErr.status !== 400 ? tailErr : null} />}
          {incomplete && rows.length > 0 && <div className="lg-scan">{t("lg_scanned", { from: tsLocal(scan.frontier), to: tsLocal(first.range.end), n: fullN(scan.lines, lang) })}
            {fromCold && <ColdProgress range={first.range} frontier={scan.frontier} />}
            {autoOn ? <button className="linklike" onClick={() => setAuto(COLD_AUTO)}>{t("lg_cold_stop")}</button>
              : <button className="linklike" onClick={more} disabled={loadingMore}>{loadingMore && fromCold ? t("lg_cold_scanning") : t("lg_scan_more")}</button>}</div>}
        </div>

        {standalone && <div className="card lg-standalone"><div className="row between"><b className="small">{t("lg_linked_line", { id: st.line })}</b><button className="btn sm ghost" onClick={() => st.setLine(null)}>✕</button></div>
          <LineDetail id={st.line} q={st.q} onFilter={onFilter} onGroup={onGroupKey} onHost={setHd} /></div>}

        {st.group ? <GroupTable data={groups.data} openKey={openGroup} setOpenKey={setOpenGroup} onGroupToggle={(v) => st.setGroup(v)} onGroupKey={onGroupKey} onFilter={onFilter} empty={empty} /> :
          <LogTable ref={tableRef} rows={rows} cols={cols} openId={st.line} focusId={focusId} fresh={st.tail ? fresh : new Set()} q={st.q} group={false} onGroupToggle={(v) => st.setGroup(v)}
            onToggle={toggleLine} onFocus={setFocusId} onFilter={onFilter} onGroupKey={onGroupKey} onHost={setHd} empty={empty} top={top} bottom={bottom} pill={pill}
            onScroll={(y) => { if (st.tail) setScrolled(y > 40); }} colMenu={<ColumnMenu cols={cols} setCols={st.setCols} fields={fields.data?.fields ?? []} />} />}
      </div>
    </div>

    {dialog === "save" && <SaveDialog q={st.q} range={st.range} cols={cols} group={st.group} onClose={() => setDialog(null)} onSaved={(n) => { setDialog(null); setToast(t("lg_saved_ok", { n })); }} />}
    {dialog === "alert" && <AlertDialog q={st.q} onClose={() => setDialog(null)} onSaved={() => { setDialog(null); setToast(<span>{t("lg_alert_saved")} <Link to="/alerts">{t("lg_alert_rules")} →</Link></span>); }} />}
    {hd && <HostDetail host={hd} base={qs(rangeParams(viewRange)).slice(1)} span={Math.max(1, Math.round(((): number => { const [s, e] = bounds(); return (e - s) / 60000; })()))} focused onClose={() => setHd(null)}
      onFocus={() => { /* the explorer filters instead */ }} onLogs={() => { const h = hd; setHd(null); if (h) onFilter("add", "host", h); }}
      onZoom={(x, stepMin) => { setHd(null); st.setAbs(new Date(x * 1000), new Date((x + stepMin * 60) * 1000)); }} />}
    {toast && <div className="lg-toast card" role="status">{toast}</div>}
  </div>;
}

/** a team member sees only the team's hosts: say so next to the count */
function ScopeChip() {
  const { t } = useT(); const { user } = useAuth();
  const q = useQuery({ queryKey: ["scope-mine"], queryFn: () => get("/teams/mine"), staleTime: 60000, enabled: !!user && user.all === false });
  const ms = user?.teams ?? [];
  return <span className="chip lg-scope" title={t("tm_scope_hint")}>{t("lg_scope", { t: ms.map((m) => m.name).join(", ") || "–", n: q.data?.hosts ?? Math.max(0, ...ms.map((m) => m.hosts)) })}</span>;
}

/** How far back a scan over the cold archive got: the part of the pinned range from its end down to the scan frontier. */
function ColdProgress({ range, frontier }: { range: { start: string; end: string }; frontier: string }) {
  const { t } = useT();
  const s = new Date(range.start).getTime(), e = new Date(range.end).getTime(), f = new Date(frontier).getTime();
  const pct = e > s && Number.isFinite(f) ? Math.max(0, Math.min(100, Math.round((100 * (e - f)) / (e - s)))) : 0;
  return <span className="lg-coldbar" title={t("lg_cold_progress", { p: pct })} role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}><i style={{ width: `${pct}%` }} /></span>;
}
