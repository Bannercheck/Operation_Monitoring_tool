import { useState } from "react";
import { useT } from "../../i18n";
import { fmtN, fmtPct, hostState } from "./util";

/** Every server as one square, grouped by pool, worst pools first: the shape of 300 hosts in one glance. Click a square to
 *  scope the screen to that host, a pool name to scope to the pool. */
export function HostMap({ pools, sel, pool, onHost, onPool }: { pools: any[]; sel: string[]; pool: string; onHost: (h: string) => void; onPool: (p: string) => void }) {
  const { t, lang } = useT(); const [f, setF] = useState("");
  const fl = f.toLowerCase();
  const rows = pools.filter((p) => !fl || p.pool.toLowerCase().includes(fl) || p.hosts.some((h: any) => h.host.toLowerCase().includes(fl)))
    .sort((a, b) => Number(b.pool === pool) - Number(a.pool === pool));          // the pool in focus first
  return <div>
    <div className="row between" style={{ marginBottom: 10 }}>
      <div className="hm-legend small">{(["ok", "warn", "crit", "silent"] as const).map((k) => <span key={k}><i className={`hm-c ${k}`} />{t(`hm_${k}`)}</span>)}</div>
      <input className="input sm" style={{ width: 160 }} placeholder={t("hm_find")} value={f} onChange={(e) => setF(e.target.value)} />
    </div>
    <div className="hm">{rows.map((p) => <div key={p.pool} className={`hm-row ${pool === p.pool ? "on" : ""}`}>
      <button className="hm-pool" onClick={() => onPool(pool === p.pool ? "" : p.pool)} title={t("hm_pool_tip")}>
        <b>{p.pool}</b><span className="dim small">{p.host_count} · {fmtN(p.per_min, lang)}/{t("tr_min")}{p.errors ? <> · <span style={{ color: p.error_rate >= 0.05 ? "var(--red)" : "var(--amber)" }}>{fmtPct(p.error_rate, 1)}</span></> : null}</span></button>
      <div className="hm-cells">{p.hosts.map((h: any) => <button key={h.host} className={`hm-c ${hostState(h)} ${sel.includes(h.host) ? "sel" : ""} ${fl && h.host.toLowerCase().includes(fl) ? "hit" : ""}`} onClick={() => onHost(h.host)}
        title={`${h.host}\n${fmtN(h.events, lang)} ${t("hm_lines")} · ${h.errors} ${t("hm_errors")} (${fmtPct(h.error_rate, 2)})${h.silent ? `\n${t("hm_silent")}` : ""}`} />)}</div>
    </div>)}</div>
  </div>;
}
