from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..security import require, services

router = APIRouter(prefix="/actions", tags=["actions"])


class ActionIn(BaseModel):
    incident_id: str
    title: str
    priority: str = "P2"
    owner: str = ""
    recommendation: str = ""
    evidence: str = ""


class ActionPatch(BaseModel):
    title: str | None = None
    status: str | None = None
    owner: str | None = None
    priority: str | None = None
    recommendation: str | None = None


@router.get("")
def list_actions(incident_id: str | None = None, user=Depends(require("page.data")), svc=Depends(services)):
    return svc.actions.list(incident_id or None)


@router.post("", status_code=201)
def create(body: ActionIn, user=Depends(require("page.data")), svc=Depends(services)):
    return svc.actions.create(body.incident_id, body.title, body.priority, body.owner, body.recommendation, body.evidence)


@router.get("/{aid}")
def get(aid: int, user=Depends(require("page.data")), svc=Depends(services)):
    return svc.actions.get(aid)


@router.patch("/{aid}")
def update(aid: int, body: ActionPatch, user=Depends(require("page.data")), svc=Depends(services)):
    return svc.actions.update(aid, **body.model_dump(exclude_none=True))


@router.delete("/{aid}")
def delete(aid: int, user=Depends(require("page.data")), svc=Depends(services)):
    svc.actions.get(aid)
    svc.actions.delete(aid)
    return {"ok": True}
