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
    assert any(x.key == "login-google" for x in at.button)             # brand buttons are on the page
    assert not any("admin@" in str(x.value) for x in at.info)           # no credential hint (the SMTP note of the sign-up tab is fine)
    assert any(x.key == "reg_email" for x in at.text_input)            # self-registration is on by default (viewer role, admin raises it)
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
    # an untouched administrator from an older build is re-keyed to the fixed initial password
    us.kb._exec("UPDATE users SET pw_hash=? WHERE email=?", (wo_auth._hash("something-else-9"), wo_auth.INITIAL_EMAIL))
    assert us.bootstrap() and us.login(wo_auth.INITIAL_EMAIL, wo_auth.INITIAL_PASSWORD)
    us.change_password(u["id"], "Yeni-Parola-2026")
    assert not us.initial_password_active() and us.login(wo_auth.INITIAL_EMAIL, "Yeni-Parola-2026")["must_change"] is False
    assert us.login(wo_auth.INITIAL_EMAIL, wo_auth.INITIAL_PASSWORD) is None and not us.bootstrap()   # a changed password is never re-keyed
    # an installation whose first account was registered by an older build: the built-in administrator is added next to it
    us2 = wo_auth.Users(Knowledge(str(tmp_path / "k2.db")))
    us2.register("tayfur@sirket.com", "Kurumsal-Parola-2026", "Tayfur")
    assert us2.count() == 1 and us2.bootstrap() and us2.count() == 2
    assert us2.login(wo_auth.INITIAL_EMAIL, wo_auth.INITIAL_PASSWORD)["role"] == "admin" and us2.get("tayfur@sirket.com")["role"] == "admin"
    us2.set_status(us2.get(wo_auth.INITIAL_EMAIL)["id"], "disabled")
    assert us2.bootstrap() and us2.get(wo_auth.INITIAL_EMAIL)["status"] == "active"                    # never signed in: brought back


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


