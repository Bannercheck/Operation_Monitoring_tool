"""Final labels: exactly one outcome per step, the worst (docs/design/action-agent.md §4.7, §7.3; C22).

`side_effect` = `rollback_failed` = `unsafe_blocked` > `rolled_back` > `verify_failed` > `exec_error` > `deviated` >
`unverified` > `ok`: a failed verify followed by a rollback counts once, as `rolled_back`. Execution events (`component =
exec`, `act_trust_events`) are written when the label is final, one per executed or human-done act / rollback / diagnose
step; verify steps have none (their result is the act step's outcome). The trust engine (WP9) turns them into levels."""
from __future__ import annotations

import json
from typing import Iterable, List

from .types import cell_key, now, worst

# §7.3: credit c and penalty k of an execution outcome; neutral outcomes are not counted
WEIGHTS = {"ok": (1.0, 0.0), "ok_corrected": (1.0, 0.0), "unverified": (0.0, 0.25), "verify_failed": (0.0, 2.0), "rolled_back": (0.0, 3.0),
           "rollback_failed": (0.0, 4.0), "side_effect": (0.0, 4.0), "unsafe_blocked": (0.0, 4.0), "exec_error": (0.0, 1.0),
           "deviated": (0.0, 0.5), "relapse": (0.0, 0.5)}
NEUTRAL = ("blocked", "rejected_config", "infra_error", "aborted", "not_needed", "skipped")
# reject codes of the actuator (§9.3.4): a check the server had passed → unsafe_blocked; configuration → neutral
UNSAFE_REJECTS = ("param_invalid", "path_escape", "precondition_unsafe", "bad_sig", "wrong_host", "seq_replay")
CONFIG_REJECTS = ("disabled", "allowlist_perms", "halt_local", "halt_server", "key_unconfirmed", "key_unpinned", "mode_denied", "hours",
                  "freeze_local", "no_template", "sha_mismatch", "os_mismatch", "kind_mode", "risk_policy", "denied_local", "cosign_missing")
INFRA_REJECTS = ("not_yet", "expired", "rate_local", "busy", "precondition", "pid_recycled", "manifest_failed", "duplicate", "state_error",
                 "start_failed")


def classify_reject(code: str) -> str:
    if code in UNSAFE_REJECTS:
        return "unsafe_blocked"
    if code in CONFIG_REJECTS:
        return "rejected_config"
    return "infra_error"


def step_outcome(events: Iterable[str]) -> str:
    """The one outcome of a step from everything that happened to it (C22)."""
    ev = [e for e in events if e]
    counted = [e for e in ev if e not in NEUTRAL]
    if counted:
        return worst(counted)
    return ev[0] if ev else ""


def run_label(step_outcomes: Iterable[str], status: str) -> str:
    """The run's label: the worst act / rollback step outcome, or what the run's state says."""
    outs = [o for o in step_outcomes if o and o not in NEUTRAL]
    if outs:
        return worst(outs)
    if status == "aborted":
        return "aborted"
    return "ok" if status in ("completed_ok",) else (status or "")


def write_events(db, audit, run: dict, steps: List[dict], case: dict) -> int:
    """One exec trust event per executed or human-done act / rollback / diagnose step (the step's final outcome)."""
    n = 0
    for s in steps:
        if s["type"] not in ("act", "rollback", "diagnose") or not s.get("outcome") or s["outcome"] in NEUTRAL:
            continue
        if s["outcome"] not in WEIGHTS:
            continue
        if db._exec("SELECT 1 AS x FROM act_trust_events WHERE step_id=? AND component='exec' AND outcome<>'relapse'", (s["id"],)):
            continue
        c, k = WEIGHTS[s["outcome"]]
        rb = run.get("runbook_key") or ""
        ck = cell_key(run.get("team_id"), rb, s["step_key"], run.get("env") or "", run.get("host_class") or "")
        verify = json.loads(s.get("verify") or "{}") if isinstance(s.get("verify"), str) else (s.get("verify") or {})
        db._exec("INSERT INTO act_trust_events (ts, component, cell_key, unit_key, runbook_key, step_key, team_id, env, host_class, host, "
                 "performer, run_id, step_id, case_id, outcome, credit, penalty, omega, blind, level, system_exec, version_id, independent_ok) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (now(), "exec", ck, f"{rb}/{run.get('env') or ''}", rb, s["step_key"], run.get("team_id"), run.get("env") or "",
                  run.get("host_class") or "", run.get("host") or "", s.get("performed_by") or "", run["id"], s["id"], run.get("case_id"),
                  s["outcome"], c, k, 1.0, int(bool(case.get("blind"))), int(s.get("mode") or 0), int((s.get("executor") or "") == "agent"),
                  run.get("version_id"), int(bool(verify.get("independent_ok")))))
        n += 1
    if n:
        audit.write("run.labelled", object_kind="run", object_id=run["id"], team_id=run.get("team_id"),
                    payload={"label": run.get("label"), "events": n})
    return n


def relapse(db, audit, case_id: int) -> int:
    """The case key returned within act_reopen_min: the earlier run gets a `relapse` event (its `ok` stays)."""
    rows = db._exec("SELECT r.*, b.key AS runbook_key FROM act_runs r LEFT JOIN act_runbooks b ON b.id=r.runbook_id "
                    "WHERE r.case_id=? AND r.label IN ('ok','ok_corrected') ORDER BY r.id DESC LIMIT 1", (case_id,))
    if not rows:
        return 0
    run = rows[0]
    n = 0
    for s in db._exec("SELECT * FROM act_steps WHERE run_id=? AND type IN ('act','rollback')", (run["id"],)):
        if db._exec("SELECT 1 AS x FROM act_trust_events WHERE step_id=? AND outcome='relapse'", (s["id"],)):
            continue
        c, k = WEIGHTS["relapse"]
        db._exec("INSERT INTO act_trust_events (ts, component, cell_key, runbook_key, step_key, team_id, env, host_class, host, run_id, step_id, "
                 "case_id, outcome, credit, penalty, omega, level) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (now(), "exec", cell_key(run.get("team_id"), run.get("runbook_key") or "", s["step_key"], run.get("env") or "",
                                          run.get("host_class") or ""), run.get("runbook_key") or "",
                  s["step_key"], run.get("team_id"), run.get("env") or "", run.get("host_class") or "", run.get("host") or "", run["id"],
                  s["id"], case_id, "relapse", c, k, 1.0, int(s.get("mode") or 0)))
        n += 1
    if n:
        audit.write("run.relapse", object_kind="run", object_id=run["id"], team_id=run.get("team_id"), payload={"case_id": case_id})
    return n


__all__ = ["WEIGHTS", "NEUTRAL", "classify_reject", "step_outcome", "run_label", "write_events", "relapse"]
