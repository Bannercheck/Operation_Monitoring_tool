import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, del, get, getToken, post, put } from "../../api";
import { useT } from "../../i18n";
import { Btn, Collapsible, Confirm, Err, Field, Modal, Table, fmtTs } from "../ui";
import { actErr } from "../act/ActModeCard";

/** System › Güncelleme ve sürümler. Docker / Podman: the page uploads the package into the spool shared with the separate updater
 *  container (the only one with the engine socket), asks it to apply or roll back, and follows the steps it writes; the page keeps
 *  polling while the dashboard itself restarts. Classic installs unpack in place (snapshot first). OpenShift: the Helm commands.
 *  Upload, apply and rollback need sys.update and a fresh step-up (password / e-mail code / OIDC re-login). */

type Step = { name: string; status: string; at?: string; end?: string; msg?: string };

const ICON: Record<string, [string, string]> = { ok: ["✓", "var(--green)"], running: ["⏳", "var(--accent)"], failed: ["✗", "var(--red)"],
  skipped: ["–", "var(--muted)"], pending: ["○", "var(--dim)"] };
const STATUS_BADGE: Record<string, string> = { running: "blue", done: "green", failed: "red", rolled_back: "amber", rollback_failed: "red", queued: "blue" };

function errText(e: any, t: (k: string, kw?: any) => string, lang: string): string {
  if (!e) return "";
  const d: any = actErr(e);
  const code = String(d.code ?? "").replace(/^upd_/, "");
  if (code && t(`upd_err_${code}`) !== `upd_err_${code}`) return t(`upd_err_${code}`) + (d.en && d.en !== code && !/^upd_/.test(d.en) ? ` (${lang === "tr" ? d.tr : d.en})` : "");
  if (d.tr || d.en) return lang === "tr" ? d.tr : d.en;
  return String(e?.message ?? e);
}

/** Resolves once the account holds a fresh step-up; shows the code / password form when one is needed (OIDC: redirect). */
export function useStepUp() {
  const { t } = useT();
  const [need, setNeed] = useState<{ method: string; ok: () => void; no: (e: any) => void } | null>(null);
  const [secret, setSecret] = useState(""); const [busy, setBusy] = useState(false); const [err, setErr] = useState<any>(null);
  const ensure = async (): Promise<void> => {
    try { await post("/system/updates/stepup"); return; } catch (e: any) {
      if (actErr(e).code !== "act_stepup_required") throw e;
    }
    const r: any = await post("/auth/stepup", { next: "/system" });
    if (r?.method === "oidc" && r.url) { window.location.href = r.url; return new Promise<void>(() => undefined); }
    return new Promise<void>((ok, no) => { setSecret(""); setErr(null); setNeed({ method: r?.method ?? "password", ok, no }); });
  };
  const verify = async () => {
    if (!need) return;
    setBusy(true); setErr(null);
    try { await post("/auth/stepup/verify", need.method === "otp" ? { code: secret } : { password: secret }); const n = need; setNeed(null); n.ok(); }
    catch (e) { setErr(e); } finally { setBusy(false); }
  };
  const cancel = () => { if (!need) return; const n = need; setNeed(null); n.no(new Error(t("cancel"))); };
  // a dialog of its own: it must show above the rollback dialog and when the card is collapsed
  const form = need && <Modal title={`🔐 ${t("upd_stepup_lead")}`} onClose={cancel}>
    <form className="stack" onSubmit={(e) => { e.preventDefault(); verify(); }}>
      <Field label={t(need.method === "otp" ? "au_stepup_code" : "au_stepup_password")}>
        <input className="input" type={need.method === "otp" ? "text" : "password"} autoComplete={need.method === "otp" ? "one-time-code" : "current-password"}
          value={secret} onChange={(e) => setSecret(e.target.value)} required autoFocus /></Field>
      <div className="row"><Btn kind="primary" type="submit" disabled={busy}>{t("au_stepup_verify")}</Btn><Btn type="button" onClick={cancel}>{t("cancel")}</Btn></div>
      {err && <div className="err small">{String(err?.message ?? err)}</div>}
    </form>
  </Modal>;
  return { ensure, form };
}

