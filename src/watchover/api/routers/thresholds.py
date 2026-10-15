"""Per-server threshold policies: list / create / update / delete, a live preview of a draft (which servers it covers and which
are beyond it now), the per-server overview (current values, effective levels and where they come from) and the open breaches."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ...thresholds import CHANNELS, METRICS, SCOPES
from ..security import require, services, user_scope

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


@router.get("/meta")
def meta(user=Depends(require("page.ops"))):
    return {"metrics": METRICS, "scopes": SCOPES, "channels": CHANNELS}


@router.get("")
def list_policies(user=Depends(require("page.ops")), svc=Depends(services)):
    return svc.thresholds.list()


@router.post("", status_code=201)
def create(body: PolicyIn, user=Depends(require("sys.notify")), svc=Depends(services)):
    return svc.thresholds.save(body.model_dump())


@router.put("/{pid}")
def update(pid: int, body: PolicyIn, user=Depends(require("sys.notify")), svc=Depends(services)):
    return svc.thresholds.save(body.model_dump(), pid)


@router.delete("/{pid}", status_code=204)
def delete(pid: int, user=Depends(require("sys.notify")), svc=Depends(services)):
    svc.thresholds.delete(pid)


@router.post("/preview")
def preview(body: PolicyIn, user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    out = svc.thresholds.preview(body.model_dump())
    if sc is not None:
        out["hosts"] = [h for h in out["hosts"] if sc.allows(h)]
        out["beyond"] = [b for b in out["beyond"] if sc.allows(b["host"])]
    return out


@router.get("/overview")
def overview(user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    return {"hosts": svc.thresholds.overview(None if sc is None else sc.allows), "runs": svc.thresholds.runs, "last_run": svc.thresholds.last_run}


@router.get("/state")
def state(user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    return [s for s in svc.thresholds.state() if sc is None or sc.allows(s["host"])]


@router.post("/evaluate")
def evaluate(user=Depends(require("sys.notify")), svc=Depends(services)):
    return {"sent": svc.thresholds.evaluate()}
