import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { get, post } from "../../api";
import { useT } from "../../i18n";
import { Btn, Collapsible } from "../ui";
import { actErr } from "./ActModeCard";

/** System › Durum: the action agents (wo-agent act) that said hello, and "Eylem anahtarını onayla" (docs/design/action-agent.md
 *  §9.3.1, WP11c). A person compares the fingerprint the server printed on its console with the one reported here; until then
 *  the host gets no job. Shown only while act_mode is on (the agents stay dormant otherwise). */
export function ActActuatorsCard() {
  const { t, lang } = useT(); const qc = useQueryClient();
  const st = useQuery({ queryKey: ["act-status"], queryFn: () => get("/act/status") });
  const on = !!(st.data as any)?.enabled;
  const list = useQuery({ queryKey: ["act-actuators"], queryFn: () => get("/act/actuators"), enabled: on, refetchInterval: 15000 });
  const [fp, setFp] = useState<Record<number, string>>({});
  const confirm = useMutation({
    mutationFn: (id: number) => post(`/act/actuators/${id}/confirm-key`, { fingerprint: (fp[id] || "").trim() }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["act-actuators"] }),
  });
  const revoke = useMutation({
    mutationFn: (id: number) => post(`/act/actuators/${id}/revoke-key`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["act-actuators"] }),
  });
  const d = actErr(confirm.error);
  const rows: any[] = (list.data as any[]) || [];
  return <Collapsible id="sys-actuators" title={`🔑 ${t("au_act_title")}`}>
    <div className="stack small">
      <p className="muted" style={{ margin: 0 }}>{t(on ? "au_act_lead" : "au_act_off")}</p>
      {on && rows.length === 0 && <div className="dim">{t("au_act_none")}</div>}
      {on && rows.length > 0 && <div className="table-wrap"><table className="table">
        <thead><tr><th>{t("au_col_host")}</th><th>{t("au_act_state")}</th><th>{t("au_act_fp")}</th><th /></tr></thead>
        <tbody>{rows.map((r) => {
          const x = r.extra || {}; const pins = x.pins || {};
          return <tr key={r.agent_id}>
            <td><b>{r.host}</b> <span className="dim">{r.version || ""}{x.arch ? ` · ${x.arch}` : ""}</span>
              {x.sim ? <span className="badge" style={{ marginLeft: 6 }}>{t("au_act_sim")}</span> : null}
              {r.insecure_install ? <span className="badge red" style={{ marginLeft: 6 }}>{t("au_act_insecure")}</span> : null}
              {r.allowlist_ok === 0 ? <span className="badge red" style={{ marginLeft: 6 }}>{t("au_act_perms")}</span> : null}
              {r.halt ? <span className="badge red" style={{ marginLeft: 6 }}>{t("au_act_halt")}</span> : null}
              <div className="dim">{t("au_act_templates", { n: (r.templates || []).length })} · max_risk {r.policy?.max_risk ?? "-"}
                {pins.source === "tofu" ? ` · ${t("au_act_pin_tofu")}` : pins.source === "install" ? ` · ${t("au_act_pin_install")}` : ""}</div></td>
            <td>{r.key_status === "confirmed" ? <span className="badge green">{t("au_act_confirmed")}</span>
              : <span className="badge">{t(r.key_status === "revoked" ? "au_act_revoked" : "au_act_unconfirmed")}</span>}</td>
            <td><code style={{ fontSize: 12 }}>{r.fingerprint || "-"}</code></td>
            <td>{r.key_status === "unconfirmed" && r.fingerprint
              ? <form className="row" onSubmit={(e) => { e.preventDefault(); confirm.mutate(r.agent_id); }}>
                <input className="input" style={{ width: 230 }} placeholder={t("au_act_paste")} aria-label={t("au_act_paste")}
                  value={fp[r.agent_id] || ""} onChange={(e) => setFp({ ...fp, [r.agent_id]: e.target.value })} required />
                <Btn sm kind="primary" type="submit" disabled={confirm.isPending}>{t("au_act_confirm")}</Btn>
              </form>
              : r.key_status === "confirmed" ? <Btn sm type="button" disabled={revoke.isPending} onClick={() => revoke.mutate(r.agent_id)}>{t("au_act_revoke")}</Btn> : null}
            </td>
          </tr>;
        })}</tbody>
      </table></div>}
      {d.code && <div className="err">{d.code === "act_key_mismatch" ? t("au_act_mismatch") : (lang === "tr" ? d.tr : d.en)}</div>}
      <p className="dim" style={{ margin: 0 }}>{t("au_act_note")}</p>
    </div>
  </Collapsible>;
}
