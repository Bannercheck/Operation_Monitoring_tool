import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Filter, X } from "lucide-react";
import { useT } from "../../i18n";
import { errText, lg, qv, rangeParams, toErr, type LgErr } from "./lib";
import type { Range } from "../ops/util";

/** The query bar: a monospace input over a syntax-coloured mirror (fields, operators, quoted text, a red wavy underline on the
 *  span the parser refused), autocomplete of field names and of values (facet values from the server, level aliases), and one
 *  hint line for parse errors and warnings. Enter runs the query; the draft is checked with /logs/parse while typing. */
type Tok = { s: number; e: number; k: "ws" | "f" | "op" | "p" | "q" | "n" | "v" | "w" };
const OPS = new Set(["OR", "AND", "NOT", "VEYA", "VE", "DEĞİL", "||", "&&"]);
const FIELD_RE = /^([A-Za-z_@][\w.\-@]*)(:|>=|<=|>|<)/;
const BUILTIN = ["level", "service", "host", "env", "pool", "source", "agent", "kind", "origin", "trace", "session", "user", "client", "message", "raw", "line", "group", "duration_ms", "app"];
const LEVEL_VALUES = ["error", "warn", "info", "debug", "critical"];

export function tokenize(text: string): Tok[] {
  const out: Tok[] = []; let i = 0; const n = text.length;
  while (i < n) {
    const c = text[i];
    if (/\s/.test(c)) { let j = i; while (j < n && /\s/.test(text[j])) j++; out.push({ s: i, e: j, k: "ws" }); i = j; continue; }
    if (c === "(" || c === ")") { out.push({ s: i, e: i + 1, k: "p" }); i++; continue; }
    if (c === '"') { let j = i + 1; while (j < n && text[j] !== '"') j += text[j] === "\\" ? 2 : 1; j = Math.min(n, j + 1); out.push({ s: i, e: j, k: "q" }); i = j; continue; }
    let j = i; while (j < n && !/[\s()"]/.test(text[j])) j += text[j] === "\\" ? 2 : 1; j = Math.min(n, j);
    word(text, i, j, out); i = j;
  }
  return out;
}
function word(text: string, s: number, e: number, out: Tok[]) {
  const w = text.slice(s, e);
  if (OPS.has(w)) { out.push({ s, e, k: "op" }); return; }
  if (w.length > 1 && w[0] === "-" && (s === 0 || /[\s(]/.test(text[s - 1]))) { out.push({ s, e: s + 1, k: "n" }); word(text, s + 1, e, out); return; }
  const m = FIELD_RE.exec(w);
  if (m) { out.push({ s, e: s + m[0].length, k: "f" }); if (e > s + m[0].length) out.push({ s: s + m[0].length, e, k: "v" }); return; }
  out.push({ s, e, k: "w" });
}

/** the token under the caret, for autocomplete: its start, its text, a "-" prefix and, after "field:", the field and partial value */
function current(text: string, caret: number): { start: number; prefix: string; field: string | null; part: string; word: string } {
  let st = caret; while (st > 0 && !/[\s(]/.test(text[st - 1])) st--;
  let w = text.slice(st, caret); let prefix = "";
  if (w.startsWith("-")) { prefix = "-"; w = w.slice(1); }
  const m = /^([A-Za-z_@][\w.\-@]*):(.*)$/.exec(w);
  if (m) return { start: st, prefix, field: m[1], part: m[2].replace(/^["(]/, ""), word: w };
  return { start: st, prefix, field: null, part: w, word: w };
}

export type QueryBarHandle = { focus: () => void; blur: () => void; acOpen: () => boolean; closeAc: () => void };
type Sugg = { label: string; insert: string; kind: string; field?: boolean };

export const QueryBar = forwardRef<QueryBarHandle, { value: string; onRun: (q: string) => void; error: LgErr | null; warnings: any[]; range: Range; fields: any[];
  children?: ReactNode; onEscape?: () => boolean }>(function QueryBar({ value, onRun, error, warnings, range, fields, children, onEscape }, ref) {
  const { t, lang } = useT();
  const [draft, setDraft] = useState(value); const [caret, setCaret] = useState(0); const [scroll, setScroll] = useState(0);
  const [acOn, setAcOn] = useState(false); const [hi, setHi] = useState(-1); const [focused, setFocused] = useState(false);
  const inp = useRef<HTMLInputElement>(null);
  useEffect(() => { setDraft(value); }, [value]);
  useImperativeHandle(ref, () => ({ focus: () => { inp.current?.focus(); inp.current?.select(); }, blur: () => inp.current?.blur(), acOpen: () => acOn && sugg.length > 0, closeAc: () => setAcOn(false) }));

  // the draft is checked while typing (debounced); the committed query's own error comes from the search answer
  const [dq, setDq] = useState(draft);
  useEffect(() => { const h = setTimeout(() => setDq(draft), 300); return () => clearTimeout(h); }, [draft]);
  const parse = useQuery({ queryKey: ["lg-parse", dq], queryFn: () => lg("/parse", { q: dq }), enabled: dq !== value && dq.trim() !== "", retry: false, staleTime: 60000 });
  const dirty = draft !== value;
  const perr: LgErr | null = dirty ? (parse.error ? toErr(parse.error) : null) : error;
  const warn: any[] = dirty ? (parse.data?.warnings ?? []) : warnings;

  // autocomplete
  const cur = useMemo(() => current(draft, caret), [draft, caret]);
  const facetField = cur.field && ["service", "svc", "host", "hostname", "env", "environment", "pool", "source", "file"].includes(cur.field) ? cur.field : null;
  const [vpart, setVpart] = useState("");
  useEffect(() => { const h = setTimeout(() => setVpart(cur.part), 200); return () => clearTimeout(h); }, [cur.part]);
  const canon = (f: string) => ({ svc: "service", hostname: "host", environment: "env", file: "source" } as Record<string, string>)[f] ?? f;
  const vals = useQuery({ queryKey: ["lg-ac", facetField, vpart, range], queryFn: () => lg("/facets", { ...rangeParams(range), fields: canon(facetField!), search: `${canon(facetField!)}:${vpart}`, limit: 10 }),
    enabled: !!facetField && acOn && focused, staleTime: 15000, retry: false });
  const sugg: Sugg[] = useMemo(() => {
    if (!focused) return [];
    if (cur.field) {
      const f = cur.field.toLowerCase(); const p = cur.part.toLowerCase();
      if (["level", "lvl", "severity", "sev"].includes(f)) return LEVEL_VALUES.filter((v) => v.startsWith(p) && v !== p).map((v) => ({ label: v, insert: `${cur.prefix}${cur.field}:${v} `, kind: t("lg_f_level") }));
      if (["app", "application", "uygulama"].includes(f)) {          // application names (watchover.applications): app:<name> = its hosts and services
        const av = ((fields.find((x: any) => x.name === "app")?.values ?? []) as string[]).filter((v) => v.toLowerCase().includes(p.replace(/"/g, "")) && v.toLowerCase() !== p);
        return av.slice(0, 10).map((v) => ({ label: v, insert: `${cur.prefix}${cur.field}:${qv(v)} `, kind: t("lg_ac_app") }));
      }
      if (facetField) { const fv = (vals.data?.facets?.[canon(facetField)]?.values ?? []) as any[];
        return fv.filter((x) => String(x.v).toLowerCase() !== p).map((x) => ({ label: String(x.v), insert: `${cur.prefix}${cur.field}:${qv(String(x.v))} `, kind: String(x.n) })); }
      return [];
    }
    const w = cur.part; if (!w || /[:"]/.test(w)) return [];
    const names = Array.from(new Set([...BUILTIN, ...fields.map((f: any) => String(f.name))]));
    const out: Sugg[] = names.filter((n) => n.toLowerCase().startsWith(w.toLowerCase()) && n.toLowerCase() !== w.toLowerCase()).slice(0, 10)
      .map((n) => ({ label: `${n}:`, insert: `${cur.prefix}${n}:`, kind: n.startsWith("@") ? t("lg_cols_attrs") : t("lg_ac_field"), field: true }));
    if (/^[A-ZĞ]+$/.test(w) && !cur.prefix) for (const op of ["AND", "OR", "NOT"]) if (op.startsWith(w) && op !== w) out.push({ label: op, insert: `${op} `, kind: t("lg_ac_op") });
    return out;
  }, [cur, focused, fields, vals.data, facetField, t]);  // eslint-disable-line
  useEffect(() => { setHi(-1); }, [cur.start, cur.word]);

  const accept = (s: Sugg) => {
    const before = draft.slice(0, cur.start); let rest = draft.slice(caret); rest = rest.replace(/^[^\s()]*/, "");
    const next = before + s.insert + (s.insert.endsWith(" ") && rest.startsWith(" ") ? rest.slice(1) : rest);
    const pos = (before + s.insert).length; setDraft(next); setAcOn(!!s.field);
    requestAnimationFrame(() => { const el = inp.current; if (el) { el.focus(); el.setSelectionRange(pos, pos); setCaret(pos); setScroll(el.scrollLeft); } });
  };
  const run = () => { setAcOn(false); onRun(draft.trim()); };
  const sync = () => { const el = inp.current; if (el) { setCaret(el.selectionStart ?? el.value.length); setScroll(el.scrollLeft); } };
  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    const open = acOn && sugg.length > 0;
    if (e.key === "ArrowDown" && open) { e.preventDefault(); setHi((h) => (h + 1) % sugg.length); return; }
    if (e.key === "ArrowUp" && open) { e.preventDefault(); setHi((h) => (h <= 0 ? sugg.length - 1 : h - 1)); return; }
    if (e.key === "Tab" && open) { e.preventDefault(); accept(sugg[Math.max(0, hi)]); return; }
    if (e.key === "Enter") { e.preventDefault(); if (open && hi >= 0) accept(sugg[hi]); else run(); return; }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); if (open) { setAcOn(false); return; } if (onEscape?.()) return; inp.current?.blur(); }
  };

  // the coloured mirror: tokens split at the error span so the underline covers exactly [pos, pos + len)
  const quiet = acOn && focused && sugg.length > 0 && dirty;   // still choosing from the autocomplete: no red yet
  const errSpan = perr && !quiet && perr.pos !== undefined && draft.length ? (() => { const s = Math.min(Math.max(0, perr.pos!), draft.length - 1); return [s, Math.max(s + 1, Math.min(draft.length, s + (perr.len || 1)))]; })() : null;
  const parts: { s: string; c: string }[] = [];
  for (const tk of tokenize(draft)) {
    const cuts = [tk.s, tk.e]; if (errSpan) for (const x of errSpan) if (x > tk.s && x < tk.e) cuts.push(x);
    cuts.sort((a, b) => a - b);
    for (let i = 0; i < cuts.length - 1; i++) { const a = cuts[i], b = cuts[i + 1]; if (a === b) continue;
      const inErr = !!errSpan && a >= errSpan[0] && b <= errSpan[1]; parts.push({ s: draft.slice(a, b), c: `tk-${tk.k}${inErr ? " lg-q-err" : ""}` }); }
  }
  const hint = perr ? <span className="lg-hint-err">{errText(perr, lang)}</span> : warn.length ? <span>⚠ {warn.map((w: any) => (lang === "tr" ? w.tr : w.en) || w.code).join(" · ")}</span> : null;

  return <div className="lg-qbar">
    <div className="lg-qrow">
      <div className={`lg-q ${perr ? "bad" : ""} ${focused ? "focus" : ""}`}>
        <Filter size={14} className="lg-q-ic" />
        <div className="lg-q-mirror" aria-hidden><span style={{ transform: `translateX(${-scroll}px)` }}>{parts.map((p, i) => <span key={i} className={p.c}>{p.s}</span>)}</span></div>
        <input ref={inp} value={draft} spellCheck={false} autoComplete="off" placeholder={t("lg_query_ph")} aria-label={t("lg_query_ph")}
          onChange={(e) => { setDraft(e.target.value); setAcOn(true); setCaret(e.target.selectionStart ?? e.target.value.length); setScroll(e.target.scrollLeft); }}
          onKeyDown={onKey} onKeyUp={sync} onClick={sync} onSelect={sync} onScroll={sync} onFocus={() => setFocused(true)} onBlur={() => { setFocused(false); setTimeout(() => setAcOn(false), 120); }} />
        {draft && <button className="lg-q-x" title={t("lg_clear")} onMouseDown={(e) => e.preventDefault()} onClick={() => { setDraft(""); if (value) onRun(""); inp.current?.focus(); }}><X size={13} /></button>}
        {acOn && focused && sugg.length > 0 && <div className="lg-ac card" role="listbox">
          {sugg.map((s, i) => <button key={s.label + i} role="option" aria-selected={i === hi} className={`lg-ac-i ${i === hi ? "on" : ""}`} onMouseDown={(e) => { e.preventDefault(); accept(s); }} onMouseEnter={() => setHi(i)}>
            <span className="mono">{s.label}</span><span className="dim small">{s.kind}</span></button>)}
        </div>}
      </div>
      {children}
    </div>
    {hint && !(acOn && focused && sugg.length > 0 && dirty) && <div className={`lg-hint ${perr ? "is-err" : ""}`}>{hint}</div>}
  </div>;
});
