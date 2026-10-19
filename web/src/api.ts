// Thin client over the Watchover HTTP API: bearer token from localStorage, JSON in / JSON out, 401 with the current token -> sign-out.
export const TOKEN_KEY = "wo_token";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
}

export function getToken(): string | null { try { return localStorage.getItem(TOKEN_KEY); } catch { return null; } }
export function setToken(t: string | null) { try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch { /* private mode */ } }

let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: () => void) { onUnauthorized = fn; }

/** A password change ends every older token and hands back a new one. Requests already on their way with the old token (the page
 *  behind the dialog keeps polling) then answer 401: while the change runs, and for any request sent with a token that is no longer
 *  the current one, that 401 must not sign the user out. It did, and the new token was thrown away with it: the next sign-in with
 *  the old password then said "wrong password" (first sign-in on the RHEL VM, v1.58.3). */
let rotating = 0;
export async function rotateToken<T>(fn: () => Promise<T>): Promise<T> {
  rotating++;
  try { return await fn(); } finally { window.setTimeout(() => { rotating--; }, 3000); }
}

export async function api<T = any>(path: string, opts: { method?: string; body?: any; form?: FormData; text?: boolean; auth?: boolean; headers?: Record<string, string> } = {}): Promise<T> {
  const headers: Record<string, string> = { ...(opts.headers ?? {}) };
  const tok = getToken();
  if (tok && opts.auth !== false) headers.Authorization = `Bearer ${tok}`;
  let body: any = opts.form;
  if (opts.body !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(opts.body); }
  const r = await fetch(`/api${path}`, { method: opts.method || (body ? "POST" : "GET"), headers, body });
  if (r.status === 401 && opts.auth !== false && path !== "/auth/login" && rotating === 0 && tok === getToken()) { onUnauthorized?.(); }
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j); } catch { /* not json */ }
    throw new ApiError(r.status, msg);
  }
  if (r.status === 204) return undefined as T;
  return (opts.text ? r.text() : r.json()) as Promise<T>;
}

export const get = <T = any>(p: string) => api<T>(p);
export const post = <T = any>(p: string, body?: any) => api<T>(p, { method: "POST", body: body ?? {} });
export const patch = <T = any>(p: string, body: any) => api<T>(p, { method: "PATCH", body });
export const put = <T = any>(p: string, body: any) => api<T>(p, { method: "PUT", body });
export const del = <T = any>(p: string) => api<T>(p, { method: "DELETE" });
