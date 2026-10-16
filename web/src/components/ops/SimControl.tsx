// The simulator switch (Operations › ⋯ › Simulation): starting is refused next to real data (the reasons are shown, nothing is
// offered that would mix them); switching off first shows what will be deleted per category, then purges everything the
// simulator made (watchover.demo). A banner explains a simulator that stopped by itself because real data arrived.
import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { get, post } from "../../api";
import { useT } from "../../i18n";
import { Btn, Err, Modal } from "../ui";

export const CATS = ["lines", "metrics", "traces", "anomalies", "alerts", "topology", "bizflow", "actions", "datasets", "llm_memory", "models"];

/** The assistant's history in this browser tab came from the demo data too: forgotten with it. */
export function forgetChat() { try { sessionStorage.removeItem("wo_chat"); } catch { /* private mode */ } }

/** Any tab: when the server reports a demo purge newer than the one this tab has seen, its assistant history is dropped. */
export function useForgetAfterPurge(onPurge: () => void) {
  const h = useQuery({ queryKey: ["api-health"], queryFn: () => get("/health"), staleTime: 60000, refetchInterval: 60000 });
  const at = Number((h.data as any)?.demo_purged_at || 0);
  useEffect(() => {
    if (!at) return;
    let seen = 0; try { seen = Number(sessionStorage.getItem("wo_chat_purge") || 0); } catch { /* private mode */ }
    if (at > seen) { try { sessionStorage.setItem("wo_chat_purge", String(at)); } catch { /* private mode */ } forgetChat(); onPurge(); }
  }, [at]);  // eslint-disable-line react-hooks/exhaustive-deps
}

export function useSimState(enabled = true, check = false) {
  return useQuery({ queryKey: ["sim-state", check], queryFn: () => get(`/live/simulate${check ? "?check=true" : ""}`), refetchInterval: check ? false : 15000, enabled });
}

/** Per-category counts of a purge preview / result, with the store-level numbers behind a details toggle. */
export function PurgeCounts({ res }: { res: any }) {
  const { t } = useT(); const [open, setOpen] = useState(false);
  const cats: Record<string, number> = res?.categories ?? {};
  return <div className="stack small">
    <table className="table"><tbody>{CATS.map((c) => <tr key={c} className={cats[c] ? "" : "dim"}><td>{t(`sim_cat_${c}`)}</td><td className="right mono">{cats[c] ?? 0}</td></tr>)}</tbody></table>
    <div className="row between"><b>{t("sim_total", { n: res?.total ?? 0 })}</b><Btn sm kind="ghost" onClick={() => setOpen(!open)}>{t("sim_details")}</Btn></div>
    {open && <div className="row" style={{ flexWrap: "wrap" }}>{Object.entries(res?.counts ?? {}).filter(([, n]) => n).map(([k, n]) => <span key={k} className="chip mono">{k}: {String(n)}</span>)}</div>}
  </div>;
}

export function SimDialog({ onClose }: { onClose: () => void }) {
  const { t, lang } = useT(); const qc = useQueryClient();
  const st = useSimState(true, true);                     // the dialog asks whether real data refuses a start
  const state: string = st.data?.state ?? "off";
  const off = state !== "off";
  const preview = useQuery({ queryKey: ["sim-preview"], queryFn: () => get("/live/simulate/preview"), enabled: off, staleTime: 0, gcTime: 0 });
  const [busy, setBusy] = useState(false); const [err, setErr] = useState<any>(null); const [done, setDone] = useState<any>(null);
  const run = async (body: any) => {
    setBusy(true); setErr(null);
    try { const r = await post("/live/simulate", body); if (!body.on) { forgetChat(); setDone(r.purge ?? { total: 0, categories: {} }); } else onClose(); qc.invalidateQueries(); }
    catch (e) { setErr(e); } finally { setBusy(false); }
  };
  const blocked = st.data?.blocked;
  const reasons: Record<string, string[]> = blocked?.reasons ?? {};
  return <Modal title={`🧪 ${off ? t("sim_title_off") : t("sim_title_on")}`} onClose={onClose}>
    {st.isLoading ? <div className="muted">…</div> : done ? <div className="stack">
      <div className="ok">{done.total ? t("sim_done", { n: done.total }) : t("sim_nothing")}</div>
      {!!done.total && <PurgeCounts res={done} />}
      <div className="row"><Btn onClick={onClose}>OK</Btn></div>
    </div> : off ? <div className="stack">
      <p className="muted" style={{ margin: 0 }}>{t("sim_off_lead")}</p>
      {preview.isLoading ? <div className="muted">{t("sim_counting")}</div> : preview.data && <PurgeCounts res={preview.data} />}
      <div className="row"><Btn kind="danger" disabled={busy || preview.isLoading} onClick={() => run({ on: false, confirm: true })}>🧹 {t("sim_confirm")}</Btn><Btn kind="ghost" onClick={onClose}>{t("sim_cancel")}</Btn></div>
      <Err e={err || preview.error} />
    </div> : blocked ? <div className="stack">
      <div className="err">{t("sim_blocked")}</div>
      <table className="table"><tbody>{Object.entries(reasons).map(([k, v]) => <tr key={k}><td>{t(`sim_reason_${k}`)}</td><td className="mono">{v.slice(0, 8).join(", ")}{v.length > 8 ? " …" : ""}</td></tr>)}</tbody></table>
      <p className="muted small" style={{ margin: 0 }}>{lang === "tr" ? blocked.tr : blocked.en}</p>
      <div className="row"><Btn kind="ghost" onClick={onClose}>{t("sim_cancel")}</Btn></div>
    </div> : <div className="stack">
      <p className="muted" style={{ margin: 0 }}>{t("sim_on_lead")}</p>
      <div className="row"><Btn kind="primary" disabled={busy} onClick={() => run({ on: true })}>▶ {t("sim_start")}</Btn><Btn kind="ghost" onClick={onClose}>{t("sim_cancel")}</Btn></div>
      <Err e={err} />
    </div>}
  </Modal>;
}

/** A simulator that stopped by itself (real data arrived) or demo data left from before a restart: say why, offer the purge. */
export function SimBanner({ onClean, enabled }: { onClean: () => void; enabled: boolean }) {
  const { t } = useT();
  const st = useSimState(enabled);
  if (!enabled || st.data?.state !== "paused") return null;
  const n = st.data?.notice;
  return <div className="card row between" style={{ marginBottom: 10, borderColor: "var(--amber)" }}>
    <span>⚠ {n?.code === "real_data" ? t("sim_paused_banner", { s: n.sender }) : t("sim_leftover_banner")}</span>
    <Btn sm kind="danger" onClick={onClean}>🧹 {t("sim_title_off")}</Btn>
  </div>;
}
