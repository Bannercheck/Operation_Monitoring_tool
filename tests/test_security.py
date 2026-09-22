"""Regression tests for the security review fixes: path traversal, SSRF, must_change, SQLi allowlist, decompression bomb, OIDC redirect."""
import gzip
import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
ADMIN = {"email": "admin@watchover.local", "password": "Watchover!Admin2026"}


@pytest.fixture()
def app(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"),
                 "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from watchover.api import create_app
    return create_app()


def _hdr(app):
    c = TestClient(app)                                        # no context manager: keep the app (and its DB) open for the test
    app.state.services.kb._exec("UPDATE users SET must_change=0 WHERE email=?", (ADMIN["email"],))
    r = c.post("/api/auth/login", json=ADMIN)
    return c, {"Authorization": f"Bearer {r.json()['token']}"}


def test_spa_path_traversal_blocked(app, tmp_path):
    # write the API secret file and a fake web dist, then try to escape it
    home = Path(app.state.services.kb.__dict__.get("url", "")) if False else tmp_path / "home"
    with TestClient(app) as c:
        for attempt in ("/../../pyproject.toml", "/..%2f..%2fpyproject.toml", "/%2e%2e/%2e%2e/pyproject.toml"):
            r = c.get(attempt)
            assert b"[project]" not in r.content and b"tool.setuptools" not in r.content


def test_must_change_blocks_api_until_password_set(app):
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json=ADMIN)
        assert r.status_code == 200
        h = {"Authorization": f"Bearer {r.json()['token']}"}
        # the initial admin has must_change set: every route but the password change is refused
        assert c.get("/api/system/status", headers=h).status_code == 403
        assert c.get("/api/auth/me", headers=h).status_code == 200
        assert c.post("/api/auth/password", json={"current": ADMIN["password"], "new": "Brand!NewPass2026"}, headers=h).status_code == 200
        r2 = c.post("/api/auth/login", json={"email": ADMIN["email"], "password": "Brand!NewPass2026"})
        h2 = {"Authorization": f"Bearer {r2.json()['token']}"}
        assert c.get("/api/system/status", headers=h2).status_code == 200


def test_ssrf_file_scheme_blocked(app):
    c, h = _hdr(app)
    r = c.post("/api/datasets/fetch", headers=h, json={"kind": "http", "url": "file:///etc/passwd"})
    assert r.status_code >= 400 and b"root:" not in r.content
    r2 = c.post("/api/datasets/fetch", headers=h, json={"kind": "http", "url": "http://169.254.169.254/latest/meta-data/"})
    assert r2.status_code >= 400


def test_notify_update_column_allowlist(app):
    from watchover.notify import Notifier
    n = app.state.services.notifier
    rid = n.add_recipient("x", email="x@y.z")
    n.update_recipient(rid, name="ok", **{"email=email, note=(SELECT 1)": "evil"})   # bogus identifier is dropped, no SQL error
    assert n.recipients()[0]["name"] == "ok"


def test_decompression_bomb_refused():
    from watchover.loader import iter_bytes, ArchiveTooBig, MAX_TOTAL_BYTES
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.txt", b"0" * (MAX_TOTAL_BYTES + 1))     # a single member over the ceiling
    with pytest.raises(ArchiveTooBig):
        list(iter_bytes("bomb.zip", buf.getvalue()))


def test_oidc_next_is_same_origin_only():
    from watchover.api.routers.auth import _safe_next
    assert _safe_next("https://evil.example/") == "/"
    assert _safe_next("//evil.example") == "/"
    assert _safe_next("/dashboard") == "/dashboard"
    assert _safe_next("") == "/"
