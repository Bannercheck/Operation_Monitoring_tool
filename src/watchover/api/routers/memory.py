"""Corporate memory: stats, semantic search, learn-now, background re-indexing and the day-by-day timeline. A team-scoped account
sees only the memories it may read (its teams', its hosts', and shared ones)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ... import ownership
from ..security import access, require, require_install, services, user_scope

router = APIRouter(prefix="/memory", tags=["memory"])


@router.get("/stats")
def stats(acc=Depends(access), user=Depends(require("page.assist")), svc=Depends(services), sc=Depends(user_scope)):
    st = svc.memory.stats()
    if acc.wide:
        return st
    rows = ownership.kb_for(svc, acc, sc).all(None, 5000)
    kinds: dict = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    return {**st, "lessons": kinds, "total": len(rows)}


@router.get("/search")
def search(q: str = "", k: int = 12, kinds: str = "", acc=Depends(access), user=Depends(require("page.assist")), svc=Depends(services), sc=Depends(user_scope)):
    kb = ownership.kb_for(svc, acc, sc)
    ks = tuple(x for x in kinds.split(",") if x) or None
    if not q.strip():
        return kb.all(ks[0] if ks and len(ks) == 1 else None, k)
    return kb.search(q, k, ks)


@router.get("/timeline")
def timeline(days: int = 14, acc=Depends(access), user=Depends(require("page.assist")), svc=Depends(services), sc=Depends(user_scope)):
    if acc.wide:
        return svc.memory.timeline(days)
    out: dict = {}
    for r in ownership.kb_for(svc, acc, sc).all(None, 5000):
        day = str(r.get("updated_at") or "")[:10]
        if not day:
            continue
        d = out.setdefault(day, {"day": day, "total": 0})
        d[r["kind"]] = d.get(r["kind"], 0) + 1; d["total"] += 1
    return sorted(out.values(), key=lambda d: d["day"], reverse=True)[:days]


@router.get("/runs")
def runs(n: int = 10, user=Depends(require("page.assist")), svc=Depends(services)):
    return svc.memory.learner.history(n)


@router.post("/learn")
def learn(user=Depends(require("page.assist")), svc=Depends(services)):
    return svc.memory.learn_now()


@router.post("/reindex")
def reindex(user=Depends(require_install("page.assist")), svc=Depends(services)):
    return svc.memory.reindex(background=True)


@router.get("/reindex")
def reindex_state(user=Depends(require("page.assist")), svc=Depends(services)):
    return dict(svc.memory.reindex_state)
