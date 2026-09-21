from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..security import require, services

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class NoteIn(BaseModel):
    title: str
    text: str
    tags: list[str] = []
    services: list[str] = []


class DocIn(BaseModel):
    name: str
    text: str
    tags: list[str] = []


class RuleIn(BaseModel):
    kind: str
    key: str
    value: str
    reason: str = ""


class DecideIn(BaseModel):
    approve: bool


@router.get("/search")
def search(q: str, k: int = 6, user=Depends(require("page.assist")), svc=Depends(services)):
    return svc.kb.search(q, k)


@router.get("/lessons")
def lessons(kind: str | None = None, limit: int = 200, user=Depends(require("page.assist")), svc=Depends(services)):
    return svc.kb.all(kind or None, limit)


@router.post("/notes", status_code=201)
def add_note(body: NoteIn, user=Depends(require("page.assist")), svc=Depends(services)):
    return svc.kb.get(svc.kb.add_note(body.title, body.text, body.tags, body.services))


@router.post("/docs", status_code=201)
def add_doc(body: DocIn, user=Depends(require("page.assist")), svc=Depends(services)):
    return {"ids": svc.kb.add_doc(body.name, body.text, body.tags)}


@router.delete("/lessons/{lid}")
def delete_lesson(lid: int, user=Depends(require("page.assist")), svc=Depends(services)):
    svc.kb.delete(lid); return {"ok": True}


@router.get("/stats")
def stats(user=Depends(require("page.assist")), svc=Depends(services)):
    return svc.kb.stats()


@router.get("/rules")
def rules(status: str | None = None, user=Depends(require("page.assist")), svc=Depends(services)):
    return svc.kb.rules(status or None)


@router.post("/rules", status_code=201)
def propose(body: RuleIn, user=Depends(require("page.assist")), svc=Depends(services)):
    return {"id": svc.kb.propose(body.kind, body.key, body.value, body.reason, source=f"api:{user['email']}")}


@router.post("/rules/{rid}/decide")
def decide(rid: int, body: DecideIn, user=Depends(require("page.assist")), svc=Depends(services)):
    svc.kb.decide(rid, body.approve)
    return {"applied": svc.kb.apply_rules()}


@router.delete("/rules/{rid}")
def delete_rule(rid: int, user=Depends(require("page.assist")), svc=Depends(services)):
    svc.kb.delete_rule(rid); return {"ok": True}
