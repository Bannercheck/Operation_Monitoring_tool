import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { Check, Clock, Cpu, Database, Radio, Smartphone } from "lucide-react";
import { get, post, put } from "../../api";
import { useT } from "../../i18n";
import { Btn, Err, Field, Modal } from "../../components/ui";
import { InstallCmds, useIngestBase } from "../../components/InstallCmd";
import { AppEditor, CRITS, ChipInput, Toggles } from "./common";

type Way = "agent" | "otel" | "rum" | "platform";
const WAYS: { k: Way; icon: any }[] = [{ k: "agent", icon: Cpu }, { k: "rum", icon: Smartphone }, { k: "platform", icon: Database }, { k: "otel", icon: Radio }];   // Watchover's own agent first; OpenTelemetry only as an optional door

/** The OpenTelemetry service.namespace (and agent name part) for an application name: Turkish letters folded, lower case, a-z0-9-. */
export const slugOf = (s: string) => s.toLocaleLowerCase("tr").replace(/[çğıöşü]/g, (c) => ({ ç: "c", ğ: "g", ı: "i", ö: "o", ş: "s", ü: "u" } as any)[c])
  .normalize("NFKD").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);

/** "Yeni uygulama" in three steps: 1) the application (name, team, criticality, environment); 2) how its data arrives (agents on its
 *  servers, OpenTelemetry from the application itself, the web / mobile sensor, an existing log platform), each with what to hand
 *  over or ask for; 3) a live check that data is arriving. The application is created at the end of step 1 with
 *  service.namespace = its slug as a membership rule, so OpenTelemetry data that carries it joins without further setup. */
