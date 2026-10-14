import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { Smartphone } from "lucide-react";
import { del, get, patch, post } from "../../api";
import { useAuth } from "../../auth";
import { useT } from "../../i18n";
import { Btn, Card, Confirm, Empty, Err, Field, Kpi, Modal, Tabs } from "../../components/ui";
import { fmtMs, pct } from "../apm/common";

/** User experience (web and mobile): sessions, crash-free and error-free rates, Web Vitals and app start, slow steps and top
 *  errors; the session list; and the apps whose keys the sensors carry, with ready-to-paste snippets per platform. */
const RANGES = [60, 360, 1440, 10080];
const GRADE: Record<string, string> = { good: "var(--green)", needs: "var(--amber)", poor: "var(--red)" };
const PLAT_ICON: Record<string, string> = { web: "🖥", android: "🤖", ios: "", flutter: "🦋", "react-native": "⚛", other: "📦" };

export default function Rum() {
  const { t } = useT(); const [sp, setSp] = useSearchParams();
  const tab = (sp.get("tab") || "overview") as "overview" | "sessions" | "apps";
  const minutes = Number(sp.get("minutes") || 1440); const app = sp.get("app") || "";
  const set = (k: string, v: string) => { const n = new URLSearchParams(sp); if (v) n.set(k, v); else n.delete(k); setSp(n, { replace: true }); };
  const ov = useQuery({ queryKey: ["rum-ov", minutes, app], queryFn: () => get(`/rum/overview?minutes=${minutes}${app ? `&app=${encodeURIComponent(app)}` : ""}`), refetchInterval: 20000 });
  const apps = useQuery({ queryKey: ["rum-apps"], queryFn: () => get("/rum/apps") });
  const d = ov.data; const rows: any[] = d?.apps ?? [];
  const tot = rows.reduce((a, r) => ({ s: a.s + r.sessions, u: a.u + r.users, c: a.c + (r.crashed ?? 0), b: a.b + (r.bad ?? 0) }), { s: 0, u: 0, c: 0, b: 0 });
  return <div className="stack">
    <div className="row between"><div><h2><Smartphone size={20} style={{ verticalAlign: -3 }} /> {t("rum_title")}</h2><span className="muted small">{t("rum_sub")}</span></div>
      <div className="row" style={{ gap: 8 }}>
        <select className="input" style={{ width: 170 }} value={app} onChange={(e) => set("app", e.target.value)}><option value="">{t("rum_all_apps")}</option>{(apps.data?.apps ?? []).map((a: any) => <option key={a.id} value={a.name}>{a.name}</option>)}</select>
        <select className="input" style={{ width: 130 }} value={minutes} onChange={(e) => set("minutes", e.target.value === "1440" ? "" : e.target.value)}>{RANGES.map((m) => <option key={m} value={m}>{t("apm_last")} {m < 1440 ? `${m / 60} sa` : `${m / 1440} gün`}</option>)}</select>
      </div></div>
    <Tabs value={tab} onChange={(k) => set("tab", k === "overview" ? "" : k)} tabs={[{ k: "overview", label: t("rum_tab_overview") }, { k: "sessions", label: t("rum_tab_sessions") }, { k: "apps", label: `${t("rum_tab_apps")} · ${apps.data?.apps?.length ?? 0}` }]} />
    {tab === "overview" && <>
      <div className="grid k4">
        <Kpi value={tot.s.toLocaleString()} label={t("rum_k_sessions")} sub={`${rows.length} ${t("rum_k_apps")}`} />
        <Kpi value={tot.u.toLocaleString()} label={t("rum_k_users")} accent="#60a5fa" sub={t("rum_k_users_sub")} />
        <Kpi value={tot.s ? pct(1 - tot.c / tot.s, 1) : "-"} label={t("rum_k_crashfree")} accent="#2dd4bf" sub={`${tot.c} ${t("rum_k_crashed")}`} />
        <Kpi value={tot.s ? pct(1 - tot.b / tot.s, 1) : "-"} label={t("rum_k_errfree")} accent="#f87171" sub={`${tot.b} ${t("rum_k_witherr")}`} />
      </div>
      {!rows.length ? <Empty>{t("rum_none")}</Empty> : <>
        <Card title={t("rum_apps_title")}>
          <div className="table-wrap"><table className="table"><thead><tr><th>{t("rum_col_app")}</th><th className="right">{t("rum_k_sessions")}</th><th className="right">{t("rum_k_users")}</th><th className="right">{t("rum_col_views")}</th>
            <th className="right">{t("rum_k_crashfree")}</th><th className="right">{t("rum_k_errfree")}</th><th className="right">{t("rum_col_avg")}</th><th>{t("rum_col_perf")}</th></tr></thead>
            <tbody>{rows.map((r) => { const v = d.vitals?.[r.app]; const st = d.app_start?.[r.app];
              return <tr key={r.app + r.platform}><td><Link to={`/rum?tab=sessions&app=${encodeURIComponent(r.app)}`}><b>{PLAT_ICON[r.platform] ?? ""} {r.app}</b></Link><div className="dim small">{r.platform}</div></td>
                <td className="num">{r.sessions}</td><td className="num">{r.users}</td><td className="num">{r.views}</td>
                <td className="num" style={{ color: r.crash_free < 0.99 ? "var(--red)" : "var(--green)" }}>{pct(r.crash_free, 1)}</td>
                <td className="num" style={{ color: r.error_free < 0.95 ? "var(--amber)" : undefined }}>{pct(r.error_free, 1)}</td>
                <td className="num">{fmtMs(r.avg_ms)}</td>
                <td>{v ? <span className="row" style={{ gap: 6 }}>{["lcp", "inp", "cls"].map((k) => v[k] && <span key={k} className="badge" style={{ color: GRADE[v[k].grade], textTransform: "none" }}>{k.toUpperCase()} {k === "cls" ? v[k].p75 : fmtMs(v[k].p75)}</span>)}</span>
                  : st ? <span className="small">{t("rum_appstart")} P50 <b>{fmtMs(st.p50)}</b> · P95 {fmtMs(st.p95)}</span> : <span className="dim">-</span>}</td></tr>; })}</tbody></table></div>
        </Card>
        <div className="grid k2 top">
          <Card title={t("rum_slow_steps")}>{!d.slow_steps?.length ? <Empty>{t("apm_none")}</Empty> :
            <div className="table-wrap"><table className="table"><thead><tr><th>{t("rum_col_step")}</th><th className="right">{t("apm_calls")}</th><th className="right">P75</th><th className="right">P95</th></tr></thead>
              <tbody>{d.slow_steps.map((s: any) => <tr key={s.app + s.name}><td><span className="dim small">{s.app}</span> · <span className="mono">{s.name}</span></td><td className="num">{s.count}</td><td className="num"><b>{fmtMs(s.p75)}</b></td><td className="num">{fmtMs(s.p95)}</td></tr>)}</tbody></table></div>}</Card>
          <Card title={t("rum_top_errors")}>{!d.errors?.length ? <Empty>{t("rum_no_errors")}</Empty> :
            <div className="table-wrap"><table className="table"><thead><tr><th>{t("rum_col_error")}</th><th className="right">{t("rum_col_n")}</th><th className="right">{t("rum_k_sessions")}</th></tr></thead>
              <tbody>{d.errors.map((e: any, i: number) => <tr key={i}><td><span className={`badge ${e.severity === "CRITICAL" ? "red" : "amber"}`}>{e.severity === "CRITICAL" ? t("rum_crash") : t("rum_error")}</span> <span className="dim small">{e.app}</span><div className="mono small">{e.message}</div></td>
                <td className="num">{e.n}</td><td className="num">{e.sessions}</td></tr>)}</tbody></table></div>}</Card>
        </div>
        <div className="dim small">{t("rum_kvkk")}</div>
      </>}
    </>}
    {tab === "sessions" && <Sessions app={app} minutes={minutes} apps={apps.data?.apps ?? []} />}
    {tab === "apps" && <Apps data={apps.data} />}
  </div>;
}