def test_apple_and_microsoft_secrets(tmp_path):
    from watchover import auth as wo_auth
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_key().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode() if hasattr(key, "private_key") else key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    tok = wo_auth.apple_client_secret("TEAM1", "KEY1", "com.corp.watchover", pem)
    from authlib.jose import jwt
    claims = jwt.decode(tok, key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    assert claims["iss"] == "TEAM1" and claims["sub"] == "com.corp.watchover" and claims["aud"] == "https://appleid.apple.com"
    body = wo_auth.secrets_toml("http://localhost:8501/oauth2callback", {"apple": {"client_id": "com.corp.watchover", "client_secret": tok}, "microsoft": {"tenant": "t-1", "client_id": "c", "client_secret": "s"}})
    assert "[auth.apple]" in body and "appleid.apple.com" in body and 'scope = "openid"' in body
    assert "[auth.microsoft]" in body and "login.microsoftonline.com/t-1/v2.0/.well-known" in body


def test_clear_caches_purges_previous_version(tmp_path):
    from watchover import admin
    root = tmp_path / "code"; (root / "src" / "watchover" / "__pycache__").mkdir(parents=True); (root / ".venv" / "lib" / "__pycache__").mkdir(parents=True)
    (root / "src" / "watchover" / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"0"); (root / "old.pyc").write_bytes(b"0"); (root / ".pytest_cache").mkdir()
    (root / ".venv" / "lib" / "__pycache__" / "keep.pyc").write_bytes(b"0")
    out = admin.clear_caches(root)
    assert out["dirs"] >= 2 and out["files"] >= 1
    assert not (root / "src" / "watchover" / "__pycache__").exists() and not (root / "old.pyc").exists() and not (root / ".pytest_cache").exists()
    assert (root / ".venv" / "lib" / "__pycache__" / "keep.pyc").exists()          # the virtual environment is never touched


def test_clear_caches_keeps_llm_models_and_data(tmp_path):
    from watchover import admin
    root = tmp_path / "code"
    for d in ("models/qwen2.5", ".ollama/models/blobs", "data/versions/x", "data/live"):
        (root / d).mkdir(parents=True)
    (root / "models" / "qwen2.5" / "weights.gguf").write_bytes(b"\x00" * 64); (root / ".ollama" / "models" / "blobs" / "sha256-abc").write_bytes(b"\x00" * 64)
    (root / "models" / "qwen2.5" / "__pycache__").mkdir(); (root / "data" / "live" / "events.jsonl").write_text("{}")
    (root / "src").mkdir(); (root / "src" / "__pycache__").mkdir(); (root / "src" / "x.pyc").write_bytes(b"0")
    admin.clear_caches(root)
    assert (root / "models" / "qwen2.5" / "weights.gguf").exists() and (root / ".ollama" / "models" / "blobs" / "sha256-abc").exists()
    assert (root / "models" / "qwen2.5" / "__pycache__").exists() and (root / "data" / "live" / "events.jsonl").exists()   # protected trees stay whole
    assert not (root / "src" / "__pycache__").exists() and not (root / "src" / "x.pyc").exists()


def test_remember_me_tokens(tmp_path):
    from watchover.knowledge import Knowledge
    from watchover import auth as wo_auth
    us = wo_auth.Users(Knowledge(str(tmp_path / "k.db")))
    u = us.register("ops@corp.com", "Parola-2026-x", "Ops")
    tok = us.remember_issue(u["id"], agent="Safari")
    assert len(tok) > 30 and us.kb._exec("SELECT token_hash FROM remember_tokens")[0]["token_hash"] != tok     # only the hash is stored
    r = us.remember_lookup(tok)
    assert r and r["email"] == "ops@corp.com" and r["must_change"] is False
    assert us.remember_lookup("nope") is None and us.remember_lookup("") is None
    us.kb._exec("UPDATE remember_tokens SET expires_at='2000-01-01T00:00:00+00:00'")
    assert us.remember_lookup(tok) is None and not us.kb._exec("SELECT 1 FROM remember_tokens")                 # expired tokens are dropped
    tok2 = us.remember_issue(u["id"])
    us.change_password(u["id"], "Baska-Parola-2026")
    assert us.remember_lookup(tok2) is None                                                                     # a password change ends remembered sessions
    tok3 = us.remember_issue(u["id"]); us.remember_revoke(tok3)
    assert us.remember_lookup(tok3) is None


def test_email_otp_codes(tmp_path):
    from watchover.knowledge import Knowledge
    from watchover import auth as wo_auth
    us = wo_auth.Users(Knowledge(str(tmp_path / "k.db")))
    u = us.register("ops@corp.com", "Parola-2026-x", "Ops")
    code = us.otp_issue(u["id"])
    assert len(code) == 6 and code.isdigit() and us.kb._exec("SELECT code_hash FROM otp_codes")[0]["code_hash"] != code
    assert us.otp_verify(u["id"], "000000" if code != "000000" else "111111") == (False, wo_auth.OTP_ATTEMPTS - 1)
    assert us.otp_verify(u["id"], code) == (True, 0) and not us.kb._exec("SELECT 1 FROM otp_codes")            # single use
    assert us.otp_verify(u["id"], code) == (False, 0)
    code = us.otp_issue(u["id"])
    for _ in range(wo_auth.OTP_ATTEMPTS):
        us.otp_verify(u["id"], "999999" if code != "999999" else "888888")
    assert not us.kb._exec("SELECT 1 FROM otp_codes") and us.otp_verify(u["id"], code) == (False, 0)          # too many attempts void the code
    code = us.otp_issue(u["id"]); us.kb._exec("UPDATE otp_codes SET expires_at='2000-01-01T00:00:00+00:00'")
    assert us.otp_verify(u["id"], code) == (False, 0)                                                            # expired


def test_useradmin_cli(tmp_path, monkeypatch, capsys):
    from watchover import useradmin, auth as wo_auth
    monkeypatch.setenv("KNOWLEDGE_DB", str(tmp_path / "k.db"))
    assert useradmin.main(["list"]) == 0 and "no accounts" in capsys.readouterr().out
    assert useradmin.main(["add", "Tayfur@Sirket.com", "--admin", "--name", "Tayfur", "--password", "Kurumsal-Parola-2026"]) == 0
    assert useradmin.main(["add", "ops@sirket.com", "--password", "Operator-2026-x"]) == 0
    us = useradmin._users()
    assert us.get("tayfur@sirket.com")["role"] == "admin" and us.get("ops@sirket.com")["role"] == "operator"
    assert useradmin.main(["promote", "ops@sirket.com"]) == 0 and us.get("ops@sirket.com")["role"] == "admin"
    for _ in range(wo_auth.LOCK_FAILURES):
        us.login("ops@sirket.com", "wrong-wrong-1")
    assert us.locked_until("ops@sirket.com") and useradmin.main(["unlock", "ops@sirket.com"]) == 0 and not us.locked_until("ops@sirket.com")
    assert useradmin.main(["password", "ops@sirket.com", "--password", "Yeni-Parola-2026"]) == 0 and us.login("ops@sirket.com", "Yeni-Parola-2026")
    assert useradmin.main(["disable", "ops@sirket.com"]) == 0 and us.login("ops@sirket.com", "Yeni-Parola-2026") is None
    assert useradmin.main(["enable", "ops@sirket.com"]) == 0 and us.login("ops@sirket.com", "Yeni-Parola-2026")
    assert useradmin.main(["delete", "ops@sirket.com"]) == 0 and us.get("ops@sirket.com") is None
    assert useradmin.main(["promote", "nobody@sirket.com"]) == 1
    assert useradmin.main(["list"]) == 0 and "tayfur@sirket.com" in capsys.readouterr().out


def test_roles_and_permissions(tmp_path):
    from watchover.knowledge import Knowledge
    from watchover import rbac, auth as wo_auth
    kb = Knowledge(str(tmp_path / "k.db"))
    rl = rbac.Roles(kb)
    assert rl.names() == ["admin", "operator", "viewer"]
    assert rl.can("admin", "sys.users") and not rl.can("operator", "page.sys") and rl.can("operator", "act.connect") and not rl.can("viewer", "act.sim")
    r = rl.save("dba_lead", {"page.ops", "page.inv", "act.inventory", "bogus.perm"}, "DBA lideri")
    assert r["perms"] == {"page.ops", "page.inv", "act.inventory"} and not r["builtin"] and rl.can("dba_lead", "page.inv")
    us = wo_auth.Users(kb); us.bootstrap()
    u = us.register("dba@corp.com", "Parola-2026-x", "DBA")
    us.set_role(u["id"], "dba_lead", valid=rl.names())
    assert us.get("dba@corp.com")["role"] == "dba_lead"
    import pytest
    with pytest.raises(ValueError):
        rl.delete("dba_lead")                                  # still assigned
    with pytest.raises(ValueError):
        rl.delete("operator")                                  # built-in
    with pytest.raises(ValueError):
        rl.save("admin", set())                                # admin is never narrowed
    rl.save("operator", {"page.ops"})
    assert rl.perms("operator") == {"page.ops"}
    rl.reset("operator")
    assert rl.perms("operator") == rbac.BUILTIN["operator"]["perms"]
    us.set_role(u["id"], "viewer", valid=rl.names()); rl.delete("dba_lead")
    assert "dba_lead" not in rl.names()


def test_pending_registration_until_verified(tmp_path):
    from watchover.knowledge import Knowledge
    from watchover import auth as wo_auth
    us = wo_auth.Users(Knowledge(str(tmp_path / "k.db")))
    us.bootstrap()
    u = us.register("new@corp.com", "Parola-2026-x", "New", status="pending")
    assert u["status"] == "pending" and us.login("new@corp.com", "Parola-2026-x") is None       # not before the e-mail is verified
    code = us.otp_issue(u["id"])
    assert us.otp_verify(u["id"], code)[0]
    us.activate(u["id"])
    assert us.login("new@corp.com", "Parola-2026-x")["role"] == "operator"


def test_selfmon_command_and_token(tmp_path, monkeypatch):
    from watchover.knowledge import Knowledge
    from watchover import selfmon, agents, settings
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    reg = agents.AgentRegistry(Knowledge(str(tmp_path / "k.db")))
    tok = selfmon.ensure_token(reg, settings)
    assert tok.startswith("wo_") and reg.verify(tok)["name"] == selfmon.host_name() and settings.load()["self_agent_token"] == tok
    assert selfmon.ensure_token(reg, settings) == tok                                             # stable across restarts
    settings.save({"self_agent_token": ""})
    tok2 = selfmon.ensure_token(reg, settings)
    assert tok2 != tok and reg.verify(tok2) and reg.verify(tok) is None                         # lost token: rotated, old one dead
    # pid file: a stale agent from a previous application process is replaced, never duplicated
    import subprocess, sys
    stale = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)  # agent.py stand-in"])
    selfmon.pid_file().write_text(str(stale.pid))
    selfmon._kill_stale()
    assert stale.wait(timeout=5) is not None and not selfmon.pid_file().exists()
    cmd = selfmon.command(8600, tok2, root=tmp_path)
    assert "--auto" in cmd and "--metrics" in cmd and f"http://127.0.0.1:8600/ingest" in cmd and str(tmp_path / "agent.py") in cmd


