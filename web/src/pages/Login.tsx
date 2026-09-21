import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Btn, Err, Field } from "../components/ui";
import { api, get } from "../api";

export default function Login() {
  const { login, mfa } = useAuth();
  const { t, lang, setLang } = useT();
  const [email, setEmail] = useState(""); const [pw, setPw] = useState(""); const [code, setCode] = useState("");
  const [challenge, setChallenge] = useState<string | null>(null); const [err, setErr] = useState<any>(null); const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState<"login" | "register" | "verify">("login"); const [name, setName] = useState(""); const [pw2, setPw2] = useState(""); const [info, setInfo] = useState("");
  const options = useQuery({ queryKey: ["auth-options"], queryFn: () => get<{ register: boolean }>("/auth/options") });
  const { acceptToken } = useAuth();
  const providers = useQuery({ queryKey: ["providers"], queryFn: () => get<{ name: string; label: string; configured: boolean }[]>("/auth/providers") });
  useEffect(() => { const m = /error=([^&]+)/.exec(window.location.search); if (m) setErr(decodeURIComponent(m[1])); }, []);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); setErr(null); setBusy(true);
    try {
      if (mode === "register") { if (pw !== pw2) throw new Error(t("reg_mismatch")); const r = await api<any>("/auth/register", { method: "POST", body: { email, password: pw, name }, auth: false }); setInfo(t("reg_sent", { m: r.minutes })); setMode("verify"); }
      else if (mode === "verify") { const r = await api<any>("/auth/verify", { method: "POST", body: { email, code }, auth: false }); await acceptToken(r.token); }
      else if (challenge) await mfa(challenge, code); else { const r = await login(email, pw); if (r.mfa) setChallenge(r.mfa); }
    }
    catch (x: any) { setErr(x?.message || t("login_failed")); } finally { setBusy(false); }
  };
  return (
    <div className="login-bg"><div className="login">
      <div className="row between"><span className="pill"><span className="chip"><span className="d" /> Watchover</span> {t("login_lead")}</span>
        <div className="langs"><button className={lang === "tr" ? "on" : ""} onClick={() => setLang("tr")}>TR</button><button className={lang === "en" ? "on" : ""} onClick={() => setLang("en")}>EN</button></div></div>
      <h1>{challenge || mode === "verify" ? t("mfa_title") : mode === "register" ? t("reg_title") : t("login_title")}</h1>
      <form className="stack" onSubmit={submit} style={{ marginTop: 14 }}>
        {challenge || mode === "verify" ? (<><p className="muted">{info || t("mfa_lead", { m: 5 })}</p><input className="input mono" style={{ fontSize: 22, letterSpacing: 8, textAlign: "center" }} value={code} onChange={(e) => setCode(e.target.value)} maxLength={6} autoFocus inputMode="numeric" /></>) : (<>
          {mode === "register" && <Field label={t("name")}><input className="input" value={name} onChange={(e) => setName(e.target.value)} /></Field>}
          <Field label={t("email")}><input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" autoFocus required /></Field>
          <Field label={t("password")}><input className="input" type="password" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete={mode === "register" ? "new-password" : "current-password"} required minLength={mode === "register" ? 10 : undefined} /></Field>
          {mode === "register" && <Field label={t("reg_pw2")}><input className="input" type="password" value={pw2} onChange={(e) => setPw2(e.target.value)} required /></Field>}</>)}
        <Err e={err} />
        <Btn kind="primary" type="submit" disabled={busy}>{mode === "verify" ? t("reg_verify") : challenge ? t("mfa_btn") : mode === "register" ? t("reg_btn") : t("login_btn")}</Btn>
        {(challenge || mode !== "login") && <Btn kind="ghost" type="button" onClick={() => { setChallenge(null); setCode(""); setMode("login"); setErr(null); }}>{t("back")}</Btn>}
        {!challenge && mode === "login" && options.data?.register && <Btn kind="ghost" type="button" onClick={() => setMode("register")}>{t("reg_new")}</Btn>}
      </form>
      {!challenge && mode === "login" && (<><div className="or">{t("login_or")}</div><div className="sso">{(providers.data ?? [{ name: "google", label: "Google", configured: false }, { name: "microsoft", label: "Microsoft", configured: false }, { name: "apple", label: "Apple", configured: false }]).filter((p) => p.name !== "oidc" || p.configured).map((p) => (
        p.configured ? <a key={p.name} className="btn sso-btn" href={`/api/auth/oidc/${p.name}/start?next=/ops`}><ProviderLogo name={p.name} /> {t("login_with", { p: p.label })}</a>
          : <button key={p.name} className="btn sso-btn" disabled title={t("login_not_configured")}><ProviderLogo name={p.name} /> {t("login_with", { p: p.label })}</button>))}</div></>)}
    </div></div>
  );
}


function ProviderLogo({ name }: { name: string }) {
  if (name === "google") return <svg width="18" height="18" viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9 3.6l6.8-6.8C35.6 2.4 30.1 0 24 0 14.6 0 6.5 5.4 2.6 13.3l7.9 6.1C12.4 13.6 17.7 9.5 24 9.5z"/><path fill="#4285F4" d="M46.5 24.5c0-1.6-.1-3.1-.4-4.5H24v8.5h12.7c-.5 2.9-2.2 5.4-4.7 7.1l7.6 5.9c4.4-4.1 6.9-10.1 6.9-17z"/><path fill="#FBBC05" d="M10.5 28.6c-.5-1.5-.8-3-.8-4.6s.3-3.1.8-4.6l-7.9-6.1C1 16.4 0 20.1 0 24s1 7.6 2.6 10.7l7.9-6.1z"/><path fill="#34A853" d="M24 48c6.1 0 11.6-2 15.6-5.5l-7.6-5.9c-2.1 1.4-4.8 2.3-8 2.3-6.3 0-11.6-4.1-13.5-9.9l-7.9 6.1C6.5 42.6 14.6 48 24 48z"/></svg>;
  if (name === "microsoft") return <svg width="18" height="18" viewBox="0 0 23 23"><rect x="1" y="1" width="10" height="10" fill="#F25022"/><rect x="12" y="1" width="10" height="10" fill="#7FBA00"/><rect x="1" y="12" width="10" height="10" fill="#00A4EF"/><rect x="12" y="12" width="10" height="10" fill="#FFB900"/></svg>;
  if (name === "apple") return <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M16.4 12.6c0-2.5 2-3.7 2.1-3.8-1.2-1.7-3-1.9-3.6-2-1.5-.2-3 .9-3.8.9-.8 0-2-.9-3.3-.9-1.7 0-3.3 1-4.2 2.5-1.8 3.1-.5 7.7 1.3 10.2.9 1.2 1.9 2.6 3.2 2.6 1.3-.1 1.8-.8 3.3-.8s2 .8 3.3.8c1.4 0 2.3-1.3 3.1-2.5 1-1.4 1.4-2.8 1.4-2.9-.1 0-2.8-1.1-2.8-4.1zM14 5.2c.7-.8 1.2-2 1-3.2-1 0-2.2.7-2.9 1.5-.6.7-1.2 1.9-1 3 1.1.1 2.2-.5 2.9-1.3z"/></svg>;
  return <span>🔐</span>;
}