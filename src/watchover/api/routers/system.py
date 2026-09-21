from __future__ import annotations

import os

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from fastapi import Request

from ... import admin as wo_admin
from ... import release as wo_release
from ... import settings as wo_settings
from ..security import require, services

AUTH_KEYS = ("auth_google", "google_client_id", "google_client_secret", "auth_microsoft", "ms_tenant", "ms_client_id", "ms_client_secret",
             "auth_apple", "apple_client_id", "apple_team_id", "apple_key_id", "apple_private_key", "auth_oidc", "oidc_issuer", "oidc_client_id", "oidc_client_secret",
             "auth_self_register", "auth_domains", "public_host", "mfa_email")
MASK = "•••"

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


@router.get("/auth")
def auth_settings(request: Request, user=Depends(require("sys.auth"))):
    """Sign-in provider settings; secrets come back masked and stay untouched when the mask is sent back."""
    from .auth import PROVIDERS, _redirect_uri
    cfg = wo_settings.load()
    out = {k: (MASK if k in wo_settings.SECRET_KEYS and cfg.get(k) else cfg.get(k, "")) for k in AUTH_KEYS}
    out["callbacks"] = {n: _redirect_uri(request, n) for n in PROVIDERS}
    return out


@router.put("/auth")
def save_auth_settings(body: dict, user=Depends(require("sys.auth"))):
    cur = wo_settings.load()
    vals = {}
    for k in AUTH_KEYS:
        if k not in body:
            continue
        v = body[k]
        if k in wo_settings.SECRET_KEYS and v == MASK:
            continue
        vals[k] = bool(v) if k.startswith("auth_") and k != "auth_domains" or k == "mfa_email" else str(v or "").strip()
    wo_settings.save(vals)
    return {"ok": True, "changed": sorted(vals)}
