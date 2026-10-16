"""Everything the API needs from the engine, built once per process without Streamlit: the shared database and every store,
the live feed with its rollups, alerting and anomaly tracking, plus the dataset registry with background loads."""
from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime, timezone

from .. import admin as wo_admin
from .. import anomaly as wo_anomaly
from .. import migrate as wo_migrate
from .. import notify as wo_notify
from .. import settings as wo_settings
from ..actions import ActionStore, open_store as open_actions
from ..agents import AgentRegistry
from ..analysis import Analysis
from ..auth import Users
from ..history import History
from ..i18n import t
from ..inventory import Inventory
from ..knowledge import Knowledge
from ..live import LiveStore, start_receiver, start_simulator, SIM_HOSTS
from ..llm import LLMConfig, embed as llm_embed, set_sink as llm_set_sink
from .. import ollama as wo_ollama
from .. import selfmon as wo_selfmon
from ..pipeline import ingest_bytes, ingest_path
from ..playbook import Playbook
from ..profiler import profile
from ..rbac import Roles
from ..sources import Poller, SourceStore

UTC = timezone.utc


def notify_channels() -> dict:
    """SMTP and SMS settings from config.json, read at send time."""
    c = wo_settings.load()
    return {"email": {"host": c.get("smtp_host", ""), "port": c.get("smtp_port", 587), "security": c.get("smtp_security", "starttls"), "user": c.get("smtp_user", ""),
                      "password": c.get("smtp_password", ""), "from_addr": c.get("smtp_from", ""), "from_name": c.get("smtp_from_name", "Watchover")},
            "sms": {k[4:]: c.get(k, "") for k in ("sms_preset", "sms_url", "sms_method", "sms_auth", "sms_user", "sms_password", "sms_token", "sms_from",
                                                  "sms_account", "sms_body", "sms_content_type", "sms_extra")},
            "webhook": {"url": c.get("webhook_url", ""), "kind": c.get("webhook_kind", "generic"), "secret": c.get("webhook_secret", "")}}


