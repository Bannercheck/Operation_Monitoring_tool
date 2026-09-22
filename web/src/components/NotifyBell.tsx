import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Bell } from "lucide-react";
import { get } from "../api";
import { useT } from "../i18n";
import { Badge, Btn } from "./ui";

/** Top-bar bell: open anomalies polled every 15 s, a badge with the count, a panel with the newest ones; unseen ones are
 *  highlighted and (once allowed) announced with a browser notification, so an operator on another page still notices. */
export function NotifyBell() {
  const { t } = useT(); const [open, setOpen] = useState(false); const ref = useRef<HTMLDivElement>(null);
  const [seen, setSeen] = useState<Set<number>>(() => { try { return new Set(JSON.parse(localStorage.getItem("wo_seen_anom") || "[]")); } catch { return new Set(); } });
  const [perm, setPerm] = useState<string>(typeof Notification !== "undefined" ? Notification.permission : "denied");
  const q = useQuery({ queryKey: ["bell-anomalies"], queryFn: () => get("/anomalies?status=active&limit=30"), refetchInterval: 15000 });
  const rows: any[] = q.data ?? []; const fresh = rows.filter((r) => !seen.has(r.id));
  useEffect(() => { const h = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); }; document.addEventListener("mousedown", h); return () => document.removeEventListener("mousedown", h); }, []);
  const announced = useRef<Set<number>>(new Set());
  useEffect(() => {                                         // browser notification for anomalies that appeared since the page loaded
    if (perm !== "granted" || !rows.length) return;
    for (const r of fresh) { if (announced.current.has(r.id)) continue; announced.current.add(r.id); if (announced.current.size > 1 || document.visibilityState === "hidden") { try { new Notification(`Watchover · ${r.severity ?? r.kind}`, { body: `${r.title} · ${r.host || r.service || r.env || ""}`, tag: `anom-${r.id}` }); } catch { /* ignore */ } } }
  }, [rows.length, perm]);
  useEffect(() => { document.title = fresh.length ? `(${fresh.length}) Watchover` : "Watchover"; }, [fresh.length]);
  const markAll = () => { const s = new Set(rows.map((r) => r.id)); setSeen(s); try { localStorage.setItem("wo_seen_anom", JSON.stringify([...s])); } catch { /* ignore */ } };
  const ask = async () => { try { setPerm(await Notification.requestPermission()); } catch { /* ignore */ } };
  return <div className="bell" ref={ref}>
    <Btn kind="ghost" sm onClick={() => setOpen((o) => !o)} title={t("bell_title")}><Bell size={18} />{fresh.length > 0 && <span className="cnt">{fresh.length}</span>}</Btn>
    {open && <div className="bell-panel">
      <div className="row between" style={{ marginBottom: 6 }}><b>🔔 {t("bell_title")} · {rows.length} {t("bell_anom")}</b><div className="row">{fresh.length > 0 && <Btn sm onClick={markAll}>{t("bell_mark")}</Btn>}<Link className="btn sm" to="/anomalies" onClick={() => setOpen(false)}>{t("bell_all")}</Link></div></div>
      {typeof Notification !== "undefined" && (perm === "granted" ? <div className="dim small" style={{ marginBottom: 6 }}>✓ {t("bell_browser_on")}</div> : perm !== "denied" ? <div style={{ marginBottom: 6 }}><Btn sm onClick={ask}>🔔 {t("bell_browser")}</Btn></div> : null)}
      {!rows.length && <div className="muted small">{t("bell_none")}</div>}
      {rows.slice(0, 15).map((r) => <div key={r.id} className={`item ${seen.has(r.id) ? "" : "new"}`}>
        <div className="row between"><span><Badge v={r.kind === "errors" || (r.kind === "metric" && String(r.key || "").startsWith("threshold:")) ? "high" : r.status} label={r.kind === "metric" ? (r.metric || "metric") : r.kind} /> <b>{r.title}</b>{!seen.has(r.id) && <span className="badge green" style={{ marginLeft: 6 }}>{t("bell_new")}</span>}</span><span className="dim small">{(r.last_seen || "").slice(11, 16)}</span></div>
        <div className="muted small">{[r.env, r.host, r.service, r.metric].filter(Boolean).join(" · ")}{r.detail ? ` · ${String(r.detail).slice(0, 90)}` : ""}</div>
      </div>)}
    </div>}
  </div>;
}
