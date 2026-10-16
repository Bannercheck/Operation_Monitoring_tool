"""/api/act: status, policy (incl. act_mode), kill switches, audit, key status (docs/design/action-agent.md §12.1).

While act_mode = off every route answers 409 act_disabled except GET /act/status and PUT /act/policy (so the feature can be
switched on). Turning act_mode off needs no step-up (the safe direction, like a stop); every other policy change does."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

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


# ---------------------------------------------------------------- WP2: export, acknowledgement, keys, anchors, two-person changes
class AckIn(BaseModel):
    broken_at: int
    reason: str = ""


class PartIn(BaseModel):
    part: str


class SinksIn(BaseModel):
    sinks: List[Dict[str, Any]] = []


class RecipientsIn(BaseModel):
    recipients: List[str] = []


class ConfirmIn(BaseModel):
    decision: str = "approve"
    inputs: Dict[str, Any] = {}


def _change(svc, kind: str, body: dict, user: dict, reason: str = "") -> dict:
    stepup(svc, user)
    return svc.act.changes.request(kind, body, user["email"], reason)


@router.get("/audit/export")
def audit_export(since: str = "", until: str = "", svc=Depends(act_on), acc=Depends(access), user=Depends(require("act.audit"))):
    """Every row with its MAC and kept payload as NDJSON (oldest first), for an external verifier or the SIEM."""
    from fastapi.responses import Response
    tids = None if acc.wide else acc.team_ids("act.audit")
    rows = list(reversed(svc.act.audit.events("", "", since, until, 100000, team_ids=tids)))
    svc.act.audit.write("audit.exported", object_kind="audit", actor=user["email"], actor_kind="user", payload={"rows": len(rows)})
    body = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
    return Response(body, media_type="application/x-ndjson", headers={"Content-Disposition": "attachment; filename=watchover-act-audit.ndjson"})


@router.post("/audit/acknowledge", status_code=202)
def audit_acknowledge(body: AckIn, svc=Depends(act_on), user=Depends(require_install("act.policy"))):
    """First of two act.policy holders acknowledges a chain break; the `chain` cap lifts when the second confirms."""
    return _change(svc, "chain_ack", {"broken_at": body.broken_at, "reason": body.reason[:300]}, user, f"Zincir kırığı #{body.broken_at}")


@router.post("/keys/dispatch/rotate", status_code=202)
def keys_rotate(svc=Depends(act_on), user=Depends(require_install("act.policy"))):
    return _change(svc, "key_rotate", {}, user, "Dispatch anahtarı yenileme")


@router.post("/keys/export", status_code=202)
def keys_export(body: PartIn, svc=Depends(act_on), user=Depends(require_install("act.policy"))):
    """Encrypted key export: each act.policy holder brings one pass-phrase half; keyd keeps the first in memory (15 min)."""
    stepup(svc, user)
    from ...act.keys import KeyRefused, SigningUnavailable
    try:
        tok = svc.act.keys.export_begin(body.part, user["email"])
    except (KeyRefused, SigningUnavailable) as e:
        raise lift(e)
    return svc.act.changes.request("key_export", {"token": tok["token"]}, user["email"], "Anahtar dışa aktarma")


@router.get("/anchors")
def anchors(svc=Depends(act_on), user=Depends(require_install("act.policy"))):
    a = svc.act.anchors
    return {"freshness": a.freshness(), "latest": a.latest(), "sinks": a.public_sinks(), "recipients": a.recipients(),
            "anchor_pub": svc.act.keys.anchor_pub()}


@router.post("/anchors/now", status_code=201)
def anchor_now(svc=Depends(act_on), user=Depends(require_install("act.policy"))):
    a = svc.act.anchors.anchor("manual")
    if a is None:
        raise err(503, "act_signing_unavailable", "Çapa imzalanamadı: anahtar servisi yok ya da zincir boş",
                  "No anchor: keyd unavailable or the chain is empty")
    return a


@router.get("/anchors/sinks")
def anchor_sinks(svc=Depends(act_on), user=Depends(require_install("act.policy"))):
    return svc.act.anchors.public_sinks()


@router.put("/anchors/sinks", status_code=202)
def anchor_sinks_set(body: SinksIn, svc=Depends(act_on), user=Depends(require_install("act.policy"))):
    from ...act.anchors import Anchors
    from ... import vault
    try:
        items = Anchors.check_sinks(body.sinks)
    except ValueError as e:
        raise err(422, "act_param_invalid", str(e), str(e))
    for it in items:
        it["secret"] = vault.encrypt(it["secret"]) if it["secret"] else ""
    return _change(svc, "anchor_sinks", {"sinks": items}, user, "Dış çapa hedefleri")


@router.post("/anchors/sinks/{sid}/test")
def anchor_sink_test(sid: int, svc=Depends(act_on), user=Depends(require_install("act.policy"))):
    return svc.act.anchors.test_sink(sid)


@router.get("/anchors/recipients")
def anchor_recipients(svc=Depends(act_on), user=Depends(require_install("act.policy"))):
    return {"recipients": svc.act.anchors.recipients()}


@router.put("/anchors/recipients", status_code=202)
def anchor_recipients_set(body: RecipientsIn, svc=Depends(act_on), user=Depends(require_install("act.policy"))):
    rec = sorted({r.strip().lower() for r in body.recipients if "@" in r})
    return _change(svc, "anchor_recipients", {"recipients": rec}, user, "Çapa e-posta alıcıları")


@router.get("/changes")
def changes(svc=Depends(act_on), user=Depends(require_install("act.policy"))):
    return svc.act.changes.pending()


@router.post("/changes/{cid}/confirm")
def change_confirm(cid: int, body: ConfirmIn, svc=Depends(act_on), user=Depends(require_install("act.policy"))):
    su = stepup(svc, user)
    from ...act.approvals import ApprovalError
    from ...act.changes import ChangeError
    from ...act.keys import KeyRefused, SigningUnavailable
    try:
        return svc.act.changes.confirm(cid, user["email"], body.decision, body.inputs, stepup_method=(su or {}).get("method", ""))
    except (ApprovalError, ChangeError, KeyRefused, SigningUnavailable) as e:
        raise lift(e)


# ---------------------------------------------------------------- WP11a: actuators (tokens, key confirmation)
class ActuatorIn(BaseModel):
    agent_id: int
    criticality: str = "normal"


class FingerprintIn(BaseModel):
    fingerprint: str


def _actuator_host(svc, acc, agent_id: int, perm: str = "act.connect") -> str:
    rec = svc.agents.get(int(agent_id))
    if not rec:
        raise KeyError(agent_id)
    host_guard(acc, rec["name"], perm, "page.conn")
    return rec["name"]


@router.get("/actuators")
def actuators(svc=Depends(act_on), acc=Depends(access), user=Depends(require("page.conn"))):
    sc = acc.scope("page.conn")
    rows = svc.act.jobs.actuators.list()
    return [r for r in rows if sc is None or sc.allows(r["host"])]


@router.post("/actuators", status_code=201)
def actuator_add(body: ActuatorIn, svc=Depends(act_on), acc=Depends(access), user=Depends(require("act.connect"))):
    """Issue the per-host actuator token (wa_…, shown once) and the install line (pinned archive, dispatch.pub, anchor.pub)."""
    if body.criticality not in ("low", "normal", "high", "critical"):
        raise err(422, "act_param_invalid", "Geçersiz kritiklik", "Invalid criticality")
    host = _actuator_host(svc, acc, body.agent_id)
    return svc.act.jobs.actuators.issue(body.agent_id, host, user["email"], body.criticality)


@router.post("/actuators/{agent_id}/confirm-key")
def actuator_confirm(agent_id: int, body: FingerprintIn, svc=Depends(act_on), acc=Depends(access), user=Depends(require("act.connect"))):
    _actuator_host(svc, acc, agent_id)
    from ...act.jobs import ChannelError
    try:
        return svc.act.jobs.actuators.confirm_key(agent_id, body.fingerprint, user["email"])
    except ChannelError as e:
        raise err(409, "act_key_mismatch", "Parmak izi ajanın bildirdiği anahtarla uyuşmuyor", e.error)


@router.post("/actuators/{agent_id}/revoke-key")
def actuator_revoke(agent_id: int, svc=Depends(act_on), acc=Depends(access), user=Depends(require("act.connect"))):
    _actuator_host(svc, acc, agent_id)
    return svc.act.jobs.actuators.revoke_key(agent_id, user["email"])


@router.post("/actuators/{agent_id}/rotate-token")
def actuator_rotate(agent_id: int, svc=Depends(act_on), acc=Depends(access), user=Depends(require("act.connect"))):
    _actuator_host(svc, acc, agent_id)
    return svc.act.jobs.actuators.rotate_token(agent_id, user["email"], "manual")
