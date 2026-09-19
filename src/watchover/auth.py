"""Users and sign-in. Three modes: off (single-operator install), local (e-mail + password, scrypt hashes in the
knowledge database) and oidc (corporate SSO through Streamlit's native OpenID Connect login: Entra ID, Okta, Keycloak,
Google Workspace or any OIDC issuer; the settings page writes the [auth] block of .streamlit/secrets.toml).
The first local account becomes admin; later accounts are operators until an admin changes them."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timezone

UTC = timezone.utc
ROLES = ("admin", "operator", "viewer")
MODES = ("off", "local", "oidc")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def _verify(password: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        return hmac.compare_digest(_hash(password, bytes.fromhex(salt)).split("$")[2], h)
    except (ValueError, AttributeError):
        return False


class Users:
    def __init__(self, kb):
        self.kb = kb
        pk = "SERIAL PRIMARY KEY" if kb.pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        kb._exec(f"""CREATE TABLE IF NOT EXISTS users (id {pk}, email TEXT NOT NULL UNIQUE, name TEXT DEFAULT '', pw_hash TEXT DEFAULT '',
            role TEXT DEFAULT 'operator', status TEXT DEFAULT 'active', provider TEXT DEFAULT 'local', created_at TEXT, last_login TEXT DEFAULT '')""")

    # ---- queries
    def count(self) -> int:
        return int(self.kb._exec("SELECT COUNT(*) AS n FROM users")[0]["n"])

    def list(self) -> list[dict]:
        return [dict(r) for r in self.kb._exec("SELECT id, email, name, role, status, provider, created_at, last_login FROM users ORDER BY created_at")]

    def get(self, email: str) -> dict | None:
        rows = self.kb._exec("SELECT * FROM users WHERE email=?", (email.strip().lower(),))
        return dict(rows[0]) if rows else None

    # ---- lifecycle
    def register(self, email: str, password: str, name: str = "", allowed_domains: str = "", provider: str = "local") -> dict:
        email = email.strip().lower()
        if not _EMAIL.match(email):
            raise ValueError("invalid e-mail address")
        if provider == "local" and len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        domains = [d.strip().lower().lstrip("@") for d in (allowed_domains or "").split(",") if d.strip()]
        if domains and email.split("@", 1)[1] not in domains:
            raise ValueError("e-mail domain is not allowed")
        if self.get(email):
            raise ValueError("an account with this e-mail already exists")
        role = "admin" if self.count() == 0 else "operator"
        self.kb._exec("INSERT INTO users (email, name, pw_hash, role, status, provider, created_at) VALUES (?,?,?,?,?,?,?)",
                      (email, name.strip()[:80], _hash(password) if provider == "local" else "", role, "active", provider, datetime.now(UTC).isoformat(timespec="seconds")))
        return self.get(email)

    def login(self, email: str, password: str) -> dict | None:
        u = self.get(email)
        if not u or u["status"] != "active" or u["provider"] != "local" or not _verify(password, u["pw_hash"]):
            return None
        self.kb._exec("UPDATE users SET last_login=? WHERE id=?", (datetime.now(UTC).isoformat(timespec="seconds"), u["id"]))
        return {k: u[k] for k in ("id", "email", "name", "role", "provider")}

    def sso_login(self, email: str, name: str = "", allowed_domains: str = "", auto_create: bool = True) -> dict | None:
        """An identity the OIDC provider vouched for: create on first sight (if allowed), refuse disabled accounts."""
        u = self.get(email)
        if not u:
            if not auto_create:
                return None
            u = self.register(email, "", name, allowed_domains, provider="oidc")
        if u["status"] != "active":
            return None
        self.kb._exec("UPDATE users SET last_login=? WHERE id=?", (datetime.now(UTC).isoformat(timespec="seconds"), u["id"]))
        return {k: u[k] for k in ("id", "email", "name", "role", "provider")}

    def set_role(self, uid: int, role: str) -> None:
        if role not in ROLES:
            raise ValueError("unknown role")
        if role != "admin" and self._is_last_admin(uid):
            raise ValueError("the last admin cannot be demoted")
        self.kb._exec("UPDATE users SET role=? WHERE id=?", (role, uid))

    def set_status(self, uid: int, status: str) -> None:
        if status == "disabled" and self._is_last_admin(uid):
            raise ValueError("the last admin cannot be disabled")
        self.kb._exec("UPDATE users SET status=? WHERE id=?", ("disabled" if status == "disabled" else "active", uid))

    def change_password(self, uid: int, password: str) -> None:
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        self.kb._exec("UPDATE users SET pw_hash=? WHERE id=?", (_hash(password), uid))

    def delete(self, uid: int) -> None:
        if self._is_last_admin(uid):
            raise ValueError("the last admin cannot be deleted")
        self.kb._exec("DELETE FROM users WHERE id=?", (uid,))

    def _is_last_admin(self, uid: int) -> bool:
        admins = [r for r in self.list() if r["role"] == "admin" and r["status"] == "active"]
        return len(admins) == 1 and admins[0]["id"] == uid


# ---------------------------------------------------------------- OIDC: Streamlit native login needs [auth] in secrets.toml
def oidc_secrets_toml(issuer: str, client_id: str, client_secret: str, redirect_uri: str, cookie_secret: str = "") -> str:
    meta = issuer.rstrip("/") + "/.well-known/openid-configuration"
    esc = lambda v: v.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
    return (f'[auth]\nredirect_uri = "{esc(redirect_uri)}"\ncookie_secret = "{esc(cookie_secret or secrets.token_urlsafe(32))}"\n'
            f'client_id = "{esc(client_id)}"\nclient_secret = "{esc(client_secret)}"\nserver_metadata_url = "{esc(meta)}"\n')


def write_oidc_secrets(path, issuer: str, client_id: str, client_secret: str, redirect_uri: str) -> str:
    """Write / replace the [auth] block, keeping any other sections. Returns the file path."""
    from pathlib import Path
    p = Path(path)
    existing = p.read_text(encoding="utf-8") if p.exists() else ""
    keep, skip, cookie = [], False, ""
    for ln in existing.splitlines():
        if ln.strip().startswith("["):
            skip = ln.strip() == "[auth]"
        if skip and ln.strip().startswith("cookie_secret"):
            cookie = ln.split("=", 1)[1].strip().strip('"')
        if not skip:
            keep.append(ln)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = ("\n".join(keep).rstrip() + "\n\n" if any(k.strip() for k in keep) else "") + oidc_secrets_toml(issuer, client_id, client_secret, redirect_uri, cookie)
    p.write_text(body, encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return str(p)
