"""The in-app update API (System › Güncelleme): sys.update + step-up for upload / apply / rollback, packages checked on upload,
requests written for the updater (never executed by the dashboard), refusals and finished jobs in the security log."""
import io
import json
import time
import zipfile
from pathlib import Path

import pytest

from watchover import __version__, updates as U
from test_updater import make_tree, zip_tree          # noqa: E402 (tests/ is on sys.path in pytest's default import mode)

ADMIN = ("admin@watchover.local", "Test-Yonetici-Parola-42")
PW = "Str0ng!Passw0rd"


@pytest.fixture
def c(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0",
                 "WATCHOVER_SELFMON": "0", "WATCHOVER_PSEUDO_KEY": "k", "WATCHOVER_UPDATES_DIR": str(tmp_path / "spool")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("WATCHOVER_UPDATE_PUBKEY", raising=False)
    monkeypatch.setattr(U, "mode", lambda: "updater")
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app(); svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    svc.users.register("viewer@firma.com", PW, role="operator")
    svc.kb._exec("UPDATE users SET must_change=0")
    with TestClient(a) as cl:
        cl.svc, cl.spool = svc, tmp_path / "spool"
        yield cl


def login(c, email, pw):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def heartbeat(spool: Path, **kw):
    U.write_json(spool / "updater.json", {"ts": time.time(), "heartbeat": "now", "keep": 5, "health_s": 240, "signing": {"required": False}, **kw})


def pkg(tmp_path, version="9.1.0", **kw) -> bytes:
    return zip_tree(make_tree(tmp_path / f"t{version}", version, **kw), tmp_path / f"p{version}.zip").read_bytes()


def upload(c, h, data, name="hackathon.zip", sig=""):
    return c.post(f"/api/system/updates/upload?name={name}", content=data, headers={**h, "Content-Type": "application/octet-stream",
                                                                                   **({"X-Watchover-Signature": sig} if sig else {})})


def events(c):
    return [(r["event"], r["ok"], r["detail"]) for r in c.svc.kb._exec("SELECT event, ok, detail FROM auth_events WHERE event='sys.update' ORDER BY id")]


def test_permissions_and_step_up(c, tmp_path):
    v = login(c, "viewer@firma.com", PW)
    for method, path in (("get", "/api/system/updates"), ("post", "/api/system/updates/upload"), ("post", "/api/system/updates/rollback"),
                         ("get", "/api/system/updates/state"), ("put", "/api/system/updates/config")):
        r = getattr(c, method)(path, headers=v, **({"json": {"version": "1.0.0"}} if method != "get" else {}))
        assert r.status_code == 403, (path, r.status_code)
    h = login(c, *ADMIN)
    assert c.get("/api/system/updates", headers=h).json()["mode"] == "updater"
    data = pkg(tmp_path)
    r = upload(c, h, data)                                                    # administrators are not exempt
    assert r.status_code == 428 and r.json()["detail"]["code"] == "act_stepup_required"
    assert c.post("/api/system/updates/stepup", headers=h).status_code == 428
    assert c.post("/api/system/updates/rollback", json={"version": "1.0.0"}, headers=h).status_code == 428
    assert c.post("/api/system/updates/packages/20261016-100000-aaaaaa/apply", json={}, headers=h).status_code == 428
    assert not list((c.spool / "inbox").iterdir())                           # nothing stored before the step-up
    assert c.post("/api/auth/stepup/verify", json={"password": ADMIN[1]}, headers=h).status_code == 200
    assert c.post("/api/system/updates/stepup", headers=h).json()["ok"]
    r = upload(c, h, data)
    assert r.status_code == 201, r.text
    m = r.json()
    assert m["version"] == "9.1.0" and m["current"] == __version__ and m["direction"] == "upgrade" and "sig" not in m and m["signature"]["status"] == "none"


def test_upload_checks_queue_and_rollback(c, tmp_path):
    h = login(c, *ADMIN)
    c.post("/api/auth/stepup/verify", json={"password": ADMIN[1]}, headers=h)
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("hackathon/../../etc/cron.d/x", "* * * * * root sh")
    r = upload(c, h, bad.getvalue(), name="../../evil.zip")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "upd_unsafe_path"
    r = upload(c, h, pkg(tmp_path, "9.2.0", drop=("src/watchover/updater.py",)))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "upd_no_updater"
    assert not list((c.spool / "inbox").iterdir())
    m = upload(c, h, pkg(tmp_path, "1.0.0"), name="old.zip").json()
    assert m["direction"] == "downgrade"                                      # allowed, the page warns
    m = upload(c, h, pkg(tmp_path)).json()
    r = c.post(f"/api/system/updates/packages/{m['id']}/apply", json={"auto_rollback": True}, headers=h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "upd_offline"   # no updater heartbeat
    heartbeat(c.spool)
    r = c.post(f"/api/system/updates/packages/{m['id']}/apply", json={"auto_rollback": False}, headers=h)
    assert r.status_code == 200 and r.json()["queued"] == m["id"]
    req = json.loads((c.spool / "requests" / f"{m['id']}.json").read_text())
    assert req["kind"] == "apply" and req["version"] == "9.1.0" and req["auto_rollback"] is False and req["user"] == ADMIN[0]
    assert c.post(f"/api/system/updates/packages/{m['id']}/apply", json={}, headers=h).json()["detail"]["code"] == "upd_busy"
    assert c.delete(f"/api/system/updates/packages/{m['id']}", headers=h).status_code == 409          # queued: kept
    ov = c.get("/api/system/updates", headers=h).json()
    assert ov["updater"]["online"] and ov["pending"][0]["id"] == m["id"] and len(ov["packages"]) == 2
    # the updater picked it up and finished: state shown, the job audited once
    (c.spool / "requests" / f"{m['id']}.json").unlink()
    U.write_json(c.spool / "state.json", {"id": m["id"], "kind": "apply", "user": ADMIN[0], "from": __version__, "to": "9.1.0", "status": "rolled_back",
                                          "error": {"code": "health_failed", "detail": "no answer"}, "steps": []})
    U.write_json(c.spool / "releases.json", {"releases": [{"version": __version__, "current": True, "backup": None},
                                                          {"version": "1.40.0", "current": False, "backup": {"at": "2026-10-01"}},
                                                          {"version": "1.41.0", "current": False, "backup": None}]})
    st = c.get("/api/system/updates/state", headers=h).json()
    assert st["state"]["status"] == "rolled_back" and st["updater"]["online"] and st["version"] == __version__
    c.get("/api/system/updates/state", headers=h)
    ev = events(c)
    assert sum(1 for e in ev if "rolled_back" in e[2]) == 1
    assert any(not ok and "refused" in d and "unsafe_path" in d for _, ok, d in ev) and any("update requested" in d for _, _, d in ev)
    # rollback requests: only kept releases, never the running one, the database only with a backup
    for body, code in (({"version": "1.39.0"}, "upd_unknown_release"), ({"version": __version__}, "upd_same_version"),
                       ({"version": "1.41.0", "restore_db": True}, "upd_no_backup"), ({"version": "1.0; rm"}, "upd_bad_version")):
        r = c.post("/api/system/updates/rollback", json=body, headers=h)
        assert r.json()["detail"]["code"] == code, (body, r.text)
    r = c.post("/api/system/updates/rollback", json={"version": "1.40.0", "restore_db": True}, headers=h)
    assert r.status_code == 200
    req = json.loads((c.spool / "requests" / f"{r.json()['queued']}.json").read_text())
    assert req == {**req, "kind": "rollback", "version": "1.40.0", "restore_db": True, "auto_rollback": True}
    assert any("WITH database restore" in d for _, _, d in events(c))
    assert c.post("/api/system/updates/rollback", json={"version": "1.40.0"}, headers=h).json()["detail"]["code"] == "upd_busy"
    # settings are clamped, the log is masked
    assert c.put("/api/system/updates/config", json={"keep": 1, "health_s": 5, "auto_rollback": False}, headers=h).json() == {"keep": 2, "health_s": 30, "auto_rollback": False}
    (c.spool / "jobs").mkdir(exist_ok=True)
    (c.spool / "jobs" / f"{m['id']}.log").write_text("POSTGRES_PASSWORD=hunter2\nbuilding\n")
    log = c.get(f"/api/system/updates/log/{m['id']}", headers=h).text
    assert "hunter2" not in log and "building" in log
    assert c.get("/api/system/updates/log/..%2F..%2Fetc", headers=h).status_code == 404


def test_signature_enforced_by_the_dashboard(c, tmp_path, monkeypatch):
    from watchover import release
    r = release.keygen(tmp_path / "keys")
    monkeypatch.setenv("WATCHOVER_UPDATE_PUBKEY", r["pubkey"])
    h = login(c, *ADMIN)
    c.post("/api/auth/stepup/verify", json={"password": ADMIN[1]}, headers=h)
    data = pkg(tmp_path)
    assert upload(c, h, data).json()["detail"]["code"] == "upd_signature_missing"
    p = tmp_path / "s.zip"; p.write_bytes(data)
    sig = release.sign_package(p, Path(r["key"])).read_text().strip()
    m = upload(c, h, data, sig=sig).json()
    assert m["signature"]["status"] == "ok" and m["signature"]["fingerprint"] == r["fingerprint"]
    assert c.get("/api/system/updates", headers=h).json()["signing"]["required"]


def test_other_modes(c, monkeypatch):
    h = login(c, *ADMIN)
    monkeypatch.setattr(U, "mode", lambda: "openshift")
    ov = c.get("/api/system/updates", headers=h).json()
    assert ov["mode"] == "openshift" and any("helm rollback" in x for x in ov["commands"])
    c.post("/api/auth/stepup/verify", json={"password": ADMIN[1]}, headers=h)
    assert upload(c, h, b"PK").json()["detail"]["code"] == "upd_unavailable"
    monkeypatch.setattr(U, "mode", lambda: "manual")
    assert "./watchover.sh updater on" in c.get("/api/system/updates", headers=h).json()["commands"]
    assert c.get("/api/system/updates/state", headers=h).json()["state"] is None


def test_inprocess_apply_unpacks_after_snapshot(c, tmp_path, monkeypatch):
    """Classic installs: the checked zip is unpacked in place after a snapshot, then the process restarts (all stubbed here)."""
    from watchover import admin
    monkeypatch.setattr(U, "mode", lambda: "inprocess")
    calls = []
    monkeypatch.setattr(admin, "snapshot", lambda reason, **kw: calls.append(("snapshot", reason)) or {})
    monkeypatch.setattr(admin, "apply_zip", lambda data, dest=None: calls.append(("apply", len(data))) or (True, "12 files updated"))
    monkeypatch.setattr(admin, "deploy_finish", lambda reason="": calls.append(("restart", reason)) or {"restart": "exit"})
    h = login(c, *ADMIN)
    c.post("/api/auth/stepup/verify", json={"password": ADMIN[1]}, headers=h)
    data = pkg(tmp_path, extra={"app.py": "x", "src/watchover/analysis.py": "x"}, drop=("Dockerfile.updater",))
    m = upload(c, h, data).json()
    assert m["version"] == "9.1.0", m
    r = c.post(f"/api/system/updates/packages/{m['id']}/apply", json={}, headers=h).json()
    assert r["inprocess"] and [x[0] for x in calls] == ["snapshot", "apply", "restart"]
    r = upload(c, h, pkg(tmp_path, "9.3.0"))                                  # no app.py: not a classic tree
    assert r.json()["detail"]["code"] == "upd_not_watchover"
