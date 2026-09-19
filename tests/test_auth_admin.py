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
        us.register("a@b.co", "short1234")                                    # policy: 10+ characters
    with pytest.raises(ValueError):
        us.register("a@b.co", "onlyletters!!")                                # policy: letters and digits
    first = us.register("Admin@Corp.com", "s3cret-pass-2026", "Ada")
    assert first["email"] == "admin@corp.com" and first["role"] == "admin" and first["pw_hash"].startswith("scrypt$32768$8$2$")
    second = us.register("ops@corp.com", "another-pass-77", allowed_domains="corp.com")
    assert second["role"] == "operator"
    with pytest.raises(ValueError):
        us.register("x@other.com", "another-pass-77", allowed_domains="corp.com, grp.com.tr")
    with pytest.raises(ValueError):
        us.register("ops@corp.com", "another-pass-77")
    assert us.login("admin@corp.com", "s3cret-pass-2026")["role"] == "admin" and us.login("admin@corp.com", "wrong") is None and us.login("ghost@corp.com", "x") is None
    assert us.get("admin@corp.com")["last_login"]
    us.set_role(second["id"], "viewer"); assert us.get("ops@corp.com")["role"] == "viewer"
    with pytest.raises(ValueError):
        us.set_role(first["id"], "viewer")                                   # last admin stays admin
    with pytest.raises(ValueError):
        us.set_status(first["id"], "disabled")
    us.set_status(second["id"], "disabled"); assert us.login("ops@corp.com", "another-pass-77") is None
    us.change_password(second["id"], "new-password-99"); us.set_status(second["id"], "active")
    assert us.login("ops@corp.com", "new-password-99")
    # legacy hash upgrade + lockout + security log
    us.kb._exec("UPDATE users SET pw_hash=? WHERE id=?", (auth._hash("new-password-99", b"0123456789abcdef", 2 ** 14, 8, 1).replace("scrypt$16384$8$1$", "scrypt$"), second["id"]))
    assert us.get("ops@corp.com")["pw_hash"].count("$") == 2 and us.login("ops@corp.com", "new-password-99") and us.get("ops@corp.com")["pw_hash"].startswith("scrypt$32768$")
    for _ in range(auth.LOCK_FAILURES):
        assert us.login("ops@corp.com", "nope-nope-1") is None
    assert us.locked_until("ops@corp.com") is not None and us.login("ops@corp.com", "new-password-99") is None
    ev = us.events(5)
    assert ev[0]["event"] == "login" and not ev[0]["ok"] and ev[0]["detail"] == "locked" and any(e["detail"] == "bad password" for e in ev)
    sso = us.sso_login("jane@corp.com", "Jane", allowed_domains="corp.com", provider="google")
    assert sso["provider"] == "google" and sso["role"] == "operator" and us.login("jane@corp.com", "") is None
    assert us.sso_login("eve@other.com", allowed_domains="corp.com") is None
    assert us.sso_login("bob@corp.com", auto_create=False) is None
    with pytest.raises(ValueError):
        us.delete(first["id"])
    us.delete(second["id"]); assert us.count() == 2


def test_oidc_secrets(tmp_path):
    p = tmp_path / ".streamlit" / "secrets.toml"
    p.parent.mkdir(); p.write_text('[other]\nx = "1"\n\n[auth]\ncookie_secret = "keepme"\nclient_id = "old"\n')
    out = auth.write_secrets(p, "https://wo.corp.com/oauth2callback", {"google": {"client_id": "g-id", "client_secret": "g-sec"},
                                                                       "oidc": {"issuer": "https://login.example.com/realm", "client_id": "cid", "client_secret": 'se"cret'}})
    text = Path(out).read_text()
    assert '[other]' in text and 'x = "1"' in text and text.count("[auth]") == 1 and 'cookie_secret = "keepme"' in text and "[auth.google]" in text and "[auth.oidc]" in text
    assert 'client_secret = "se\\"cret"' in text and 'server_metadata_url = "https://login.example.com/realm/.well-known/openid-configuration"' in text and "old" not in text
    assert auth.GOOGLE_METADATA in text and (Path(out).stat().st_mode & 0o777) == 0o600
    text2 = Path(auth.write_secrets(p, "https://wo.corp.com/oauth2callback", {"google": {"client_id": "g2", "client_secret": "s2"}})).read_text()
    assert "[auth.oidc]" not in text2 and 'client_id = "g2"' in text2 and 'cookie_secret = "keepme"' in text2


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
