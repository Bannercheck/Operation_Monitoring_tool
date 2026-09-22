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
from ..actions import ActionStore
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
                                                  "sms_account", "sms_body", "sms_content_type", "sms_extra")}}


class Services:
    def __init__(self, background: bool | None = None):
        bg = (os.environ.get("WATCHOVER_API_BACKGROUND", "1") != "0") if background is None else background
        self.lang = wo_settings.load().get("lang", "tr") or "tr"
        self.kb = Knowledge(os.environ.get("DATABASE_URL") or os.environ.get("KNOWLEDGE_DB", "knowledge.db"))
        wo_migrate.auto(self.kb)
        self.kb.apply_rules()
        self.users = Users(self.kb); self.users.bootstrap()
        self.roles = Roles(self.kb)
        self.actions = ActionStore(self.kb if self.kb.pg else os.environ.get("ACTIONS_DB", "actions.db"))
        self.playbook = Playbook(self.kb if self.kb.pg else os.environ.get("PLAYBOOK_DB", "playbook.db"))
        self.live = LiveStore(spool=os.environ.get("LIVE_SPOOL", "data/live/events.jsonl"))
        self.agents = AgentRegistry(self.kb)
        self.sources = SourceStore(self.kb)
        self.inventory = Inventory(self.kb); self.live.enricher = self.inventory.enrich
        self.history = History(self.kb); self.live.on_ingest = self.history.add
        self.notifier = wo_notify.Notifier(self.kb, notify_channels)
        self.anomalies = wo_anomaly.AnomalyTracker(self.kb, self.live)
        from ..drain import TemplateStore
        from ..memory import Memory
        from ..scanner import ScanStore
        self.templates = TemplateStore(self.kb)
        c0 = wo_settings.load()
        self.memory = Memory(self.kb, self.live, self.lang, interval_min=int(c0.get("learn_min", 15) or 0), inventory_fn=None)
        self.anomalies.on_open = self.memory.remember_anomaly
        self.scanner = ScanStore(self.kb, home=os.environ.get("WATCHOVER_HOME", "") or None)
        self.anomalies.templates = self.templates
        self.alerts = wo_notify.AlertEngine(self.notifier, self.live, self.agents, self.sources); self.alerts.anomalies = self.anomalies
        self.poller = Poller(self.sources, self.live)
        self.receiver = None
        self.sim_stop = None
        self.selfmon = None
        self._live_an: tuple[int, object] | None = None
        self.datasets = DatasetRegistry(self)
        self.started = time.time()
        llm_set_sink(self.kb.log_call)                       # every LLM call lands in the quality log
        self.refresh_llm()
        if bg:
            self.history.start(); self.anomalies.start(); self.alerts.start(); self.poller.start(); self.memory.learner.start()
            c = wo_settings.load()
            if os.environ.get("WATCHOVER_API_RECEIVER", "1") == "1":      # the API is the product now: it owns the agent receiver
                try:
                    self.receiver = start_receiver(self.live, int(os.environ.get("LIVE_PORT", c.get("live_port", 8600)) or 8600), c.get("live_key") or None, self.agents)
                except OSError:
                    self.receiver = None
            if os.environ.get("WATCHOVER_SELFMON", "1") != "0" and c.get("self_monitor", True) and self.receiver is not None:
                try:
                    tok = wo_selfmon.ensure_token(self.agents, wo_settings)
                    self.selfmon = wo_selfmon.SelfMonitor(int(os.environ.get("LIVE_PORT", c.get("live_port", 8600)) or 8600), tok, log=os.environ.get("WATCHOVER_LOG", "")).start()
                except Exception:  # noqa: BLE001 - the host agent is best effort
                    self.selfmon = None

    # ---- LLM (optional, never in the core path)
    def llm_cfg(self) -> LLMConfig:
        c = wo_settings.load()
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

    def live_analysis(self):
        """Analysis over the live buffer, recomputed only when new events arrived."""
        from ..analysis import Analysis
        n = self.live.received
        if self._live_an is None or self._live_an[0] != n:
            obs = self.live.snapshot()
            self._live_an = (n, Analysis(list(obs), []) if obs else None)
        return self._live_an[1]

    def simulate(self, on: bool) -> bool:
        if on and self.sim_stop is None:
            self.sim_stop = start_simulator(self.live, interval=1.0)
        elif not on and self.sim_stop is not None:
            self.sim_stop.set(); self.sim_stop = None
            self.live.purge("simulator", tuple(SIM_HOSTS))
        return self.sim_stop is not None

    def status(self) -> dict:
        from .. import __version__
        return {"version": {"version": __version__, **wo_admin.version_info()}, "database": wo_admin.db_info(self.kb), "uptime_s": int(time.time() - self.started),
                "live": {"received": self.live.received, "agents": len(self.live.agents), "receiver": bool(self.receiver), "simulator": self.sim_stop is not None,
                         "selfmon": self.selfmon.status() if self.selfmon else None},
                "llm": {"provider": self.llm_cfg().kind, "model": self.llm_cfg().model, "enabled": self.llm_cfg().enabled},
                "services": {"history": bool(self.history.thread and self.history.thread.is_alive()) if hasattr(self.history, "thread") else None,
                             "anomalies": bool(self.anomalies.thread and self.anomalies.thread.is_alive()), "alerts": self.alerts.runs, "poller": self.poller.thread.is_alive() if getattr(self.poller, "thread", None) else False},
                "anomalies": self.anomalies.stats(), "datasets": len(self.datasets.items), "users": self.users.count()}

    def close(self) -> None:
        for obj in (self.anomalies, self.alerts, self.memory.learner):
            try:
                obj.stop.set()
            except Exception:  # noqa: BLE001
                pass
        if self.receiver is not None:
            self.receiver.shutdown()
        if self.selfmon is not None:
            try:
                self.selfmon.stop()
            except Exception:  # noqa: BLE001
                pass
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
        existing = {x["evidence"] for x in st_.list()}
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
