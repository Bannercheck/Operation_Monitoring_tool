import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { get, put } from "../api";
import { useT } from "../i18n";
import { Badge, Btn, Card, Empty, Err, Field, Modal } from "../components/ui";
import { Badge as B } from "../components/ui";

export default function Playbook() {
  const { t } = useT(); const [q, setQ] = useState(""); const [sel, setSel] = useState<any>(null);
  const list = useQuery({ queryKey: ["playbook", q], queryFn: () => get(`/playbook?q=${encodeURIComponent(q)}`) });
  return (
    <div className="stack">
      <div><h2>📚 {t("pb_title")}</h2><span className="muted small">{t("pb_sub")}</span></div>
      <input className="input" placeholder={t("search")} value={q} onChange={(e) => setQ(e.target.value)} style={{ maxWidth: 420 }} />
      {!list.data?.length ? <Empty /> : list.data.map((e: any) => (
        <Card key={e.key} className="tight"><div className="svc" style={{ cursor: "pointer" }} onClick={() => setSel(e)}>
          <div className="h"><B v={e.severity} /> <span className="mono">{e.template.slice(0, 120)}</span>{!!e.root_cause_count && <span className="chip">root × {e.root_cause_count}</span>}</div>
          <div className="v">{t("pb_occ")} {e.occurrences} · {e.events} {t("ds_events")} · {e.datasets.length} {t("pb_datasets")} · {(e.services ?? []).slice(0, 4).join(", ")} · {t("pb_last")} {e.last_seen?.slice(0, 16).replace("T", " ")}{e.resolution && <> · ✅ {e.resolution.slice(0, 80)}</>}</div></div></Card>))}
      {sel && <Entry e={sel} onClose={() => setSel(null)} />}
    </div>
  );
}

function Entry({ e, onClose }: { e: any; onClose: () => void }) {
  const { t } = useT(); const qc = useQueryClient(); const [res, setRes] = useState(e.resolution || ""); const [rb, setRb] = useState(e.runbook || "");
  const save = useMutation({ mutationFn: () => put(`/playbook/entry?key=${encodeURIComponent(e.key)}`, { resolution: res, runbook: rb }), onSuccess: () => { qc.invalidateQueries({ queryKey: ["playbook"] }); onClose(); } });
  return <Modal wide title={<span><Badge v={e.severity} /> <span className="mono">{e.template.slice(0, 100)}</span></span>} onClose={onClose}><div className="stack">
    <div className="row small muted"><span>{t("pb_datasets")}: {e.datasets.join(", ")}</span><span>· {t("host")}: {(e.hosts ?? []).join(", ")}</span><span>· {t("inc_recovery")}: {Object.entries(e.recoveries ?? {}).map(([k, v]) => `${k} ${v}`).join(", ") || "-"}</span></div>
    <Field label={t("pb_resolution")}><textarea className="input" value={res} onChange={(x) => setRes(x.target.value)} /></Field>
    <Field label={t("pb_runbook")}><textarea className="input" style={{ minHeight: 140 }} value={rb} onChange={(x) => setRb(x.target.value)} /></Field>
    <Err e={save.error} /><div className="row"><Btn kind="primary" onClick={() => save.mutate()}>{t("save")}</Btn></div></div></Modal>;
}
