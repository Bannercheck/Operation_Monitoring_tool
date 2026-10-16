"""The run engine (docs/design/action-agent.md §6, §9.7, §10.3–§10.6; WP8: C22, C46, C48, C49, R7, R8, I7, I13).

A run executes one published runbook version for one case. Steps go in order with `when` conditions and `choose` jumps:

* `diagnose` runs through the host's actuator at once when the step may (light R0 templates, a confirmed actuator that has the
  template, act_mode on, no kill, within the diagnosis budget); otherwise it is a guided step with a paste box.
* `act` / `rollback` follow the step's level: min(cell level, mode cap, key custody, external anchor, chain, evidence
  provenance, kill → L1, freeze → L1, insecure install → L1, actuator unable → L1, irreversible → L2, L3/L4 not built (WP16)
  → L2). L ≤ 1 is guided (the manual executor renders the exact command); L2 waits for an approval (WP12 calls
  `dispatch_approved`, which dispatches through `act.jobs` and takes the host lock in the dispatch transaction).
* `verify` always runs automatically when its data source is available (server-side checks and verify jobs), with `retries`
  and `interval_s`; without a confirmed actuator a template check becomes a paste box. Its result is the outcome of the act
  step it verifies; verify steps carry no trust events.
* A failed verify runs the act step's `rollback` (guided or approved), then the **post-rollback verify** (the same checks
  again, R8): pass → the run is `rolled_back`; fail, or a failed rollback → `rollback_failed`, a host stop is kept and the
  case is escalated.
* After the last act step verified, the run **watches** the host for `watch.minutes` (§4.7, act/watch.py), then act/label.py
  writes exactly one outcome per step and the exec trust events.

Kills are checked at start and before every dispatch (I7); "Sert durdur" (hard) also cancels in-flight jobs. "Bana bırak"
turns any waiting step into a guided one. A job whose result never arrives becomes `unknown`: the engine runs the read-only
verify once and asks a person ("Başarılı say / Tekrar dene / Bana bırak"); the host lock stays until then.

Times use `self.clock` (tests pass a fake clock) and `limits.time_scale()` (WATCHOVER_ACT_TIME_SCALE, demo only)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import privacy
from . import conditions, label as labels, verify as V, watch as W
from .executors.agent import AgentExecutor
from .executors.manual import ManualExecutor
from .limits import Budgets, Windows, time_scale
from .types import MODE_CAP, OPEN_PHASES, cell_key, env_tier

ACTIVE_RUN = ("planned", "running", "blocked", "paused", "verifying", "watching", "rolling_back", "unknown")
DONE_STEP = ("succeeded", "done_by_human", "skipped", "failed", "timed_out", "rejected_by_agent", "cancelled", "deviated", "rejected",
             "vetoed")
WAIT_STEP = ("guided", "awaiting_approval", "dispatched", "running", "unknown", "ready", "waiting")
OK_STEP = ("succeeded", "done_by_human")
PRECOND_DEFAULT_ELSE = {"agent_online": "guided", "actuator_ready": "guided", "template_available": "guided", "no_other_run_on_host": "stop",
                        "metric_at_least": "ask", "metric_below": "ask", "service_state": "ask", "fact": "ask", "case_still_active": "ask",
                        "not_frozen": "guided", "in_window": "wait", "pool_healthy_at_least": "stop", "inventory_status": "stop",
                        "change_approved": "wait", "clock_skew_below": "guided"}
STEP_REF = re.compile(r"steps\.([A-Za-z0-9_]+)")


class EngineError(Exception):
    def __init__(self, code: str, tr: str, en: str, status: int = 409, **extra):
        super().__init__(en)
        self.code, self.tr, self.en, self.status, self.extra = code, tr, en, status, extra

    def detail(self) -> dict:
        return {"code": self.code, "tr": self.tr, "en": self.en, **self.extra}


def _j(v, default):
    if isinstance(v, (dict, list)):
        return v
    try:
        out = json.loads(v or "null")
    except (TypeError, ValueError):
        return default
    return default if out is None else out


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class Engine:
    def __init__(self, act, clock: Optional[Callable[[], datetime]] = None, sources=None):
        self.act, self.db = act, act.db
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sources = sources or V.Sources(act)
        self.agent = AgentExecutor(act)
        self.manual = ManualExecutor(act)
        self.windows = Windows(act.db)
        self.budgets = Budgets(act.db, act.policy)

    # ---------------------------------------------------------------- time
    def now(self) -> datetime:
        return self.clock()

    def iso(self, d: Optional[datetime] = None) -> str:
        return (d or self.now()).astimezone(timezone.utc).isoformat(timespec="seconds")

    def later(self, seconds: float) -> str:
        return self.iso(self.now() + timedelta(seconds=max(0.0, float(seconds)) / time_scale()))

    # ---------------------------------------------------------------- reads
    def run(self, rid: int) -> dict:
        rows = self.db._exec("SELECT r.*, b.key AS runbook_key FROM act_runs r LEFT JOIN act_runbooks b ON b.id=r.runbook_id WHERE r.id=?",
                             (int(rid),))
        if not rows:
            raise KeyError(rid)
        r = dict(rows[0])
        r["state"] = _j(r.get("state"), {})
        r["params"] = _j(r.get("params"), {})
        r["param_sources"] = _j(r.get("param_sources"), {})
        r["mode_reasons"] = _j(r.get("mode_reasons"), [])
        return r

    def steps(self, rid: int) -> List[dict]:
        out = []
        for s in self.db._exec("SELECT * FROM act_steps WHERE run_id=? ORDER BY seq", (int(rid),)):
            s = dict(s)
            for k, d in (("params", {}), ("fields", {}), ("captures", {}), ("verify", {}), ("post_rollback_verify", {}), ("side_effect", {}),
                         ("human_claim", {}), ("dry_output", {})):
                s[k] = _j(s.get(k), d)
            out.append(s)
        return out

    def get(self, rid: int) -> dict:
        r = self.run(rid)
        r["steps"] = self.steps(rid)
        return r

    def active_for_case(self, cid: int) -> Optional[dict]:
        rows = self.db._exec(f"SELECT id FROM act_runs WHERE case_id=? AND status IN ({','.join('?' * len(ACTIVE_RUN))}) ORDER BY id DESC LIMIT 1",
                             (int(cid),) + ACTIVE_RUN)
        return self.run(rows[0]["id"]) if rows else None

    def _version(self, run: dict) -> dict:
        r = self.db._exec("SELECT * FROM act_runbook_versions WHERE id=?", (run["version_id"],))
        v = dict(r[0])
        v["body"] = _j(v.get("body"), {})
        return v

    def _body_steps(self, run: dict) -> Dict[str, dict]:
        return {s["id"]: s for s in self._version(run)["body"].get("steps") or []}

    def _case(self, cid: int) -> dict:
        return dict(self.db._exec("SELECT * FROM actions WHERE id=?", (int(cid),))[0])

    def _inv(self, host: str) -> dict:
        svc = getattr(self.act, "svc", None)
        try:
            return (svc.inventory.resolve(host) or {}) if svc is not None and getattr(svc, "inventory", None) is not None and host else {}
        except Exception:  # noqa: BLE001
            return {}

    # ---------------------------------------------------------------- writes
    def _set_run(self, rid: int, **f) -> None:
        if "state" in f and not isinstance(f["state"], str):
            f["state"] = json.dumps(f["state"], default=str)
        for k in ("params", "param_sources", "mode_reasons"):
            if k in f and not isinstance(f[k], str):
                f[k] = json.dumps(f[k], default=str)
        self.db._exec(f"UPDATE act_runs SET {', '.join(f'{k}=?' for k in f)} WHERE id=?", (*f.values(), int(rid)))

    def _set_step(self, sid: int, **f) -> None:
        for k in ("params", "fields", "captures", "verify", "post_rollback_verify", "side_effect", "human_claim", "dry_output"):
            if k in f and not isinstance(f[k], str):
                f[k] = json.dumps(f[k], default=str)
        self.db._exec(f"UPDATE act_steps SET {', '.join(f'{k}=?' for k in f)} WHERE id=?", (*f.values(), int(sid)))

    def _audit(self, event: str, run: dict, by: str = "system", **payload) -> None:
        self.act.audit.write(event, object_kind="run", object_id=run["id"], actor=by, actor_kind="user" if "@" in by else "system",
                             team_id=run.get("team_id"), payload={"case_id": run.get("case_id"), **payload})

    def _case_phase(self, run: dict, phase: str, by: str = "system", **fields) -> None:
        try:
            case = self._case(run["case_id"])
            if case.get("phase") != phase or fields:
                self.act.cases.transition(run["case_id"], phase, by, **fields)
        except Exception:  # noqa: BLE001 - the run never fails because of the case row
            pass

    # ---------------------------------------------------------------- parameters (§6.3)
    def resolve_params(self, case: dict, body: dict, given: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """{values, sources, missing, ambiguous: {name: [values]}, invalid: {name: reason}} — `given` are a person's answers
        or the decision's parameters; a value that fails validation falls back to `ask` with the reason."""
        from .params import ParamError, validate
        given = dict(given or {})
        ctx = self.act.shadow.matcher.context(case)
        out = {"values": {}, "sources": {}, "missing": [], "ambiguous": {}, "invalid": {}}
        vctx = {"case_host": case.get("host") or "", "host": case.get("host") or "", "health_url": ctx["inventory"].get("health_url", "")}
        for name, spec in (body.get("params") or {}).items():
            if spec.get("type") == "host":
                out["values"][name], out["sources"][name] = case.get("host") or "", "case.host"
                continue
            if name in given and given[name] not in (None, ""):
                try:
                    out["values"][name] = validate(spec, given[name], vctx)
                    out["sources"][name] = "ask"
                except ParamError as e:
                    out["invalid"][name] = e.code
                    out["missing"].append(name)
                continue
            vals = self.act.shadow.matcher.values(name, spec, ctx)
            if len(vals) == 1 or (vals and "ask" not in (spec.get("from") or []) and len({str(v["value"]) for v in vals}) == 1):
                out["values"][name] = vals[0]["value"]
                out["sources"][name] = vals[0]["origin"]
            elif len(vals) > 1:
                out["ambiguous"][name] = [v["value"] for v in vals]
                out["missing"].append(name)
            else:
                if any(str(f).startswith("steps.") for f in spec.get("from") or []):
                    continue                                          # filled when the step's capture arrives
                out["missing"].append(name)
        return out

    def _ref(self, ref: str, run: dict, steps: Dict[str, dict]):
        if ref.startswith("steps."):
            parts = ref.split(".")
            st = steps.get(parts[1]) if len(parts) > 2 else None
            if st is None:
                return None
            cur: Any = {**(st.get("fields") or {}), **(st.get("captures") or {}), "out": st.get("fields") or {}}
            for p in parts[2:]:
                if not isinstance(cur, dict) or p not in cur:
                    return None
                cur = cur[p]
            return cur
        if ref.startswith("inventory."):
            return self._inv(run.get("host") or "").get(ref[10:])
        if ref.startswith("case."):
            return self._case(run["case_id"]).get(ref[5:])
        return run["params"].get(ref)

    def args(self, run: dict, bstep: dict, by_key: Dict[str, dict]) -> Tuple[Dict[str, Any], List[str]]:
        """The step's arguments with {param} / {steps.x.out.y} / {inventory.attr} filled; names still missing."""
        vals, missing = {}, []
        for k, v in (bstep.get("args") or {}).items():
            if isinstance(v, str):
                m = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}", v)
                if m:
                    x = self._ref(m.group(1), run, by_key)
                    if x in (None, ""):
                        missing.append(m.group(1))
                    else:
                        vals[k] = x
                    continue
                vals[k] = V.render(v, {**run["params"], **{f"inventory.{a}": b for a, b in self._inv(run.get("host") or "").items()}})
            else:
                vals[k] = v
        return vals, missing

    def ctx(self, run: dict, steps: List[dict]) -> Dict[str, Any]:
        """Conditions context: steps.<id>.<capture> / .out / ran / ok / failed / skipped, case.*, inventory.*."""
        sc = {}
        for s in steps:
            sc[s["step_key"]] = {**(s.get("fields") or {}), **(s.get("captures") or {}), "out": s.get("fields") or {},
                                 "ran": s["status"] in OK_STEP + ("failed", "timed_out"), "ok": s["status"] in OK_STEP,
                                 "failed": s["status"] in ("failed", "timed_out", "rejected_by_agent"), "skipped": s["status"] == "skipped"}
        case = self._case(run["case_id"])
        return {"steps": sc, "case": {k: case.get(k) for k in ("env", "severity", "source", "host", "service")},
                "inventory": self._inv(run.get("host") or "")}

    # ---------------------------------------------------------------- levels (§7.6; trust engine WP9, autonomy WP16 later)
    def cell_level(self, run: dict, step_key: str, default: int) -> int:
        ck = cell_key(run.get("team_id"), run.get("runbook_key") or "", step_key, run.get("env") or "", run.get("host_class") or "")
        r = self.db._exec("SELECT level, state FROM act_cells WHERE cell_key=?", (ck,))
        if not r:
            return default
        if r[0]["state"] in ("frozen",):
            return min(int(r[0]["level"] or 0), 1)
        return int(r[0]["level"] if r[0]["level"] is not None else default)

    def level(self, run: dict, st: dict, bstep: dict, tmpl: Optional[dict], mode: str) -> Tuple[int, List[str]]:
        """Effective level of an act / rollback step and the caps that bind it."""
        caps: List[Tuple[str, int]] = [("cell", self.cell_level(run, st["step_key"], 1)), ("mode", MODE_CAP.get(self.act.mode, 0))]
        try:
            caps += [("key_custody", self.act.keys.cap()), ("anchor", self.act.anchors.cap()), ("chain", self.act.audit.chain_cap())]
        except Exception:  # noqa: BLE001 - keyd unreachable: guided (I13)
            caps.append(("key_custody", 1))
        try:
            caps.append(("evidence", int(self.act.evidence.cap(run["case_id"], run["id"])["level"])))
        except Exception:  # noqa: BLE001
            caps.append(("evidence", 1))
        k = self.act.kill.killed(team_id=run.get("team_id"), host=run.get("host") or "", runbook=run.get("runbook_key") or "",
                                 template=(tmpl or {}).get("id", ""))
        if k:
            caps.append(("kill", 1))
        fr = self.windows.frozen(run.get("team_id"), run.get("env") or "", run.get("host_class") or "", run.get("host") or "", self.now())
        if fr and not self._version(run)["body"].get("freeze_exempt"):
            caps.append(("freeze", 1))
        if (tmpl or {}).get("reversibility") == "irreversible":
            caps.append(("irreversible", 2))                          # I5
        if self.budgets.runbook_host(run["runbook_id"], run.get("host") or "", self.now(), exclude_run=run["id"]):
            caps.append(("loop_guard", 2))                            # same runbook on the same host: ≤ 3 / 24 h, ≥ 30 min apart (§10.4)
        caps.append(("autonomy", 2))                                  # L3 / L4 need WP16 (veto countdown, run gate)
        if run["state"].get("guided_all"):
            caps.append(("precondition", 1))
        ok, why = self.agent.can_run({"template_id": (tmpl or {}).get("id", ""), "template_sha256": (tmpl or {}).get("sha256", "")},
                                     run.get("host") or "", mode)
        if not ok:
            caps.append((why or "actuator", 1))
        lv = min(c[1] for c in caps)
        return lv, [name for name, c in caps if c == lv and name != "cell"]

    # ---------------------------------------------------------------- start
    def plan(self, cid: int, runbook_key: str, version: Optional[int] = None, params: Optional[Dict[str, Any]] = None) -> dict:
        """A read-only preview of a run (shown after a decision; nothing is created)."""
        case = self._case(cid)
        ver = self.act.runbooks.version(runbook_key, version or self.act.runbooks.get(runbook_key)["current_version"])
        res = self.resolve_params(case, ver["body"], params)
        steps = [{"key": s["id"], "type": s.get("type"), "title": V.render(s.get("title") or s["id"], res["values"]),
                  "template": s.get("template", "")} for s in ver["body"].get("steps") or []]
        return {"runbook_key": runbook_key, "version": ver["version"], "params": res["values"], "param_sources": res["sources"],
                "missing": res["missing"], "ambiguous": res["ambiguous"], "invalid": res["invalid"], "steps": steps,
                "risk": ver.get("risk_effective") or ver["body"].get("risk")}

    def start(self, cid: int, by: str, runbook_key: Optional[str] = None, version: Optional[int] = None,
              params: Optional[Dict[str, Any]] = None, start_mode: str = "human") -> dict:
        case = self._case(cid)
        if case.get("kind") != "case" or case.get("phase") not in OPEN_PHASES:
            raise EngineError("act_case_closed", "Vaka açık değil", "The case is not open")
        if self.active_for_case(cid):
            raise EngineError("act_run_active", "Bu vakada çalışan bir koşu var", "A run is already active for this case")
        attempt = int(case.get("reopened") or 0) + 1
        dec = self.act.shadow.decision(cid, attempt)
        if not runbook_key:
            if not dec or not dec.get("runbook_key"):
                raise EngineError("act_decision_required", "Önce bir runbook seçin", "Choose a runbook first", 422)
            runbook_key, params = dec["runbook_key"], {**(dec.get("params") or {}), **(params or {})}
        try:
            rb = self.act.runbooks.get(runbook_key)
        except KeyError:
            raise EngineError("act_param_invalid", f"Bilinmeyen runbook: {runbook_key}", f"Unknown runbook: {runbook_key}", 422)
        if rb.get("status") != "active" or rb.get("killed") or not rb.get("current_version"):
            raise EngineError("act_runbook_unpublished", "Runbook yayında değil", "The runbook is not published")
        ver = self.act.runbooks.version(runbook_key, version or rb["current_version"])
        if ver.get("status") != "approved":
            raise EngineError("act_runbook_unpublished", "Bu sürüm yayında değil", "This version is not published")
        if dec is None:                                               # starting is a decision for this attempt (C10)
            try:
                self.act.shadow.decide(cid, by, runbook_key, version=ver["version"], params=params or {}, implicit=True)
            except Exception:  # noqa: BLE001 - an existing decision of another shape stays authoritative
                pass
        body = ver["body"]
        res = self.resolve_params(case, body, params)
        host = case.get("host") or ""
        inv = self._inv(host)
        ts = self.iso()
        state = {"cursor": 0, "phase": "main", "missing": res["missing"], "ambiguous": res["ambiguous"], "invalid": res["invalid"],
                 "proceed": [], "attempt": attempt}
        with self.db.tx():
            rid = self.db._insert(
                "INSERT INTO act_runs (case_id, runbook_id, version_id, team_id, host, hosts, env, env_tier, host_class, criticality, start_mode, "
                "started_by, params, param_sources, status, run_level, started_at, state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, rb["id"], ver["id"], case.get("team_id"), host, json.dumps([host] if host else []), case.get("env") or "",
                 case.get("env_tier") or env_tier(case.get("env") or ""), case.get("host_class") or inv.get("class") or "",
                 inv.get("criticality") or "normal", start_mode, self.act.audit.actor(by, "user"), json.dumps(res["values"], default=str),
                 json.dumps(res["sources"]), "running", 1, ts, json.dumps(state)))
            hashes = ver.get("step_hashes") or {}
            for i, s in enumerate(body.get("steps") or []):
                t = self.act.catalogue.get(s["template"]) if s.get("template") else None
                self.db._exec("INSERT INTO act_steps (run_id, seq, step_key, type, step_hash, template_id, template_sha256, status) VALUES (?,?,?,?,?,?,?,?)",
                              (rid, i, s["id"], s.get("type", "manual"), (hashes or {}).get(s["id"], ""), s.get("template", ""),
                               (t or {}).get("sha256", ""), "pending"))
            self.db._exec("UPDATE actions SET run_id=?, runbook_key=? WHERE id=?", (rid, runbook_key, cid))
            self.act.audit.write("run.started", object_kind="run", object_id=rid, actor=by, actor_kind="user" if "@" in by else "system",
                                 team_id=case.get("team_id"), payload={"case_id": cid, "runbook": runbook_key, "version": ver["version"],
                                                                       "params": res["values"], "sources": res["sources"]})
        self._case_phase({"case_id": cid}, "running", by)
        run = self.run(rid)
        pc = self._preconditions(run, None)
        if pc and pc[0] == "stop":
            self._block(run, pc)
            return self.get(rid)
        return self.advance(rid)

    # ---------------------------------------------------------------- preconditions (§6.5)
    def _preconditions(self, run: dict, before: Optional[str]) -> Optional[Tuple[str, str, str]]:
        """None, or the most restrictive failure (else-mode, check, reason): stop > ask > wait > guided. A `guided` failure is
        also remembered for the run (its act steps stay guided) when a stricter one is answered. Checks that reference a step
        capture run only before act steps after it."""
        fails = self._precondition_failures(run, before)
        if not fails:
            return None
        if any(f[0] == "guided" for f in fails) and not run["state"].get("guided_all"):
            st = dict(run["state"])
            st["guided_all"] = True
            self._set_run(run["id"], state=st)
            run["state"] = st
        order = {"stop": 0, "ask": 1, "wait": 2, "guided": 3}
        return sorted(fails, key=lambda f: order.get(f[0], 1))[0]

    def _precondition_failures(self, run: dict, before: Optional[str]) -> List[Tuple[str, str, str]]:
        body = self._version(run)["body"]
        fails: List[Tuple[str, str, str]] = []
        steps = self.steps(run["id"])
        done = {s["step_key"] for s in steps if s["status"] in DONE_STEP}
        cx = None
        for pc in body.get("preconditions") or []:
            chk = str(pc.get("check") or "")
            refs = set(STEP_REF.findall(str(pc.get("if") or "")))
            if refs and (before is None or not refs <= done):
                continue
            if chk in run["state"].get("proceed", []):
                continue
            ok, why = True, ""
            host = run.get("host") or ""
            if chk in ("agent_online", "actuator_ready"):
                rec = self.act.jobs.actuators.for_host(host) if host else None
                fresh = False
                if rec and rec.get("key_status") == "confirmed":
                    lh = _parse(rec.get("last_hello"))
                    mono = self.act.jobs.last_poll.get(int(rec["agent_id"]))
                    import time as _t
                    fresh = (mono is not None and _t.monotonic() - mono < int(pc.get("max_age_s") or 120)) or \
                            (lh is not None and (datetime.now(timezone.utc) - lh).total_seconds() < 360)
                ok, why = fresh, "Eylem ajanı çevrimiçi değil"
            elif chk == "no_other_run_on_host":
                other = self.db._exec(f"SELECT id FROM act_runs WHERE host=? AND id<>? AND status IN ({','.join('?' * len(ACTIVE_RUN))})",
                                      (host, run["id"]) + ACTIVE_RUN)
                lock = self.db._exec("SELECT run_id FROM act_host_locks WHERE host=?", (host,))
                holder = (other[0]["id"] if other else None) or (lock[0]["run_id"] if lock and lock[0]["run_id"] != run["id"] else None)
                ok, why = holder is None, f"{host} üzerinde koşu #{holder} çalışıyor"
            elif chk in ("metric_at_least", "metric_below"):
                v = self.sources.metric(host, str(pc.get("metric") or ""), int(pc.get("max_age_s") or 300))
                lim = float(pc.get("value") or 0)
                ok = v is None or (v >= lim if chk == "metric_at_least" else v < lim)
                why = f"Sorun düzelmiş görünüyor: {pc.get('metric')} {v}. Yine de devam?"
            elif chk in ("fact", "service_state"):
                cx = cx or self.ctx(run, steps)
                expr = pc.get("if") or pc.get("expect") or ""
                ok = bool(expr) and conditions.evaluate(expr, cx)
                why = f"Koşul sağlanmadı: {expr}"
            elif chk == "case_still_active":
                ok, why = self._case(run["case_id"]).get("phase") in OPEN_PHASES, "Vaka artık açık değil"
            elif chk == "not_frozen":
                fr = self.windows.frozen(run.get("team_id"), run.get("env") or "", run.get("host_class") or "", host, self.now())
                ok, why = fr is None, f"Dondurma: {(fr or {}).get('name', '')}"
            elif chk == "in_window":
                name = str(pc.get("window") or body.get("windows", {}).get("requires") or "")
                ok = not name or self.windows.in_window(name, run.get("team_id"), run.get("env") or "", run.get("host_class") or "", host, self.now())
                why = f"Pencere dışında: {name}"
            elif chk == "inventory_status":
                st = str(self._inv(host).get("status") or "")
                ok, why = st not in ("maintenance", "decommissioned"), f"Envanter durumu: {st}"
            elif chk == "clock_skew_below":
                rec = self.act.jobs.actuators.for_host(host) if host else None
                ok, why = not rec or abs(int(rec.get("clock_skew_s") or 0)) <= int(pc.get("max_s") or 120), "Sunucu saati kaymış"
            elif chk == "change_approved":
                pol = (body.get("itsm") or {}).get("policy", "off")
                ok, why = pol != "required" or bool(run.get("change_ref")), "Değişiklik kaydı onayı bekleniyor"
            if not ok:
                fails.append((str(pc.get("else") or PRECOND_DEFAULT_ELSE.get(chk, "ask")), chk, why))
        return fails

    def _block(self, run: dict, pc: Tuple[str, str, str]) -> None:
        st = dict(run["state"])
        st["blocked"] = {"mode": pc[0], "check": pc[1], "reason": pc[2]}
        self._set_run(run["id"], status="blocked", state=st)
        self._audit("run.blocked", run, check=pc[1], reason=pc[2], mode=pc[0])
        self._case_phase(run, "waiting_human", needs_human=1)

    # ---------------------------------------------------------------- the main loop
    def tick(self) -> int:
        """The act thread calls this every second: kills, job results, timers, watch windows of every active run."""
        n = 0
        for r in self.db._exec(f"SELECT id FROM act_runs WHERE status IN ({','.join('?' * len(ACTIVE_RUN))}) ORDER BY id", ACTIVE_RUN):
            try:
                self.advance(r["id"])
                n += 1
            except Exception:  # noqa: BLE001 - one broken run never stops the others
                pass
        return n

    def advance(self, rid: int) -> dict:
        for _ in range(200):                                          # bounded: every pass either moves or stops
            run = self.run(rid)
            if run["status"] not in ACTIVE_RUN:
                break
            self._kills(run)
            if run["status"] == "blocked":
                blk = run["state"].get("blocked") or {}
                if blk.get("mode") == "wait":
                    pc = self._preconditions(run, blk.get("before"))
                    if pc is None or pc[1] != blk.get("check"):
                        st = dict(run["state"])
                        st.pop("blocked", None)
                        self._set_run(rid, status="running", state=st)
                        continue
                break
            phase = run["state"].get("phase", "main")
            moved = {"main": self._main, "verify": self._phase_verify, "rollback": self._phase_rollback, "post_verify": self._phase_verify,
                     "watch": self._phase_watch}.get(phase, lambda r: False)(run)
            if not moved:
                break
        return self.get(rid)

    def _kills(self, run: dict) -> None:
        """I7: a hard stop cancels in-flight jobs of act / rollback steps; their steps fall to guided."""
        k = self.act.kill.killed(team_id=run.get("team_id"), host=run.get("host") or "", runbook=run.get("runbook_key") or "")
        if not k or not k.get("hard"):
            return
        for s in self.steps(run["id"]):
            if s["type"] in ("act", "rollback") and s["status"] in ("dispatched", "running") and s.get("job_id"):
                try:
                    self.act.jobs.cancel(s["job_id"], by="system")
                except Exception:  # noqa: BLE001
                    pass
                self._set_step(s["id"], status="guided", reason="kill_hard")
                self._audit("step.cancelled", run, step=s["step_key"], reason="Sert durdur")

    # ---- main phase: steps in order
    def _main(self, run: dict) -> bool:
        steps = self.steps(run["id"])
        bsteps = self._body_steps(run)
        by_key = {s["step_key"]: s for s in steps}
        for st in steps:
            b = bsteps.get(st["step_key"], {})
            if self._unprocessed(st):
                res = self.agent.poll(st["job_id"])
                if res is not None:
                    return self._job_done(run, st, b, res)
            if st["status"] in DONE_STEP:
                continue
            if st["status"] in WAIT_STEP:
                return self._waiting(run, st, b, by_key)
            # pending
            if st["type"] == "rollback":
                self._set_step(st["id"], status="skipped", reason="rollback_not_needed")
                return True
            if b.get("when") and isinstance(b["when"], str):
                if not conditions.evaluate(b["when"], self.ctx(run, steps)):
                    self._set_step(st["id"], status="skipped", reason="when_false")
                    return True
            return self._begin(run, st, b, by_key)
        return self._after_main(run, steps)

    @staticmethod
    def _unprocessed(st: dict) -> bool:
        """act/jobs.py already wrote the job's result on the step; the engine has not taken it in yet (captures, outcome)."""
        return bool(st.get("job_id")) and st.get("executor") == "agent" and not st.get("performed_by") and \
            st["status"] in ("succeeded", "failed", "rejected_by_agent", "cancelled")

    def _skip_until(self, run: dict, from_key: str, to_key: str, reason: str) -> None:
        """A jump (choose / when rule): the steps between are skipped (rollback steps stay for failure paths)."""
        steps = self.steps(run["id"])
        keys = [s["step_key"] for s in steps]
        if to_key not in keys:
            return
        i, j = keys.index(from_key), keys.index(to_key)
        for s in steps[i + 1:j] if j > i else []:
            if s["status"] == "pending" and s["type"] != "rollback":
                self._set_step(s["id"], status="skipped", reason=reason)

    def _begin(self, run: dict, st: dict, b: dict, by_key: Dict[str, dict]) -> bool:
        t = st["type"]
        if t == "choose":
            cx = self.ctx(run, self.steps(run["id"]))
            for rule in b.get("when") or []:
                if isinstance(rule, dict) and rule.get("if") and conditions.evaluate(rule["if"], cx):
                    self._choose_to(run, st, b, str(rule.get("to")), "system", rule="when")
                    return True
            self._set_step(st["id"], status="guided", reason="choose", started_at=self.iso())
            self._case_phase(run, "waiting_human", needs_human=1)
            return False
        if t == "verify":
            self._set_step(st["id"], status="running", started_at=self.iso(), verify={"attempt": 0, "results": {}, "jobs": {}})
            return True
        if t == "manual" and b.get("optional"):
            self._set_step(st["id"], status="skipped", reason="optional")   # offered on the card, never blocks the run
            return True
        if t in ("manual", "itsm"):
            self._set_step(st["id"], status="guided", reason="manual" if t == "manual" else "itsm_wp17", started_at=self.iso(), mode=1)
            self._case_phase(run, "waiting_human", needs_human=1)
            return False
        if t == "wait":
            secs = int(b.get("seconds") or 60)
            self._set_step(st["id"], status="waiting", wait_until=self.later(secs), started_at=self.iso())
            return True
        if t == "notify":
            self._notify(run, b)
            self._set_step(st["id"], status="succeeded", started_at=self.iso(), ended_at=self.iso())
            return True
        if t == "diagnose":
            return self._begin_diag(run, st, b, by_key)
        if t == "act":
            return self._begin_act(run, st, b, by_key, mode="execute")
        self._set_step(st["id"], status="skipped", reason=f"unknown_type:{t}")
        return True

    def _notify(self, run: dict, b: dict) -> None:
        try:
            svc = getattr(self.act, "svc", None)
            text = privacy.scrub_strict(V.render(str(b.get("text") or b.get("title") or ""), run["params"]))[:500]
            if svc is not None and getattr(svc, "notifier", None) is not None and text:
                svc.notifier.send(f"[Watchover] {text}", text)
        except Exception:  # noqa: BLE001 - a notification never stops a run
            pass

    def _template(self, st: dict) -> Optional[dict]:
        if not st.get("template_id"):
            return None
        t = self.act.catalogue.get(st["template_id"], st.get("template_sha256") or None) or self.act.catalogue.get(st["template_id"])
        if t is None:
            return None
        return {"id": t["id"], "sha256": t["sha256"], "body": t["body"], "kind": t.get("kind"), "risk": t.get("risk"),
                "reversibility": t.get("reversibility"), "cost": t.get("cost") or "light"}

    def _validated(self, run: dict, st: dict, b: dict, by_key: Dict[str, dict], tmpl: Optional[dict]) -> Tuple[Optional[dict], str]:
        """(validated arguments, '') or (None, reason) — a missing or invalid value makes the step ask a person."""
        vals, missing = self.args(run, b, by_key)
        if missing:
            return None, "ask:" + ",".join(missing)
        if not tmpl:
            return vals, ""
        from .params import ParamError
        from .templates import validate_params
        inv = self._inv(run.get("host") or "")
        try:
            return validate_params(tmpl["body"], vals, {"case_host": run.get("host") or "", "host": run.get("host") or "",
                                                        "health_url": inv.get("health_url", "")}), ""
        except ParamError as e:
            return None, f"param_invalid:{e.code}"

    def _guided(self, run: dict, st: dict, tmpl: Optional[dict], params: Optional[dict], reason: str, level: int = 1) -> bool:
        cmd = ""
        if tmpl and params is not None:
            plan = self.manual.plan({"template_id": tmpl["id"], "template_sha256": tmpl["sha256"], "key": st["step_key"]}, params,
                                    run.get("host") or "", "execute")
            cmd = plan.human_cmd
        self._set_step(st["id"], status="guided", executor="manual", mode=level, reason=reason, human_cmd=cmd,
                       params=params or {}, started_at=st.get("started_at") or self.iso())
        self._case_phase(run, "waiting_human", needs_human=1)
        return False

    def _dispatch(self, run: dict, st: Optional[dict], tmpl: dict, params: dict, mode: str, step_key: str, by: str = "system",
                  approvals: Optional[list] = None) -> Optional[str]:
        """A job through the pull channel; act modes take the host lock in the dispatch transaction (R9). None when refused."""
        from .jobs import ChannelError, HostLocked
        version_sha = self._version(run).get("body_sha256", "")
        plan = self.agent.plan({"template_id": tmpl["id"], "template_sha256": tmpl["sha256"], "key": step_key}, params, run.get("host") or "",
                               mode, version_sha)
        try:
            jid = self.agent.dispatch(plan, step_id=st["id"] if st is not None else None, run_id=run["id"], case_id=run["case_id"],
                                      step_key=step_key, timeout_s=int(tmpl["body"].get("timeout_s") or 120),
                                      max_output_kb=int(tmpl["body"].get("max_output_kb") or 64), team_id=run.get("team_id"), by=by,
                                      approvals=approvals or [])
        except HostLocked as e:
            if st is not None:
                self._set_step(st["id"], reason=f"host_locked:{e.run_id}")
            return None
        except (ChannelError, Exception) as e:  # noqa: BLE001 - refused or keyd down: the caller falls back to guided
            if st is not None:
                self._set_step(st["id"], reason=f"dispatch_refused:{str(getattr(e, 'error', e))[:80]}")
            return None
        if st is not None:
            self._set_step(st["id"], executor="agent", job_hash=plan.job_hash, preview=json.dumps(list(plan.preview or [])),
                           params=params, started_at=st.get("started_at") or self.iso())
        return jid

    # ---- diagnose
    def _begin_diag(self, run: dict, st: dict, b: dict, by_key: Dict[str, dict]) -> bool:
        tmpl = self._template(st)
        params, why = self._validated(run, st, b, by_key, tmpl)
        if params is None:
            return self._guided(run, st, tmpl, None, why)
        system = bool(tmpl) and MODE_CAP.get(self.act.mode, 0) >= 1 and tmpl.get("cost") != "heavy" and not run["state"].get("guided_all")
        reason = ""
        if system and self.act.kill.killed(team_id=run.get("team_id"), host=run.get("host") or "", runbook=run.get("runbook_key") or "",
                                           template=tmpl["id"]):
            system, reason = False, "kill"
        if system:
            ok, reason = self.agent.can_run({"template_id": tmpl["id"], "template_sha256": tmpl["sha256"]}, run.get("host") or "", "diagnose")
            system = ok
        if system:
            b_ = self.budgets.diag(run.get("team_id"), run["case_id"], self.now())
            if b_:
                system, reason = False, b_
        if system and self._dispatch(run, st, tmpl, params, "diagnose", st["step_key"]):
            self._set_step(st["id"], mode=4)
            return True
        if tmpl and tmpl.get("cost") == "heavy" and MODE_CAP.get(self.act.mode, 0) >= 1 and \
                self.agent.can_run({"template_id": tmpl["id"], "template_sha256": tmpl["sha256"]}, run.get("host") or "", "diagnose")[0]:
            # heavy R0 diagnose starts at L2: one click "Teşhisi çalıştır" (dispatch_approved), or a person does it
            self._set_step(st["id"], status="awaiting_approval", executor="agent", params=params, mode=2, reason="heavy",
                           started_at=self.iso())
            self._case_phase(run, "waiting_human", needs_human=1)
            return False
        return self._guided(run, st, tmpl, params, reason or "no_actuator")

    # ---- act / rollback
    def _begin_act(self, run: dict, st: dict, b: dict, by_key: Dict[str, dict], mode: str) -> bool:
        tmpl = self._template(st)
        params, why = self._validated(run, st, b, by_key, tmpl)
        if params is None:
            self._set_step(st["id"], reason=why)
            return self._guided(run, st, tmpl, None, why)
        pc = self._preconditions(run, st["step_key"])
        if pc:
            if pc[0] == "stop":
                self._block(run, pc)
                return False
            if pc[0] in ("ask", "wait"):
                stt = dict(run["state"])
                stt["blocked"] = {"mode": pc[0], "check": pc[1], "reason": pc[2], "before": st["step_key"]}
                self._set_run(run["id"], status="blocked", state=stt)
                self._audit("run.blocked", run, check=pc[1], reason=pc[2], mode=pc[0])
                self._case_phase(run, "waiting_human", needs_human=1)
                return False
            if pc[0] == "guided":
                stt = dict(run["state"])
                stt["guided_all"] = True
                self._set_run(run["id"], state=stt)
                run["state"] = stt
        if tmpl is None:
            return self._guided(run, st, None, params, "no_template")
        level, reasons = self.level(run, st, b, tmpl, mode)
        # dry run first when the step asks for it and an actuator can do it (read-only)
        if b.get("dry_run") in ("required", "optional") and tmpl["body"].get("dry_run") and not st.get("dry_output"):
            ok, _ = self.agent.can_run({"template_id": tmpl["id"], "template_sha256": tmpl["sha256"]}, run.get("host") or "", "dry_run")
            if ok and MODE_CAP.get(self.act.mode, 0) >= 1 and not self.act.kill.killed(team_id=run.get("team_id"), host=run.get("host") or "",
                                                                                      template=tmpl["id"]):
                jid = self._dispatch(run, None, tmpl, params, "dry_run", st["step_key"])
                if jid:
                    self._set_step(st["id"], status="ready", dry_output={"job_id": jid}, params=params, mode=level,
                                   reason="dry_run", started_at=self.iso())
                    return True
        self._set_step(st["id"], mode=level)
        if level <= 1:
            return self._guided(run, st, tmpl, params, ",".join(reasons) or "level", level)
        plan = self.agent.plan({"template_id": tmpl["id"], "template_sha256": tmpl["sha256"], "key": st["step_key"]}, params,
                               run.get("host") or "", mode, self._version(run).get("body_sha256", ""))
        human = self.manual.plan({"template_id": tmpl["id"], "template_sha256": tmpl["sha256"], "key": st["step_key"]}, params,
                                 run.get("host") or "", mode).human_cmd
        self._set_step(st["id"], status="awaiting_approval", executor="agent", params=params, job_hash=plan.job_hash, human_cmd=human,
                       preview=json.dumps(list(plan.preview or [])), reason="approval", started_at=self.iso())
        self._audit("step.awaiting_approval", run, step=st["step_key"], level=level, job_hash=plan.job_hash)
        self._case_phase(run, "waiting_human", needs_human=1)
        return False

    # ---- a step that waits for something
    def _waiting(self, run: dict, st: dict, b: dict, by_key: Dict[str, dict]) -> bool:
        s = st["status"]
        if s == "waiting":                                              # wait step / timers
            if _parse(st.get("wait_until")) and self.now() >= _parse(st["wait_until"]):
                self._set_step(st["id"], status="succeeded", ended_at=self.iso())
                return True
            return False
        if s == "running" and st["type"] == "verify":
            return self._phase_verify(run, st)
        if s == "ready" and st.get("reason") == "dry_run":            # dry run of an act step in flight
            dj = (st.get("dry_output") or {}).get("job_id")
            res = self.agent.poll(dj) if dj else None
            if dj and res is None:
                return False
            dry = {"job_id": dj, "state": getattr(res, "state", ""), "fields": getattr(res, "facts", {}) or {}}
            exp = b.get("dry_expect")
            if exp and dry["fields"]:
                dry["expect_ok"] = bool(conditions.evaluate(exp, {"captures": {**dry["fields"], "bytes": dry["fields"].get("bytes",
                                                                                                                           dry["fields"].get("bytes_before"))}}))
            self._set_step(st["id"], status="pending", dry_output=dry, reason="")
            st = {**st, "status": "pending", "dry_output": dry}
            return self._begin_act(run, st, b, by_key, mode="rollback" if st["type"] == "rollback" else "execute")
        if s in ("dispatched", "running"):
            if not st.get("job_id"):
                return False
            res = self.agent.poll(st["job_id"])
            if res is None:
                return False
            return self._job_done(run, st, b, res)
        if s == "unknown":
            return self._unknown(run, st, b)
        return False                                                    # guided / awaiting_approval / ready(ask): a person acts

    def _job_done(self, run: dict, st: dict, b: dict, res) -> bool:
        """The job's result is in (act/jobs.py already wrote status, fields, signed, display tail on the step)."""
        fresh = next(s for s in self.steps(run["id"]) if s["id"] == st["id"])
        fields = fresh.get("fields") or {}
        caps = {k: fields.get(k) for k in (b.get("capture") or {}) if k in fields}
        upd: Dict[str, Any] = {"captures": caps, "ended_at": fresh.get("ended_at") or self.iso(), "performed_by": "system"}
        if res.state == "ok":
            upd["status"] = "succeeded"
        elif res.state == "rejected":
            upd["status"], upd["reason"] = "rejected_by_agent", res.reject_code
        elif res.state == "cancelled":
            upd["status"] = "cancelled"
        elif res.state == "expired":
            upd["status"], upd["reason"] = "cancelled", "expired"
        else:
            upd["status"] = "failed"
        self._set_step(st["id"], **upd)
        self._audit("step.result", run, step=st["step_key"], state=res.state, reject=res.reject_code)
        if upd["status"] in ("rejected_by_agent", "cancelled") and st["type"] in ("diagnose", "act", "rollback"):
            # refused / cancelled: the person can do it by hand (guided) — trust treats configuration refusals as neutral
            if st["type"] == "diagnose" or labels.classify_reject(res.reject_code) != "unsafe_blocked":
                return self._guided(run, st, self._template(st), fresh.get("params") or None, f"agent:{res.reject_code or upd['status']}")
            self._set_step(st["id"], outcome="unsafe_blocked")
        if st["type"] == "rollback":
            return self._after_rollback(run, st, upd["status"])
        return True

    # ---- unknown (§9.7): result lost; read-only verify decides with a person
    def _unknown(self, run: dict, st: dict, b: dict) -> bool:
        probe = (st.get("verify") or {}).get("probe")
        if probe is None:
            vkey = b.get("verify")
            bsteps = self._body_steps(run)
            vb = bsteps.get(vkey) or {}
            results = []
            for chk in vb.get("checks") or []:
                kind = V.check_kind(chk)
                if kind and kind != "template":
                    results.append(V.server_check(kind, V.render(chk[kind], run["params"]), run.get("host") or "", self.sources, self.now()))
            probe = V.combine(results, vb.get("require", "all"))
            self._set_step(st["id"], verify={**(st.get("verify") or {}), "probe": probe})
            self._audit("step.unknown", run, step=st["step_key"], probe_ok=probe.get("ok"))
            self._case_phase(run, "waiting_human", needs_human=1)
        return False

    # ---- verify (main and post-rollback)
    def _verify_step(self, run: dict) -> Optional[dict]:
        key = run["state"].get("verify_step")
        if not key:
            return None
        return next((s for s in self.steps(run["id"]) if s["step_key"] == key), None)

    def _phase_verify(self, run: dict, vst: Optional[dict] = None) -> bool:
        post = run["state"].get("phase") == "post_verify"
        if vst is None:
            vst = self._verify_step(run)
            if vst is None:
                return False
        b = self._body_steps(run).get(vst["step_key"], {})
        vs = dict(vst.get("post_rollback_verify") if post else vst.get("verify") or {})
        if vs.get("next_at") and self.now() < _parse(vs["next_at"]):
            return False
        steps = self.steps(run["id"])
        by_key = {s["step_key"]: s for s in steps}
        cx = self.ctx(run, steps)
        results = dict(vs.get("results") or {})
        jobs = dict(vs.get("jobs") or {})
        pasted = dict(vs.get("pasted") or {})
        pending, need_paste = False, False
        host = run.get("host") or ""
        values = {**run["params"], **{f"inventory.{k}": v for k, v in self._inv(host).items()}, "host": host}
        for i, chk in enumerate(b.get("checks") or []):
            k = str(i)
            if k in results:
                continue
            if chk.get("when") and not conditions.evaluate(chk["when"], cx):
                results[k] = {"kind": V.check_kind(chk), "ok": None, "skipped": True}
                continue
            kind = V.check_kind(chk)
            if kind == "template":
                if k in jobs:
                    res = self.agent.poll(jobs[k])
                    if res is None:
                        pending = True
                        continue
                    results[k] = V.template_check(chk, res.facts if res.state == "ok" else None)
                    results[k]["job_id"] = jobs[k]
                    continue
                if k in pasted:
                    results[k] = V.template_check(chk, pasted[k])
                    results[k]["pasted"] = True
                    continue
                t = self.act.catalogue.get(chk["template"])
                tmpl = {"id": t["id"], "sha256": t["sha256"], "body": t["body"]} if t else None
                args, _why = self._validated(run, vst, {"args": chk.get("args") or {}}, by_key, tmpl)
                ok = False
                if tmpl and args is not None and MODE_CAP.get(self.act.mode, 0) >= 1:
                    ok, _ = self.agent.can_run({"template_id": tmpl["id"], "template_sha256": tmpl["sha256"]}, host, "verify")
                jid = self._dispatch(run, None, tmpl, args, "verify", vst["step_key"]) if ok else None
                if jid:
                    jobs[k] = jid
                    pending = True
                else:
                    need_paste = True                                   # guided paste box (§7.1: no confirmed actuator)
            elif kind:
                results[k] = V.server_check(kind, V.render(chk[kind], values), host, self.sources, self.now())
            else:
                results[k] = {"kind": "unknown", "ok": None}
        vs.update({"results": results, "jobs": jobs})
        if pending or need_paste:
            self._set_step(vst["id"], **({"post_rollback_verify": vs} if post else {"verify": vs}),
                           status="guided" if need_paste and not pending else "running", reason="paste" if need_paste else "")
            if need_paste and not pending:
                self._case_phase(run, "waiting_human", needs_human=1)
            return False
        verdict = V.combine([results[k] for k in sorted(results, key=int)], b.get("require", "all"))
        attempt = int(vs.get("attempt") or 0) + 1
        hist = list(vs.get("history") or []) + [{"attempt": attempt, "ok": verdict["ok"], "at": self.iso()}]
        tries = max(1, int(b.get("retries") or 1))
        if verdict["ok"] is not True and attempt < tries:
            nxt = {"attempt": attempt, "next_at": self.later(int(b.get("interval_s") or 20)), "results": {}, "jobs": {}, "history": hist}
            self._set_step(vst["id"], **({"post_rollback_verify": nxt} if post else {"verify": nxt}), status="running")
            return False
        final = {**verdict, "attempt": attempt, "history": hist, "pasted": pasted}
        self._set_step(vst["id"], **({"post_rollback_verify": final} if post else {"verify": final}),
                       status="succeeded" if verdict["ok"] is not False else "failed", ended_at=self.iso())
        self._audit("verify.done", run, step=vst["step_key"], ok=verdict["ok"], post=post, independent=verdict["independent_ok"])
        if post:
            return self._after_post_verify(run, verdict)
        return self._after_verify(run, vst, verdict)

    def _acts_of(self, run: dict, vkey: str) -> List[dict]:
        bs = self._body_steps(run)
        return [s for s in self.steps(run["id"]) if s["type"] == "act" and bs.get(s["step_key"], {}).get("verify") == vkey
                and s["status"] in OK_STEP + ("failed", "timed_out")]

    def _after_verify(self, run: dict, vst: dict, verdict: dict) -> bool:
        acts = self._acts_of(run, vst["step_key"])
        for a in acts:
            self._set_step(a["id"], verify={k: verdict[k] for k in ("ok", "no_data", "independent_ok", "checks")})
        st = dict(run["state"])
        st["phase"] = "main"
        st.pop("verify_step", None)
        if verdict["ok"] is False and acts:
            failing = acts[-1]
            b = self._body_steps(run).get(failing["step_key"], {})
            on_fail = self._body_steps(run).get(vst["step_key"], {}).get("on_fail") or ("rollback" if b.get("rollback") else "escalate")
            if on_fail == "rollback" and b.get("rollback"):
                st.update({"phase": "rollback", "failing": failing["step_key"], "rollback": b["rollback"]})
                self._set_run(run["id"], status="rolling_back", state=st)
                self._audit("run.rolling_back", run, step=failing["step_key"], rollback=b["rollback"])
                return True
            self._set_run(run["id"], state=st)
            return self._finish(run, "failed", case_phase="escalated" if on_fail == "escalate" else "failed")
        self._set_run(run["id"], state=st)
        return True

    def _phase_rollback(self, run: dict) -> bool:
        key = run["state"].get("rollback")
        st = next((s for s in self.steps(run["id"]) if s["step_key"] == key), None)
        if st is None:
            return self._finish(run, "rollback_failed", case_phase="escalated")
        b = self._body_steps(run).get(key, {})
        by_key = {s["step_key"]: s for s in self.steps(run["id"])}
        if self._unprocessed(st):
            res = self.agent.poll(st["job_id"])
            if res is not None:
                return self._job_done(run, st, b, res)
        if st["status"] in ("pending", "skipped"):
            self._set_step(st["id"], status="pending", reason="")
            st = {**st, "status": "pending"}
            return self._begin_act(run, st, b, by_key, mode="rollback")
        if st["status"] in OK_STEP:
            return self._after_rollback(run, st, st["status"])
        if st["status"] in ("failed", "timed_out", "rejected_by_agent", "cancelled", "deviated"):
            return self._after_rollback(run, st, st["status"])
        return self._waiting(run, st, b, by_key)

    def _after_rollback(self, run: dict, st: dict, status: str) -> bool:
        run = self.run(run["id"])
        if run["state"].get("phase") != "rollback":
            return True
        stt = dict(run["state"])
        if status not in OK_STEP:
            self._set_run(run["id"], state=stt)
            return self._finish(run, "rollback_failed", case_phase="escalated")
        # post-rollback verify (R8): the failing act step's verify, again, with its retries
        bs = self._body_steps(run)
        vkey = bs.get(stt.get("failing"), {}).get("verify")
        stt.update({"phase": "post_verify", "verify_step": vkey})
        self._set_run(run["id"], state=stt)
        vst = next((s for s in self.steps(run["id"]) if s["step_key"] == vkey), None)
        if vst is None:
            return self._finish(run, "rollback_failed", case_phase="escalated")
        self._set_step(vst["id"], post_rollback_verify={"attempt": 0, "results": {}, "jobs": {}}, status="running")
        return True

    def _after_post_verify(self, run: dict, verdict: dict) -> bool:
        run = self.run(run["id"])
        failing = next((s for s in self.steps(run["id"]) if s["step_key"] == run["state"].get("failing")), None)
        if failing:
            self._set_step(failing["id"], post_rollback_verify={k: verdict[k] for k in ("ok", "no_data", "independent_ok", "checks")})
        if verdict["ok"] is True:
            return self._finish(run, "rolled_back", case_phase="rolled_back")
        return self._finish(run, "rollback_failed", case_phase="escalated")

    def _after_main(self, run: dict, steps: List[dict]) -> bool:
        acted = [s for s in steps if s["type"] == "act" and s["status"] in OK_STEP]
        if not acted:
            bad = [s for s in steps if s["type"] == "act" and s["status"] in ("failed", "timed_out", "rejected_by_agent")]
            return self._finish(run, "failed" if bad else "completed_ok", case_phase="failed" if bad else "ready")
        b = self._version(run)["body"]
        minutes = int((b.get("watch") or {}).get("minutes") or self.act.policy.get("act_watch_min", 15) or 15)
        minutes = max(5, min(240, minutes))
        st = dict(run["state"])
        last = max((_parse(s.get("ended_at")) or self.now()) for s in acted)
        st.update({"phase": "watch", "watch_from": self.iso(last)})
        self._set_run(run["id"], status="watching", state=st, watch_until=self.later(minutes * 60))
        self._audit("run.watching", run, minutes=minutes, time_scale=time_scale())
        self._case_phase(run, "watching", needs_human=0)
        return True

    def _phase_watch(self, run: dict) -> bool:
        until = _parse(run.get("watch_until"))
        if until and self.now() < until:
            return False
        b = self._version(run)["body"]
        w = b.get("watch") or {}
        case = self._case(run["case_id"])
        known = []
        try:
            known = [e.get("text", "")[:80] for e in self.act.evidence.items(run["case_id"])]
        except Exception:  # noqa: BLE001
            pass
        res = W.collect(self.sources, run.get("host") or "", case.get("case_key") or "", _parse(run["state"].get("watch_from")) or self.now(),
                        self.now(), expected_noise_s=min(int(w.get("expected_noise_s") or 0), 300),
                        expected_noise=[str(x.get("template") if isinstance(x, dict) else x) for x in (w.get("expected_noise") or [])][:5],
                        known_templates=known, expect_silence_s=min(int(w.get("expect_silence_s") or 0), 120))
        for s in self.steps(run["id"]):
            if s["type"] == "act" and s["status"] in OK_STEP:
                self._set_step(s["id"], side_effect=res)
        self._audit("run.watched", run, side_effect=res["side_effect"], hits=[h["rule"] for h in res["hits"]],
                    suppressed=len(res["suppressed"]))
        if res["side_effect"]:
            return self._finish(run, "completed_side_effect", case_phase="failed")
        return self._finish(run, "completed_ok", case_phase="resolved")

    # ---------------------------------------------------------------- the end: one outcome per step, labels, case
    def _step_outcomes(self, run: dict, status: str) -> None:
        steps = self.steps(run["id"])
        for s in steps:
            ev: List[str] = []
            if s.get("outcome"):
                ev.append(s["outcome"])
            ver, post = s.get("verify") or {}, s.get("post_rollback_verify") or {}
            claim = s.get("human_claim") or {}
            if s["type"] in ("act", "diagnose"):
                if s["status"] in ("failed", "timed_out"):
                    ev.append("exec_error")
                elif s["status"] == "rejected_by_agent":
                    ev.append(labels.classify_reject(s.get("reason") or ""))
                elif s["status"] == "deviated":
                    ev.append("deviated")
                elif s["status"] in ("skipped", "pending", "guided", "awaiting_approval", "cancelled"):
                    ev.append("skipped" if s["status"] != "cancelled" else "aborted")
                elif s["status"] in OK_STEP:
                    if s["type"] == "diagnose":
                        ev.append("ok")
                    elif claim.get("result") == "failed":
                        ev.append("exec_error")
                    if s["type"] == "act" and s["status"] in OK_STEP:
                        if ver.get("ok") is False:
                            ev.append("verify_failed")
                        elif ver.get("no_data") or not ver:
                            ev.append("unverified")
                        else:
                            ev.append("ok_corrected" if claim.get("corrected") else "ok")
                if s["type"] == "act" and post:
                    ev.append("rolled_back" if post.get("ok") is True else "rollback_failed")
                if s["type"] == "act" and status == "rollback_failed" and run["state"].get("failing") == s["step_key"]:
                    ev.append("rollback_failed")
                if (s.get("side_effect") or {}).get("side_effect"):
                    ev.append("side_effect")
            elif s["type"] == "rollback" and s["status"] not in ("skipped", "pending"):
                ev.append("ok" if s["status"] in OK_STEP and status == "rolled_back" else
                          ("rollback_failed" if status == "rollback_failed" else ("exec_error" if s["status"] in ("failed", "timed_out") else "skipped")))
            out = labels.step_outcome(ev)
            if out:
                self._set_step(s["id"], outcome=out)

    def _finish(self, run: dict, status: str, case_phase: str = "") -> bool:
        run = self.run(run["id"])
        if run["status"] not in ACTIVE_RUN:
            return False
        self._step_outcomes(run, status)
        steps = self.steps(run["id"])
        lab = labels.run_label([s.get("outcome") for s in steps if s["type"] in ("act", "rollback")], status)
        if status == "rollback_failed":
            lab = "rollback_failed"
        st = dict(run["state"])
        st["phase"] = "done"
        with self.db.tx():
            self._set_run(run["id"], status=status, label=lab, outcome=lab, ended_at=self.iso(), state=st)
            self.db._exec("DELETE FROM act_host_locks WHERE run_id=?", (run["id"],))
            self._audit("run.finished", run, status=status, label=lab)
        run = self.run(run["id"])
        labels.write_events(self.db, self.act.audit, run, steps, self._case(run["case_id"]))
        if status == "rollback_failed" and run.get("host"):
            # the host's state is unknown: act steps stop there until a later post-rollback verify passes or a person clears it
            try:
                if not self.act.kill.killed(host=run["host"]):
                    self.act.kill.stop("host", run["host"], by="system", reason="Sunucunun durumu bilinmiyor (geri alma başarısız)")
            except Exception:  # noqa: BLE001
                pass
        if case_phase:
            fields = {"outcome": lab, "label": lab}
            if case_phase in ("ready", "failed", "rolled_back", "escalated"):
                fields["needs_human"] = 1
            self._case_phase(run, case_phase, **fields)
        return False

    # ---------------------------------------------------------------- people
    def _step(self, rid: int, sid: int) -> Tuple[dict, dict]:
        run = self.run(rid)
        s = next((x for x in self.steps(rid) if x["id"] == int(sid)), None)
        if s is None:
            raise KeyError(sid)
        return run, s

    def done(self, rid: int, sid: int, by: str, result: str = "ok", output: str = "", note: str = "", params_changed: bool = False) -> dict:
        """"Yaptım" (with optional pasted output) on a guided step; on a verify paste box the output is the check's data."""
        run, s = self._step(rid, sid)
        if s["status"] not in ("guided", "unknown") and not (s["type"] == "verify" and s["status"] == "guided"):
            raise EngineError("act_step_state", "Bu adım şu an bir kişiden sonuç beklemiyor", "This step is not waiting for a person")
        tmpl = self._template(s)
        claim = self.manual.claim({"body": tmpl["body"]} if tmpl else None, result, output, note)
        if params_changed:
            claim["corrected"] = True
        if s["type"] == "verify":
            post = run["state"].get("phase") == "post_verify"
            vs = dict((s.get("post_rollback_verify") if post else s.get("verify")) or {})
            b = self._body_steps(run).get(s["step_key"], {})
            pasted = dict(vs.get("pasted") or {})
            for i, chk in enumerate(b.get("checks") or []):
                if V.check_kind(chk) == "template" and str(i) not in (vs.get("results") or {}):
                    ct = self.act.catalogue.get(chk["template"])     # parsed with the check's own template parser
                    pasted[str(i)] = self.manual.claim({"body": ct["body"]} if ct else None, result, output)["fields"]
            vs["pasted"] = pasted
            self._set_step(s["id"], **({"post_rollback_verify": vs} if post else {"verify": vs}), status="running", reason="")
            self._audit("verify.pasted", run, by, step=s["step_key"])
            return self.advance(rid)
        b = self._body_steps(run).get(s["step_key"], {})
        caps = {k: claim["fields"].get(k) for k in (b.get("capture") or {}) if k in claim["fields"]}
        status = "done_by_human" if result in ("ok", "partial") else "failed"
        self._set_step(s["id"], status=status, performed_by=self.act.audit.actor(by, "user"), human_claim=claim, fields=claim["fields"],
                       captures=caps, display_tail=claim["text"][:64 * 1024], ended_at=self.iso(), reason="", executor="manual")
        if s["status"] == "unknown":
            self.db._exec("DELETE FROM act_host_locks WHERE run_id=? AND step_id=?", (run["id"], s["id"]))
        self._audit("step.done_by_human", run, by, step=s["step_key"], result=result)
        self._case_phase(run, "running", by, needs_human=0)
        if s["type"] == "rollback":
            self._after_rollback(run, s, status)
        return self.advance(rid)

    def deviate(self, rid: int, sid: int, by: str, text: str) -> dict:
        run, s = self._step(rid, sid)
        if s["status"] in DONE_STEP:
            raise EngineError("act_step_state", "Adım tamamlanmış", "The step is finished")
        self._set_step(s["id"], status="deviated", deviation=privacy.scrub_strict(text or "")[:2000], performed_by=self.act.audit.actor(by, "user"),
                       ended_at=self.iso())
        self._audit("step.deviated", run, by, step=s["step_key"])
        return self.advance(rid)

    def skip(self, rid: int, sid: int, by: str, reason: str) -> dict:
        run, s = self._step(rid, sid)
        if not (reason or "").strip():
            raise EngineError("act_reason_required", "Atlamak için bir neden yazın", "A reason is required to skip", 422)
        if s["status"] in DONE_STEP:
            raise EngineError("act_step_state", "Adım tamamlanmış", "The step is finished")
        if s["type"] == "verify":
            raise EngineError("act_step_state", "Doğrulama atlanamaz", "A verify step is never skipped")
        self._set_step(s["id"], status="skipped", reason="person", note=privacy.scrub_strict(reason)[:500], ended_at=self.iso())
        self._audit("step.skipped", run, by, step=s["step_key"], reason=reason[:300])
        return self.advance(rid)

    def _choose_to(self, run: dict, st: dict, b: dict, to: str, by: str, rule: str = "person") -> None:
        options = [str(o.get("to")) for o in b.get("options") or []] + [str(r.get("to")) for r in b.get("when") or [] if isinstance(r, dict)]
        if to not in options:
            raise EngineError("act_param_invalid", "Geçersiz seçenek", "Invalid option", 422)
        self._set_step(st["id"], status="succeeded", fields={"to": to, "by": rule}, ended_at=self.iso(), performed_by=self.act.audit.actor(by, "user"))
        for o in options:                                              # the options not taken are skipped
            if o != to:
                for s in self.steps(run["id"]):
                    if s["step_key"] == o and s["status"] == "pending":
                        self._set_step(s["id"], status="skipped", reason=f"choose:{to}")
        self._skip_until(run, st["step_key"], to, f"choose:{to}")
        self._audit("step.chosen", run, by, step=st["step_key"], to=to, rule=rule)

    def choose(self, rid: int, sid: int, by: str, to: str) -> dict:
        run, s = self._step(rid, sid)
        if s["type"] != "choose" or s["status"] != "guided":
            raise EngineError("act_step_state", "Bu adım bir seçim beklemiyor", "This step is not waiting for a choice")
        self._choose_to(run, s, self._body_steps(run).get(s["step_key"], {}), to, by)
        self._case_phase(run, "running", by, needs_human=0)
        return self.advance(rid)

    def answer(self, rid: int, by: str, params: Dict[str, Any]) -> dict:
        """Answers for `ask` parameters (validated with the parameter's type); waiting steps are tried again."""
        from .params import ParamError, validate
        run = self.run(rid)
        body = self._version(run)["body"]
        vals, srcs = dict(run["params"]), dict(run["param_sources"])
        for k, v in (params or {}).items():
            spec = (body.get("params") or {}).get(k)
            if not spec:
                raise EngineError("act_param_invalid", f"Bilinmeyen parametre: {k}", f"Unknown parameter: {k}", 422)
            try:
                vals[k] = validate(spec, v, {"case_host": run.get("host") or "", "host": run.get("host") or ""})
            except ParamError as e:
                raise EngineError("act_param_invalid", f"{k}: geçersiz değer ({e.code})", f"{k}: invalid value ({e.code})", 422)
            srcs[k] = "ask"
        st = dict(run["state"])
        st["missing"] = [m for m in st.get("missing", []) if m not in vals]
        self._set_run(rid, params=vals, param_sources=srcs, state=st)
        self._audit("run.params", run, by, params=sorted(params or {}))
        for s in self.steps(rid):                                      # steps that waited for these values start over
            if s["status"] == "guided" and str(s.get("reason") or "").startswith(("ask:", "param_invalid")):
                self._set_step(s["id"], status="pending", reason="")
        return self.advance(rid)

    def proceed(self, rid: int, by: str) -> dict:
        """"Yine de devam": a precondition that asked is accepted for this run (audited); `stop` needs a fix, not a click."""
        run = self.run(rid)
        blk = run["state"].get("blocked") or {}
        if run["status"] != "blocked" or blk.get("mode") not in ("ask", "stop"):
            raise EngineError("act_step_state", "Koşu bir onay beklemiyor", "The run is not waiting for a confirmation")
        st = dict(run["state"])
        if blk.get("mode") == "ask":
            st["proceed"] = sorted(set(st.get("proceed", [])) | {blk.get("check")})
        st.pop("blocked", None)
        self._set_run(rid, status="running", state=st)
        self._audit("run.proceed", run, by, check=blk.get("check"), mode=blk.get("mode"))
        return self.advance(rid)

    def takeover(self, rid: int, sid: int, by: str) -> dict:
        """"Bana bırak": any waiting step becomes guided (a queued job is cancelled; a running one finishes)."""
        run, s = self._step(rid, sid)
        if s["status"] in DONE_STEP:
            raise EngineError("act_step_state", "Adım tamamlanmış", "The step is finished")
        if s.get("job_id") and s["status"] in ("dispatched", "awaiting_approval"):
            try:
                self.act.jobs.cancel(s["job_id"], by=by)
            except Exception:  # noqa: BLE001
                pass
        if s["status"] == "unknown":
            self.db._exec("DELETE FROM act_host_locks WHERE run_id=? AND step_id=?", (run["id"], s["id"]))
        tmpl = self._template(s)
        self._set_step(s["id"], status="guided", reason="takeover", executor="manual", performed_by=self.act.audit.actor(by, "user"))
        if tmpl and s.get("params"):
            plan = self.manual.plan({"template_id": tmpl["id"], "template_sha256": tmpl["sha256"], "key": s["step_key"]}, s["params"],
                                    run.get("host") or "", "execute")
            self._set_step(s["id"], human_cmd=plan.human_cmd)
        self._audit("step.takeover", run, by, step=s["step_key"])
        return self.get(rid)

    def resolve_unknown(self, rid: int, sid: int, by: str, choice: str) -> dict:
        """"Başarılı say" (count_ok) · "Tekrar dene" (retry) · "Bana bırak" (takeover). The host lock goes with the decision."""
        run, s = self._step(rid, sid)
        if s["status"] != "unknown":
            raise EngineError("act_step_state", "Adımın sonucu bilinmiyor değil", "The step's result is not unknown")
        if choice == "takeover":
            return self.takeover(rid, sid, by)
        self.db._exec("DELETE FROM act_host_locks WHERE run_id=? AND step_id=?", (run["id"], s["id"]))
        if choice == "count_ok":
            self._set_step(s["id"], status="succeeded", performed_by=self.act.audit.actor(by, "user"), reason="counted_ok", ended_at=self.iso())
        elif choice == "retry":
            self._set_step(s["id"], status="pending", job_id="", reason="retry")
        else:
            raise EngineError("act_param_invalid", "Geçersiz seçim", "Invalid choice", 422)
        self._audit("step.unknown_resolved", run, by, step=s["step_key"], choice=choice)
        return self.advance(rid)

    def abort(self, rid: int, by: str, reason: str = "") -> dict:
        run = self.run(rid)
        if run["status"] not in ACTIVE_RUN:
            raise EngineError("act_step_state", "Koşu bitmiş", "The run is finished")
        for s in self.steps(rid):
            if s.get("job_id") and s["status"] in ("dispatched",):
                try:
                    self.act.jobs.cancel(s["job_id"], by=by)
                except Exception:  # noqa: BLE001
                    pass
        self._audit("run.abort", run, by, reason=(reason or "")[:300])
        self._finish(run, "aborted", case_phase="ready")
        return self.get(rid)

    def dispatch_approved(self, rid: int, sid: int, by: str, approvals: Optional[list] = None, job_hash: str = "") -> dict:
        """The L2 hook (WP12 calls it after the approval; the approval's job_hash must equal the step's): dispatch through the
        actuator, host lock in the same transaction. Heavy diagnose steps use it for "Teşhisi çalıştır"."""
        run, s = self._step(rid, sid)
        if s["status"] != "awaiting_approval":
            raise EngineError("act_step_state", "Adım onay beklemiyor", "The step is not awaiting approval")
        if job_hash and job_hash != s.get("job_hash"):
            raise EngineError("act_plan_changed", "Plan değişti: yeni onay gerekli", "The plan changed: a new approval is needed")
        tmpl = self._template(s)
        mode = "rollback" if s["type"] == "rollback" else ("diagnose" if s["type"] == "diagnose" else "execute")
        b = self._body_steps(run).get(s["step_key"], {})
        level, reasons = self.level(run, s, b, tmpl, mode) if s["type"] != "diagnose" else (2, [])
        if level < 2:
            self._guided(run, s, tmpl, s.get("params") or {}, ",".join(reasons) or "level")
            return self.get(rid)
        jid = self._dispatch(run, s, tmpl, s.get("params") or {}, mode, s["step_key"], by=by, approvals=approvals)
        if not jid:
            fresh = next(x for x in self.steps(rid) if x["id"] == s["id"])
            if str(fresh.get("reason") or "").startswith("host_locked"):
                return self.get(rid)
            self._guided(run, s, tmpl, s.get("params") or {}, fresh.get("reason") or "dispatch_refused")
            return self.get(rid)
        self._audit("step.dispatched", run, by, step=s["step_key"], job_id=jid)
        self._case_phase(run, "running", by, needs_human=0)
        return self.advance(rid)


__all__ = ["Engine", "EngineError", "ACTIVE_RUN"]
