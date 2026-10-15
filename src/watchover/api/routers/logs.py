"""Log explorer ("Loglar", docs/design/logs.md section 3): search with a query language, histogram, facets, group similar, live
tail, line detail and context, query helpers, saved searches and alerts from a query. Every endpoint needs page.logs and is
narrowed to the viewer's team hosts first (a line on another team's host answers 404)."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel

from ... import logquery as lq
from ...logsearch import BadCursor, Busy, TooFast
from ..security import require, services, user_scope

router = APIRouter(prefix="/logs", tags=["logs"])

BUSY = {"code": "busy", "tr": "Arama kuyruğu dolu, birkaç saniye sonra tekrar deneyin.", "en": "Search queue is full, try again in a few seconds."}


def _run(fn, *a, **kw):
    """Engine errors -> the HTTP answers of the design (400 query / cursor, 429 too fast, 503 busy)."""
    try:
        return fn(*a, **kw)
    except lq.QueryError as e:
        raise HTTPException(400, e.as_dict())
    except BadCursor as e:
        tr, en = lq.MESSAGES.get(e.code, (e.code, e.code))
        raise HTTPException(400, {"code": e.code, "pos": 0, "len": 0, "tr": tr, "en": en})
    except TooFast as e:
        raise HTTPException(429, {"code": "too_fast", "retry_ms": e.retry_ms})
    except Busy:
        raise HTTPException(503, BUSY)


def _who(user: dict, tab: Optional[str]) -> tuple:
    return (user.get("email", ""), (tab or "")[:64])


@router.get("/search")
def search(q: str = "", last: Optional[int] = Query(None, ge=1, le=576000), start: Optional[str] = None, end: Optional[str] = None,
           limit: int = Query(100, ge=1, le=500), cursor: Optional[str] = None, cols: str = "", x_tab_id: Optional[str] = Header(None),
           user=Depends(require("page.logs")), svc=Depends(services), sc=Depends(user_scope)):
    """Lines newest first by (ts, seq); `next` is an opaque keyset cursor; `total` is exact (counters / complete scan), estimated
    or at_least; `scan` says how far the bounded scan got."""
    return _run(svc.logs.search, q, last, start, end, limit, cursor, cols, sc, _who(user, x_tab_id))


@router.get("/histogram")
def histogram(q: str = "", last: Optional[int] = Query(None, ge=1, le=576000), start: Optional[str] = None, end: Optional[str] = None,
              by: str = Query("level", pattern="^(level|service|host|source|pool)$"), top: int = Query(5, ge=1, le=10), x_tab_id: Optional[str] = Header(None),
              user=Depends(require("page.logs")), svc=Depends(services), sc=Depends(user_scope)):
    return _run(svc.logs.histogram, q, last, start, end, by, top, sc, _who(user, x_tab_id))


@router.get("/facets")
def facets(q: str = "", last: Optional[int] = Query(None, ge=1, le=576000), start: Optional[str] = None, end: Optional[str] = None,
           fields: str = "", limit: int = Query(12, ge=1, le=200), search: str = "", x_tab_id: Optional[str] = Header(None),
           user=Depends(require("page.logs")), svc=Depends(services), sc=Depends(user_scope)):
    return _run(svc.logs.facets, q, last, start, end, fields, limit, search, sc, _who(user, x_tab_id))


@router.get("/groups")
def groups(q: str = "", last: Optional[int] = Query(None, ge=1, le=576000), start: Optional[str] = None, end: Optional[str] = None,
           limit: int = Query(100, ge=1, le=500), x_tab_id: Optional[str] = Header(None),
           user=Depends(require("page.logs")), svc=Depends(services), sc=Depends(user_scope)):
    return _run(svc.logs.groups, q, last, start, end, limit, sc, _who(user, x_tab_id))


@router.get("/tail")
def tail(q: str = "", after: Optional[int] = None, limit: int = Query(200, ge=1, le=500), cols: str = "", x_tab_id: Optional[str] = Header(None),
         user=Depends(require("page.logs")), svc=Depends(services), sc=Depends(user_scope)):
    """Live tail: lines that arrived after the cursor `after` (a line id), newest first; at most 500 per poll and 250 per second
    since the previous poll (the rest is counted in `dropped`); 429 when polled faster than once a second per browser tab."""
    return _run(svc.logs.tail, q, after, limit, cols, sc, _who(user, x_tab_id))


@router.get("/line/{lid}")
def line(lid: int, user=Depends(require("page.logs")), svc=Depends(services), sc=Depends(user_scope)):
    return _run(svc.logs.line, lid, sc)


@router.get("/context/{lid}")
def context(lid: int, before: int = Query(20, ge=0, le=200), after: int = Query(20, ge=0, le=200), q: str = "",
            user=Depends(require("page.logs")), svc=Depends(services), sc=Depends(user_scope)):
    return _run(svc.logs.context, lid, before, after, q, sc)


@router.get("/parse")
def parse(q: str = "", user=Depends(require("page.logs")), svc=Depends(services), sc=Depends(user_scope)):
    return _run(svc.logs.parse, q, sc)


@router.get("/rewrite")
def rewrite(q: str = "", op: str = Query(..., pattern="^(add|remove|only|exclude)$"), field: str = Query(..., min_length=1, max_length=80),
            value: str = Query(..., max_length=500), user=Depends(require("page.logs")), svc=Depends(services)):
    return _run(svc.logs.rewrite, q, op, field, value)


@router.get("/fields")
def fields(user=Depends(require("page.logs")), svc=Depends(services), sc=Depends(user_scope)):
    return svc.logs.fields(sc)


# ---------------------------------------------------------------- saved searches (per user, optionally shared)
class SavedIn(BaseModel):
    name: str
    query: str = ""
    range: str = ""
    columns: List[str] = []
    group_similar: bool = False
    shared: bool = False


class SavedPatch(BaseModel):
    name: Optional[str] = None
    query: Optional[str] = None
    range: Optional[str] = None
    columns: Optional[List[str]] = None
    group_similar: Optional[bool] = None
    shared: Optional[bool] = None


def _team_shared(request: Request, user: dict, svc, items: list) -> list:
    """A search someone else shared is listed to installation-wide viewers, and to a team member only when its owner is in one of
    the viewer's teams: a query names hosts, services and words of the logs, so it must not cross teams."""
    try:
        from .. import security as sec
        acc = sec.access_of(request, user)
    except Exception:  # noqa: BLE001 - installations without role-based teams
        return items
    if getattr(acc, "wide", True):
        return items
    mates: set = set()
    for m in acc.memberships:
        try:
            mates |= {e.lower() for e in svc.teams.get(m.team_id)["members"]}
        except KeyError:
            pass
    owner = {r["id"]: (r["owner"] or "").lower() for r in svc.kb._exec("SELECT id, owner FROM log_searches WHERE shared = 1")}
    return [i for i in items if i.get("mine") or owner.get(i.get("id")) in mates]


