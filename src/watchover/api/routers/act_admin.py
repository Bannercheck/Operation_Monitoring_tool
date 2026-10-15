"""/api/act: status, policy (incl. act_mode), kill switches, audit, key status (docs/design/action-agent.md §12.1).

While act_mode = off every route answers 409 act_disabled except GET /act/status and PUT /act/policy (so the feature can be
switched on). Turning act_mode off needs no step-up (the safe direction, like a stop); every other policy change does."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..security import access, check_team, current_user, host_guard, require, require_install, services
from .act_common import act_on, err, lift, stepup

router = APIRouter(prefix="/act", tags=["act"])


class PolicyIn(BaseModel):
    values: Dict[str, Any] = {}
    act_mode: Optional[str] = None


class KillIn(BaseModel):
    scope: str
    ref: str = ""
    reason: str
    hard: bool = False
    team_id: Optional[int] = None


class ResumeIn(BaseModel):
    reason: str = ""


@router.get("/status")
def status(svc=Depends(services), acc=Depends(access), user=Depends(require("page.actions"))):
    st = svc.act.status()
    tids = None if acc.wide else acc.team_ids("page.auto") + acc.team_ids("page.actions")
    st["kill"] = svc.act.kill.active(tids)
    st["can"] = {k: acc.can(k) for k in ("page.auto", "act.case", "act.execute", "act.approve", "act.kill", "act.policy", "act.audit")}
    st["can"]["policy_install"] = acc.install("act.policy")
    return st


@router.get("/policy")
def get_policy(svc=Depends(act_on), user=Depends(require("page.auto"))):
    return {"policy": svc.act.policy.all(), "stored": svc.act.policy.stored()}


@router.put("/policy")
def put_policy(body: PolicyIn, svc=Depends(services), user=Depends(require_install("act.policy"))):
    values = dict(body.values or {})
    if body.act_mode is not None:
        values["act_mode"] = body.act_mode
    if not values:
        raise err(422, "act_param_invalid", "Değişiklik yok", "Nothing to change")
    if "act_residual_risk_accepted" in values:                # its own route with the note shown (WP12, §10.1)
        raise err(422, "act_param_invalid", "Kalan risk notu kendi ekranından kabul edilir", "The residual-risk note is accepted on its own screen")
    if not (set(values) == {"act_mode"} and values["act_mode"] == "off"):
        stepup(svc, user)                           # policy changes need a fresh step-up; switching off does not
    if not svc.act.enabled and set(values) - {"act_mode"}:
        raise err(409, "act_disabled", "Otomasyon kapalıyken yalnız mod değiştirilebilir", "Only the mode can change while automation is off")
    from ...act.policy import PolicyError
    out = {}
    try:
        for k, v in values.items():
            out[k] = svc.act.set_mode(v, by=user["email"]) if k == "act_mode" else svc.act.policy.set(
                k, v, by=user["email"], custody=svc.act.keys.custody())
    except PolicyError as e:
        raise lift(e)
    return {"changed": out, "status": svc.act.status()}


# ---------------------------------------------------------------- kill switches (§10.3)
def _runbook_team(svc, key: str) -> Optional[int]:
    rows = svc.kb._exec("SELECT team_id FROM act_runbooks WHERE key=?", (key,))
    if not rows:
        raise KeyError(key)
    return rows[0]["team_id"]


def _kill_guard(svc, acc, scope: str, ref: str, team_id: Optional[int], perm: str):
    """Who may stop (perm=act.kill) or resume (perm=act.promote) at which scope; returns the owning team of the stop."""
    if scope == "global":
        if not acc.install("act.kill" if perm == "act.kill" else "act.policy"):
            raise err(403, "act_sod" if perm != "act.kill" else "act_forbidden", "Kurulum geneli yetki gerekli",
                      "Installation-wide permission required")
        return None
    if scope == "template":
        if not acc.install("act.policy"):
            raise err(403, "act_forbidden", "Kurulum act.policy yetkisi gerekli", "Installation act.policy required")
        return None
    if scope == "team":
        tid = int(ref)
        check_team(acc, tid, perm, "page.auto")
        return tid
    if scope == "runbook":
        tid = _runbook_team(svc, ref)
        check_team(acc, tid, perm, "page.auto")
        return tid
    if scope == "host":
        host_guard(acc, ref, perm, "page.auto")
        owners = svc.teams.owners(ref)
        return owners[0] if len(owners) == 1 else team_id
    raise err(422, "act_param_invalid", f"Geçersiz kapsam: {scope}", f"Invalid scope: {scope}")


@router.get("/kill")
def kills(svc=Depends(act_on), acc=Depends(access), user=Depends(require("page.auto"))):
    return svc.act.kill.active(None if acc.wide else acc.team_ids("page.auto"))


@router.post("/kill", status_code=201)
def kill(body: KillIn, svc=Depends(act_on), acc=Depends(access), user=Depends(require("act.kill"))):
    tid = _kill_guard(svc, acc, body.scope, body.ref, body.team_id, "act.kill")
    from ...act.limits import KillError
    try:
        return svc.act.kill.stop(body.scope, body.ref, by=user["email"], reason=body.reason, team_id=tid, hard=body.hard)
    except KillError as e:
        raise lift(e)


@router.post("/kill/{kid}/resume")
def resume(kid: int, body: ResumeIn, svc=Depends(act_on), acc=Depends(access), user=Depends(current_user)):
    rows = svc.kb._exec("SELECT * FROM act_kill WHERE id=?", (kid,))
    if not rows:
        raise KeyError(kid)
    k = rows[0]
    _kill_guard(svc, acc, k["scope"], k["ref"], k["team_id"], "act.promote")
    if k["scope"] in ("global", "team"):
        stepup(svc, user)
    from ...act.limits import KillError
    try:
        return svc.act.kill.resume(kid, by=user["email"], reason=body.reason)
    except KillError as e:
        raise lift(e)


# ---------------------------------------------------------------- audit and keys
@router.get("/audit")
def audit(object: str = "", id: str = "", since: str = "", until: str = "", limit: int = 200, svc=Depends(act_on), acc=Depends(access),
          user=Depends(require("act.audit"))):
    tids = None if acc.wide else acc.team_ids("act.audit")
    rows = svc.act.audit.events(object, id, since, until, min(max(limit, 1), 1000), team_ids=tids)
    actors = {r["pseudo"]: r["email"] for r in svc.kb._exec("SELECT pseudo, email FROM act_actors WHERE erased_at=''")}
    for r in rows:
        r["actor_display"] = actors.get(r["actor"], r["actor"])
    return rows


@router.get("/audit/verify")
def audit_verify(svc=Depends(act_on), user=Depends(require("act.audit"))):
    return svc.act.audit.verify()


@router.get("/keys/status")
def keys_status(svc=Depends(act_on), user=Depends(require_install("act.policy"))):
    return svc.act.keys.status()
