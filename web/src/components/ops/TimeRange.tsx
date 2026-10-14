import { useEffect, useRef, useState } from "react";
import { CalendarClock, ChevronLeft, ChevronRight, X } from "lucide-react";
import { useT } from "../../i18n";
import { QUICK, rangeLabel, type Range } from "./util";

/** Quick ranges as one segmented control, a custom range in a small popover, and ‹ › to step an absolute range back and forth. */
export function TimeRange({ range, live, setLast, setAbs }: { range: Range; live: boolean; setLast: (m: number) => void; setAbs: (s: Date, e: Date) => void }) {
  const { t, lang } = useT(); const [open, setOpen] = useState(false); const box = useRef<HTMLDivElement>(null);
  const local = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  const [s, setS] = useState(""); const [e, setE] = useState("");
  useEffect(() => { const now = new Date(); const st = "last" in range ? new Date(now.getTime() - range.last * 60000) : new Date(range.start); const en = "last" in range ? now : new Date(range.end); setS(local(st)); setE(local(en)); }, [open]);  // eslint-disable-line
  useEffect(() => { const f = (ev: MouseEvent) => { if (box.current && !box.current.contains(ev.target as Node)) setOpen(false); }; document.addEventListener("mousedown", f); return () => document.removeEventListener("mousedown", f); }, []);
  const shift = (dir: number) => { if ("last" in range) return; const a = new Date(range.start).getTime(), b = new Date(range.end).getTime(); const w = b - a; const nb = Math.min(Date.now(), b + dir * w); setAbs(new Date(nb - w), new Date(nb)); };
  const q = (m: number) => (m < 60 ? `${m} ${t("tr_min")}` : m < 1440 ? `${m / 60} ${t("tr_hour")}` : `${m / 1440} ${t("tr_day")}`);
  return <div className="tr" ref={box}>
    <span className={`tr-live ${live ? "on" : ""}`} title={live ? t("tr_live_tip") : t("tr_paused_tip")}><i />{live ? t("tr_live") : t("tr_paused")}</span>
    {!live && <button className="tr-step" onClick={() => shift(-1)} title={t("tr_prev")}><ChevronLeft size={15} /></button>}
    <div className="seg">{QUICK.map((m) => <button key={m} className={"last" in range && range.last === m ? "on" : ""} onClick={() => setLast(m)}>{q(m)}</button>)}
      <button className={!live ? "on" : ""} onClick={() => setOpen(!open)}><CalendarClock size={14} style={{ verticalAlign: -2 }} /> {!live ? rangeLabel(range, lang) : t("tr_custom")}</button></div>
    {!live && <><button className="tr-step" onClick={() => shift(1)} title={t("tr_next")}><ChevronRight size={15} /></button><button className="tr-step" onClick={() => setLast(15)} title={t("tr_back_live")}><X size={15} /></button></>}
    {open && <div className="tr-pop card">
      <div className="small muted" style={{ marginBottom: 8 }}>{t("tr_custom_lead")}</div>
      <label className="small">{t("tr_from")}<input type="datetime-local" className="input" value={s} onChange={(x) => setS(x.target.value)} /></label>
      <label className="small">{t("tr_to")}<input type="datetime-local" className="input" value={e} onChange={(x) => setE(x.target.value)} /></label>
      <div className="row" style={{ marginTop: 10 }}><button className="btn primary sm" onClick={() => { if (s && e) { setAbs(new Date(s), new Date(e)); setOpen(false); } }}>{t("tr_apply")}</button><button className="btn sm" onClick={() => setOpen(false)}>{t("cancel")}</button></div>
    </div>}
  </div>;
}
