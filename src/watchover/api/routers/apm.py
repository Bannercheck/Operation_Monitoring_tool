"""APM: services (RED, percentiles, Apdex), one service in detail (operations, database and external calls, slow traces),
trace search and the waterfall of one trace. `dataset=<id>` runs the same views over an uploaded dataset instead of the live feed."""
from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException

from ...traces import store_for_observations
from ..security import require, services, user_scope

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


def _svc_ok(svc, sc, dataset):
    """None when everything is visible, else a predicate service-name -> visible for this user's teams (live feed only)."""
    if sc is None or (dataset and dataset != "live"):
        return None
    sh = svc.traces.service_hosts(10080)
    try:
        for o in svc.live.snapshot():
            if o.service and o.host:
                sh.setdefault(o.service, set()).add(o.host)
    except Exception:  # noqa: BLE001
        pass
    return lambda name: sc.allows_service(name, sh.get(name))


def _minutes(minutes: int | None, default: int | None) -> int | None:
    return default if minutes is None else (minutes or None)


@router.get("/apm/services")
def services_list(minutes: int | None = None, env: str | None = None, dataset: str | None = None, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    st, dflt = _store(svc, dataset)
    ok = _svc_ok(svc, sc, dataset)
    rows = st.services(_minutes(minutes, dflt), env or None)
    return {"services": [r for r in rows if ok is None or ok(r["service"])], "received": getattr(st, "received", 0), "kept": getattr(st, "kept", 0)}


@router.get("/apm/topology")
def topology(minutes: int | None = None, env: str | None = None, dataset: str | None = None, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    """Service map: trace edges (calls, errors, latency, explicit / inferred) and, for the live feed, the agents' network flows.
    For a team: its services and hosts plus the direct neighbours they talk to (shown, so the boundary is visible)."""
    st, dflt = _store(svc, dataset)
    m = _minutes(minutes, dflt)
    net = svc.flows.graph(m, env or None) if (not dataset or dataset == "live") and getattr(svc, "flows", None) is not None else {"nodes": [], "edges": []}
    top = st.topology(m, env or None)
    ok = _svc_ok(svc, sc, dataset)
    if ok is not None:
        top = _cut(top, ok)
        net = _cut(net, sc.allows)
    return {"services": top, "network": net}


def _cut(g: dict, ok) -> dict:
    edges = [e for e in g["edges"] if ok(e["source"]) or ok(e["target"])]
    keep = {x for e in edges for x in (e["source"], e["target"])} | {n["id"] for n in g["nodes"] if ok(n["id"])}
    return {**g, "edges": edges, "nodes": [n for n in g["nodes"] if n["id"] in keep]}


@router.get("/apm/hosts")
def integration_hosts(minutes: int = 1440, env: str | None = None, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    """Hosts with integrations (from agent network flows): how many inbound and outbound, which protocols."""
    rows = svc.flows.hosts(minutes or None, env or None)
    return rows if sc is None else [h for h in rows if sc.allows(h["host"])]


@router.get("/apm/hosts/{host}/integrations")
def host_integrations(host: str, minutes: int = 1440, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    if sc is not None and not sc.allows(host):
        raise HTTPException(404, "host not found")
    """One host's integration map: callers, listening ports with processes, outbound integrations by category, and the
    services seen on that host in the traces."""
    m = svc.flows.host_map(host, minutes or None)
    rows = svc.kb._exec("SELECT DISTINCT service FROM spans WHERE host = ? AND kind NOT IN ('user','mobile','log') LIMIT 50", (host,))
    m["services"] = sorted(r["service"] for r in rows)
    return m


@router.get("/apm/services/{name:path}")
def service_detail(name: str, minutes: int | None = None, env: str | None = None, dataset: str | None = None, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    st, dflt = _store(svc, dataset)
    ok = _svc_ok(svc, sc, dataset)
    if ok is not None and not ok(name):
        raise HTTPException(404, "service not found")
    return st.service(name, _minutes(minutes, dflt), env or None)


@router.get("/traces")
def trace_search(service: str | None = None, operation: str | None = None, minutes: int | None = None, env: str | None = None, min_ms: float | None = None,
                 errors: bool = False, q: str | None = None, session: str | None = None, user_ref: str | None = None, order: str = "recent", limit: int = 50,
                 dataset: str | None = None, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    st, dflt = _store(svc, dataset)
    ok = _svc_ok(svc, sc, dataset)
    if ok is not None:
        hits = st.search(service or None, operation or None, _minutes(minutes, dflt), env or None, min_ms, errors, q or None, session or None, user_ref or None, order, 500)
        return [h for h in hits if any(ok(x) for x in h["services"])][:limit]
    return st.search(service or None, operation or None, _minutes(minutes, dflt), env or None, min_ms, errors, q or None, session or None, user_ref or None, order, limit)


@router.get("/traces/{trace_id}")
def trace_detail(trace_id: str, dataset: str | None = None, user=Depends(require("page.apm")), svc=Depends(services), sc=Depends(user_scope)):
    st, _ = _store(svc, dataset)
    t = st.trace(trace_id)
    ok = _svc_ok(svc, sc, dataset)
    if t is not None and ok is not None and not any(ok(s["service"]) or sc.allows(s["host"]) for s in t["spans"]):
        t = None
    if t is None:
        raise HTTPException(404, "trace not found (outside the retention window or not sampled)")
    return t
