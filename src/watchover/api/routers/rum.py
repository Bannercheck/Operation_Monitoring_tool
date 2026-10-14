"""User experience (web and mobile): overview (sessions, crash-free and error-free rates, Web Vitals p75, app start, slow steps,
top errors), session list and one session's step-by-step timeline with the backend trace of each step; app keys for the
sensors; an audited lookup that turns a known user id into its pseudonym so support can find that person's sessions without
the database ever holding the id itself."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ... import settings as wo_settings
from ...privacy import pseudonym
from ..security import require, services, user_scope

router = APIRouter(prefix="/rum", tags=["rum"])


class AppIn(BaseModel):
    name: str
    platform: str = "web"
    origins: str = ""
    sample_pct: int = 100
    note: str = ""


class AppPatch(BaseModel):
    origins: str | None = None
    sample_pct: int | None = None
    status: str | None = None
    note: str | None = None


class LookupIn(BaseModel):
    user: str
    reason: str = ""


def _apps_ok(svc, sc):
    return None if sc is None else {a["name"] for a in svc.rum.apps() if sc.allows_service(a["name"])}


@router.get("/overview")
def overview(minutes: int = 1440, app: str | None = None, user=Depends(require("page.rum")), svc=Depends(services), sc=Depends(user_scope)):
    ok = _apps_ok(svc, sc)
    if ok is None:
        return svc.rum.overview(minutes or None, app or None)
    if app and app not in ok:
        raise HTTPException(404, "app not found")
    if app:
        return svc.rum.overview(minutes or None, app)
    parts = [svc.rum.overview(minutes or None, a) for a in sorted(ok)]
    merged = {"apps": [], "vitals": {}, "app_start": {}, "slow_steps": [], "errors": [], "thresholds": parts[0]["thresholds"] if parts else {}}
    for p_ in parts:
        merged["apps"] += p_["apps"]; merged["vitals"].update(p_["vitals"]); merged["app_start"].update(p_["app_start"])
        merged["slow_steps"] += p_["slow_steps"]; merged["errors"] += p_["errors"]
    merged["slow_steps"] = sorted(merged["slow_steps"], key=lambda x: -(x["p75"] or 0))[:15]
    merged["errors"] = sorted(merged["errors"], key=lambda x: -x["n"])[:15]
    return merged


@router.get("/sessions")
def sessions(app: str | None = None, platform: str | None = None, user_ref: str | None = None, errors: bool = False, minutes: int = 1440,
             version: str | None = None, limit: int = 100, user=Depends(require("page.rum")), svc=Depends(services), sc=Depends(user_scope)):
    ok = _apps_ok(svc, sc)
    rows = svc.rum.sessions(app or None, platform or None, user_ref or None, errors, minutes or None, version or None, limit if ok is None else 500)
    return rows if ok is None else [r for r in rows if r["app"] in ok][:limit]


@router.get("/sessions/{sid}")
def session(sid: str, user=Depends(require("page.rum")), svc=Depends(services), sc=Depends(user_scope)):
    s = svc.rum.session(sid)
    ok = _apps_ok(svc, sc)
    if s is not None and ok is not None and s["app"] not in ok:
        s = None
    if s is None:
        raise HTTPException(404, "session not found")
    return s


def _receiver_base(request: Request) -> str:
    c = wo_settings.load()
    host = (c.get("public_host") or "").rstrip("/")
    if host:
        return host if host.startswith("http") else f"https://{host}"
    return f"http://{request.url.hostname}:{c.get('live_port', 8600) or 8600}"


@router.get("/apps")
def apps(request: Request, user=Depends(require("page.rum")), svc=Depends(services), sc=Depends(user_scope)):
    base = _receiver_base(request)
    ok = _apps_ok(svc, sc)
    return {"apps": [a for a in svc.rum.apps() if ok is None or a["name"] in ok], "receiver": base}


@router.post("/apps", status_code=201)
def create_app(body: AppIn, user=Depends(require("act.rum")), svc=Depends(services)):
    return svc.rum.create_app(body.name, body.platform, body.origins, body.sample_pct, body.note)


@router.patch("/apps/{aid}")
def patch_app(aid: int, body: AppPatch, user=Depends(require("act.rum")), svc=Depends(services)):
    return svc.rum.update_app(aid, **body.model_dump())


@router.post("/apps/{aid}/rotate")
def rotate_app(aid: int, user=Depends(require("act.rum")), svc=Depends(services)):
    return svc.rum.rotate_key(aid)


@router.delete("/apps/{aid}", status_code=204)
def delete_app(aid: int, user=Depends(require("act.rum")), svc=Depends(services)):
    svc.rum.delete_app(aid)


@router.post("/lookup")
def lookup(body: LookupIn, user=Depends(require("act.rum")), svc=Depends(services)):
    """A known user id -> its pseudonym (to search that person's sessions). Recorded in the security log with the reason."""
    if not body.user.strip():
        raise HTTPException(400, "user required")
    if len(body.reason.strip()) < 5:
        raise HTTPException(400, "a reason is required (recorded in the security log)")
    ref = pseudonym(body.user)
    svc.users._event(user.get("email", ""), "rum.user_lookup", True, f"{ref} · {body.reason.strip()[:150]}")
    return {"user_ref": ref, "sessions": len(svc.rum.sessions(user_ref=ref, minutes=None, limit=500))}
