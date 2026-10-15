import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { get } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";
import { Badge, Card, Empty, Table, Tabs, fmtTs } from "../components/ui";
import { actErr } from "../components/act/ActModeCard";

/** İş kutusu (read-only, stage 1 of docs/design/action-agent.md §13.2): cases and open plain actions ordered by "needs a human
 *  now". The full inbox with the case screen and the step runner comes with WP10. While act_mode = off it explains and links to
 *  System › Durum for act.policy holders. Log-derived text is always rendered as text (§10.2). */
export default function Inbox() {
  const { t, lang } = useT(); const { can } = useAuth();
  const [tab, setTab] = useState<"team" | "mine" | "closed">("team");
  const q = useQuery({ queryKey: ["act-inbox", tab], queryFn: () => get(`/act/inbox?tab=${tab}`), refetchInterval: 15000, retry: false });
  const d = actErr(q.error);
  const st = useQuery({ queryKey: ["act-status"], queryFn: () => get("/act/status"), refetchInterval: 15000, retry: false });
  if (d.code === "act_disabled") return <div className="stack">
    <h2>{t("au_inbox")}</h2>
    <Card><div className="stack small au-off">
      <b>{t("au_off_title")}</b><p className="muted" style={{ margin: 0 }}>{t("au_off_lead")}</p>
      {can("act.policy") && <Link className="btn primary sm" to="/system">{t("au_off_enable")}</Link>}
    </div></Card>
  </div>;
  const rows: any[] = q.data ?? [];
  const kills: any[] = (st.data as any)?.kill ?? [];
  return <div className="stack">
    <div className="row between"><h2>{t("au_inbox")}</h2><span className="chip">{t("au_inbox_ro")}</span></div>
    {kills.map((k) => <div key={`${k.id}-${k.scope}`} className="err au-kill" role="alert">{t("au_kill_banner", { scope: k.scope === "global" ? t("au_kill_global") : `${k.scope} ${k.ref || ""}`.trim(), by: k.by_ || "-", ts: fmtTs(k.ts), reason: k.reason || "-" })}</div>)}
    <Tabs value={tab} onChange={setTab} tabs={[{ k: "team", label: t("au_tab_team") }, { k: "mine", label: t("au_tab_mine") }, { k: "closed", label: t("au_tab_closed") }]} />
    <Card>{q.error && !d.code ? <div className="err">{String((q.error as any)?.message ?? q.error)}</div> : null}
      {d.code && d.code !== "act_disabled" ? <div className="err">{lang === "tr" ? d.tr : d.en}</div> : null}
      <Table empty={<Empty>{t("au_inbox_empty")}</Empty>} cols={[
        { k: "priority_score", label: t("au_col_score"), num: true },
        { k: "severity", label: t("au_col_sev"), render: (r) => <Badge v={r.severity || ""} label={r.severity || "-"} /> },
        { k: "title", label: t("au_col_title"), render: (r) => <div><b>{r.title}</b>
          <div className="muted small">{r.kind === "case" ? t("au_kind_case") : t("au_kind_action")}{r.hits > 1 ? ` · ${t("au_hits", { n: r.hits })}` : ""}
            {r.children ? ` · ${t("au_storm", { n: r.children })}` : ""}{r.next_step ? ` · ${r.next_step}` : ""}</div></div> },
        { k: "host", label: t("au_col_host"), render: (r) => <span className="mono">{r.host || "-"}</span> },
        { k: "phase", label: t("au_col_phase"), render: (r) => r.kind === "case" ? t(`au_ph_${r.phase}`) : t("st_" + r.status) },
        { k: "owner", label: t("owner"), render: (r) => r.owner || <span className="dim">-</span> },
        { k: "last_seen", label: t("time"), render: (r) => <span className="muted small">{fmtTs(r.last_seen)}</span> },
      ]} rows={rows} />
    </Card>
  </div>;
}