function uploadPackage(file: File, sig: string, onProgress: (p: number) => void): Promise<any> {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", `/api/system/updates/upload?name=${encodeURIComponent(file.name)}`);
    const tok = getToken(); if (tok) x.setRequestHeader("Authorization", `Bearer ${tok}`);
    x.setRequestHeader("Content-Type", "application/octet-stream");
    if (sig) x.setRequestHeader("X-Watchover-Signature", sig.trim());
    x.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    x.onload = () => {
      let j: any = null; try { j = JSON.parse(x.responseText); } catch { /* not json */ }
      if (x.status >= 200 && x.status < 300) resolve(j);
      else reject(new ApiError(x.status, typeof j?.detail === "string" ? j.detail : JSON.stringify(j?.detail ?? j ?? x.statusText)));
    };
    x.onerror = () => reject(new ApiError(0, "network error"));
    x.send(file);
  });
}

export function UpdateCenter() {
  const { t } = useT(); const qc = useQueryClient();
  const ov = useQuery({ queryKey: ["upd"], queryFn: () => get("/system/updates"), refetchInterval: 20000 });
  const d: any = ov.data;
  const loaded = useRef<string>("");
  if (d?.current && !loaded.current) loaded.current = d.current;
  const su = useStepUp();
  const [live, setLive] = useState<any>(null); const [watch, setWatch] = useState(false); const [down, setDown] = useState(0);
  const [restarting, setRestarting] = useState(false); const [newVer, setNewVer] = useState("");
  const refresh = () => { qc.invalidateQueries({ queryKey: ["upd"] }); };

  // follow a job: the updater writes the state into the spool; the page keeps polling while the dashboard restarts
  useEffect(() => { if (d?.mode === "updater" && (d.state?.status === "running" || d.pending?.length)) setWatch(true); }, [d?.mode, d?.state?.status, d?.pending?.length]);
  useEffect(() => {
    if (!watch) return;
    let stop = false;
    const tick = async () => {
      try {
        const r: any = await get("/system/updates/state");
        if (stop) return;
        setLive(r); setDown(0);
        if (r.version && loaded.current && r.version !== loaded.current) setNewVer(r.version);
        if (r.state && r.state.status !== "running" && !r.pending?.length) { setWatch(false); refresh(); }
      } catch { if (!stop) setDown((n) => n + 1); }
    };
    tick(); const id = setInterval(tick, 2000);
    return () => { stop = true; clearInterval(id); };
  }, [watch]);
  // classic installs: the process restarts after unpacking; wait for /api/health with the new version
  useEffect(() => {
    if (!restarting) return;
    const id = setInterval(async () => {
      try { const h: any = await api("/health", { auth: false }); if (h?.version && h.version !== loaded.current) { setNewVer(h.version); setRestarting(false); } } catch { /* still restarting */ }
    }, 2000);
    return () => clearInterval(id);
  }, [restarting]);

  if (!d) return <Collapsible id="sys-update" title={`⬆ ${t("upd_title")}`}><Err e={ov.error} /></Collapsible>;
  const state = live?.state ?? d.state; const pending = live?.pending ?? d.pending ?? [];
  const online = (live?.updater ?? d.updater)?.online;
  const mode: string = d.mode;
  return <Collapsible id="sys-update" title={`⬆ ${t("upd_title")}`} right={<span className="chip">{t("upd_running_ver")}: <b>{d.display}</b></span>}>
    <div className="stack">
      {newVer && <div className="ok row between" style={{ flexWrap: "wrap", gap: 8 }}><span>{t("upd_new_running", { v: `v${newVer}` })}</span><Btn sm kind="primary" onClick={() => window.location.reload()}>{t("upd_reload")}</Btn></div>}
      {watch && down > 0 && <div className="muted small">⏳ {t("upd_reconnect", { n: down })}</div>}
      {restarting && <div className="muted small">⏳ {t("upd_restarting_inproc")}</div>}
      {mode === "openshift" && <><p className="muted small" style={{ margin: 0 }}>{t("upd_openshift_lead")}</p><pre className="code">{d.commands.join("\n")}</pre></>}
      {mode === "manual" && <><p className="muted small" style={{ margin: 0 }}>{t("upd_manual_lead")}</p><pre className="code">{d.commands.join("\n")}</pre></>}
      {mode === "updater" && <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
        <span className="chip"><span className="d" style={{ background: online ? "var(--green)" : "var(--red)" }} />{t("upd_mode_updater")}: {t(online ? "upd_online" : "upd_offline")}</span>
        {d.updater?.engine && <span className="chip">{d.updater.engine}</span>}
        <span className="chip" title={d.signing?.fingerprint || ""}>{d.signing?.required ? `🔏 ${t("upd_sign_on")}` : t("upd_sign_off")}</span>
      </div>}
      {mode === "updater" && !online && <><div className="muted small">{t("upd_offline_lead")}</div><pre className="code">{(d.commands ?? []).join("\n")}</pre></>}
      {mode === "updater" && <p className="dim small" style={{ margin: 0 }}>{t("upd_updater_note")}{!d.signing?.required && <> {t("upd_sign_hint")}</>}</p>}
      {mode === "inprocess" && <p className="muted small" style={{ margin: 0 }}>{t("upd_inprocess_lead")}</p>}
      {(mode === "updater" || mode === "inprocess") && <>
        {state && mode === "updater" && !(pending.length && state.status !== "running") && <JobCard s={state} watching={watch} />}
        {!!pending.length && state?.status !== "running" && <div className="chip">⏳ {t("upd_st_queued")}{online ? "" : ` · ${t("upd_offline")}`}</div>}
        <Upload d={d} ensure={su.ensure} busy={state?.status === "running" || !!pending.length}
          hide={[...pending.map((r: any) => r.id), ...(state?.status === "running" ? [state.id] : [])]}
          onApplied={(r: any) => { if (r?.inprocess) setRestarting(true); else setWatch(true); refresh(); }} onChange={refresh} />
        {mode === "updater" && <Releases d={d} ensure={su.ensure} busy={state?.status === "running" || !!pending.length} onQueued={() => { setWatch(true); refresh(); }} />}
        {mode === "updater" && <UpdSettings d={d} onSaved={refresh} />}
      </>}
      {su.form}
    </div>
  </Collapsible>;
}

