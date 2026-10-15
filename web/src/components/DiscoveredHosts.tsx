import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Search, X } from "lucide-react";
import { get, post } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Btn, Confirm, Err } from "./ui";

/** Discovered hosts at fleet scale, shared by Connections and Inventory. 300 servers arrive as ~15 pools (the name stem the server
 *  sends, the inventory has no cluster for them yet): one quiet row per pool with one "add all" button instead of 300 table rows and
 *  300 buttons. A pool opens into checkable host chips; a sticky bar promotes a hand-picked selection. Pools start collapsed unless
 *  there are only a few, and a search opens every pool it matches. */
const CRIT = ["critical", "high", "normal", "low"];
type H = { hostname: string; env?: string; ip?: string; dc?: string; pool?: string };
type P = { pool: string; hosts: H[]; envs: string[] };
const envOf = (h: H) => (!h.env || h.env === "unknown" ? "-" : h.env);   // "unknown" is what the feed says when a line carries no environment

export default function DiscoveredHosts() {
  const { t } = useT(); const { can } = useAuth(); const qc = useQueryClient(); const editable = can("act.inventory");
  const q = useQuery({ queryKey: ["discovered"], queryFn: () => get<H[]>("/inventory/discovered"), refetchInterval: 15000 });
  const [s, setS] = useState(""); const [tog, setTog] = useState<Set<string>>(new Set()); const [sel, setSel] = useState<Set<string>>(new Set());
  const [crit, setCrit] = useState<Record<string, string>>({}); const [bulkCrit, setBulkCrit] = useState("normal"); const [done, setDone] = useState<number | null>(null);
  const add = useMutation({ mutationFn: (p: { hosts: H[]; criticality: string }) => post("/inventory/promote", { hosts: p.hosts.map((h) => ({ hostname: h.hostname, env: envOf(h) === "-" ? "" : envOf(h), ip: h.ip ?? "", dc: h.dc ?? "" })), criticality: p.criticality }),
    onSuccess: (r: any, p) => { setDone(r.added); setSel((o) => { const n = new Set(o); p.hosts.forEach((h) => n.delete(h.hostname)); return n; }); ["discovered", "inventory", "inv-stats"].forEach((k) => qc.invalidateQueries({ queryKey: [k] })); } });
  useEffect(() => { if (done == null) return; const id = setTimeout(() => setDone(null), 6000); return () => clearTimeout(id); }, [done]);   // one timer per note, cleared on unmount
  const all: H[] = q.data ?? []; const ql = s.trim().toLowerCase();
  const pools = useMemo<P[]>(() => {
    const by = new Map<string, H[]>();
    for (const h of all) { if (ql && ![h.hostname, h.ip, h.env, h.dc, h.pool].some((v) => (v ?? "").toLowerCase().includes(ql))) continue; const k = h.pool || "-"; if (!by.has(k)) by.set(k, []); by.get(k)!.push(h); }
    return [...by.entries()].map(([pool, hosts]) => ({ pool, hosts, envs: [...new Set(hosts.map(envOf))].filter((e) => e !== "-").sort() }))
      .sort((a, b) => Number(a.pool === "-") - Number(b.pool === "-") || b.hosts.length - a.hosts.length || a.pool.localeCompare(b.pool));
  }, [all, ql]);
  const envs = useMemo(() => { const c = new Map<string, number>(); all.forEach((h) => c.set(envOf(h), (c.get(envOf(h)) ?? 0) + 1)); return [...c.entries()].sort((a, b) => b[1] - a[1]); }, [all]);
  const nPools = useMemo(() => new Set(all.map((h) => h.pool || "-")).size, [all]);
  const picked = all.filter((h) => sel.has(h.hostname));
  const openByDefault = !!ql || nPools <= 3; const isOpen = (p: string) => openByDefault !== tog.has(p);
  const flip = <T,>(set: Set<T>, k: T) => { const n = new Set(set); n.has(k) ? n.delete(k) : n.add(k); return n; };
  const pickPool = (p: P, on: boolean) => setSel((o) => { const n = new Set(o); p.hosts.forEach((h) => (on ? n.add(h.hostname) : n.delete(h.hostname))); return n; });
  if (q.isLoading) return <div className="muted small">…</div>;
  if (!all.length) return <div className="ok">{t("inv_disc_none")}</div>;
  return <div className="dh">
    <div className="dh-top"><div className="dh-sum"><b>{t("disc_sum", { n: all.length, p: nPools })}</b>{envs.map(([e, n]) => <span key={e} className="muted"> · {e === "-" ? t("disc_env_none") : e} {n}</span>)}{done != null && <span className="dh-done">✓ {t("disc_added", { n: done })}</span>}</div>
      <label className="lx-search dh-search"><Search size={14} /><input placeholder={t("disc_search")} value={s} onChange={(e) => { setS(e.target.value); setTog(new Set()); }} />{s && <button className="dh-x" onClick={() => { setS(""); setTog(new Set()); }} title={t("disc_clear")}><X size={13} /></button>}</label></div>
    <div className="dh-list">{pools.map((p) => { const open = isOpen(p.pool); const nOn = p.hosts.filter((h) => sel.has(h.hostname)).length; return <div key={p.pool} className={`dh-pool${open ? " open" : ""}`}>
      <div className="dh-ph"><button className="dh-tg" aria-expanded={open} onClick={() => setTog((o) => flip(o, p.pool))}>{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}<b>{p.pool === "-" ? t("disc_ip_pool") : p.pool}</b><span className="muted small">{t("disc_n", { n: p.hosts.length })}</span>{nOn > 0 && <span className="dh-nsel">{t("disc_n_sel", { n: nOn })}</span>}</button>
        <span className="dh-envs">{p.envs.slice(0, 3).map((e) => <span key={e} className="chip">{e}</span>)}</span>
        {editable && <><select className="input sm dh-crit" title={t("disc_crit")} value={crit[p.pool] ?? "normal"} onChange={(e) => setCrit({ ...crit, [p.pool]: e.target.value })}>{CRIT.map((c) => <option key={c}>{c}</option>)}</select>
          <Confirm kind="" onConfirm={() => add.mutate({ hosts: p.hosts, criticality: crit[p.pool] ?? "normal" })}>{t("disc_add_all")}</Confirm></>}</div>
      {open && <div className="dh-hosts">{editable && <button className="dh-all" onClick={() => pickPool(p, nOn < p.hosts.length)}>{nOn < p.hosts.length ? t("disc_pick_all") : t("disc_pick_none")}</button>}
        {p.hosts.map((h) => { const tip = [h.ip !== h.hostname && h.ip, h.env, h.dc].filter(Boolean).join(" · "); return editable
          ? <label key={h.hostname} className={`dh-h${sel.has(h.hostname) ? " on" : ""}`} title={tip}><input type="checkbox" checked={sel.has(h.hostname)} onChange={() => setSel((o) => flip(o, h.hostname))} />{h.hostname}</label>
          : <span key={h.hostname} className="dh-h" title={tip}>{h.hostname}</span>; })}</div>}
    </div>; })}{!pools.length && <div className="muted small dh-none">{t("disc_no_match")}</div>}</div>
    {editable && picked.length > 0 && <div className="dh-bulk"><b>{t("disc_sel", { n: picked.length })}</b><span className="muted">→</span>
      <select className="input sm dh-crit" title={t("disc_crit")} value={bulkCrit} onChange={(e) => setBulkCrit(e.target.value)}>{CRIT.map((c) => <option key={c}>{c}</option>)}</select>
      <Btn sm kind="primary" disabled={add.isPending} onClick={() => add.mutate({ hosts: picked, criticality: bulkCrit })}>{t("inv_add_from")}</Btn><Btn sm kind="ghost" onClick={() => setSel(new Set())}>{t("disc_clear")}</Btn></div>}
    <Err e={add.error} />
  </div>;
}
