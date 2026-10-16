"""The log-driven action agent (docs/design/action-agent.md). `ActService` is wired onto Services as `svc.act`.

While `act_mode = off` (the default after an upgrade, K1 / I14) every hook is a no-op: `emit()` queues nothing, no case is
opened, nothing runs. Turning it on (System page, `PUT /api/act/policy`) starts with cases and guided mode."""
from __future__ import annotations

import collections
import threading
import time
from typing import Any, Dict, Optional

from . import store
from .audit import Audit
from .keys import KeyClient
from .limits import Kills
from .policy import Policy, PolicyError
from .types import MODE_CAP, Trigger, now

QUEUE_MAX = 10000


def _smtp_ok() -> bool:
    try:
        from ..api.services import notify_channels
        return bool((notify_channels().get("email") or {}).get("host"))
    except Exception:  # noqa: BLE001
        return False


def _sso_ok(provider: str) -> bool:
    try:
        from .. import settings
        from ..api.routers.auth import _provider
        return _provider(provider, settings.load()) is not None
    except Exception:  # noqa: BLE001
        return False


class ActService:
    def __init__(self, svc=None, db=None):
        self.svc = svc
        self.db = db if db is not None else svc.kb
        store.ensure(self.db)
        self.keys = KeyClient(None)
        self.audit = Audit(self.db, self.keys)
        self.policy = Policy(self.db, self.audit)
        self.keys.policy = self.policy
        self.kill = Kills(self.db, self.audit)
        from .anchors import Anchors
        self.anchors = Anchors(self.db, self.keys, self.audit, self.policy)     # WP2: external anchors (I19)
        from .stepup import StepUp
        self.stepup = StepUp(self.db, self.policy, self.audit, getattr(svc, "users", None), smtp_ok=_smtp_ok, sso_ok=_sso_ok)
        self.queue: "collections.deque[Trigger]" = collections.deque()
        self.stats = {"emitted": 0, "dropped": 0, "ignored_off": 0, "errors": 0, "routed": 0}
        self._qlock = threading.Lock()
        self._mode = ("off", 0.0)
        self.cases = None                              # CaseRouter (act/cases.py, WP6a)
        self._init_parts()

    def _init_parts(self) -> None:
        """Later work packages attach here (cases, templates, runbooks); kept separate so a broken part never blocks startup."""
        from .templates import Catalogue
        self.catalogue = Catalogue(self.db, self.audit)
        try:
            self.catalogue.sync_vendor()                   # §15.3 step 4: vendor packs -> act_templates (approved); idempotent
        except Exception as e:  # noqa: BLE001 - a broken pack never blocks the app; `act doctor` / templates lint say why
            self.stats["catalogue_error"] = str(e)[:200]
        from .approvals import Approvals
        from .runbooks import Runbooks
        users = getattr(self.svc, "users", None)

        def active(email: str) -> bool:
            if users is None:
                return True
            u = users.get(email)
            return bool(u) and u.get("status") == "active"
        self.approvals = Approvals(self.db, self.audit, active_fn=active)
        self.runbooks = Runbooks(self.db, self.audit, self.catalogue, self.policy, self.approvals)
        from .changes import Changes
        self.changes = Changes(self.db, self.audit, self.approvals)        # WP2: two act.policy holders
        self._init_changes()
        from .evidence import Evidence
        self.evidence = Evidence(self)                     # WP6b: provenance, slots, quarantine V2
        from .jobs import Jobs
        self.jobs = Jobs(self)                             # WP11a: the pull job channel (/agent/v1/), host locks
        from .shadow import Shadow
        self.shadow = Shadow(self)                         # WP7: matcher, frozen inputs sealed at ready, blind sampling, decisions
        from .engine import Engine
        self.engine = Engine(self)                         # WP8: runs, guided / approved steps, verify, rollback, watch, labels
        from .select import Selector
        self.selector = Selector(self)                     # WP13: the model selector on frozen inputs (act-llm queue, one worker)
        try:
            from .cases import CaseRouter
            self.cases = CaseRouter(self)
        except ImportError:
            self.cases = None

    def _init_changes(self) -> None:
        ch, audit = self.changes, self.audit

        def recipients(body, by, inp):
            old = self.anchors.recipients()
            self.policy.set("act_anchor_recipients", list(body.get("recipients") or []), by=by)
            if old:                                        # the old recipients learn about the change (§10.7)
                self.anchors.daily_mail()
            return {"recipients": self.anchors.recipients(), "notified": old}

        def chain_ack(body, by, inp):
            audit.write("chain.acknowledged", object_kind="audit", object_id=body.get("broken_at"), actor=by, actor_kind="user",
                        payload={"broken_at": body.get("broken_at"), "reason": body.get("reason", "")[:300]})
            return {"broken_at": body.get("broken_at")}

        def rotate(body, by, inp):
            r = self.keys.rotate_dispatch()
            audit.write("key.rotated", object_kind="key", object_id="dispatch", actor=by, actor_kind="user",
                        payload={"fingerprint": r.get("fingerprint", ""), "new": r.get("new", "")})
            self._rotation = r                             # hello answers carry it to the actuators (WP11a / WP11c)
            return r

        def export(body, by, inp):
            r = self.keys.export_finish(str(body.get("token") or ""), str(inp.get("part") or ""), by)
            audit.write("key.exported", object_kind="key", object_id="all", actor=by, actor_kind="user", payload={"bytes": len(r.get("blob", ""))})
            return r
        ch.on("anchor_sinks", lambda body, by, inp: self.anchors.set_sinks(body.get("sinks") or [], by))
        ch.on("anchor_recipients", recipients)
        ch.on("chain_ack", chain_ack)
        ch.on("key_rotate", rotate)
        ch.on("key_export", export)

    def ensure_starter(self) -> int:
        """The starter runbooks as shared, disabled drafts (§6.11, §15.3 step 4); loaded once, when the feature is first used."""
        try:
            if self.db._exec("SELECT value FROM rbac_meta WHERE key='act_starter_v1'"):
                return 0
            from .importer import load_starter
            n = load_starter(self.runbooks)
            self.db._exec("INSERT INTO rbac_meta (key, value) VALUES ('act_starter_v1', ?)", (str(n),))
            return n
        except Exception as e:  # noqa: BLE001
            self.stats["starter_error"] = str(e)[:200]
            return 0

    # ---------------------------------------------------------------- mode (cached: emit runs on the anomaly and alert threads)
    @property
    def mode(self) -> str:
        m, at = self._mode
        if time.monotonic() - at > 2.0:
            try:
                m = self.policy.mode
            except Exception:  # noqa: BLE001 - unreadable policy fails closed
                m = "off"
            self._mode = (m, time.monotonic())
        return m

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def set_mode(self, mode: str, by: str, actor_kind: str = "user") -> dict:
        if mode != "off" and self.keys.custody() != "none":
            try:
                self.keys.ensure_keys()                    # §15.3 step 6: keyd creates its keys when act_mode first leaves off
            except Exception as e:  # noqa: BLE001 - keyd down: guided mode still works; the settings banner says why
                self.stats["keys_error"] = str(e)[:200]
        res = self.policy.set("act_mode", mode, by=by, actor_kind=actor_kind, custody=self.keys.custody())
        self._mode = (mode, time.monotonic())
        if mode != "off":
            self.ensure_starter()
        return res

    def caps(self) -> dict:
        """Installation-wide caps on act steps from WP2 (consumed by the trust engine, WP9 / WP16): key custody, external
        anchor freshness (I19) and the chain state."""
        return {"key_custody": self.keys.cap(), "anchor": self.anchors.cap(), "chain": self.audit.chain_cap()}

    # ---------------------------------------------------------------- hooks (never raise into the caller)
    def emit(self, trigger) -> bool:
        """Queue one trigger for the CaseRouter. Bounded (oldest dropped and counted); a no-op while act_mode = off."""
        try:
            if not self.enabled:
                self.stats["ignored_off"] += 1
                return False
            if isinstance(trigger, dict):
                trigger = Trigger(**trigger)
            with self._qlock:
                if len(self.queue) >= QUEUE_MAX:
                    self.queue.popleft()
                    self.stats["dropped"] += 1
                self.queue.append(trigger)
                self.stats["emitted"] += 1
            return True
        except Exception:  # noqa: BLE001
            self.stats["errors"] += 1
            return False

    def drain(self, limit: int = 1000) -> int:
        """Route queued triggers into cases (the act-cases thread calls this; tests call it directly)."""
        n = 0
        while n < limit:
            with self._qlock:
                if not self.queue:
                    break
                trig = self.queue.popleft()
            if self.cases is None or not self.enabled:
                n += 1
                continue
            try:
                self.cases.route(trig)
                self.stats["routed"] += 1
            except Exception:  # noqa: BLE001 - one bad trigger never stops the others
                self.stats["errors"] += 1
            n += 1
        if self.cases is not None:
            try:
                self.cases.flush()
            except Exception:  # noqa: BLE001
                self.stats["errors"] += 1
        return n

    def from_anomaly(self, anomaly: Dict[str, Any]) -> bool:
        """The anomaly tracker's on_open fan-out (threshold breaches arrive here too, C7). Simulator anomalies never become
        triggers: the agent acts on real servers, and its matcher / metrics / trust never learn from demo input (demo.py)."""
        if not self.enabled or self.cases is None or (isinstance(anomaly, dict) and anomaly.get("demo")):
            return False
        try:
            trig = self.cases.trigger_from_anomaly(anomaly)
            return self.emit(trig) if trig is not None else False
        except Exception:  # noqa: BLE001
            self.stats["errors"] += 1
            return False

    def from_query(self, rule: Dict[str, Any], count: Dict[str, Any]) -> int:
        """LogWatch.evaluate, after the dispatch, for rules with open_case (the three-host rule is in the router)."""
        if not self.enabled or self.cases is None or not rule.get("open_case"):
            return 0
        try:
            return sum(1 for t in self.cases.triggers_from_query(rule, count) if self.emit(t))
        except Exception:  # noqa: BLE001
            self.stats["errors"] += 1
            return 0

    def from_analysis(self, analysis) -> int:
        """LiveLearner.learn_once: live incidents of severity >= high whose root cause is not noise (the learner's window holds no
        simulator lines; an analysis that still carries demo observations is ignored)."""
        if not self.enabled or self.cases is None:
            return 0
        from ..demo import is_demo
        if any(is_demo(o) for o in getattr(analysis, "observations", []) or []):
            return 0
        try:
            return sum(1 for t in self.cases.triggers_from_analysis(analysis) if self.emit(t))
        except Exception:  # noqa: BLE001
            self.stats["errors"] += 1
            return 0

    def check_bizflows(self) -> int:
        """Every 60 s from the act thread: business-flow steps with stuck items become triggers."""
        if not self.enabled or self.cases is None or getattr(self.svc, "bizflows", None) is None:
            return 0
        try:
            stuck = self.svc.bizflows.check_stuck(int(self.policy.get("act_bizflow_min", 20) or 20))
            live = getattr(self.svc, "live", None)
            demo_host = getattr(live, "is_demo_host", None) or (lambda h: False)
            stuck = [s for s in stuck if not s.get("demo") and not any(demo_host(h) for h in s.get("hosts") or [])]   # never demo flows
            return sum(1 for t in self.cases.triggers_from_bizflow(stuck) if self.emit(t))
        except Exception:  # noqa: BLE001
            self.stats["errors"] += 1
            return 0

    # ---------------------------------------------------------------- background
    def start(self) -> None:
        """The act-cases thread: drains the trigger queue every second while the feature is on."""
        if getattr(self, "_thread", None) is not None:
            return

        def loop():
            last_flow = last_anchor = last_sel = time.monotonic()
            last_verify = 0.0
            mailed = ""
            while not self._stop.is_set():
                try:
                    if self.enabled and time.monotonic() - last_flow >= 60:
                        last_flow = time.monotonic()
                        self.check_bizflows()
                    if self.enabled and (time.monotonic() - last_anchor >= 30 or self.anchors._pending):
                        last_anchor = time.monotonic()
                        self.anchors.tick()                # hourly anchors, anchors after kills / policy changes, sink retries
                        day = now()[:10]
                        if mailed != day and now()[11:13] >= "07":
                            mailed = day
                            self.anchors.daily_mail()
                    if self.enabled and self.jobs is not None:
                        self.jobs.sweep()                  # queued → expired, silent leased → unknown
                    if self.enabled and getattr(self, "engine", None) is not None:
                        self.engine.tick()                 # job results, verify retries, watch windows, kills of every active run
                    if self.enabled and time.monotonic() - last_sel >= 30 and getattr(self, "selector", None) is not None:
                        last_sel = time.monotonic()
                        self.selector.sweep()              # frozen inputs without a model answer → the act-llm queue
                    if self.enabled and time.monotonic() - last_verify >= 3600:
                        last_verify = time.monotonic()
                        self.audit.verify()                # the chain chip and the `chain` cap follow the last walk
                    if self.enabled and self.queue:
                        self.drain()
                except Exception:  # noqa: BLE001
                    pass
                self._stop.wait(1.0)

        self._stop = threading.Event()
        self._thread = threading.Thread(target=loop, name="act-cases", daemon=True)
        self._thread.start()
        if getattr(self, "selector", None) is not None:
            self.selector.queue.start()                    # the act-llm worker (one model call at a time)

    def stop(self) -> None:
        if getattr(self, "_thread", None) is not None:
            self._stop.set()
            self._thread = None
        if getattr(self, "selector", None) is not None:
            self.selector.queue.stop()

    def _model_state(self) -> dict:
        try:
            st = self.selector.state()
            return {"state": "on" if st["on"] else "off", **st, "queue": len(self.selector.queue)}
        except Exception:  # noqa: BLE001
            return {"state": "off"}

    # ---------------------------------------------------------------- status (header chips; GET /api/act/status)
    def status(self) -> dict:
        mode = self.mode
        keys = self.keys.status()
        kills = self.kill.active()
        head = self.audit.head()
        fr = self.anchors.freshness()
        last = self.anchors.latest()
        chain = {"seq": head["seq"], "mac": keys["mac"], "anchor": {"seq": last["seq"], "ts": last["ts"]} if last else None,
                 "anchor_age_min": fr["age_min"], "anchor_fresh": fr["fresh"], "sinks": fr["configured"], **self.audit.chain_state()}
        if chain["state"] == "unchecked" and keys["mac"] == "p0":
            chain["state"] = "placeholder"
        return {"mode": mode, "mode_cap": MODE_CAP.get(mode, 0), "enabled": mode != "off", "custody": keys["custody"],
                "signing": keys["available"], "keys": keys, "kill": kills, "killed_global": any(k["scope"] == "global" for k in kills),
                "chain": chain, "caps": {"key_custody": self.keys.cap(), "anchor": 4 if fr["fresh"] else 2, "chain": self.audit.chain_cap()},
                "breaker": {"open": False}, "model": self._model_state(), "queue": len(self.queue), "stats": dict(self.stats),
                "residual_risk_accepted": bool(self.policy.get("act_residual_risk_accepted")), "ts": now()}


__all__ = ["ActService", "PolicyError"]
