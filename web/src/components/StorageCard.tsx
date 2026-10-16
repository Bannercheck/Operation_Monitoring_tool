import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { get, post, put } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Btn, Collapsible, Field, fmtTs } from "./ui";
import { actErr } from "./act/ActModeCard";
import { useStepUp } from "./sys/Updates";

/** System › Depolama (GET /api/system/storage, logtiers.py + logaudit.py): where the hot logs and the archive live — given by hand,
 *  or "ayarlanmadı" (the data volume) — how long each keeps lines, usage against the disk / budget, the yearly need at the current
 *  rate, the storage alerts (80 / 90 %, archiving paused on a full disk, integrity), the form that sets places and retention
 *  (Docker: through the updater or the command; OpenShift: the Helm values), and the audit tools: verify the hash-chained
 *  manifest, export a range for auditors, search months in the background. */
const fmtB = (b?: number | null) => {
  if (b == null) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"]; let v = b; let i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
};
const dayLabel = (d?: string) => (d && d.length === 8 ? `${d.slice(6, 8)}.${d.slice(4, 6)}.${d.slice(0, 4)}` : "–");
const isoDay = (d?: string) => (d && d.length === 8 ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}` : "");
const diskPct = (k?: any) => {
  if (!k || !k.total) return null;
  const used = k.used ?? k.total - k.free;
  return Math.round((1000 * used) / (used + k.free)) / 10;
};
type T = (k: string, kw?: Record<string, any>) => string;

function Usage({ bytes, cap, pct, disk }: { bytes: number; cap: number; pct: number | null; disk?: any }) {
  const { t } = useT();
  const dp = diskPct(disk);
  const p = Math.max(pct ?? 0, dp ?? 0);
  const cls = p >= 90 ? "bad" : p >= 80 ? "warn" : "";
  return <div className="stg-use">
    <div className={`stg-bar ${cls}`} role="meter" aria-valuenow={p} aria-valuemin={0} aria-valuemax={100}><i style={{ width: `${Math.min(100, p)}%` }} /></div>
    <span className={`small ${cls ? "" : "dim"}`} style={cls ? { color: cls === "bad" ? "var(--red)" : "var(--amber, #f59e0b)" } : undefined}>
      {cap ? t("stg_cap", { u: fmtB(bytes), c: fmtB(cap), p: pct ?? 0 }) : t("stg_nocap", { u: fmtB(bytes) })}</span>
    {disk && <span className="small dim">{t("stg_disk_of", { t: fmtB(disk.total), f: fmtB(disk.free) })}{dp != null ? ` · %${dp}` : ""}</span>}
  </div>;
}

function errMsg(e: any, t: T, lang: string): string {
  const d: any = actErr(e);
  if (d.code && t(`stg_err_${d.code}`) !== `stg_err_${d.code}`) return t(`stg_err_${d.code}`);
  if (d.tr || d.en) return lang === "tr" ? d.tr : d.en;
  return String(e?.message ?? e ?? "");
}

function alertText(a: any, t: T): string {
  return t(`stg_a_${a.code}`, { p: a.pct ?? "", d: dayLabel(a.day), days: a.code === "archive_integrity" ? (a.days ?? []).map(dayLabel).join(", ") || "–" : "",
    need: fmtB(a.need), room: fmtB(a.room), fit: a.days_fit ?? "–", codes: (a.problems ?? []).join(", ") });
}

function Placement({ d }: { d: any }) {
  const { t } = useT();
  const paths = d.setup?.paths ?? {};
  const row = (k: "hot" | "cold") => {
    const p = paths[k] ?? {};
    const badge = p.kind === "pvc" ? <span className="badge blue">{t("stg_pvc")}</span>
      : p.configured ? <span className="badge green">{t("stg_set_by_hand")}</span> : <span className="badge amber">{t("stg_unset")}</span>;
    return <div className="row" style={{ gap: 8, flexWrap: "wrap" }} key={k}>
      <b className="small" style={{ minWidth: 92 }}>{t(k === "hot" ? "stg_place_hot" : "stg_place_cold")}</b>{badge}
      <code className="small">{p.label ?? "–"}</code>
      {!p.configured && p.kind !== "pvc" && <span className="small dim">{t("stg_unset_note", { p: p.label })}</span>}
    </div>;
  };
  return <div className="stack" style={{ gap: 4, marginBottom: 10 }}>{row("hot")}{row("cold")}</div>;
}

function Estimate({ d }: { d: any }) {
  const { t } = useT();
  const e = d.estimate ?? {}; const cold = d.cold ?? {};
  if (!e.basis) return <div className="small dim">{t("stg_est")}: {t("stg_est_none")}</div>;
  const room = cold.cap_bytes || (cold.disk ? (cold.bytes ?? 0) + cold.disk.free : 0);
  const short = room && e.retention_bytes > room && e.daily_bytes ? Math.floor(room / e.daily_bytes) : null;
  return <div className="small">
    <b>{t("stg_est")}:</b> {t("stg_est_line", { lpm: (e.lines_per_min ?? 0).toLocaleString(), bpl: e.bytes_per_line, m: t(e.bpl_measured ? "stg_est_measured" : "stg_est_assumed"),
      d: fmtB(e.daily_bytes), y: fmtB(e.yearly_bytes), days: Math.round(d.cold_retention_days ?? 365), r: fmtB(e.retention_bytes) })}
    {room ? <> · {t("stg_est_vs", { c: fmtB(room) })}</> : null}
    {short != null && <span style={{ color: "var(--amber, #f59e0b)" }}> · {t("stg_est_short", { n: short })}</span>}
  </div>;
}

function ConfigForm({ d }: { d: any }) {
  const { t, lang } = useT(); const qc = useQueryClient(); const step = useStepUp();
  const paths = d.setup?.paths ?? {}; const mode = d.setup?.mode ?? "";
  const [hot, setHot] = useState(""); const [cold, setCold] = useState("");
  const [hours, setHours] = useState<string>(String(Math.round(d.retention_h ?? 48)));
  const [days, setDays] = useState<string>(String(Math.round(d.cold_retention_days ?? 365)));
  const [keepOld, setKeepOld] = useState(false);
  const [res, setRes] = useState<any>(null); const [err, setErr] = useState<any>(null);
  useEffect(() => { setHot(paths.hot?.host_path ?? ""); setCold(paths.cold?.host_path ?? ""); }, [paths.hot?.host_path, paths.cold?.host_path]);
  const pinned: string[] = d.setup?.pinned ?? [];
  const coldDays: any[] = d.cold?.days ?? [];
  const apply = useMutation({
    mutationFn: async () => {
      const nd = Number(days), nh = Number(hours);
      const body: any = { keep_old: keepOld };
      if (hot.trim() && hot.trim() !== (paths.hot?.host_path ?? "")) body.hot_path = hot.trim();
      if (cold.trim() && cold.trim() !== (paths.cold?.host_path ?? "")) body.cold_path = cold.trim();
      if (nh !== Math.round(d.retention_h ?? 48)) body.hot_hours = nh;
      if (nd !== Math.round(d.cold_retention_days ?? 365) || body.hot_path || body.cold_path) body.cold_days = nd;
      if (nd < (d.cold_retention_days ?? 365) && mode !== "openshift") {
        const cut = new Date(Date.now() - nd * 86400000).toISOString().slice(0, 10).replace(/-/g, "");
        const k = coldDays.filter((x) => x.day < cut).length;
        if (k && !window.confirm(t("stg_cfg_expire_warn", { n: nd, k }))) throw new Error(t("cancel"));
      }
      await step.ensure();
      return put("/system/storage/config", body);
    },
    onSuccess: (r) => { setRes(r); setErr(null); qc.invalidateQueries({ queryKey: ["sys-storage"] }); },
    onError: (e) => { setErr(e); setRes(null); },
  });
  const state = useQuery({ queryKey: ["stg-upd-state"], queryFn: () => get("/system/updates/state"), enabled: !!res?.queued, refetchInterval: 2500 });
  const job = state.data?.state && state.data.state.kind === "storage" ? state.data.state : null;
  const label = mode === "openshift" ? t("stg_cfg_show_helm") : mode === "manual" ? t("stg_cfg_show_cmd") : t("stg_cfg_apply");
  return <div className="stack" style={{ gap: 8 }}>
    <div className="small muted">{t("stg_cfg_lead")}</div>
    <div className="grid k2">
      <Field label={t("stg_cfg_hot")}><input className="input mono" placeholder="/srv/watchover/hot" value={hot} onChange={(e) => setHot(e.target.value)} disabled={mode === "openshift"} /></Field>
      <Field label={t("stg_cfg_cold")}><input className="input mono" placeholder="/srv/watchover/arsiv" value={cold} onChange={(e) => setCold(e.target.value)} disabled={mode === "openshift"} /></Field>
      <Field label={t("stg_cfg_hours")}><input className="input" type="number" min={1} max={8760} value={hours} onChange={(e) => setHours(e.target.value)} /></Field>
      <Field label={t("stg_cfg_days")}><input className="input" type="number" min={0} max={3650} value={days} onChange={(e) => setDays(e.target.value)} /></Field>
    </div>
    <label className="check small"><input type="checkbox" checked={keepOld} onChange={(e) => setKeepOld(e.target.checked)} /> {t("stg_cfg_keep_old")}</label>
    {!!pinned.length && mode !== "openshift" && <div className="small dim">{t("stg_cfg_pinned")}</div>}
    <div className="row"><Btn kind="primary" sm onClick={() => apply.mutate()} disabled={apply.isPending}>{label}</Btn></div>
    {err && <div className="err small">{errMsg(err, t, lang)}</div>}
    {res?.applied && <div className="small" style={{ color: "var(--green)" }}>{t("stg_cfg_saved")}</div>}
    {res?.queued && <div className="small">{t("stg_cfg_queued")}
      {job && <ol className="small" style={{ margin: "4px 0 0 18px" }}>{(job.steps ?? []).map((s: any) =>
        <li key={s.name} style={{ color: s.status === "failed" ? "var(--red)" : s.status === "ok" ? "var(--green)" : s.status === "running" ? "var(--accent)" : "var(--dim)" }}>
          {t(`upd_step_${s.name}`)} · {s.status}{s.msg ? ` · ${s.msg}` : ""}</li>)}</ol>}
      {job?.error && <div className="err small">{job.error.code}: {job.error.detail}</div>}</div>}
    {res?.command && !res?.queued && <div className="small">{t("stg_cfg_cmd")}<pre className="mono small">{res.command}</pre></div>}
    {res?.helm && <div className="small">{t("stg_cfg_helm")}<pre className="mono small" style={{ whiteSpace: "pre-wrap" }}>{res.helm.join("\n")}</pre></div>}
    {res?.commands && <div className="small">{t("stg_cfg_inproc")}<pre className="mono small" style={{ whiteSpace: "pre-wrap" }}>{res.commands.join("\n")}</pre></div>}
    {step.form}
  </div>;
}

function Audit({ d }: { d: any }) {
  const { t, lang } = useT(); const qc = useQueryClient(); const step = useStepUp("/system/storage/stepup");
  const sp = new URLSearchParams(window.location.search);
  const coldDays: any[] = d.cold?.days ?? [];
  const [from, setFrom] = useState(sp.get("audit_from") ?? isoDay(coldDays[0]?.day));
  const [to, setTo] = useState(sp.get("audit_to") ?? new Date().toISOString().slice(0, 10));
  const [fmt, setFmt] = useState("archive"); const [q, setQ] = useState(sp.get("audit_q") ?? "");
  const [err, setErr] = useState<any>(null);
  const jobs = useQuery({ queryKey: ["stg-jobs"], queryFn: () => get("/system/storage/jobs"),
    refetchInterval: (qq: any) => ((qq.state.data ?? []).some((j: any) => j.status === "running") ? 1500 : 15000) });
  const start = useMutation({
    mutationFn: async (body: any) => { if (body.kind !== "verify") await step.ensure(); return post("/system/storage/jobs", body); },
    onSuccess: () => { setErr(null); qc.invalidateQueries({ queryKey: ["stg-jobs"] }); },
    onError: (e) => setErr(e),
  });
  const cancel = useMutation({ mutationFn: (id: string) => post(`/system/storage/jobs/${id}/cancel`), onSuccess: () => qc.invalidateQueries({ queryKey: ["stg-jobs"] }) });
  const download = async (name: string) => { const r: any = await post(`/system/storage/exports/${encodeURIComponent(name)}/ticket`); window.location.href = r.url; };
  const m = d.manifest ?? {}; const ck = m.check; const lv = m.last_verify;
  const list: any[] = (jobs.data as any[]) ?? [];
  const running = list.some((j) => j.status === "running");
  return <div className="stack" style={{ gap: 8 }}>
    <div className="small muted">{t("stg_audit_lead")}</div>
    <div className="small">{t("stg_manifest", { n: m.head?.seq ?? 0, s: m.head?.seq ?? 0, h: m.head?.hash ? `${String(m.head.hash).slice(0, 16)}…` : "–" })}</div>
    {ck && <div className="small" style={{ color: ck.ok ? "var(--green)" : "var(--red)" }}>{t(ck.ok ? "stg_check_ok" : "stg_check_bad", { n: ck.problem_count ?? 0, t: fmtTs(ck.at) })}</div>}
    {!!(m.problems ?? []).length && <ul className="small" style={{ margin: 0, color: "var(--red)" }}>{m.problems.slice(0, 8).map((p: any, i: number) =>
      <li key={i}>{p.code}{p.day ? ` · ${dayLabel(p.day)}` : ""}{p.seq ? ` · seq ${p.seq}` : ""}</li>)}</ul>}
    <div className="small dim">{lv ? t("stg_last_verify", { t: fmtTs(lv.at), r: t(lv.ok ? "stg_ok_word" : "stg_bad_word") }) : t("stg_never_verified")}</div>
    <div className="grid k2">
      <Field label={t("stg_from")}><input className="input" type="date" value={from} onChange={(e) => setFrom(e.target.value)} /></Field>
      <Field label={t("stg_to")}><input className="input" type="date" value={to} onChange={(e) => setTo(e.target.value)} /></Field>
    </div>
    <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
      <Btn sm onClick={() => start.mutate({ kind: "verify", start: from, end: to })} disabled={running || start.isPending}>🔏 {t("stg_verify")}</Btn>
      <select className="input" style={{ width: 240 }} value={fmt} onChange={(e) => setFmt(e.target.value)}>
        <option value="archive">{t("stg_fmt_archive")}</option><option value="ndjson">{t("stg_fmt_ndjson")}</option></select>
      <Btn sm onClick={() => start.mutate({ kind: "export", start: from, end: to, format: fmt })} disabled={running || start.isPending}>⬇ {t("stg_export")}</Btn>
    </div>
    <Field label={t("stg_query")} hint={t("stg_search_lead")}>
      <div className="row" style={{ gap: 6 }}><input className="input mono" value={q} onChange={(e) => setQ(e.target.value)} placeholder="level:error host:ea-*" />
        <Btn sm onClick={() => start.mutate({ kind: "search", start: from, end: to, query: q })} disabled={running || start.isPending || !from || !to}>🔎 {t("stg_search")}</Btn></div>
    </Field>
    {running && <div className="small dim">{t("stg_busy_job")}</div>}
    {err && <div className="err small">{errMsg(err, t, lang)}</div>}
    {!!list.length && <div className="stack" style={{ gap: 4 }}>
      <b className="small">{t("stg_jobs")}</b>
      {list.slice(0, 6).map((j) => <div key={j.id} className="small" style={{ borderTop: "1px solid var(--line2)", paddingTop: 4 }}>
        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
          <b>{t(`stg_job_${j.kind}`)}</b><span className="dim">{j.params?.start ? dayLabel(j.params.start) : "–"} → {j.params?.end ? dayLabel(j.params.end) : "–"}</span>
          <span className={`badge ${j.status === "done" ? "green" : j.status === "running" ? "blue" : j.status === "failed" ? "red" : ""}`}>{t(`stg_js_${j.status}`)}</span>
          <span className="dim">{fmtTs(j.started)} · {j.user}</span>
          {j.status === "running" && <><span className="stg-bar" style={{ width: 120, display: "inline-block" }}><i style={{ width: `${Math.round((j.progress ?? 0) * 100)}%` }} /></span>
            <span>{Math.round((j.progress ?? 0) * 100)}%</span><button className="linklike" onClick={() => cancel.mutate(j.id)}>{t("stg_cancel")}</button></>}
        </div>
        {j.status === "done" && j.kind === "verify" && <div style={{ color: j.result?.ok ? "var(--green)" : "var(--red)" }}>
          {t("stg_res_verify", { c: j.result?.checked ?? 0, p: j.result?.problem_count ?? 0 })}
          {(j.result?.problems ?? []).slice(0, 5).map((p: any) => ` · ${p.code}${p.day ? " " + dayLabel(p.day) : ""}`).join("")}</div>}
        {j.status === "done" && j.result?.file && <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
          <span className="mono">{t(j.kind === "search" ? "stg_res_search" : "stg_res_export", { f: j.result.file, b: fmtB(j.result.bytes),
            s: `${String(j.result.sha256).slice(0, 16)}…`, m: (j.result.matched ?? 0).toLocaleString() })}</span>
          <button className="linklike" onClick={() => download(j.result.file)}>⬇ {t("stg_download")}</button>
          {!!(j.result.not_ok ?? []).length && <span style={{ color: "var(--red)" }}>{j.result.not_ok.map(dayLabel).join(", ")}</span>}</div>}
        {j.error && <div className="err">{j.error.code}: {j.error.detail}</div>}
      </div>)}
    </div>}
    {step.form}
  </div>;
}

export function StorageCard() {
  const { t } = useT(); const { can } = useAuth();
  const q = useQuery({ queryKey: ["sys-storage"], queryFn: () => get("/system/storage"), refetchInterval: 60000 });
  const d: any = q.data;
  if (!d) return null;
  const hot = d.hot ?? {}; const cold = d.cold; const alerts: any[] = d.alerts ?? [];
  const hotDays: any[] = hot.days ?? []; const coldDays: any[] = cold?.days ?? [];
  const last = cold?.archived?.length ? cold.archived[cold.archived.length - 1] : null;
  const mig = d.migration ?? {};
  const crit = alerts.some((a) => a.level === "crit");
  const capWarn: any[] = (d.warnings ?? []).filter((w: any) => w.code === "hot_cap");
  const n = alerts.length + capWarn.length;
  return <Collapsible id="sys-storage" title={`🗄 ${t("stg_title")}`}
    right={<span className={`badge ${crit ? "red" : n ? "amber" : "green"}`}>{n ? `${n} ${t("stg_warn")}` : t("stg_ok")}</span>}>
    <div className="muted small" style={{ marginBottom: 8 }}>{d.mode === "files" ? t("stg_hint") : t("stg_dbmode")}</div>
    {d.mode === "files" && <Placement d={d} />}
    <div className="grid k3">
      <div className="stg-tier">
        <div className="stg-h">{t("stg_hot", { h: Math.round(d.retention_h ?? 48) })}</div>
        <div className="stg-v">{fmtB(hot.bytes)}</div>
        <div className="small dim">{hotDays.length ? `${t("stg_days", { n: hotDays.length })} · ${dayLabel(hotDays[0]?.day)} → ${dayLabel(hotDays[hotDays.length - 1]?.day)}` : (hot.in_main_db ? t("stg_in_main") : t("stg_empty"))}</div>
        <div className="small dim">{t("stg_lines", { n: (hot.events ?? 0).toLocaleString() })}{hot.bytes_per_line ? ` · ${t("stg_bpl", { b: hot.bytes_per_line })}` : ""}</div>
        <Usage bytes={hot.bytes ?? 0} cap={hot.cap_bytes ?? 0} pct={hot.pct ?? null} disk={hot.disk} />
      </div>
      <div className="stg-tier">
        <div className="stg-h">{t("stg_cold", { d: Math.round(d.cold_retention_days ?? 365) })}</div>
        {cold ? <>
          <div className="stg-v">{fmtB(cold.bytes)}</div>
          <div className="small dim">{coldDays.length ? `${t("stg_days", { n: coldDays.length })} · ${dayLabel(coldDays[0]?.day)} → ${dayLabel(coldDays[coldDays.length - 1]?.day)}` : (cold.enabled ? t("stg_never") : t("stg_disabled"))}</div>
          <div className="small dim">{t("stg_lines", { n: (cold.lines ?? 0).toLocaleString() })}{cold.bytes_per_line ? ` · ${t("stg_bpl", { b: cold.bytes_per_line })}` : ""}</div>
          <Usage bytes={cold.bytes ?? 0} cap={cold.cap_bytes ?? 0} pct={cold.pct ?? null} disk={cold.disk} />
          {last && <div className="small dim">{t("stg_last", { d: dayLabel(last.day), s: last.seconds, r: (last.lines_per_s ?? 0).toLocaleString() })}</div>}
          {!!cold.last_run?.errors?.length && <div className="small bad">{cold.last_run.errors[0].error}</div>}
        </> : <div className="small dim">{t("stg_dbmode")}</div>}
      </div>
      <div className="stg-tier">
        <div className="stg-h">{t("stg_main")}</div>
        <div className="stg-v">{fmtB(d.main_db?.bytes)}</div>
        <div className="small dim">{d.main_db?.backend === "postgresql" ? "PostgreSQL" : "SQLite"}</div>
        <div className="small dim">{t("stg_main_sub")}</div>
        {mig.state === "running" && <div className="small" style={{ color: "var(--amber, #f59e0b)" }}>{t("stg_migrating", { c: (mig.copied ?? 0).toLocaleString(), n: (mig.total ?? 0).toLocaleString() })}</div>}
        {mig.state === "done" && <div className="small dim">{t("stg_migrated")}</div>}
      </div>
    </div>
    {cold && <div style={{ marginTop: 8 }}><Estimate d={d} /></div>}
    {!!n && <ul className="small" style={{ marginTop: 8 }}>
      {alerts.map((a) => <li key={a.code} style={{ color: a.level === "crit" ? "var(--red)" : "var(--amber, #f59e0b)" }}>{alertText(a, t)}</li>)}
      {capWarn.map((w) => <li key={w.code} style={{ color: "var(--amber, #f59e0b)" }}>{t("stg_a_hot_cap", { p: w.pct })}</li>)}</ul>}
    {cold && <div className="small dim" style={{ marginTop: 4 }}>{t("stg_full_policy")} {t("stg_alert_route")}</div>}
    {cold?.last_run?.at && <div className="small dim" style={{ marginTop: 6 }}>{t("stg_job", { t: fmtTs(cold.last_run.at) })}</div>}
    {d.mode === "files" && can("sys.update") && <Collapsible id="sys-storage-cfg" title={`⚙ ${t("stg_cfg")}`} className="nested"
      defaultOpen={!d.setup?.paths?.cold?.configured && d.setup?.mode !== "openshift"}><ConfigForm d={d} /></Collapsible>}
    {cold && can("sys.maint") && <Collapsible id="sys-storage-audit" title={`🔏 ${t("stg_audit")}`} defaultOpen={new URLSearchParams(window.location.search).has("audit_q")} className="nested">
      <Audit d={d} /></Collapsible>}
  </Collapsible>;
}
