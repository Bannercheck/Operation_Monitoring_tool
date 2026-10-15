"""Step-up verification (docs/design/action-agent.md §10.5, K17): a fresh second proof of identity before four-eyes votes,
promotions to L3/L4, resuming team / global stops, policy and cap changes, template and connector approvals, model promotion
and freeze overrides. Administrators are not exempt.

Methods: e-mail OTP for local accounts when SMTP works; OIDC re-authentication (prompt=login, max_age=0, `auth_time` within
`act_stepup_oidc_max_age_s`) for SSO accounts; password re-entry for local accounts without SMTP (recorded as "password", shown
as weak). A step-up is valid for `act_stepup_min` (10) minutes. Rows: act_stepups(user_id, at, method, ok)."""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Tuple

from .types import now


class StepUpError(Exception):
    def __init__(self, code: str, tr: str, en: str, status: int = 428, **extra):
        super().__init__(en)
        self.code, self.tr, self.en, self.status, self.extra = code, tr, en, status, extra

    def detail(self) -> dict:
        return {"code": self.code, "tr": self.tr, "en": self.en, **self.extra}


def id_token_claims(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def check_oidc(claims: dict, expect_email: str, max_age_s: int = 120, at: Optional[float] = None) -> Tuple[bool, str]:
    """An OIDC re-authentication counts only for the same account and a sign-in at most `max_age_s` seconds old."""
    at = time.time() if at is None else at
    email = (claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    if not email or email != (expect_email or "").strip().lower():
        return False, "other_account"
    try:
        auth_time = float(claims.get("auth_time"))
    except (TypeError, ValueError):
        return False, "no_auth_time"
    if at - auth_time > max_age_s or auth_time - at > 120:
        return False, "auth_time_old"
    return True, ""


class StepUp:
    def __init__(self, db, policy, audit, users=None, smtp_ok: Optional[Callable[[], bool]] = None,
                 sso_ok: Optional[Callable[[str], bool]] = None):
        self.db, self.policy, self.audit, self.users = db, policy, audit, users
        self.smtp_ok = smtp_ok or (lambda: False)
        self.sso_ok = sso_ok or (lambda provider: False)

    # ---------------------------------------------------------------- what this account can do
    def methods(self, user: dict) -> List[str]:
        u = self.users.get(user["email"]) if self.users is not None else user
        provider = (u or {}).get("provider") or "local"
        if provider != "local":
            return ["oidc"] if self.sso_ok(provider) else []
        if self.smtp_ok():
            return ["otp"]
        return ["password"]

    # ---------------------------------------------------------------- record and check
    def record(self, user: dict, method: str, ok: bool) -> dict:
        ts = now()
        with self.db.tx():
            self.db._insert("INSERT INTO act_stepups (user_id, at, method, ok) VALUES (?,?,?,?)", (int(user["id"]), ts, method, 1 if ok else 0))
            self.audit.write("stepup.ok" if ok else "stepup.failed", object_kind="user", object_id="", actor=user["email"],
                             actor_kind="user", payload={"method": method, "weak": method == "password"})
        return {"ok": ok, "method": method, "weak": method == "password", "at": ts,
                "valid_until": (datetime.fromisoformat(ts) + timedelta(minutes=self._minutes())).isoformat(timespec="seconds") if ok else ""}

    def _minutes(self) -> int:
        try:
            return int(self.policy.get("act_stepup_min", 10) or 10)
        except (TypeError, ValueError):
            return 10

    def valid(self, user: dict) -> Optional[dict]:
        since = (datetime.now(timezone.utc) - timedelta(minutes=self._minutes())).isoformat(timespec="seconds")
        rows = self.db._exec("SELECT at, method FROM act_stepups WHERE user_id=? AND ok=1 AND at>=? ORDER BY id DESC LIMIT 1",
                             (int(user["id"]), since))
        return {"at": rows[0]["at"], "method": rows[0]["method"], "weak": rows[0]["method"] == "password"} if rows else None

    def require(self, user: dict) -> dict:
        """Raise 428 unless a step-up of this account is fresh (administrators included)."""
        v = self.valid(user)
        if v:
            return v
        m = self.methods(user)
        if not m:
            raise StepUpError("act_stepup_unavailable", "Bu hesap için adım doğrulaması yöntemi yok",
                              "No step-up method is available for this account", methods=[])
        raise StepUpError("act_stepup_required", "Bu işlem için kimliğinizi yeniden doğrulayın",
                          "Verify your identity again for this action", methods=m)

    # ---------------------------------------------------------------- verification
    def verify_otp(self, user: dict, code: str) -> dict:
        ok, left = self.users.otp_verify(int(user["id"]), code or "")
        res = self.record(user, "otp", ok)
        res["attempts_left"] = left
        return res

    def verify_password(self, user: dict, password: str) -> dict:
        if "password" not in self.methods(user):
            raise StepUpError("act_stepup_unavailable", "Parola ile doğrulama bu hesapta kapalı", "Password step-up is not allowed here", 400)
        ok = self.users.login(user["email"], password or "") is not None
        return self.record(user, "password", ok)

    def verify_oidc(self, user: dict, claims: dict) -> dict:
        ok, why = check_oidc(claims, user["email"], int(self.policy.get("act_stepup_oidc_max_age_s", 120) or 120))
        res = self.record(user, "oidc", ok)
        res["reason"] = why
        return res