function Upload({ d, ensure, busy, hide, onApplied, onChange }: { d: any; ensure: () => Promise<void>; busy: boolean; hide: string[]; onApplied: (r: any) => void; onChange: () => void }) {
  const { t, lang } = useT();
  const [over, setOver] = useState(false); const [prog, setProg] = useState<number | null>(null); const [sig, setSig] = useState<{ name: string; text: string } | null>(null);
  const [err, setErr] = useState<any>(null); const [pkg, setPkg] = useState<any>(null); const input = useRef<HTMLInputElement>(null);
  const [autoRb, setAutoRb] = useState<boolean>(d.config?.auto_rollback ?? true);
  const waiting = (d.packages ?? []).filter((p: any) => !hide.includes(p.id));
  useEffect(() => { if (!pkg && waiting.length) setPkg(waiting[0]); }, [d.packages]);   // an upload survives a page reload (not one already queued)
  const take = async (files: FileList | null) => {
    if (!files?.length) return;
    const list = Array.from(files);
    const s = list.find((f) => f.name.endsWith(".sig"));
    let sigText = sig?.text ?? "";
    if (s) { sigText = (await s.text()).trim(); setSig({ name: s.name, text: sigText }); }
    const file = list.find((f) => /\.(zip|tar|tgz|tar\.gz)$/i.test(f.name));
    if (!file) return;
    setErr(null); setPkg(null);
    try {
      await ensure();
      setProg(0);
      const meta = await uploadPackage(file, sigText, (p) => setProg(p));
      setPkg(meta); setSig(null); onChange();
    } catch (e) { setErr(e); } finally { setProg(null); }
  };
  const apply = useMutation({
    mutationFn: async () => { await ensure(); return post(`/system/updates/packages/${pkg.id}/apply`, { auto_rollback: autoRb }); },
    onSuccess: (r) => { setPkg(null); onApplied(r); },
  });
  const discard = useMutation({ mutationFn: () => del(`/system/updates/packages/${pkg.id}`), onSuccess: () => { setPkg(null); onChange(); } });
  const dir = pkg?.direction ?? "upgrade";
  return <div className="stack">
    {!pkg && <div className={`drop ${over ? "on" : ""}`} role="button" tabIndex={0} aria-disabled={prog != null}
      onClick={() => prog == null && input.current?.click()} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") input.current?.click(); }}
      onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); take(e.dataTransfer.files); }} style={{ padding: 20 }}>
      <div>📦 {t("upd_drop")}</div>
      <div className="dim small" style={{ marginTop: 4 }}>{t("upd_drop_max", { n: d.max_mb })}</div>
      {sig && <div className="small" style={{ marginTop: 6 }}>🔏 {t("upd_sig_attached", { f: sig.name })}</div>}
      {prog != null && <div style={{ marginTop: 10 }}><div className="progress"><div style={{ width: `${Math.round(prog * 100)}%` }} /></div>
        <div className="small" style={{ marginTop: 4 }}>{prog < 1 ? t("upd_uploading", { p: Math.round(prog * 100) }) : t("upd_checking")}</div></div>}
      <input ref={input} type="file" multiple accept=".zip,.tar,.tgz,.gz,.sig" style={{ display: "none" }} onChange={(e) => { take(e.target.files); e.target.value = ""; }} />
    </div>}
    {err && <div className="err">{errText(err, t, lang)}</div>}
    {pkg && <div className="card tight" style={{ boxShadow: "none" }}>
      <div className="row between" style={{ flexWrap: "wrap", gap: 8 }}>
        <b>{t("upd_pkg")}: <span className="mono small">{pkg.name}</span></b>
        <span className={`badge ${dir === "downgrade" ? "red" : dir === "same" ? "amber" : "green"}`} lang={lang}>{t(`upd_dir_${dir}`)}</span>
      </div>
      <div style={{ fontSize: 20, margin: "6px 0" }}><span className="muted">v{pkg.current}</span> → <b>{pkg.display}</b></div>
      <div className="row small muted" style={{ flexWrap: "wrap", gap: 10 }}>
        <span>{t(`upd_kind_${pkg.kind}`)}</span><span>{(pkg.size / 1048576).toFixed(1)} MB</span><span className="mono" title={pkg.sha256}>sha256 {String(pkg.sha256).slice(0, 12)}</span>
        {pkg.git && <span className="mono">git {pkg.git.slice(0, 7)}</span>}
        <span style={{ color: pkg.signature?.status === "ok" ? "var(--green)" : undefined }}>{pkg.signature?.status === "ok" ? "🔏 " : ""}{t(`upd_sig_${pkg.signature?.status ?? "none"}`)}</span>
      </div>
      {dir === "downgrade" && <div className="err small" style={{ marginTop: 8 }}>⚠ {t("upd_downgrade_warn")}</div>}
      {dir === "same" && <div className="muted small" style={{ marginTop: 8 }}>{t("upd_same_warn")}</div>}
      <div className="small" style={{ marginTop: 8 }}><b>{t("upd_notes")}</b>{pkg.notes?.date ? <span className="muted"> · {pkg.notes.date}</span> : null}
        {pkg.notes?.lines?.length ? <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{pkg.notes.lines.map((n: string, i: number) => <li key={i}>{n}</li>)}</ul> : <div className="dim">{t("upd_no_notes")}</div>}</div>
      {d.mode === "updater" && <p className="dim small">{t("upd_apply_lead")}</p>}
      {d.mode === "updater" && <label className="check"><input type="checkbox" checked={autoRb} onChange={(e) => setAutoRb(e.target.checked)} /> {t("upd_auto_rb")}</label>}
      <div className="row" style={{ marginTop: 8, flexWrap: "wrap" }}>
        <Confirm kind="primary" sm={false} onConfirm={() => apply.mutate()}>⬆ {t("upd_apply")}</Confirm>
        <Btn onClick={() => discard.mutate()} disabled={apply.isPending}>{t("upd_discard")}</Btn>
        {busy && <span className="muted small">{t("upd_err_busy")}</span>}
      </div>
      {(apply.error || discard.error) && <div className="err" style={{ marginTop: 8 }}>{errText(apply.error || discard.error, t, lang)}</div>}
    </div>}
  </div>;
}

