import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { del, get, post, put } from "../api";
import { useT } from "../i18n";
import { Btn, Card, Confirm, Err, Field, Table, Tabs, fmtTs } from "../components/ui";

export default function Notify() {
  const { t } = useT(); const qc = useQueryClient(); const [tab, setTab] = useState<"recipients" | "rules" | "log" | "channels">("rules");
  const [rc, setRc] = useState({ name: "", email: "", phone: "", groups: "" }); const [rl, setRl] = useState({ name: "", condition: "anomaly", threshold: 0, env: "", severity: "high", channels: "email", targets: "", cooldown_min: 30 });
  const recipients = useQuery({ queryKey: ["ntf-rec"], queryFn: () => get("/notify/recipients") });
  const rules = useQuery({ queryKey: ["ntf-rules"], queryFn: () => get("/notify/rules") });
  const conds = useQuery({ queryKey: ["ntf-cond"], queryFn: () => get("/notify/conditions") });
  const log = useQuery({ queryKey: ["ntf-log"], queryFn: () => get("/notify/alerts?n=100"), enabled: tab === "log" });
  const inv = () => qc.invalidateQueries();
  const addRec = useMutation({ mutationFn: () => post("/notify/recipients", rc), onSuccess: () => { setRc({ name: "", email: "", phone: "", groups: "" }); inv(); } });
  const rmRec = useMutation({ mutationFn: (id: number) => del(`/notify/recipients/${id}`), onSuccess: inv });
  const addRule = useMutation({ mutationFn: () => post("/notify/rules", { ...rl, threshold: Number(rl.threshold), cooldown_min: Number(rl.cooldown_min) }), onSuccess: () => { setRl({ ...rl, name: "", targets: "" }); inv(); } });
  const rmRule = useMutation({ mutationFn: (id: number) => del(`/notify/rules/${id}`), onSuccess: inv });
  const evaluate = useMutation({ mutationFn: () => post("/notify/evaluate"), onSuccess: inv });
  return <div className="stack">
    <div className="row between"><h2>🔔 {t("ntf_title")}</h2><Btn onClick={() => evaluate.mutate()}>{t("ntf_evaluate")}</Btn></div>
    <Tabs value={tab} onChange={setTab} tabs={[{ k: "rules", label: `${t("ntf_rules")} · ${rules.data?.length ?? 0}` }, { k: "recipients", label: `${t("ntf_recipients")} · ${recipients.data?.length ?? 0}` }, { k: "log", label: t("ntf_log") }, { k: "channels", label: t("ntf_channels_tab") }]} />
    {tab === "channels" && <Channels />}
    {tab === "recipients" && <div className="stack"><Card><div className="row"><input className="input" style={{ width: 160 }} placeholder={t("name")} value={rc.name} onChange={(e) => setRc({ ...rc, name: e.target.value })} /><input className="input" style={{ width: 220 }} placeholder={t("email")} value={rc.email} onChange={(e) => setRc({ ...rc, email: e.target.value })} />
      <input className="input" style={{ width: 160 }} placeholder={t("ntf_phone")} value={rc.phone} onChange={(e) => setRc({ ...rc, phone: e.target.value })} /><input className="input" style={{ width: 160 }} placeholder={t("ntf_groups")} value={rc.groups} onChange={(e) => setRc({ ...rc, groups: e.target.value })} /><Btn kind="primary" onClick={() => addRec.mutate()} disabled={!rc.name}>{t("add")}</Btn></div><Err e={addRec.error} /></Card>
      <Card><Table cols={[{ k: "name", label: t("name") }, { k: "email", label: t("email") }, { k: "phone", label: t("ntf_phone") }, { k: "groups", label: t("ntf_groups") }, { k: "enabled", label: t("src_enabled"), render: (r) => (r.enabled ? "✓" : "–") }, { k: "x", label: "", render: (r) => <Confirm onConfirm={() => rmRec.mutate(r.id)}>{t("delete")}</Confirm> }]} rows={recipients.data ?? []} /></Card></div>}
    {tab === "rules" && <div className="stack"><Card><div className="row"><input className="input" style={{ width: 150 }} placeholder={t("name")} value={rl.name} onChange={(e) => setRl({ ...rl, name: e.target.value })} />
      <select className="input" style={{ width: 170 }} value={rl.condition} onChange={(e) => setRl({ ...rl, condition: e.target.value })}>{(conds.data ?? []).map((c: string) => <option key={c}>{c}</option>)}</select><input className="input" style={{ width: 90 }} type="number" placeholder={t("ntf_threshold")} value={rl.threshold} onChange={(e) => setRl({ ...rl, threshold: Number(e.target.value) })} />
      <input className="input" style={{ width: 90 }} placeholder={t("env")} value={rl.env} onChange={(e) => setRl({ ...rl, env: e.target.value })} /><select className="input" style={{ width: 120 }} value={rl.channels} onChange={(e) => setRl({ ...rl, channels: e.target.value })}>{["email", "sms", "email,sms"].map((c) => <option key={c}>{c}</option>)}</select>
      <input className="input" style={{ width: 160 }} placeholder={t("ntf_targets")} value={rl.targets} onChange={(e) => setRl({ ...rl, targets: e.target.value })} /><input className="input" style={{ width: 80 }} type="number" value={rl.cooldown_min} onChange={(e) => setRl({ ...rl, cooldown_min: Number(e.target.value) })} /><Btn kind="primary" onClick={() => addRule.mutate()} disabled={!rl.name}>{t("add")}</Btn></div><Err e={addRule.error || evaluate.error} /></Card>
      <Card><Table cols={[{ k: "name", label: t("name") }, { k: "condition", label: t("ntf_condition") }, { k: "threshold", label: t("ntf_threshold"), num: true }, { k: "env", label: t("env") }, { k: "channels", label: t("ntf_channels") }, { k: "targets", label: t("ntf_targets") }, { k: "cooldown_min", label: t("ntf_cooldown"), num: true }, { k: "enabled", label: t("src_enabled"), render: (r) => (r.enabled ? "✓" : "–") }, { k: "x", label: "", render: (r) => <Confirm onConfirm={() => rmRule.mutate(r.id)}>{t("delete")}</Confirm> }]} rows={rules.data ?? []} /></Card></div>}
    {tab === "log" && <Card><Table cols={[{ k: "ts", label: t("time"), render: (r) => fmtTs(r.ts) }, { k: "severity", label: t("severity") }, { k: "title", label: t("act_title_f") }, { k: "channels", label: t("ntf_channels") }, { k: "recipients", label: t("ntf_recipients") }, { k: "ok", label: t("status"), render: (r) => (r.ok ? "✓" : r.detail?.slice(0, 60)) }]} rows={log.data ?? []} /></Card>}
  </div>;
}


