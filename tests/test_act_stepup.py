"""WP3: step-up (§10.5, K17): administrators are not exempt, OIDC with an old auth_time is refused, no SMTP -> password (weak)."""
import time

import pytest

from watchover.act.stepup import StepUpError, check_oidc, id_token_claims

ADMIN = ("admin@watchover.local", "Watchover!Admin2026")
PW = "Str0ng!Passw0rd"


@pytest.fixture
def c(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0",
                 "WATCHOVER_SELFMON": "0", "WATCHOVER_PSEUDO_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app(); svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    with TestClient(a) as cl:
        cl.svc = svc
        yield cl


def login(c, email, pw):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def test_administrators_need_step_up(c):
    h = login(c, *ADMIN)
    r = c.put("/api/act/policy", json={"act_mode": "guided"}, headers=h)
    assert r.status_code == 428
    d = r.json()["detail"]
    assert d["code"] == "act_stepup_required" and d["methods"] == ["password"] and d["tr"] and d["en"]
    assert c.post("/api/auth/stepup", json={}, headers=h).json() == {"method": "password", "weak": True}
    r = c.post("/api/auth/stepup/verify", json={"password": "wrong"}, headers=h)
    assert r.status_code == 401 and r.json()["detail"]["code"] == "act_stepup_failed"
    assert c.put("/api/act/policy", json={"act_mode": "guided"}, headers=h).status_code == 428
    ok = c.post("/api/auth/stepup/verify", json={"password": ADMIN[1]}, headers=h).json()
    assert ok["ok"] and ok["method"] == "password" and ok["weak"] is True and ok["valid_until"]
    r = c.put("/api/act/policy", json={"act_mode": "guided"}, headers=h)
    assert r.status_code == 200 and r.json()["status"]["mode"] == "guided"
    rows = c.svc.kb._exec("SELECT method, ok FROM act_stepups ORDER BY id")
    assert [(x["method"], x["ok"]) for x in rows] == [("password", 0), ("password", 1)]
    ev = [e["event"] for e in c.svc.act.audit.events()]
    assert "stepup.failed" in ev and "stepup.ok" in ev and "policy.changed" in ev
    # switching off needs no step-up (the safe direction); an expired step-up is required again for anything else
    c.svc.kb._exec("UPDATE act_stepups SET at='2000-01-01T00:00:00+00:00'")
    assert c.put("/api/act/policy", json={"act_mode": "off"}, headers=h).status_code == 200
    assert c.put("/api/act/policy", json={"act_mode": "guided"}, headers=h).status_code == 428


def test_mode_rules_through_the_api(c):
    h = login(c, *ADMIN)
    c.post("/api/auth/stepup/verify", json={"password": ADMIN[1]}, headers=h)
    r = c.put("/api/act/policy", json={"act_mode": "approve"}, headers=h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "act_risk_unaccepted"
    r = c.put("/api/act/policy", json={"values": {"act_watch_min": 30}}, headers=h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "act_disabled"
    c.svc.users.register("viewer@corp.com", PW, name="v", role="viewer")
    v = login(c, "viewer@corp.com", PW)
    assert c.put("/api/act/policy", json={"act_mode": "guided"}, headers=v).status_code == 403


def test_no_smtp_password_recorded_weak(c):
    c.svc.users.register("op@corp.com", PW, name="op", role="operator")
    user = {"id": c.svc.users.get("op@corp.com")["id"], "email": "op@corp.com"}
    su = c.svc.act.stepup
    assert su.methods(user) == ["password"]
    with pytest.raises(StepUpError) as e:
        su.require(user)
    assert e.value.code == "act_stepup_required"
    res = su.verify_password(user, PW)
    assert res["ok"] and res["weak"] and su.require(user)["weak"] is True
    su.smtp_ok = lambda: True                                      # with SMTP the code by e-mail is the method, no password
    assert su.methods(user) == ["otp"]
    with pytest.raises(StepUpError):
        su.verify_password(user, PW)


def test_otp_step_up(c):
    c.svc.users.register("op2@corp.com", PW, name="op", role="operator")
    user = {"id": c.svc.users.get("op2@corp.com")["id"], "email": "op2@corp.com"}
    su = c.svc.act.stepup
    su.smtp_ok = lambda: True
    code = c.svc.users.otp_issue(user["id"])
    assert su.verify_otp(user, "000000" if code != "000000" else "111111")["ok"] is False
    assert su.verify_otp(user, code)["ok"] is True
    assert su.valid(user)["method"] == "otp"


def test_oidc_old_auth_time_refused(c):
    now = time.time()
    assert check_oidc({"email": "Ayse@corp.com", "auth_time": now - 30}, "ayse@corp.com", 120, at=now) == (True, "")
    assert check_oidc({"email": "ayse@corp.com", "auth_time": now - 600}, "ayse@corp.com", 120, at=now) == (False, "auth_time_old")
    assert check_oidc({"email": "ayse@corp.com"}, "ayse@corp.com", 120, at=now) == (False, "no_auth_time")
    assert check_oidc({"email": "eve@corp.com", "auth_time": now}, "ayse@corp.com", 120, at=now) == (False, "other_account")
    c.svc.users.sso_login("sso@corp.com", "SSO", "", True, provider="oidc")
    user = {"id": c.svc.users.get("sso@corp.com")["id"], "email": "sso@corp.com"}
    su = c.svc.act.stepup
    assert su.methods(user) == []                                  # provider not configured: nothing to step up with
    with pytest.raises(StepUpError) as e:
        su.require(user)
    assert e.value.code == "act_stepup_unavailable"
    su.sso_ok = lambda provider: provider == "oidc"
    assert su.methods(user) == ["oidc"]
    assert su.verify_oidc(user, {"email": "sso@corp.com", "auth_time": now - 3600})["ok"] is False
    with pytest.raises(StepUpError):
        su.require(user)
    assert su.verify_oidc(user, {"email": "sso@corp.com", "auth_time": now - 5})["ok"] is True
    assert su.require(user)["method"] == "oidc"
    import base64
    import json
    tok = "x." + base64.urlsafe_b64encode(json.dumps({"auth_time": 123}).encode()).decode().rstrip("=") + ".y"
    assert id_token_claims(tok)["auth_time"] == 123


def test_oidc_start_adds_prompt_login(c, monkeypatch):
    from watchover.api.routers import auth as auth_router
    monkeypatch.setattr(auth_router, "_provider", lambda name, cfg: {"issuer": "https://idp", "client_id": "cid", "client_secret": "s"})
    monkeypatch.setattr(auth_router, "_discover", lambda issuer: {"authorization_endpoint": "https://idp/auth"})
    c.svc.users.sso_login("sso2@corp.com", "SSO", "", True, provider="oidc")
    c.svc.act.stepup.sso_ok = lambda provider: True
    tok = auth_router.access_token(c.svc.users.get("sso2@corp.com"))
    h = {"Authorization": f"Bearer {tok}"}
    r = c.post("/api/auth/stepup", json={"next": "/actions"}, headers=h).json()
    assert r["method"] == "oidc" and r["url"].startswith("/api/auth/oidc/oidc/start?")
    loc = c.get(r["url"], follow_redirects=False).headers["location"]
    assert "prompt=login" in loc and "max_age=0" in loc
    plain = c.get("/api/auth/oidc/oidc/start", follow_redirects=False).headers["location"]
    assert "prompt=login" not in plain
