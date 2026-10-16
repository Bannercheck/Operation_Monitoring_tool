"""Every API operation in the OpenAPI schema is called without a token: only the sign-in endpoints and the health check may answer
anything but 401 / 403 / 405. A new route that forgets its permission dependency fails here (security review, v1.57.1)."""
import re

PUBLIC = {("GET", "/api/health"), ("GET", "/api/auth/options"), ("GET", "/api/auth/providers"), ("POST", "/api/auth/login"),
          ("POST", "/api/auth/mfa"), ("POST", "/api/auth/register"), ("POST", "/api/auth/verify"), ("GET", "/api/auth/oidc/{name}/start"),
          ("GET", "/api/auth/oidc/{name}/callback"), ("GET", "/api/openapi.json"), ("GET", "/api/docs"), ("GET", "/api/redoc")}


def test_no_operation_answers_without_a_token(tmp_path, monkeypatch):
    for k, v in {"WATCHOVER_HOME": str(tmp_path / "home"), "KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"),
                 "PLAYBOOK_DB": str(tmp_path / "pb.db"), "LIVE_SPOOL": str(tmp_path / "l.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    app = create_app()
    with TestClient(app) as c:
        ops = [(m.upper(), p) for p, d in app.openapi()["paths"].items() for m in d]
        assert len(ops) > 400
        leaks = []
        for m, p in ops:
            if (m, p) in PUBLIC:
                continue
            r = c.request(m, re.sub(r"\{[^}]+\}", "1", p), json={})
            if r.status_code not in (401, 403, 405):
                leaks.append(f"{m} {p} -> {r.status_code}")
        assert not leaks, leaks
        bad = c.get("/api/auth/me", headers={"Authorization": "Bearer x.y.z"})
        assert bad.status_code == 401
