from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..security import require, services

router = APIRouter(prefix="/playbook", tags=["playbook"])


class NotesIn(BaseModel):
    resolution: str | None = None
    runbook: str | None = None


@router.get("")
def entries(q: str = "", user=Depends(require("page.pb")), svc=Depends(services)):
    return svc.playbook.all(q)


@router.get("/lookup")
def lookup(template: str, threshold: float = 0.6, user=Depends(require("page.pb")), svc=Depends(services)):
    return svc.playbook.lookup(template, threshold)


@router.get("/entry")
def entry(key: str, user=Depends(require("page.pb")), svc=Depends(services)):
    e = svc.playbook.get(key)
    if not e:
        raise KeyError(key)
    return e


@router.put("/entry")
def notes(key: str, body: NotesIn, user=Depends(require("page.pb")), svc=Depends(services)):
    if not svc.playbook.get(key):
        raise KeyError(key)
    return svc.playbook.set_notes(key, body.resolution, body.runbook)


@router.delete("/entry")
def delete(key: str, user=Depends(require("page.pb")), svc=Depends(services)):
    svc.playbook.delete(key)
    return {"ok": True}