class Services:
    def _simulate_users(self, rng, incident: bool) -> None:
        """Simulator extra: web and mobile HR sessions through the RUM path, their backend logs through the live path."""
        from ..live import simulate_rum
        from .. import demo as wo_demo
        if not wo_demo.enabled():                                # production (WATCHOVER_DEMO=0): nothing is ever seeded
            return
        if not getattr(self, "_sim_inventory", False):          # name the simulated peers once (only when they are not in the inventory yet)
            self._sim_inventory = True
            from ..techstack import SIM_TECH_EXTRA_INVENTORY, SIM_TECH_INVENTORY
            for h, ip, role in wo_demo.DEMO_PEERS:
                try:
                    if self.inventory.get(h) is None:
                        self.inventory.upsert({"hostname": h, "ip": ip, "env": "prod", "role": role, "application": SIM_TECH_EXTRA_INVENTORY.get(h, ""), "owner": "demo (simulator)"})
                except Exception:  # noqa: BLE001
                    pass
            for h, ip, os_, app, role in SIM_TECH_INVENTORY:        # the integration map's technology demo: varied stacks
                try:
                    if self.inventory.get(h) is None:
                        self.inventory.upsert({"hostname": h, "ip": ip, "env": "prod", "os": os_, "application": app, "role": role, "owner": "demo (simulator)"})
                except Exception:  # noqa: BLE001
                    pass
        if not hasattr(self, "_sim_tech_n"):
            self._sim_tech_n = 0
        self._sim_tech_n += 1
        if self._sim_tech_n % 10 == 1:                               # instrumented requests of two demo services (traces confirm FastAPI / Spring)
            from ..techstack import simulate_tech_lines
            self.live.ingest("otel.jsonl", simulate_tech_lines(rng), "simulator")
        if not getattr(self, "_sim_flow", False):
            self._sim_flow = True
            if not any(f["name"] == wo_demo.DEMO_FLOW["name"] for f in self.bizflows.list()):
                try:
                    self.bizflows.save(wo_demo.DEMO_FLOW["name"], wo_demo.DEMO_FLOW["key_regex"], [dict(s) for s in wo_demo.DEMO_FLOW["steps"]])
                except Exception:  # noqa: BLE001
                    pass
        apps = {a["name"]: a for a in self.rum.apps()}
        for name, platform in wo_demo.DEMO_RUM_APPS:
            if name not in apps:
                apps[name] = self.rum.create_app(name, platform, note="demo (simulator)")
        sessions, backend = simulate_rum(rng, incident)
        if backend:
            self.live.ingest("hr-api.jsonl", ("\n".join(backend) + "\n").encode(), "simulator")
        for app, doc in sessions:
            self.rum.ingest(apps[app], doc, "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0" if doc["platform"] == "web" else "")

    def _pool_of(self, host: str):
        """The inventory cluster of a host, from one inventory read per minute (not one query per host)."""
        t, m = self._pools
        if time.time() - t > 60:
            try:
                m = {str(r.get("hostname") or "").lower(): r.get("cluster") or None for r in self.inventory.list()}
            except Exception:  # noqa: BLE001
                m = {}
            self._pools = (time.time(), m)
        return m.get((host or "").lower())

    def _on_anomaly_open(self, row: dict) -> None:
        """Every newly opened anomaly (threshold breaches included): company memory, then the action agent (a no-op while
        act_mode = off; never raises). Learning-period anomalies never reach here."""
        try:
            self.memory.remember_anomaly(row)
        except Exception:  # noqa: BLE001
            pass
        act = getattr(self, "act", None)
        if act is not None:
            act.from_anomaly(row)

    def _on_ingest(self, events) -> None:
        """Every accepted live batch: service-level history rollups, then spans / RED metrics (each best effort on its own)."""
        try:
            self.history.add(events)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.traces.add_events(events)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.bizflows.add_events(events)
        except Exception:  # noqa: BLE001
            pass
        ts = getattr(self, "techstack", None)                # technology evidence per host (trace attributes, log formats)
        if ts is not None:
            try:
                ts.observe(events)
            except Exception:  # noqa: BLE001
                pass
        lw = getattr(self, "logwatch", None)                 # alerts from a log query: stream counters
        if lw is not None:
            try:
                lw.add_events(events)
            except Exception:  # noqa: BLE001
                pass

    def __init__(self, background: bool | None = None):
        bg = (os.environ.get("WATCHOVER_API_BACKGROUND", "1") != "0") if background is None else background
        self.lang = wo_settings.load().get("lang", "tr") or "tr"
        self.kb = Knowledge(os.environ.get("DATABASE_URL") or os.environ.get("KNOWLEDGE_DB", "knowledge.db"))
        wo_migrate.auto(self.kb)
        self.kb.apply_rules()
        self.users = Users(self.kb); self.users.bootstrap()
        self.roles = Roles(self.kb)
        from ..teams import Teams
        self.teams = Teams(self.kb, self)                    # several teams, each sees only its own hosts
        self.actions: ActionStore = open_actions(self.kb)    # shared database; the old SQLite actions.db is copied in once (C5)
        self.playbook = Playbook(self.kb if self.kb.pg else os.environ.get("PLAYBOOK_DB", "playbook.db"))
        cfg0 = wo_settings.load()
        self.live = LiveStore(spool=os.environ.get("LIVE_SPOOL", "data/live/events.jsonl"), maxlen=int(cfg0.get("live_ring", 50000) or 50000))
        from .. import logtiers                              # raw lines off the main database: hot day files + cold archive
        self.log_hot = self.log_cold = None
        if logtiers.store_mode() == "files":
            try:
                self.log_hot, self.log_cold = logtiers.HotStore(), logtiers.ColdStore()
            except Exception as e:  # noqa: BLE001 - an unwritable log directory: keep the old layout, say why
                print(f"watchover: log tiers unavailable ({e}); raw lines stay in the main database", flush=True)
                self.log_hot = self.log_cold = None
        if self.log_hot is not None:
            self._log_migrate = logtiers.LogTiers.prepare(self.kb, self.log_hot)   # ids above the old main-database table
            self.live.attach_db(self.log_hot)
            self.live.cold = self.log_cold
        else:                                                 # WATCHOVER_LOG_STORE=db (or no log directory): the main database
            self._log_migrate = False
            self.live.attach_db(self.kb)                      # 100+ hosts: the ring is seconds of data, the database holds the window
        self.log_tiers = logtiers.LogTiers(self.log_hot, self.log_cold, wo_settings.load)
        logtiers.register_purger(self.log_tiers)              # demo clean-up reaches the hot day files and the archive (demo.PURGERS)
        self.apply_capacity_settings(cfg0)
        self.agents = AgentRegistry(self.kb)
        self.sources = SourceStore(self.kb)
        self.inventory = Inventory(self.kb); self.live.enricher = self.inventory.enrich
        self._pools = (0.0, {})
        self.live.rollup.pool_fn = self._pool_of                 # pools: inventory cluster, else the name stem
        self.history = History(self.kb, retention_days=int(cfg0.get("history_retention_days", 400) or 400))
        from ..timerange import RangeView
        self.ranges = RangeView(self.live, self.history)      # any time range for the operations screen
        from ..traces import TraceStore
        self.traces = TraceStore(self.kb, settings=wo_settings.load)     # spans and service rollups rebuilt from the same live events
        from ..bizflow import BizFlows
        self.bizflows = BizFlows(self.kb)                    # business keys followed across systems (funnels, stuck items)
        from ..flows import FlowStore
        self.flows = FlowStore(self.kb); self.live.on_flows = self.flows.add
        from ..rum import RumStore
        self.rum = RumStore(self.kb, self.traces, self.live, settings=wo_settings.load)   # web and mobile user sessions
        self.live.on_ingest = self._on_ingest
        self.notifier = wo_notify.Notifier(self.kb, notify_channels)
        self.anomalies = wo_anomaly.AnomalyTracker(self.kb, self.live)
        from ..drain import TemplateStore
        from ..memory import Memory
        from ..scanner import ScanStore
        self.templates = TemplateStore(self.kb)
        c0 = wo_settings.load()
        self.memory = Memory(self.kb, self.live, self.lang, interval_min=int(c0.get("learn_min", 15) or 0), inventory_fn=None)
        self.anomalies.on_open = self._on_anomaly_open      # memory + action agent; threshold breaches arrive here too (C7)
        self.scanner = ScanStore(self.kb, home=os.environ.get("WATCHOVER_HOME", "") or None)
        from ..techstack import TechStack                    # which technology each integration runs on (integration map)
        self.techstack = TechStack(self.kb, self.live); self.flows.tech = self.techstack; self.traces.on_spans = self.techstack.observe_spans
        self.techstack.attach_teams(self.teams)
        self.anomalies.templates = self.templates
        self.alerts = wo_notify.AlertEngine(self.notifier, self.live, self.agents, self.sources); self.alerts.anomalies = self.anomalies
        from ..logsearch import LogSearch                    # log explorer (docs/design/logs.md): search, tail, detail, alerts from a query
        from ..logwatch import LogWatch, owner_scope
        self.logs = LogSearch(self.live, self.ranges, self.history, self.traces, self.kb, self.users, wo_settings.load)
        self.logwatch = LogWatch(self.logs, self.notifier, lambda email: owner_scope(self, email)); self.alerts.logwatch = self.logwatch
        from ..thresholds import Thresholds                  # per-server threshold policies: warn / crit, sustained minutes, e-mail / SMS / webhook
        self.thresholds = Thresholds(self.kb, self.live, self.notifier, self.anomalies, globals_fn=wo_anomaly.threshold_settings)
        self.live.threshold_fn = self.thresholds.limit_for   # the operations screen's breach list and the anomaly scan use the same levels
        self.anomalies.limit_fn = self.thresholds.limit_for
        self.poller = Poller(self.sources, self.live)
        self.receiver = None
        self.spool_consumer = None
        self.sim_stop = None
        self.selfmon = None
        self._live_an: tuple[int, object] | None = None
        self.datasets = DatasetRegistry(self)
        from ..access import wire as _wire_access
        _wire_access(self)                                   # role-based teams: effective permissions, team hooks on agents / thresholds / alerts
        from ..act import ActService                         # log-driven action agent (docs/design/action-agent.md): no-op while act_mode = off
        self.act = ActService(self)
        self.logwatch.on_case = self.act.from_query           # log-query rules with "Vaka aç" (open_case)
        self.memory.learner.on_incidents = self.act.from_analysis   # live incidents of severity >= high
        self.started = time.time()
        llm_set_sink(self.kb.log_call)                       # every LLM call lands in the quality log
        self.refresh_llm()
        if bg:
            self.history.start(); self.anomalies.start(); self.alerts.start(); self.thresholds.start(); self.poller.start(); self.memory.learner.start()
            self.act.start()                                 # act-cases thread: idle while act_mode = off
            self.log_tiers.start(self.kb if self._log_migrate else None)   # copy the old table once, then the hourly archive job
            c = wo_settings.load()
            port = int(os.environ.get("LIVE_PORT", c.get("live_port", 8600)) or 8600)
            if os.environ.get("WATCHOVER_RECEIVER", "python") == "go":      # the Go receiver owns :8600 and spools; this process consumes the spool
                from ..spool_consumer import SpoolConsumer
                from ..live import rum_request
                rum_h = lambda h, body: rum_request(self.rum, h.get("app_key", ""), h.get("origin") or None, h.get("ua", ""), body)   # noqa: E731
                self.spool_consumer = SpoolConsumer(os.environ.get("WATCHOVER_SPOOL", os.path.join(os.environ.get("WATCHOVER_HOME", "data") or "data", "spool")),
                                                    self.live, self.agents, key_fn=lambda: wo_settings.load().get("live_key") or "",
                                                    handlers={"/rum": rum_h, "/mobile": rum_h}, apps_fn=self.rum.apps).start()
                port = int(os.environ.get("WATCHOVER_INTERNAL_PORT", "8601") or 8601)    # enrolment (proxied by Go) and this machine's own agent
            if os.environ.get("WATCHOVER_API_RECEIVER", "1") == "1":      # the API is the product now: it owns the agent receiver
                try:
                    self.receiver = start_receiver(self.live, port, c.get("live_key") or None, self.agents, self.rum,
                                                   jobs=getattr(self.act, "jobs", None))   # /agent/v1/: the action agent's job channel
                except OSError:
                    self.receiver = None
            if os.environ.get("WATCHOVER_SELFMON", "1") != "0" and c.get("self_monitor", True) and self.receiver is not None:
                try:
                    tok = wo_selfmon.ensure_token(self.agents, wo_settings)
                    self.selfmon = wo_selfmon.SelfMonitor(port, tok, log=os.environ.get("WATCHOVER_LOG", "")).start()
                except Exception:  # noqa: BLE001 - the host agent is best effort
                    self.selfmon = None

    def apply_capacity_settings(self, c: dict | None = None) -> None:
        c = c or wo_settings.load()
        self.live.retention_h = float(c.get("live_retention_h", 48) or 48)
        self.live.window_max_events = int(c.get("live_window_max_events", 200000) or 200000)
        self.live.spool_max_bytes = int(c.get("live_spool_mb", 64) or 64) * 1024 * 1024
        if hasattr(self, "history"):
            self.history.retention_days = int(c.get("history_retention_days", 400) or 400)

    # ---- LLM (optional, never in the core path)
    OPS_MODEL = "watchover-ops"

    def ops_ready(self) -> dict | None:
        """The last auto-trained model that passed the quality gate (cached 60 s), or None."""
        now = time.time()
        if getattr(self, "_ops_at", 0) + 60 > now:
            return self._ops
        self._ops_at = now; self._ops = None
        try:
            if "train_runs" in self.kb.tables():
                rows = self.kb._exec("SELECT ts, model, examples, gate FROM train_runs WHERE status='pass' ORDER BY id DESC LIMIT 1")
                self._ops = dict(rows[0]) if rows else None
        except Exception:  # noqa: BLE001
            self._ops = None
        return self._ops

    def llm_cfg(self, purpose: str = "chat") -> LLMConfig:
        """purpose='chat': the connection the operator configured (LLM page / .env) - never touched by training.
        purpose='ops': the auto-trained watchover-ops model when it exists, passed the gate and 'use it' is on; else the chat one."""
        c = wo_settings.load()
        if purpose == "ops" and c.get("train_use_ops", True) and self.ops_ready():
            url = os.environ.get("OLLAMA_URL") or ("http://ollama:11434" if os.environ.get("WATCHOVER_DOCKER") else "http://localhost:11434")
            return LLMConfig(url, self.OPS_MODEL, "", embed_model=c.get("llm_embed", "") or os.environ.get("LLM_EMBED_MODEL", ""), provider="ollama")
        return LLMConfig(c.get("llm_base", "") or os.environ.get("LLM_BASE_URL", ""), c.get("llm_model", "") or os.environ.get("LLM_MODEL", ""),
                         c.get("llm_key", "") or os.environ.get("LLM_API_KEY", ""), embed_model=c.get("llm_embed", "") or os.environ.get("LLM_EMBED_MODEL", ""),
                         provider=c.get("llm_provider", "") or os.environ.get("LLM_PROVIDER", "auto"))

    def refresh_llm(self) -> LLMConfig:
        """Adopt a local Ollama on first run (like the dashboard did), attach the embedder to the knowledge base."""
        c = wo_settings.load()
        if not c.get("llm_base") and not os.environ.get("LLM_BASE_URL") and not c.get("x_llm_probed"):
            found = wo_ollama.discover()
            vals = {"x_llm_probed": True}
            if found:
                inst = [m["name"] for m in wo_ollama.installed(found)]
                vals.update({"llm_base": found, "llm_provider": "ollama", "llm_model": next((m for m in inst if not any(e in m for e in ("embed", "bge"))), ""),
                             "llm_embed": next((m for m in inst if any(e in m for e in ("embed", "bge"))), "")})
            wo_settings.save(vals)
        cfg = self.llm_cfg()
        self.kb.embedder = (lambda texts, c_=cfg: llm_embed(c_, texts)) if cfg.base_url and cfg.embed_model else None
        self.kb.embed_name = cfg.embed_model if self.kb.embedder else ""
        return cfg

    def scope_of(self, user: dict):
        """None (everything) or the Scope of hosts / services this user may see."""
        return self.access_of(user).scope(None)

    def access_of(self, user: dict):
        """Effective permissions and per-permission team scopes of one account (watchover.access, cached)."""
        return self.access.of(user)

    def live_analysis(self, scope=None):
        """Analysis over the live buffer, recomputed only when new events arrived."""
        from ..analysis import Analysis
        n = self.live.received
        if scope is not None:                                 # a team's view: its own hosts only (not cached, it is per user)
            obs = [o for o in self.live.snapshot() if scope.allows(o.host)]
            return Analysis(obs, []) if obs else None
        if self._live_an is None or self._live_an[0] != n:
            obs = self.live.snapshot()
            self._live_an = (n, Analysis(list(obs), []) if obs else None)
        return self._live_an[1]

    def simulate(self, on: bool) -> bool:
        from .. import demo as wo_demo
        if on and not wo_demo.enabled():                     # production install: the API answers 403
            raise wo_demo.DemoDisabled("the simulator is disabled on this installation (WATCHOVER_DEMO=0)")
        if on and self.sim_stop is None:
            self.sim_stop = start_simulator(self.live, interval=1.0, extra=self._simulate_users)
        elif not on and self.sim_stop is not None:
            self.sim_stop.set(); self.sim_stop = None
            self.live.purge("simulator", tuple(SIM_HOSTS))
        return self.sim_stop is not None

    def status(self) -> dict:
        from .. import __version__
        from ..demo import enabled as demo_enabled
        return {"version": {"version": __version__, **wo_admin.version_info()}, "database": wo_admin.db_info(self.kb), "uptime_s": int(time.time() - self.started),
                "live": {"received": self.live.received, "agents": len(self.live.agents), "receiver": bool(self.receiver), "simulator": self.sim_stop is not None, "demo": demo_enabled(),
                         "selfmon": self.selfmon.status() if self.selfmon else None},
                "llm": {"provider": self.llm_cfg().kind, "model": self.llm_cfg().model, "enabled": self.llm_cfg().enabled},
                "services": {"history": bool(self.history.thread and self.history.thread.is_alive()) if hasattr(self.history, "thread") else None,
                             "anomalies": bool(self.anomalies.thread and self.anomalies.thread.is_alive()), "alerts": self.alerts.runs, "poller": self.poller.thread.is_alive() if getattr(self.poller, "thread", None) else False},
                "anomalies": self.anomalies.stats(), "datasets": len(self.datasets.items), "users": self.users.count(), "capacity": {**self.live.capacity(), "receiver": self.spool_consumer.stats() if self.spool_consumer else {"engine": "python"}}}

    def close(self) -> None:
        for obj in (self.anomalies, self.alerts, self.memory.learner):
            try:
                obj.stop.set()
            except Exception:  # noqa: BLE001
                pass
        if self.receiver is not None:
            self.receiver.shutdown()
        if self.spool_consumer is not None:
            self.spool_consumer.stop()
        if self.selfmon is not None:
            try:
                self.selfmon.stop()
            except Exception:  # noqa: BLE001
                pass
        self.log_tiers.stop.set()
        if self.log_hot is not None:
            self.log_hot.close()
        self.kb.close()