function JobCard({ s, watching }: { s: any; watching: boolean }) {
  const { t, lang } = useT();
  const [log, setLog] = useState<string | null>(null);
  const loadLog = async () => { try { setLog(await api(`/system/updates/log/${s.id}?n=400`, { text: true })); } catch (e: any) { setLog(String(e?.message ?? e)); } };
  useEffect(() => { if (log != null && s.status === "running") { const id = setInterval(loadLog, 4000); return () => clearInterval(id); } }, [log != null, s.status, s.id]);
  const status = s.status;
  const e = s.error; const ecode = e?.code ? `upd_err_${e.code}` : "";
  return <div className="card tight" style={{ boxShadow: "none" }}>
    <div className="row between" style={{ flexWrap: "wrap", gap: 8 }}>
      <b>{t(s.kind === "rollback" ? "upd_job_rollback" : "upd_job_apply")}: v{s.from || "?"} → v{s.to || "?"}</b>
      <span className={`badge ${STATUS_BADGE[status] ?? ""}`} lang={lang}>{t(`upd_st_${status}`)}</span>
    </div>
    <div className="dim small">{fmtTs(s.started)}{s.ended ? ` – ${fmtTs(s.ended)}` : ""}{s.user ? ` · ${s.user}` : ""}{s.restore_db ? " · DB" : ""}</div>
    <ol style={{ listStyle: "none", padding: 0, margin: "8px 0 0" }}>
      {(s.steps as Step[]).map((st) => { const [ic, col] = ICON[st.status] ?? ICON.pending; return <li key={st.name} className="row" style={{ alignItems: "flex-start", gap: 8, padding: "3px 0" }}>
        <span style={{ color: col, width: 16, textAlign: "center", flex: "none" }} aria-label={st.status}>{ic}</span>
        <span style={{ minWidth: 0 }}><span style={{ fontWeight: st.status === "running" ? 700 : 500 }}>{t(`upd_step_${st.name}`)}</span>
          {st.msg && <span className="dim small" style={{ overflowWrap: "anywhere" }}> · {st.msg}</span>}</span></li>; })}
    </ol>
    {e && <div className={s.status === "rolled_back" ? "muted small" : "err small"} style={{ marginTop: 6 }}>{ecode && t(ecode) !== ecode ? t(ecode) : e.code}{e.detail ? ` (${e.detail})` : ""}</div>}
    {s.status === "rollback_failed" && <div className="err small" style={{ marginTop: 6 }}>{t("upd_rb_failed_note")}</div>}
    <div className="row" style={{ marginTop: 6 }}><Btn sm onClick={() => (log == null ? loadLog() : setLog(null))}>{log == null ? t("upd_log") : t("upd_hide_log")}</Btn>{watching && s.status === "running" && <span className="dim small">⏳</span>}</div>
    {log != null && <pre className="code" style={{ maxHeight: 320, marginTop: 6 }}>{log || "…"}</pre>}
  </div>;
}

