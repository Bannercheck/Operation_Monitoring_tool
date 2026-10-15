"""Knowledge base: lessons (patterns, notes, runbooks, feedback, anomaly and resolution memories) and rule proposals. A
team-scoped account reads its teams' rows, memories of hosts in its scope and shared rows (written by installation-wide users);
what it writes belongs to its team. Rules change the engine for everyone: a team decides only its own proposals."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from ... import ownership
from ..security import access, require, services, user_scope

router = APIRouter(prefix="/knowledge", tags=["knowledge"])


class NoteIn(BaseModel):
    title: str
    text: str
    tags: List[str] = []
    services: List[str] = []
    team_id: Optional[int] = None


class DocIn(BaseModel):
    name: str
    text: str
    tags: List[str] = []
    team_id: Optional[int] = None


class RuleIn(BaseModel):
    kind: str
    key: str
    value: str
    reason: str = ""


class DecideIn(BaseModel):
    approve: bool


def _team(acc, team_id: Optional[int] = None) -> Optional[int]:
    if acc.wide:
        return team_id if team_id is not None and acc.can_in(team_id, "page.assist") else None
    ids = acc.team_ids("page.assist")
    return team_id if team_id in ids else (ids[0] if ids else None)


def _stamp(svc, table: str, ids, team: Optional[int]) -> None:
    if team is None:
        return
    for i in ids:
        svc.kb._exec(f"UPDATE {table} SET team_id=? WHERE id=? AND team_id IS NULL", (team, i))


def _kb(svc, acc, sc):
    return ownership.kb_for(svc, acc, sc)


@router.get("/search")
def search(q: str, k: int = 6, acc=Depends(access), user=Depends(require("page.assist")), svc=Depends(services), sc=Depends(user_scope)):
    return _kb(svc, acc, sc).search(q, k)


@router.get("/lessons")
def lessons(kind: Optional[str] = None, limit: int = 200, acc=Depends(access), user=Depends(require("page.assist")), svc=Depends(services), sc=Depends(user_scope)):
    return _kb(svc, acc, sc).all(kind or None, limit)


@router.post("/notes", status_code=201)
def add_note(body: NoteIn, acc=Depends(access), user=Depends(require("page.assist")), svc=Depends(services)):
    lid = svc.kb.add_note(body.title, body.text, body.tags, body.services)
    _stamp(svc, "lessons", [lid], _team(acc, body.team_id))
    return svc.kb.get(lid)


@router.post("/docs", status_code=201)
def add_doc(body: DocIn, acc=Depends(access), user=Depends(require("page.assist")), svc=Depends(services)):
    ids = svc.kb.add_doc(body.name, body.text, body.tags)
    _stamp(svc, "lessons", ids, _team(acc, body.team_id))
    return {"ids": ids}


@router.post("/docs/upload", status_code=201)
async def upload_doc(file: UploadFile = File(...), acc=Depends(access), user=Depends(require("page.assist")), svc=Depends(services)):
    """A runbook / postmortem / wiki page as text or markdown: chunked and searchable."""
    text = (await file.read()).decode("utf-8", "replace")
    ids = svc.kb.add_doc(file.filename or "doc", text, ["doc"])
    _stamp(svc, "lessons", ids, _team(acc))
    return {"ids": ids}


@router.delete("/lessons/{lid}")
def delete_lesson(lid: int, acc=Depends(access), user=Depends(require("page.assist")), svc=Depends(services), sc=Depends(user_scope)):
    row = svc.kb.get(lid)
    if row is None or _kb(svc, acc, sc).get(lid) is None:
        raise KeyError(lid)
    if not acc.wide and row.get("team_id") is None:
        raise HTTPException(403, "shared lessons are removed by installation-wide users")
    svc.kb.delete(lid); return {"ok": True}


@router.get("/stats")
def stats(acc=Depends(access), user=Depends(require("page.assist")), svc=Depends(services), sc=Depends(user_scope)):
    st = svc.kb.stats()
    if acc.wide:
        return st
    rows = _kb(svc, acc, sc).all(None, 5000)
    kinds: dict = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    rules = _kb(svc, acc, sc).rules(None)
    by: dict = {}
    for r in rules:
        by[r["status"]] = by.get(r["status"], 0) + 1
    return {**st, "lessons": kinds, "rules": by}


@router.get("/rules")
def rules(status: Optional[str] = None, acc=Depends(access), user=Depends(require("page.assist")), svc=Depends(services), sc=Depends(user_scope)):
    return _kb(svc, acc, sc).rules(status or None)


@router.post("/rules", status_code=201)
def propose(body: RuleIn, acc=Depends(access), user=Depends(require("page.assist")), svc=Depends(services)):
    rid = svc.kb.propose(body.kind, body.key, body.value, body.reason, source=f"api:{user['email']}")
    _stamp(svc, "rules", [rid], _team(acc))
    return {"id": rid}


def _own_rule(svc, acc, rid: int) -> dict:
    rows = svc.kb._exec("SELECT * FROM rules WHERE id=?", (rid,))
    if not rows:
        raise KeyError(rid)
    r = dict(rows[0])
    if acc.wide:
        return r
    if r.get("team_id") is None:
        raise HTTPException(403, "shared rules are decided by installation-wide users")
    if r["team_id"] not in acc.team_ids("page.assist"):
        raise KeyError(rid)
    return r


@router.post("/rules/{rid}/decide")
def decide(rid: int, body: DecideIn, acc=Depends(access), user=Depends(require("page.assist")), svc=Depends(services)):
    _own_rule(svc, acc, rid)
    svc.kb.decide(rid, body.approve)
    return {"applied": svc.kb.apply_rules()}


@router.delete("/rules/{rid}")
def delete_rule(rid: int, acc=Depends(access), user=Depends(require("page.assist")), svc=Depends(services)):
    _own_rule(svc, acc, rid)
    svc.kb.delete_rule(rid); return {"ok": True}
