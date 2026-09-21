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
    now = int(time.time())
    body = {**claims, "iat": now, "exp": now + int(minutes * 60)}
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
    if not u or u.get("status") != "active":
        raise HTTPException(401, "account disabled or removed")
    return {"id": u["id"], "email": u["email"], "name": u.get("name", ""), "role": u.get("role", ""), "must_change": bool(u.get("must_change"))}


def require(perm: str):
    """Dependency: the signed-in account must hold `perm` (administrators hold everything)."""
    def dep(request: Request, user: dict = Depends(current_user)) -> dict:
        if not request.app.state.services.roles.can(user["role"], perm):
            raise HTTPException(403, f"permission required: {perm}")
        return user
    return dep
