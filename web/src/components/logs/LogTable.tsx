import { forwardRef, memo, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ArrowDown, ArrowUp, ChevronDown, ChevronRight, Clock, Search, Settings2 } from "lucide-react";
import { useT } from "../../i18n";
import { Spark } from "../ui";
import { fmtN } from "../ops/util";
import { fmtSkew, LV_COLOR, OPTIONAL_COLS, tsLocal, tsUtc } from "./lib";
import { LineDetail, type FilterFn } from "./LineDetail";

/** The result table: Timestamp (local, ms), Level (coloured), Service, Host, chosen extra columns and Message (monospace, one
 *  line), with the "Group similar events" toggle in the Message header and the column chooser behind the gear. Rows are keyed by
 *  line id; a click (or Enter / Space on the focused row) expands the row in place. The list scrolls inside the card so the
 *  header stays put; the parent loads older pages when the end comes into view. */
const W: Record<string, number> = { time: 150, level: 62, service: 150, host: 136, source: 128, env: 84, pool: 120, agent: 130, trace: 132, session: 150 };
const colW = (c: string) => W[c] ?? 112;

export function useMedia(q: string): boolean {
  const [m, setM] = useState(() => typeof window !== "undefined" && window.matchMedia(q).matches);
  useEffect(() => { const mq = window.matchMedia(q); const h = () => setM(mq.matches); mq.addEventListener("change", h); return () => mq.removeEventListener("change", h); }, [q]);
  return m;
}

export function Switch({ on, onChange, label }: { on: boolean; onChange: (v: boolean) => void; label: string }) {
  return <button className={`lg-switch ${on ? "on" : ""}`} role="switch" aria-checked={on} onClick={(e) => { e.stopPropagation(); onChange(!on); }}><span className="lg-sw-l">{label}</span><i><b /></i></button>;
}

type TableProps = { rows: any[]; cols: string[]; openId: string; focusId: string; fresh: Set<string>; q: string; group: boolean; onGroupToggle: (v: boolean) => void;
  onToggle: (id: string) => void; onFocus: (id: string) => void; onFilter: FilterFn; onGroupKey: (k: string) => void; onHost: (h: string) => void;
  colMenu: ReactNode; top?: ReactNode; bottom?: ReactNode; empty?: ReactNode; onScroll?: (top: number) => void; pill?: ReactNode };

export const LogTable = forwardRef<HTMLDivElement, TableProps>(function LogTable({ rows, cols, openId, focusId, fresh, q, group, onGroupToggle, onToggle, onFocus, onFilter, onGroupKey, onHost,
  colMenu, top, bottom, empty, onScroll, pill }, ref) {
  const { t } = useT(); const phone = useMedia("(max-width: 640px)");
  const shown = phone ? ["time", "level"] : cols.filter((c) => c !== "message");
  const tpl = phone ? "14px 64px 10px minmax(0, 1fr)" : `16px ${shown.map((c) => `${colW(c)}px`).join(" ")} minmax(280px, 1fr)`;
  const head = (c: string) => c === "time" ? t(phone ? "lg_c_time_short" : "lg_c_time") : c.startsWith("@") ? c : t(`lg_c_${c}`);
  const act = useRef<(id: string) => void>(() => undefined);
  act.current = (id: string) => { onFocus(id); onToggle(id); };
  const shownKey = shown.join(",");
  return <div className="lg-tcard card">
    {pill}
    <div className="lg-table" ref={ref} onScroll={(e) => onScroll?.((e.target as HTMLDivElement).scrollTop)} role="grid" aria-rowcount={rows.length}>
      <div className="lg-thead" style={{ gridTemplateColumns: tpl }} role="row">
        <span />{shown.map((c) => <span key={c} className={c === "time" ? "" : "c-x"}>{phone && c === "level" ? "" : head(c)}</span>)}
        <span className="lg-th-msg">{t("lg_c_message")}<span className="grow" /><Switch on={group} onChange={onGroupToggle} label={phone ? t("lg_group_short") : t("lg_group")} />{colMenu}</span>
      </div>
      {top}
      {!rows.length ? empty : rows.map((l) => { const open = openId === l.id;
        return <LogRow key={l.id} l={l} shownKey={shownKey} tpl={tpl} phone={phone} open={open} fresh={fresh.has(l.id)} focus={focusId === l.id} act={act}>
          {open ? <LineDetail id={l.id} q={q} onFilter={onFilter} onGroup={onGroupKey} onHost={onHost} /> : null}</LogRow>; })}
      {bottom}
    </div>
  </div>;
});

/** One table row, memoised: a live-tail poll mounts its new rows and leaves the other 500-1,000 alone (re-rendering every row each
 *  poll blocked the page for 80-280 ms every 2 s). A row that stops being "fresh" keeps its class: its fade-in already ran. */