function Channels() {
  const { t } = useT(); const qc = useQueryClient();
  const q = useQuery({ queryKey: ["channels"], queryFn: () => get("/system/channels") });
  const [f, setF] = useState<any>(null); const [target, setTarget] = useState(""); const [msg, setMsg] = useState<any>(null);
  useEffect(() => { if (q.data && !f) setF(q.data); }, [q.data, f]);
  const save = useMutation({ mutationFn: () => put("/system/channels", f), onSuccess: () => { setMsg({ ok: true, message: t("saved") }); qc.invalidateQueries({ queryKey: ["channels"] }); } });
  const test = useMutation({ mutationFn: (channel: string) => post("/notify/test", { channel, target }), onSuccess: (r: any) => setMsg(r) });
  if (!f) return null;
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  const F = (k: string, type = "text") => <Field label={t(k)}><input className="input" type={type} value={f[k] ?? ""} onChange={set(k)} autoComplete="off" /></Field>;
  return <div className="grid k2">
    <Card title="SMTP"><div className="stack">{F("smtp_host")}<div className="grid k2">{F("smtp_port")}<Field label={t("smtp_security")}><select className="input" value={f.smtp_security} onChange={set("smtp_security")}>{["starttls", "ssl", "none"].map((x) => <option key={x}>{x}</option>)}</select></Field></div>
      <div className="grid k2">{F("smtp_user")}{F("smtp_password", "password")}</div><div className="grid k2">{F("smtp_from")}{F("smtp_from_name")}</div></div></Card>
    <Card title="SMS"><div className="stack"><Field label={t("sms_preset")}><select className="input" value={f.sms_preset} onChange={set("sms_preset")}>{["http", "twilio", "netgsm", "iletimerkezi", "verimor", "turkcell", "vodafone", "turktelekom"].map((x) => <option key={x}>{x}</option>)}</select></Field>{F("sms_url")}
      <div className="grid k2">{F("sms_user")}{F("sms_password", "password")}</div><div className="grid k2">{F("sms_token", "password")}{F("sms_from")}</div></div></Card>
    <div className="row" style={{ gridColumn: "1 / -1" }}><Btn kind="primary" onClick={() => save.mutate()}>{t("save")}</Btn><input className="input" style={{ width: 260 }} placeholder={t("ntf_test_target")} value={target} onChange={(e) => setTarget(e.target.value)} /><Btn onClick={() => test.mutate("email")} disabled={!target}>{t("ntf_test_send")} · e-mail</Btn><Btn onClick={() => test.mutate("sms")} disabled={!target}>{t("ntf_test_send")} · SMS</Btn>
      {msg && <span className={msg.ok ? "ok" : "err"}>{msg.message}</span>}<Err e={save.error || test.error} /></div>
  </div>;
}