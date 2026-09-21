import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Btn, Err, Field } from "../components/ui";
import { get } from "../api";

export default function Login() {
  const { login, mfa } = useAuth();
  const { t, lang, setLang } = useT();
  const [email, setEmail] = useState(""); const [pw, setPw] = useState(""); const [code, setCode] = useState("");
  const [challenge, setChallenge] = useState<string | null>(null); const [err, setErr] = useState<any>(null); const [busy, setBusy] = useState(false);
  const providers = useQuery({ queryKey: ["providers"], queryFn: () => get<{ name: string; label: string }[]>("/auth/providers") });
  useEffect(() => { const m = /error=([^&]+)/.exec(window.location.search); if (m) setErr(decodeURIComponent(m[1])); }, []);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); setErr(null); setBusy(true);
    try { if (challenge) await mfa(challenge, code); else { const r = await login(email, pw); if (r.mfa) setChallenge(r.mfa); } }
    catch (x: any) { setErr(x?.message || t("login_failed")); } finally { setBusy(false); }
  };
  return (
    <div className="login-bg"><div className="login">
      <div className="row between"><span className="pill"><span className="chip"><span className="d" /> Watchover</span> {t("login_lead")}</span>
        <div className="langs"><button className={lang === "tr" ? "on" : ""} onClick={() => setLang("tr")}>TR</button><button className={lang === "en" ? "on" : ""} onClick={() => setLang("en")}>EN</button></div></div>
      <h1>{challenge ? t("mfa_title") : t("login_title")}</h1>
      <form className="stack" onSubmit={submit} style={{ marginTop: 14 }}>
        {challenge ? (<><p className="muted">{t("mfa_lead", { m: 5 })}</p><input className="input mono" style={{ fontSize: 22, letterSpacing: 8, textAlign: "center" }} value={code} onChange={(e) => setCode(e.target.value)} maxLength={6} autoFocus inputMode="numeric" /></>) : (<>
          <Field label={t("email")}><input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" autoFocus required /></Field>
          <Field label={t("password")}><input className="input" type="password" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="current-password" required /></Field></>)}
        <Err e={err} />
        <Btn kind="primary" type="submit" disabled={busy}>{challenge ? t("mfa_btn") : t("login_btn")}</Btn>
        {challenge && <Btn kind="ghost" type="button" onClick={() => { setChallenge(null); setCode(""); }}>{t("back")}</Btn>}
      </form>
      {!challenge && !!providers.data?.length && (<><div className="or">{t("login_or")}</div><div className="sso">{providers.data.map((p) => (
        <a key={p.name} className="btn" href={`/api/auth/oidc/${p.name}/start?next=/ops`}>{t("login_with", { p: p.label })}</a>))}</div></>)}
    </div></div>
  );
}