@router.get("/saved")
def saved(request: Request, user=Depends(require("page.logs")), svc=Depends(services)):
    return {"items": _team_shared(request, user, svc, svc.logs.saved_list(user["email"]))}


@router.post("/saved")
def saved_create(body: SavedIn, user=Depends(require("page.logs")), svc=Depends(services)):
    try:
        return {"id": _run(svc.logs.saved_create, user["email"], body.model_dump() if hasattr(body, "model_dump") else body.dict())}
    except FileExistsError:
        raise HTTPException(409, {"code": "duplicate_name"})


@router.put("/saved/{sid}")
def saved_update(sid: int, body: SavedPatch, user=Depends(require("page.logs")), svc=Depends(services)):
    d = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    try:
        _run(svc.logs.saved_update, sid, user["email"], user.get("role") == "admin", d)
    except PermissionError:
        raise HTTPException(403, "owner or administrator only")
    except FileExistsError:
        raise HTTPException(409, {"code": "duplicate_name"})
    return {"ok": True}


@router.delete("/saved/{sid}")
def saved_delete(sid: int, user=Depends(require("page.logs")), svc=Depends(services)):
    try:
        svc.logs.saved_delete(sid, user["email"], user.get("role") == "admin")
    except PermissionError:
        raise HTTPException(403, "owner or administrator only")
    return {"ok": True}


