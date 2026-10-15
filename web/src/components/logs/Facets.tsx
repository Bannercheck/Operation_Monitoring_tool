import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Minus, RotateCcw, Search, X } from "lucide-react";
import { useT } from "../../i18n";
import { fmtN, type Range } from "../ops/util";
import { FACETS, LEVELS, LV_ALIAS, LV_COLOR, lg, rangeKey, rangeParams } from "./lib";

/** The filter sidebar: one group per facet field (level, service, host, env, pool, file) with counts, an in-group search box,
 *  "n more", and the reference's multi-select behaviour (a field's own clauses do not narrow its own counts). Checkbox: add /
 *  remove the value; click on the value: only this; Alt-click or "−": exclude (shown struck through). Every click rewrites the
 *  query text through the server, so the bar always shows what is applied. */
export type FacetOp = "add" | "remove" | "only" | "exclude";
const LABEL: Record<string, string> = { level: "lg_f_level", service: "lg_f_service", host: "lg_f_host", env: "lg_f_env", pool: "lg_f_pool", source: "lg_f_source" };

function useOpenState(): [Record<string, boolean>, (f: string) => void] {
  const [o, setO] = useState<Record<string, boolean>>(() => { try { return { level: true, service: true, host: true, env: true, ...JSON.parse(localStorage.getItem("wo_logs_facets") || "{}") }; } catch { return { level: true, service: true, host: true, env: true }; } });
  const flip = (f: string) => setO((x) => { const n = { ...x, [f]: !x[f] }; try { localStorage.setItem("wo_logs_facets", JSON.stringify(n)); } catch { /* ignore */ } return n; });
  return [o, flip];
}

export function Facets({ data, loading, q, range, onOp, onReset, onClose, note }: { data: any; loading: boolean; q: string; range: Range; onOp: (op: FacetOp, field: string, value: string) => void;
  onReset: () => void; onClose?: () => void; note?: string }) {
  const { t } = useT(); const [open, flip] = useOpenState();
  const facets = data?.facets ?? {};
  const nSel = FACETS.reduce((a, f) => a + (facets[f]?.selected?.length ?? 0) + (facets[f]?.excluded?.length ?? 0), 0);
  return <div className="lg-side-in">
    <div className="lg-side-h"><b>{t("lg_filters")}</b>{nSel > 0 && <span className="lg-nsel">{nSel}</span>}<span className="grow" />
      {nSel > 0 && <button className="icon-btn" title={t("lg_f_reset")} onClick={onReset}><RotateCcw size={14} /></button>}
      {onClose && <button className="icon-btn" onClick={onClose} aria-label="close"><X size={15} /></button>}</div>
    {data?.source === "history" && <div className="dim small" style={{ padding: "0 4px 6px" }}>{t("lg_f_history")}</div>}
    {data && data.source !== "history" && data.exact === false && <div className="dim small" style={{ padding: "0 4px 6px" }} title={t("lg_min_tip")}>
      {data.scaled ? t("lg_f_scaled") : (data.sample ?? 1) > 1 ? t("lg_f_sampled", { k: data.sample }) : t("lg_f_approx")}</div>}
    {note && <div className="dim small" style={{ padding: "0 4px 6px" }}>{note}</div>}
    {FACETS.map((f) => <FacetGroup key={f} field={f} label={t(LABEL[f])} fx={facets[f]} open={!!open[f]} flip={() => flip(f)} loading={loading && !data} q={q} range={range} onOp={onOp} />)}
  </div>;
}

