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


def test_login_gate_hides_the_app(tmp_path, monkeypatch):
    """Without a signed-in user nothing but the sign-in page renders; a session user opens the app."""
    from pathlib import Path
    import json
    from streamlit.testing.v1 import AppTest
    home = tmp_path / "home"; home.mkdir()
    (home / "config.json").write_text(json.dumps({"setup_done": True, "lang": "tr", "sim_on": False}))
    for k, v in {"ACTIONS_DB": "a.db", "KNOWLEDGE_DB": "k.db", "PLAYBOOK_DB": "pb.db", "LIVE_SPOOL": "live.jsonl"}.items():
        monkeypatch.setenv(k, str(tmp_path / v))
    monkeypatch.setenv("WATCHOVER_HOME", str(home)); monkeypatch.setenv("LIVE_PORT", "8698"); monkeypatch.delenv("WATCHOVER_SKIP_SETUP", raising=False)
    root = Path(__file__).resolve().parents[1]
    at = AppTest.from_file(str(root / "app.py"), default_timeout=60).run()
    assert not at.exception
    assert not at.sidebar.radio and not at.sidebar.markdown           # no navigation, no page content
    assert at.info and "admin@watchover.local" in at.info[0].value      # first run: the built-in administrator's initial password is shown
    assert not any(x.key == "reg_email" for x in at.text_input)        # self-registration is off by default
    at.session_state["user"] = {"id": 1, "email": "a@corp.com", "name": "Admin", "role": "admin", "provider": "local"}
    at.run()
    assert not at.exception and at.sidebar.radio(key="page")


def test_bootstrap_admin_and_forced_change(tmp_path):
    from watchover.knowledge import Knowledge
    from watchover import auth as wo_auth
    us = wo_auth.Users(Knowledge(str(tmp_path / "k.db")))
    assert us.bootstrap() and not us.bootstrap() and us.count() == 1 and us.initial_password_active()
    u = us.login(wo_auth.INITIAL_EMAIL, wo_auth.INITIAL_PASSWORD)
    assert u and u["role"] == "admin" and u["must_change"] is True
    assert us.get(wo_auth.INITIAL_EMAIL)["pw_hash"].startswith("scrypt$")      # never the clear text
    us.change_password(u["id"], "Yeni-Parola-2026")
    assert not us.initial_password_active() and us.login(wo_auth.INITIAL_EMAIL, "Yeni-Parola-2026")["must_change"] is False
    assert us.login(wo_auth.INITIAL_EMAIL, wo_auth.INITIAL_PASSWORD) is None


def test_vault_encrypts_settings_and_source_secrets(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    from watchover import settings, vault, sources
    from watchover.knowledge import Knowledge
    vault._FERNET = None
    settings.save({"smtp_password": "gizli-parola", "sms_token": "tok", "smtp_host": "smtp.corp"})
    raw = json.loads(settings.path().read_text())
    assert raw["smtp_password"].startswith("enc:v1:") and raw["sms_token"].startswith("enc:v1:") and raw["smtp_host"] == "smtp.corp"
    assert settings.load()["smtp_password"] == "gizli-parola"
    assert vault.status()["key_exists"] and vault.status()["mode"] == "0o600"
    store = sources.SourceStore(Knowledge(str(tmp_path / "k.db")))
    src = store.add(sources.Source(None, "es", "elasticsearch", "http://es:9200", auth="basic", user="u", secret="s3cret"))
    assert src.secret == "s3cret" and store.kb._exec("SELECT secret FROM sources")[0]["secret"].startswith("enc:v1:")
    store.update(src.id, secret="other")
    assert store.get(src.id).secret == "other" and store.kb._exec("SELECT secret FROM sources")[0]["secret"].startswith("enc:v1:")
    vault._FERNET = None


def test_version_snapshot_and_rollback(tmp_path, monkeypatch):
    from watchover import admin
    from watchover.knowledge import Knowledge
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KNOWLEDGE_DB", str(tmp_path / "k.db")); monkeypatch.setenv("ACTIONS_DB", str(tmp_path / "none.db")); monkeypatch.setenv("PLAYBOOK_DB", str(tmp_path / "none2.db"))
    kb = Knowledge(str(tmp_path / "k.db")); kb._exec("CREATE TABLE marker (v TEXT)"); kb._exec("INSERT INTO marker VALUES ('one')")
    root = tmp_path / "code"; (root / "src" / "watchover").mkdir(parents=True); (root / "data").mkdir()
    (root / "app.py").write_text("v1"); (root / "src" / "watchover" / "analysis.py").write_text("a"); (root / "data" / "x").write_text("keep"); (root / ".env").write_text("S=1")
    m = admin.snapshot("before update", root=root)
    assert m["files"] == 2 and m["dbs"] == ["k.db"] and admin.versions()[0]["id"] == m["id"]
    (root / "app.py").write_text("v2"); kb._exec("UPDATE marker SET v='two'")
    ok, msg = admin.rollback(m["id"], root=root)
    assert ok and (root / "app.py").read_text() == "v1" and (root / "data" / "x").read_text() == "keep" and (root / ".env").read_text() == "S=1"
    assert Knowledge(str(tmp_path / "k.db"))._exec("SELECT v FROM marker")[0]["v"] == "one"
    assert len(admin.versions()) == 2 and admin.versions()[0]["reason"].startswith("before rollback")
    admin.delete_version(m["id"]); assert len(admin.versions()) == 1
    assert admin.prune_versions(0) == 1 and admin.versions() == []
