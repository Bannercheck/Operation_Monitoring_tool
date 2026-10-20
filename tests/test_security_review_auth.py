"""Sign-in and account findings of the October 2026 security review (v1.60.1): a self-registered or SSO account starts only from an
explicitly allowed company domain (else an administrator approves it); a pending registration cannot be pre-claimed with someone
else's password; sign-in codes cannot be brute-forced by asking for new ones; a delegated user manager hands out and manages only
what they hold; SSO is bound to the provider's subject and links to a password account only with a verified address; the OIDC
callback must come back to the browser that started it."""
import re

import pytest

from watchover import auth as wo_auth
from watchover import settings as wo_settings

PW = "Guclu-Parola-2026"
PW2 = "Baska-Parola-2027"


@pytest.fixture()
def c(tmp_path, monkeypatch):
    for k, v in {"WATCHOVER_HOME": str(tmp_path / "home"), "KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"),
                 "PLAYBOOK_DB": str(tmp_path / "pb.db"), "LIVE_SPOOL": str(tmp_path / "l.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    from watchover.api.routers import auth as rauth
    rauth._FAILS.clear(); rauth._RATE.clear()
    app = create_app(); svc = app.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    sent: list = []
    monkeypatch.setattr(rauth.wo_notify, "send_email", lambda cfg, to, subj, body: sent.append((to[0], body)))
    wo_settings.save({"auth_self_register": True, "smtp_host": "smtp.test", "smtp_port": 25, "smtp_security": "none", "mfa_email": True, "auth_domains": ""})
    with TestClient(app) as cl:
        cl.svc, cl.sent = svc, sent
        yield cl
    rauth._FAILS.clear(); rauth._RATE.clear()


def last_code(c, email):
    return re.search(r"code: (\d{6})", [b for to, b in c.sent if to == email][-1]).group(1)


def test_self_registration_waits_for_approval_unless_the_domain_is_listed(c):
    r = c.post("/api/auth/register", json={"email": "x@attacker.example", "password": PW})
    assert r.status_code == 202 and r.json()["pending"]
    r = c.post("/api/auth/verify", json={"email": "x@attacker.example", "code": last_code(c, "x@attacker.example"), "password": PW})
    assert r.status_code == 200 and r.json() == {"pending": True, "approval": True, "verified": True} and "token" not in r.json()
    assert c.svc.users.get("x@attacker.example")["status"] == "pending"
    assert c.post("/api/auth/login", json={"email": "x@attacker.example", "password": PW}).status_code == 403
    wo_settings.save({"auth_domains": "sirket.com.tr, gmail.com"})
    c.post("/api/auth/register", json={"email": "ayse@sirket.com.tr", "password": PW})
    r = c.post("/api/auth/verify", json={"email": "ayse@sirket.com.tr", "code": last_code(c, "ayse@sirket.com.tr"), "password": PW})
    assert "token" in r.json() and c.svc.users.get("ayse@sirket.com.tr")["role"] == "viewer"
    c.post("/api/auth/register", json={"email": "ali@gmail.com", "password": PW})                 # listed, but a public mail service
    r = c.post("/api/auth/verify", json={"email": "ali@gmail.com", "code": last_code(c, "ali@gmail.com"), "password": PW})
    assert r.json().get("approval") is True


def test_a_pending_registration_cannot_be_pre_claimed(c):
    c.post("/api/auth/register", json={"email": "cfo@corp.example", "password": PW2})          # the attacker, first
    c.post("/api/auth/register", json={"email": "cfo@corp.example", "password": PW})           # the real owner, later
    code = last_code(c, "cfo@corp.example")
    assert c.post("/api/auth/verify", json={"email": "cfo@corp.example", "code": code, "password": PW2}).status_code == 401   # attacker's password: no
    assert c.post("/api/auth/verify", json={"email": "cfo@corp.example", "code": code}).status_code == 401                  # code alone: no
    wo_settings.save({"auth_domains": "corp.example"})
    r = c.post("/api/auth/verify", json={"email": "cfo@corp.example", "code": code, "password": PW})
    assert "token" in r.json()
    assert c.post("/api/auth/login", json={"email": "cfo@corp.example", "password": PW2}).status_code == 401
    assert c.post("/api/auth/login", json={"email": "cfo@corp.example", "password": PW}).status_code == 200


def test_registration_is_throttled_per_address(c):
    codes = [c.post("/api/auth/register", json={"email": f"u{i}@x.example", "password": PW}).status_code for i in range(12)]
    assert codes[:10] == [202] * 10 and codes[-1] == 429


def test_codes_cannot_be_brute_forced_by_asking_for_new_ones(c):
    us = c.svc.users
    u = us.register("op@corp.com", PW, role="operator")
    for _ in range(wo_auth.OTP_LOCK_FAILURES // wo_auth.OTP_ATTEMPTS):
        us.otp_issue(int(u["id"]))
        for _ in range(wo_auth.OTP_ATTEMPTS):
            us.otp_verify(int(u["id"]), "000000")
    code = us.otp_issue(int(u["id"]))
    assert us.otp_locked(int(u["id"])) and us.otp_verify(int(u["id"]), code) == (False, 0)      # even the right code, for an hour
    v = us.register("v@corp.com", PW, role="operator")
    for _ in range(wo_auth.OTP_ISSUE_MAX):
        us.otp_issue(int(v["id"]))
    assert us.otp_throttled(int(v["id"]))
    r = c.post("/api/auth/login", json={"email": "v@corp.com", "password": PW})                # sign-in asks for a code: refused
    assert r.status_code == 429


def test_a_delegated_user_manager_cannot_escalate(c):
    us, roles = c.svc.users, c.svc.roles
    roles.save("helpdesk", ["page.users"], "Helpdesk", "")
    hd = us.register("hd@corp.com", PW); us.activate(int(hd["id"])); us.set_role(int(hd["id"]), "helpdesk", roles.names())
    wo_settings.save({"mfa_email": False})
    tok = c.post("/api/auth/login", json={"email": "hd@corp.com", "password": PW}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert c.post("/api/users", json={"email": "me2@corp.com", "password": PW, "role": "operator"}, headers=h).status_code == 403
    assert c.post("/api/users", json={"email": "me3@corp.com", "password": PW, "role": "helpdesk"}, headers=h).status_code == 201
    adm = us.get(wo_auth.INITIAL_EMAIL)
    op = us.register("op2@corp.com", PW, role="operator"); us.activate(int(op["id"]))
    assert c.delete(f"/api/users/{adm['id']}", headers=h).status_code == 403
    assert c.post(f"/api/users/{op['id']}/password", json={"password": PW2}, headers=h).status_code == 403
    assert c.patch(f"/api/users/{op['id']}", json={"status": "disabled"}, headers=h).status_code == 403
    assert c.post("/api/roles/viewer/reset", headers=h).status_code == 403


def test_sso_is_bound_to_the_subject_and_never_takes_a_password_account_on_e_mail_alone(c):
    us = c.svc.users
    u, why = us.sso_check("jane@corp.com", "Jane", "corp.com", provider="google", subject="google:1", explicit=True)
    assert why == "ok"
    assert us.sso_check("jane@corp.com", provider="google", subject="google:2", explicit=True) == (None, "mismatch")
    loc = us.register("cfo@corp.com", PW, role="operator"); us.activate(int(loc["id"]))
    assert us.sso_check("cfo@corp.com", provider="microsoft", subject="microsoft:9")[1] == "not_allowed"   # no email_verified claim
    assert us.sso_check("cfo@corp.com", provider="google", subject="google:9", explicit=True)[1] == "ok"
    us.register("pend@corp.com", PW2, status="pending", role="viewer")                            # an attacker's unproven registration
    assert us.sso_check("pend@corp.com", "", "corp.com", provider="google", subject="google:5", explicit=True)[1] == "pending"
    assert us.get("pend@corp.com")["pw_hash"] == "" and us.get("pend@corp.com")["provider"] == "google"
    assert us.sso_check("new@corp.com", "", "", provider="google", subject="google:7", explicit=True) == (None, "pending")   # no domain list
