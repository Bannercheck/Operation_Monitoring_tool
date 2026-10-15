import { useEffect, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Activity, ClipboardList, FileText, Fingerprint, Layers, Lock, LockOpen, Network, PenLine, Plug, Radar, Radio, ScrollText, Server, Terminal, Undo2, Workflow } from "lucide-react";
import { del, get, put } from "../../api";
import { useAuth } from "../../auth";
import { useT } from "../../i18n";
import { Btn, Err, Field, Modal } from "../../components/ui";
import { TeamPick } from "../../components/TeamPick";

/** Which technology an integration runs on (protocol, client, server), how sure we are and why: the confidence marks, the
 *  evidence list and the detail drawers of the integration map. The backend (techstack.py) decides; this only shows it. */

export type Ev = { source: string; tr: string; en: string; weight?: number; conflict?: boolean };
export type Hop = { host: string; port: number; name: string; product?: string | null; framework?: string | null; runtime?: string | null; version?: string | null; confidence: string; chain?: Hop[] };
export type Tech = {
  label: string; confidence: string; evidence: Ev[]; manual: boolean; note?: string | null; team_id?: number | null;
  protocol: { name: string; family: string; port: number; tls: boolean; key: string };
  client: { name: string | null; runtime: string | null; libs: string[] };
  server: { name: string | null; product: string | null; framework: string | null; runtime: string | null; version: string | null; app: string | null; chain: Hop[] };
};
/** What the drawer shows: one integration (outbound / inbound / listening port) or the host's technology stack. */
export type Sel = { dir: "out" | "in" | "listen"; peer: string; port: string } | { dir: "host"; key: string };

export const CAT_COLOR: Record<string, string> = { db: "#a78bfa", cache: "#c084fc", messaging: "#fb923c", api: "#60a5fa", sap: "#2dd4bf", identity: "#facc15",
  mail: "#f472b6", file: "#34d399", observability: "#94a3b8", infra: "#64748b", remote: "#f87171", other: "#94a3b8" };

const CONF_K: Record<string, string> = { kesin: "kesin", "yüksek": "yuksek", "olası": "olasi" };
/** "kesin" | "yuksek" | "olasi": an ASCII key for CSS classes and i18n keys. */
export const confK = (c?: string | null) => CONF_K[c ?? ""] ?? "olasi";
const DIRECT = ["agent", "trace", "banner", "manual"];
const SRC_ICON: Record<string, any> = { agent: Fingerprint, trace: Activity, inventory: ClipboardList, listener: Radio, process: Terminal, port: Plug, banner: Radar, manual: PenLine, log: FileText };

export const fmtTs = (s?: string) => (s ? s.replace("T", " ").slice(5, 16) : "-");

/** ix URL value <-> selection: "out:<port>:<peer>", "in:<port>:<peer>", "listen:<port>:", "host::<stack key>". */
export function parseSel(v: string | null): Sel | null {
  if (!v) return null;
  const a = v.indexOf(":"), b = v.indexOf(":", a + 1);
  if (a < 0 || b < 0) return null;
  const dir = v.slice(0, a), port = v.slice(a + 1, b), rest = v.slice(b + 1);
  if (dir === "host") return { dir, key: rest };
  if (dir === "out" || dir === "in" || dir === "listen") return { dir, port, peer: rest };
  return null;
}
export const selStr = (s: Sel) => (s.dir === "host" ? `host::${s.key}` : `${s.dir}:${s.port}:${s.peer}`);
export const sameSel = (a: Sel | null, b: Sel) => !!a && selStr(a) === selStr(b);

/** The item of the host map a selection points at (null when it is gone from the window). */
export function findSel(m: any, s: Sel | null): any {
  if (!m || !s || s.dir === "host") return null;
  if (s.dir === "out") return (m.outbound ?? []).find((o: any) => o.target === s.peer && String(o.port) === s.port) ?? null;
  if (s.dir === "in") return (m.inbound ?? []).find((c: any) => c.source === s.peer && String(c.port) === s.port) ?? null;
  return (m.listens ?? []).find((l: any) => String(l.port) === s.port) ?? null;
}

