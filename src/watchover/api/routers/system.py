from __future__ import annotations

import os

from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from fastapi import Request

from ... import admin as wo_admin
from ... import release as wo_release
from ... import settings as wo_settings
from ..security import require, services

SETTING_KEYS = ("self_monitor", "learn_min", "alerts_on", "live_port", "live_key", "public_host", "lang", "workspace", "demo_on_start")
CHANNEL_KEYS = ("smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_password", "smtp_from", "smtp_from_name",
                "sms_preset", "sms_url", "sms_method", "sms_auth", "sms_user", "sms_password", "sms_token", "sms_from", "sms_account", "sms_body", "sms_content_type")
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


def _masked(keys: tuple) -> dict:
    cfg = wo_settings.load()
    return {k: (MASK if k in wo_settings.SECRET_KEYS and cfg.get(k) else cfg.get(k, "")) for k in keys}


def _save(keys: tuple, body: dict) -> list:
    vals = {}
    for k in keys:
        if k in body and not (k in wo_settings.SECRET_KEYS and body[k] == MASK):
            vals[k] = body[k]
    wo_settings.save(vals)
    return sorted(vals)


@router.get("/settings")
def get_settings(user=Depends(require("sys.status"))):
    return _masked(SETTING_KEYS)


@router.put("/settings")
def put_settings(body: dict, user=Depends(require("sys.maint")), svc=Depends(services)):
    changed = _save(SETTING_KEYS, body)
    if "alerts_on" in body:
        (svc.alerts.start() if body["alerts_on"] else svc.alerts.stop.set())
    if "self_monitor" in body and svc.selfmon is not None:
        (svc.selfmon.start() if body["self_monitor"] else svc.selfmon.stop())
    return {"ok": True, "changed": changed}


@router.get("/channels")
def get_channels(user=Depends(require("sys.notify"))):
    return _masked(CHANNEL_KEYS)


@router.put("/channels")
def put_channels(body: dict, user=Depends(require("sys.notify"))):
    return {"ok": True, "changed": _save(CHANNEL_KEYS, body)}


@router.get("/update")
def update_info(user=Depends(require("sys.update"))):
    """How this installation updates: in Docker the commands, elsewhere the launcher; plus the release notes."""
    return {"docker": wo_admin.in_docker(), "commands": ["./watchover.sh update", "./watchover.sh backup", "./watchover.sh restore backups/<file>.tgz"] if wo_admin.in_docker() else ["scripts/watchover-launcher.sh update"],
            "releases": wo_release.entries()[:10]}


@router.post("/snapshots/{sid}/rollback")
def rollback(sid: str, code: bool = True, db: bool = True, user=Depends(require("sys.update")), svc=Depends(services)):
    """Bring a snapshot back and restart (Docker's restart policy or the launcher brings the process up again)."""
    ok, msg = wo_admin.rollback(sid, code, db, kb=svc.kb)
    if not ok:
        raise ValueError(msg)
    out = wo_admin.deploy_finish("rollback")
    return {"ok": True, "message": msg, "restart": out.get("restart")}


@router.post("/restart")
def restart(user=Depends(require("sys.update"))):
    return {"restart": wo_admin.restart_app(delay=1.0, hard=True)}


@router.post("/snapshots/prune")
def prune(keep: int = 10, user=Depends(require("sys.update"))):
    return {"removed": wo_admin.prune_versions(keep)}


@router.post("/deploy")
async def deploy(file: UploadFile = File(...), user=Depends(require("sys.update")), svc=Depends(services)):
    """Classic installs: unpack a release zip over the code folder (a snapshot is taken first), then restart. Docker updates the image instead."""
    if wo_admin.in_docker():
        raise ValueError("in Docker the update is the image: ./watchover.sh update")
    wo_admin.snapshot("before zip deploy", kb=svc.kb); wo_admin.prune_versions(12)
    ok, msg = wo_admin.apply_zip(await file.read())
    if not ok:
        raise ValueError(msg)
    out = wo_admin.deploy_finish("zip")
    return {"ok": True, "message": msg, "restart": out.get("restart")}


@router.post("/git-update")
def git_update(branch: str = "", user=Depends(require("sys.update")), svc=Depends(services)):
    if wo_admin.in_docker():
        raise ValueError("in Docker the update is the image: ./watchover.sh update")
    wo_admin.snapshot("before git update", kb=svc.kb); wo_admin.prune_versions(12)
    ok, msg = wo_admin.git_update(branch=branch)
    if not ok:
        raise ValueError(msg)
    out = wo_admin.deploy_finish("git")
    return {"ok": True, "message": msg, "restart": out.get("restart")}


@router.get("/readme", response_class=PlainTextResponse)
def readme(user=Depends(require("page.readme"))):
    p = Path(__file__).resolve().parents[4] / "README.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""
