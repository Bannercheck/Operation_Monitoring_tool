"""Per-server threshold policies: list / create / update / delete, a live preview of a draft (which servers it covers and which
are beyond it now), the per-server overview (current values, effective levels and where they come from) and the open breaches.

A policy is installation-wide (team_id NULL: written with sys.notify installation-wide) or a team's (written with act.thresholds
in that team, or sys.notify installation-wide). A team's policy covers only the team's hosts, beats installation-wide policies on
them, and alerts only the team's own recipients / groups by e-mail or SMS (no webhook: an arbitrary URL would leak alert data)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...thresholds import CHANNELS, METRICS, SCOPES
from ..security import access, perm_scope, pick_team, require, require_install, services

router = APIRouter(prefix="/thresholds", tags=["thresholds"])


class PolicyIn(BaseModel):
    name: str = ""
    scope: str
    scope_value: str = ""
    metric: str
    warn: Optional[float] = None      # Optional[]: pydantic evaluates these at runtime, and X | None needs Python 3.10
    crit: Optional[float] = None
    for_min: int = 3
    channels: str = "email"
    targets: str = ""
    cooldown_min: int = 30
    notify_resolved: bool = True
    enabled: bool = True
    note: str = ""
    team_id: Optional[int] = None


def _may_write(acc, team_id: Optional[int]) -> bool:
    if acc.install("sys.notify"):
        return True
    return team_id is not None and acc.can_in(team_id, "act.thresholds")


def _team_for_new(acc, team_id: Optional[int]) -> Optional[int]:
    """None (installation-wide) needs sys.notify installation-wide; a team policy needs act.thresholds there. A team-scoped
    manager who leaves the team out gets their only team."""
    if team_id is None:
        if acc.install("sys.notify"):
            return None
        if acc.team_ids("act.thresholds"):
            return pick_team(acc, "act.thresholds", None)
        raise HTTPException(403, "permission required: sys.notify (installation-wide) or act.thresholds (team)")
    if not _may_write(acc, team_id):
        raise HTTPException(403, "permission required in that team: act.thresholds")
    return team_id


def _validate_team(svc, body: dict, team_id: Optional[int], acc=None) -> None:
    if team_id is not None:
        svc.notifier.check_team_targets(body.get("targets") or "", body.get("channels") or "", team_id)
    _app_scope(svc, body, acc)


def _app_scope(svc, body: dict, acc) -> None:
    """scope "application": the value must name an application the writer may see (watchover.applications); stored as its name."""
    if body.get("scope") != "application":
        return
    a = svc.apps.by_name(str(body.get("scope_value") or ""))
    if a is None or (acc is not None and not svc.apps.visible(a, acc.scope("page.thr"))):
        raise HTTPException(400, "unknown application")
    body["scope_value"] = a["name"]


@router.get("/meta")
def meta(acc=Depends(access), svc=Depends(services), user=Depends(require("page.thr"))):
    apps = []
    if getattr(svc, "apps", None) is not None:
        apps = svc.apps.names(acc.scope("page.thr"))       # application scope: the names the writer may pick
    return {"metrics": METRICS, "scopes": SCOPES, "channels": CHANNELS, "applications": apps}


@router.get("")
def list_policies(acc=Depends(access), svc=Depends(services), _u=Depends(require("page.thr"))):
    """Installation-wide policies (read-only unless sys.notify installation-wide) and the policies of the caller's teams."""
    names = {t["id"]: t["name"] for t in svc.teams.list()}
    mine = set(acc.team_ids("page.thr"))
    out = []
    for p in svc.thresholds.list():
        tid = p.get("team_id")
        if not acc.wide and tid is not None and tid not in mine:
            continue
        out.append({**p, "team": names.get(tid, "") if tid is not None else "", "editable": _may_write(acc, tid)})
    return out


@router.post("", status_code=201)
def create(body: PolicyIn, acc=Depends(access), svc=Depends(services), _u=Depends(require("page.thr"))):
    d = body.model_dump()
    d["team_id"] = _team_for_new(acc, body.team_id)
    _validate_team(svc, d, d["team_id"], acc)
    return svc.thresholds.save(d)


@router.put("/{pid}")
def update(pid: int, body: PolicyIn, acc=Depends(access), svc=Depends(services), _u=Depends(require("page.thr"))):
    cur = svc.thresholds.get(pid)
    if not acc.wide and cur.get("team_id") is not None and cur["team_id"] not in acc.team_ids("page.thr"):
        raise KeyError(f"policy {pid}")
    if not _may_write(acc, cur.get("team_id")):
        raise HTTPException(403, "permission required to edit this policy")
    d = body.model_dump()
    d["team_id"] = cur.get("team_id") if "team_id" not in body.model_fields_set else _team_for_new(acc, body.team_id)
    _validate_team(svc, d, d["team_id"], acc)
    return svc.thresholds.save(d, pid)


@router.delete("/{pid}", status_code=204)
def delete(pid: int, acc=Depends(access), svc=Depends(services), _u=Depends(require("page.thr"))):
    cur = svc.thresholds.get(pid)
    if not acc.wide and cur.get("team_id") is not None and cur["team_id"] not in acc.team_ids("page.thr"):
        raise KeyError(f"policy {pid}")
    if not _may_write(acc, cur.get("team_id")):
        raise HTTPException(403, "permission required to delete this policy")
    svc.thresholds.delete(pid)


@router.post("/preview")
def preview(body: PolicyIn, acc=Depends(access), svc=Depends(services), sc=Depends(perm_scope("page.thr"))):
    d = body.model_dump()
    _app_scope(svc, d, acc)
    out = svc.thresholds.preview(d)
    if sc is not None:
        out["hosts"] = [h for h in out["hosts"] if sc.allows(h)]
        out["beyond"] = [b for b in out["beyond"] if sc.allows(b["host"])]
        out["covered"] = len(out["hosts"])
    return out


@router.get("/overview")
def overview(svc=Depends(services), sc=Depends(perm_scope("page.thr"))):
    names = {t["id"]: t["name"] for t in svc.teams.list()}
    rows = svc.thresholds.overview(None if sc is None else sc.allows)
    for r in rows:
        for m in r["metrics"].values():
            src = m.get("source")
            if src and src.get("team_id") is not None:
                src["team"] = names.get(src["team_id"], "")
    return {"hosts": rows, "runs": svc.thresholds.runs, "last_run": svc.thresholds.last_run}


@router.get("/state")
def state(svc=Depends(services), sc=Depends(perm_scope("page.thr"))):
    return [s for s in svc.thresholds.state() if sc is None or sc.allows(s["host"])]


@router.post("/evaluate")
def evaluate(user=Depends(require_install("sys.notify")), svc=Depends(services)):
    return {"sent": svc.thresholds.evaluate()}
