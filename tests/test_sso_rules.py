"""Sign-in through Google / Microsoft / the company's OIDC (v1.58.5): a new account gets the least privilege; one from a public
mail service (gmail.com ...) waits for an administrator; an address the provider did not verify, and the built-in administrator,
never sign in this way; Microsoft only with the company's own tenant ("common" would let any Entra tenant name any e-mail)."""
from watchover import auth
from watchover.api.routers import auth as rauth
from watchover.db import Database


def test_new_accounts_are_viewers_and_public_mail_waits(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    us = auth.Users(Database(str(tmp_path / "k.db"))); us.bootstrap()
    u, why = us.sso_check("ayse@sirket.com.tr", "Ayse", "sirket.com.tr, gmail.com", provider="google")
    assert why == "ok" and u["role"] == "viewer" and us.get("ayse@sirket.com.tr")["status"] == "active"
    u, why = us.sso_check("ali.veli@gmail.com", "Ali", "sirket.com.tr, gmail.com", provider="google")
    assert u is None and why == "pending" and us.get("ali.veli@gmail.com")["status"] == "pending" and us.get("ali.veli@gmail.com")["role"] == "viewer"
    assert us.sso_check("ali.veli@gmail.com", provider="google") == (None, "pending")          # still waiting
    us.set_status(us.get("ali.veli@gmail.com")["id"], "active")                                 # Users page: approved
    assert us.sso_check("ali.veli@gmail.com", provider="google")[1] == "ok"
    assert us.sso_check("eve@other.com", allowed_domains="sirket.com.tr")[1] == "not_allowed"
    assert us.sso_check("mehmet@sirket.com.tr", verified=False) == (None, "unverified") and us.get("mehmet@sirket.com.tr") is None
    assert us.sso_check(auth.INITIAL_EMAIL, provider="google") == (None, "builtin")


def test_microsoft_needs_the_company_tenant():
    cfg = {"auth_microsoft": True, "ms_client_id": "id", "ms_client_secret": "s"}
    for tenant in ("", "common", "organizations", "consumers", "Common"):
        assert rauth._provider("microsoft", dict(cfg, ms_tenant=tenant)) is None, tenant
    p = rauth._provider("microsoft", dict(cfg, ms_tenant="sirket.com.tr"))
    assert p and p["issuer"] == "https://login.microsoftonline.com/sirket.com.tr/v2.0"