function Sessions({ app, minutes, apps }: { app: string; minutes: number; apps: any[] }) {
  const { t } = useT(); const { can } = useAuth(); const [sp, setSp] = useSearchParams();
  const [platform, setPlatform] = useState(""); const [errors, setErrors] = useState(false); const userRef = sp.get("user_ref") || "";
  const [look, setLook] = useState(false);
  const q = useQuery({ queryKey: ["rum-sessions", app, minutes, platform, errors, userRef], queryFn: () => get(`/rum/sessions?minutes=${minutes}&limit=200${app ? `&app=${encodeURIComponent(app)}` : ""}${platform ? `&platform=${platform}` : ""}${errors ? "&errors=true" : ""}${userRef ? `&user_ref=${userRef}` : ""}`), refetchInterval: 20000 });
  const rows: any[] = q.data ?? [];
  return <Card>
    <div className="row" style={{ gap: 8, marginBottom: 10 }}>
      <select className="input" style={{ width: 150 }} value={platform} onChange={(e) => setPlatform(e.target.value)}><option value="">{t("rum_all_platforms")}</option>{["web", "android", "ios", "flutter", "react-native"].map((p) => <option key={p} value={p}>{p}</option>)}</select>
      <label className="row small" style={{ gap: 4 }}><input type="checkbox" checked={errors} onChange={(e) => setErrors(e.target.checked)} />{t("rum_with_errors")}</label>
      {userRef ? <span className="badge blue" style={{ textTransform: "none" }}>{t("apm_user")}: {userRef} <a href="#" onClick={(e) => { e.preventDefault(); const n = new URLSearchParams(sp); n.delete("user_ref"); setSp(n); }}>✕</a></span> : null}
      <div className="grow" />{can("act.rum") && <Btn sm onClick={() => setLook(true)}>🔎 {t("rum_lookup")}</Btn>}
    </div>
    {!rows.length ? <Empty>{t("rum_no_sessions")}</Empty> :
      <div className="table-wrap"><table className="table"><thead><tr><th>{t("rum_col_last")}</th><th>{t("rum_col_app")}</th><th>{t("rum_col_device")}</th><th>{t("apm_user")}</th><th>{t("rum_col_path")}</th>
        <th className="right">{t("rum_col_steps")}</th><th className="right">{t("rum_col_dur")}</th><th>{t("rum_col_state")}</th></tr></thead>
        <tbody>{rows.map((s) => <tr key={s.session_id}>
          <td className="mono"><Link to={`/rum/sessions/${encodeURIComponent(s.session_id)}`}>{s.last.replace("T", " ").slice(5, 19)}</Link></td>
          <td>{PLAT_ICON[s.platform] ?? ""} {s.app} <span className="dim small">{s.version}</span></td>
          <td className="small">{[s.os, s.browser, s.model, s.net, s.carrier].filter(Boolean).join(" · ")}</td>
          <td className="mono small">{s.user_ref || "-"}</td>
          <td className="small mono">{s.entry}{s.exit && s.exit !== s.entry ? ` → ${s.exit}` : ""}</td>
          <td className="num">{s.views + s.actions}<span className="dim small"> / {s.requests}</span></td><td className="num">{fmtMs(s.duration_ms)}</td>
          <td>{s.crashes ? <span className="badge red">{t("rum_crash")}</span> : s.errors ? <span className="badge amber">{s.errors} {t("rum_error")}</span> : <span className="badge green">ok</span>}</td>
        </tr>)}</tbody></table></div>}
    {look && <Lookup onClose={() => setLook(false)} onFound={(ref) => { const n = new URLSearchParams(sp); n.set("user_ref", ref); setSp(n); setLook(false); }} />}
  </Card>;
}

