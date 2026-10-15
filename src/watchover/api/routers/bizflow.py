"""Business flows: definitions, funnel, stuck / failing items, one key's path, and step suggestions from the live logs."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...bizflow import BizFlows
from ..security import access, pick_team, require, services, user_scope

router = APIRouter(prefix="/flows", tags=["flows"])


class Step(BaseModel):
    name: str
    match: str
    service: str = ""


class FlowIn(BaseModel):
    name: str
    key_regex: str
    steps: List[Step]
    hash_keys: bool = False
    enabled: bool = True
    team_id: Optional[int] = None


class SuggestIn(BaseModel):
    key_regex: str
    minutes: int = 1440


def _hosts(sc):
    return None if sc is None else sc.hosts


def _visible(acc, f: dict) -> bool:
    """A team's flow only for that team; installation-wide flows are shared definitions (their items stay host-scoped)."""
    return acc.wide or f.get("team_id") is None or f["team_id"] in acc.team_ids("page.apm")


def _flow(svc, acc, fid: int, write: bool = False) -> dict:
    f = svc.bizflows.get(fid)
    if not _visible(acc, f):
        raise KeyError(fid)
    if write:
        tid = f.get("team_id")
        if not (acc.install("act.anomaly") if tid is None else acc.can_in(tid, "act.anomaly")):
            raise HTTPException(403, "permission required to change this flow")
    return f


@router.get("")
def list_flows(minutes: int = 1440, acc=Depends(access), user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    out = []
    for f in svc.bizflows.list():
        if not _visible(acc, f):
            continue
        fun = svc.bizflows.funnel(f["id"], minutes or None, _hosts(sc))
        out.append({**f, "items": fun["items"], "completed": fun["completed"], "completion": fun["completion"],
                    "stuck": sum(s["stuck"] for s in fun["steps"]), "errors": sum(s["errors"] for s in fun["steps"]), "funnel": fun["steps"]})
    return out


@router.post("", status_code=201)
def create(body: FlowIn, acc=Depends(access), user=Depends(require("act.anomaly")), svc=Depends(services)):
    tid = None if acc.wide and body.team_id is None else pick_team(acc, "act.anomaly", body.team_id)
    f = svc.bizflows.save(body.name, body.key_regex, [s.model_dump() for s in body.steps], body.hash_keys, body.enabled)
    if tid is not None:
        svc.kb._exec("UPDATE biz_flows SET team_id=? WHERE id=?", (tid, f["id"]))
        f = svc.bizflows.get(f["id"])
    return f


@router.put("/{fid}")
def update(fid: int, body: FlowIn, acc=Depends(access), user=Depends(require("act.anomaly")), svc=Depends(services)):
    _flow(svc, acc, fid, write=True)
    return svc.bizflows.save(body.name, body.key_regex, [s.model_dump() for s in body.steps], body.hash_keys, body.enabled, fid=fid)


@router.delete("/{fid}", status_code=204)
def delete(fid: int, acc=Depends(access), user=Depends(require("act.anomaly")), svc=Depends(services)):
    _flow(svc, acc, fid, write=True)
    svc.bizflows.delete(fid)


@router.post("/suggest")
def suggest(body: SuggestIn, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    from datetime import datetime, timedelta, timezone
    obs = svc.live.snapshot(since=datetime.now(timezone.utc) - timedelta(minutes=max(5, min(body.minutes, 10080))))
    if sc is not None:
        obs = [o for o in obs if sc.allows(o.host)]
    return {"steps": BizFlows.suggest(body.key_regex, obs)}


@router.get("/{fid}/funnel")
def funnel(fid: int, minutes: int = 1440, acc=Depends(access), user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    _flow(svc, acc, fid)
    return svc.bizflows.funnel(fid, minutes or None, _hosts(sc))


@router.get("/{fid}/items")
def items(fid: int, state: str = "stuck", minutes: int = 1440, limit: int = 100, acc=Depends(access), user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    _flow(svc, acc, fid)
    return svc.bizflows.items(fid, state, minutes or None, limit, _hosts(sc))


@router.get("/{fid}/items/{key}")
def item(fid: int, key: str, acc=Depends(access), user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    _flow(svc, acc, fid)
    it = svc.bizflows.item(fid, key)
    if sc is not None and not any(sc.allows(s.get("host")) for s in it["steps"] if not s.get("missing")):
        raise KeyError(key)
    return it
