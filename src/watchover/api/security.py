"""Bearer tokens (HS256 JWT, standard library only) and permission dependencies."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException, Request

from .. import settings as wo_settings

ACCESS_HOURS = 12
MFA_MINUTES = 10


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def secret() -> bytes:
    """WATCHOVER_API_SECRET, else a key file next to the data (created once, 0600)."""
    env = os.environ.get("WATCHOVER_API_SECRET")
    if env:
        return env.encode()
    p = Path(wo_settings.home()) / ".api.key"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(secrets.token_hex(32), encoding="utf-8")
        try:
            p.chmod(0o600)
        except OSError:
            pass
    return p.read_text(encoding="utf-8").strip().encode()


def issue(claims: dict, minutes: float) -> str:
    t = time.time()
    now = int(t)
    body = {**claims, "iat": now, "iatms": int(t * 1000), "exp": now + int(minutes * 60)}   # iatms: a sign-out ends the tokens before it
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps(body, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret(), f"{head}.{payload}".encode(), hashlib.sha256).digest())
    return f"{head}.{payload}.{sig}"


def verify(token: str) -> dict:
    try:
        head, payload, sig = token.split(".")
    except ValueError:
        raise HTTPException(401, "malformed token")
    good = _b64(hmac.new(secret(), f"{head}.{payload}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(good, sig):
        raise HTTPException(401, "bad signature")
    claims = json.loads(_unb64(payload))
    if claims.get("exp", 0) < time.time():
        raise HTTPException(401, "token expired")
    return claims


def access_token(user: dict) -> str:
    return issue({"sub": int(user["id"]), "email": user["email"], "role": user.get("role", ""), "name": user.get("name", ""), "typ": "access"}, ACCESS_HOURS * 60)


def mfa_token(user: dict) -> str:
    return issue({"sub": int(user["id"]), "email": user["email"], "typ": "mfa"}, MFA_MINUTES)


def services(request: Request):
    return request.app.state.services


def current_user(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    claims = verify(auth[7:].strip())
    if claims.get("typ") != "access":
        raise HTTPException(401, "not an access token")
    svc = request.app.state.services
    u = svc.users.get(claims["email"])
    if not u or u.get("status") != "active" or int(u["id"]) != int(claims.get("sub") or 0):
        raise HTTPException(401, "account disabled or removed")      # (a re-created account with the same e-mail is another account)
    if int(claims.get("iatms") or int(claims.get("iat") or 0) * 1000) < int(u.get("sessions_after") or 0):
        raise HTTPException(401, "session ended")                     # signed out, password changed or account disabled since
    if u.get("must_change"):                                  # a never-changed initial account can only change its own password
        path = request.url.path.rstrip("/")
        if path not in ("/api/auth/password", "/api/auth/me", "/api/auth/logout"):
            raise HTTPException(403, "password change required")
    return {"id": u["id"], "email": u["email"], "name": u.get("name", ""), "role": u.get("role", ""), "must_change": bool(u.get("must_change"))}


def access_of(request: Request, user: dict):
    """The Access (effective permissions + team scopes) of the signed-in user, once per request."""
    acc = getattr(request.state, "wo_access", None)
    if acc is None or acc.email != (user.get("email") or "").lower():
        acc = request.app.state.services.access_of(user)
        request.state.wo_access = acc
    return acc


def access(request: Request, user: dict = Depends(current_user)):
    """Dependency: the Access of the signed-in user (see watchover.access)."""
    return access_of(request, user)


def user_scope(request: Request, user: dict = Depends(current_user)):
    """Dependency: None when the user sees every host, else the Scope (team hosts / services) to filter by. The scope follows the
    permission the endpoint's `require(...)` checked: a permission from the installation role covers all of the user's teams, one
    from a team role only that team (watchover.access). Without a require: every membership."""
    return access_of(request, user).scope(getattr(request.state, "wo_perm", None))


def require(perm: str):
    """Dependency: the signed-in account must hold `perm` (installation role or any team role; administrators hold everything)."""
    def dep(request: Request, user: dict = Depends(current_user)) -> dict:
        if not access_of(request, user).can(perm):
            raise HTTPException(403, f"permission required: {perm}")
        request.state.wo_perm = perm
        return user
    return dep


def perm_scope(perm: str):
    """Dependency: 403 unless the user holds `perm`; returns the Scope in which `perm` applies (None = every host)."""
    def dep(request: Request, user: dict = Depends(current_user)):
        acc = access_of(request, user)
        if not acc.can(perm):
            raise HTTPException(403, f"permission required: {perm}")
        return acc.scope(perm)
    return dep


def require_install(perm: str):
    """Dependency: `perm` must hold installation-wide (installation role, and admin / scope.all / no team): installation-wide
    objects such as the fleet enrolment key, global policies and alert rules, integration settings."""
    def dep(request: Request, user: dict = Depends(current_user)) -> dict:
        if not access_of(request, user).install(perm):
            raise HTTPException(403, f"installation-wide permission required: {perm}")
        request.state.wo_perm = perm
        return user
    return dep


def pick_team(acc, perm: str, team_id: Optional[int]) -> Optional[int]:
    """Which team a new record belongs to. Installation-wide callers: the given team or None (installation-wide). Team-scoped
    callers: the given team (must be one where `perm` applies, else 403), else their only such team, else 400."""
    if acc.wide:
        if team_id is not None and not acc.can(perm):
            raise HTTPException(403, f"permission required: {perm}")
        return team_id
    ids = acc.team_ids(perm)
    if team_id is not None:
        if team_id not in ids:
            raise HTTPException(403, f"permission required in that team: {perm}")
        return team_id
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise HTTPException(403, f"permission required: {perm}")
    raise HTTPException(400, "team_id required: you hold this permission in several teams")


def check_team(acc, team_id: Optional[int], perm: str, read_perm: Optional[str] = None) -> None:
    """Object-level check before a write on a team-owned record: installation-wide rows need `perm` installation-wide, a team's
    row needs `perm` in that team. A row the user cannot even read answers 404 (no hint that it exists)."""
    if team_id is None:
        if not acc.install(perm):
            if acc.wide or read_perm is None or acc.visible_team(team_id, read_perm):
                raise HTTPException(403, f"installation-wide permission required: {perm}")
            raise KeyError("not found")
        return
    if acc.can_in(team_id, perm):
        return
    if read_perm and acc.visible_team(team_id, read_perm):
        raise HTTPException(403, f"permission required in that team: {perm}")
    raise KeyError("not found")


def host_guard(acc, host: str, perm: str, read_perm: Optional[str] = None) -> None:
    """Object-level check for a host-bound write (or read): the host must be in the scope of `perm`. A host the user cannot even
    see with `read_perm` answers 404, a visible one 403."""
    sc = acc.scope(perm)
    if sc is None or sc.allows(host):
        return
    if read_perm:
        rs = acc.scope(read_perm)
        if rs is None or rs.allows(host):
            raise HTTPException(403, f"permission required on that host: {perm}")
    raise KeyError(host)


def claim_host(svc, acc, user: dict, perm: str, host: str, origin: str, team_id: Optional[int] = None) -> Optional[int]:
    """A write that brings a new host into Watchover (inventory record, import row, promotion, agent): decide the owning team and
    claim the host for it (teams.claim). Installation-wide callers create installation-wide records unless an installation admin
    names a team. Returns the team id (None = installation-wide)."""
    from .. import settings as _s
    admin = "page.teams" in acc.global_perms
    if acc.wide:
        if team_id is None:
            return None
        if not admin:
            raise HTTPException(403, "permission required: page.teams")
        tid = int(team_id)
    else:
        tid = pick_team(acc, perm, team_id)
    t = svc.teams.get(tid)
    was = host in t["hosts"]
    added, rejected = svc.teams.claim(tid, [host], origin, by=user.get("id"), admin=admin, self_claim=bool(_s.load().get("teams_self_claim")))
    if rejected:
        if rejected[0]["reason"] == "other_team":
            raise HTTPException(409, "host belongs to another team")
        raise HTTPException(403, "host is known but unowned: ask an installation admin")
    if added and not was:
        try:
            svc.users._event(user.get("email", ""), "team", True, f"{t['name']}: host claimed {host} ({origin})"[:200])
        except Exception:  # noqa: BLE001
            pass
    return tid
