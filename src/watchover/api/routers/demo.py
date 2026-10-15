"""Demo mode (WATCHOVER_DEMO) and the clean-up of simulator-made data (watchover.demo): installation administrators only."""
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
    """Demo mode and what a clean-up would delete now (nothing is deleted)."""
    return {"demo": wo_demo.enabled(), "simulator": svc.sim_stop is not None, **wo_demo.clean(svc, dry_run=True)}


@router.post("/demo-clean")
def demo_clean(user=Depends(install_admin), svc=Depends(services)):
    """Delete only what the simulator made (marked rows; host-keyed rows of simulator-only host names). Audited."""
    out = wo_demo.clean(svc)
    try:
        svc.users._event(user.get("email", ""), "demo-clean", True, f"demo data removed: {out['total']} ({json.dumps(out['counts'], sort_keys=True)})")
    except Exception:  # noqa: BLE001 - the audit log never blocks the clean-up
        pass
    return out
