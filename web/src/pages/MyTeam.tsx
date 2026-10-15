import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { get } from "../api";
import { useT } from "../i18n";
import { Empty, Err } from "../components/ui";
import { TeamEditor, type Team } from "./Teams";

/** "Ekibim": a team manager manages their own team (members by e-mail, roles within their own permissions, the team's hosts and
 *  enrolment key). Rules, the manager role and other teams stay with installation admins. */
export default function MyTeam() {
  const { t } = useT();
  const q = useQuery({ queryKey: ["my-teams"], queryFn: () => get<{ teams: Team[] }>("/teams") });
  const [sel, setSel] = useState<number | null>(null);
  const teams = q.data?.teams ?? [];
  const tid = sel ?? teams[0]?.id;
  return <div className="stack">
    <div className="row between"><h2>👥 {t("tmr_my_team")}</h2>
      {teams.length > 1 && <div className="seg">{teams.map((x) => <button key={x.id} className={x.id === tid ? "on" : ""} onClick={() => setSel(x.id)}>{x.name}</button>)}</div>}</div>
    {q.error ? <Err e={q.error} /> : !q.data ? <div className="muted">…</div> : !tid ? <Empty>{t("tmr_none_mine")}</Empty> :
      <div className="card"><TeamEditor key={tid} tid={tid} /></div>}
  </div>;
}
