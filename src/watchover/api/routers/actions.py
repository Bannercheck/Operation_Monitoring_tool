"""Tracked actions. An action belongs to a team (created by a team member, or the first action of a team's dataset) or to a host
(created from an anomaly); a team-scoped account sees and changes only its teams' actions and those of hosts in its scope.
Actions with neither are installation-wide."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..security import access, perm_scope, pick_team, services

router = APIRouter(prefix="/actions", tags=["actions"])


class ActionIn(BaseModel):
    incident_id: str
    title: str
    priority: str = "P2"
    owner: str = ""
    recommendation: str = ""
    evidence: str = ""
    host: str = ""
    team_id: Optional[int] = None


class ActionPatch(BaseModel):
    title: Optional[str] = None
    status: Optional[str] = None
    owner: Optional[str] = None
    priority: Optional[str] = None
    recommendation: Optional[str] = None


def visible(acc, sc, a: dict) -> bool:
    if acc.wide:
        return True
    if a.get("team_id") is not None and a["team_id"] in acc.team_ids("page.actions"):
        return True
    return bool(a.get("host")) and sc is not None and sc.allows(a["host"])


def _mine(svc, acc, sc, aid: int) -> dict:
    a = svc.actions.get(aid)
    if not visible(acc, sc, a):
        raise KeyError(aid)
    return a


def tag(svc, aid: int, team_id: Optional[int] = None, host: str = "") -> dict:
    """Stamp the owning team / host on a freshly created action."""
    svc.actions.db._exec("UPDATE actions SET team_id=?, host=? WHERE id=?", (team_id, host or "", aid))
    return svc.actions.get(aid)


@router.get("")
def list_actions(incident_id: Optional[str] = None, kind: str = "action", acc=Depends(access), svc=Depends(services),
                 sc=Depends(perm_scope("page.actions"))):
    """Plain actions by default; ?kind=all also lists the action agent's cases (?kind=case only those)."""
    if kind not in ("action", "case", "all"):
        raise ValueError("kind must be action, case or all")
    return [a for a in svc.actions.list(incident_id or None, kind=kind) if visible(acc, sc, a)]


@router.post("", status_code=201)
def create(body: ActionIn, acc=Depends(access), svc=Depends(services), sc=Depends(perm_scope("page.actions"))):
    if body.host and sc is not None and not sc.allows(body.host):
        raise KeyError(body.host)
    tid = pick_team(acc, "page.actions", body.team_id) if (body.team_id is not None or (not acc.wide and not body.host)) else None
    a = svc.actions.create(body.incident_id, body.title, body.priority, body.owner, body.recommendation, body.evidence)
    return tag(svc, a["id"], tid, body.host)


@router.get("/{aid}")
def get(aid: int, acc=Depends(access), svc=Depends(services), sc=Depends(perm_scope("page.actions"))):
    return _mine(svc, acc, sc, aid)


@router.patch("/{aid}")
def update(aid: int, body: ActionPatch, acc=Depends(access), svc=Depends(services), sc=Depends(perm_scope("page.actions"))):
    cur = _mine(svc, acc, sc, aid)
    if cur.get("kind") == "case" and body.status is not None:      # cases close through /api/act/cases/{id}/close (§4.6)
        raise HTTPException(409, {"code": "act_case_managed", "tr": "Bu bir vaka; durumu İş kutusu'ndan değiştirin",
                                  "en": "This is a case; change its state from the inbox"})
    a = svc.actions.update(aid, **body.model_dump(exclude_none=True))
    lid = svc.memory.remember_action(a)            # a closed action becomes a resolution memory (owned like the action)
    if lid and (a.get("team_id") is not None or a.get("host")):
        try:
            team = a.get("team_id")
            if team is None:
                owners = svc.teams.owners(a["host"])
                team = owners[0] if len(owners) == 1 else None
            if team is not None:
                svc.kb._exec("UPDATE lessons SET team_id=? WHERE id=?", (team, lid))
        except Exception:  # noqa: BLE001
            pass
    return a


@router.delete("/{aid}")
def delete(aid: int, acc=Depends(access), svc=Depends(services), sc=Depends(perm_scope("page.actions"))):
    if _mine(svc, acc, sc, aid).get("kind") == "case":
        raise HTTPException(409, {"code": "act_case_managed", "tr": "Vakalar silinmez; kapatın ya da bastırın",
                                  "en": "Cases are not deleted; close or suppress them"})
    svc.actions.delete(aid)
    return {"ok": True}
