"""Demo mode (WATCHOVER_DEMO) and the purge of simulator-made data (watchover.demo): installation administrators only."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request

from ... import demo as wo_demo
from ..security import access_of, current_user, services

router = APIRouter(prefix="/system", tags=["system"])


def install_admin(request: Request, user: dict = Depends(current_user)) -> dict:
    """The installation administrator role, installation-wide (not a team role, not a delegated permission)."""
    if user.get("role") != "admin" or not access_of(request, user).wide:
        raise HTTPException(403, "installation administrator required")
    return user


@router.get("/demo")
def demo_status(user=Depends(install_admin), svc=Depends(services)):
    """Demo mode, the simulator's state and what the purge would delete now, per category (nothing is deleted)."""
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
