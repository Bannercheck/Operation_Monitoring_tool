"""Users and sign-in.

Providers can be combined: local accounts (e-mail + password kept in Watchover), Google accounts and any corporate
OpenID Connect issuer (Entra ID, Okta, Keycloak). Google and OIDC go through Streamlit's native login (Authlib);
Watchover writes the [auth] block of .streamlit/secrets.toml from the settings page.

Password storage: scrypt (N=2^15, r=8, p=2, 32-byte key) with a random 16-byte salt per account, parameters recorded
in the hash string so they can be raised later (the hash is upgraded transparently at the next successful login).
Verification is constant-time; nothing but the hash is stored. Sign-in attempts are recorded in auth_events; five
failures in fifteen minutes lock the account for fifteen minutes. The first local account becomes admin."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
ROLES = ("admin", "operator", "viewer")
PROVIDERS = ("local", "google", "oidc")
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 2}
LOCK_FAILURES, LOCK_MINUTES = 5, 15
REMEMBER_DAYS, REMEMBER_COOKIE = 30, "wo_remember"
OTP_MINUTES, OTP_ATTEMPTS = 5, 5
INITIAL_EMAIL, INITIAL_PASSWORD = "admin@watchover.local", "Watchover!Admin2026"     # built-in administrator; must be changed at first sign-in
APPLE_METADATA = "https://appleid.apple.com/.well-known/openid-configuration"
MS_METADATA = "https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration"


def apple_client_secret(team_id: str, key_id: str, client_id: str, private_key_pem: str, days: int = 180) -> str:
    """Sign in with Apple wants the client secret as an ES256 JWT signed with the .p8 key from the developer portal."""
    from authlib.jose import jwt
    now = int(time.time())
    header = {"alg": "ES256", "kid": key_id.strip()}
    claims = {"iss": team_id.strip(), "iat": now, "exp": now + days * 86400, "aud": "https://appleid.apple.com", "sub": client_id.strip()}
    return jwt.encode(header, claims, private_key_pem.strip().encode("utf-8")).decode("ascii")
MIN_PASSWORD = 10
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
GOOGLE_METADATA = "https://accounts.google.com/.well-known/openid-configuration"


def password_policy(password: str) -> str:
    """'' when acceptable, otherwise the reason (used for both registration and password change)."""
    if len(password) < MIN_PASSWORD:
        return f"password must be at least {MIN_PASSWORD} characters"
    if not re.search(r"[A-Za-zÇĞİÖŞÜçğıöşü]", password) or not re.search(r"\d", password):
        return "password must contain letters and digits"
    if password.lower() in ("watchover12", "password12", "1234567890", "qwerty1234"):
        return "password is too common"
    return ""


def _hash(password: str, salt: bytes | None = None, n: int = SCRYPT["n"], r: int = SCRYPT["r"], p: int = SCRYPT["p"]) -> str:
    salt = salt or os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=128 * 1024 * 1024)
    return f"scrypt${n}${r}${p}${salt.hex()}${h.hex()}"


def _verify(password: str, stored: str) -> tuple[bool, bool]:
    """(matches, needs_rehash). Accepts the legacy 'scrypt$salt$hash' form (N=2^14, r=8, p=1) and upgrades it."""
    try:
        parts = stored.split("$")
        if len(parts) == 3:
            n, r, p, salt, h = 2 ** 14, 8, 1, parts[1], parts[2]
        else:
            n, r, p, salt, h = int(parts[1]), int(parts[2]), int(parts[3]), parts[4], parts[5]
        calc = _hash(password, bytes.fromhex(salt), n, r, p).split("$")[-1]
        ok = hmac.compare_digest(calc, h)
        return ok, ok and (n, r, p) != (SCRYPT["n"], SCRYPT["r"], SCRYPT["p"])
    except (ValueError, IndexError, AttributeError):
        return False, False


class Users:
    def __init__(self, kb):
        self.kb = kb
        pk = kb.pk
        kb._exec(f"""CREATE TABLE IF NOT EXISTS users (id {pk}, email TEXT NOT NULL UNIQUE, name TEXT DEFAULT '', pw_hash TEXT DEFAULT '',
            role TEXT DEFAULT 'operator', status TEXT DEFAULT 'active', provider TEXT DEFAULT 'local', created_at TEXT, last_login TEXT DEFAULT '')""")
        try:
            kb._exec("ALTER TABLE users ADD COLUMN must_change INTEGER DEFAULT 0")     # forced password change at first sign-in
        except Exception:  # noqa: BLE001
            pass
        kb._exec(f"CREATE TABLE IF NOT EXISTS otp_codes (id {pk}, user_id INTEGER NOT NULL UNIQUE, code_hash TEXT NOT NULL, created_at TEXT, expires_at TEXT, attempts INTEGER DEFAULT 0)")
        kb._exec(f"CREATE TABLE IF NOT EXISTS remember_tokens (id {pk}, user_id INTEGER NOT NULL, token_hash TEXT NOT NULL UNIQUE, created_at TEXT, expires_at TEXT, agent TEXT DEFAULT '')")
        kb._exec(f"CREATE TABLE IF NOT EXISTS auth_events (id {pk}, ts TEXT, email TEXT, event TEXT, ok INTEGER, detail TEXT DEFAULT '')")
        kb._exec("CREATE INDEX IF NOT EXISTS auth_events_email ON auth_events(email, ts)")

    # ---- queries
    def bootstrap(self) -> bool:
        """The built-in administrator always exists: created when missing (also next to accounts registered by older builds),
        re-keyed to the fixed initial password while it still carries must_change (never signed in), re-enabled if disabled
        in that state. A changed password is never touched. Returns True when something was written."""
        u = self.get(INITIAL_EMAIL)
        if not u:
            self.kb._exec("INSERT INTO users (email, name, pw_hash, role, status, provider, created_at, must_change) VALUES (?,?,?,?,?,?,?,1)",
                          (INITIAL_EMAIL, "Administrator", _hash(INITIAL_PASSWORD), "admin", "active", "local", datetime.now(UTC).isoformat(timespec="seconds")))
            self._event(INITIAL_EMAIL, "bootstrap", True, "built-in administrator created")
            return True
        if u.get("must_change"):
            changed = False
            if not _verify(INITIAL_PASSWORD, u["pw_hash"])[0]:
                self.kb._exec("UPDATE users SET pw_hash=? WHERE id=?", (_hash(INITIAL_PASSWORD), u["id"])); changed = True
            if u["status"] != "active" or u["role"] != "admin":
                self.kb._exec("UPDATE users SET status='active', role='admin' WHERE id=?", (u["id"],)); changed = True
            if changed:
                self._event(INITIAL_EMAIL, "bootstrap", True, "initial password re-keyed")
            return changed
        return False

    def initial_password_active(self) -> bool:
        """True while the built-in administrator still carries the initial password (shown as a hint on the sign-in page)."""
        rows = self.kb._exec("SELECT must_change FROM users WHERE email=? AND provider='local'", (INITIAL_EMAIL,))
        return bool(rows and rows[0]["must_change"])

    def count(self) -> int:
        return int(self.kb._exec("SELECT COUNT(*) AS n FROM users")[0]["n"])

    def list(self) -> list[dict]:
        return [dict(r) for r in self.kb._exec("SELECT id, email, name, role, status, provider, created_at, last_login FROM users ORDER BY created_at")]

    def get(self, email: str) -> dict | None:
        rows = self.kb._exec("SELECT * FROM users WHERE email=?", (email.strip().lower(),))
        return dict(rows[0]) if rows else None

    def events(self, n: int = 50, email: str | None = None) -> list[dict]:
        if email:
            return [dict(r) for r in self.kb._exec("SELECT * FROM auth_events WHERE email=? ORDER BY id DESC LIMIT ?", (email.lower(), n))]
        return [dict(r) for r in self.kb._exec("SELECT * FROM auth_events ORDER BY id DESC LIMIT ?", (n,))]

    def _event(self, email: str, event: str, ok: bool, detail: str = "") -> None:
        self.kb._exec("INSERT INTO auth_events (ts, email, event, ok, detail) VALUES (?,?,?,?,?)", (datetime.now(UTC).isoformat(timespec="seconds"), email.lower()[:200], event, int(ok), detail[:200]))

    def locked_until(self, email: str) -> datetime | None:
        since = (datetime.now(UTC) - timedelta(minutes=LOCK_MINUTES)).isoformat(timespec="seconds")
        rows = self.kb._exec("SELECT ts, ok FROM auth_events WHERE email=? AND event='login' AND ts>=? ORDER BY id DESC LIMIT ?", (email.lower(), since, LOCK_FAILURES))
        if len(rows) >= LOCK_FAILURES and all(not r["ok"] for r in rows):
            return datetime.fromisoformat(rows[0]["ts"]) + timedelta(minutes=LOCK_MINUTES)
        return None

    def unlock(self, email: str) -> None:
        """Lift the lockout: a successful marker event breaks the run of failed sign-ins the lock is computed from."""
        self._event(email, "login", True, "unlocked by an administrator")

    # ---- lifecycle
    def activate(self, uid: int) -> None:
        self.kb._exec("UPDATE users SET status='active' WHERE id=? AND status='pending'", (uid,))
        self._event(str(uid), "verify", True, "e-mail verified")

    def register(self, email: str, password: str, name: str = "", allowed_domains: str = "", provider: str = "local", status: str = "active", role: str = "") -> dict:
        email = email.strip().lower()
        if not _EMAIL.match(email):
            raise ValueError("invalid e-mail address")
        if provider == "local":
            why = password_policy(password)
            if why:
                raise ValueError(why)
        domains = [d.strip().lower().lstrip("@") for d in (allowed_domains or "").split(",") if d.strip()]
        if domains and email.split("@", 1)[1] not in domains:
            raise ValueError("e-mail domain is not allowed")
        if self.get(email):
            raise ValueError("an account with this e-mail already exists")
        role = "admin" if self.count() == 0 else (role if role in ROLES else "operator")   # self-registration passes "viewer": the least privilege, an admin raises it later
        self.kb._exec("INSERT INTO users (email, name, pw_hash, role, status, provider, created_at) VALUES (?,?,?,?,?,?,?)",
                      (email, name.strip()[:80], _hash(password) if provider == "local" else "", role, status, provider, datetime.now(UTC).isoformat(timespec="seconds")))
        self._event(email, "register", True, provider)
        return self.get(email)

    def login(self, email: str, password: str) -> dict | None:
        """Local sign-in; None on any failure (wrong secret, unknown, disabled, locked). Reasons are in auth_events."""
        email = email.strip().lower()
        if self.locked_until(email):
            self._event(email, "login", False, "locked")
            return None
        u = self.get(email)
        if not u or u["status"] != "active" or u["provider"] != "local":
            _verify(password, _hash("timing-equaliser"))          # same cost whether or not the account exists
            self._event(email, "login", False, "unknown or disabled")
            return None
        ok, rehash = _verify(password, u["pw_hash"])
        if not ok:
            self._event(email, "login", False, "bad password")
            return None
        if rehash:
            self.kb._exec("UPDATE users SET pw_hash=? WHERE id=?", (_hash(password), u["id"]))
        self.kb._exec("UPDATE users SET last_login=? WHERE id=?", (datetime.now(UTC).isoformat(timespec="seconds"), u["id"]))
        self._event(email, "login", True)
        return {k: u[k] for k in ("id", "email", "name", "role", "provider")} | {"must_change": bool(u.get("must_change"))}

    def sso_login(self, email: str, name: str = "", allowed_domains: str = "", auto_create: bool = True, provider: str = "oidc") -> dict | None:
        """An identity the provider vouched for: create on first sight (if allowed), refuse disabled accounts."""
        email = email.strip().lower()
        u = self.get(email)
        if not u:
            if not auto_create:
                self._event(email, "sso", False, "no account and self-registration off")
                return None
            try:
                u = self.register(email, "", name, allowed_domains, provider=provider)
            except ValueError as e:
                self._event(email, "sso", False, str(e))
                return None
        if u["status"] != "active":
            self._event(email, "sso", False, "disabled")
            return None
        self.kb._exec("UPDATE users SET last_login=? WHERE id=?", (datetime.now(UTC).isoformat(timespec="seconds"), u["id"]))
        self._event(email, "sso", True, provider)
        return {k: u[k] for k in ("id", "email", "name", "role", "provider")}

    def set_role(self, uid: int, role: str, valid: set[str] | list[str] | None = None) -> None:
        if role not in (valid if valid is not None else ROLES):
            raise ValueError("unknown role")
        if role != "admin" and self._is_last_admin(uid):
            raise ValueError("the last admin cannot be demoted")
        self.kb._exec("UPDATE users SET role=? WHERE id=?", (role, uid))

    def set_name(self, uid: int, name: str) -> None:
        self.kb._exec("UPDATE users SET name=? WHERE id=?", (name.strip()[:80], uid))

    def set_status(self, uid: int, status: str) -> None:
        if status == "disabled" and self._is_last_admin(uid):
            raise ValueError("the last admin cannot be disabled")
        self.kb._exec("UPDATE users SET status=? WHERE id=?", ("disabled" if status == "disabled" else "active", uid))

    # ---- e-mail MFA: a 6-digit one-time code, 5 minutes, 5 attempts, only its hash stored; administrators are exempt by policy
    def otp_issue(self, uid: int) -> str:
        code = f"{secrets.randbelow(1_000_000):06d}"
        now = datetime.now(UTC)
        self.kb._exec("DELETE FROM otp_codes WHERE user_id=?", (uid,))
        self.kb._exec("INSERT INTO otp_codes (user_id, code_hash, created_at, expires_at, attempts) VALUES (?,?,?,?,0)",
                      (uid, hashlib.sha256(f"{uid}:{code}".encode()).hexdigest(), now.isoformat(timespec="seconds"), (now + timedelta(minutes=OTP_MINUTES)).isoformat(timespec="seconds")))
        return code

    def otp_verify(self, uid: int, code: str) -> tuple[bool, int]:
        """(ok, attempts_left). A wrong code counts; after OTP_ATTEMPTS or after expiry the code is void and a new one is needed."""
        rows = self.kb._exec("SELECT * FROM otp_codes WHERE user_id=?", (uid,))
        if not rows:
            return False, 0
        r = rows[0]
        if r["expires_at"] < datetime.now(UTC).isoformat(timespec="seconds"):
            self.kb._exec("DELETE FROM otp_codes WHERE user_id=?", (uid,)); return False, 0
        if hmac.compare_digest(r["code_hash"], hashlib.sha256(f"{uid}:{code.strip()}".encode()).hexdigest()):
            self.kb._exec("DELETE FROM otp_codes WHERE user_id=?", (uid,))
            self._event(str(uid), "mfa", True)
            return True, 0
        left = OTP_ATTEMPTS - int(r["attempts"]) - 1
        if left <= 0:
            self.kb._exec("DELETE FROM otp_codes WHERE user_id=?", (uid,))
        else:
            self.kb._exec("UPDATE otp_codes SET attempts=attempts+1 WHERE user_id=?", (uid,))
        self._event(str(uid), "mfa", False, f"wrong code, {max(left, 0)} left")
        return False, max(left, 0)

    # ---- "remember me": a random token in a browser cookie, only its sha256 in the database, 30 days, revoked at sign-out
    def remember_issue(self, uid: int, days: int = REMEMBER_DAYS, agent: str = "") -> str:
        tok = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        self.kb._exec("INSERT INTO remember_tokens (user_id, token_hash, created_at, expires_at, agent) VALUES (?,?,?,?,?)",
                      (uid, hashlib.sha256(tok.encode()).hexdigest(), now.isoformat(timespec="seconds"), (now + timedelta(days=days)).isoformat(timespec="seconds"), agent[:120]))
        self.kb._exec("DELETE FROM remember_tokens WHERE expires_at < ?", (now.isoformat(timespec="seconds"),))
        return tok

    def remember_lookup(self, tok: str) -> dict | None:
        """The session user for a valid cookie token (unexpired, account still active), else None."""
        if not tok:
            return None
        rows = self.kb._exec("SELECT r.expires_at, u.* FROM remember_tokens r JOIN users u ON u.id = r.user_id WHERE r.token_hash=?", (hashlib.sha256(tok.encode()).hexdigest(),))
        if not rows:
            return None
        r = rows[0]
        if r["expires_at"] < datetime.now(UTC).isoformat(timespec="seconds") or r["status"] != "active":
            self.remember_revoke(tok)
            return None
        self._event(r["email"], "login", True, "remembered session")
        return {k: r[k] for k in ("id", "email", "name", "role", "provider")} | {"must_change": bool(r.get("must_change"))}

    def remember_revoke(self, tok: str) -> None:
        if tok:
            self.kb._exec("DELETE FROM remember_tokens WHERE token_hash=?", (hashlib.sha256(tok.encode()).hexdigest(),))

    def remember_revoke_all(self, uid: int) -> None:
        self.kb._exec("DELETE FROM remember_tokens WHERE user_id=?", (uid,))

    def change_password(self, uid: int, password: str) -> None:
        why = password_policy(password)
        if why:
            raise ValueError(why)
        self.kb._exec("UPDATE users SET pw_hash=?, must_change=0 WHERE id=?", (_hash(password), uid))
        self.remember_revoke_all(uid)                                      # a new password ends every remembered session
        rows = self.kb._exec("SELECT email FROM users WHERE id=?", (uid,))
        self._event(rows[0]["email"] if rows else str(uid), "password_change", True)

    def delete(self, uid: int) -> None:
        if self._is_last_admin(uid):
            raise ValueError("the last admin cannot be deleted")
        self.kb._exec("DELETE FROM users WHERE id=?", (uid,))

    def _is_last_admin(self, uid: int) -> bool:
        admins = [r for r in self.list() if r["role"] == "admin" and r["status"] == "active"]
        return len(admins) == 1 and admins[0]["id"] == uid


# ---------------------------------------------------------------- Streamlit native login: [auth] in secrets.toml
def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"')


def secrets_toml(redirect_uri: str, providers: dict, cookie_secret: str = "") -> str:
    """[auth] + one [auth.<name>] block per enabled provider. providers: {"google": {client_id, client_secret},
    "oidc": {issuer, client_id, client_secret}}."""
    out = [f'[auth]\nredirect_uri = "{_esc(redirect_uri)}"\ncookie_secret = "{_esc(cookie_secret or secrets.token_urlsafe(32))}"\n']
    g = providers.get("google")
    if g:
        out.append(f'[auth.google]\nclient_id = "{_esc(g["client_id"])}"\nclient_secret = "{_esc(g["client_secret"])}"\nserver_metadata_url = "{GOOGLE_METADATA}"\n')
    a = providers.get("apple")
    if a:
        out.append(f'[auth.apple]\nclient_id = "{_esc(a["client_id"])}"\nclient_secret = "{_esc(a["client_secret"])}"\nserver_metadata_url = "{APPLE_METADATA}"\nclient_kwargs = {{ scope = "openid" }}\n')
    m = providers.get("microsoft")
    if m:
        out.append(f'[auth.microsoft]\nclient_id = "{_esc(m["client_id"])}"\nclient_secret = "{_esc(m["client_secret"])}"\nserver_metadata_url = "{_esc(MS_METADATA.format(tenant=m.get("tenant") or "common"))}"\n')
    o = providers.get("oidc")
    if o:
        meta = o["issuer"].rstrip("/") + "/.well-known/openid-configuration"
        out.append(f'[auth.oidc]\nclient_id = "{_esc(o["client_id"])}"\nclient_secret = "{_esc(o["client_secret"])}"\nserver_metadata_url = "{_esc(meta)}"\n')
    return "\n".join(out)


def write_secrets(path, redirect_uri: str, providers: dict) -> str:
    """Write / replace every [auth*] section, keeping other sections and the existing cookie secret. Mode 0600."""
    from pathlib import Path
    p = Path(path)
    existing = p.read_text(encoding="utf-8") if p.exists() else ""
    keep, skip, cookie = [], False, ""
    for ln in existing.splitlines():
        if ln.strip().startswith("["):
            skip = ln.strip().startswith("[auth")
        if skip and ln.strip().startswith("cookie_secret"):
            cookie = ln.split("=", 1)[1].strip().strip('"')
        if not skip:
            keep.append(ln)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = ("\n".join(keep).rstrip() + "\n\n" if any(k.strip() for k in keep) else "") + secrets_toml(redirect_uri, providers, cookie)
    p.write_text(body, encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return str(p)


def write_oidc_secrets(path, issuer: str, client_id: str, client_secret: str, redirect_uri: str) -> str:
    return write_secrets(path, redirect_uri, {"oidc": {"issuer": issuer, "client_id": client_id, "client_secret": client_secret}})