function Releases({ d, ensure, busy, onQueued }: { d: any; ensure: () => Promise<void>; busy: boolean; onQueued: () => void }) {
  const { t, lang } = useT();
  const [pick, setPick] = useState<any>(null); const [withDb, setWithDb] = useState(false); const [autoRb, setAutoRb] = useState(true);
  const go = useMutation({
    mutationFn: async () => { await ensure(); return post("/system/updates/rollback", { version: pick.version, restore_db: withDb, auto_rollback: autoRb }); },
    onSuccess: () => { setPick(null); setWithDb(false); onQueued(); },
  });
  const rows = (d.releases ?? []).map((r: any) => ({ id: r.version, ...r }));
  const keep = d.config?.keep ?? d.updater?.keep ?? 5;
  return <div>
    <h4 style={{ margin: "6px 0" }}>{t("upd_versions")}</h4>
    <p className="dim small" style={{ marginTop: 0 }}>{t("upd_versions_lead", { n: keep })}</p>
    <Table rows={rows} cols={[
      { k: "version", label: t("sys_version"), render: (r) => <div style={{ minWidth: 120 }}><b>{r.display}</b>{r.current && <> <span className="badge green" lang={lang}>{t("upd_v_current")}</span></>}{r.previous && <> <span className="badge blue" lang={lang}>{t("upd_v_previous")}</span></>}
        {r.failed_at && <div className="small" style={{ color: "var(--amber)" }}>⚠ {t("upd_v_failed")}</div>}
        {!!r.notes?.lines?.length && <details className="small"><summary className="muted" style={{ cursor: "pointer" }}>{t("upd_notes")}{r.notes.date ? ` · ${r.notes.date}` : ""}</summary><ul style={{ margin: "2px 0 0", paddingLeft: 16 }}>{r.notes.lines.map((n: string, i: number) => <li key={i}>{n}</li>)}</ul></details>}</div> },
      { k: "installed_at", label: t("upd_v_installed"), render: (r) => <span className="small">{fmtTs(r.installed_at)}</span> },
      { k: "source", label: t("upd_v_source"), render: (r) => <span className="small">{t(`upd_src_${r.source}`) !== `upd_src_${r.source}` ? t(`upd_src_${r.source}`) : r.source}{r.signed ? " 🔏" : ""}{r.git ? <span className="mono dim"> {r.git.slice(0, 7)}</span> : null}</span> },
      { k: "images", label: t("upd_v_images"), render: (r) => r.images == null ? "-" : <span style={{ color: r.images ? "var(--green)" : "var(--amber)" }}>{r.images ? "✓" : "✗"}</span> },
      { k: "backup", label: t("upd_v_backup"), render: (r) => <span className="small">{r.backup ? `${fmtTs(r.backup.at)} · ${(r.backup.size / 1048576).toFixed(0)} MB` : "-"}</span> },
      { k: "x", label: "", render: (r) => r.current ? null : <Btn sm disabled={busy} onClick={() => { setPick(r); setWithDb(false); setAutoRb(true); go.reset(); }}>↩ {t("upd_rollback_btn")}</Btn> },
    ]} />
    {pick && <Modal title={t("upd_rb_title", { v: pick.display })} onClose={() => setPick(null)}>
      <div className="stack small">
        <p style={{ margin: 0 }}>{t("upd_rb_lead")}</p>
        <div style={{ fontSize: 18 }}><span className="muted">v{d.current}</span> → <b>{pick.display}</b></div>
        {pick.backup ? <label className="check" style={{ alignItems: "flex-start" }}><input type="checkbox" checked={withDb} onChange={(e) => setWithDb(e.target.checked)} />
          <span>{t("upd_rb_db", { d: fmtTs(pick.backup.at) })}</span></label> : <div className="dim">{t("upd_rb_db_none")}</div>}
        {withDb && <div className="err"><b>⚠ {t("upd_rb_db_warn")}</b></div>}
        <label className="check"><input type="checkbox" checked={autoRb} onChange={(e) => setAutoRb(e.target.checked)} /> {t("upd_auto_rb")}</label>
        <div className="row" style={{ flexWrap: "wrap" }}>
          <Confirm kind={withDb ? "danger" : "primary"} sm={false} onConfirm={() => go.mutate()}>↩ {t("upd_rb_go")}</Confirm>
          <Btn onClick={() => setPick(null)}>{t("cancel")}</Btn>
        </div>
        {go.error && <div className="err">{errText(go.error, t, lang)}</div>}
      </div>
    </Modal>}
  </div>;
}

