"""Shared pieces of the /api/act/* routers (docs/design/action-agent.md §12): the error shape
`{"detail": {"code", "tr", "en", …}}` (C36), the act_mode = off guard and the step-up check."""
from __future__ import annotations

from fastapi import Depends, HTTPException

from ..security import current_user, services


def err(status: int, code: str, tr: str, en: str, **extra) -> HTTPException:
    return HTTPException(status, {"code": code, "tr": tr, "en": en, **extra})


def lift(e: Exception) -> HTTPException:
    """Act-layer errors (PolicyError, KillError, StepUpError, SigningUnavailable, …) carry .status and .detail()."""
    return HTTPException(getattr(e, "status", 400), e.detail())


def act_on(svc=Depends(services)):
    """Dependency: 409 act_disabled while act_mode = off (every /api/act route except GET status and PUT policy)."""
    if not svc.act.enabled:
        raise err(409, "act_disabled", "Otomasyon kapalı (act_mode = off). Sistem › Otomasyon'dan açılabilir.",
                  "Automation is off (act_mode = off). Turn it on under System › Automation.")
    return svc


def stepup(svc, user: dict) -> dict:
    from ...act.stepup import StepUpError
    try:
        return svc.act.stepup.require(user)
    except StepUpError as e:
        raise lift(e)


def me(user: dict = Depends(current_user)) -> dict:
    return user
