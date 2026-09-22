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


class RegisterIn(BaseModel):
    email: str
    password: str
    name: str = ""


class VerifyIn(BaseModel):
    email: str
    code: str


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
        ex = us.get(body.email.strip().lower())
        if ex and ex.get("status") == "pending" and ex.get("provider") == "local":
            raise HTTPException(403, "pending_approval")
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


@router.get("/options")
def options():
    """What the sign-in page offers: local self-registration (needs SMTP for the verification code) and the providers."""
    cfg = wo_settings.load()
    smtp = bool(notify_channels().get("email", {}).get("host"))
    return {"register": bool(cfg.get("auth_self_register", True)), "verify": smtp, "mfa": bool(cfg.get("mfa_email", True))}


@router.post("/register", status_code=202)
def register(body: RegisterIn, svc=Depends(services)):
    """Local account with the least privilege (viewer); an administrator raises the role later on the Users page.
    With SMTP a verification code goes to the e-mail and /auth/verify activates the account; without SMTP it is active at once and signed in."""
    cfg = wo_settings.load()
    if not cfg.get("auth_self_register", True):
        raise HTTPException(403, "self-registration is off")
    us = svc.users
    ex = us.get(body.email.strip().lower())
    smtp = bool(notify_channels().get("email", {}).get("host"))
    if ex and ex["status"] == "pending":
        u = ex
    elif ex:
        raise HTTPException(409, "an account with this e-mail already exists")
    else:
        u = us.register(body.email, body.password, body.name, cfg.get("auth_domains", ""), status="pending" if smtp else "active", role="viewer")
    if not smtp:
        return {"created": True, "token": access_token(u), "user": {k: u[k] for k in ("id", "email", "name", "role")}}
    ok, why = _send_otp(us, u)
    if not ok:
        raise HTTPException(502, why)
    return {"pending": True, "minutes": wo_auth.OTP_MINUTES}


@router.post("/verify")
def verify_registration(body: VerifyIn, svc=Depends(services)):
    us = svc.users
    u = us.get(body.email.strip().lower())
    if not u or u["status"] != "pending":
        raise HTTPException(404, "no pending registration")
    ok, left = us.otp_verify(int(u["id"]), body.code)
    if not ok:
        raise HTTPException(401, f"wrong code, {left} attempts left")
    us.activate(int(u["id"]))
    u = us.get(u["email"])
    return {"token": access_token(u), "user": {k: u[k] for k in ("id", "email", "name", "role")}}


@router.post("/logout")
def logout(user: dict = Depends(current_user)):
    """Tokens are stateless: the client drops it. Remembered sessions of the account are revoked."""
    return {"ok": True}


# ---------------------------------------------------------------- SSO: OpenID Connect (Google, Microsoft, any OIDC issuer)
# Settings keys are the same ones the Streamlit System › Sign-in form writes, so both interfaces share one configuration.
PROVIDERS = {"google": {"issuer": "https://accounts.google.com", "flag": "auth_google", "id": "google_client_id", "secret": "google_client_secret", "label": "Google"},
             "microsoft": {"issuer": "https://login.microsoftonline.com/{tenant}/v2.0", "flag": "auth_microsoft", "id": "ms_client_id", "secret": "ms_client_secret", "label": "Microsoft"},
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
    return {"name": name, "label": (cfg.get("oidc_label") or p["label"]) if name == "oidc" else p["label"], "issuer": issuer.rstrip("/"),
            "client_id": cfg[p["id"]], "client_secret": cfg[p["secret"]]}


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


def _safe_next(nxt: str) -> str:
    """Only a same-origin relative path is a valid return target (blocks open-redirect / token theft via ?next=https://evil)."""
    nxt = nxt or "/"
    if not nxt.startswith("/") or nxt.startswith("//") or nxt.startswith("/\\"):
        return "/"
    return nxt[:200]


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
    state = issue({"typ": "oidc", "p": name, "v": verifier, "n": nonce, "next": _safe_next(next)}, 10)
    q = {"response_type": "code", "client_id": p["client_id"], "redirect_uri": _redirect_uri(request, name), "state": state, "nonce": nonce}
    q.update({"scope": "openid email profile", "code_challenge": challenge, "code_challenge_method": "S256"})
    return RedirectResponse(disc["authorization_endpoint"] + "?" + urllib.parse.urlencode(q), status_code=302)


def _finish(name: str, request: Request, code: str, state: str, error: str, svc):
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
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": _redirect_uri(request, name), "client_id": p["client_id"], "client_secret": p["client_secret"],
            "code_verifier": claims["v"]}
    tok = httpx.post(disc["token_endpoint"], data=data, timeout=15)
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
        ident = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    email = (ident.get("email") or ident.get("preferred_username") or "").lower()
    if not email or (ident.get("nonce") and ident.get("nonce") != claims["n"]):
        return RedirectResponse(f"/login?error={urllib.parse.quote('no e-mail from provider')}", status_code=302)
    u = svc.users.sso_login(email, ident.get("name", ""), cfg.get("auth_domains", ""), bool(cfg.get("auth_self_register", True)), provider=name)
    if not u:
        return RedirectResponse(f"/login?error={urllib.parse.quote('account not allowed')}", status_code=302)
    return RedirectResponse(f"{_safe_next(claims.get('next') or '/')}#sso={access_token(u)}", status_code=302)


@router.get("/oidc/{name}/callback")
def oidc_callback(name: str, request: Request, code: str = "", state: str = "", error: str = "", svc=Depends(services)):
    return _finish(name, request, code, state, error, svc)


@router.post("/oidc/{name}/callback")
async def oidc_callback_post(name: str, request: Request, svc=Depends(services)):
    """Providers that post the code back (response_mode=form_post)."""
    form = await request.form()
    return _finish(name, request, str(form.get("code", "")), str(form.get("state", "")), str(form.get("error", "")), svc)
