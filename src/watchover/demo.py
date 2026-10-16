"""The simulator's retirement, the strict separation of simulator data from real data, and the clean-up of what it left behind.

The simulator is retired (v1.57): no installation runs it and no setting, environment variable or API turns it on. SIMULATOR is a
module constant only the test suite sets, to generate fixture data. With it off: Operations has no simulator (the API answers 403),
Datasets has no demo / sample sets, ITSM no demo tickets, the action agent no demo controls (act.policy.act_demo).
What older versions' simulator left on an installation goes at the first start (Services.purge_leftovers: purge(), markers only,
real data stays) and with ./watchover.sh demo-clean. A full reset (reset_all; ./watchover.sh reset-data; System › Veri sıfırlama)
also empties every analysis store whoever wrote it: anomalies, incidents' actions and cases, alerts, learned baselines, the
assistant's memory, fine-tunes, history, traces, discovered topology (RESET_WIPE). Accounts, teams, settings (the LLM connection
included), enrolled agents, sources, policies, audit logs, operator-defined inventory / applications / flows, stored log lines of
real senders and downloaded base models stay.

Marking at the source (never by guessing a host name):
  * every observation the simulator ingests has sender (agent) "simulator" and the attribute DEMO_ATTR ("wo.demo" = 1); the
    ingest path strips the attribute from every other sender's lines, so a real log line cannot claim it. Stored lines keep
    "demo": 1 and agent "simulator" (main database, hot day files, cold archive, the live spool). A real sender named
    "simulator" is renamed (sender_name) and no agent can enrol under that name.
  * derived rows carry a `demo` column (DEMO_COL, 1 = made from demo input) or an explicit marker column, set where created;
    upserts keep a row real once real input touched it (keep_real).
  * LiveStore.demo_hosts: the hosts demo input wrote (from the marker, not from names); derived writers ask live.is_demo_host().
  * the assistant / LLM side: Services.demo_present() (simulator on, or demo data still stored) marks LLM calls, ratings,
    evaluation runs, saved chats and proposed rules made meanwhile (Knowledge.demo_fn).
Host-name heuristics (sim_hosts) remain only as the fallback for rows written before these markers, minus names a real source
uses (claimed).

Never learned from demo input: anomaly baselines / maturity (anomaly_learning, anomaly_signal, seasonal profiles), new-pattern
templates (anomaly_templates), Drain (drain_clusters), learned column profiles (profiles.json), the knowledge-base learner (live
windows, memories of anomalies and closed actions), dataset learning (playbook, lessons, Drain) of demo / sample datasets,
training-example export and automatic training (demo-flagged lessons / rules / answer feedback are excluded; the gate dataset
skips simulator lines), the action agent (demo anomalies, incidents, log-query hits and flows never become triggers, so its cases,
evidence, matcher, metrics and trust events never see demo input). The per-service hourly history skips demo lines.

The clean-up (Services.purge_leftovers at start; ./watchover.sh demo-clean; POST /api/system/demo-clean; python -m watchover.demo
clean): preview() counts per category, purge() stops the simulator and empties every store below of its rows.

STORES - every place simulator-derived data can land. Add new stores here; tests/test_demo_purge.py fails on a table that is
in none of FLAGGED / HOST_TABLES / MARKED_TABLES / EXEMPT.
  database (SQLite knowledge.db + playbook.db, or PostgreSQL)           marker                               category
    live_batches (main DB when WATCHOVER_LOG_STORE=db)                  agent = 'simulator'                  lines
    slo_minute                                                          sender = 'simulator'                 metrics
    svc_minute, spans                                                   demo = 1                             metrics / traces
    rum_apps, rum_sessions (+ the sessions' spans)                      demo = 1 / note = DEMO_OWNER         traces
    anomalies                                                           demo = 1                             anomalies
    anomaly_learning, anomaly_signal                                    never written for demo hosts         anomalies (old: host)
    alerts, threshold_state                                             demo = 1                             alerts
    inventory                                                           demo = 1 / owner = DEMO_OWNER        topology
    applications                                                        origin = 'demo' (applications.py)    topology
    app_namespaces, net_flows, tech_seen                                demo = 1                             topology
    integ_tech, team_hosts, scan_results, app_dismissed                 demo host names (explicit set)       topology
    biz_flows, biz_events                                               demo = 1                             bizflow
    actions (first actions of demo datasets, actions of demo anomalies) demo = 1                             actions
    act_* (cases, triggers, evidence, runs, steps, proposals, decisions, frozen inputs, examples, trust events)
                                                                        never from demo; old: cases on demo hosts   actions
    lessons (chats saved in demo context), rules, answer_feedback,
    llm_calls, eval_runs                                                demo = 1                             llm_memory
    train_runs (+ the fine-tuned watchover-ops tags, the run's adapter) demo = 1 (old: examples name demo hosts)  models
    act_models / act_dataset_examples (action-agent fine-tunes)         never from demo; old: retired        models
    anomaly_templates, drain_clusters                                   never learned; old: simulator templates     models
    playbook                                                            never recorded for demo datasets; old: demo sets only  models
    slo_hour_service, learn_runs                                        never written from demo input        -
    auth_events, mcp_audit, act_audit*                                  audit logs: kept untouched           -
  files under WATCHOVER_HOME
    live/events.jsonl (+ .1 .. .3; LIVE_SPOOL)                          "agent": "simulator" / "demo": 1     lines
    logs/hot/logs-*.db, logs/cold/* (log tiers; logtiers purger)        agent = 'simulator'                  lines
    spool/ (Go receiver spool)                                          the simulator never goes through it  -
    profiles.json (learned column profiles)                             never learned; old: simulator streams   models
    models/<run>/ (WATCHOVER_MODELS_DIR: train.jsonl, eval.jsonl, adapter)   demo-trained runs               models
    config.json                                                         ITSM "demo" selection (reset)        datasets
  in memory (the API process)
    live ring buffer, metrics window, metric_hosts / metric_agent, senders, rollup counters, format hints (_detect), the live
    analysis cache (Services._live_an), history / traces / techstack / applications pending buffers, anomaly / threshold /
    applications / log search / time range / APM / dashboard / ITSM caches, log-query alert counters, loaded datasets.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SIM_AGENT = "simulator"
DEMO_ATTR = "wo.demo"                      # observation attribute: 1 on simulator input (only the ingest path sets it)
DEMO_COL = "demo"                          # column on derived tables: 1 = made from demo input
DEMO_OWNER = "demo (simulator)"
DEMO_RUM_APPS = (("ik-mobil", "android"), ("ik-portal", "web"))
DEMO_PEERS = (("db-01", "172.16.1.55", "PostgreSQL"), ("cache-01", "172.16.1.60", "Redis"), ("kafka-01", "172.16.1.70", "Kafka"), ("hr-db", "172.16.2.10", "Oracle"))
DEMO_FLOW = {"name": "Sipariş → Fatura", "key_regex": r"sipariş\s+(\d{7})",
             "steps": [{"name": "Oluşturuldu", "match": "oluşturuldu"}, {"name": "Kredi onayı", "match": "kredi kontrolü onaylandı"},
                       {"name": "Sevkiyat", "match": "sevkiyat"}, {"name": "Fatura", "match": "fatura"}]}
SIM_FILES = ("sim.jsonl", "metrics.jsonl", "flows.jsonl", "otel.jsonl", "hr-api.jsonl")   # stream names the simulator ingests under
CLAIM_DAYS = 30                           # another sender in the history this recent is real data (no mixing; claimed names)
EXTRA_HOSTS = ("payroll-db",)             # backend lines of the simulated HR sessions (live.simulate_rum)
SIMULATOR = False                         # retired: no setting / environment variable / API turns it on (the test suite sets it for fixtures)
PURGERS: list = []                        # other stores of simulator data (log tiers, sample applications): fn(agent, hosts, dry_run) -> {name: count}

# what each count of purge() belongs to (the confirmation dialog shows the categories; web i18n sim_cat_*)
CATEGORIES = ("lines", "metrics", "traces", "anomalies", "alerts", "topology", "bizflow", "actions", "datasets", "llm_memory", "models")
CATEGORY_OF = {
    "live_lines": "lines", "live_lines_hot": "lines", "live_lines_cold": "lines", "spool_lines": "lines", "live_window": "lines",
    "history_minutes": "metrics", "svc_minutes": "metrics", "live_metrics": "metrics",
    "spans": "traces", "rum_spans": "traces", "rum_sessions": "traces", "rum_apps": "traces",
    "anomalies": "anomalies", "anomaly_learning": "anomalies", "anomaly_signal": "anomalies",
    "alerts": "alerts", "threshold_states": "alerts",
    "inventory": "topology", "applications": "topology", "app_namespaces": "topology", "app_dismissed": "topology", "net_flows": "topology",
    "tech_seen": "topology", "integ_tech": "topology", "team_hosts": "topology", "scan_results": "topology",
    "biz_flows": "bizflow", "biz_events": "bizflow",
    "actions": "actions", "act_rows": "actions",
    "datasets": "datasets", "itsm_demo": "datasets",
    "lessons": "llm_memory", "rules": "llm_memory", "answer_feedback": "llm_memory", "llm_calls": "llm_memory", "eval_runs": "llm_memory",
    "train_runs": "models", "trained_models": "models", "adapters": "models", "act_models": "models", "profiles": "models",
    "drain_clusters": "models", "anomaly_templates": "models", "playbook": "models",
    "history_hours": "metrics", "learn_runs": "models", "scan_runs": "topology", "mcp_tickets": "actions",
}
# tables with a demo column (DEMO_COL) -> purge() count key
FLAGGED = (("svc_minute", "svc_minutes"), ("spans", "spans"), ("rum_sessions", "rum_sessions"), ("anomalies", "anomalies"), ("alerts", "alerts"),
           ("threshold_state", "threshold_states"), ("inventory", "inventory"), ("app_namespaces", "app_namespaces"), ("net_flows", "net_flows"),
           ("tech_seen", "tech_seen"), ("biz_events", "biz_events"), ("biz_flows", "biz_flows"), ("rum_apps", "rum_apps"), ("actions", "actions"),
           ("lessons", "lessons"), ("rules", "rules"), ("answer_feedback", "answer_feedback"), ("llm_calls", "llm_calls"), ("eval_runs", "eval_runs"),
           ("train_runs", "train_runs"))
# host-keyed tables without a demo column: rows of demo host names go (table, host column, count key)
HOST_TABLES = (("anomaly_learning", "host", "anomaly_learning"), ("anomaly_signal", "host", "anomaly_signal"), ("integ_tech", "host", "integ_tech"),
               ("team_hosts", "host", "team_hosts"), ("scan_results", "hostname", "scan_results"))
# other tables purge() covers with their own marker (see STORES)
MARKED_TABLES = ("live_batches", "slo_minute", "applications", "app_dismissed", "anomaly_templates", "drain_clusters", "playbook", "act_models",
                 "act_datasets", "act_dataset_examples", "act_examples", "act_triggers", "act_evidence", "act_runs", "act_steps", "act_proposals",
                 "act_decisions", "act_frozen_inputs", "act_trust_events")
# tables that never hold demo content, and why
EXEMPT = {
    "users": "accounts", "roles": "roles", "rbac_meta": "settings", "teams": "teams", "team_members": "teams", "team_roles": "teams",
    "team_enroll_keys": "teams", "auth_events": "audit log (kept; demo-clean writes counts only)", "otp_codes": "sign-in", "remember_tokens": "sign-in",
    "agents": "enrolled agents (the simulator never enrols; the name is reserved)", "agent_settings": "enrolment key", "sources": "operator config",
    "notify_rules": "operator config", "notify_groups": "operator config", "recipients": "operator config", "threshold_policies": "operator config",
    "log_searches": "operator config", "mcp_keys": "operator config", "mcp_sources": "operator config", "mcp_pulls": "operator config",
    "mcp_tickets": "tickets pulled from external MCP tools", "mcp_audit": "audit log (kept)", "learn_runs": "learner run counts (demo lines are never learned)",
    "slo_hour_service": "never written from demo input", "meta": "log tier counters", "tier_state": "log tier state",
    "scan_runs": "scan run counts (the per-host results are scan_results)",
}
ACT_EXEMPT_PREFIX = "act_"                  # the remaining act_* tables: configuration, keys, audit chain, policy (never fed by demo input)


class DemoDisabled(Exception):
    """A demo-only action on a production installation (the API answers 403)."""


class RealDataPresent(Exception):
    """The simulator refuses to start next to real data (the API answers 409 with the reasons)."""
    status = 409

    def __init__(self, reasons: dict):
        super().__init__("real data present: " + describe(reasons, "en"))
        self.reasons = reasons

    def detail(self) -> dict:
        return {"code": "real_data", "reasons": self.reasons,
                "tr": "Simülasyon başlatılamaz: bu kurulumda gerçek veri var (" + describe(self.reasons, "tr") + "). Gerçek veri ile simülasyon "
                      "verisi asla karışmasın diye simülasyon yalnız gerçek veri olmayan bir kurulumda açılır.",
                "en": "The simulator cannot start: this installation has real data (" + describe(self.reasons, "en") + "). Simulated and real data "
                      "never mix, so the simulator runs only on an installation without real data."}


class ConfirmRequired(Exception):
    """Switching the simulator off deletes the demo data: the caller confirms after seeing the counts (409)."""
    status = 409

    def __init__(self, preview: dict):
        super().__init__("confirmation required")
        self.preview = preview

    def detail(self) -> dict:
        n = self.preview.get("total", 0)
        return {"code": "confirm", "preview": self.preview,
                "tr": f"Simülasyonu kapatmak simülasyonun ürettiği her şeyi siler ({n} kayıt; LLM hafızası ve öğrenilmiş modeller dahil). Onaylayın (confirm: true).",
                "en": f"Switching the simulator off deletes everything it produced ({n} items, LLM memory and learned models included). Confirm it (confirm: true)."}


def describe(reasons: dict, lang: str = "tr") -> str:
    names = {"agents": ("kayıtlı ajan", "enrolled agent"), "senders": ("veri gönderen", "sender"), "rum_apps": ("RUM uygulaması", "RUM app"),
             "sources": ("çekme kaynağı", "pull source"), "traces": ("iz gönderen sunucu", "traced host")}
    out = []
    for k, (tr, en) in names.items():
        v = reasons.get(k) or []
        if v:
            out.append(f"{tr if lang == 'tr' else en}: {', '.join(str(x) for x in v[:5])}{'…' if len(v) > 5 else ''}")
    return "; ".join(out)


def enabled() -> bool:
    """The simulator and the demo content it brings (Datasets demo / sample sets, ITSM demo tickets, act demo controls): retired."""
    return SIMULATOR


def require() -> None:
    """Demo-only content: refused (the simulator is retired; DemoDisabled answers 403)."""
    if not enabled():
        raise DemoDisabled("the simulator is retired: demo content is not available")


# ---------------------------------------------------------------- markers
def is_demo(o) -> bool:
    """An observation from demo input (the ingest path marks it; the sender check covers lines read back from storage)."""
    a = getattr(o, "attributes", None) or {}
    return bool(a.get(DEMO_ATTR)) or a.get("agent") == SIM_AGENT


def sender_name(name: str) -> str:
    """A real sender may not call itself "simulator" (that name marks demo data): its lines stay real under "simulator-ext"."""
    return f"{name}-ext" if str(name or "").strip().lower() == SIM_AGENT else name


def ensure_column(db, table: str) -> None:
    """Add the demo column to a derived table (both engines; a concurrent process adding it first is fine), with a partial index
    over the demo rows only: finding / deleting them never scans a large table, and real rows cost the index nothing."""
    try:
        if DEMO_COL not in db.columns(table):
            db._exec(f"ALTER TABLE {table} ADD COLUMN {DEMO_COL} INTEGER DEFAULT 0")
    except Exception:  # noqa: BLE001 - another process added it first
        pass
    try:
        db._exec(f"CREATE INDEX IF NOT EXISTS {table}_demo1 ON {table}({DEMO_COL}) WHERE {DEMO_COL} = 1")
    except Exception:  # noqa: BLE001
        pass


def keep_real(table: str) -> str:
    """Upsert clause for the demo column: a row real input touched stays real (demo input never turns real data into demo)."""
    return f"{DEMO_COL}=CASE WHEN excluded.{DEMO_COL} = 0 THEN 0 ELSE {table}.{DEMO_COL} END"


def sim_hosts() -> set:
    """Every host name the simulator writes (live lines, metrics, flows, technology demo, seeded inventory): the fallback for
    rows written before the demo markers existed."""
    from .live import SIM_GPU_HOSTS, SIM_HOSTS
    from .techstack import SIM_TECH_EXTRA_INVENTORY, SIM_TECH_FLOWS, SIM_TECH_INVENTORY, SIM_TECH_LISTENS
    return (set(SIM_HOSTS) | set(SIM_GPU_HOSTS) | {r[0] for r in SIM_TECH_INVENTORY} | set(SIM_TECH_EXTRA_INVENTORY)
            | {r[0] for r in SIM_TECH_FLOWS} | {r[0] for r in SIM_TECH_LISTENS} | {r[0] for r in DEMO_PEERS} | set(EXTRA_HOSTS))


def sample_names() -> set:
    """Datasets shipped with the product (samples/*.zip: the Datasets demo button and the sample buttons): demo content."""
    root = Path(__file__).resolve().parents[2] / "samples"
    try:
        return {p.name for p in root.glob("*.zip")} | {"demo_mixed.zip"}
    except OSError:
        return {"demo_mixed.zip"}


def _in(names, cases: bool = False) -> tuple:
    """SQL placeholders and parameters for `col IN (...)`; cases=True adds the upper-case spelling of each name (an index on
    the column stays usable, unlike LOWER(col))."""
    hs = sorted(set(names) | ({n.upper() for n in names} if cases else set()), key=str)
    return ",".join("?" * len(hs)), tuple(hs)


def _rows(db, sql: str, params: tuple = ()) -> list:
    guard = bool(getattr(db, "pg", False) and getattr(db, "in_tx", False))   # PostgreSQL: one failed statement must not abort the preview's transaction
    try:
        if guard:
            db._exec("SAVEPOINT wo_demo")
        out = db._exec(sql, params)
        if guard:
            db._exec("RELEASE SAVEPOINT wo_demo")
        return out
    except Exception:  # noqa: BLE001 - a missing table / column on an old schema means "nothing there"
        if guard:
            try:
                db._exec("ROLLBACK TO SAVEPOINT wo_demo")
            except Exception:  # noqa: BLE001
                pass
        return []


def _since(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")


def _word(h: str):
    return re.compile(rf"(?<![\w.-]){re.escape(h)}(?![\w-])")


# ---------------------------------------------------------------- real data (no mixing)
def self_agents(svc) -> set:
    """Senders of the machine Watchover runs on (its built-in host agent, tag "self"): real, but present on every install."""
    out = set()
    reg = getattr(svc, "agents", None)
    try:
        for a in reg.list() if reg is not None else []:
            if "self" in [t.strip() for t in str(a.get("tags") or "").split(",")]:
                out.add(str(a["name"]))
    except Exception:  # noqa: BLE001
        pass
    if getattr(svc, "selfmon", None) is not None:
        try:
            from .selfmon import host_name
            out.add(host_name())
        except Exception:  # noqa: BLE001
            pass
    return out


def real_sources(svc) -> dict:
    """Evidence of real data: {"agents", "senders", "rum_apps", "sources", "traces"} (only the non-empty ones). The machine's own
    host agent does not count (it runs on every installation; its lines never carry the demo marker)."""
    kb = svc.kb
    me = {n.lower() for n in self_agents(svc)}
    demo_apps = {r["name"] for r in _rows(kb, f"SELECT name FROM rum_apps WHERE note = ? OR {DEMO_COL} = 1", (DEMO_OWNER,))}

    def real(n) -> bool:
        n = str(n or "")
        if not n or n in ("-", "unknown", SIM_AGENT) or n.lower() in me:
            return False
        return not (n.startswith("rum:") and n[4:] in demo_apps)

    out: dict = {}
    reg = getattr(svc, "agents", None)
    try:
        out["agents"] = sorted(a["name"] for a in (reg.list() if reg is not None else []) if a.get("status") == "active" and a["name"].lower() not in me)
    except Exception:  # noqa: BLE001
        pass
    senders = set()
    live = getattr(svc, "live", None)
    if live is not None:
        with live.lock:
            senders |= {a for a in live.agents if real(a)}
    skip = {SIM_AGENT, "-", "unknown", ""} | {n for n in self_agents(svc)} | {f"rum:{a}" for a in demo_apps}
    for _ in range(5):                                       # a few names are enough: each probe stops at its first row (no 30-day scan)
        q, ps = _in(skip)
        r = _rows(kb, f"SELECT sender FROM slo_minute WHERE minute >= ? AND sender NOT IN ({q}) LIMIT 1", (_since(CLAIM_DAYS), *ps))
        if not r:
            break
        skip.add(r[0]["sender"])
        if real(r[0]["sender"]):
            senders.add(r[0]["sender"])
    out["senders"] = sorted(senders)
    since = (datetime.now(timezone.utc) - timedelta(days=CLAIM_DAYS)).isoformat()
    out["rum_apps"] = sorted({r["app"] for r in _rows(kb, f"SELECT DISTINCT app FROM rum_sessions WHERE last >= ? AND COALESCE({DEMO_COL}, 0) = 0", (since,))
                              if r["app"] not in demo_apps})
    out["sources"] = sorted(r["name"] for r in _rows(kb, "SELECT name FROM sources WHERE enabled = 1"))
    hs = {r["host"] for r in _rows(kb, f"SELECT host FROM spans WHERE ts >= ? AND COALESCE({DEMO_COL}, 0) = 0 AND kind NOT IN ('user', 'mobile') "
                                       "AND host NOT IN ('', '-') LIMIT 50", (since,))}
    out["traces"] = sorted(h for h in hs if h.lower() not in me)[:20]
    return {k: v for k, v in out.items() if v}


def present(svc) -> bool:
    """Is any demo data stored? (explicit markers only; at most one row per table is read)"""
    kb = svc.kb
    tables = set(kb.tables())
    probes = [("slo_minute", f"sender = '{SIM_AGENT}'", ()), ("inventory", "owner = ?", (DEMO_OWNER,)), ("rum_apps", "note = ?", (DEMO_OWNER,)),
              ("applications", "origin = 'demo'", ())] + [(t, f"{DEMO_COL} = 1", ()) for t, _k in FLAGGED]     # literals: the partial indexes apply
    for t, where, params in probes:
        if t in tables and _rows(kb, f"SELECT 1 AS x FROM {t} WHERE {where} LIMIT 1", params):
            return True
    if _rows(_live_db(svc), f"SELECT 1 AS x FROM live_batches WHERE agent = '{SIM_AGENT}' LIMIT 1"):
        return True
    reg = getattr(svc, "datasets", None)
    return bool(reg is not None and any(d.get("demo") for d in list(reg.items.values())))


def marked_hosts(svc) -> set:
    """Host names demo input wrote, from the explicit markers (the live window's demo hosts, simulator history rows, demo inventory)."""
    kb = svc.kb
    hs = set()
    live = getattr(svc, "live", None)
    if live is not None:
        hs |= set(getattr(live, "demo_hosts", set()) or ())
        hs |= {h.lower() for h in getattr(live, "metric_hosts", {}).get(SIM_AGENT, set())}
    hs |= {str(r["host"]).lower() for r in _rows(kb, f"SELECT DISTINCT host FROM slo_minute WHERE sender = '{SIM_AGENT}' AND minute >= ?", (_since(CLAIM_DAYS),))}
    hs |= {str(r["hostname"]).lower() for r in _rows(kb, f"SELECT hostname FROM inventory WHERE owner = ? OR {DEMO_COL} = 1", (DEMO_OWNER,))}
    return {h for h in hs if h and h != "-"}


def claimed(svc, hosts: set | None = None) -> set:
    """Host names some real source also uses; their unmarked (host-keyed) rows are never deleted."""
    hosts = {h.lower() for h in (hosts or sim_hosts())}
    if not hosts:
        return set()
    kb = svc.kb; real: set = set()
    tables = set(kb.tables()); q, hp = _in(hosts, cases=True)
    since = _since(CLAIM_DAYS)
    probes = []
    if "slo_minute" in tables:                               # (minute leads the primary key: a bounded range, not the 400-day table)
        probes.append((f"SELECT DISTINCT host FROM slo_minute WHERE minute >= ? AND sender <> ? AND host IN ({q})", (since, SIM_AGENT, *hp)))
    if "live_batches" in tables and "host" in kb.columns("live_batches"):
        probes.append((f"SELECT DISTINCT host FROM live_batches WHERE agent <> ? AND host IN ({q})", (SIM_AGENT, *hp)))
    if "inventory" in tables:
        probes.append((f"SELECT hostname AS host FROM inventory WHERE COALESCE(owner, '') <> ? AND COALESCE({DEMO_COL}, 0) = 0 AND LOWER(hostname) IN ({q})",
                       (DEMO_OWNER, *hp)))
    if "agents" in tables:
        probes.append((f"SELECT name AS host FROM agents WHERE LOWER(name) IN ({q})", hp))
    ldb = _live_db(svc)
    if ldb is not kb:                                        # stored lines in the log tier's hot files (logtiers.HotStore)
        probes.append((f"SELECT DISTINCT host FROM live_batches WHERE agent <> ? AND host IN ({q})", (SIM_AGENT, *hp), ldb))
    for sql, params, *db in probes:
        try:
            real |= {str(r["host"]).lower() for r in (db[0] if db else kb)._exec(sql, params)}
        except Exception:  # noqa: BLE001 - a missing column on an old schema only means "no evidence there"
            pass
    live = getattr(svc, "live", None)
    if live is not None:                                     # the running window: other senders of the same names
        with live.lock:
            for agent, hs in getattr(live, "metric_hosts", {}).items():
                if agent != SIM_AGENT:
                    real |= {h.lower() for h in hs} & hosts
            for o in live.buf:
                if o.host and o.host.lower() in hosts and not is_demo(o):
                    real.add(o.host.lower())
    return real & hosts


def _live_db(svc):
    """Where the live store keeps its stored lines: the main database, or the log tier's hot day files (no DELETE there)."""
    db = getattr(getattr(svc, "live", None), "db", None)
    return db if db is not None else svc.kb


# ---------------------------------------------------------------- the purge
class _Rollback(Exception):
    """Ends the preview's rehearsal: every DELETE it ran on the main database is rolled back."""


class _Purge:
    def __init__(self, svc, dry_run: bool):
        self.svc, self.kb, self.dry = svc, svc.kb, dry_run
        self.rehearse = False                                # preview: the main database's DELETEs run inside a transaction rolled back at
        self.tables = set(self.kb.tables())                  # the end, so rows two markers both match are counted once
        self.counts: dict = {}
        self.cols: dict = {}

    def has(self, table: str, col: str | None = None) -> bool:
        if table not in self.tables:
            return False
        if col is None:
            return True
        if table not in self.cols:
            try:
                self.cols[table] = set(self.kb.columns(table))
            except Exception:  # noqa: BLE001
                self.cols[table] = set()
        return col in self.cols[table]

    def add(self, key: str, n: int) -> None:
        self.counts[key] = self.counts.get(key, 0) + int(n or 0)

    def delete(self, key: str, table: str, where: str, params: tuple = (), db=None) -> int:
        db = db or self.kb
        if db is self.kb and table not in self.tables:
            self.counts.setdefault(key, 0)
            return 0
        what = "COALESCE(SUM(n), 0)" if table == "live_batches" else "COUNT(*)"      # stored lines: count lines, not batches
        rows = _rows(db, f"SELECT {what} AS n FROM {table} WHERE {where}", params)
        n = int(rows[0]["n"] or 0) if rows else 0
        if n and (not self.dry or (self.rehearse and db is self.kb)):
            db._exec(f"DELETE FROM {table} WHERE {where}", params)
        self.add(key, n)
        return n

    def delete_ids(self, key: str, table: str, col: str, ids: list) -> None:
        for i in range(0, len(ids), 200):
            q, ps = _in(ids[i:i + 200])
            self.delete(key, table, f"{col} IN ({q})", ps)
        self.counts.setdefault(key, 0)


def _stop(svc) -> None:
    st = getattr(svc, "sim_stop", None)
    if st is not None:
        st.set()
        svc.sim_stop = None
    live = getattr(svc, "live", None)
    if live is not None:
        live.real_guard = None


def _drop_pending(svc) -> None:
    """Demo rows still waiting in memory for their next flush would be written again after the purge: drop them first."""
    h = getattr(svc, "history", None)
    if h is not None:
        with h.lock:
            for k in [k for k in h.pending_min if k[3] == SIM_AGENT]:
                h.pending_min.pop(k, None)
    tr = getattr(svc, "traces", None)
    if tr is not None:
        with tr.lock:
            for k in [k for k in tr._agg if len(k) > 5 and k[5]]:
                tr._agg.pop(k, None)
            for tid in [t for t, sps in tr._held.items() if any(sp.get("demo") for sp in sps)]:
                tr._held_n -= len(tr._held.pop(tid))
    ts = getattr(svc, "techstack", None)
    if ts is not None:
        with ts._plock:
            for k in [k for k, v in ts._pending.items() if len(v) > 2 and v[2]]:
                ts._pending.pop(k, None)
    apps = getattr(svc, "apps", None)
    if apps is not None:
        for k in [k for k in list(apps._ns_pending) if len(k) > 3 and k[3]]:
            apps._ns_pending.pop(k, None)


def _sim_templates() -> set:
    """The templates of the simulator's messages (generated, not guessed): old anomaly_templates / Drain rows learned from them."""
    import random
    from .analysis import template_of
    from .live import simulate_batch, simulate_flows, simulate_rum
    from .techstack import simulate_tech_lines
    rng = random.Random(11)
    out = set()
    for i in range(60):
        chunks = [simulate_batch(rng, 12, i % 4 == 0, traces=True), simulate_flows(rng), simulate_tech_lines(rng),
                  ("\n".join(simulate_rum(rng, i % 3 == 0)[1]) + "\n").encode()]
        for ch in chunks:
            for ln in ch.decode().splitlines():
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                msg = d.get("msg") or d.get("message") or ""
                if msg:
                    out.add(template_of(msg))
    out.discard("")
    return out


def _ollama_url() -> str:
    return os.environ.get("OLLAMA_URL") or ("http://ollama:11434" if os.environ.get("WATCHOVER_DOCKER") else "http://localhost:11434")


def _purge_models(p: _Purge, own: set) -> None:
    """Fine-tunes trained with demo examples: the train_runs row is retired (status 'retired': Services.llm_cfg('ops') never uses it
    again), its watchover-ops tags leave the bundled Ollama when the active one was demo-trained (the newest real pass is copied
    back), its adapter directory goes. Downloaded base models are never touched."""
    kb, svc = p.kb, p.svc
    if not p.has("train_runs"):
        return
    cols = "id, ts, status, model, note" + (f", {DEMO_COL}" if p.has("train_runs", DEMO_COL) else "")
    runs = _rows(kb, f"SELECT {cols} FROM train_runs WHERE status <> 'retired' ORDER BY id")
    pats = [_word(h) for h in own]
    bad = []
    for r in runs:
        demo = bool(r.get(DEMO_COL))
        note = str(r.get("note") or "")
        d = Path(note.split(" · ")[-1].strip()) if note else None
        if d is not None and not (d.name and (d / "train.jsonl").is_file()):
            d = None
        if not demo and d is not None:                       # old runs: did the exported examples carry demo content?
            try:
                text = (d / "train.jsonl").read_text(encoding="utf-8", errors="replace").lower()
                demo = DEMO_OWNER in text or any(pt.search(text) for pt in pats)
            except OSError:
                demo = False
        if demo:
            bad.append((r, d))
    p.add("train_runs", len(bad))
    if not bad:
        return
    active = _rows(kb, "SELECT id FROM train_runs WHERE status='pass' ORDER BY id DESC LIMIT 1")
    active_id = active[0]["id"] if active else None
    p.add("trained_models", sum(1 for r, _d in bad if r["status"] == "pass"))
    p.add("adapters", sum(1 for _r, d in bad if d is not None))
    if p.dry:
        return
    for r, d in bad:
        kb._exec("UPDATE train_runs SET status='retired', model='', note=? WHERE id=?", (f"retired: trained with demo data · {r.get('note') or ''}"[:500], r["id"]))
        if d is not None:
            shutil.rmtree(d, ignore_errors=True)
    tag = lambda r: "watchover-ops:" + str(r.get("ts") or "")[:10].replace("-", "")     # noqa: E731 - autotrain's dated copy
    bad_tags = {tag(r) for r, _d in bad if r["status"] == "pass"}
    for r in _rows(kb, "SELECT id, ts, note FROM train_runs WHERE status='pass'"):
        if tag(r) in bad_tags:                               # a real pass of the same day: its dated copy was overwritten by demo weights
            kb._exec("UPDATE train_runs SET status='retired', model='', note=? WHERE id=?",
                     (f"retired: its dated model was overwritten by a demo-trained one · {r.get('note') or ''}"[:500], r["id"]))
    if active_id is not None and any(r["id"] == active_id for r, _d in bad):
        try:                                                 # the active watchover-ops was demo-trained: out of the bundled Ollama
            from . import ollama as wo_ollama
            url = _ollama_url()
            prev = _rows(kb, "SELECT ts FROM train_runs WHERE status='pass' ORDER BY id DESC LIMIT 1")
            for t in sorted(bad_tags):
                wo_ollama.delete(url, t)
            wo_ollama.delete(url, "watchover-ops")
            if prev:                                         # the newest real pass is watchover-ops again
                wo_ollama.copy(url, tag(prev[0]), "watchover-ops")
        except Exception:  # noqa: BLE001 - Ollama unreachable: "watchover-ops" may still hold the demo weights, so no run is "pass"
            kb._exec("UPDATE train_runs SET status='retired', note='retired: watchover-ops could not be re-pointed after the demo purge "   # any more:
                     "(the next training makes a new one) · ' || COALESCE(note, '') WHERE status='pass'")      # 'ops' falls back to the chat model
    if hasattr(svc, "_ops_at"):
        svc._ops_at = 0


def _purge_profiles(p: _Purge) -> None:
    """Learned column profiles of the simulator's streams (old data: new demo batches are never remembered)."""
    try:
        from . import profiles as wo_profiles
        with wo_profiles._lock:
            d = wo_profiles.load()
            gone = [k for k, r in d.items() if r.get("source") == "auto" and str(r.get("name") or "") in SIM_FILES]
            if gone and not p.dry:
                for k in gone:
                    d.pop(k, None)
                wo_profiles.save(d)
        p.add("profiles", len(gone))
    except Exception:  # noqa: BLE001
        p.add("profiles", 0)


def _sim_window(svc) -> tuple | None:
    """When the simulator ran alone, from its history rows: (first, last + 10 min) as ISO text. None when it never ran, or when
    another sender (not this machine's own agent) wrote in that window: then a learned template cannot be told apart and stays."""
    kb = svc.kb
    r = _rows(kb, f"SELECT MIN(minute) AS a, MAX(minute) AS b FROM slo_minute WHERE sender = '{SIM_AGENT}'")
    if not r or not r[0]["a"]:
        return None
    end = (datetime.fromisoformat(str(r[0]["b"]) + ":00") + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M")
    me = {n.lower() for n in self_agents(svc)}
    others = {str(x["sender"]).lower() for x in _rows(kb, f"SELECT DISTINCT sender FROM slo_minute WHERE minute >= ? AND minute <= ? AND sender <> '{SIM_AGENT}'",
                                                      (str(r[0]["a"]), end))}
    if others - me - {"", "-"}:
        return None
    return str(r[0]["a"]), end


def _purge_learned(p: _Purge, own: set, win: tuple | None) -> None:
    """anomaly_templates / Drain clusters / playbook entries from demo input. Old data only (new demo input is never learned):
    a template of the simulator's own messages first seen while it ran, a cluster of a shipped sample set or of a demo host."""
    kb = p.kb
    tpls = None
    if win and p.has("anomaly_templates"):
        rows = _rows(kb, "SELECT key, first_seen FROM anomaly_templates WHERE first_seen >= ? AND first_seen <= ?", (win[0], win[1] + ":59"))
        if rows:
            tpls = _sim_templates()
            p.delete_ids("anomaly_templates", "anomaly_templates", "key", [r["key"] for r in rows if r["key"] in tpls])
    p.counts.setdefault("anomaly_templates", 0)
    if p.has("drain_clusters") and _rows(kb, "SELECT 1 AS x FROM drain_clusters LIMIT 1"):
        from .analysis import template_of
        samples = sample_names()
        pats = [_word(h) for h in own]
        ids = []
        for r in _rows(kb, "SELECT id, sample, source, first_seen FROM drain_clusters"):
            s = str(r.get("sample") or "")
            in_win = bool(win) and str(r.get("source") or "") == "live" and win[0] <= str(r.get("first_seen") or "")[:16] <= win[1]
            if in_win and tpls is None:
                tpls = _sim_templates()
            if str(r.get("source") or "") in samples or (s and (any(pt.search(s.lower()) for pt in pats) or (in_win and template_of(s) in tpls))):
                ids.append(r["id"])
        p.delete_ids("drain_clusters", "drain_clusters", "id", ids)
    p.counts.setdefault("drain_clusters", 0)
    db = getattr(getattr(p.svc, "playbook", None), "db", None)
    n = 0
    if db is not None:
        samples = sample_names()
        for r in _rows(db, "SELECT key, datasets FROM playbook"):
            try:
                ds = set(json.loads(r["datasets"] or "[]"))
            except (TypeError, ValueError):
                continue
            if ds and ds <= samples:
                n += 1
                if not p.dry:
                    db._exec("DELETE FROM playbook WHERE key=?", (r["key"],))
    p.add("playbook", n)


def _purge_act(p: _Purge, hosts: set) -> None:
    """Action-agent rows. New demo input never reaches the agent; old data: cases on demo host names and every row keyed to them,
    action-agent fine-tunes whose dataset holds examples of those cases (retired). The hash-chained audit log is never edited."""
    kb = p.kb
    p.counts.setdefault("act_rows", 0); p.counts.setdefault("act_models", 0)
    if not hosts or not p.has("actions", "kind"):
        return
    q, hp = _in(hosts, cases=True)
    cases = [r["id"] for r in _rows(kb, f"SELECT id FROM actions WHERE kind='case' AND host IN ({q})", hp)]
    for i in range(0, len(cases), 200):
        qc, pc = _in(cases[i:i + 200])
        runs = [r["id"] for r in _rows(kb, f"SELECT id FROM act_runs WHERE case_id IN ({qc})", pc)] if p.has("act_runs") else []
        if runs:
            qr, pr = _in(runs)
            for t in ("act_steps", "act_trust_events"):
                if p.has(t, "run_id"):
                    p.delete("act_rows", t, f"run_id IN ({qr})", pr)
        if p.has("act_examples", "case_id") and p.has("act_dataset_examples"):
            ex = [r["id"] for r in _rows(kb, f"SELECT id FROM act_examples WHERE case_id IN ({qc})", pc)]
            if ex:
                qe, pe = _in(ex)
                ds = {r["dataset_id"] for r in _rows(kb, f"SELECT DISTINCT dataset_id FROM act_dataset_examples WHERE example_id IN ({qe})", pe)}
                if ds and p.has("act_models", "dataset_id"):
                    qd, pd = _in(ds)
                    m = _rows(kb, f"SELECT COUNT(*) AS n FROM act_models WHERE dataset_id IN ({qd}) AND status <> 'retired'", pd)
                    p.add("act_models", int(m[0]["n"]) if m else 0)
                    if not p.dry:
                        kb._exec(f"UPDATE act_models SET status='retired' WHERE dataset_id IN ({qd})", pd)
                p.delete("act_rows", "act_dataset_examples", f"example_id IN ({qe})", pe)
        for t in ("act_examples", "act_trust_events", "act_decisions", "act_proposals", "act_frozen_inputs", "act_runs", "act_evidence", "act_triggers"):
            if p.has(t, "case_id"):
                p.delete("act_rows", t, f"case_id IN ({qc})", pc)
        p.delete("actions", "actions", f"id IN ({qc})", pc)


def spool_line_demo(ln: str) -> bool:
    """A spool line of the simulator ("agent": "simulator" or the "demo" marker), decided from its JSON keys, not the message text."""
    if '"simulator"' not in ln and '"demo"' not in ln:
        return False
    try:
        d = json.loads(ln)
    except ValueError:
        return False
    return isinstance(d, dict) and (d.get("agent") == SIM_AGENT or d.get("demo") == 1)


def _clean_spool(svc, dry_run: bool) -> int:
    """The live spool (events.jsonl and its rotations): drop the simulator's lines (the trainer's gate reads these files)."""
    sp = getattr(getattr(svc, "live", None), "spool", None)
    if not sp:
        return 0
    sp = Path(sp)
    n = 0
    for f in [x for x in [sp] + sorted(sp.parent.glob(sp.name + ".*")) if x.is_file() and not x.name.endswith(".demo-clean")]:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            keep = [ln for ln in lines if not spool_line_demo(ln)]
            if len(keep) == len(lines):
                continue
            n += len(lines) - len(keep)
            if not dry_run:
                tmp = f.with_name(f.name + ".demo-clean")
                tmp.write_text("".join(keep), encoding="utf-8")
                os.replace(tmp, f)
        except OSError:                                      # a busy or vanished rotation is skipped, never fatal
            continue
    return n


def _purge_datasets(p: _Purge) -> None:
    """Loaded datasets of demo content (the demo / sample sets, live snapshots taken while demo data was shown), the ITSM demo set."""
    reg = getattr(p.svc, "datasets", None)
    keys = [k for k, d in list(getattr(reg, "items", {}).items()) if d.get("demo")] if reg is not None else []
    if not p.dry and reg is not None:
        for k in keys:
            try:
                reg.delete(k)
            except KeyError:
                pass
        with reg.lock:
            for j in [j for j, job in reg.jobs.items() if job.get("demo")]:
                reg.jobs.pop(j, None)
    p.add("datasets", len(keys))
    try:
        from . import settings as wo_settings
        n = 1 if str(wo_settings.load().get("x_itsm_system") or "") == "demo" else 0
        if n and not p.dry:
            wo_settings.save({"x_itsm_system": ""})
        p.add("itsm_demo", n)
    except Exception:  # noqa: BLE001
        p.add("itsm_demo", 0)


def reset_memory(svc) -> None:
    """Every in-memory copy that could still show or reuse demo data: windows, counters, learned state, caches (real data rebuilds them)."""
    live = getattr(svc, "live", None)
    if live is not None:
        live.purge(SIM_AGENT, tuple(sorted(getattr(live, "demo_hosts", set()) or ())))
        with live.lock:
            for k in [k for k in live._detect if k[0] == SIM_AGENT]:
                live._detect.pop(k, None)
            live.demo_hosts = set()
    tpl = getattr(svc, "templates", None)
    if tpl is not None and hasattr(tpl, "_load"):            # Drain's in-memory clusters: reloaded from what the purge left
        try:
            from .drain import Drain
            tpl.drain = Drain()
            tpl._load()
        except Exception:  # noqa: BLE001
            pass
    an = getattr(svc, "anomalies", None)
    if an is not None:
        for attr, val in (("_sig", {}), ("_learn_memo", {}), ("_season", {}), ("last_found", [])):
            if hasattr(an, attr):
                setattr(an, attr, val)
    for obj, attr, val in ((svc, "_sim_inventory", False), (svc, "_sim_flow", False), (svc, "_sim_tech_n", 0)):
        if obj is not None and hasattr(obj, attr):
            setattr(obj, attr, val)
    lw = getattr(svc, "logwatch", None)
    if lw is not None and isinstance(getattr(lw, "rules", None), dict):
        lw.rules.clear()                                     # stream counters of log-query alerts (rebuilt from the next lines)
    al = getattr(svc, "alerts", None)
    if al is not None and hasattr(al, "last_fired"):
        al.last_fired = []
    clear_caches(svc)


def clear_caches(svc) -> list:
    """Cached computations only (System › Bakım › Önbelleği temizle; also after a purge): the live analysis, anomaly / threshold /
    inventory / applications / technology / log search / time range / APM / dashboard / ITSM caches, agent token lookups. No stored
    data and no learned state is touched; every cache refills from the stores on its next read. Returns the names cleared."""
    done = []
    live = getattr(svc, "live", None)
    if live is not None and hasattr(live, "_memo"):
        live._memo.clear(); done.append("live_analysis")
    svc._live_an = None
    an = getattr(svc, "anomalies", None)
    if an is not None:
        for attr, val in (("_slo_cache", None), ("_ov", None), ("_win", None)):
            if hasattr(an, attr):
                setattr(an, attr, val)
        done.append("anomalies")
    for name, obj, attr, val in (("inventory", getattr(svc, "inventory", None), "_index", None), ("bizflows", getattr(svc, "bizflows", None), "_compiled", None),
                                 ("thresholds", getattr(svc, "thresholds", None), "_cache", (0.0, [])), ("ranges", getattr(svc, "ranges", None), "_cache", {}),
                                 ("log_search", getattr(svc, "logs", None), "_cache", {})):
        if obj is not None and hasattr(obj, attr):
            setattr(obj, attr, val); done.append(name)
    ag = getattr(svc, "agents", None)
    if ag is not None and hasattr(ag, "_cache"):
        ag._cache.clear(); ag._active = None; done.append("agents")
    for name in ("apps", "techstack"):
        o = getattr(svc, name, None)
        if o is not None and hasattr(o, "invalidate"):
            try:
                o.invalidate(); done.append(name)
            except Exception:  # noqa: BLE001
                pass
    for obj, attrs in ((getattr(svc, "apps", None), (("_uni", None), ("_suni", None), ("_ns", None))),     # host / service / namespace universes
                       (getattr(svc, "techstack", None), (("_ix", None), ("_svc", {})))):
        for attr, val in attrs if obj is not None else ():
            if hasattr(obj, attr):
                setattr(obj, attr, val)
    import sys
    for mod, fn in (("watchover.api.routers.apm", lambda m: m._cache.clear()), ("watchover.api.routers.dash", lambda m: m.CACHE._d.clear()),
                    ("watchover.api.routers.itsm", lambda m: m._cache.update({"tickets": None, "at": 0.0, "system": ""}))):
        m = sys.modules.get(mod)
        if m is not None:
            try:
                fn(m); done.append(mod.rsplit(".", 1)[-1])
            except Exception:  # noqa: BLE001
                pass
    return done


def preview(svc) -> dict:
    """What switching the simulator off would delete, per category (nothing is deleted)."""
    return purge(svc, dry_run=True)


def purge(svc, dry_run: bool = False) -> dict:
    """Stop the simulator and delete every demo-derived item in every store of STORES; real data stays.
    Returns {"dry_run", "counts": {store: n}, "categories": {category: n}, "total", "hosts", "kept_hosts", "pending"}."""
    if not dry_run:
        _stop(svc)
        _drop_pending(svc)
    p = _Purge(svc, dry_run)
    kb = p.kb
    marked = marked_hosts(svc)
    win = _sim_window(svc)
    ran = bool(marked) or win is not None or present(svc)    # the simulator left a trace here: only then are its host names looked for
    names = (sim_hosts() if ran else set()) | marked
    real = claimed(svc, names) if names else set()
    own = names - real                                       # names only demo input ever used: their unmarked rows are demo too (old data)
    pending: dict = {}
    ldb = _live_db(svc)
    if ldb is not kb:                                        # hot day files: counted here, removed by the log tier's purger (PURGERS)
        rows = _rows(ldb, f"SELECT COALESCE(SUM(n), 0) AS n FROM live_batches WHERE agent = '{SIM_AGENT}'")
        if rows and int(rows[0]["n"] or 0):
            pending["live_lines_hot"] = int(rows[0]["n"])
    if dry_run:
        try:
            with kb.tx():
                p.rehearse = True
                _purge_database(p, own, win)
                raise _Rollback()
        except _Rollback:
            pass
        finally:
            p.rehearse = False
    else:
        _purge_database(p, own, win)
    _purge_files(p, own, pending)
    return _finish(p, svc, own, real, pending)


def _purge_database(p: _Purge, own: set, win: tuple | None) -> None:
    """Every demo row of the main database (and the playbook file): explicit markers first, then the old-data fallbacks."""
    kb = p.kb
    # ---- stored lines and history (sender "simulator")
    p.delete("live_lines", "live_batches", f"agent = '{SIM_AGENT}'")
    p.delete("history_minutes", "slo_minute", f"sender = '{SIM_AGENT}'")
    # ---- dependants first: the demo RUM apps' sessions and spans, the demo flows' events
    demo_apps = [r["name"] for r in _rows(kb, f"SELECT name FROM rum_apps WHERE note = ? OR COALESCE({DEMO_COL}, 0) = 1", (DEMO_OWNER,))] \
        or [r["name"] for r in _rows(kb, "SELECT name FROM rum_apps WHERE note = ?", (DEMO_OWNER,))]
    if demo_apps:
        qa, pa = _in(demo_apps)
        if p.has("rum_sessions"):
            p.delete("rum_spans", "spans", f"session_id IN (SELECT session_id FROM rum_sessions WHERE app IN ({qa}))", pa)
        p.delete("rum_sessions", "rum_sessions", f"app IN ({qa})", pa)
        p.delete("rum_apps", "rum_apps", f"name IN ({qa})", pa)
    flows = [r["id"] for r in _rows(kb, f"SELECT id FROM biz_flows WHERE {DEMO_COL} = 1")]
    for f in _rows(kb, "SELECT id, key_regex, steps FROM biz_flows WHERE name = ?", (DEMO_FLOW["name"],)):    # old data: the seeded flow, unedited
        try:
            steps = [{"name": s.get("name"), "match": s.get("match")} for s in json.loads(f["steps"] or "[]")]
        except (TypeError, ValueError, AttributeError):
            steps = []
        if f["key_regex"] == DEMO_FLOW["key_regex"] and steps == DEMO_FLOW["steps"] and f["id"] not in flows:
            flows.append(f["id"])
    if flows:
        qf, pf = _in(flows)
        p.delete("biz_events", "biz_events", f"flow_id IN ({qf})", pf)
        p.delete("biz_flows", "biz_flows", f"id IN ({qf})", pf)
    # ---- rows the demo marker flags (demo = 1)
    for table, key in FLAGGED:
        if table == "train_runs":
            continue                                         # retired below (the fine-tuned model behind the row goes too)
        if p.has(table, DEMO_COL):
            p.delete(key, table, f"{DEMO_COL} = 1")
        p.counts.setdefault(key, 0)
    p.delete("inventory", "inventory", "owner = ?", (DEMO_OWNER,))
    # ---- old data: first actions of the shipped demo / sample datasets, rows keyed by names only demo input used
    if p.has("actions", "evidence"):
        for name in sorted(sample_names()):
            p.delete("actions", "actions", "evidence LIKE ? AND COALESCE(kind, 'action') = 'action'", (f"first:{name}:%",))
    _purge_act(p, own)
    if own:
        qh, ph = _in(own, cases=True)
        for key, table in (("spans", "spans"), ("net_flows", "net_flows"), ("anomalies", "anomalies"), ("threshold_states", "threshold_state"),
                           ("tech_seen", "tech_seen"), ("biz_events", "biz_events"), ("app_namespaces", "app_namespaces")):
            if p.has(table, "host"):
                p.delete(key, table, f"host IN ({qh})", ph)
        for table, col, key in HOST_TABLES:
            if p.has(table, col):
                p.delete(key, table, f"{col} IN ({qh})", ph)
        pats = [_word(h) for h in own]
        if p.has("app_dismissed"):
            p.delete_ids("app_dismissed", "app_dismissed", "key", [r["key"] for r in _rows(kb, "SELECT key FROM app_dismissed")
                                                                   if any(pt.search(str(r["key"]).lower()) for pt in pats)])
        if p.has("lessons"):                                 # old memories of demo anomalies / live windows that name demo hosts
            p.delete_ids("lessons", "lessons", "id", [r["id"] for r in _rows(kb, "SELECT id, title, text, dataset FROM lessons WHERE kind IN "
                                                                             "('anomaly', 'pattern', 'resolution', 'chat')")
                                                      if str(r.get("dataset") or "") in sample_names()
                                                      or any(pt.search(f"{r['title']}\n{r['text']}".lower()) for pt in pats)])
        if p.has("answer_feedback"):
            p.delete_ids("answer_feedback", "answer_feedback", "id", [r["id"] for r in _rows(kb, "SELECT * FROM answer_feedback")
                                                                      if any(pt.search(f"{r.get('question', '')}\n{r.get('answer', '')}".lower()) for pt in pats)])
    for _t, _c, key in HOST_TABLES:
        p.counts.setdefault(key, 0)
    p.counts.setdefault("app_dismissed", 0)
    # ---- learned stores, trained models
    _purge_learned(p, own, win)
    _purge_models(p, own)


def _purge_files(p: _Purge, own: set, pending: dict) -> None:
    """Learned column profiles, the live spool, the log tiers and other registered stores, loaded datasets / the ITSM demo set."""
    svc, dry_run = p.svc, p.dry
    _purge_profiles(p)
    p.add("spool_lines", _clean_spool(svc, dry_run))
    for fn in PURGERS:                                       # log tiers and the sample applications, registered by their modules
        try:
            for k, v in (fn(SIM_AGENT, own, dry_run) or {}).items():
                p.add(k, int(v or 0))
            pending.pop("live_lines_hot", None)              # the tier's own purger owns its files
        except Exception:  # noqa: BLE001 - one store failing must not hide the counts of the others
            p.add("errors", 1)
    _purge_datasets(p)


def _finish(p: _Purge, svc, own: set, real: set, pending: dict) -> dict:
    dry_run, kb = p.dry, p.kb
    live = getattr(svc, "live", None)
    if live is not None:                                     # the running window: events, metrics, rollups, the sender itself
        with live.lock:
            p.add("live_window", sum(1 for o in live.buf if is_demo(o)))
            gone = {h.lower() for h in set(live.metric_hosts.get(SIM_AGENT, set())) | set(getattr(live, "demo_hosts", set()) or ())} - real
            p.add("live_metrics", sum(1 for r in live.metrics if str(r[1]).lower() in gone))
            if not dry_run:
                mh = live.metric_hosts.get(SIM_AGENT)
                if mh is not None:
                    mh.difference_update({h for h in mh if h.lower() in real})   # a real "db-01" keeps its metric rows in the window
                live.demo_hosts = {h for h in getattr(live, "demo_hosts", set()) if h not in real}
    if not dry_run:
        reset_memory(svc)
        try:
            kb.apply_rules()                                 # approved demo rules leave the engine too
        except Exception:  # noqa: BLE001
            pass
        svc._demo_check = (time.time(), False)
        svc.demo_notice = None
        svc.demo_purged_at = time.time()
    counts = dict(p.counts)
    cats = {c: 0 for c in CATEGORIES}
    for k, v in counts.items():
        c = CATEGORY_OF.get(k)
        if c and isinstance(v, int):
            cats[c] += v
    total = sum(v for k, v in counts.items() if isinstance(v, int) and k in CATEGORY_OF)
    return {"dry_run": dry_run, "counts": counts, "categories": cats, "total": total, "hosts": sorted(own), "kept_hosts": sorted(real), "pending": pending}


# ---------------------------------------------------------------- the full reset
# Emptied by reset_all whoever wrote them: what analysis made from the logs (table, count key). The rest of the database stays:
# EXEMPT (accounts, teams, settings, agents, sources, policies, audit logs), the action agent's configuration / keys / audit chain,
# operator-defined inventory / applications / flows / RUM apps / team hosts / integration notes (their demo rows go with purge()),
# stored log lines of real senders (live_batches, the hot / cold tiers: the archive auditors may ask for).
RESET_WIPE = (("anomalies", "anomalies"), ("anomaly_learning", "anomaly_learning"), ("anomaly_signal", "anomaly_signal"),
              ("anomaly_templates", "anomaly_templates"), ("drain_clusters", "drain_clusters"), ("alerts", "alerts"), ("threshold_state", "threshold_states"),
              ("act_trust_events", "act_rows"), ("act_steps", "act_rows"), ("act_decisions", "act_rows"), ("act_proposals", "act_rows"),
              ("act_frozen_inputs", "act_rows"), ("act_runs", "act_rows"), ("act_evidence", "act_rows"), ("act_triggers", "act_rows"),
              ("act_dataset_examples", "act_rows"), ("act_examples", "act_rows"), ("act_datasets", "act_rows"), ("actions", "actions"),
              ("lessons", "lessons"), ("rules", "rules"), ("answer_feedback", "answer_feedback"), ("llm_calls", "llm_calls"), ("eval_runs", "eval_runs"),
              ("spans", "spans"), ("svc_minute", "svc_minutes"), ("rum_sessions", "rum_sessions"), ("slo_minute", "history_minutes"),
              ("slo_hour_service", "history_hours"), ("learn_runs", "learn_runs"), ("net_flows", "net_flows"), ("tech_seen", "tech_seen"),
              ("app_namespaces", "app_namespaces"), ("biz_events", "biz_events"), ("scan_results", "scan_results"), ("scan_runs", "scan_runs"),
              ("mcp_tickets", "mcp_tickets"))
RESET_WORD = "SIFIRLA"                     # typed by the operator before a full reset (System › Veri sıfırlama, ./watchover.sh reset-data)
RESET_REQUEST = ".reset-request"           # under WATCHOVER_HOME: the API process runs the reset at its next start, before any thread
RESET_RESULT = "reset-result.json"         # can flush memory back (./watchover.sh reset-data writes the request, then restarts it)


def _wipe_models(p: _Purge) -> None:
    """Every fine-tune: watchover-ops tags leave the bundled Ollama, adapter directories go, the train_runs rows go; the action agent's
    fine-tunes are retired (its speed probes of base models stay). Downloaded base models are never touched."""
    kb = p.kb
    if p.has("train_runs"):
        runs = _rows(kb, "SELECT id, ts, status, note FROM train_runs")
        dirs = []
        for r in runs:
            note = str(r.get("note") or "")
            d = Path(note.split(" · ")[-1].strip()) if note else None
            if d is not None and d.name and (d / "train.jsonl").is_file():
                dirs.append(d)
        passed = [r for r in runs if r["status"] == "pass"]
        p.add("trained_models", len(passed))
        p.add("adapters", len(dirs))
        if not p.dry:
            for d in dirs:
                shutil.rmtree(d, ignore_errors=True)
            if passed:
                try:
                    from . import ollama as wo_ollama
                    url = _ollama_url()
                    for t in sorted({"watchover-ops:" + str(r.get("ts") or "")[:10].replace("-", "") for r in passed}) + ["watchover-ops"]:
                        wo_ollama.delete(url, t)
                except Exception:  # noqa: BLE001 - Ollama unreachable: no run is "pass" any more, so 'ops' falls back to the chat model
                    pass
        p.delete("train_runs", "train_runs", "1 = 1")
        if hasattr(p.svc, "_ops_at"):
            p.svc._ops_at = 0
    if p.has("act_models", "status"):
        where = "status <> 'retired' AND (train_run_id IS NOT NULL OR dataset_id IS NOT NULL)"
        r = _rows(kb, f"SELECT COUNT(*) AS n FROM act_models WHERE {where}")
        p.add("act_models", int(r[0]["n"] or 0) if r else 0)
        if not p.dry or p.rehearse:
            kb._exec(f"UPDATE act_models SET status='retired' WHERE {where}")


def _wipe(p: _Purge) -> None:
    for table, key in RESET_WIPE:
        p.delete(key, table, "1 = 1")
    _wipe_models(p)


def _drop_all_pending(svc) -> None:
    """Rows of any sender still waiting in memory for their next flush (history minutes, trace aggregates, held spans, discoveries)."""
    h = getattr(svc, "history", None)
    if h is not None and hasattr(h, "pending_min"):
        with h.lock:
            h.pending_min.clear()
    tr = getattr(svc, "traces", None)
    if tr is not None and hasattr(tr, "_agg"):
        with tr.lock:
            tr._agg.clear()
            tr._held.clear()
            tr._held_n = 0
    ts = getattr(svc, "techstack", None)
    if ts is not None and hasattr(ts, "_pending"):
        with ts._plock:
            ts._pending.clear()
    apps = getattr(svc, "apps", None)
    if apps is not None and hasattr(apps, "_ns_pending"):
        apps._ns_pending.clear()


def reset_all(svc, dry_run: bool = False) -> dict:
    """The full reset: every row of RESET_WIPE and every fine-tune, whoever made them, plus everything purge() removes (the demo rows of
    the kept tables, the simulator's lines in every store, demo datasets). Returns purge()'s shape with "reset": True; dry_run counts only."""
    if not dry_run:
        _stop(svc)
        _drop_pending(svc)
        _drop_all_pending(svc)
    p = _Purge(svc, dry_run)
    kb = p.kb
    marked = marked_hosts(svc)
    win = _sim_window(svc)
    ran = bool(marked) or win is not None or present(svc)
    names = (sim_hosts() if ran else set()) | marked
    real = claimed(svc, names) if names else set()
    own = names - real
    pending: dict = {}
    ldb = _live_db(svc)
    if ldb is not kb:
        rows = _rows(ldb, f"SELECT COALESCE(SUM(n), 0) AS n FROM live_batches WHERE agent = '{SIM_AGENT}'")
        if rows and int(rows[0]["n"] or 0):
            pending["live_lines_hot"] = int(rows[0]["n"])
    pb = getattr(getattr(svc, "playbook", None), "db", None)
    n_pb = 0
    if pb is not None:                                       # the playbook file: every learned entry
        r = _rows(pb, "SELECT COUNT(*) AS n FROM playbook")
        n_pb = int(r[0]["n"] or 0) if r else 0
        if n_pb and not dry_run:
            pb._exec("DELETE FROM playbook")
    if dry_run:                                              # one rehearsal: a row both the wipe and a demo marker match counts once
        try:
            with kb.tx():
                p.rehearse = True
                _wipe(p)
                _purge_database(p, own, win)
                raise _Rollback()
        except _Rollback:
            pass
        finally:
            p.rehearse = False
    else:
        _wipe(p)
        _purge_database(p, own, win)
    p.counts["playbook"] = n_pb                              # _purge_learned counted the demo entries of these again (dry run)
    _purge_files(p, own, pending)
    n_prof = 0
    try:                                                     # every learned (auto) column profile; ones the operator wrote stay
        from . import profiles as wo_profiles
        with wo_profiles._lock:
            d = wo_profiles.load()
            gone = [k for k, r in d.items() if r.get("source") == "auto"]
            n_prof = len(gone)
            if gone and not dry_run:
                for k in gone:
                    d.pop(k, None)
                wo_profiles.save(d)
    except Exception:  # noqa: BLE001
        pass
    p.counts["profiles"] = n_prof
    return {**_finish(p, svc, own, real, pending), "reset": True}     # _finish: in-memory windows, caches, the live analysis


def clean(svc, dry_run: bool = False) -> dict:
    """The full purge (the name of the earlier, narrower clean-up; ./watchover.sh demo-clean and the System page call it)."""
    return purge(svc, dry_run=dry_run)


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m watchover.demo", description="The simulator's state, the purge of everything it made, the full reset of analysis data.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="simulator available (always false: retired)")
    c = sub.add_parser("clean", help="delete every demo-derived item (counts printed as JSON); real data stays")
    c.add_argument("--dry-run", action="store_true", help="count, delete nothing")
    r = sub.add_parser("reset", help="full reset: every analysis store (anomalies, actions and cases, alerts, learned baselines, the "
                                     "assistant's memory, fine-tunes, history, traces, discovered topology) plus the demo clean-up")
    r.add_argument("--dry-run", action="store_true", help="count, delete nothing")
    r.add_argument("--yes", action="store_true", help="required to delete (the counts first: --dry-run)")
    r.add_argument("--at-start", action="store_true", help="with --yes: ask the API process to reset at its next start (then restart it)")
    r.add_argument("--result", action="store_true", help="print the result of the last reset run at start")
    a = ap.parse_args(argv)
    if a.cmd == "reset" and (a.at_start or a.result):
        from . import settings as wo_settings
        h = wo_settings.home()
        if a.result:
            f = h / RESET_RESULT
            print(f.read_text(encoding="utf-8") if f.is_file() else "{}")
            return 0
        if not a.yes:
            ap.error("--at-start needs --yes")
        (h / RESET_RESULT).unlink(missing_ok=True)
        (h / RESET_REQUEST).write_text(json.dumps({"by": "cli", "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}), encoding="utf-8")
        print(json.dumps({"requested": True}))
        return 0
    if a.cmd == "reset" and not a.dry_run and not a.yes:
        ap.error("reset deletes every analysis store: run with --dry-run first, then --yes")
    if a.cmd == "status":
        print(json.dumps({"demo": enabled()}))
        return 0
    os.environ.setdefault("WATCHOVER_SELFMON", "0")
    from .api.services import Services
    svc = Services(background=False)                         # no receiver, no threads: the database and the files only
    try:
        out = reset_all(svc, dry_run=a.dry_run) if a.cmd == "reset" else clean(svc, dry_run=a.dry_run)
        if not a.dry_run:
            try:
                what = ("data-reset", "full reset") if a.cmd == "reset" else ("demo-clean", "demo data removed")
                svc.users._event("cli", what[0], True, f"{what[1]}: {out['total']} ({json.dumps(out['categories'], sort_keys=True)})")
            except Exception:  # noqa: BLE001 - the audit log never blocks the clean-up
                pass
        print(json.dumps(out, ensure_ascii=False, indent=1))
    finally:
        svc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
