"""User experience (web and mobile): overview (sessions, crash-free and error-free rates, Web Vitals p75, app start, slow steps,
top errors), session list and one session's step-by-step timeline with the backend trace of each step; app keys for the
sensors; an audited lookup that turns a known user id into its pseudonym so support can find that person's sessions without
the database ever holding the id itself."""
from __future__ import annotations

import os
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ... import settings as wo_settings
from ...privacy import pseudonym
from ..security import access, pick_team, require, services, user_scope

router = APIRouter(prefix="/rum", tags=["rum"])


class AppIn(BaseModel):
    name: str
    platform: str = "web"
    origins: str = ""
    sample_pct: int = 100
    note: str = ""
    team_id: Optional[int] = None


class AppPatch(BaseModel):
    origins: Optional[str] = None
    sample_pct: Optional[int] = None
    status: Optional[str] = None
    note: Optional[str] = None


class LookupIn(BaseModel):
    user: str
    reason: str = ""


def _service_taken(svc, tid: int, name: str) -> bool:
    """The pin a team's new app gets (service rule 'equals <name>') opens every span and service view of that name: a team may not
    pin a name another team's rule holds or a service already seen on hosts outside the team."""
    from ...teams import _match
    low = name.lower()
    for t in svc.teams.list():
        if t["id"] != tid and any(r.get("field") == "service" and _match(r.get("op", "glob"), r.get("value", ""), name) for r in t["rules"]):
            return True
    hosts = {h for s, hs in svc.traces.service_hosts(10080).items() if (s or "").lower() == low for h in hs if h}
    try:
        hosts |= {o.host for o in svc.live.snapshot() if o.host and (o.service or "").lower() == low}
    except Exception:  # noqa: BLE001
        pass
    return bool(hosts - set(svc.teams.get(tid)["matched"]))


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
    ingest = os.environ.get("WATCHOVER_INGEST_URL", "").strip().rstrip("/")   # the receiver's own public address (OpenShift ingest Route)
    if ingest:
        return ingest
    c = wo_settings.load()
    host = wo_settings.public_url()
    if host:
        return host
    return f"http://{request.url.hostname}:{c.get('live_port', 8600) or 8600}"


@router.get("/apps")
def apps(request: Request, user=Depends(require("page.rum")), svc=Depends(services), sc=Depends(user_scope)):
    base = _receiver_base(request)
    ok = _apps_ok(svc, sc)
    return {"apps": [a for a in svc.rum.apps() if ok is None or a["name"] in ok], "receiver": base}


@router.post("/apps", status_code=201)
def create_app(body: AppIn, acc=Depends(access), user=Depends(require("act.rum")), svc=Depends(services)):
    """A team-scoped account's new app is pinned to its team (a service rule with origin "rum" on the team)."""
    tid = None if acc.wide and body.team_id is None else pick_team(acc, "act.rum", body.team_id)
    if tid is not None and any(a["name"].lower() == body.name.strip().lower() for a in svc.rum.apps()):
        raise ValueError(f"an app named '{body.name.strip()}' exists")
    if tid is not None and _service_taken(svc, tid, re.sub(r"[^\w.\- ]", "", body.name or "").strip()[:60]):
        raise HTTPException(409, "this name is a service that runs outside your team: pick another app name")
    app = svc.rum.create_app(body.name, body.platform, body.origins, body.sample_pct, body.note)
    if tid is not None:
        t = svc.teams.get(tid)
        rule = {"field": "service", "op": "equals", "value": app["name"], "origin": "rum"}
        if not any(r.get("field") == "service" and r.get("value") == app["name"] for r in t["rules"]):
            svc.teams.save(t["name"], t.get("description") or "", list(t["rules"]) + [rule], None, None, tid=tid)
        svc.users._event(user.get("email", ""), "team", True, f"{t['name']}: rum app {app['name']} pinned")
    return app


def _app(svc, acc, aid: int) -> dict:
    """The app, when it is in the caller's act.rum scope (404 for another team's app)."""
    a = next((x for x in svc.rum.apps() if x["id"] == aid), None)
    if a is None:
        raise KeyError(aid)
    sc = acc.scope("act.rum")
    if sc is not None and not sc.allows_service(a["name"]):
        raise KeyError(aid)
    return a


@router.patch("/apps/{aid}")
def patch_app(aid: int, body: AppPatch, acc=Depends(access), user=Depends(require("act.rum")), svc=Depends(services)):
    _app(svc, acc, aid)
    return svc.rum.update_app(aid, **body.model_dump())


@router.post("/apps/{aid}/rotate")
def rotate_app(aid: int, acc=Depends(access), user=Depends(require("act.rum")), svc=Depends(services)):
    _app(svc, acc, aid)
    return svc.rum.rotate_key(aid)


@router.delete("/apps/{aid}", status_code=204)
def delete_app(aid: int, acc=Depends(access), user=Depends(require("act.rum")), svc=Depends(services)):
    _app(svc, acc, aid)
    svc.rum.delete_app(aid)


@router.post("/lookup")
def lookup(body: LookupIn, user=Depends(require("act.rum")), svc=Depends(services), sc=Depends(user_scope)):
    """A known user id -> its pseudonym (to search that person's sessions). Recorded in the security log with the reason."""
    if not body.user.strip():
        raise HTTPException(400, "user required")
    if len(body.reason.strip()) < 5:
        raise HTTPException(400, "a reason is required (recorded in the security log)")
    ref = pseudonym(body.user)
    ok = _apps_ok(svc, sc)                                     # a team looks a user up only inside the apps it sees
    teams = ", ".join(sc.teams) if sc is not None else ""
    svc.users._event(user.get("email", ""), "rum.user_lookup", True, f"{ref} · {body.reason.strip()[:150]}" + (f" · team {teams}" if teams else ""))
    rows = svc.rum.sessions(user_ref=ref, minutes=None, limit=500)
    return {"user_ref": ref, "sessions": len([r for r in rows if ok is None or r.get("app") in ok])}