type RowProps = { l: any; shownKey: string; tpl: string; phone: boolean; open: boolean; fresh: boolean; focus: boolean; act: { current: (id: string) => void }; children: ReactNode };
const LogRow = memo(function LogRow({ l, shownKey, tpl, phone, open, fresh, focus, act, children }: RowProps) {
  const { t } = useT();
  const cell = (c: string) => {
    if (c === "time") return <span key={c} className="c-time mono" title={`${tsUtc(l.ts)}${l.skew_s != null ? ` · ${t("lg_skew_short", { d: fmtSkew(l.skew_s) })}` : ""}`}>{phone ? tsLocal(l.ts, { date: false, ms: false }) : tsLocal(l.ts)}{l.skew_s != null && <Clock size={11} className="lg-skew" />}</span>;
    if (c === "level") return phone ? <span key={c} className="lg-dot-l" style={{ background: LV_COLOR[l.level] ?? "var(--dim)" }} title={l.level} /> : <span key={c} className={`lg-lvl ${l.level}`}>{t(`lg_lv_${l.level}`)}</span>;
    const v = c.startsWith("@") || c === "agent" ? l.cols?.[c] : l[c];
    return <span key={c} className={`c-x ${c === "host" ? "mono" : ""}`} title={v == null ? "" : String(v)}>{v == null || v === "" ? <span className="dim">–</span> : String(v)}</span>;
  };
  return <div data-id={l.id} className={`lg-rw ${open ? "open" : ""}`}>
    <div className={`lg-row ${fresh ? "fresh" : ""} ${focus ? "focus" : ""}`} style={{ gridTemplateColumns: tpl }} role="row" aria-expanded={open} onClick={() => act.current(l.id)}>
      <span className="lg-chev">{open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}</span>
      {(shownKey ? shownKey.split(",") : []).map(cell)}
      <span className="c-msg mono">{l.message}</span>
    </div>
    {children}
  </div>;
}, (a, b) => a.l === b.l && a.open === b.open && a.focus === b.focus && a.tpl === b.tpl && a.shownKey === b.shownKey && a.phone === b.phone && a.act === b.act
  && a.children === b.children && (a.fresh === b.fresh || !b.fresh));

/** Group similar events: one row per message pattern and service, with count, hosts, trend and last seen; a row expands to the
 *  latest sample, the busiest hosts and "lines of this pattern" (adds group:<key> to the query). */
export function GroupTable({ data, openKey, setOpenKey, onGroupToggle, onGroupKey, onFilter, empty }: { data: any; openKey: string; setOpenKey: (k: string) => void; onGroupToggle: (v: boolean) => void;
  onGroupKey: (k: string) => void; onFilter: FilterFn; empty: ReactNode }) {
  const { t, lang } = useT(); const phone = useMedia("(max-width: 640px)");
  const tpl = phone ? "14px 10px minmax(0, 1fr) 64px" : "16px 62px minmax(200px, 1fr) 128px 70px 64px 84px 70px";
  const gs: any[] = data?.groups ?? [];
  return <div className="lg-tcard card"><div className="lg-table" role="grid">
    <div className="lg-thead" style={{ gridTemplateColumns: tpl }} role="row">
      <span />{!phone && <span>{t("lg_c_level")}</span>}{phone && <span />}
      <span className="lg-th-msg">{t("lg_c_template")}<span className="grow" /><Switch on onChange={onGroupToggle} label={phone ? t("lg_group_short") : t("lg_group")} /></span>
      {!phone && <><span className="c-x">{t("lg_c_service")}</span><span className="c-x right">{t("lg_c_count")}</span><span className="c-x right" title={t("lg_c_hosts")}>{t("lg_c_hosts_short")}</span><span className="c-x">{t("lg_trend")}</span><span className="c-x" title={t("lg_last_seen")}>{t("lg_c_last")}</span></>}
      {phone && <span className="c-x right">{t("lg_c_count")}</span>}
    </div>
    {!gs.length ? empty : gs.map((g) => { const open = openKey === g.key;
      return <div key={g.key} className={`lg-rw ${open ? "open" : ""}`}>
        <div className="lg-row" style={{ gridTemplateColumns: tpl }} onClick={() => setOpenKey(open ? "" : g.key)} role="row" aria-expanded={open}>
          <span className="lg-chev">{open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}</span>
          {phone ? <span className="lg-dot-l" style={{ background: LV_COLOR[g.level] ?? "var(--dim)" }} /> : <span className={`lg-lvl ${g.level}`}>{t(`lg_lv_${g.level}`)}</span>}
          <span className="c-msg mono">{g.template}{g.new && <span className="tag new" style={{ marginLeft: 8 }}>{t("lx_new")}</span>}</span>
          {!phone && <><span className="c-x" title={g.service}>{g.service}</span><span className="c-x right num">{fmtN(g.count, lang)}</span><span className="c-x right num">{g.hosts}</span>
            <span className="lg-spark"><Spark data={g.trend?.length > 1 ? g.trend : [g.trend?.[0] ?? 0, g.trend?.[0] ?? 0]} color={LV_COLOR[g.level] ?? "var(--accent2)"} /></span>
            <span className="c-x mono dim">{g.last ? tsLocal(g.last, { date: false, ms: false }) : "–"}</span></>}
          {phone && <span className="c-x right num">{fmtN(g.count, lang)}</span>}
        </div>
        {open && <div className="lg-exp" onClick={(e) => e.stopPropagation()}>
          <div className="dim small">{t("lx_latest")}{g.last ? ` · ${tsLocal(g.last)}` : ""} · {g.service}</div>
          <pre className="lx-pre lg-pre">{g.sample || g.template}</pre>
          {g.top_hosts?.length > 0 && <><div className="dim small" style={{ margin: "8px 0 4px" }}>{t("lx_where")} · {g.hosts}</div>
            <div className="row wrap" style={{ gap: 6 }}>{g.top_hosts.map((h: string) => <button key={h} className="chip" onClick={() => onFilter("add", "host", h)}>{h}</button>)}</div></>}
          <div className="row" style={{ marginTop: 10 }}><button className="btn sm primary" onClick={() => onGroupKey(g.key)}>{t("lg_same_group")}</button>
            {g.service && g.service !== "-" && <button className="btn sm" onClick={() => onFilter("add", "service", g.service)}>{t("lg_filter_by")}: {g.service}</button>}</div>
        </div>}
      </div>; })}
  </div></div>;
}