/** Small confidence mark + word: solid dot = kesin, ring = yüksek, dashed ring with "?" = olası. */
export function ConfBadge({ c, short }: { c?: string | null; short?: boolean }) {
  const { t } = useT(); const k = confK(c);
  return <span className={`tconf ${k}${short ? " bare" : ""}`} title={`${t(`itc_conf_${k}`)}: ${t(`itc_leg_${k}`)}`}><i />{short ? null : t(`itc_conf_${k}`)}</span>;
}

/** A technology label as text: dotted underline + "?" when it is only possible. */
export function TechLabel({ tech, port }: { tech?: Tech; port?: number | string }) {
  const { t } = useT();
  if (!tech) return <span className="dim">-</span>;
  const k = confK(tech.confidence);
  return <span className="tcell"><span className={`tlabel ${k}`} title={tech.label}>{tech.label}{k === "olasi" ? " ?" : ""}{port != null ? <span className="dim"> · :{port}</span> : null}</span>
    <ConfBadge c={tech.confidence} />{tech.manual && <span className="tman" title={t("itc_manual_hint")}><PenLine size={11} /> {t("itc_manual")}</span>}</span>;
}

export function ConfLegend() {
  const { t } = useT();
  return <div className="tlegend"><span className="dim">{t("itc_legend")}</span>
    {["kesin", "yuksek", "olasi"].map((k) => <span key={k} className="tlg"><span className={`tconf ${k}`}><i />{t(`itc_conf_${k}`)}</span><span className="dim">{t(`itc_leg_${k}`)}</span></span>)}</div>;
}

/** One line on why the label has its confidence, from the rule the engine applies and the kinds of evidence present. */
export function whyLine(t: (k: string, kw?: Record<string, any>) => string, tech: Tech): string {
  if (tech.manual) return t("itc_why_manual");
  const ev = tech.evidence ?? [];
  const srcs = Array.from(new Set(ev.map((e) => e.source)));
  const names = (xs: string[]) => xs.map((s) => t(`itc_src_${s}`)).join(", ");
  const k = confK(tech.confidence);
  // direct proof only: weight 1 marks the same kinds when they are not proof (an old scan, a library that may be a client)
  const direct = Array.from(new Set(ev.filter((e) => DIRECT.includes(e.source) && (e.weight ?? 3) >= 3 && !e.conflict).map((e) => e.source)));
  if (ev.some((e) => e.conflict)) return t("itc_why_conflict");
  if (k === "kesin") return t("itc_why_kesin", { src: names(direct) || "-" });
  if (k === "yuksek") return t("itc_why_yuksek");
  if (srcs.every((s) => s === "port")) return t("itc_why_port");
  return t("itc_why_olasi");
}

export function EvidenceList({ ev }: { ev: Ev[] }) {
  const { t, lang } = useT();
  if (!ev?.length) return <div className="dim small">{t("itc_no_evidence")}</div>;
  return <ul className="tev">{ev.map((e, i) => { const Ic = SRC_ICON[e.source] ?? FileText;
    return <li key={i} className={e.conflict ? "conflict" : DIRECT.includes(e.source) && (e.weight ?? 3) >= 3 ? "direct" : ""}><span className="tev-ic" title={t(`itc_src_${e.source}`)}><Ic size={13} /></span>
      <span className="tev-tx">{lang === "tr" ? e.tr : e.en}</span><span className="tev-src">{t(`itc_src_${e.source}`)}</span></li>; })}</ul>;
}

const KV = ({ k, v }: { k: string; v: ReactNode }) => (v == null || v === "" ? null : <><dt>{k}</dt><dd>{v}</dd></>);

