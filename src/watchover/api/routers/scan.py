"""Vulnerability scanning of inventory hosts: catalog-based advisory match, posture checks and an opt-in reachability probe."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..security import require, services

router = APIRouter(prefix="/scan", tags=["scan"])


class ScanIn(BaseModel):
    probe: bool = False                 # opt-in network reachability pass (needs act.scan); default off, catalog-only


@router.get("/stats")
def stats(user=Depends(require("page.scan")), svc=Depends(services)):
    return svc.scanner.stats()


@router.get("/results")
def results(user=Depends(require("page.scan")), svc=Depends(services)):
    return svc.scanner.results()


@router.get("/results/{hostname}")
def result(hostname: str, user=Depends(require("page.scan")), svc=Depends(services)):
    r = svc.scanner.result(hostname)
    if r is None:
        raise KeyError(hostname)
    return r


@router.get("/runs")
def runs(n: int = 10, user=Depends(require("page.scan")), svc=Depends(services)):
    return svc.scanner.runs(n)


@router.get("/catalog")
def catalog(user=Depends(require("page.scan")), svc=Depends(services)):
    return {"count": len(svc.scanner.catalog), "advisories": svc.scanner.catalog}


@router.post("/catalog/reload")
def reload_catalog(user=Depends(require("act.scan")), svc=Depends(services)):
    return {"count": svc.scanner.reload_catalog()}


@router.post("/host/{hostname}")
def scan_host(hostname: str, body: ScanIn, user=Depends(require("act.scan")), svc=Depends(services)):
    rec = svc.inventory.get(hostname)
    if rec is None:
        raise KeyError(hostname)
    return svc.scanner.scan_host(rec, probe=body.probe)


@router.post("/all")
def scan_all(body: ScanIn, user=Depends(require("act.scan")), svc=Depends(services)):
    return svc.scanner.scan_all(svc.inventory.list(), probe=body.probe)
