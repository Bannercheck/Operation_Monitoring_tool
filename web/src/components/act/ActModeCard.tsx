import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, get, post, put } from "../../api";
import { useT } from "../../i18n";
import { Btn, Collapsible, Err } from "../ui";

/** {"code", "tr", "en", …} of an /api/act error (the API sends it as `detail`; api.ts keeps it as the JSON message). */
export function actErr(e: any): { code?: string; tr?: string; en?: string; methods?: string[] } {
  if (!(e instanceof ApiError)) return {};
  try { return JSON.parse(e.message); } catch { return {}; }
}

/** System › Durum: the action agent's switch (docs/design/action-agent.md K1). Off after an upgrade; "Rehberli" turns on cases
 *  and guided mode only. Changing it needs a fresh step-up (administrators too); switching off does not. */
export function ActModeCard() {
  const { t, lang } = useT(); const qc = useQueryClient();
  const st = useQuery({ queryKey: ["act-status"], queryFn: () => get("/act/status") });
  const [want, setWant] = useState<string | null>(null); const [method, setMethod] = useState<string>(""); const [secret, setSecret] = useState("");
  const [msg, setMsg] = useState<string>("");
  const apply = useMutation({
    mutationFn: (mode: string) => put("/act/policy", { act_mode: mode }),
    onSuccess: () => { setWant(null); setMethod(""); setSecret(""); setMsg(""); qc.invalidateQueries({ queryKey: ["act-status"] }); qc.invalidateQueries({ queryKey: ["act-inbox"] }); },
    onError: async (e: any, mode: string) => {
      const d = actErr(e);
      if (d.code === "act_stepup_required") {
        setWant(mode);
        const r: any = await post("/auth/stepup", { next: "/system" }).catch(() => null);
        if (r?.method === "oidc" && r.url) { window.location.href = r.url; return; }
        setMethod(r?.method ?? "password");
      }
    },
  });
  const verify = useMutation({
    mutationFn: () => post("/auth/stepup/verify", method === "otp" ? { code: secret } : { password: secret }),
    onSuccess: (r: any) => { setMsg(r?.weak ? t("au_stepup_weak") : ""); if (want) apply.mutate(want); },
  });
  const s: any = st.data; const mode = s?.mode ?? "off";
  const d = actErr(apply.error);
  const errText = d.code && d.code !== "act_stepup_required" ? (lang === "tr" ? d.tr : d.en) : "";
  return <Collapsible id="sys-act" title={`🤖 ${t("au_mode_title")}`}>
    <div className="stack small">
      <p className="muted" style={{ margin: 0 }}>{t("au_mode_lead")}</p>
      <div className="row au-modes" role="radiogroup" aria-label={t("au_mode_title")}>
        {(["off", "guided"] as const).map((m) => <button key={m} type="button" role="radio" aria-checked={mode === m}
          className={`chip au-mode ${mode === m ? "on" : ""}`} disabled={apply.isPending || mode === m} onClick={() => apply.mutate(m)}>
          {t(m === "off" ? "au_mode_off" : "au_mode_guided")}</button>)}
      </div>
      <div className="muted">{t("au_mode_now", { mode: t(mode === "off" ? "au_mode_off" : mode === "guided" ? "au_mode_guided" : "au_mode_other"), custody: s?.custody ?? "-" })}
        {s?.killed_global ? <span className="badge red" style={{ marginLeft: 8 }}>{t("au_killed")}</span> : null}</div>
      <p className="dim" style={{ margin: 0 }}>{t("au_mode_note")}</p>
      {want && method && method !== "oidc" && <form className="row au-stepup" onSubmit={(e) => { e.preventDefault(); verify.mutate(); }}>
        <span>{t(method === "otp" ? "au_stepup_code" : "au_stepup_password")}</span>
        <input className="input" type={method === "otp" ? "text" : "password"} autoComplete={method === "otp" ? "one-time-code" : "current-password"}
          value={secret} onChange={(e) => setSecret(e.target.value)} style={{ width: 200 }} required />
        <Btn sm kind="primary" type="submit" disabled={verify.isPending}>{t("au_stepup_verify")}</Btn>
        <Btn sm type="button" onClick={() => { setWant(null); setMethod(""); setSecret(""); }}>{t("cancel")}</Btn>
      </form>}
      {msg && <div className="muted">{msg}</div>}
      {errText && <div className="err">{errText}</div>}
      <Err e={verify.error} />
    </div>
  </Collapsible>;
}