function HopRow({ h, onPick }: { h: Hop; onPick: (host: string) => void }) {
  const { t } = useT();
  return <div className="thop"><span className="dim">→</span> <a href="#" onClick={(e) => { e.preventDefault(); onPick(h.host); }} title={t("itc_map_of", { h: h.host })}><b>{h.host}</b></a>
    <span className="mono dim">:{h.port}</span> <span>{h.name}</span>{h.version ? <span className="dim"> {h.version}</span> : null} <ConfBadge c={h.confidence} short />
    {(h.chain ?? []).map((x, i) => <HopRow key={i} h={x} onPick={onPick} />)}</div>;
}

/** Links out of a drawer: the peer's map, its traces (services seen on the server side), the log explorer, its inventory record. */
function GoLinks({ hosts, services, onPick, current }: { hosts: string[]; services: string[]; onPick: (h: string) => void; current: string }) {
  const { t } = useT(); const { can } = useAuth();
  return <div className="tgo">
    {hosts.filter((h) => h && h !== current).map((h) => <button key={"m" + h} className="chip tgo-l" onClick={() => onPick(h)}><Network size={13} /> {t("itc_map_of", { h })}</button>)}
    {services.slice(0, 3).map((s) => <Link key={"s" + s} className="chip tgo-l" to={`/traces?service=${encodeURIComponent(s)}`}><Activity size={13} /> {t("itc_traces")} · {s}</Link>)}
    {can("page.logs") && hosts.filter(Boolean).map((h) => <Link key={"l" + h} className="chip tgo-l" to={`/logs?q=${encodeURIComponent(`host:${h}`)}`}><ScrollText size={13} /> {t("itc_logs")} · {h}</Link>)}
    {can("page.inv") && hosts.filter(Boolean).map((h) => <Link key={"i" + h} className="chip tgo-l" to={`/inventory?q=${encodeURIComponent(h)}`}><ClipboardList size={13} /> {t("itc_inv")} · {h}</Link>)}
  </div>;
}

/** The inventory row of a host in one line (operating system · application · role · owner), when the reader may see it. */
function InvLine({ host }: { host: string }) {
  const { can } = useAuth();
  const q = useQuery({ queryKey: ["inv-one", host], queryFn: () => get(`/inventory?q=${encodeURIComponent(host)}`), enabled: !!host && can("page.inv"), retry: false, staleTime: 60000 });
  const r = (Array.isArray(q.data) ? q.data : []).find((x: any) => x.hostname === host); if (!r) return null;     // the search, not GET /inventory/{h}: no 404 for hosts outside the inventory
  const parts = [r.os, r.application, r.role, r.owner].filter(Boolean);
  return parts.length ? <div className="dim small tinv"><ClipboardList size={12} /> {parts.join(" · ")}</div> : null;
}

/** The detail drawer of one integration: protocol, client and server technology, confidence and why, evidence, traffic, links,
 *  and the manual correction for users who may change the inventory. */
