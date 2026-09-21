from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..security import require, services

router = APIRouter(prefix="/agents", tags=["agents"])


class EnrollIn(BaseModel):
    name: str
    env: str = ""
    site: str = ""
    tags: str = ""
    note: str = ""


@router.get("")
def list_agents(user=Depends(require("page.conn")), svc=Depends(services)):
    return svc.agents.list()


@router.post("", status_code=201)
def enroll(body: EnrollIn, user=Depends(require("act.connect")), svc=Depends(services)):
    """The plaintext token is returned once."""
    rec, token = svc.agents.enroll(body.name, body.env, body.site, body.tags, body.note)
    return {"agent": rec, "token": token}


@router.get("/enroll-key")
def enroll_key(user=Depends(require("act.connect")), svc=Depends(services)):
    return {"key": svc.agents.enroll_key(create=False)}


@router.post("/enroll-key/rotate")
def rotate_key(user=Depends(require("act.connect")), svc=Depends(services)):
    return {"key": svc.agents.rotate_enroll_key()}


@router.delete("/enroll-key")
def disable_key(user=Depends(require("act.connect")), svc=Depends(services)):
    svc.agents.disable_enroll_key()
    return {"ok": True}


@router.get("/{aid}")
def get(aid: int, user=Depends(require("page.conn")), svc=Depends(services)):
    a = svc.agents.get(aid)
    if not a:
        raise KeyError(aid)
    return a


@router.post("/{aid}/revoke")
def revoke(aid: int, user=Depends(require("act.connect")), svc=Depends(services)):
    svc.agents.revoke(aid); return svc.agents.get(aid)


@router.post("/{aid}/reactivate")
def reactivate(aid: int, user=Depends(require("act.connect")), svc=Depends(services)):
    svc.agents.reactivate(aid); return svc.agents.get(aid)


@router.post("/{aid}/rotate")
def rotate(aid: int, user=Depends(require("act.connect")), svc=Depends(services)):
    return {"token": svc.agents.rotate(aid)}


@router.delete("/{aid}")
def delete(aid: int, user=Depends(require("act.connect")), svc=Depends(services)):
    svc.agents.delete(aid); return {"ok": True}
