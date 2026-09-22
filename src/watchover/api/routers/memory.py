"""Corporate memory: stats, semantic search, learn-now, background re-indexing and the day-by-day timeline."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..security import require, services

router = APIRouter(prefix="/memory", tags=["memory"])


@router.get("/stats")
def stats(user=Depends(require("page.assist")), svc=Depends(services)):
    return svc.memory.stats()


@router.get("/search")
def search(q: str = "", k: int = 12, kinds: str = "", user=Depends(require("page.assist")), svc=Depends(services)):
    ks = tuple(x for x in kinds.split(",") if x) or None
    if not q.strip():
        return svc.memory.recent(k, ks[0] if ks and len(ks) == 1 else None)
    return svc.kb.search(q, k, ks)


@router.get("/timeline")
def timeline(days: int = 14, user=Depends(require("page.assist")), svc=Depends(services)):
    return svc.memory.timeline(days)


@router.get("/runs")
def runs(n: int = 10, user=Depends(require("page.assist")), svc=Depends(services)):
    return svc.memory.learner.history(n)


@router.post("/learn")
def learn(user=Depends(require("page.assist")), svc=Depends(services)):
    return svc.memory.learn_now()


@router.post("/reindex")
def reindex(user=Depends(require("page.assist")), svc=Depends(services)):
    return svc.memory.reindex(background=True)


@router.get("/reindex")
def reindex_state(user=Depends(require("page.assist")), svc=Depends(services)):
    return dict(svc.memory.reindex_state)