function UpdSettings({ d, onSaved }: { d: any; onSaved: () => void }) {
  const { t } = useT();
  const [f, setF] = useState<any>(null);
  useEffect(() => { if (!f && d) setF({ keep: d.config?.keep ?? 5, health_s: d.config?.health_s ?? 240, auto_rollback: d.config?.auto_rollback ?? true }); }, [d, f]);
  const save = useMutation({ mutationFn: () => put("/system/updates/config", { keep: Number(f.keep), health_s: Number(f.health_s), auto_rollback: !!f.auto_rollback }), onSuccess: (r) => { setF(r); onSaved(); } });
  if (!f) return null;
  return <details className="small"><summary className="muted" style={{ cursor: "pointer" }}>⚙ {t("upd_settings")}</summary>
    <div className="grid k2" style={{ marginTop: 8 }}>
      <Field label={t("upd_keep")}><input className="input" type="number" min={2} max={50} value={f.keep} onChange={(e) => setF({ ...f, keep: e.target.value })} /></Field>
      <Field label={t("upd_health_s")}><input className="input" type="number" min={30} max={1800} value={f.health_s} onChange={(e) => setF({ ...f, health_s: e.target.value })} /></Field>
    </div>
    <label className="check"><input type="checkbox" checked={!!f.auto_rollback} onChange={(e) => setF({ ...f, auto_rollback: e.target.checked })} /> {t("upd_auto_rb")}</label>
    <div className="row"><Btn sm kind="primary" onClick={() => save.mutate()} disabled={save.isPending}>{t("save")}</Btn>{save.isSuccess && <span className="ok small">{t("saved")}</span>}</div>
    <Err e={save.error} />
  </details>;
}
