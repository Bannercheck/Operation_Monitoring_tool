"""The clean-up of what the retired simulator made and the full reset of analysis data (watchover.demo): installation administrators only."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ... import demo as wo_demo
from ..security import access_of, current_user, require, services
from .act_common import stepup

router = APIRouter(prefix="/system", tags=["system"])


def install_admin(request: Request, user: dict = Depends(current_user)) -> dict:
    """The installation administrator role, installation-wide (not a team role, not a delegated permission)."""
    if user.get("role") != "admin" or not access_of(request, user).wide:
        raise HTTPException(403, "installation administrator required")
    return user


@router.get("/demo")
def demo_status(user=Depends(install_admin), svc=Depends(services)):
    """The simulator's state (retired) and what the demo clean-up would delete now, per category (nothing is deleted)."""
    return {**svc.demo_state(), **wo_demo.preview(svc)}


@router.post("/demo-clean")
def demo_clean(user=Depends(install_admin), svc=Depends(services)):
    """Stop the simulator and delete everything it made, in every store (demo.purge; real data stays). Audited."""
    out = wo_demo.purge(svc)
    try:
        svc.users._event(user.get("email", ""), "demo-clean", True, f"demo data removed: {out['total']} ({json.dumps(out['categories'], sort_keys=True)})")
    except Exception:  # noqa: BLE001 - the audit log never blocks the clean-up
        pass
    return out


class ResetIn(BaseModel):
    confirm: str = ""


@router.get("/data-reset")
def data_reset_preview(user=Depends(install_admin), svc=Depends(services)):
    """What the full reset would delete now, per category (nothing is deleted)."""
    return {**svc.reset_all(dry_run=True), "word": wo_demo.RESET_WORD}


@router.post("/data-reset")
def data_reset(body: ResetIn, user=Depends(install_admin), svc=Depends(services)):
    """The full reset (demo.reset_all): every analysis store emptied whoever wrote it; accounts, settings, agents, policies, audit logs,
    operator-defined rows and stored real log lines stay. Needs the typed word and a fresh step-up. Audited."""
    if (body.confirm or "").strip().upper() != wo_demo.RESET_WORD:
        raise HTTPException(400, f"type {wo_demo.RESET_WORD} to confirm the full reset")
    stepup(svc, user)
    return svc.reset_all(by=user.get("email", ""))


@router.post("/cache-clear")
def cache_clear(user=Depends(require("sys.maint")), svc=Depends(services)):
    """Cached computations only (demo.clear_caches): no stored data, no learned state. Every cache refills on its next read."""
    return {"cleared": wo_demo.clear_caches(svc)}