/** the gear: optional built-in columns and the attribute keys seen in the last hour, searchable, with up / down to order them */
export function ColumnMenu({ cols, setCols, fields }: { cols: string[]; setCols: (c: string[]) => void; fields: any[] }) {
  const { t } = useT(); const [open, setOpen] = useState(false); const [f, setF] = useState(""); const box = useRef<HTMLDivElement>(null);
  useEffect(() => { if (!open) return; const h = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); }; document.addEventListener("mousedown", h); return () => document.removeEventListener("mousedown", h); }, [open]);
  const extra = cols.filter((c) => !["time", "level", "message"].includes(c));
  const attrs = useMemo(() => fields.filter((x: any) => x.kind === "attr").map((x: any) => String(x.name)), [fields]);
  const toggle = (c: string) => { const has = cols.includes(c); const base = cols.filter((x) => x !== c && x !== "message"); setCols(has ? [...base, "message"] : [...base, c, "message"]); };
  const move = (c: string, d: number) => { const e = [...extra]; const i = e.indexOf(c); const j = i + d; if (j < 0 || j >= e.length) return; [e[i], e[j]] = [e[j], e[i]]; setCols(["time", "level", ...e, "message"]); };
  const pick = (c: string, label: string) => <label key={c} className="lg-colrow"><input type="checkbox" checked={cols.includes(c)} onChange={() => toggle(c)} /><span className={c.startsWith("@") ? "mono" : ""}>{label}</span></label>;
  return <span className="lg-colw" ref={box} onClick={(e) => e.stopPropagation()}>
    <button className="icon-btn" title={t("lg_columns")} aria-label={t("lg_columns")} onClick={() => setOpen(!open)}><Settings2 size={14} /></button>
    {open && <div className="lg-colpop card">
      <div className="lg-colsec">{t("lg_columns")}</div>
      {["time", "level"].map((c) => <label key={c} className="lg-colrow dis"><input type="checkbox" checked disabled /><span>{t(c === "time" ? "lg_c_time" : "lg_c_level")}</span></label>)}
      {OPTIONAL_COLS.map((c) => pick(c, t(`lg_c_${c}`)))}
      <label className="lg-colrow dis"><input type="checkbox" checked disabled /><span>{t("lg_c_message")}</span></label>
      {attrs.length > 0 && <><div className="lg-colsec">{t("lg_cols_attrs")}</div>
        <div className="lg-fsearch" style={{ margin: "2px 0 4px" }}><Search size={12} /><input value={f} onChange={(e) => setF(e.target.value)} placeholder={t("lg_f_search")} /></div>
        <div className="lg-colattrs">{attrs.filter((a) => a.toLowerCase().includes(f.toLowerCase())).slice(0, 60).map((a) => pick(a, a))}</div></>}
      {extra.length > 1 && <><div className="lg-colsec">{t("lg_col_order")}</div>
        {extra.map((c, i) => <div key={c} className="lg-colord"><span className={c.startsWith("@") ? "mono" : ""}>{c.startsWith("@") ? c : t(`lg_c_${c}`)}</span><span className="grow" />
          <button className="icon-btn" disabled={i === 0} onClick={() => move(c, -1)} aria-label="up"><ArrowUp size={12} /></button><button className="icon-btn" disabled={i === extra.length - 1} onClick={() => move(c, 1)} aria-label="down"><ArrowDown size={12} /></button></div>)}</>}
      <div className="lg-colfoot dim small">{t("lg_keys")}</div>
    </div>}
  </span>;
}
