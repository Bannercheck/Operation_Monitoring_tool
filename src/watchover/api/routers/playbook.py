"""Playbook: what was seen before (per signal template) and what fixed it. A team-scoped account reads the entries of hosts in
its scope and host-less (shared) entries, and edits only entries of its own hosts."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..security import access, require, services, user_scope

router = APIRouter(prefix="/playbook", tags=["playbook"])


class NotesIn(BaseModel):
    resolution: Optional[str] = None
    runbook: Optional[str] = None


def _visible(sc, e: dict) -> bool:
    hosts = e.get("hosts") or []
    return sc is None or not hosts or any(sc.allows(h) for h in hosts)


def _writable(acc, sc, e: dict) -> bool:
    hosts = e.get("hosts") or []
    return acc.wide or sc is None or (bool(hosts) and any(sc.allows(h) for h in hosts))


@router.get("")
def entries(q: str = "", user=Depends(require("page.pb")), svc=Depends(services), sc=Depends(user_scope)):
    return [e for e in svc.playbook.all(q) if _visible(sc, e)]


@router.get("/lookup")
def lookup(template: str, threshold: float = 0.6, user=Depends(require("page.pb")), svc=Depends(services), sc=Depends(user_scope)):
    e = svc.playbook.lookup(template, threshold)
    return e if e is None or _visible(sc, e) else None


@router.get("/entry")
def entry(key: str, user=Depends(require("page.pb")), svc=Depends(services), sc=Depends(user_scope)):
    e = svc.playbook.get(key)
    if not e or not _visible(sc, e):
        raise KeyError(key)
    return e


def _editable(svc, acc, sc, key: str) -> dict:
    e = svc.playbook.get(key)
    if not e or not _visible(sc, e):
        raise KeyError(key)
    if not _writable(acc, sc, e):
        raise HTTPException(403, "shared playbook entries are edited by installation-wide users")
    return e


@router.put("/entry")
def notes(key: str, body: NotesIn, acc=Depends(access), user=Depends(require("page.pb")), svc=Depends(services), sc=Depends(user_scope)):
    _editable(svc, acc, sc, key)
    return svc.playbook.set_notes(key, body.resolution, body.runbook)


@router.delete("/entry")
def delete(key: str, acc=Depends(access), user=Depends(require("page.pb")), svc=Depends(services), sc=Depends(user_scope)):
    _editable(svc, acc, sc, key)
    svc.playbook.delete(key)
    return {"ok": True}