export function AppWizard({ onClose }: { onClose: () => void }) {
  const { t } = useT(); const qc = useQueryClient(); const nav = useNavigate();
  const cat = useQuery({ queryKey: ["apps-catalog"], queryFn: () => get("/apps/catalog"), staleTime: 30000 });
  const c: any = cat.data ?? {}; const teams: any[] = c.teams ?? [];
  const [step, setStep] = useState(1); const [err, setErr] = useState<any>(null); const [busy, setBusy] = useState(false);
  const [f, setF] = useState({ name: "", description: "", criticality: "normal", env: "prod", team: undefined as string | undefined });
  const [app, setApp] = useState<any>(null);
  const [ways, setWays] = useState<Way[]>(["agent"]); const [adv, setAdv] = useState(false);
  const team = f.team !== undefined ? f.team : c.install ? "" : String(teams[0]?.id ?? "");
  const slug = slugOf(f.name);
  const create = async () => {
    setErr(null); setBusy(true);
    try {
      const a = await post("/apps", { name: f.name.trim(), description: f.description, criticality: f.criticality, team_id: team === "" ? null : Number(team),
        members: { namespaces: slug ? [slug] : [] } });
      setApp(a); qc.invalidateQueries({ queryKey: ["apps"] }); setStep(2);
    } catch (e) { setErr(e); } finally { setBusy(false); }
  };
  const steps = [t("wz_s1"), t("wz_s2"), t("wz_s3")];
  if (adv) return <AppEditor onClose={onClose} />;
  return <Modal wide title={t("ap_ed_new")} onClose={onClose}>
    <div className="stack">
      <ol className="wz-steps">{steps.map((s, i) => <li key={s} className={i + 1 === step ? "on" : i + 1 < step ? "done" : ""}><span>{i + 1 < step ? <Check size={12} /> : i + 1}</span>{s}</li>)}</ol>
      <Err e={err} />
      {step === 1 && <>
        <div className="dim small">{t("wz_s1_lead")}</div>
        <div className="ap-form">
          <Field label={t("ap_name")}><input className="input" value={f.name} maxLength={80} autoFocus onChange={(e) => setF({ ...f, name: e.target.value })} placeholder={t("wz_name_ph")} /></Field>
          <Field label={t("ap_team")}><select className="input" value={team} onChange={(e) => setF({ ...f, team: e.target.value })}>
            {c.install && <option value="">{t("ap_install")}</option>}{teams.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}</select></Field>
          <Field label={t("ap_criticality")}><select className="input" value={f.criticality} onChange={(e) => setF({ ...f, criticality: e.target.value })}>
            {CRITS.map((k) => <option key={k} value={k}>{t(`ap_crit_${k}`)}</option>)}</select></Field>
          <Field label={t("wz_env")} hint={t("wz_env_hint")}><input className="input" value={f.env} maxLength={30} onChange={(e) => setF({ ...f, env: e.target.value.replace(/[^A-Za-z0-9_.-]/g, "") })} /></Field>
        </div>
        <Field label={t("ap_desc")}><textarea className="input" rows={2} maxLength={500} value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
        {slug && <div className="dim small">{t("wz_slug", { s: slug })}</div>}
        <div className="row between"><Btn kind="ghost" sm onClick={() => setAdv(true)}>{t("wz_advanced")}</Btn><Btn kind="primary" disabled={busy || !f.name.trim() || !slug} onClick={create}>{t("wz_create_next")}</Btn></div>
      </>}
      {step === 2 && app && <>
        <div className="dim small">{t("wz_s2_lead")}</div>
        <div className="wz-ways">{WAYS.map(({ k, icon: I }) => <button key={k} type="button" className={`wz-way ${ways.includes(k) ? "on" : ""}`} aria-pressed={ways.includes(k)}
          onClick={() => setWays(ways.includes(k) ? ways.filter((x) => x !== k) : [...ways, k])}>
          <I size={18} /><b>{t(`wz_w_${k}`)}</b><span className="dim small">{t(`wz_w_${k}_sub`)}</span><em className="small">{t(`wz_w_${k}_who`)}</em></button>)}</div>
        {ways.includes("agent") && <AgentWay app={app} env={f.env} catalogHosts={c.hosts ?? []} onErr={setErr} />}
        {ways.includes("otel") && <OtelWay app={app} slug={slug} env={f.env} onErr={setErr} />}
        {ways.includes("rum") && <RumWay app={app} rumApps={c.rum_apps ?? []} onErr={setErr} />}
        {ways.includes("platform") && <PlatformWay />}
        <div className="row between"><span className="dim small">{t("wz_later")}</span><Btn kind="primary" onClick={() => setStep(3)}>{t("wz_next_check")}</Btn></div>
      </>}
      {step === 3 && app && <LiveCheck app={app} ways={ways} onOpen={() => { onClose(); nav(`/apps/${app.id}`); }} onBack={() => setStep(2)} />}
    </div>
  </Modal>;
}

function Box({ title, children }: { title: string; children: React.ReactNode }) {
  return <div className="card tight wz-box"><h4 style={{ textTransform: "none", letterSpacing: 0, color: "var(--fg)", margin: "0 0 6px" }}>{title}</h4>{children}</div>;
}

/** Agents on the application's servers: the servers become explicit members, and the install command carries the team's (or the
 *  installation's) enrolment key when the reader may see it; otherwise they enrol each server on Settings › Connections. */