function Lookup({ onClose, onFound }: { onClose: () => void; onFound: (ref: string) => void }) {
  const { t } = useT(); const [u, setU] = useState(""); const [r, setR] = useState("");
  const m = useMutation({ mutationFn: () => post("/rum/lookup", { user: u, reason: r }), onSuccess: (x: any) => onFound(x.user_ref) });
  return <Modal title={t("rum_lookup")} onClose={onClose}>
    <form className="stack" onSubmit={(e) => { e.preventDefault(); m.mutate(); }}>
      <p className="muted small">{t("rum_lookup_hint")}</p>
      <Field label={t("rum_lookup_user")}><input className="input" value={u} onChange={(e) => setU(e.target.value)} required autoComplete="off" /></Field>
      <Field label={t("rum_lookup_reason")}><input className="input" value={r} onChange={(e) => setR(e.target.value)} required minLength={5} /></Field>
      <Err e={m.error} /><div className="row"><Btn kind="primary" type="submit" disabled={m.isPending}>{t("rum_lookup_go")}</Btn></div>
    </form>
  </Modal>;
}

function snippet(platform: string, base: string, key: string): string {
  if (platform === "web") return `<script src="${base}/rum.js" data-key="${key}" async></script>\n<!-- giriş sonrası: window.watchover.setUser(personelNo)  (sunucuda takma ada çevrilir) -->`;
  if (platform === "android") return `// sensors/android/WatchoverSensor.kt dosyasını projeye ekleyin\nclass App : Application() {\n  override fun onCreate() { super.onCreate()\n    WatchoverSensor.start(this, endpoint = "${base}/mobile", key = "${key}") }\n}\n// OkHttp: OkHttpClient.Builder().addInterceptor(WatchoverSensor.interceptor()).build()\n// giriş sonrası: WatchoverSensor.setUser(personelNo)`;
  if (platform === "ios") return `// sensors/ios/WatchoverSensor.swift dosyasını projeye ekleyin\nWatchoverSensor.start(endpoint: "${base}/mobile", key: "${key}")\nlet session = WatchoverSensor.urlSession()   // ağ çağrıları ölçülür, traceparent eklenir\nWatchoverSensor.screen("Bordro")             // ekran geçişlerinde\nWatchoverSensor.setUser(personelNo)`;
  if (platform === "flutter") return `// sensors/flutter/watchover_sensor.dart\nawait WatchoverSensor.start(endpoint: '${base}/mobile', key: '${key}');\nMaterialApp(navigatorObservers: [WatchoverSensor.observer], ...)\nfinal client = WatchoverSensor.httpClient();   // http paketi yerine`;
  if (platform === "react-native") return `// sensors/react-native/watchover.js\nimport Watchover from './watchover';\nWatchover.start({ endpoint: '${base}/mobile', key: '${key}' });\n// react-navigation: <NavigationContainer onStateChange={Watchover.onNavigationChange}>`;
  return `POST ${base}/mobile?k=${key}`;
}

