"""Business flows: definitions, funnel, stuck / failing items, one key's path, and step suggestions from the live logs."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ...bizflow import BizFlows
from ..security import require, services, user_scope

router = APIRouter(prefix="/flows", tags=["flows"])


class Step(BaseModel):
    name: str
    match: str
    service: str = ""


class FlowIn(BaseModel):
    name: str
    key_regex: str
    steps: list[Step]
    hash_keys: bool = False
    enabled: bool = True


class SuggestIn(BaseModel):
    key_regex: str
    minutes: int = 1440


def _hosts(sc):
    return None if sc is None else sc.hosts


@router.get("")
def list_flows(minutes: int = 1440, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    out = []
    for f in svc.bizflows.list():
        fun = svc.bizflows.funnel(f["id"], minutes or None, _hosts(sc))
        out.append({**f, "items": fun["items"], "completed": fun["completed"], "completion": fun["completion"],
                    "stuck": sum(s["stuck"] for s in fun["steps"]), "errors": sum(s["errors"] for s in fun["steps"]), "funnel": fun["steps"]})
    return out


@router.post("", status_code=201)
def create(body: FlowIn, user=Depends(require("act.anomaly")), svc=Depends(services)):
    return svc.bizflows.save(body.name, body.key_regex, [s.model_dump() for s in body.steps], body.hash_keys, body.enabled)


@router.put("/{fid}")
def update(fid: int, body: FlowIn, user=Depends(require("act.anomaly")), svc=Depends(services)):
    svc.bizflows.get(fid)
    return svc.bizflows.save(body.name, body.key_regex, [s.model_dump() for s in body.steps], body.hash_keys, body.enabled, fid=fid)


@router.delete("/{fid}", status_code=204)
def delete(fid: int, user=Depends(require("act.anomaly")), svc=Depends(services)):
    svc.bizflows.delete(fid)


@router.post("/suggest")
def suggest(body: SuggestIn, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    from datetime import datetime, timedelta, timezone
    obs = svc.live.snapshot(since=datetime.now(timezone.utc) - timedelta(minutes=max(5, min(body.minutes, 10080))))
    if sc is not None:
        obs = [o for o in obs if sc.allows(o.host)]
    return {"steps": BizFlows.suggest(body.key_regex, obs)}


@router.get("/{fid}/funnel")
def funnel(fid: int, minutes: int = 1440, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    return svc.bizflows.funnel(fid, minutes or None, _hosts(sc))


@router.get("/{fid}/items")
def items(fid: int, state: str = "stuck", minutes: int = 1440, limit: int = 100, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    return svc.bizflows.items(fid, state, minutes or None, limit, _hosts(sc))


@router.get("/{fid}/items/{key}")
def item(fid: int, key: str, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    it = svc.bizflows.item(fid, key)
    if sc is not None and not any(sc.allows(s.get("host")) for s in it["steps"] if not s.get("missing")):
        raise KeyError(key)
    return it