function FacetGroup({ field, label, fx, open, flip, loading, q, range, onOp }: { field: string; label: string; fx: any; open: boolean; flip: () => void; loading: boolean; q: string; range: Range;
  onOp: (op: FacetOp, field: string, value: string) => void }) {
  const { t, lang } = useT();
  const [text, setText] = useState(""); const [dt, setDt] = useState(""); const [limit, setLimit] = useState(12);
  useEffect(() => { const h = setTimeout(() => setDt(text.trim()), 250); return () => clearTimeout(h); }, [text]);
  useEffect(() => { setLimit(12); }, [q, rangeKey(range)]);  // eslint-disable-line
  const extra = useQuery({ queryKey: ["lg-facet", field, q, rangeKey(range), dt, limit], enabled: open && (!!dt || limit > 12), staleTime: 15000, retry: false,
    queryFn: () => lg("/facets", { q, ...rangeParams(range), fields: field, limit: dt ? 50 : limit, ...(dt ? { search: `${field}:${dt}` } : {}) }) });
  const src = (dt || limit > 12) && extra.data ? extra.data.facets?.[field] : fx;
  const sel: string[] = (fx?.selected ?? []).map((x: string) => x.toLowerCase()); const exc: string[] = (fx?.excluded ?? []).map((x: string) => x.toLowerCase());
  let values: { v: string; n: number | null }[] = (src?.values ?? []).map((x: any) => ({ v: String(x.v), n: x.n }));
  if (field === "level" && !dt) { const by = Object.fromEntries(values.map((x) => [x.v.toUpperCase(), x.n])); values = LEVELS.map((l) => ({ v: l, n: by[l] ?? 0 })); }
  for (const x of fx?.excluded ?? []) if (!values.some((y) => y.v.toLowerCase() === String(x).toLowerCase())) values.push({ v: String(x), n: null });   // excluded values stay listed
  const distinct = src?.distinct ?? 0; const more = field !== "level" && !dt ? Math.max(0, distinct - (src?.values?.length ?? 0)) : 0;
  const nSel = (fx?.selected?.length ?? 0) + (fx?.excluded?.length ?? 0);
  const valOf = (v: string) => (field === "level" ? LV_ALIAS[v.toUpperCase()] ?? v : v);
  return <div className={`lg-facet ${open ? "open" : ""}`}>
    <button className="lg-fh" onClick={flip} aria-expanded={open}>{open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}<span>{label}</span>
      {nSel > 0 && <span className="lg-nsel">{nSel}</span>}<span className="grow" />{!open && distinct > 0 && <span className="dim small">{fmtN(distinct, lang)}</span>}</button>
    {open && <div className="lg-fb">
      {(distinct > 8 || text) && field !== "level" && <div className="lg-fsearch"><Search size={12} /><input value={text} onChange={(e) => setText(e.target.value)} placeholder={t("lg_f_search")} aria-label={`${label} ${t("lg_f_search")}`} />
        {text && <button className="icon-btn" onClick={() => setText("")}><X size={12} /></button>}</div>}
      {loading ? <div className="dim small lg-fnone">…</div> : !values.length ? <div className="dim small lg-fnone">{t("lg_f_none")}</div> :
        values.map(({ v, n }) => { const lv = v.toLowerCase(); const on = sel.includes(lv) || (field === "level" && sel.includes(lv)); const ex = exc.includes(lv);
          return <div key={v} className={`lg-fv ${on ? "on" : ""} ${ex ? "excl" : ""}`}>
            <input type="checkbox" checked={on} onChange={() => onOp(on || ex ? "remove" : "add", field, on || ex ? (field === "level" ? v.toUpperCase() : v) : valOf(v))} aria-label={v} />
            {field === "level" && <i className="lg-dot-l" style={{ background: LV_COLOR[v.toUpperCase()] ?? "var(--dim)" }} />}
            <button className="v" title={`${v} — ${t("lg_f_only")} · Alt: ${t("lg_f_exclude")}`} onClick={(e) => (e.altKey ? onOp("exclude", field, valOf(v)) : onOp("only", field, valOf(v)))}>
              {field === "level" ? t(`lg_lv_${v.toUpperCase()}`) : v}</button>
            <span className="n">{n == null ? "" : fmtN(n, lang)}</span>
            <button className="lg-fx" title={ex ? t("lg_f_unexclude") : t("lg_f_exclude")} onClick={() => onOp(ex ? "remove" : "exclude", field, ex ? v : valOf(v))}><Minus size={12} /></button>
          </div>; })}
      {more > 0 && <button className="linklike lg-fmore" onClick={() => setLimit((l) => l + 50)}>{t("lg_f_more", { n: fmtN(more, lang) })}</button>}
    </div>}
  </div>;
}