def test_release_bump_and_changelog(tmp_path):
    from watchover import release
    from datetime import date
    root = tmp_path; (root / "src" / "watchover").mkdir(parents=True)
    (root / "src" / "watchover" / "__init__.py").write_text('__version__ = "1.0.0"\n'); (root / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.0.0"\n')
    assert release.display("1.0.0") == "v1.0" and release.display("1.2.0") == "v1.2" and release.display("1.2.3") == "v1.2.3"
    assert release.next_version("1.0.0", "minor") == "1.1.0" and release.next_version("1.1.0", "patch") == "1.1.1" and release.next_version("1.1.1", "major") == "2.0.0"
    assert release.bump("minor", "Bildirimler sekmesi\n- SMS sağlayıcıları", root, date(2026, 9, 20)) == "1.1.0"
    assert release.current(root) == "1.1.0" and 'version = "1.1.0"' in (root / "pyproject.toml").read_text()
    assert release.bump("patch", "MFA düzeltmesi", root, date(2026, 9, 21)) == "1.1.1"
    e = release.entries(root / "CHANGELOG.md")
    assert [x["version"] for x in e] == ["v1.1.1", "v1.1"] and e[1]["lines"] == ["Bildirimler sekmesi", "SMS sağlayıcıları"] and e[0]["kind"] == "düzeltme" and e[1]["date"] == "2026-09-20"
    import pytest
    with pytest.raises(ValueError):
        release.next_version("1.0.0", "huge")


def test_docker_mode_stamp_and_restart(monkeypatch):
    from watchover import admin, stamp
    monkeypatch.setenv("WATCHOVER_DOCKER", "1"); monkeypatch.setenv("WATCHOVER_GIT_REV", "abcdef1234567")
    stamp.git_rev.cache_clear(); admin._git_cache.clear()
    assert admin.in_docker() and stamp.git_rev() == "abcdef1"
    monkeypatch.setattr(admin.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("git")))   # no git in the image
    assert admin.version_info()["git"][:7] == "abcdef1" and admin.version_info()["docker"] is True
    stamp.git_rev.cache_clear(); admin._git_cache.clear()