function AgentWay({ app, env, catalogHosts, onErr }: { app: any; env: string; catalogHosts: string[]; onErr: (e: any) => void }) {
  const { t } = useT(); const qc = useQueryClient();
  const tid = app.team_id;
  const key = useQuery({ queryKey: ["wz-enroll-key", tid ?? "install"], retry: false,
    queryFn: () => (tid != null ? get(`/teams/${tid}/enroll-key`) : get("/agents/enroll-key")) });
  const k: string = (key.data as any)?.key || "";
  const mayMake = key.isSuccess && !k;                       // the reader may see the key (no 403) but none was made yet
  const makeKey = async () => { onErr(null); try { await post(tid != null ? `/teams/${tid}/enroll-key/rotate` : "/agents/enroll-key/rotate"); key.refetch(); } catch (e) { onErr(e); } };
  const [hosts, setHosts] = useState<string[]>(app.members?.hosts ?? []); const [saved, setSaved] = useState(false);
  const save = async () => { onErr(null); try { await put(`/apps/${app.id}/members`, { hosts }); setSaved(true); qc.invalidateQueries({ queryKey: ["apps"] }); } catch (e) { onErr(e); } };
  return <Box title={t("wz_w_agent")}>
    <Field label={t("wz_agent_hosts")}><ChipInput id="wz-hosts" values={hosts} onChange={(v) => { setHosts(v); setSaved(false); }} options={catalogHosts} placeholder={t("ap_add_value")} /></Field>
    <div className="row" style={{ marginBottom: 8 }}><Btn sm disabled={!hosts.length} onClick={save}>{t("wz_agent_save")}</Btn>{saved && <span className="small" style={{ color: "var(--green)" }}>✓ {t("wz_saved")}</span>}</div>
    {k ? <><div className="small muted">{t("wz_agent_cmd")}</div><InstallCmds auth={`--enroll-key ${k} --env ${env || "prod"}`} compact /></>
      : mayMake ? <div className="row"><span className="small">{t("wz_agent_makekey_lead")}</span><Btn sm kind="primary" onClick={makeKey}>{t("wz_agent_makekey")}</Btn></div>
      : <div className="small">{t("wz_agent_nokey")} <Link to="/connections">{t("nav_hub_settings")} › {t("nav_conn")}</Link></div>}
  </Box>;
}

/** OpenTelemetry from the application itself: a token of its own (an agent record "otel-<slug>", revocable on Settings › Connections)
 *  and the standard OTEL_* variables; service.namespace=<slug> makes everything it sends a member of this application. */
function OtelWay({ app, slug, env, onErr }: { app: any; slug: string; env: string; onErr: (e: any) => void }) {
  const { t } = useT(); const qc = useQueryClient(); const ingest = useIngestBase();
  const info = useQuery({ queryKey: ["agent-install-info"], queryFn: () => get("/agents/install-info"), staleTime: 60000 });
  const base = ((info.data as any)?.https_base as string) || ingest;
  const [tok, setTok] = useState(""); const [busy, setBusy] = useState(false);
  const make = async () => {
    onErr(null); setBusy(true);
    try {
      const r: any = await post("/agents", { name: `otel-${slug}`, env: env || "prod", note: `OpenTelemetry: ${app.name}`, team_id: app.team_id ?? null });
      setTok(r.token); qc.invalidateQueries({ queryKey: ["agents"] });
    } catch (e) { onErr(e); } finally { setBusy(false); }
  };
  const vars = `OTEL_EXPORTER_OTLP_ENDPOINT=${base}
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer ${tok || "<token>"}
OTEL_SERVICE_NAME=<${t("wz_otel_svc")}>
OTEL_RESOURCE_ATTRIBUTES=service.namespace=${slug},deployment.environment=${env || "prod"}`;
  return <Box title={t("wz_w_otel")}>
    <div className="small" style={{ marginBottom: 8 }}>{t("wz_otel_lead")}</div>
    {!tok ? <Btn sm kind="primary" disabled={busy} onClick={make}>{t("wz_otel_make")}</Btn>
      : <div className="small" style={{ color: "var(--amber)", marginBottom: 6 }}>{t("wz_otel_once", { n: `otel-${slug}` })}</div>}
    <pre className="code" style={{ marginTop: 8 }}>{vars}</pre>
    {base.startsWith("http://") && <div className="small" style={{ color: "var(--amber)" }}>{t("wz_otel_plain")}</div>}
    <div className="dim small">{t("wz_otel_java")}</div>
  </Box>;
}

