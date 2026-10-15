/** "Kullanıcı deneyimi": sessions on the servers and in the web / mobile apps, crash-free and error-free shares, active sessions over
 *  time and per server, Web Vitals p75 per app with their grade in words, the slowest steps and the apps table. Sources: the range's
 *  sessions (/live/range/sessions) and the user sensors (/rum/overview, with page.rum). */
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { get } from "../../api";
import { useAuth } from "../../auth";
import { useT } from "../../i18n";
import { fmtN, fmtPct } from "../ops/util";
import { BarTable, Grid, Panel, Stat, TimeChart, seriesCsv, type Lv } from "./kit";
import { useDash } from "./ctx";

const POOR: Record<string, number> = { lcp: 4000, inp: 500, cls: 0.25, fcp: 3000, ttfb: 1800 };
const GL: Record<string, Lv> = { good: "ok", needs: "warn", poor: "crit" };
const vfmt = (k: string, v: number) => (k === "cls" ? v.toFixed(2) : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`);
const ms = (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`);

export function UxDash() {
  const { t, lang } = useT(); const c = useDash(); const { can } = useAuth(); const nav = useNavigate();
  const sess = useQuery({ queryKey: ["range-sessions-dash", c.rq], queryFn: () => get(`/live/range/sessions?${c.rq}&limit=20`), refetchInterval: c.ms });
  const rumOk = can("page.rum"); const mins = Math.max(5, Math.round(c.minutes || 60));
  const rum = useQuery({ queryKey: ["rum-ov-dash", mins], queryFn: () => get(`/rum/overview?minutes=${mins}`), refetchInterval: c.ms ? Math.max(Number(c.ms), 30000) : false, enabled: rumOk });
  const sd = sess.data; const r = rum.data; const apps = (r?.apps ?? []) as any[];
  const S = apps.reduce((a, x) => ({ n: a.n + (x.sessions || 0), u: a.u + (x.users || 0), cr: a.cr + (x.crashed || 0), bad: a.bad + (x.bad || 0) }), { n: 0, u: 0, cr: 0, bad: 0 });
  const vit = Object.entries(r?.vitals ?? {}).flatMap(([app, d]: [string, any]) => Object.entries(d).map(([k, v]: [string, any]) => ({ key: `${app}·${k}`, app, vital: k, ...v })))
    .sort((a, b) => (b.p75 ?? 0) / POOR[b.vital] - (a.p75 ?? 0) / POOR[a.vital]);
  const steps = (r?.slow_steps ?? []) as any[];
  const act = (sd?.active_series ?? []) as any[];
  const noRum = rumOk ? t("db_no_rum") : t("db_no_perm");
  const freeLv = (v: number | null): Lv => (v == null ? "none" : v >= 0.995 ? "ok" : v >= 0.98 ? "warn" : "crit");
  const rumLink = { to: `/rum?minutes=${mins}`, label: t("db_x_rum") };
  return <Grid>
    <Panel w={3} title={t("db_sessions")} qs={[sess]} explore={{ go: () => c.goLogs({}), label: t("db_x_logs") }}>
      <Stat value={fmtN(sd?.total, lang)} sub={<span>{t("db_on_servers")}{sd?.by_host?.length ? ` · ${sd.by_host.length} ${t("hm_hosts")}` : ""}</span>} spark={act.map((x) => x.n)} color="var(--s1)" />
    </Panel>
    <Panel w={3} title={t("db_app_sessions")} qs={rumOk ? [rum] : []} explore={rumLink}>
      <Stat value={rumOk ? fmtN(S.n, lang) : "–"} sub={<span>{rumOk ? `${fmtN(S.u, lang)} ${t("db_users")}` : t("db_no_perm")}</span>} onClick={rumOk ? () => nav(rumLink.to) : undefined} />
    </Panel>
    <Panel w={3} title={t("db_crash_free")} qs={rumOk ? [rum] : []} explore={rumLink}>
      <Stat value={S.n ? fmtPct(1 - S.cr / S.n, 2) : "–"} tone={S.n ? (freeLv(1 - S.cr / S.n) === "ok" ? "ok" : freeLv(1 - S.cr / S.n)) : ""} sub={<span>{S.cr ? t("db_crashed_n", { n: S.cr }) : t("db_no_crash")}</span>} onClick={rumOk ? () => nav(rumLink.to) : undefined} />
    </Panel>
    <Panel w={3} title={t("db_error_free")} qs={rumOk ? [rum] : []} explore={rumLink}>
      <Stat value={S.n ? fmtPct(1 - S.bad / S.n, 1) : "–"} tone={S.n ? (freeLv(1 - S.bad / S.n) === "ok" ? "ok" : "warn") : ""} sub={<span>{t("db_bad_n", { n: S.bad })}</span>} onClick={rumOk ? () => nav(rumLink.to) : undefined} />
    </Panel>

    <Panel w={6} title={t("lc_active")} qs={[sess]} empty={!act.some((x) => x.n)} explore={{ go: () => c.goLogs({}), label: t("db_x_logs") }} csv={() => seriesCsv("watchover-active-sessions", act, [{ key: "n", label: "sessions" }])}>
      <TimeChart data={act} series={[{ key: "n", label: t("lc_active"), color: "var(--s1)" }]} kind="area" fmt={(v) => fmtN(v, lang)} span={c.span} legend="none" />
    </Panel>
    <Panel w={6} title={t("db_sess_host")} qs={[sess]} empty={!(sd?.by_host ?? []).length} explore={{ to: "/inventory", label: t("db_x_inv") }}
      csv={() => ({ name: "watchover-sessions-per-server", cols: ["host", "sessions", "users", "errors"], rows: (sd?.by_host ?? []).map((h: any) => [h.host, h.sessions, h.users, h.errors]) })}>
      <BarTable rows={(sd?.by_host ?? []).slice(0, 10)} rowKey={(r) => r.host} onRow={(r) => c.openHost(r.host)} cols={[{ k: "host", label: t("db_c_host") },
        { k: "users", label: t("db_users"), num: true }, { k: "errors", label: "ERROR+", num: true },
        { k: "sessions", label: t("db_sessions"), bar: { value: (r) => r.sessions, color: "var(--s1)", fmt: (v) => fmtN(v, lang) } }]} />
    </Panel>

    <Panel w={6} title={t("db_vitals")} sub="p75" qs={rumOk ? [rum] : []} empty={!vit.length} emptyText={noRum} explore={rumLink}
      csv={() => ({ name: "watchover-web-vitals", cols: ["app", "vital", "p75", "samples", "grade"], rows: vit.map((v) => [v.app, v.vital, v.p75, v.n, v.grade]) })}>
      <BarTable rows={vit.slice(0, 12)} rowKey={(r) => r.key} onRow={() => nav(rumLink.to)} cols={[{ k: "app", label: t("db_app") }, { k: "vital", label: "Vital", render: (v) => <b className="mono">{v.vital.toUpperCase()}</b> },
        { k: "grade", label: t("db_grade"), render: (v) => <span className={`lvl lv-${GL[v.grade] ?? "none"}`}><i />{t("db_gr_" + (v.grade ?? "none"))}</span> },
        { k: "p75", label: "p75", bar: { value: (v) => (v.p75 == null ? null : Math.min(1.25, v.p75 / POOR[v.vital]) * 100), max: 125, lv: (v) => GL[v.grade] ?? "none", fmt: () => "" } },
        { k: "v", label: "", num: true, render: (v) => (v.p75 == null ? "–" : vfmt(v.vital, v.p75)) }]} />
    </Panel>
    <Panel w={6} title={t("db_slow_steps")} sub="p75" qs={rumOk ? [rum] : []} empty={!steps.length} emptyText={noRum} explore={rumLink}
      csv={() => ({ name: "watchover-slowest-steps", cols: ["app", "step", "name", "count", "p50", "p75", "p95"], rows: steps.map((s) => [s.app, s.step, s.name, s.count, s.p50, s.p75, s.p95]) })}>
      <BarTable rows={steps.slice(0, 10)} rowKey={(r) => `${r.app}${r.name}${r.step}`} onRow={() => nav(rumLink.to)} cols={[{ k: "name", label: t("db_step"), render: (s) => <span className="ell" title={s.name}>{s.name}</span> },
        { k: "app", label: t("db_app"), render: (s) => <span className="dim">{s.app}</span> }, { k: "count", label: t("db_count"), num: true },
        { k: "p75", label: "p75", bar: { value: (s) => s.p75, color: "var(--s2)", fmt: ms } }]} />
    </Panel>

    <Panel w={12} title={t("db_apps")} qs={rumOk ? [rum] : []} empty={!apps.length} emptyText={noRum} explore={rumLink}
      csv={() => ({ name: "watchover-apps", cols: ["app", "platform", "sessions", "users", "errors", "crashes", "crash_free", "error_free"], rows: apps.map((a) => [a.app, a.platform, a.sessions, a.users, a.errors, a.crashes, a.crash_free, a.error_free]) })}>
      <BarTable rows={apps} rowKey={(r) => `${r.app}${r.platform}`} onRow={() => nav(rumLink.to)} cols={[{ k: "app", label: t("db_app") }, { k: "platform", label: t("db_platform"), render: (a) => <span className="dim">{a.platform}</span> },
        { k: "sessions", label: t("db_sessions"), num: true, render: (a) => fmtN(a.sessions, lang) }, { k: "users", label: t("db_users"), num: true, render: (a) => fmtN(a.users, lang) },
        { k: "errors", label: "ERROR+", num: true }, { k: "crash_free", label: t("db_crash_free"), bar: { value: (a) => (a.crash_free == null ? null : a.crash_free * 100), max: 100, lv: (a) => freeLv(a.crash_free), fmt: (v) => `%${v.toFixed(1)}` } }]} />
    </Panel>
  </Grid>;
}

