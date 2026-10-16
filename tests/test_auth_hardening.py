"""Sign-in hardening from the security review (v1.57.1): one scrypt on every path (no timing oracle for unknown e-mails), uniform
answers, an address-wide throttle next to the per-account lockout, e-mail MFA that fails closed, sign-out and password change that
really end sessions, tokens bound to the account id, self-registration without SMTP waiting for an administrator, common passwords."""
import hashlib

import pytest

from watchover import auth as wo_auth

PW = "Guclu-Parola-2026"


@pytest.fixture()
def c(tmp_path, monkeypatch):
    for k, v in {"WATCHOVER_HOME": str(tmp_path / "home"), "KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"),
                 "PLAYBOOK_DB": str(tmp_path / "pb.db"), "LIVE_SPOOL": str(tmp_path / "l.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    from watchover.api.routers import auth as rauth
    rauth._FAILS.clear()
    app = create_app(); svc = app.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    svc.users.register("op@corp.com", PW, role="operator")
    with TestClient(app) as cl:
        cl.svc = svc
        yield cl
    rauth._FAILS.clear()


def login(c, email, pw=PW):
    return c.post("/api/auth/login", json={"email": email, "password": pw})


def scrypt_calls(monkeypatch):
    n = {"n": 0}
    real = hashlib.scrypt

    def counted(*a, **k):
        n["n"] += 1
        return real(*a, **k)
    monkeypatch.setattr(wo_auth.hashlib, "scrypt", counted)
    return n


def test_every_failed_path_costs_one_scrypt(c, monkeypatch):
    wo_auth._dummy_hash()                                     # made once per process
    n = scrypt_calls(monkeypatch)
    for email, pw in (("nobody@corp.com", "x" * 12), ("op@corp.com", "wrong-password-1"), ("op@corp.com", PW)):
        before = n["n"]
        login(c, email, pw)
        assert n["n"] - before == 1, email


def test_answers_do_not_tell_whether_an_account_exists(c):
    a, b = login(c, "nobody@corp.com", "x" * 12), login(c, "op@corp.com", "wrong-password-1")
    assert a.status_code == b.status_code == 401 and a.json() == b.json()
    c.svc.users.register("new@corp.com", PW, status="pending", role="viewer")
    assert login(c, "new@corp.com", "wrong-password-1").json() == a.json()        # pending: only with its password ...
    assert login(c, "new@corp.com", PW).status_code == 403                         # ... is it said to wait for approval
    for _ in range(wo_auth.LOCK_FAILURES):
        login(c, "op@corp.com", "wrong-password-1")
    r = login(c, "op@corp.com", PW)
    assert r.status_code == 429 and "until" not in r.text                         # locked: no unlock time handed out


def test_one_address_cannot_spray_many_accounts(c):
    from watchover.api.routers import auth as rauth
    for i in range(rauth.IP_FAILS):
        assert login(c, f"user{i}@corp.com", "wrong-password-1").status_code == 401
    assert login(c, "op@corp.com", PW).status_code == 429                          # this address is throttled for a while
    rauth._FAILS.clear()
    assert login(c, "op@corp.com", PW).status_code == 200


def test_mfa_fails_closed_and_needs_smtp(c):
    from watchover import settings as wo_settings
    wo_settings.save({"mfa_email": True, "smtp_host": ""})
    r = login(c, "op@corp.com")
    assert r.status_code == 200 and r.json()["mfa"] == "off" and c.get("/api/auth/options").json()["mfa"] is False   # no SMTP: MFA not in force, said so
    wo_settings.save({"smtp_host": "127.0.0.1", "smtp_port": 9, "smtp_security": "none"})                       # SMTP set but unreachable
    r = login(c, "op@corp.com")
    assert r.status_code == 503 and "token" not in r.json()
    from watchover.auth import INITIAL_EMAIL, INITIAL_PASSWORD
    assert "token" in login(c, INITIAL_EMAIL, INITIAL_PASSWORD).json()                                            # admins: exempt unless mfa_admins
    wo_settings.save({"mfa_admins": True})
    assert login(c, INITIAL_EMAIL, INITIAL_PASSWORD).status_code == 503


def test_logout_and_password_change_end_sessions(c):
    import time
    tok = login(c, "op@corp.com").json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert c.get("/api/auth/me", headers=h).status_code == 200
    assert c.post("/api/auth/logout", headers=h).status_code == 200
    r = c.get("/api/auth/me", headers=h)
    assert r.status_code == 401 and "session ended" in r.text
    time.sleep(0.01)
    tok = login(c, "op@corp.com").json()["token"]; h = {"Authorization": f"Bearer {tok}"}
    other = {"Authorization": "Bearer " + login(c, "op@corp.com").json()["token"]}
    time.sleep(0.01)
    r = c.post("/api/auth/password", json={"current": PW, "new": "Yeni-Parola-2027"}, headers=h)
    assert r.status_code == 200 and r.json()["token"]
    assert c.get("/api/auth/me", headers=other).status_code == 401                 # the other browser is signed out
    assert c.get("/api/auth/me", headers={"Authorization": "Bearer " + r.json()["token"]}).status_code == 200


def test_token_is_bound_to_the_account_not_the_email(c):
    tok = login(c, "op@corp.com").json()["token"]
    u = c.svc.users.get("op@corp.com")
    c.svc.kb._exec("DELETE FROM users WHERE id=?", (u["id"],))
    c.svc.users.register("op@corp.com", PW, role="operator")                       # a new account with the same e-mail
    assert c.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_self_registration_without_smtp_waits_for_an_administrator(c):
    from watchover import settings as wo_settings
    wo_settings.save({"auth_self_register": True, "smtp_host": ""})
    r = c.post("/api/auth/register", json={"email": "guest@corp.com", "password": PW, "name": "Guest"})
    assert r.status_code == 202 and r.json() == {"pending": True, "approval": True}
    assert login(c, "guest@corp.com").status_code == 403
    c.svc.users.set_status(c.svc.users.get("guest@corp.com")["id"], "active")
    assert login(c, "guest@corp.com").status_code == 200


@pytest.mark.parametrize("pw", ["aaaaaaaa11", "Password2026", "parola2026", "1111111111a", "Admin123456"])
def test_common_passwords_refused(pw):
    assert wo_auth.password_policy(pw)