# ---------------------------------------------------------------- alerts from a query
class AlertIn(BaseModel):
    name: str
    query: str
    window_min: int = 5
    op: str = ">="
    threshold: float = 10
    severity: str = "high"
    channels: str = "email"
    targets: str = ""
    cooldown_min: int = 30
    team_id: Optional[int] = None


def _team_for(request: Request, user: dict, svc, body: AlertIn) -> Optional[int]:
    """The rule's team under role-based teams (a team-scoped author creates a team rule for its own recipients), else None."""
    try:
        from .. import security as sec
        acc = sec.access_of(request, user)
    except Exception:  # noqa: BLE001 - installations without role-based teams
        return None
    if getattr(acc, "wide", True):
        return body.team_id
    tid = sec.pick_team(acc, "act.logalert", body.team_id)
    if tid is not None and hasattr(svc.notifier, "check_team_targets"):
        svc.notifier.check_team_targets(body.targets, body.channels, tid)
    return tid


@router.post("/alerts")
def alert_create(body: AlertIn, request: Request, user=Depends(require("act.logalert")), svc=Depends(services), sc=Depends(user_scope)):
    from ...logwatch import Heavy, TooMany
    from ...notify import SEVERITIES
    if not 1 <= int(body.window_min) <= 60:
        raise HTTPException(400, {"code": "bad_window", "tr": "Pencere 1-60 dakika olmalı.", "en": "The window must be 1-60 minutes."})
    if body.op not in (">=", "<"):
        raise HTTPException(400, {"code": "bad_op", "tr": "Koşul >= ya da < olmalı.", "en": "The condition must be >= or <."})
    if body.severity not in SEVERITIES:
        raise HTTPException(400, {"code": "bad_severity"})
    if not body.query.strip():
        raise HTTPException(400, {"code": "empty_query", "tr": "Uyarı için bir sorgu gerekli.", "en": "An alert needs a query."})
    try:
        mode = _run(svc.logwatch.check, body.query, sc)
    except Heavy:
        raise HTTPException(400, {"code": "lg_alert_heavy", "tr": "Bu sorgu canlı sayım için ağır; bir alan filtresi ekleyin (ör. service: ya da host:).",
                                  "en": "This query is too heavy to count live; add a field filter (e.g. service: or host:)."})
    except TooMany:
        raise HTTPException(409, {"code": "too_many_stream_rules"})
    team_id = _team_for(request, user, svc, body)
    try:
        rid = svc.notifier.add_rule(body.name, "query", body.threshold, "", body.severity, body.channels, body.targets, body.cooldown_min, team_id=team_id)
    except TypeError:                                              # notify without team-owned rules
        rid = svc.notifier.add_rule(body.name, "query", body.threshold, "", body.severity, body.channels, body.targets, body.cooldown_min)
    svc.notifier.set_query(rid, body.query, body.window_min, body.op, user["email"])
    svc.logwatch.refresh(force=True)
    return {"id": rid, "mode": mode}


@router.get("/alerts/preview")
def alert_preview(q: str = "", window_min: int = Query(5, ge=1, le=60), op: str = Query(">=", pattern="^(>=|<)$"), threshold: float = 10,
                  last: int = Query(1440, ge=1, le=2880), x_tab_id: Optional[str] = Header(None),
                  user=Depends(require("page.logs")), svc=Depends(services), sc=Depends(user_scope)):
    return _run(svc.logwatch.preview, q, window_min, op, threshold, last, sc, _who(user, x_tab_id))