/** The web / mobile sensor: its key is made on the User experience page; existing sensor apps can be tied to this application here. */
function RumWay({ app, rumApps, onErr }: { app: any; rumApps: string[]; onErr: (e: any) => void }) {
  const { t } = useT(); const qc = useQueryClient();
  const [on, setOn] = useState<string[]>(app.members?.rum_apps ?? []);
  const set = async (v: string[]) => { setOn(v); onErr(null); try { await put(`/apps/${app.id}/members`, { rum_apps: v }); qc.invalidateQueries({ queryKey: ["apps"] }); } catch (e) { onErr(e); } };
  return <Box title={t("wz_w_rum")}>
    <div className="small" style={{ marginBottom: 8 }}>{t("wz_rum_lead")} <Link to="/rum?tab=apps">{t("nav_rum")} › {t("rum_tab_apps")}</Link></div>
    {rumApps.length ? <Field label={t("wz_rum_pick")}><Toggles items={rumApps.map((x) => ({ k: x, label: x }))} on={on} onChange={set} /></Field>
      : <div className="dim small">{t("wz_rum_none")}</div>}
  </Box>;
}

function PlatformWay() {
  const { t } = useT();
  return <Box title={t("wz_w_platform")}>
    <div className="small">{t("wz_platform_lead")}</div>
    <ul className="small" style={{ margin: "6px 0", paddingLeft: 18 }}>{t("wz_platform_ask").split("\n").map((x) => <li key={x}>{x}</li>)}</ul>
    <div className="small">{t("wz_platform_where")} <Link to="/sources">{t("nav_hub_settings")} › {t("nav_sources")}</Link></div>
  </Box>;
}

/** Step 3: polls the application every 5 s and ticks what has arrived (servers reporting, services with log lines or calls, sensor apps). */
function LiveCheck({ app, ways, onOpen, onBack }: { app: any; ways: Way[]; onOpen: () => void; onBack: () => void }) {
  const { t } = useT();
  const d = useQuery({ queryKey: ["wz-live", app.id], queryFn: () => get(`/apps/${app.id}?minutes=15`), refetchInterval: 5000 });
  const x: any = d.data ?? {};
  const rep = (x.hosts ?? []).filter((h: any) => h.state === "reporting").length;
  const svcs = (x.services ?? []).filter((s: any) => s.member && (s.lines > 0 || s.calls > 0));
  const rum = (x.rum_apps ?? []).filter((r: any) => r.exists).length;
  const rows = useMemo(() => [
    { k: "hosts", ok: rep > 0, txt: t("wz_chk_hosts", { n: rep, m: (x.hosts ?? []).length }), show: ways.includes("agent") || ways.includes("platform") },
    { k: "svcs", ok: svcs.length > 0, txt: svcs.length ? t("wz_chk_svcs", { n: svcs.length, list: svcs.slice(0, 4).map((s: any) => s.service).join(", ") }) : t("wz_chk_svcs_none"), show: true },
    { k: "rum", ok: rum > 0, txt: t("wz_chk_rum", { n: rum }), show: ways.includes("rum") },
  ].filter((r) => r.show), [rep, svcs, rum, ways, x.hosts, t]);
  const any = rows.some((r) => r.ok);
  return <>
    <div className="dim small">{t("wz_s3_lead")}</div>
    <div className="stack" style={{ gap: 6 }}>{rows.map((r) => <div key={r.k} className={`wz-chk ${r.ok ? "ok" : ""}`}>{r.ok ? <Check size={15} /> : <Clock size={15} />}<span>{r.txt}</span></div>)}</div>
    <div className="small" style={{ color: any ? "var(--green)" : "var(--muted)" }}>{any ? t("wz_chk_ok") : t("wz_chk_wait")}</div>
    <div className="row between"><Btn onClick={onBack}>{t("wz_back")}</Btn><Btn kind="primary" onClick={onOpen}>{t("wz_open")}</Btn></div>
  </>;
}
