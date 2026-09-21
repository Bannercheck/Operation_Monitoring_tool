"""Sign-in: e-mail + password, optional e-mail MFA (everyone but administrators, when SMTP is configured), password change."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ... import auth as wo_auth
from ... import notify as wo_notify
from ... import settings as wo_settings
from ..security import access_token, current_user, mfa_token, services, verify
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