export function TechDrawer({ m, sel, minutes, hosts, onClose, onPick }: { m: any; sel: Sel; minutes: number; hosts: string[]; onClose: () => void; onPick: (h: string) => void }) {
  const { t } = useT();
  const it = findSel(m, sel);
  if (sel.dir === "host") return <StackDrawer m={m} focus={sel.key} onClose={onClose} onPick={onPick} />;
  if (!it || !it.tech) return <Modal title={t("itc_gone_t")} onClose={onClose}><div className="dim">{t("itc_gone")}</div></Modal>;
  const tech: Tech = it.tech;
  const out = sel.dir === "out", listen = sel.dir === "listen";
  const client = out ? m.host : listen ? "" : it.source;
  const server = out ? it.target : m.host;
  const port = Number(it.port) || tech.protocol?.port || 0;
  const cat = out || sel.dir === "in" ? it.category : tech.protocol?.family;
  const k = confK(tech.confidence);
  const procs: string[] = listen ? [it.process].filter(Boolean) : it.processes ?? [];
  return <Modal wide onClose={onClose} title={<span className="tdr-t"><Workflow size={16} /> <span className="tdr-ends">{client ? <>{client} <span className="dim">→</span> </> : null}{server}<span className="mono dim"> :{port}</span></span>
    <span className="chip small" style={{ borderColor: CAT_COLOR[cat] }}><span className="d" style={{ background: CAT_COLOR[cat] ?? "#94a3b8" }} />{t(`integ_cat_${cat}`)}</span>
    <span className="dim small">{t(`itc_dir_${sel.dir}`)}</span></span>}>
    <div className="tdr">
      <div className={`tdr-head ${k}`}>
        <div className="tdr-label"><span className={`tlabel ${k}`}>{tech.label}{k === "olasi" ? " ?" : ""}</span><ConfBadge c={tech.confidence} />
          {tech.manual && <span className="tman"><PenLine size={11} /> {t("itc_manual")}</span>}</div>
        <div className="small tdr-why"><b>{t("itc_conf")}:</b> {t(`itc_conf_${k}`)} · {whyLine(t, tech)}</div>
        {tech.note ? <div className="small dim">{t("itc_f_note")}: {tech.note}</div> : null}
      </div>
      <div className="tdr-grid">
        <section className="tdr-sec"><h4><Plug size={14} /> {t("itc_protocol")}</h4><dl className="tkv">
          <KV k={t("itc_family")} v={t(`integ_cat_${tech.protocol?.family || cat}`)} />
          <KV k={t("itc_proto_name")} v={tech.protocol?.name} />
          <KV k={t("itc_port")} v={<span className="mono">:{port}</span>} />
          <KV k="TLS" v={tech.protocol?.tls ? <span className="tls on"><Lock size={12} /> {t("itc_tls_on")}</span> : <span className="tls"><LockOpen size={12} /> {t("itc_tls_off")}</span>} />
        </dl></section>
        <section className="tdr-sec"><h4><Terminal size={14} /> {t("itc_client")}</h4>{listen ? <div className="dim small">{t("itc_listen_client")}</div> : <dl className="tkv">
          <KV k={t("itc_host")} v={<b>{client}</b>} />
          <KV k={t("itc_tech")} v={tech.client?.name || <span className="dim">{t("itc_unknown")}</span>} />
          <KV k={t("itc_runtime")} v={tech.client?.runtime} />
          <KV k={t("itc_libs")} v={tech.client?.libs?.length ? <span className="tchips">{tech.client.libs.map((l) => <span key={l} className="chip small">{l}</span>)}</span> : null} />
          <KV k={t("integ_procs")} v={procs.length ? <span className="mono small">{procs.join(", ")}</span> : null} />
        </dl>}{client ? <InvLine host={client} /> : null}</section>
        <section className="tdr-sec"><h4><Server size={14} /> {t("itc_server")}</h4><dl className="tkv">
          <KV k={t("itc_host")} v={<b>{server}</b>} />
          <KV k={t("itc_tech")} v={tech.server?.name || <span className="dim">{t("itc_unknown")}</span>} />
          <KV k={t("itc_product")} v={tech.server?.product} />
          <KV k={t("itc_framework")} v={tech.server?.framework} />
          <KV k={t("itc_runtime")} v={tech.server?.runtime} />
          <KV k={t("itc_version")} v={tech.server?.version} />
          <KV k={t("itc_app")} v={tech.server?.app} />
          {listen ? <KV k={t("integ_procs")} v={procs.length ? <span className="mono small">{procs.join(", ")}</span> : null} /> : null}
        </dl>
          {tech.server?.chain?.length ? <div className="tchain"><div className="dim small">{t("itc_chain")}</div>{tech.server.chain.map((h, i) => <HopRow key={i} h={h} onPick={onPick} />)}</div> : null}
          <InvLine host={server} /></section>
        <section className="tdr-sec"><h4><Activity size={14} /> {t("itc_traffic")}</h4><dl className="tkv">
          <KV k={t("itc_conns")} v={<b>{listen ? it.peers : it.connections}</b>} />
          <KV k={t("itc_samples")} v={it.samples} />
          <KV k={t("integ_first")} v={it.first_seen ? fmtTs(it.first_seen) : null} />
          <KV k={t("integ_last")} v={fmtTs(it.last_seen)} />
          {out && !it.known ? <KV k={t("integ_target")} v={<span className="dim">{t("integ_unknown")}</span>} /> : null}
        </dl></section>
      </div>
      <section className="tdr-sec"><h4><Fingerprint size={14} /> {t("itc_evidence")} <span className="dim small">({tech.evidence?.length ?? 0})</span></h4><EvidenceList ev={tech.evidence} /></section>
      <section className="tdr-sec"><h4><Network size={14} /> {t("itc_go")}</h4>
        <TargetLinks client={client} server={server} m={m} minutes={minutes} hosts={hosts} onPick={onPick} /></section>
      {!listen && <ManualEdit tech={tech} host={client} target={server} port={port} />}
    </div>
  </Modal>;
}

