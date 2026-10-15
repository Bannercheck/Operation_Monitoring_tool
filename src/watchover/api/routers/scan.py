"""Vulnerability scanning of inventory hosts: catalog-based advisory match, posture checks and an opt-in reachability probe.
A team-scoped account reads and scans only its teams' hosts; reloading the advisory catalogue is installation-wide."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..security import access, host_guard, perm_scope, require, require_install, services

router = APIRouter(prefix="/scan", tags=["scan"])


class ScanIn(BaseModel):
    probe: bool = False                 # opt-in network reachability pass (needs act.scan); default off, catalog-only


def _results(svc, sc) -> list:
    rows = svc.scanner.results()
    return rows if sc is None else [r for r in rows if sc.allows(r["hostname"])]


@router.get("/stats")
def stats(acc=Depends(access), svc=Depends(services), sc=Depends(perm_scope("page.scan"))):
    if sc is None:
        return svc.scanner.stats()
    rows = _results(svc, sc)
    agg = {"hosts_scanned": len(rows), "catalog": len(svc.scanner.catalog)}
    for s in ("critical", "high", "medium", "low"):
        agg[s] = sum(r.get(s, 0) for r in rows)
    agg["exposed"] = sum(1 for r in rows if r.get("worst") in ("critical", "high"))
    runs = _runs(svc, acc, 1)
    agg["last_run"] = runs[0] if runs else None
    return agg


@router.get("/results")
def results(svc=Depends(services), sc=Depends(perm_scope("page.scan"))):
    return _results(svc, sc)


@router.get("/results/{hostname}")
def result(hostname: str, svc=Depends(services), sc=Depends(perm_scope("page.scan"))):
    r = svc.scanner.result(hostname)
    if r is None or (sc is not None and not sc.allows(r["hostname"])):
        raise KeyError(hostname)
    return r


def _runs(svc, acc, n: int) -> list:
    if acc.wide:
        return svc.scanner.runs(n)
    mine = set(acc.team_ids("page.scan"))
    rows = [dict(r) for r in svc.kb._exec("SELECT * FROM scan_runs ORDER BY id DESC LIMIT ?", (max(n, 1) * 20,))]
    return [r for r in rows if r.get("team_id") in mine][:n]


@router.get("/runs")
def runs(n: int = 10, acc=Depends(access), svc=Depends(services), _u=Depends(require("page.scan"))):
    return _runs(svc, acc, n)


@router.get("/catalog")
def catalog(user=Depends(require("page.scan")), svc=Depends(services)):
    return {"count": len(svc.scanner.catalog), "advisories": svc.scanner.catalog}


@router.post("/catalog/reload")
def reload_catalog(user=Depends(require_install("act.scan")), svc=Depends(services)):
    return {"count": svc.scanner.reload_catalog()}


@router.post("/host/{hostname}")
def scan_host(hostname: str, body: ScanIn, acc=Depends(access), svc=Depends(services), _u=Depends(require("act.scan"))):
    rec = svc.inventory.get(hostname)
    if rec is None:
        raise KeyError(hostname)
    host_guard(acc, rec["hostname"], "act.scan", "page.scan")
    return svc.scanner.scan_host(rec, probe=body.probe)


@router.post("/all")
def scan_all(body: ScanIn, acc=Depends(access), svc=Depends(services), sc=Depends(perm_scope("act.scan"))):
    """Every inventory host in the caller's act.scan scope (a team scans only its own hosts)."""
    recs = svc.inventory.list()
    if sc is not None:
        recs = [r for r in recs if sc.allows(r["hostname"])]
        if not recs:
            raise HTTPException(400, "no inventory host in your team scope")
    out = svc.scanner.scan_all(recs, probe=body.probe)
    if sc is not None:
        ids = acc.team_ids("act.scan")
        try:
            last = svc.kb._exec("SELECT MAX(id) AS id FROM scan_runs")[0]["id"]
            svc.kb._exec("UPDATE scan_runs SET team_id=? WHERE id=?", (ids[0] if len(ids) == 1 else None, last))
        except Exception:  # noqa: BLE001
            pass
    return out