function Apps({ data }: { data: any }) {
  const { t } = useT(); const { can } = useAuth(); const qc = useQueryClient(); const edit = can("act.rum");
  const [f, setF] = useState({ name: "", platform: "web", origins: "", sample_pct: 100 }); const [show, setShow] = useState<any>(null);
  const add = useMutation({ mutationFn: () => post("/rum/apps", f), onSuccess: (a: any) => { setShow(a); setF({ name: "", platform: "web", origins: "", sample_pct: 100 }); qc.invalidateQueries({ queryKey: ["rum-apps"] }); } });
  const upd = useMutation({ mutationFn: ({ id, body }: any) => patch(`/rum/apps/${id}`, body), onSuccess: () => qc.invalidateQueries({ queryKey: ["rum-apps"] }) });
  const rot = useMutation({ mutationFn: (id: number) => post(`/rum/apps/${id}/rotate`), onSuccess: (a: any) => { setShow(a); qc.invalidateQueries({ queryKey: ["rum-apps"] }); } });
  const rm = useMutation({ mutationFn: (id: number) => del(`/rum/apps/${id}`), onSuccess: () => qc.invalidateQueries({ queryKey: ["rum-apps"] }) });
  const base = data?.receiver ?? "";
  return <div className="stack">
    {edit && <Card title={t("rum_app_new")}>
      <form className="row" style={{ gap: 8, alignItems: "flex-end" }} onSubmit={(e) => { e.preventDefault(); add.mutate(); }}>
        <Field label={t("rum_col_app")}><input className="input" style={{ width: 180 }} value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} required placeholder="ik-mobil" /></Field>
        <Field label={t("rum_platform")}><select className="input" value={f.platform} onChange={(e) => setF({ ...f, platform: e.target.value })}>{["web", "android", "ios", "flutter", "react-native"].map((p) => <option key={p}>{p}</option>)}</select></Field>
        {f.platform === "web" && <Field label={t("rum_origins")}><input className="input" style={{ width: 280 }} value={f.origins} onChange={(e) => setF({ ...f, origins: e.target.value })} placeholder="https://ik.sirket.com.tr, *.sirket.com.tr" /></Field>}
        <Field label={t("rum_sample")}><input className="input" style={{ width: 90 }} type="number" min={1} max={100} value={f.sample_pct} onChange={(e) => setF({ ...f, sample_pct: +e.target.value })} /></Field>
        <Btn kind="primary" type="submit" disabled={add.isPending}>{t("rum_app_add")}</Btn>
      </form><Err e={add.error} />
    </Card>}
    <Card title={t("rum_tab_apps")}>
      {!data?.apps?.length ? <Empty>{t("rum_no_apps")}</Empty> :
        <div className="table-wrap"><table className="table"><thead><tr><th>{t("rum_col_app")}</th><th>{t("rum_platform")}</th><th>{t("rum_key")}</th><th>{t("rum_origins")}</th><th className="right">{t("rum_sample")}</th><th>{t("rum_col_state")}</th><th></th></tr></thead>
          <tbody>{data.apps.map((a: any) => <tr key={a.id}><td><b>{a.name}</b>{a.note ? <div className="dim small">{a.note}</div> : null}</td><td>{PLAT_ICON[a.platform]} {a.platform}</td><td className="mono small">{a.key}</td>
            <td className="small">{a.origins || <span className="dim">{t("rum_any_origin")}</span>}</td><td className="num">{a.sample_pct}%</td>
            <td><span className={`badge ${a.status === "active" ? "green" : ""}`}>{a.status}</span></td>
            <td><div className="row" style={{ gap: 4, flexWrap: "nowrap" }}><Btn sm onClick={() => setShow(a)}>{t("rum_snippet")}</Btn>
              {edit && <><Btn sm onClick={() => upd.mutate({ id: a.id, body: { status: a.status === "active" ? "disabled" : "active" } })}>{a.status === "active" ? t("rum_disable") : t("rum_enable")}</Btn>
                <Confirm onConfirm={() => rot.mutate(a.id)} kind="">{t("rum_rotate")}</Confirm><Confirm onConfirm={() => rm.mutate(a.id)}>🗑</Confirm></>}</div></td></tr>)}</tbody></table></div>}
    </Card>
    <div className="dim small">{t("rum_apps_hint")}</div>
    {show && <Modal title={`${t("rum_snippet")} · ${show.name}`} onClose={() => setShow(null)} wide>
      <div className="stack small"><div className="muted">{t(`rum_snip_${show.platform === "web" ? "web" : "mobile"}`)}</div>
        <pre className="code">{snippet(show.platform, base, show.key)}</pre>
        <div className="dim">{t("rum_snip_kvkk")}</div></div>
    </Modal>}
  </div>;
}