/** Links for one integration; the server side's services come from its own map (fetched only when the reader can see that host). */
function TargetLinks({ client, server, m, minutes, hosts, onPick }: { client: string; server: string; m: any; minutes: number; hosts: string[]; onPick: (h: string) => void }) {
  const other = server === m.host ? "" : server;
  const sm = useQuery({ queryKey: ["integ", other, minutes], queryFn: () => get(`/apm/hosts/${encodeURIComponent(other)}/integrations?minutes=${minutes}`),
    enabled: !!other && hosts.includes(other), retry: false, staleTime: 30000 });
  const services: string[] = other ? sm.data?.services ?? [] : m.services ?? [];
  const peers = [server, client].filter((h) => h && (h === m.host || hosts.includes(h)));
  return <GoLinks hosts={Array.from(new Set(peers))} services={services} onPick={onPick} current={m.host} />;
}

/** "Correct by hand": protocol / client / server / note for one integration, and "back to automatic". */
function ManualEdit({ tech, host, target, port }: { tech: Tech; host: string; target: string; port: number }) {
  const { t } = useT(); const { can, teamsWith } = useAuth(); const qc = useQueryClient();
  const allowed = can("act.inventory") || teamsWith("act.inventory").length > 0;
  const init = () => ({ protocol: tech.manual ? tech.protocol?.name ?? "" : "", client: tech.manual ? tech.client?.name ?? "" : "", server: tech.manual ? tech.server?.name ?? "" : "", note: tech.note ?? "" });
  const [open, setOpen] = useState(false); const [f, setF] = useState(init); const [team, setTeam] = useState<number | null>(tech.team_id ?? null); const [done, setDone] = useState("");
  useEffect(() => { if (!open) setF(init()); }, [tech.label, tech.manual, open]);
  const refresh = () => { qc.invalidateQueries({ queryKey: ["integ"] }); qc.invalidateQueries({ queryKey: ["integ-hosts"] }); };
  const save = useMutation({ mutationFn: () => put("/apm/integrations/tech", { host, target, port, protocol: f.protocol, client: f.client, server: f.server, note: f.note, team_id: team }),
    onSuccess: () => { setOpen(false); setDone(t("itc_saved")); refresh(); } });
  const back = useMutation({ mutationFn: () => del(`/apm/integrations/tech?host=${encodeURIComponent(host)}&target=${encodeURIComponent(target)}&port=${port}${tech.team_id != null ? `&team_id=${tech.team_id}` : ""}`),
    onSuccess: () => { setDone(t("itc_reverted")); refresh(); } });
  if (!allowed) return null;
  const any = !!(f.protocol.trim() || f.client.trim() || f.server.trim() || f.note.trim());
  return <section className="tdr-sec tdr-edit"><div className="row between"><h4><PenLine size={14} /> {t("itc_edit")}</h4>
    <div className="row" style={{ gap: 6 }}>{tech.manual && <Btn sm onClick={() => back.mutate()} disabled={back.isPending}><Undo2 size={13} /> {t("itc_auto")}</Btn>}
      {!open && <Btn sm kind="primary" onClick={() => { setDone(""); setOpen(true); }}><PenLine size={13} /> {tech.manual ? t("edit") : t("itc_edit")}</Btn>}</div></div>
    {done && !open ? <div className="ok small">{done}</div> : null}
    <Err e={back.error} />
    {open ? <form className="stack" style={{ gap: 10 }} onSubmit={(e) => { e.preventDefault(); if (any) save.mutate(); }}>
      <div className="dim small">{t("itc_edit_hint")}</div>
      <div className="tdr-form">
        <Field label={t("itc_f_protocol")}><input className="input" name="protocol" maxLength={80} value={f.protocol} placeholder={tech.protocol?.name ?? ""} onChange={(e) => setF({ ...f, protocol: e.target.value })} /></Field>
        <Field label={t("itc_f_client")}><input className="input" name="client" maxLength={80} value={f.client} placeholder={tech.client?.name ?? ""} onChange={(e) => setF({ ...f, client: e.target.value })} /></Field>
        <Field label={t("itc_f_server")}><input className="input" name="server" maxLength={80} value={f.server} placeholder={tech.server?.name ?? ""} onChange={(e) => setF({ ...f, server: e.target.value })} /></Field>
        <Field label={t("itc_f_note")}><input className="input" name="note" maxLength={300} value={f.note} placeholder={t("itc_note_ph")} onChange={(e) => setF({ ...f, note: e.target.value })} /></Field>
      </div>
      <div className="row" style={{ gap: 8 }}><TeamPick perm="act.inventory" value={team} onChange={setTeam} style={{ width: "auto" }} />
        <span className="grow" /><Btn type="button" sm kind="ghost" onClick={() => setOpen(false)}>{t("cancel")}</Btn>
        <Btn sm kind="primary" type="submit" disabled={!any || save.isPending}>{t("save")}</Btn></div>
      <Err e={save.error} />
    </form> : null}
  </section>;
}

