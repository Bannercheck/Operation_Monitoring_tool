"""Sign-in: e-mail + password, optional e-mail MFA (everyone but administrators, when SMTP is configured), password change."""
from __future__ import annotations

import base64
import hashlib
import json
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


# ---------------------------------------------------------------- SSO: OpenID Connect (Google, Microsoft, Apple, any OIDC issuer)
# Settings keys are the same ones the Streamlit System › Sign-in form writes, so both interfaces share one configuration.
PROVIDERS = {"google": {"issuer": "https://accounts.google.com", "flag": "auth_google", "id": "google_client_id", "secret": "google_client_secret", "label": "Google"},
             "microsoft": {"issuer": "https://login.microsoftonline.com/{tenant}/v2.0", "flag": "auth_microsoft", "id": "ms_client_id", "secret": "ms_client_secret", "label": "Microsoft"},
             "apple": {"issuer": "https://appleid.apple.com", "flag": "auth_apple", "id": "apple_client_id", "secret": "apple_private_key", "label": "Apple"},
             "oidc": {"issuer": None, "flag": "auth_oidc", "id": "oidc_client_id", "secret": "oidc_client_secret", "label": "SSO"}}
_discovery: dict[str, tuple[float, dict]] = {}


def _provider(name: str, cfg: dict) -> dict | None:
    """The provider's live settings, or None when it is switched off or incomplete."""
    p = PROVIDERS.get(name)
    if not p or not cfg.get(p["flag"]) or not cfg.get(p["id"]) or not cfg.get(p["secret"]):
        return None
    issuer = p["issuer"] or cfg.get("oidc_issuer", "")
    if name == "microsoft":
        issuer = issuer.format(tenant=cfg.get("ms_tenant") or "common")
    if not issuer:
        return None
    if name == "apple":
        if not (cfg.get("apple_team_id") and cfg.get("apple_key_id")):
            return None
        secret = wo_auth.apple_client_secret(cfg["apple_team_id"], cfg["apple_key_id"], cfg["apple_client_id"], cfg["apple_private_key"])
    else:
        secret = cfg[p["secret"]]
    return {"name": name, "label": (cfg.get("oidc_label") or p["label"]) if name == "oidc" else p["label"], "issuer": issuer.rstrip("/"),
            "client_id": cfg[p["id"]], "client_secret": secret}


def _discover(issuer: str) -> dict:
    hit = _discovery.get(issuer)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    r = httpx.get(issuer + "/.well-known/openid-configuration", timeout=10)
    r.raise_for_status()
    _discovery[issuer] = (time.time(), r.json())
    return r.json()


def _base(request: Request) -> str:
    return (wo_settings.load().get("public_host") or "").rstrip("/") or str(request.base_url).rstrip("/")


def _redirect_uri(request: Request, name: str) -> str:
    return f"{_base(request)}/api/auth/oidc/{name}/callback"


@router.get("/providers")
def providers(request: Request):
    """Every sign-in provider the sign-in page shows: `configured` says whether its button works (System › Sign-in providers)."""
    cfg = wo_settings.load()
    return [{"name": n, "label": p["label"], "configured": _provider(n, cfg) is not None, "callback": _redirect_uri(request, n)} for n, p in PROVIDERS.items()]


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
    q = {"response_type": "code", "client_id": p["client_id"], "redirect_uri": _redirect_uri(request, name), "state": state, "nonce": nonce}
    if name == "apple":                                              # Apple: name + email come back as a form POST, no PKCE
        q.update({"scope": "name email", "response_mode": "form_post"})
    else:
        q.update({"scope": "openid email profile", "code_challenge": challenge, "code_challenge_method": "S256"})
    return RedirectResponse(disc["authorization_endpoint"] + "?" + urllib.parse.urlencode(q), status_code=302)


def _finish(name: str, request: Request, code: str, state: str, error: str, svc, user_json: str = ""):
    """Exchanges the code, reads the identity (userinfo or id_token claims), signs the account in and returns to the app with the token in the URL fragment."""
    if error:
        return RedirectResponse(f"/login?error={urllib.parse.quote(error)}", status_code=302)
    claims = verify(state)
    if claims.get("typ") != "oidc" or claims.get("p") != name:
        raise HTTPException(400, "bad state")
    cfg = wo_settings.load()
    p = _provider(name, cfg)
    if not p:
        raise HTTPException(404, "provider not configured")
    disc = _discover(p["issuer"])
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": _redirect_uri(request, name), "client_id": p["client_id"], "client_secret": p["client_secret"]}
    if name != "apple":
        data["code_verifier"] = claims["v"]
    tok = httpx.post(disc["token_endpoint"], data=data, timeout=15)
    if tok.status_code != 200:
        return RedirectResponse(f"/login?error={urllib.parse.quote('token exchange failed')}", status_code=302)
    t = tok.json()
    ident: dict = {}
    if disc.get("userinfo_endpoint") and t.get("access_token") and name != "apple":
        ui = httpx.get(disc["userinfo_endpoint"], headers={"Authorization": f"Bearer {t['access_token']}"}, timeout=10)
        if ui.status_code == 200:
            ident = ui.json()
    if not ident.get("email") and t.get("id_token"):
        payload = t["id_token"].split(".")[1]
        ident = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if user_json and not ident.get("name"):                         # Apple sends the name once, in the `user` form field
        try:
            n = json.loads(user_json).get("name", {})
            ident["name"] = " ".join(x for x in (n.get("firstName"), n.get("lastName")) if x)
        except ValueError:
            pass
    email = (ident.get("email") or ident.get("preferred_username") or "").lower()
    if not email or (ident.get("nonce") and ident.get("nonce") != claims["n"]):
        return RedirectResponse(f"/login?error={urllib.parse.quote('no e-mail from provider')}", status_code=302)
    u = svc.users.sso_login(email, ident.get("name", ""), cfg.get("auth_domains", ""), bool(cfg.get("auth_self_register", True)), provider=name)
    if not u:
        return RedirectResponse(f"/login?error={urllib.parse.quote('account not allowed')}", status_code=302)
    return RedirectResponse(f"{claims.get('next') or '/'}#sso={access_token(u)}", status_code=302)


@router.get("/oidc/{name}/callback")
def oidc_callback(name: str, request: Request, code: str = "", state: str = "", error: str = "", svc=Depends(services)):
    return _finish(name, request, code, state, error, svc)


@router.post("/oidc/{name}/callback")
async def oidc_callback_post(name: str, request: Request, svc=Depends(services)):
    """Apple (response_mode=form_post) and any provider that posts the code back."""
    form = await request.form()
    return _finish(name, request, str(form.get("code", "")), str(form.get("state", "")), str(form.get("error", "")), svc, str(form.get("user", "")))
