from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..security import require, services, user_scope

router = APIRouter(prefix="/anomalies", tags=["anomalies"])


class StatusIn(BaseModel):
    status: str
    owner: str | None = None
    note: str | None = None


class StepIn(BaseModel):
    done: bool = True


@router.get("")
def list_anomalies(status: str | None = "active", env: str | None = None, kind: str | None = None, limit: int = 200,
                   user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    rows = svc.anomalies.list(None if status in (None, "", "all") else status, env or None, kind or None, limit if sc is None else 2000)
    return rows if sc is None else [r for r in rows if sc.allows(r["host"]) or sc.allows_service(r.get("service") or "")][:limit]


@router.get("/stats")
def stats(user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    st = svc.anomalies.stats()
    if sc is None:
        return st
    mine = [r for r in svc.anomalies.list(None, None, None, 5000) if sc.allows(r["host"]) or sc.allows_service(r.get("service") or "")]
    today = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%d")
    out = {**st, "open": 0, "ack": 0, "resolved": 0, "ignored": 0, "learning_rows": 0, "kinds": {}}
    for r in mine:
        out[r["status"]] = out.get(r["status"], 0) + 1
        if r["status"] in ("open", "ack"):
            out["kinds"][r["kind"]] = out["kinds"].get(r["kind"], 0) + 1
    out["resolved_today"] = sum(1 for r in mine if r["status"] == "resolved" and (r.get("resolved_at") or "") >= today)
    lr = st.get("learning") or {}
    out["learning"] = {**lr, "learning": [h for h in lr.get("learning", []) if sc.allows(h["host"])]}
    return out


@router.post("/scan")
def scan(user=Depends(require("act.anomaly")), svc=Depends(services)):
    return {"found": svc.anomalies.scan(), "stats": svc.anomalies.stats()}


class LearnIn(BaseModel):
    host: str | None = None
    env: str = ""


@router.post("/learning/graduate")
def graduate(body: LearnIn, user=Depends(require("act.anomaly")), svc=Depends(services), sc=Depends(user_scope)):
    """End a host's learning week now: from the next scan its anomalies open and notify."""
    if not body.host:
        raise ValueError("host required")
    if sc is not None and not sc.allows(body.host):
        raise KeyError(body.host)
    return svc.anomalies.graduate(body.host, body.env)


@router.post("/learning/restart")
def restart_learning(body: LearnIn, user=Depends(require("act.anomaly")), svc=Depends(services), sc=Depends(user_scope)):
    if sc is not None and (not body.host or not sc.allows(body.host)):
        raise KeyError(body.host or "all hosts")
    """One host (or all, without host) learns again from now, e.g. after a release that changed what normal looks like."""
    return {"restarted": svc.anomalies.restart_learning(body.host, body.env)}


def _mine(svc, sc, aid: int) -> dict:
    """The anomaly, or 404 when it belongs to a host outside the user's teams (no hint that it exists)."""
    a = svc.anomalies.get(aid)
    if sc is not None and not (sc.allows(a["host"]) or sc.allows_service(a.get("service") or "")):
        raise KeyError(aid)
    return a


@router.get("/{aid}")
def get(aid: int, user=Depends(require("page.ops")), svc=Depends(services), sc=Depends(user_scope)):
    return _mine(svc, sc, aid)


@router.post("/{aid}/status")
def set_status(aid: int, body: StatusIn, user=Depends(require("act.anomaly")), svc=Depends(services), sc=Depends(user_scope)):
    _mine(svc, sc, aid)
    return svc.anomalies.set_status(aid, body.status, body.owner, body.note)


@router.post("/{aid}/steps/{step}")
def set_step(aid: int, step: str, body: StepIn, user=Depends(require("act.anomaly")), svc=Depends(services), sc=Depends(user_scope)):
    _mine(svc, sc, aid)
    return svc.anomalies.set_step(aid, step, body.done)


@router.post("/{aid}/action", status_code=201)
def create_action(aid: int, user=Depends(require("act.anomaly")), svc=Depends(services), sc=Depends(user_scope)):
    _mine(svc, sc, aid)
    a = svc.anomalies.get(aid)
    act = svc.actions.create(f"ANOM-{aid}", a["title"][:120], "P1" if a["score"] >= 8 else "P2", a["owner"] or user["name"], recommendation=a["detail"][:400], evidence=a["key"])
    return {"action": act, "anomaly": svc.anomalies.attach_action(aid, act["id"])}


@router.delete("/{aid}")
def delete(aid: int, user=Depends(require("act.anomaly")), svc=Depends(services), sc=Depends(user_scope)):
    _mine(svc, sc, aid)
    svc.anomalies.get(aid)
    svc.anomalies.delete(aid)
    return {"ok": True}
