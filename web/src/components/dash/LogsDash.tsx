/** "Loglar": volume by level per bar (stacked) and its split, the error rate over time, the servers and services that write the
 *  most, the most frequent error groups (click: the explorer on that group), and the compact log card. The service variable
 *  narrows the groups and is carried into the explorer. */
import { useQuery } from "@tanstack/react-query";
import { get } from "../../api";
import { useT } from "../../i18n";
import { fmtN, fmtPct } from "../ops/util";
import { BarTable, Donut, Grid, Panel, ST, TimeChart, seriesCsv } from "./kit";
import { useDash } from "./ctx";
import type { ReactNode } from "react";

export function LogsDash({ logs }: { logs: ReactNode }) {
  const { t, lang } = useT(); const c = useDash();
  const sum = useQuery({ queryKey: ["range-sum", c.rq], queryFn: () => get(`/live/range/summary?${c.rq}`), refetchInterval: c.ms });
  const grp = useQuery({ queryKey: ["range-groups-err", c.rq, c.svc], queryFn: () => get(`/live/range/groups?${c.rq}&severity=ERROR&limit=12${c.svc ? `&service=${encodeURIComponent(c.svc)}` : ""}`), refetchInterval: c.ms });
  const hs = useQuery({ queryKey: ["range-hs", c.rq, "lines8"], queryFn: () => get(`/live/range/hostseries?${c.rq}&metric=lines&top=8`), refetchInterval: c.ms });
  const s = sum.data;
  const lv = (s?.series ?? []).map((b: any) => ({ t: b.t, info: Math.max(0, b.total - b.errors - (b.warn || 0)), warn: b.warn ?? null, error: b.errors, rate: b.total ? b.errors / b.total : null }));
  const ser = [{ key: "info", label: "INFO", color: "var(--s1)" }, { key: "warn", label: "WARN", color: ST.warn }, { key: "error", label: "ERROR+", color: ST.crit }];
  const tot = lv.reduce((a: any, b: any) => ({ info: a.info + b.info, warn: a.warn + (b.warn || 0), error: a.error + b.error }), { info: 0, warn: 0, error: 0 });
  const groups = (grp.data?.groups ?? []) as any[]; const svcs = (s?.services ?? []) as [string, number][];
  const none = !q0(sum) && !s?.total;
  return <Grid>
    <Panel w={8} title={t("db_vol_level")} sub={t("ops_step", { m: s?.step_min ?? 1 })} qs={[sum]} empty={none} explore={{ go: () => c.goLogs({}), label: t("db_x_logs") }} csv={() => seriesCsv("watchover-volume-by-level", lv, ser)}>
      <TimeChart data={lv} series={ser} kind="bar" stacked fmt={(v) => fmtN(v, lang)} span={c.span} onPick={(x) => c.st.setAbs(new Date(x * 1000), new Date((x + (s?.step_min ?? 1) * 60) * 1000))} pickLabel={t("db_zoom")}
        onLegend={(k) => c.goLogs({ sev: k === "error" ? "ERROR" : k === "warn" ? "WARN" : "INFO" })} />
    </Panel>
    <Panel w={4} title={t("db_level_split")} qs={[sum]} empty={none} explore={{ go: () => c.goLogs({}), label: t("db_x_logs") }}
      csv={() => ({ name: "watchover-level-split", cols: ["level", "lines"], rows: [["INFO", tot.info], ["WARN", tot.warn], ["ERROR+", tot.error]] })}>
      <Donut parts={ser.map((x) => ({ key: x.key, label: x.label, value: (tot as any)[x.key], color: x.color }))} center={<b>{fmtN(s?.total, lang)}</b>} label={t("ops_k_lines")}
        onPick={(k) => c.goLogs({ sev: k === "error" ? "ERROR" : k === "warn" ? "WARN" : "INFO" })} />
    </Panel>

    <Panel w={6} title={t("db_err_trend")} qs={[sum]} empty={none} explore={{ go: () => c.goLogs({ sev: "ERROR" }), label: t("db_x_logs") }} csv={() => seriesCsv("watchover-error-rate", lv, [{ key: "rate", label: "error_rate" }])}>
      <TimeChart data={lv} series={[{ key: "rate", label: t("ops_k_err"), color: ST.crit }]} kind="area" fmt={(v) => fmtPct(v, v < 0.01 ? 2 : 1)} span={c.span} legend="none" />
    </Panel>
    <Panel w={6} title={t("db_top_hosts")} qs={[hs]} empty={!(hs.data?.hosts ?? []).length} explore={{ to: "/inventory", label: t("db_x_inv") }}
      csv={() => ({ name: "watchover-top-servers", cols: ["host", "lines"], rows: (hs.data?.hosts ?? []).map((h: any) => [h.host, h.total]) })}>
      <BarTable rows={hs.data?.hosts ?? []} rowKey={(r) => r.host} onRow={(r) => c.openHost(r.host)} cols={[{ k: "host", label: t("db_c_host") },
        { k: "total", label: t("lc_lines"), bar: { value: (r) => r.total, color: "var(--s1)", fmt: (v) => fmtN(v, lang) } }]} />
    </Panel>

    <Panel w={4} title={t("db_top_svcs")} qs={[sum]} empty={!svcs.length} explore={{ to: "/services", label: t("db_x_services") }}
      csv={() => ({ name: "watchover-top-services", cols: ["service", "lines"], rows: svcs.map((x) => [x[0], x[1]]) })}>
      <BarTable rows={svcs.map(([service, n]) => ({ service, n }))} rowKey={(r) => r.service} sel={c.svc} onRow={(r) => c.goLogs({ service: r.service })} cols={[{ k: "service", label: t("db_svc") },
        { k: "n", label: t("lc_lines"), bar: { value: (r) => r.n, color: "var(--s3)", fmt: (v) => fmtN(v, lang) } }]} />
    </Panel>
    <Panel w={8} title={t("db_err_groups")} sub={c.svc ? `${t("db_svc")}: ${c.svc}` : grp.data ? t("db_groups_n", { n: grp.data.distinct ?? groups.length }) : undefined} qs={[grp]} empty={!groups.length} emptyText={t("db_no_err")}
      explore={{ go: () => c.goLogs({ tab: "groups", sev: "ERROR", service: c.svc }), label: t("db_x_logs") }}
      csv={() => ({ name: "watchover-error-groups", cols: ["template", "service", "count", "hosts", "first", "last"], rows: groups.map((g) => [g.template, g.service, g.count, g.host_count, g.first, g.last]) })}>
      <BarTable rows={groups} rowKey={(r) => String(r.id ?? r.template)} onRow={(g) => c.goLogs({ tab: "groups", sev: "ERROR", service: g.service === "-" ? "" : g.service, q: "" })} cols={[
        { k: "template", label: t("db_msg"), render: (g) => <span className="mono ell" title={g.sample}>{g.template}</span> },
        { k: "service", label: t("db_svc"), render: (g) => <span className="dim">{g.service}</span> },
        { k: "host_count", label: t("hm_hosts"), num: true },
        { k: "count", label: t("db_count"), bar: { value: (g) => g.count, color: ST.crit, fmt: (v) => fmtN(v, lang) } }]} />
    </Panel>
    {logs}
  </Grid>;
}

const q0 = (q: { isLoading: boolean }) => q.isLoading;
