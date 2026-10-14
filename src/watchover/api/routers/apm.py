"""APM: services (RED, percentiles, Apdex), one service in detail (operations, database and external calls, slow traces),
trace search and the waterfall of one trace. `dataset=<id>` runs the same views over an uploaded dataset instead of the live feed."""
from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException

from ...traces import store_for_observations
from ..security import require, services

router = APIRouter(tags=["apm"])
_cache: dict[str, tuple[int, object]] = {}
_lock = threading.Lock()


def _store(svc, dataset: str | None):
    if not dataset or dataset == "live":
        return svc.traces, 60
    d = svc.datasets.get(dataset)
    a = d["analysis"]
    with _lock:
        hit = _cache.get(dataset)
        if hit and hit[0] == id(a):
            return hit[1], None
        st = store_for_observations(a.observations, settings=None)
        _cache[dataset] = (id(a), st)
        if len(_cache) > 8:
            _cache.pop(next(iter(_cache)))
        return st, None


def _minutes(minutes: int | None, default: int | None) -> int | None:
    return default if minutes is None else (minutes or None)


@router.get("/apm/services")
def services_list(minutes: int | None = None, env: str | None = None, dataset: str | None = None, user=Depends(require("page.apm")), svc=Depends(services)):
    st, dflt = _store(svc, dataset)
    return {"services": st.services(_minutes(minutes, dflt), env or None), "received": getattr(st, "received", 0), "kept": getattr(st, "kept", 0)}


@router.get("/apm/services/{name:path}")
def service_detail(name: str, minutes: int | None = None, env: str | None = None, dataset: str | None = None, user=Depends(require("page.apm")), svc=Depends(services)):
    st, dflt = _store(svc, dataset)
    return st.service(name, _minutes(minutes, dflt), env or None)


@router.get("/traces")
def trace_search(service: str | None = None, operation: str | None = None, minutes: int | None = None, env: str | None = None, min_ms: float | None = None,
                 errors: bool = False, q: str | None = None, session: str | None = None, user_ref: str | None = None, order: str = "recent", limit: int = 50,
                 dataset: str | None = None, user=Depends(require("page.apm")), svc=Depends(services)):
    st, dflt = _store(svc, dataset)
    return st.search(service or None, operation or None, _minutes(minutes, dflt), env or None, min_ms, errors, q or None, session or None, user_ref or None, order, limit)


@router.get("/traces/{trace_id}")
def trace_detail(trace_id: str, dataset: str | None = None, user=Depends(require("page.apm")), svc=Depends(services)):
    st, _ = _store(svc, dataset)
    t = st.trace(trace_id)
    if t is None:
        raise HTTPException(404, "trace not found (outside the retention window or not sampled)")
    return t
