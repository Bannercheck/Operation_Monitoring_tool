import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, getToken, setToken, setUnauthorizedHandler } from "./api";

/** One team membership: the team, the user's team role there and what it grants (only on that team's hosts). */
export type TeamMembership = { id: number; name: string; role_id: number | null; role_label: string; role_key: string; permissions: string[]; hosts: number; manage: boolean };
/** permissions: effective (installation role ∪ team roles). global_permissions: the installation role's own, valid in every team.
 *  all: installation-wide view (admin, "see every host", or no team while teams_strict is off). */
export type User = { id: number; email: string; name: string; role: string; must_change: boolean; permissions: string[]; global_permissions?: string[]; all?: boolean;
  teams?: TeamMembership[] };
type Ctx = { user: User | null; ready: boolean; login: (e: string, p: string) => Promise<{ mfa?: string }>; mfa: (challenge: string, code: string) => Promise<void>;
  logout: () => void; can: (perm: string) => boolean; canIn: (teamId: number, perm: string) => boolean; teamsWith: (perm: string) => number[];
  reload: () => Promise<void>; acceptToken: (t: string) => Promise<void> };
const AuthCtx = createContext<Ctx>(null as any);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  const reload = useCallback(async () => {
    if (!getToken()) { setUser(null); setReady(true); return; }
    try { setUser(await api<User>("/auth/me")); } catch { setToken(null); setUser(null); }
    setReady(true);
  }, []);

  useEffect(() => { setUnauthorizedHandler(() => { setToken(null); setUser(null); }); reload(); }, [reload]);
  useEffect(() => {                                   // a role / team change made by someone else reaches an open page too (the API applies it at once)
    const f = () => { if (getToken() && document.visibilityState === "visible") api<User>("/auth/me").then((n) => setUser((u) => (JSON.stringify(u) === JSON.stringify(n) ? u : n))).catch(() => {}); };
    const id = window.setInterval(f, 60000); window.addEventListener("focus", f);
    return () => { window.clearInterval(id); window.removeEventListener("focus", f); };
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const r = await api<any>("/auth/login", { method: "POST", body: { email, password }, auth: false });
    if (r.mfa_required) return { mfa: r.challenge as string };
    setToken(r.token); await reload(); return {};
  }, [reload]);
  const mfa = useCallback(async (challenge: string, code: string) => {
    const r = await api<any>("/auth/mfa", { method: "POST", body: { challenge, code }, auth: false });
    setToken(r.token); await reload();
  }, [reload]);
  const acceptToken = useCallback(async (t: string) => { setToken(t); await reload(); }, [reload]);
  const logout = useCallback(() => { setToken(null); setUser(null); }, []);
  const can = useCallback((perm: string) => !!user && (user.role === "admin" || user.permissions.includes(perm)), [user]);
  /** May the user use `perm` in this team: installation-wide users wherever they hold it, members through their installation role or that team's role. */
  const canIn = useCallback((teamId: number, perm: string) => {
    if (!user) return false;
    if (user.role === "admin") return true;
    if (user.all !== false) return user.permissions.includes(perm);
    const m = (user.teams ?? []).find((x) => x.id === teamId);
    return !!m && ((user.global_permissions ?? []).includes(perm) || m.permissions.includes(perm));
  }, [user]);
  const teamsWith = useCallback((perm: string) => (user?.teams ?? []).filter((m) => (user?.global_permissions ?? []).includes(perm) || m.permissions.includes(perm)).map((m) => m.id), [user]);
  const value = useMemo(() => ({ user, ready, login, mfa, logout, can, canIn, teamsWith, reload, acceptToken }), [user, ready, login, mfa, logout, can, canIn, teamsWith, reload, acceptToken]);
  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
}

export const useAuth = () => useContext(AuthCtx);
