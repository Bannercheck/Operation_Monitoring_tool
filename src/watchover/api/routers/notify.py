"""Alert recipients, groups, rules and the delivered-alert log. Rows are installation-wide (team_id NULL: sys.notify installation-
wide) or a team's (act.alerts in that team): a team-scoped account sees and changes only its teams' rows. A team's rule evaluates
only the team's hosts and alerts only the team's own recipients / groups by e-mail or SMS. Recipient e-mails and phone numbers
are personal data (KVKK): they never leave the team that entered them."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ... import notify as wo_notify
from ..security import access, current_user, pick_team, require_install, services

router = APIRouter(prefix="/notify", tags=["notifications"])
TABLES = {"recipients": "recipients", "groups": "notify_groups", "rules": "notify_rules"}


class RecipientIn(BaseModel):
    name: str
    email: str = ""
    phone: str = ""
    groups: str = ""
    note: str = ""
    team_id: Optional[int] = None


class GroupIn(BaseModel):
    name: str
    email: str = ""
    note: str = ""
    team_id: Optional[int] = None


class RuleIn(BaseModel):
    name: str
    condition: str
    threshold: float = 0
    env: str = ""
    severity: str = "high"
    channels: str = "email"
    targets: str = ""
    cooldown_min: int = 30
    team_id: Optional[int] = None


class TestIn(BaseModel):
    channel: str
    target: str


def alerts_user(user=Depends(current_user), acc=Depends(access)):
    """sys.notify (installation role) or act.alerts (a team role) opens the page."""
    if not (acc.can("sys.notify") or acc.can("act.alerts")):
        raise HTTPException(403, "permission required: sys.notify or act.alerts")
    return acc


def _may_write(acc, team_id: Optional[int]) -> bool:
    if acc.install("sys.notify"):
        return True
    return team_id is not None and acc.can_in(team_id, "act.alerts")


def _visible(acc, team_id: Optional[int]) -> bool:
    return acc.wide or (team_id is not None and team_id in acc.team_ids("act.alerts"))


def _rows(svc, acc, rows: list) -> list:
    names = {t["id"]: t["name"] for t in svc.teams.list()}
    return [{**r, "team": names.get(r.get("team_id"), "") if r.get("team_id") is not None else "", "editable": _may_write(acc, r.get("team_id"))}
            for r in rows if _visible(acc, r.get("team_id"))]


def _team_for_new(acc, team_id: Optional[int]) -> Optional[int]:
    if team_id is None:
        if acc.install("sys.notify"):
            return None
        if acc.team_ids("act.alerts"):
            return pick_team(acc, "act.alerts", None)
        raise HTTPException(403, "permission required: sys.notify (installation-wide) or act.alerts (team)")
    if not _may_write(acc, team_id):
        raise HTTPException(403, "permission required in that team: act.alerts")
    return team_id


def _row_check(svc, acc, kind: str, rid: int) -> dict:
    row = getattr(svc.notifier, {"recipients": "recipient", "groups": "group", "rules": "rule"}[kind])(rid)
    if not _visible(acc, row.get("team_id")):
        raise KeyError(rid)
    if not _may_write(acc, row.get("team_id")):
        raise HTTPException(403, "permission required to change this row")
    return row


@router.get("/conditions")
def conditions(acc=Depends(alerts_user)):
    return list(wo_notify.CONDITIONS)


@router.get("/recipients")
def recipients(acc=Depends(alerts_user), svc=Depends(services)):
    return _rows(svc, acc, svc.notifier.recipients())


@router.post("/recipients", status_code=201)
def add_recipient(body: RecipientIn, acc=Depends(alerts_user), svc=Depends(services)):
    tid = _team_for_new(acc, body.team_id)
    d = body.model_dump(); d["team_id"] = tid
    return {"id": svc.notifier.add_recipient(**d)}


@router.patch("/recipients/{rid}")
def update_recipient(rid: int, body: dict, acc=Depends(alerts_user), svc=Depends(services)):
    _row_check(svc, acc, "recipients", rid)
    body = {k: v for k, v in body.items() if k != "team_id"}
    svc.notifier.update_recipient(rid, **body); return {"ok": True}


@router.delete("/recipients/{rid}")
def delete_recipient(rid: int, acc=Depends(alerts_user), svc=Depends(services)):
    _row_check(svc, acc, "recipients", rid)
    svc.notifier.delete_recipient(rid); return {"ok": True}


@router.post("/recipients/{rid}/test")
def test_recipient(rid: int, acc=Depends(alerts_user), svc=Depends(services)):
    """A test message to one recipient (e-mail, else SMS): what a team uses instead of the installation-wide channel test."""
    r = _row_check(svc, acc, "recipients", rid)
    ch, target = ("email", r["email"]) if r.get("email") else ("sms", r.get("phone") or "")
    ok, msg = svc.notifier.test_channel(ch, target)
    return {"ok": ok, "message": msg, "channel": ch}


@router.get("/groups")
def groups(acc=Depends(alerts_user), svc=Depends(services)):
    return _rows(svc, acc, svc.notifier.groups())


@router.post("/groups", status_code=201)
def add_group(body: GroupIn, acc=Depends(alerts_user), svc=Depends(services)):
    tid = _team_for_new(acc, body.team_id)
    d = body.model_dump(); d["team_id"] = tid
    return {"id": svc.notifier.add_group(**d)}


@router.delete("/groups/{gid}")
def delete_group(gid: int, acc=Depends(alerts_user), svc=Depends(services)):
    _row_check(svc, acc, "groups", gid)
    svc.notifier.delete_group(gid); return {"ok": True}


@router.get("/rules")
def rules(acc=Depends(alerts_user), svc=Depends(services)):
    return _rows(svc, acc, svc.notifier.rules())


@router.post("/rules", status_code=201)
def add_rule(body: RuleIn, acc=Depends(alerts_user), svc=Depends(services)):
    tid = _team_for_new(acc, body.team_id)
    if tid is not None:
        svc.notifier.check_team_targets(body.targets, body.channels, tid)
    d = body.model_dump(); d["team_id"] = tid
    return {"id": svc.notifier.add_rule(**d)}


@router.patch("/rules/{rid}")
def update_rule(rid: int, body: dict, acc=Depends(alerts_user), svc=Depends(services)):
    cur = _row_check(svc, acc, "rules", rid)
    body = {k: v for k, v in body.items() if k != "team_id"}
    if cur.get("team_id") is not None and ("targets" in body or "channels" in body):
        svc.notifier.check_team_targets(str(body.get("targets", cur.get("targets") or "")), str(body.get("channels", cur.get("channels") or "")), cur["team_id"])
    svc.notifier.update_rule(rid, **body); return {"ok": True}


@router.delete("/rules/{rid}")
def delete_rule(rid: int, acc=Depends(alerts_user), svc=Depends(services)):
    _row_check(svc, acc, "rules", rid)
    svc.notifier.delete_rule(rid); return {"ok": True}


@router.get("/alerts")
def alerts(n: int = 50, acc=Depends(alerts_user), svc=Depends(services)):
    if acc.wide:
        return svc.notifier.alerts(n)
    mine = set(acc.team_ids("act.alerts"))
    return [a for a in svc.notifier.alerts(max(n, 1) * 10) if a.get("team_id") in mine][:n]


@router.post("/test")
def test(body: TestIn, user=Depends(require_install("sys.notify")), svc=Depends(services)):
    ok, msg = svc.notifier.test_channel(body.channel, body.target)
    return {"ok": ok, "message": msg}


@router.post("/evaluate")
def evaluate(user=Depends(require_install("sys.notify")), svc=Depends(services)):
    return svc.alerts.evaluate()