class DatasetRegistry:
    """Loaded datasets (analysis in memory, like the dashboard's session registry) and background load jobs."""
    def __init__(self, svc: Services):
        self.svc = svc
        self.items: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()

    # ---- loading
    def submit(self, name: str, data: bytes, mapping: dict | None = None) -> dict:
        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "name": name, "size": len(data), "state": "running", "stage": "parse", "pct": 0, "file": "", "started": time.time(), "error": "", "dataset": ""}
        inv = self.svc.inventory.as_engine()

        def on_progress(frac: float, fname: str) -> None:
            job["pct"] = int(50 * frac); job["file"] = fname

        def run():
            try:
                obs, report = ingest_bytes(name, data, mapping, progress=on_progress)
                job.update({"pct": 50, "stage": "analyze", "file": ""})
                analysis = Analysis(obs, report, inv)
                job.update({"pct": 85, "stage": "profile"})
                key = self.register(name, analysis, profile(obs, report), time.time() - job["started"], len(data), mapping)
                job.update({"state": "done", "pct": 100, "dataset": key, "elapsed": round(time.time() - job["started"], 2)})
            except Exception as e:  # noqa: BLE001
                job.update({"state": "error", "error": f"{type(e).__name__}: {str(e)[:200]}"})
        with self.lock:
            self.jobs[jid] = job
        threading.Thread(target=run, daemon=True, name=f"api-load-{jid}").start()
        return self.job_view(job)

    def load_path(self, path: str, mapping: dict | None = None) -> dict:
        t0 = time.time()
        obs, report = ingest_path(path, mapping)
        analysis = Analysis(obs, report, self.svc.inventory.as_engine())
        key = self.register(os.path.basename(path), analysis, profile(obs, report), time.time() - t0, os.path.getsize(path) if os.path.isfile(path) else 0, mapping)
        return self.summary(key)

    def register(self, name: str, analysis, prof, elapsed: float, size: int, mapping: dict | None) -> str:
        key = uuid.uuid4().hex[:8]
        with self.lock:
            self.items[key] = {"id": key, "name": name, "analysis": analysis, "profile": prof, "elapsed": round(elapsed, 2), "size": size,
                               "mapping": mapping or {}, "loaded_at": datetime.now(UTC).isoformat(timespec="seconds")}
        self.record_first_actions(analysis, name)
        self.svc.playbook.record(analysis, name)
        try:
            self.svc.templates.learn(analysis.observations[:50000], source=name)          # Drain: every dataset teaches the template miner
        except Exception:  # noqa: BLE001
            pass
        try:
            self.svc.kb.record(analysis, name, self.svc.lang)
        except Exception:  # noqa: BLE001 - lessons are best effort
            pass
        return key

    def record_first_actions(self, a, dataset: str) -> int:
        """Every incident card gets its recommended first action on record (deduplicated per dataset + incident), like the dashboard."""
        from ..analysis import suggested_owner
        st_ = self.svc.actions
        existing = {x["evidence"] for x in st_.list(kind="action")}     # first actions stay plain actions, never cases (C39)
        n = 0
        for inc in a.incidents:
            tag = f"first:{dataset}:{inc.id}"
            if tag in existing or not inc.recommendations:
                continue
            root = a.signal_by_id[inc.root_cause_signal]
            prio = "P1" if inc.severity == "critical" else "P2" if inc.severity == "high" else "P3"
            st_.create(inc.id, inc.recommendations[0][:120], prio, suggested_owner(inc, root), inc.recommendations[0], tag)
            n += 1
        return n

    # ---- reading
    def job_view(self, job: dict) -> dict:
        return {k: v for k, v in job.items() if k != "started"} | {"seconds": round(time.time() - job["started"], 1)}

    def job(self, jid: str) -> dict:
        return self.job_view(self.jobs[jid])

    def list_jobs(self) -> list[dict]:
        return [self.job_view(j) for j in self.jobs.values()]

    def get(self, key: str) -> dict:
        return self.items[key]

    def list(self) -> list[dict]:
        return [self.summary(k) for k in self.items]

    def summary(self, key: str) -> dict:
        from .. import stamp
        d = self.items[key]
        a = d["analysis"]
        prof = {k: v for k, v in d["profile"].items() if k != "per_minute"}
        return {"id": key, "name": d["name"], "loaded_at": d["loaded_at"], "elapsed": d["elapsed"], "size": d["size"], "funnel": a.funnel(),
                "incidents": len(a.incidents), "signals": len(a.signals), "events": len(a.observations), "profile": prof, "stamp": stamp.stamp(a),
                "files": a.report}

    def delete(self, key: str) -> None:
        with self.lock:
            self.items.pop(key)
