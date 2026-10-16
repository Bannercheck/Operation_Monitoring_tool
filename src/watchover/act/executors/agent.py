"""The agent executor (docs/design/action-agent.md §9.1, §9.3): a step runs on the host's actuator through the pull job channel.

plan() has no side effects and binds what people see to what runs (`job_hash`); dispatch() queues the job and takes the host
lock in the same transaction (act/jobs.py); poll() turns the job row into a JobResult; cancel() is best effort."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

from ..types import JobResult, Plan, job_hash

STATE = {"done": "ok", "failed": "failed", "rejected": "rejected", "cancelled": "cancelled", "expired": "expired", "unknown": "unknown"}


class AgentExecutor:
    kind = "agent"

    def __init__(self, act):
        self.act = act

    def can_run(self, step: Dict[str, Any], host: str, mode: str = "") -> Tuple[bool, str]:
        rec = self.act.jobs.actuators.for_host(host)
        if not rec:
            return False, "no_actuator"
        if rec.get("key_status") != "confirmed":
            return False, "key_unconfirmed"
        if not rec.get("allowlist_ok"):
            return False, "allowlist_perms"
        if mode in ("execute", "rollback") and rec.get("insecure_install"):
            return False, "insecure_install"
        if rec.get("halt"):
            return False, "halt_local"
        if abs(int(rec.get("clock_skew_s") or 0)) > 120:
            return False, "clock_skew"
        if not self.act.keys.available():
            return False, "act_signing_unavailable"
        tid = step.get("template_id") or step.get("template")
        reported = {t.get("id"): t for t in json.loads(rec.get("templates") or "[]") if isinstance(t, dict)}
        if tid and tid not in reported:
            return False, "no_template"
        want = step.get("template_sha256") or ((self.act.catalogue.get(tid) or {}).get("sha256") if tid else "")
        if tid and want and reported[tid].get("sha256") and reported[tid]["sha256"] != want:
            return False, "template_differs"                 # the host runs another revision: guided there (§5.1)
        return True, ""

    def plan(self, step: Dict[str, Any], params: Dict[str, Any], host: str, mode: str, version_sha: str = "") -> Plan:
        tid = step.get("template_id") or step.get("template") or ""
        t = self.act.catalogue.get(tid) or {}
        sha = step.get("template_sha256") or t.get("sha256", "")
        preview = ()
        try:
            from ..templates import render_argv
            body = json.loads(t["body"]) if isinstance(t.get("body"), str) else (t.get("body") or {})
            preview = tuple(render_argv(body, params, mode=mode) or ())
        except Exception:  # noqa: BLE001 - a preview is informative only
            preview = ()
        return Plan(executor="agent", host=host, template_id=tid, template_sha256=sha, params=dict(params), mode=mode, preview=preview,
                    human_cmd=" ".join(preview), job_hash=job_hash(host, tid, sha, params, mode, version_sha, step.get("key", "")))

    def dispatch(self, plan: Plan, **ctx) -> str:
        j = self.act.jobs.dispatch(host=plan.host, template_id=plan.template_id, template_sha256=plan.template_sha256, params=plan.params,
                                   mode=plan.mode, job_hash=plan.job_hash, **ctx)
        return j["job_id"]

    def poll(self, handle: str) -> Optional[JobResult]:
        j = self.act.jobs.get(handle)
        if j["state"] in ("queued", "leased", "running"):
            return None
        res = json.loads(j.get("result") or "{}")
        return JobResult(state=STATE.get(j["state"], j["state"]), exit_code=res.get("exit_code"), output=res.get("display_tail", ""),
                         truncated=bool(res.get("truncated")), facts=res.get("fields") or {}, reject_code=j.get("reject_code") or "")

    def cancel(self, handle: str) -> bool:
        return self.act.jobs.cancel(handle)
