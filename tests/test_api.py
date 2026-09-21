"""The FastAPI layer: sign-in and tokens, RBAC, datasets (demo + upload job), incidents, actions, anomalies, playbook, live,
inventory, sources, notifications, agents, knowledge, users and system, all through the HTTP interface."""
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADMIN = {"email": "admin@watchover.local", "password": "Watchover!Admin2026"}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    import os
    d = tmp_path_factory.mktemp("api")
    for k, v in {"KNOWLEDGE_DB": str(d / "k.db"), "ACTIONS_DB": str(d / "a.db"), "PLAYBOOK_DB": str(d / "pb.db"), "WATCHOVER_HOME": str(d / "home"),
                 "LIVE_SPOOL": str(d / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0"}.items():
        os.environ[k] = v
    os.environ.pop("DATABASE_URL", None)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    app = create_app()
    with TestClient(app) as c:
        c.app = app
        yield c


def token(client, creds=ADMIN) -> dict:
    r = client.post("/api/auth/login", json=creds)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_health_login_me(client):
    assert client.get("/api/health").json()["ok"] is True
    assert client.get("/api/auth/me").status_code == 401
    assert client.post("/api/auth/login", json={"email": ADMIN["email"], "password": "nope"}).status_code == 401
    h = token(client)
    me = client.get("/api/auth/me", headers=h).json()
    assert me["role"] == "admin" and me["must_change"] is True and "page.ops" in me["permissions"]
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer x.y.z"}).status_code == 401
    assert client.get("/api/docs").status_code == 200 and "/api/datasets/{key}/incidents" in client.get("/api/openapi.json").json()["paths"]


def test_datasets_incidents_actions_export(client):
    h = token(client)
    d = client.post("/api/datasets/demo", headers=h).json()
    key = d["id"]
    assert d["funnel"]["raw_events"] > 800 and d["incidents"] >= 1 and d["stamp"]["result"]
    assert [x["id"] for x in client.get("/api/datasets", headers=h).json()] == [key]
    incs = client.get(f"/api/datasets/{key}/incidents", headers=h).json()
    assert incs and incs[0]["narrative_text"] and isinstance(incs[0]["timeline"], int)
    iid = incs[0]["id"]
    inc = client.get(f"/api/datasets/{key}/incidents/{iid}", headers=h).json()
    assert inc["root_cause"]["template"] and inc["signals"] and inc["actions"] and inc["reason_text"]
    assert client.get(f"/api/datasets/{key}/incidents/{iid}/postmortem", headers=h).text.startswith("#")
    assert client.get(f"/api/datasets/{key}/incidents/NOPE", headers=h).status_code == 404
    sig = client.get(f"/api/datasets/{key}/signals?limit=5", headers=h).json()
    assert len(sig) == 5 and sig[0]["why"]["confidence"] > 0
    ev = client.get(f"/api/datasets/{key}/evidence", params={"ref": sig[0]["evidence"][0]}, headers=h).json()
    assert ev["message"] and ev["ref"] == sig[0]["evidence"][0]
    assert client.get(f"/api/datasets/{key}/noise", headers=h).json()["eliminated"] >= 0
    csv = client.get(f"/api/datasets/{key}/export?fmt=csv", headers=h)
    assert csv.status_code == 200 and csv.text.count("\n") > 800
    acts = client.get("/api/actions", headers=h).json()
    assert acts and any(a["evidence"].startswith("first:") for a in acts)
    a = client.post("/api/actions", json={"incident_id": iid, "title": "Raise pool", "priority": "P1", "owner": "ops"}, headers=h).json()
    assert client.patch(f"/api/actions/{a['id']}", json={"status": "done"}, headers=h).json()["status"] == "done"
    assert client.patch(f"/api/actions/{a['id']}", json={"status": "bogus"}, headers=h).status_code == 400
    assert client.delete(f"/api/actions/{a['id']}", headers=h).json()["ok"] and client.get(f"/api/actions/{a['id']}", headers=h).status_code == 404
    # upload: two files combined into one background job
    files = [("files", ("demo.zip", (ROOT / "samples" / "demo_mixed.zip").read_bytes(), "application/zip")), ("files", ("empty.log", b"", "text/plain"))]
    r = client.post("/api/datasets", files=files, headers=h)
    assert r.status_code == 202
    jid = r.json()["jobs"][0]["id"]
    for _ in range(300):
        j = client.get(f"/api/datasets/jobs/{jid}", headers=h).json()
        if j["state"] != "running":
            break
        time.sleep(0.2)
    assert j["state"] == "done" and j["pct"] == 100 and j["dataset"], j
    assert client.get(f"/api/datasets/{j['dataset']}", headers=h).json()["funnel"]["raw_events"] == d["funnel"]["raw_events"]
    assert client.delete(f"/api/datasets/{j['dataset']}", headers=h).json()["ok"]


def test_users_roles_and_permissions(client):
    h = token(client)
    u = client.post("/api/users", json={"email": "viewer@example.com", "password": "Sifre-123456", "name": "View", "role": "viewer"}, headers=h)
    assert u.status_code == 201 and u.json()["role"] == "viewer"
    v = client.post("/api/auth/login", json={"email": "viewer@example.com", "password": "Sifre-123456"}).json()
    assert "token" in v and v["mfa"] == "skipped"                       # no SMTP configured: the second factor cannot be sent
    hv = {"Authorization": f"Bearer {v['token']}"}
    assert client.get("/api/anomalies", headers=hv).status_code == 200
    assert client.post("/api/anomalies/scan", headers=hv).status_code == 403
    assert client.post("/api/users", json={"email": "x@example.com", "password": "Sifre-123456"}, headers=hv).status_code == 403
    assert client.get("/api/users", headers=hv).status_code == 403
    roles = client.get("/api/roles", headers=h).json()
    assert {r["name"] for r in roles["roles"]} >= {"admin", "operator", "viewer"} and "act.anomaly" in roles["permissions"]
    client.put("/api/roles/auditor", json={"perms": ["page.ops", "act.anomaly"], "label": "Auditor"}, headers=h)
    uid = u.json()["id"]
    assert client.patch(f"/api/users/{uid}", json={"role": "auditor"}, headers=h).json()["role"] == "auditor"
    hv = token(client, {"email": "viewer@example.com", "password": "Sifre-123456"})
    assert client.post("/api/anomalies/scan", headers=hv).status_code == 200
    assert client.post(f"/api/users/{uid}/password", json={"password": "short"}, headers=h).status_code == 400
    assert client.post("/api/auth/password", json={"current": "Sifre-123456", "new": "Yeni-Sifre-789"}, headers=hv).json()["ok"]
    assert client.post("/api/auth/login", json={"email": "viewer@example.com", "password": "Sifre-123456"}).status_code == 401
    assert client.delete(f"/api/users/{uid}", headers=hv).status_code == 403
    assert client.get("/api/users/events?email=viewer@example.com", headers=h).json()


def test_anomalies_playbook_knowledge(client):
    h = token(client)
    svc = client.app.state.services
    a = svc.anomalies._upsert("errors", "errors:prod:web-09", env="prod", host="web-09", title="web-09 spike", detail="seeded", observed=80, baseline=6, spread=2, score=9.5, series=[1, 2, 16])
    lst = client.get("/api/anomalies", headers=h).json()
    assert [x["id"] for x in lst] == [a["id"]] and client.get("/api/anomalies/stats", headers=h).json()["open"] == 1
    assert client.post(f"/api/anomalies/{a['id']}/steps/verify", json={"done": True}, headers=h).json()["status"] == "ack"
    r = client.post(f"/api/anomalies/{a['id']}/action", headers=h).json()
    assert r["action"]["incident_id"] == f"ANOM-{a['id']}" and r["anomaly"]["action_id"] == r["action"]["id"]
    assert client.post(f"/api/anomalies/{a['id']}/status", json={"status": "resolved", "note": "pool"}, headers=h).json()["status"] == "resolved"
    assert client.get("/api/anomalies?status=all", headers=h).json()[0]["note"] == "pool"
    pb = client.get("/api/playbook", headers=h).json()
    assert pb and client.get("/api/playbook/entry", params={"key": pb[0]["key"]}, headers=h).json()["template"] == pb[0]["template"]
    assert client.put("/api/playbook/entry", params={"key": pb[0]["key"]}, json={"resolution": "restart cleared it"}, headers=h).json()["resolution"] == "restart cleared it"
    assert client.get("/api/playbook/lookup", params={"template": pb[0]["template"]}, headers=h).json()["key"] == pb[0]["key"]
    n = client.post("/api/knowledge/notes", json={"title": "pool", "text": "connection pool exhausted at 90%"}, headers=h).json()
    assert n["kind"] == "note" and client.get("/api/knowledge/search?q=pool", headers=h).json()
    rid = client.post("/api/knowledge/rules", json={"kind": "owner", "key": "payment", "value": "payments-team"}, headers=h).json()["id"]
    assert client.post(f"/api/knowledge/rules/{rid}/decide", json={"approve": True}, headers=h).status_code == 200
    assert client.get("/api/knowledge/rules?status=approved", headers=h).json()[0]["id"] == rid
    assert client.get("/api/knowledge/stats", headers=h).json()["lessons"]


def test_live_inventory_sources_notify_agents_system(client):
    h = token(client)
    assert client.get("/api/live/stats", headers=h).json()["total"] == 0 and client.get("/api/live/hosts", headers=h).json()["hosts"] == {}
    assert client.post("/api/live/analyze", headers=h).status_code == 400
    assert client.post("/api/live/simulate", json={"on": True}, headers=h).json()["simulator"] is True
    time.sleep(1.5)
    assert client.get("/api/live/events?n=5", headers=h).status_code == 200
    assert client.post("/api/live/simulate", json={"on": False}, headers=h).json()["simulator"] is False
    assert client.get("/api/live/history?hours=1", headers=h).status_code == 200
    r = client.put("/api/inventory", json={"hostname": "WEB-01", "dc": "dc1", "owner": "platform"}, headers=h)
    assert r.status_code == 201 and client.get("/api/inventory/web-01", headers=h).json()["owner"] == "platform"
    assert "hostname" in client.get("/api/inventory/export", headers=h).text
    s = client.post("/api/sources", json={"name": "loki", "kind": "loki", "url": "http://127.0.0.1:1", "secret": "s3cret"}, headers=h).json()
    assert s["secret"] == "•••" and client.get("/api/sources", headers=h).json()[0]["name"] == "loki" and "loki" in client.get("/api/sources/kinds", headers=h).json()
    assert client.post(f"/api/sources/{s['id']}/test", headers=h).json()["ok"] is False
    assert client.patch(f"/api/sources/{s['id']}", json={"enabled": False}, headers=h).json()["enabled"] in (False, 0)
    rid = client.post("/api/notify/recipients", json={"name": "Nöbetçi", "email": "oncall@example.com", "groups": "ops"}, headers=h).json()["id"]
    assert client.get("/api/notify/recipients", headers=h).json()[0]["id"] == rid and "anomaly" in client.get("/api/notify/conditions", headers=h).json()
    client.post("/api/notify/rules", json={"name": "anomalies", "condition": "anomaly", "targets": "ops"}, headers=h)
    assert client.get("/api/notify/rules", headers=h).json()[0]["condition"] == "anomaly"
    e = client.post("/api/agents", json={"name": "web-01", "env": "prod"}, headers=h).json()
    assert e["token"].startswith("wo_") and client.get("/api/agents", headers=h).json()[0]["name"] == "web-01"
    assert client.post(f"/api/agents/{e['agent']['id']}/rotate", headers=h).json()["token"] != e["token"]
    assert client.post(f"/api/agents/{e['agent']['id']}/revoke", headers=h).json()["status"] == "revoked"
    st = client.get("/api/system/status", headers=h).json()
    assert st["database"]["backend"] == "sqlite" and st["users"] >= 2 and st["version"]["version"]
    snap = client.post("/api/system/snapshots", json={"reason": "test", "code": False}, headers=h).json()
    assert snap["dbs"] and client.get("/api/system/snapshots", headers=h).json()[0]["id"] == snap["id"]
    assert client.delete(f"/api/system/snapshots/{snap['id']}", headers=h).json()["ok"]
