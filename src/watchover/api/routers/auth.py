"""Sign-in: e-mail + password, optional e-mail MFA (everyone but administrators, when SMTP is configured), password change."""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
import urllib.parse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ... import auth as wo_auth
from ... import notify as wo_notify
from ... import settings as wo_settings
from ..security import access_token, current_user, issue, mfa_token, services, verify
from ..services import notify_channels

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str
    password: str


class MfaIn(BaseModel):
    challenge: str
    code: str


class PasswordIn(BaseModel):
    current: str
    new: str


def _send_otp(users, u: dict) -> tuple[bool, str]:
    cfg = notify_channels().get("email", {})
    if not cfg.get("host"):
        return False, "smtp not configured"
    code = users.otp_issue(int(u["id"]))
    try:
        wo_notify.send_email(cfg, [u["email"]], "[Watchover] sign-in code", f"Your Watchover sign-in code: {code} (valid {wo_auth.OTP_MINUTES} minutes)")
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:120]}"


@router.post("/login")
def login(body: LoginIn, svc=Depends(services)):
    """Returns {token} or, when a second factor is needed, {mfa_required, challenge}; the code goes to the account's e-mail."""
    us = svc.users
    until = us.locked_until(body.email)
    if until:
        raise HTTPException(423, f"account locked until {until.isoformat(timespec='seconds')}")
    u = us.login(body.email, body.password)
    if not u:
        raise HTTPException(401, "wrong e-mail or password")
    mfa_on = bool(wo_settings.load().get("mfa_email", True)) and u.get("role") != "admin"
    if mfa_on:
        ok, why = _send_otp(us, u)
        if ok:
            return {"mfa_required": True, "challenge": mfa_token(u), "minutes": wo_auth.OTP_MINUTES}
    return {"token": access_token(u), "user": u, "mfa": "skipped" if mfa_on else "off"}


@router.post("/mfa")
def mfa(body: MfaIn, svc=Depends(services)):
    claims = verify(body.challenge)
    if claims.get("typ") != "mfa":
        raise HTTPException(401, "not an MFA challenge")
    ok, left = svc.users.otp_verify(int(claims["sub"]), body.code)
    if not ok:
        raise HTTPException(401, f"wrong code, {left} attempts left")
    u = svc.users.get(claims["email"])
    return {"token": access_token(u), "user": {k: u[k] for k in ("id", "email", "name", "role")}}


@router.get("/me")
def me(user: dict = Depends(current_user), svc=Depends(services)):
    return {**user, "permissions": sorted(svc.roles.perms(user["role"]))}


@router.post("/password")
def change_password(body: PasswordIn, user: dict = Depends(current_user), svc=Depends(services)):
    if not svc.users.login(user["email"], body.current):
        raise HTTPException(401, "current password is wrong")
    why = wo_auth.password_policy(body.new)
    if why:
        raise HTTPException(400, why)
    svc.users.change_password(int(user["id"]), body.new)
    return {"ok": True}


@router.post("/logout")
def logout(user: dict = Depends(current_user)):
    """Tokens are stateless: the client drops it. Remembered sessions of the account are revoked."""
    return {"ok": True}


# ---------------------------------------------------------------- SSO: OpenID Connect (Google, Microsoft, any OIDC issuer), authorization code + PKCE
PROVIDERS = {"google": {"issuer": "https://accounts.google.com", "id": "google_client_id", "secret": "google_client_secret", "label": "Google"},
             "microsoft": {"issuer": "https://login.microsoftonline.com/{tenant}/v2.0", "id": "microsoft_client_id", "secret": "microsoft_client_secret", "label": "Microsoft"},
             "oidc": {"issuer": None, "id": "oidc_client_id", "secret": "oidc_client_secret", "label": "SSO"}}
_discovery: dict[str, tuple[float, dict]] = {}


def _provider(name: str, cfg: dict) -> dict | None:
    p = PROVIDERS.get(name)
    if not p or not cfg.get(p["id"]) or not cfg.get(p["secret"]):
        return None
    issuer = p["issuer"] or cfg.get("oidc_issuer", "")
    if name == "microsoft":
        issuer = issuer.format(tenant=cfg.get("microsoft_tenant", "common"))
    if not issuer:
        return None
    return {"name": name, "label": cfg.get("oidc_label") or p["label"] if name == "oidc" else p["label"], "issuer": issuer.rstrip("/"),
            "client_id": cfg[p["id"]], "client_secret": cfg[p["secret"]]}


