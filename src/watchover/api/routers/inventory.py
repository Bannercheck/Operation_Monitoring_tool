from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse

from ..security import require, services, user_scope

router = APIRouter(prefix="/inventory", tags=["inventory"])


@router.get("")
def list_hosts(q: str = "", user=Depends(require("page.inv")), svc=Depends(services), sc=Depends(user_scope)):
    rows = svc.inventory.list(q)
    return rows if sc is None else [r for r in rows if sc.allows(r["hostname"])]


@router.get("/stats")
def stats(user=Depends(require("page.inv")), svc=Depends(services)):
    return svc.inventory.stats(svc.live.hosts())


@router.get("/discovered")
def discovered(user=Depends(require("page.inv")), svc=Depends(services), sc=Depends(user_scope)):
    rows = svc.inventory.discovered(svc.live.hosts(), svc.agents.list())
    return rows if sc is None else [r for r in rows if sc.allows(r["hostname"])]     # a team sees only its own servers, as on the list


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


@router.post("/promote")
def promote(body: dict, user=Depends(require("act.inventory")), svc=Depends(services), sc=Depends(user_scope)):
    """Bulk promote of discovered hosts ({"hosts": [{hostname, env, ip, dc}], "criticality"}): one call per pool or selection.
    A team-scoped account promotes only hosts of its own scope (the list it was shown)."""
    hosts = body.get("hosts") or []
    if sc is not None and isinstance(hosts, list) and any(not sc.allows(str((x or {}).get("hostname") or "").strip().lower()) for x in hosts if isinstance(x, dict)):
        raise HTTPException(403, "host outside your team scope")
    return {"added": svc.inventory.promote(hosts, str(body.get("criticality") or "normal"))}


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
