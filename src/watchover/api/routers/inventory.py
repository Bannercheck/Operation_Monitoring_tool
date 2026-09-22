from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import PlainTextResponse

from ..security import require, services

router = APIRouter(prefix="/inventory", tags=["inventory"])


@router.get("")
def list_hosts(q: str = "", user=Depends(require("page.inv")), svc=Depends(services)):
    return svc.inventory.list(q)


@router.get("/stats")
def stats(user=Depends(require("page.inv")), svc=Depends(services)):
    return svc.inventory.stats(svc.live.hosts())


@router.get("/discovered")
def discovered(user=Depends(require("page.inv")), svc=Depends(services)):
    return svc.inventory.discovered(svc.live.hosts(), svc.agents.list())


@router.get("/export", response_class=PlainTextResponse)
def export(user=Depends(require("page.inv")), svc=Depends(services)):
    return PlainTextResponse(svc.inventory.export_csv().decode("utf-8"), media_type="text/csv")


@router.get("/template", response_class=PlainTextResponse)
def template(user=Depends(require("page.inv"))):
    from ...inventory import Inventory
    return PlainTextResponse(Inventory.csv_template().decode("utf-8"), media_type="text/csv")


@router.post("/import")
async def import_csv(file: UploadFile = File(...), user=Depends(require("act.inventory")), svc=Depends(services)):
    n, errors = svc.inventory.import_csv(await file.read())
    return {"imported": n, "errors": errors}


@router.put("", status_code=201)
def upsert(rec: dict, user=Depends(require("act.inventory")), svc=Depends(services)):
    return svc.inventory.upsert(rec)


@router.get("/{hostname}")
def get(hostname: str, user=Depends(require("page.inv")), svc=Depends(services)):
    r = svc.inventory.get(hostname)
    if not r:
        raise KeyError(hostname)
    return r


@router.delete("/{hostname}")
def delete(hostname: str, user=Depends(require("act.inventory")), svc=Depends(services)):
    svc.inventory.delete(hostname); return {"ok": True}
