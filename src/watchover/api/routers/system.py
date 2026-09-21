from __future__ import annotations

import os

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from ... import admin as wo_admin
from ... import release as wo_release
from ..security import require, services

router = APIRouter(prefix="/system", tags=["system"])


class SnapshotIn(BaseModel):
    reason: str = "api"
    code: bool = True
    db: bool = True


@router.get("/status")
def status(user=Depends(require("sys.status")), svc=Depends(services)):
    return svc.status()


@router.get("/releases")
def releases(user=Depends(require("sys.status"))):
    return wo_release.entries() if hasattr(wo_release, "entries") else []


@router.get("/log", response_class=PlainTextResponse)
def log(n: int = 200, user=Depends(require("sys.status"))):
    return wo_admin.tail_log(os.environ.get("WATCHOVER_LOG", ""), n)


@router.post("/vacuum")
def vacuum(user=Depends(require("sys.maint")), svc=Depends(services)):
    wo_admin.snapshot("api maintenance", code=False, kb=svc.kb)
    return {"result": wo_admin.vacuum(svc.kb)}


@router.get("/snapshots")
def snapshots(user=Depends(require("sys.update"))):
    return wo_admin.versions()


@router.post("/snapshots", status_code=201)
def snapshot(body: SnapshotIn, user=Depends(require("sys.update")), svc=Depends(services)):
    return wo_admin.snapshot(body.reason, body.code, body.db, kb=svc.kb)


@router.delete("/snapshots/{sid}")
def delete_snapshot(sid: str, user=Depends(require("sys.update"))):
    wo_admin.delete_version(sid); return {"ok": True}
