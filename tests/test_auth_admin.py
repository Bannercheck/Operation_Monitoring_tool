"""Accounts (register / login / roles / domains / SSO identities), OIDC secrets writer, in-app zip update, simulation purge."""
import io
import zipfile
from pathlib import Path

import pytest

from watchover import admin, auth
from watchover.knowledge import Knowledge
from watchover.live import LiveStore, SIM_HOSTS, simulate_batch, simulate_metrics


def test_users(tmp_path):
    us = auth.Users(Knowledge(str(tmp_path / "k.db")))
    assert us.count() == 0
    with pytest.raises(ValueError):
        us.register("nope", "12345678")
    with pytest.raises(ValueError):
        us.register("a@b.co", "short")
    first = us.register("Admin@Corp.com", "s3cret-pass", "Ada")
    assert first["email"] == "admin@corp.com" and first["role"] == "admin" and first["pw_hash"].startswith("scrypt$")
    second = us.register("ops@corp.com", "another-pass", allowed_domains="corp.com")
    assert second["role"] == "operator"
    with pytest.raises(ValueError):
        us.register("x@other.com", "another-pass", allowed_domains="corp.com, grp.com.tr")
    with pytest.raises(ValueError):
        us.register("ops@corp.com", "another-pass")
    assert us.login("admin@corp.com", "s3cret-pass")["role"] == "admin" and us.login("admin@corp.com", "wrong") is None and us.login("ghost@corp.com", "x") is None
    assert us.get("admin@corp.com")["last_login"]
    us.set_role(second["id"], "viewer"); assert us.get("ops@corp.com")["role"] == "viewer"
    with pytest.raises(ValueError):
        us.set_role(first["id"], "viewer")                                   # last admin stays admin
    with pytest.raises(ValueError):
        us.set_status(first["id"], "disabled")
    us.set_status(second["id"], "disabled"); assert us.login("ops@corp.com", "another-pass") is None
    us.change_password(second["id"], "new-password-9"); us.set_status(second["id"], "active")
    assert us.login("ops@corp.com", "new-password-9")
    sso = us.sso_login("jane@corp.com", "Jane", allowed_domains="corp.com")
    assert sso["provider"] == "oidc" and sso["role"] == "operator" and us.login("jane@corp.com", "") is None
    assert us.sso_login("bob@corp.com", auto_create=False) is None
    with pytest.raises(ValueError):
        us.delete(first["id"])
    us.delete(second["id"]); assert us.count() == 2


def test_oidc_secrets(tmp_path):
    p = tmp_path / ".streamlit" / "secrets.toml"
    p.parent.mkdir(); p.write_text('[other]\nx = "1"\n\n[auth]\ncookie_secret = "keepme"\nclient_id = "old"\n')
    out = auth.write_oidc_secrets(p, "https://login.example.com/realm", "cid", 'se"cret', "https://wo.corp.com/oauth2callback")
    text = Path(out).read_text()
    assert '[other]' in text and 'x = "1"' in text and text.count("[auth]") == 1 and 'cookie_secret = "keepme"' in text
    assert 'client_secret = "se\\"cret"' in text and 'server_metadata_url = "https://login.example.com/realm/.well-known/openid-configuration"' in text and "old" not in text


def test_apply_zip_and_version(tmp_path):
    dest = tmp_path / "app"; (dest / "data").mkdir(parents=True); (dest / "data" / "config.json").write_text("{}"); (dest / ".env").write_text("SECRET=1")
    (dest / "app.py").write_text("old")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hackathon/app.py", "new"); z.writestr("hackathon/src/watchover/analysis.py", "x"); z.writestr("hackathon/data/config.json", "{\"hacked\":1}")
        z.writestr("hackathon/.env", "SECRET=evil"); z.writestr("hackathon/../escape.txt", "no")
    ok, msg = admin.apply_zip(buf.getvalue(), dest)
    assert ok and "2 files" in msg and (dest / "app.py").read_text() == "new" and (dest / "src" / "watchover" / "analysis.py").exists()
    assert (dest / "data" / "config.json").read_text() == "{}" and (dest / ".env").read_text() == "SECRET=1" and not (tmp_path / "escape.txt").exists()
    assert admin.apply_zip(b"garbage", dest) == (False, "not a zip file")
    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w") as z:
        z.writestr("readme.txt", "x")
    assert not admin.apply_zip(buf2.getvalue(), dest)[0]
    ok, msg = admin.git_update(dest); assert not ok and "git" in msg
    vi = admin.version_info()
    assert vi["python"] and vi["streamlit"] and vi["uptime_s"] >= 0
    kb = Knowledge(str(tmp_path / "k.db"))
    info = admin.db_info(kb); assert "lessons" in info["tables"] and info["size_mb"] is not None and admin.vacuum(kb) == "ok"


def test_simulation_purge():
    import random
    store = LiveStore(); rng = random.Random(1); ms = {}
    store.ingest("sim.jsonl", simulate_batch(rng, 10, False), "simulator"); store.ingest("metrics.jsonl", simulate_metrics(rng, ms, False), "simulator")
    store.ingest("real.jsonl", b'{"level":"error","msg":"x","host":"real-01"}\n', "agent-a")
    assert len(store.snapshot()) == 11 and store.metrics and "simulator" in store.agents
    n = store.purge("simulator", tuple(SIM_HOSTS))
    assert n == 10 and [o.host for o in store.snapshot()] == ["real-01"] and not store.metrics and "simulator" not in store.agents and "agent-a" in store.agents
