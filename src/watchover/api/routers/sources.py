from __future__ import annotations

from dataclasses import fields

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ... import sources as wo_sources
from ..security import require, services

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


class SourcePatch(BaseModel):
    name: str | None = None
    kind: str | None = None
    url: str | None = None
    selector: str | None = None
    auth: str | None = None
    user: str | None = None
    secret: str | None = None
    interval: int | None = None
    env: str | None = None
    site: str | None = None
    enabled: bool | None = None
    verify_tls: bool | None = None
    headers: str | None = None
    ts_field: str | None = None
    lookback_min: int | None = None


def _row(s: wo_sources.Source) -> dict:
    d = s.as_row()
    d["secret"] = "•••" if d.get("secret") else ""
    return d


@router.get("/kinds")
def kinds(user=Depends(require("page.src"))):
    return wo_sources.KINDS


@router.get("")
def list_sources(user=Depends(require("page.src")), svc=Depends(services)):
    return [_row(s) for s in svc.sources.list()]


@router.post("", status_code=201)
def add(body: SourceIn, user=Depends(require("act.sources")), svc=Depends(services)):
    return _row(svc.sources.add(wo_sources.Source(id=None, **body.model_dump())))


@router.get("/{sid}")
def get(sid: int, user=Depends(require("page.src")), svc=Depends(services)):
    s = svc.sources.get(sid)
    if not s:
        raise KeyError(sid)
    return _row(s)


@router.patch("/{sid}")
def update(sid: int, body: SourcePatch, user=Depends(require("act.sources")), svc=Depends(services)):
    if not svc.sources.get(sid):
        raise KeyError(sid)
    svc.sources.update(sid, **body.model_dump(exclude_none=True))
    return _row(svc.sources.get(sid))


@router.delete("/{sid}")
def delete(sid: int, user=Depends(require("act.sources")), svc=Depends(services)):
    svc.sources.delete(sid); return {"ok": True}


@router.post("/{sid}/test")
def test(sid: int, user=Depends(require("act.sources")), svc=Depends(services)):
    s = svc.sources.get(sid)
    if not s:
        raise KeyError(sid)
    ok, msg, rows = wo_sources.test_source(s)
    return {"ok": ok, "message": msg, "sample": rows[:5]}


@router.post("/{sid}/poll")
def poll(sid: int, user=Depends(require("act.sources")), svc=Depends(services)):
    s = svc.sources.get(sid)
    if not s:
        raise KeyError(sid)
    return {"events": svc.poller.poll_one(s)}
