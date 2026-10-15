/** "Altyapı": CPU / memory / disk / GPU of the servers in scope over the range - fleet gauges with their levels, each server's own
 *  line (the busiest six, legend with last / average, click opens the server), the per-server tables with inline bars (newest
 *  reading, min, average) and the fleet average over time. One call: /api/dash/infra. */
import { useQuery } from "@tanstack/react-query";
import { get } from "../../api";
import { useT } from "../../i18n";
import { BarTable, Gauge, Grid, Panel, SER, TimeChart, seriesCsv, type Col, type Lv } from "./kit";
import { useDash } from "./ctx";

type M = "cpu" | "memory" | "disk" | "gpu";
const pct = (v: number) => `%${Math.round(v)}`;

export function useInfra() {
  const c = useDash();
  return useQuery({ queryKey: ["dash-infra", c.rq], queryFn: () => get(`/dash/infra?${c.rq}&top=10&series_top=6`), refetchInterval: c.ms });
}

export function InfraDash() {
  const { t } = useT(); const c = useDash(); const q = useInfra(); const d = q.data;
  const name: Record<M, string> = { cpu: "CPU", memory: t("db_mem"), disk: "Disk", gpu: "GPU" };
  const ms = (["cpu", "memory", "disk", "gpu"] as M[]).filter((m) => m !== "gpu" || (d?.fleet?.gpu?.hosts ?? 0) > 0);
  const none = !q.isLoading && !(d?.samples > 0); const noTxt = t("db_no_metrics");
  const thr = (h?: string) => ({ to: `/thresholds${h ? `?host=${encodeURIComponent(h)}` : ""}`, label: t("db_x_thr") });
  const hostSer = (m: M) => (d?.series?.[m]?.hosts ?? []).map((h: string, i: number) => ({ key: h, label: h, color: SER[i % SER.length] }));
  const cols = (m: M): Col[] => [
    { k: "host", label: t("db_c_host") }, { k: "pool", label: "Pool" },
    { k: "min", label: t("db_min"), num: true, render: (r) => pct(r.min) }, { k: "avg", label: t("db_avg"), num: true, render: (r) => pct(r.avg) },
    { k: "value", label: t("db_now_v"), bar: { value: (r) => r.value, max: 100, lv: (r) => r.level as Lv, fmt: pct } }];
  const tableCsv = (m: M) => () => ({ name: `watchover-${m}-servers`, cols: ["host", "env", "pool", "now", "min", "avg", "max", "warn", "crit", "level"],
    rows: (d?.top?.[m] ?? []).map((r: any) => [r.host, r.env, r.pool, r.value, r.min, r.avg, r.max, r.warn, r.crit, r.level]) });
  const gw = ms.length === 4 ? 3 : 4;
  return <Grid>
    {ms.map((m) => { const f = d?.fleet?.[m] ?? {};
      return <Panel key={m} w={gw} title={t("db_fleet_m", { m: name[m] })} qs={[q]} empty={none} emptyText={noTxt} explore={thr()}
        csv={() => ({ name: `watchover-${m}-fleet`, cols: ["avg", "max", "worst", "hosts", "warn_hosts", "crit_hosts", "warn_level", "crit_level"], rows: [[f.avg, f.max, f.worst, f.hosts, f.warn_n, f.crit_n, f.warn, f.crit]] })}>
        <Gauge value={f.avg} warn={f.warn} crit={f.crit} title={t("db_fleet_avg")} onClick={() => c.openMetric(m)}
          sub={<span className="dim small">{t("db_g_sub", { max: f.max == null ? "–" : pct(f.max), host: f.worst ?? "–", n: f.hosts ?? 0 })}{f.crit_n ? <b className="bad"> · {t("db_n_crit", { n: f.crit_n })}</b> : f.warn_n ? <b className="warnc"> · {t("db_n_warn", { n: f.warn_n })}</b> : null}</span>} />
      </Panel>; })}

    {(["cpu", "memory"] as M[]).map((m) => <Panel key={"l" + m} w={6} title={t("db_per_host", { m: name[m] })} sub={t("db_top6")} qs={[q]} empty={none || !d?.series?.[m]?.hosts?.length} emptyText={noTxt}
      explore={thr()} csv={() => seriesCsv(`watchover-${m}-per-server`, d?.series?.[m]?.rows ?? [], hostSer(m))}>
      <TimeChart data={d?.series?.[m]?.rows ?? []} series={hostSer(m)} fmt={pct} span={c.span} yMax={100} onLegend={c.openHost} />
    </Panel>)}

    <Panel w={6} title={t("db_disk_full")} sub={t("db_disk_full_s")} qs={[q]} empty={none || !d?.top?.disk?.length} emptyText={noTxt} explore={thr()} csv={tableCsv("disk")}>
      <BarTable rows={d?.top?.disk ?? []} rowKey={(r) => r.host} onRow={(r) => c.openHost(r.host)} cols={[{ k: "host", label: t("db_c_host") }, { k: "env", label: t("sc_env") },
        { k: "value", label: t("db_used"), bar: { value: (r) => r.value, max: 100, lv: (r) => r.level as Lv, fmt: pct } }]} />
    </Panel>
    <Panel w={6} title={t("db_per_host", { m: "Disk" })} sub={t("db_top6")} qs={[q]} empty={none || !d?.series?.disk?.hosts?.length} emptyText={noTxt}
      explore={thr()} csv={() => seriesCsv("watchover-disk-per-server", d?.series?.disk?.rows ?? [], hostSer("disk"))}>
      <TimeChart data={d?.series?.disk?.rows ?? []} series={hostSer("disk")} fmt={pct} span={c.span} yMax={100} onLegend={c.openHost} />
    </Panel>

    {ms.filter((m) => m !== "disk").map((m) => <Panel key={"t" + m} w={ms.length === 4 ? 4 : 6} title={t("db_by_m", { m: name[m] })} qs={[q]} empty={none || !d?.top?.[m]?.length} emptyText={noTxt}
      explore={thr()} csv={tableCsv(m)}>
      <BarTable rows={d?.top?.[m] ?? []} cols={cols(m)} rowKey={(r) => r.host} onRow={(r) => c.openHost(r.host)} />
    </Panel>)}

    {ms.includes("gpu") && <Panel w={12} title={t("db_per_host", { m: "GPU" })} qs={[q]} explore={thr()} csv={() => seriesCsv("watchover-gpu-per-server", d?.series?.gpu?.rows ?? [], hostSer("gpu"))}>
      <TimeChart data={d?.series?.gpu?.rows ?? []} series={hostSer("gpu")} fmt={pct} span={c.span} yMax={100} onLegend={c.openHost} />
    </Panel>}

    <Panel w={12} title={t("db_fleet_trend")} sub={d ? t("db_hosts_n", { n: d.hosts }) : undefined} qs={[q]} empty={none} emptyText={noTxt} explore={thr()}
      csv={() => seriesCsv("watchover-fleet-average", d?.trend ?? [], ms.map((m) => ({ key: m, label: name[m] })))}>
      <TimeChart data={d?.trend ?? []} series={ms.map((m, i) => ({ key: m, label: name[m], color: SER[i] }))} fmt={pct} span={c.span} yMax={100} h={200} />
    </Panel>
  </Grid>;
}