def _discover(issuer: str) -> dict:
    hit = _discovery.get(issuer)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    r = httpx.get(issuer + "/.well-known/openid-configuration", timeout=10)
    r.raise_for_status()
    _discovery[issuer] = (time.time(), r.json())
    return r.json()


def _redirect_uri(request: Request, name: str) -> str:
    base = (wo_settings.load().get("public_host") or "").rstrip("/") or str(request.base_url).rstrip("/")
    return f"{base}/api/auth/oidc/{name}/callback"


@router.get("/providers")
def providers():
    """Sign-in providers that are configured (System › Sign-in): the sign-in page shows a button per entry."""
    cfg = wo_settings.load()
    return [{"name": p["name"], "label": p["label"]} for p in (_provider(n, cfg) for n in PROVIDERS) if p]


@router.get("/oidc/{name}/start")
def oidc_start(name: str, request: Request, next: str = "/"):
    """Redirects the browser to the provider; state is a short signed token carrying the PKCE verifier and the return path."""
    p = _provider(name, wo_settings.load())
    if not p:
        raise HTTPException(404, "provider not configured")
    disc = _discover(p["issuer"])
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    nonce = secrets.token_urlsafe(16)
    state = issue({"typ": "oidc", "p": name, "v": verifier, "n": nonce, "next": next[:200]}, 10)
    q = {"response_type": "code", "client_id": p["client_id"], "redirect_uri": _redirect_uri(request, name), "scope": "openid email profile",
         "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
    return RedirectResponse(disc["authorization_endpoint"] + "?" + urllib.parse.urlencode(q), status_code=302)


@router.get("/oidc/{name}/callback")
def oidc_callback(name: str, request: Request, code: str = "", state: str = "", error: str = "", svc=Depends(services)):
    """Exchanges the code, reads the identity (userinfo or id_token claims), signs the account in and returns to the app with the token in the URL fragment."""
    if error:
        return RedirectResponse(f"/login?error={urllib.parse.quote(error)}", status_code=302)
    claims = verify(state)
    if claims.get("typ") != "oidc" or claims.get("p") != name:
        raise HTTPException(400, "bad state")
    p = _provider(name, wo_settings.load())
    if not p:
        raise HTTPException(404, "provider not configured")
    disc = _discover(p["issuer"])
    tok = httpx.post(disc["token_endpoint"], data={"grant_type": "authorization_code", "code": code, "redirect_uri": _redirect_uri(request, name),
                                                  "client_id": p["client_id"], "client_secret": p["client_secret"], "code_verifier": claims["v"]}, timeout=15)
    if tok.status_code != 200:
        return RedirectResponse(f"/login?error={urllib.parse.quote('token exchange failed')}", status_code=302)
    t = tok.json()
    ident: dict = {}
    if disc.get("userinfo_endpoint") and t.get("access_token"):
        ui = httpx.get(disc["userinfo_endpoint"], headers={"Authorization": f"Bearer {t['access_token']}"}, timeout=10)
        if ui.status_code == 200:
            ident = ui.json()
    if not ident.get("email") and t.get("id_token"):
        payload = t["id_token"].split(".")[1]
        ident = __import__("json").loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    email = (ident.get("email") or ident.get("preferred_username") or "").lower()
    if not email or (ident.get("nonce") and ident.get("nonce") != claims["n"]):
        return RedirectResponse(f"/login?error={urllib.parse.quote('no e-mail from provider')}", status_code=302)
    cfg = wo_settings.load()
    u = svc.users.sso_login(email, ident.get("name", ""), cfg.get("allowed_domains", ""), bool(cfg.get("sso_auto_create", True)), provider=name)
    if not u:
        return RedirectResponse(f"/login?error={urllib.parse.quote('account not allowed')}", status_code=302)
    return RedirectResponse(f"{claims.get('next') or '/'}#sso={access_token(u)}", status_code=302)
