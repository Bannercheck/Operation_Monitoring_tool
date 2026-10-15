/** "Uygulama": requests, error rate and latency of the scope - headline stats with their change, lines per pool (stacked) and the
 *  pools' error rate, p50 / p90 / p99, the slowest servers by p95, and the pools / services tables (click: the service's traces or
 *  the server). The service variable marks its row and carries into "Keşfet". */
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { get } from "../../api";
import { useT } from "../../i18n";
import { delta, fmtN, fmtPct } from "../ops/util";
import { BarTable, Grid, Panel, SER, Stat, TimeChart, seriesCsv, useCanOpen, type Lv } from "./kit";
import { useDash } from "./ctx";

const ms = (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)} s` : `${Math.round(v)} ms`);

export function poolLabel(t: any, p: string) { return p === "~other" ? t("db_other") : p; }

export function AppDash() {
  const { t, lang } = useT(); const c = useDash(); const nav = useNavigate(); const canOpen = useCanOpen();
  const sum = useQuery({ queryKey: ["range-sum", c.rq], queryFn: () => get(`/live/range/summary?${c.rq}`), refetchInterval: c.ms });
  const lat = useQuery({ queryKey: ["range-lat", c.rq], queryFn: () => get(`/live/range/latency?${c.rq}`), refetchInterval: c.ms });
  const pools = useQuery({ queryKey: ["dash-pools", c.rq], queryFn: () => get(`/dash/pools?${c.rq}&top=6`), refetchInterval: c.ms });
  const det = useQuery({ queryKey: ["range-detail-dash", c.rq], queryFn: () => get(`/live/range/detail?${c.rq}`), refetchInterval: c.ms ? Math.max(Number(c.ms), 30000) : false });
  const hp = useQuery({ queryKey: ["range-hs", c.rq, "p95"], queryFn: () => get(`/live/range/hostseries?${c.rq}&metric=p95&top=6`), refetchInterval: c.ms });
  const s = sum.data; const p = pools.data;
  const minutes = s?.minutes || c.minutes || 1; const traces = (svc?: string) => `/traces?minutes=${Math.max(5, minutes)}${svc ? `&service=${encodeURIComponent(svc)}` : ""}`;
  const pser = (p?.names ?? []).map((n: string, i: number) => ({ key: n, label: poolLabel(t, n), color: n === "~other" ? "var(--dim)" : SER[i % SER.length] }));   // the folded tail stays neutral grey
  const lser = [{ key: "p50", label: "p50", color: "var(--lc-p50)" }, { key: "p90", label: "p90", color: "var(--lc-p90)" }, { key: "p99", label: "p99", color: "var(--lc-p99)" }];
  const hser = (hp.data?.hosts ?? []).map((h: any) => h.host).sort().map((h: string, i: number) => ({ key: h, label: h, color: SER[i % SER.length] }));
  const dT = delta(s?.per_min, s?.prev?.per_min); const dE = delta(s?.error_rate, s?.prev?.error_rate); const dP = delta(s?.p95_ms, s?.prev?.p95_ms);
  const errT = (r: number | null | undefined): Lv => (r == null ? "none" : r >= 0.05 ? "crit" : r >= 0.01 ? "warn" : "ok");
  const services = (det.data?.services ?? []) as any[];
  const svcRows = c.svc ? [...services.filter((x) => x.service === c.svc), ...services.filter((x) => x.service !== c.svc)] : services;
  const latRows = (lat.data?.series ?? []) as any[]; const noLat = !latRows.some((r) => r.p50 != null);
  return <Grid>
    <Panel w={3} title={t("db_req")} qs={[sum]} explore={{ to: "/services", label: t("db_x_services") }}>
      <Stat value={fmtN(s?.per_min, lang)} unit={`/${t("tr_min")}`} delta={{ text: dT.text, good: null }} sub={<span>{fmtN(s?.total, lang)} {t("ops_k_lines")}</span>}
        spark={(s?.series ?? []).map((x: any) => x.total)} color="var(--s1)" onClick={() => c.openRange("volume")} />
    </Panel>
    <Panel w={3} title={t("ops_k_err")} qs={[sum]} explore={{ go: () => c.goLogs({ sev: "ERROR" }), label: t("db_x_logs") }}>
      <Stat value={fmtPct(s?.error_rate, 2)} tone={errT(s?.error_rate)} delta={{ text: dE.text, good: dE.dir ? dE.dir === "down" : null }} sub={<span>{fmtN(s?.errors, lang)} {t("ops_k_errlines")}</span>}
        spark={(s?.series ?? []).map((x: any) => (x.total ? x.errors / x.total : null))} color="var(--st-crit)" onClick={() => c.openRange("errors")} />
    </Panel>
    <Panel w={3} title={t("db_p95")} qs={[sum, lat]} explore={{ to: traces(c.svc), label: t("db_x_traces") }}>
      <Stat value={s?.p95_ms != null ? ms(s.p95_ms) : "–"} delta={{ text: dP.text, good: dP.dir ? dP.dir === "down" : null }} sub={<span>p99 {latRows.length && latRows[latRows.length - 1]?.p99 != null ? ms(latRows[latRows.length - 1].p99) : "–"}</span>}
        spark={latRows.map((x) => x.p90)} color="var(--lc-p90)" onClick={() => c.openRange("latency")} />
    </Panel>
    <Panel w={3} title={t("ops_k_avail")} qs={[sum]} explore={{ to: "/services", label: t("db_x_services") }}>
      <Stat value={fmtPct(s?.availability, 2)} tone={s?.availability == null ? "" : s.availability >= 0.999 ? "ok" : s.availability >= 0.99 ? "warn" : "crit"}
        sub={<span>{t("ops_k_target")} %99,9</span>} spark={(s?.series ?? []).map((x: any) => (x.total ? 1 - x.errors / x.total : null))} color="var(--st-ok)" onClick={() => c.openRange("avail")} />
    </Panel>

    <Panel w={6} title={t("db_req_pool")} sub={t("db_stacked")} qs={[pools]} empty={!pser.length} explore={{ go: () => c.goLogs({}), label: t("db_x_logs") }} csv={() => seriesCsv("watchover-lines-per-pool", p?.series ?? [], pser)}>
      <TimeChart data={p?.series ?? []} series={pser} kind="area" stacked fmt={(v) => fmtN(v, lang)} span={c.span} onLegend={(k) => k !== "~other" && c.st.setPool(k)} />
    </Panel>
    <Panel w={6} title={t("db_err_pool")} qs={[pools]} empty={!pser.length} explore={{ go: () => c.goLogs({ sev: "ERROR" }), label: t("db_x_logs") }} csv={() => seriesCsv("watchover-error-rate-per-pool", p?.error_rate ?? [], pser)}>
      <TimeChart data={p?.error_rate ?? []} series={pser} fmt={(v) => fmtPct(v, v < 0.01 ? 2 : 1)} span={c.span} onLegend={(k) => k !== "~other" && c.st.setPool(k)} />
    </Panel>

    <Panel w={6} title={t("db_latency")} sub="p50 · p90 · p99" qs={[lat]} empty={noLat} emptyText={t("db_no_lat")} explore={{ to: traces(c.svc), label: t("db_x_traces") }} csv={() => seriesCsv("watchover-latency", latRows, lser)}>
      <TimeChart data={latRows} series={lser} fmt={ms} span={c.span} />
    </Panel>
    <Panel w={6} title={t("db_p95_host")} sub={t("db_top6")} qs={[hp]} empty={!hser.length} emptyText={t("db_no_lat")} explore={{ to: "/inventory", label: t("db_x_inv") }} csv={() => seriesCsv("watchover-p95-per-server", hp.data?.series ?? [], hser)}>
      <TimeChart data={hp.data?.series ?? []} series={hser} fmt={ms} span={c.span} onLegend={c.openHost} />
    </Panel>

    <Panel w={5} title={t("db_pools")} qs={[pools]} empty={!(p?.pools ?? []).length} explore={{ to: "/inventory", label: t("db_x_inv") }}
      csv={() => ({ name: "watchover-pools", cols: ["pool", "lines", "errors", "error_rate"], rows: (p?.pools ?? []).map((r: any) => [r.pool, r.lines, r.errors, r.error_rate]) })}>
      <BarTable rows={p?.pools ?? []} rowKey={(r) => r.pool} onRow={(r) => r.pool !== "~other" && c.st.setPool(r.pool)} cols={[
        { k: "pool", label: "Pool", render: (r) => <span className="row" style={{ gap: 6, flexWrap: "nowrap" }}><i className="sw" style={{ background: pser.find((x: any) => x.key === r.pool)?.color }} />{poolLabel(t, r.pool)}</span> },
        { k: "lines", label: t("lc_lines"), num: true, render: (r) => fmtN(r.lines, lang) },
        { k: "error_rate", label: t("ops_k_err"), bar: { value: (r) => r.error_rate, max: 0.1, lv: (r) => errT(r.error_rate), fmt: (v) => fmtPct(v, 2) } }]} />
    </Panel>
    <Panel w={7} title={t("db_services")} sub={c.svc ? `${t("db_svc")}: ${c.svc}` : undefined} qs={[det]} empty={!services.length} explore={{ to: c.svc ? `/services/${encodeURIComponent(c.svc)}` : "/services", label: t("db_x_services") }}
      csv={() => ({ name: "watchover-services", cols: ["service", "lines", "per_min", "errors", "error_rate", "p95_ms"], rows: services.map((r) => [r.service, r.lines, r.per_min, r.errors, r.error_rate, r.p95_ms]) })}>
      <BarTable rows={svcRows.slice(0, 12)} rowKey={(r) => r.service} sel={c.svc} onRow={canOpen("/traces") ? (r) => nav(traces(r.service)) : undefined} cols={[
        { k: "service", label: t("db_svc") }, { k: "per_min", label: `/${t("tr_min")}`, num: true, render: (r) => fmtN(r.per_min, lang) },
        { k: "p95_ms", label: "p95", num: true, render: (r) => (r.p95_ms == null ? "–" : ms(r.p95_ms)) },
        { k: "error_rate", label: t("ops_k_err"), bar: { value: (r) => r.error_rate, max: 0.1, lv: (r) => errT(r.error_rate), fmt: (v) => fmtPct(v, 2) } }]} />
    </Panel>
  </Grid>;
}

