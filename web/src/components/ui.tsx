import { useEffect, useState, type ReactNode } from "react";
import { useT } from "../i18n";
import { useTheme, type Theme } from "../theme";

export const Card = ({ children, className = "", title, right }: { children: ReactNode; className?: string; title?: ReactNode; right?: ReactNode }) => (
  <div className={`card ${className}`}>{(title || right) && <div className="row between" style={{ marginBottom: 8 }}><h3>{title}</h3><div className="row">{right}</div></div>}{children}</div>
);

export const Kpi = ({ value, label, sub, accent = "var(--accent)", icon }: { value: ReactNode; label: string; sub?: ReactNode; accent?: string; icon?: ReactNode }) => (
  <div className="card kpi" style={{ ["--acc" as any]: accent }}><div className="ic">{icon}</div><div className="lb">{label}</div><div className="v">{value}</div><div className="sub">{sub ?? " "}</div></div>
);

export const Chip = ({ children, color }: { children: ReactNode; color?: string }) => <span className="chip">{color && <span className="d" style={{ background: color }} />}{children}</span>;

const STATUS_COLOR: Record<string, string> = { open: "red", ack: "amber", in_progress: "amber", resolved: "green", done: "green", ignored: "", suppressed: "", active: "green", revoked: "red", pending: "amber", disabled: "",
  critical: "red", high: "amber", medium: "blue", low: "", P1: "red", P2: "amber", P3: "blue", P4: "", ERROR: "red", CRITICAL: "red", WARN: "amber", INFO: "blue", DEBUG: "" };
export const Badge = ({ v, label }: { v: string; label?: string }) => <span lang="en" className={`badge ${STATUS_COLOR[v] ?? ""}`}>{label ?? v}</span>;   // lang=en: CSS uppercase must give CRITICAL, not CRİTİCAL under a Turkish page

export const Btn = ({ children, kind = "", sm, ...p }: { children: ReactNode; kind?: string; sm?: boolean } & React.ButtonHTMLAttributes<HTMLButtonElement>) => (
  <button className={`btn ${kind} ${sm ? "sm" : ""}`} {...p}>{children}</button>
);

export function Confirm({ children, onConfirm, kind = "danger", sm = true }: { children: ReactNode; onConfirm: () => void; kind?: string; sm?: boolean }) {
  const { t } = useT();
  const [ask, setAsk] = useState(false);
  useEffect(() => { if (!ask) return; const id = setTimeout(() => setAsk(false), 4000); return () => clearTimeout(id); }, [ask]);
  return ask ? <Btn kind={kind} sm={sm} onClick={() => { setAsk(false); onConfirm(); }}>{t("confirm")} · {t("yes")}</Btn> : <Btn kind={kind} sm={sm} onClick={() => setAsk(true)}>{children}</Btn>;
}

export const Field = ({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) => <div className="field"><label>{label}</label>{children}{hint && <span className="dim small">{hint}</span>}</div>;

export function Modal({ title, onClose, children, wide }: { title: ReactNode; onClose: () => void; children: ReactNode; wide?: boolean }) {
  useEffect(() => { const h = (e: KeyboardEvent) => e.key === "Escape" && onClose(); window.addEventListener("keydown", h); return () => window.removeEventListener("keydown", h); }, [onClose]);
  return (
    <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${wide ? "wide" : ""}`}><div className="row between" style={{ marginBottom: 14 }}><h3>{title}</h3><Btn kind="ghost" sm onClick={onClose}>✕</Btn></div>{children}</div>
    </div>
  );
}

export const Empty = ({ children }: { children?: ReactNode }) => { const { t } = useT(); return <div className="empty">{children ?? t("none")}</div>; };
export const Err = ({ e }: { e: any }) => (e ? <div className="err">{String(e?.message ?? e)}</div> : null);

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { k: T; label: ReactNode }[]; value: T; onChange: (k: T) => void }) {
  return <div className="tabs">{tabs.map((x) => <button key={x.k} className={x.k === value ? "on" : ""} onClick={() => onChange(x.k)}>{x.label}</button>)}</div>;
}

export function Table({ cols, rows, empty }: { cols: { k: string; label: ReactNode; num?: boolean; render?: (r: any) => ReactNode }[]; rows: any[]; empty?: ReactNode }) {
  if (!rows.length) return <Empty>{empty}</Empty>;
  return (
    <div className="table-wrap"><table className="table"><thead><tr>{cols.map((c) => <th key={c.k} className={c.num ? "right" : ""}>{c.label}</th>)}</tr></thead>
      <tbody>{rows.map((r, i) => <tr key={r.id ?? r.key ?? i}>{cols.map((c) => <td key={c.k} className={c.num ? "num" : ""}>{c.render ? c.render(r) : String(r[c.k] ?? "")}</td>)}</tr>)}</tbody></table></div>
  );
}

export function Spark({ data, color = "var(--accent)" }: { data: number[]; color?: string }) {
  if (!data?.length) return null;
  const max = Math.max(...data, 1), w = 300, h = 60;
  const pts = data.map((v, i) => `${(i / Math.max(data.length - 1, 1)) * w},${h - (v / max) * (h - 6) - 3}`).join(" ");
  return <svg className="spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none"><polyline points={pts} fill="none" stroke={color} strokeWidth="2" /></svg>;
}

export const fmtTs = (s?: string) => (s ? s.replace("T", " ").slice(0, 16) : "-");
export const hhmm = (s?: string) => (s ? s.slice(11, 16) : "-");


export function LangTheme() {
  const { lang, setLang } = useT(); const { theme, setTheme } = useTheme();
  const icons: Record<Theme, string> = { dark: "🌙", system: "🖥", light: "☀️" };
  const titles: Record<Theme, string> = lang === "tr" ? { dark: "Koyu", system: "Sistem", light: "Açık" } : { dark: "Dark", system: "System", light: "Light" };
  return <div className="row" style={{ gap: 8 }}>
    <div className="langs"><button className={lang === "tr" ? "on" : ""} onClick={() => setLang("tr")}>TR</button><button className={lang === "en" ? "on" : ""} onClick={() => setLang("en")}>EN</button></div>
    <div className="langs">{(["dark", "system", "light"] as Theme[]).map((k) => <button key={k} className={theme === k ? "on" : ""} onClick={() => setTheme(k)} title={titles[k]}>{icons[k]}</button>)}</div>
  </div>;
}