import { useQuery } from "@tanstack/react-query";
import { get } from "../api";
import { useAuth } from "../auth";
import { useT } from "../i18n";

/** Which team a new record belongs to. A team member who holds `perm` in one team needs no choice (the server picks it); with
 *  several teams a select appears. Installation admins may file a record under any team or keep it installation-wide. */
export function TeamPick({ perm, value, onChange, style }: { perm: string; value: number | null; onChange: (v: number | null) => void; style?: React.CSSProperties }) {
  const { t } = useT(); const { user, can, teamsWith } = useAuth();
  const admin = !!user && user.all !== false && can("page.teams");
  const all = useQuery({ queryKey: ["teams"], queryFn: () => get("/teams"), enabled: admin, staleTime: 60000 });
  if (!user) return null;
  if (user.all !== false) {
    const teams: any[] = all.data?.teams ?? [];
    if (!admin || !teams.length) return null;
    return <select className="input team-pick" style={style} value={value ?? ""} onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)} title={t("tmr_team")}>
      <option value="">{t("tmr_install_wide")}</option>{teams.map((x) => <option key={x.id} value={x.id}>👥 {x.name}</option>)}</select>;
  }
  const ids = teamsWith(perm);
  if (ids.length < 2) return null;
  const mine = (user.teams ?? []).filter((m) => ids.includes(m.id));
  return <select className="input team-pick" style={style} value={value ?? ""} required onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)} title={t("tmr_team_required")}>
    <option value="">{t("tmr_team")}…</option>{mine.map((m) => <option key={m.id} value={m.id}>👥 {m.name}</option>)}</select>;
}

/** The owner of a row: a team chip, or "Kurulum geneli" (installation-wide). */
export function TeamChip({ name }: { name?: string | null }) {
  const { t } = useT();
  return name ? <span className="chip small team-chip">👥 {name}</span> : <span className="dim small">{t("tmr_install_wide")}</span>;
}