const LAYERS = ["app", "web", "db", "messaging", "identity", "file", "mail", "runtime", "os"];

/** The host's technology stack by layer (runtime, web / app server, framework, database …), each with its confidence and evidence. */
export function StackDrawer({ m, focus, onClose, onPick }: { m: any; focus?: string; onClose: () => void; onPick: (h: string) => void }) {
  const { t } = useT();
  const stack: any[] = m.stack ?? [];
  const layers = [...LAYERS, ...Array.from(new Set(stack.map((s) => s.layer))).filter((l) => !LAYERS.includes(l))].filter((l) => stack.some((s) => s.layer === l));
  return <Modal wide onClose={onClose} title={<span className="tdr-t"><Layers size={16} /> <span className="tdr-ends">{m.host}</span><span className="dim small">{t("itc_stack")}</span></span>}>
    <div className="tdr">
      {!stack.length ? <div className="dim">{t("itc_stack_none")}</div> : layers.map((l) => <section key={l} className="tdr-sec"><h4>{t(`itc_layer_${l}`)}</h4>
        {stack.filter((s) => s.layer === l).map((s, i) => { const on = !!focus && (s.key ?? s.name) === focus;
          return <div key={i} className={`tstk ${on ? "on" : ""}`} ref={on ? (el) => el?.scrollIntoView({ block: "nearest" }) : undefined}>
            <div className="row" style={{ gap: 8 }}><b>{stackName(s)}</b><ConfBadge c={s.confidence} /></div>
            <EvidenceList ev={s.evidence ?? []} /></div>; })}</section>)}
      <section className="tdr-sec"><h4><Network size={14} /> {t("itc_go")}</h4><GoLinks hosts={[m.host]} services={m.services ?? []} onPick={onPick} current={m.host} /></section>
    </div>
  </Modal>;
}

export const stackName = (s: any) => (s.version && !String(s.name).includes(s.version) ? `${s.name} ${s.version}` : s.name);
