from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..security import require, services

router = APIRouter(prefix="/anomalies", tags=["anomalies"])


class StatusIn(BaseModel):
    status: str
    owner: str | None = None
    note: str | None = None


class StepIn(BaseModel):
    done: bool = True


@router.get("")
def list_anomalies(status: str | None = "active", env: str | None = None, kind: str | None = None, limit: int = 200,
                   user=Depends(require("page.ops")), svc=Depends(services)):
    return svc.anomalies.list(None if status in (None, "", "all") else status, env or None, kind or None, limit)


@router.get("/stats")
def stats(user=Depends(require("page.ops")), svc=Depends(services)):
    return svc.anomalies.stats()


@router.post("/scan")
def scan(user=Depends(require("act.anomaly")), svc=Depends(services)):
    return {"found": svc.anomalies.scan(), "stats": svc.anomalies.stats()}


@router.get("/{aid}")
def get(aid: int, user=Depends(require("page.ops")), svc=Depends(services)):
    return svc.anomalies.get(aid)


@router.post("/{aid}/status")
def set_status(aid: int, body: StatusIn, user=Depends(require("act.anomaly")), svc=Depends(services)):
    return svc.anomalies.set_status(aid, body.status, body.owner, body.note)


@router.post("/{aid}/steps/{step}")
def set_step(aid: int, step: str, body: StepIn, user=Depends(require("act.anomaly")), svc=Depends(services)):
    return svc.anomalies.set_step(aid, step, body.done)


@router.post("/{aid}/action", status_code=201)
def create_action(aid: int, user=Depends(require("act.anomaly")), svc=Depends(services)):
    a = svc.anomalies.get(aid)
    act = svc.actions.create(f"ANOM-{aid}", a["title"][:120], "P1" if a["score"] >= 8 else "P2", a["owner"] or user["name"], recommendation=a["detail"][:400], evidence=a["key"])
    return {"action": act, "anomaly": svc.anomalies.attach_action(aid, act["id"])}


@router.delete("/{aid}")
def delete(aid: int, user=Depends(require("act.anomaly")), svc=Depends(services)):
    svc.anomalies.get(aid)
    svc.anomalies.delete(aid)
    return {"ok": True}
