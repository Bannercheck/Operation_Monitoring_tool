from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ... import ownership
from ... import sources as wo_sources
from ..security import access, require, services

router = APIRouter(prefix="/sources", tags=["sources"])
EDITABLE = ("name", "kind", "url", "selector", "auth", "user", "secret", "interval", "env", "site", "enabled", "verify_tls", "headers", "ts_field", "lookback_min")


class SourceIn(BaseModel):
    name: str
    kind: str
    url: str
    selector: str = ""
    auth: str = "none"
    user: str = ""
    secret: str = ""
    interval: int = 30
    env: str = ""
    site: str = ""
    enabled: bool = True
    verify_tls: bool = True
    headers: str = ""
    ts_field: str = ""
    lookback_min: int = 15
    team_id: Optional[int] = None


class SourcePatch(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    url: Optional[str] = None
    selector: Optional[str] = None
    auth: Optional[str] = None
    user: Optional[str] = None
    secret: Optional[str] = None
    interval: Optional[int] = None
    env: Optional[str] = None
    site: Optional[str] = None
    enabled: Optional[bool] = None
    verify_tls: Optional[bool] = None
    headers: Optional[str] = None
    ts_field: Optional[str] = None
    lookback_min: Optional[int] = None


def _row(s: wo_sources.Source, team_id: Optional[int] = None, team: str = "") -> dict:
    d = s.as_row()
    d["secret"] = "•••" if d.get("secret") else ""
    d["team_id"], d["team"] = team_id, team
    return d


def _visible(acc, team_id: Optional[int]) -> bool:
    """Installation-wide sources are installation configuration: a team-scoped account sees only its teams' sources."""
    return acc.wide or (team_id is not None and team_id in acc.team_ids("page.src"))


def _mine(svc, acc, sid: int, write: bool = False):
    s = svc.sources.get(sid)
    if not s:
        raise KeyError(sid)
    tid = ownership.team_of(svc.kb, "sources", sid)
    if not _visible(acc, tid):
        raise KeyError(sid)
    if write and not (acc.install("act.sources") if tid is None else acc.can_in(tid, "act.sources")):
        raise HTTPException(403, "permission required to change this source")
    return s, tid


@router.get("/kinds")
def kinds(user=Depends(require("page.src"))):
    return wo_sources.KINDS


@router.get("")
def list_sources(acc=Depends(access), user=Depends(require("page.src")), svc=Depends(services)):
    teams = ownership.team_map(svc.kb, "sources")
    names = {t["id"]: t["name"] for t in svc.teams.list()}
    return [_row(s, teams.get(s.id), names.get(teams.get(s.id), "")) for s in svc.sources.list() if _visible(acc, teams.get(s.id))]


@router.post("", status_code=201)
def add(body: SourceIn, acc=Depends(access), user=Depends(require("act.sources")), svc=Depends(services)):
    from ..security import pick_team
    tid = None if acc.wide and body.team_id is None else pick_team(acc, "act.sources", body.team_id)
    d = body.model_dump(); d.pop("team_id", None)
    s = svc.sources.add(wo_sources.Source(id=None, **d))
    if tid is not None:
        ownership.set_team(svc.kb, "sources", s.id, tid)
    return _row(s, tid)


@router.post("/test")
def test_draft(body: SourceIn, user=Depends(require("act.sources"))):
    """Try a not-yet-saved definition once over its lookback window; nothing is stored."""
    d = body.model_dump(); d.pop("team_id", None)
    ok, msg, rows = wo_sources.test_source(wo_sources.Source(id=None, **d))
    return {"ok": ok, "message": msg, "sample": rows[:5]}


@router.get("/{sid}")
def get(sid: int, acc=Depends(access), user=Depends(require("page.src")), svc=Depends(services)):
    s, tid = _mine(svc, acc, sid)
    return _row(s, tid)


@router.patch("/{sid}")
def update(sid: int, body: SourcePatch, acc=Depends(access), user=Depends(require("act.sources")), svc=Depends(services)):
    _s, tid = _mine(svc, acc, sid, write=True)
    svc.sources.update(sid, **body.model_dump(exclude_none=True))
    return _row(svc.sources.get(sid), tid)


@router.delete("/{sid}")
def delete(sid: int, acc=Depends(access), user=Depends(require("act.sources")), svc=Depends(services)):
    _mine(svc, acc, sid, write=True)
    svc.sources.delete(sid); return {"ok": True}


@router.post("/{sid}/test")
def test(sid: int, acc=Depends(access), user=Depends(require("act.sources")), svc=Depends(services)):
    s, _tid = _mine(svc, acc, sid, write=True)
    ok, msg, rows = wo_sources.test_source(s)
    return {"ok": ok, "message": msg, "sample": rows[:5]}


@router.post("/{sid}/poll")
def poll(sid: int, acc=Depends(access), user=Depends(require("act.sources")), svc=Depends(services)):
    s, _tid = _mine(svc, acc, sid, write=True)
    return {"events": svc.poller.poll_one(s)}
