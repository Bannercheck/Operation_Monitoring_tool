/** What every built-in dashboard shares with the operations shell: the range and scope (as query strings), the refresh period,
 *  the service variable, and the shell's detail openers (server drawer, range detail, metric detail, log explorer). The dashboard,
 *  refresh period and service live in the URL next to the range and scope, so a dashboard view is one shareable link. */
import { createContext, useContext } from "react";
import { useSearchParams } from "react-router-dom";
import type { useOpsState } from "../ops/util";

export type DashId = "overview" | "infra" | "app" | "logs" | "ux";
export const DASHES: DashId[] = ["overview", "infra", "app", "logs", "ux"];
export const REFRESH = [0, 10, 30, 60];

export function useDashParams() {
  const [sp, setSp] = useSearchParams();
  const raw = sp.get("d") as DashId | null; const d: DashId = raw && DASHES.includes(raw) ? raw : "overview";
  const r = sp.get("refresh"); const svc = sp.get("svc") || "";
  const set = (k: string, v: string | null) => setSp((cur) => { const n = new URLSearchParams(cur); if (v) n.set(k, v); else n.delete(k); return n; }, { replace: true });
  return {
    d, svc, refresh: r,
    /** seconds between refreshes: the chosen one, else 10 s on a live range and off on a fixed one (the screen's old behaviour) */
    every: (live: boolean) => (r === null ? (live ? 10 : 0) : Number(r) || 0),
    setD: (v: DashId) => set("d", v === "overview" ? null : v), setSvc: (v: string) => set("svc", v || null), setRefresh: (s: number) => set("refresh", String(s)),
  };
}

export type Dash = {
  st: ReturnType<typeof useOpsState>; rq: string; sq: string; span: number; live: boolean; ms: number | false; svc: string;
  openHost: (h: string) => void; openRange: (k: "volume" | "errors" | "avail" | "latency") => void; openMetric: (k: "cpu" | "memory" | "disk" | "gpu") => void;
  goLogs: (f: { sev?: string; service?: string; q?: string; host?: string; tab?: string }) => void; minutes: number; setDash: (d: DashId) => void;
};
export const DashCtx = createContext<Dash>(null as any);
export const useDash = () => useContext(DashCtx);
