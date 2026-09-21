import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, getToken, setToken, setUnauthorizedHandler } from "./api";

export type User = { id: number; email: string; name: string; role: string; must_change: boolean; permissions: string[] };
type Ctx = { user: User | null; ready: boolean; login: (e: string, p: string) => Promise<{ mfa?: string }>; mfa: (challenge: string, code: string) => Promise<void>;
  logout: () => void; can: (perm: string) => boolean; reload: () => Promise<void>; acceptToken: (t: string) => Promise<void> };
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
  const value = useMemo(() => ({ user, ready, login, mfa, logout, can, reload, acceptToken }), [user, ready, login, mfa, logout, can, reload, acceptToken]);
  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
}

export const useAuth = () => useContext(AuthCtx);
