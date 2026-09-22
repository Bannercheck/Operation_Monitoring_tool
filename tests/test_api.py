"""The FastAPI layer: sign-in and tokens, RBAC, datasets (demo + upload job), incidents, actions, anomalies, playbook, live,
inventory, sources, notifications, agents, knowledge, users and system, all through the HTTP interface."""
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ADMIN = {"email": "admin@watchover.local", "password": "Watchover!Admin2026"}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    d = tmp_path_factory.mktemp("api")
    mp = pytest.MonkeyPatch()                                      # module scope: undone by hand so other tests keep their environment
    for k, v in {"KNOWLEDGE_DB": str(d / "k.db"), "ACTIONS_DB": str(d / "a.db"), "PLAYBOOK_DB": str(d / "pb.db"), "WATCHOVER_HOME": str(d / "home"),
                 "LIVE_SPOOL": str(d / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0"}.items():
        mp.setenv(k, v)
    mp.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    app = create_app()
    with TestClient(app) as c:
        c.app = app
        yield c
    mp.undo()


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


def test_oidc_flow_with_stubbed_provider(client, monkeypatch):
    """SSO: no provider until configured; then the start URL carries PKCE + signed state, and the callback (provider stubbed)
    signs the account in and hands the token to the app in the URL fragment."""
    from watchover import settings as wo_settings
    from watchover.api.routers import auth as a
    pv = client.get("/api/auth/providers").json()
    assert [p["name"] for p in pv] == ["google", "microsoft", "oidc"] and not any(p["configured"] for p in pv) and pv[0]["callback"].endswith("/api/auth/oidc/google/callback")
    assert client.get("/api/auth/oidc/google/start", follow_redirects=False).status_code == 404
    h = token(client)
    r = client.put("/api/system/auth", json={"auth_google": True, "google_client_id": "cid", "google_client_secret": "csecret", "auth_self_register": True}, headers=h)
    assert r.status_code == 200 and "google_client_secret" in r.json()["changed"]
    got = client.get("/api/system/auth", headers=h).json()
    assert got["google_client_secret"] == "•••" and got["google_client_id"] == "cid" and got["callbacks"]["microsoft"].endswith("/microsoft/callback")
    client.put("/api/system/auth", json={"google_client_secret": "•••", "auth_domains": ""}, headers=h)             # the mask keeps the stored secret
    assert wo_settings.load()["google_client_secret"] == "csecret"
    assert [p["name"] for p in client.get("/api/auth/providers").json() if p["configured"]] == ["google"]
    monkeypatch.setattr(a, "_discover", lambda issuer: {"authorization_endpoint": "https://idp.example/auth", "token_endpoint": "https://idp.example/token", "userinfo_endpoint": "https://idp.example/userinfo"})
    r = client.get("/api/auth/oidc/google/start?next=/ops", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://idp.example/auth?") and "code_challenge_method=S256" in r.headers["location"]
    state = dict(x.split("=", 1) for x in r.headers["location"].split("?", 1)[1].split("&"))["state"]

    class R:
        def __init__(self, code, data): self.status_code, self._d = code, data
        def json(self): return self._d
    seen = {}
    monkeypatch.setattr(a.httpx, "post", lambda url, data, timeout: seen.update(data) or R(200, {"access_token": "at", "id_token": ""}))
    monkeypatch.setattr(a.httpx, "get", lambda url, headers, timeout: R(200, {"email": "sso.user@example.com", "name": "SSO User"}))
    cb = client.get(f"/api/auth/oidc/google/callback?code=abc&state={state}", follow_redirects=False)
    assert cb.status_code == 302 and cb.headers["location"].startswith("/ops#sso=") and seen["code"] == "abc" and seen["code_verifier"]
    tok = cb.headers["location"].split("#sso=", 1)[1]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"}).json()
    assert me["email"] == "sso.user@example.com" and me["role"] == "operator"
    assert client.get("/api/auth/oidc/google/callback?code=abc&state=bad.state.x", follow_redirects=False).status_code == 401
    wo_settings.save({"auth_google": False, "google_client_id": "", "google_client_secret": ""})


def test_spa_is_served_when_built(client):
    """With web/dist present the API serves the React app: / and any client route return index.html, unknown /api paths stay 404."""
    from watchover.api import WEB_DIST
    if not (WEB_DIST / "index.html").exists():
        pytest.skip("web/dist not built")
    for path in ("/", "/anomalies", "/datasets/abc"):
        r = client.get(path)
        assert r.status_code == 200 and 'id="root"' in r.text
    assert client.get("/api/nope").status_code == 404


def test_phase3_map_assist_llm_itsm_search_settings(client, monkeypatch):
    """The endpoints that replaced the last Streamlit pages: failure map, ask, LLM settings, ITSM tickets, log search, compare, report, settings / channels, update info."""
    h = token(client)
    key = client.get("/api/datasets", headers=h).json()[0]["id"]
    m = client.get(f"/api/map?dataset={key}&lang=tr", headers=h).json()
    assert not m["empty"] and m["nodes"] >= 3 and m["root"] and "<svg" in m["html"] and m["incidents"]
    assert client.get("/api/map?dataset=live", headers=h).json()["empty"] is True
    # ask: deterministic fallback without an LLM, with sources from the dataset and the knowledge base
    r = client.post("/api/assist/ask", json={"question": "kök neden ne?", "dataset": key, "lang": "tr"}, headers=h).json()
    assert r["text"] and r["used_llm"] is False and isinstance(r["sources"], list) and r["model"] == ""
    assert client.post("/api/assist/save", json={"question": "q", "answer": r["text"][:200]}, headers=h).status_code == 201
    assert client.post("/api/assist/rate", json={"question": "q", "answer": "a", "verdict": "up"}, headers=h).json()["ok"]
    # llm settings: masked key, providers, stats; test fails cleanly without a server
    cfg = client.put("/api/llm", json={"llm_provider": "ollama", "llm_base": "http://127.0.0.1:1", "llm_model": "qwen2.5:7b-instruct", "llm_key": "k"}, headers=h).json()
    assert cfg["llm_key"] == "•••" and cfg["kind"] == "ollama" and cfg["enabled"] and "ollama" in cfg["providers"]
    assert client.get("/api/llm", headers=h).json()["llm_model"] == "qwen2.5:7b-instruct"
    assert client.post("/api/llm/test", headers=h).json()["ok"] is False
    assert client.get("/api/llm/quality", headers=h).json()["stats"]["calls"] >= 0
    client.put("/api/llm", json={"llm_base": "", "llm_model": "", "llm_key": ""}, headers=h)
    # itsm demo tickets correlated with the dataset's incidents
    assert client.get("/api/itsm/config", headers=h).json()["system"] == "demo"
    client.put("/api/itsm/config", json={"system": "demo", "token": "tok"}, headers=h)
    assert client.get("/api/itsm/config", headers=h).json()["token"] == "•••"
    tk = client.get(f"/api/itsm/tickets?dataset={key}", headers=h).json()
    assert tk["system"] == "demo" and tk["tickets"] and "relevance" in tk["tickets"][0]
    # log search with field terms and exclusion; compare a dataset with itself
    s = client.get(f"/api/datasets/{key}/search", params={"q": "timeout service:payment-api -debug", "limit": 5}, headers=h).json()
    assert s["total"] >= 1 and len(s["rows"]) <= 5 and all("timeout" in x["message"].lower() for x in s["rows"])
    assert client.get(f"/api/datasets/{key}/search", params={"q": "zzz-nothing"}, headers=h).json()["total"] == 0
    cmp = client.get(f"/api/datasets/compare?a={key}&b={key}", headers=h).json()
    assert cmp["kpis"] and all(r["delta"] in (0, 0.0, None) for r in cmp["kpis"]) and cmp["shared"] and not cmp["only_a"]
    rep = client.get(f"/api/live/report?window=60&dataset={key}", headers=h)
    assert rep.status_code == 200 and "<html" in rep.text.lower() and "attachment" in rep.headers["content-disposition"]
    # settings and channels, masked secrets kept, update info, self-registration options
    st = client.put("/api/system/settings", json={"learn_min": 20, "workspace": "Ops", "live_key": "secret-key"}, headers=h).json()
    assert "learn_min" in st["changed"] and client.get("/api/system/settings", headers=h).json()["live_key"] == "•••"
    client.put("/api/system/settings", json={"live_key": "•••"}, headers=h)
    from watchover import settings as wo_settings
    assert wo_settings.load()["live_key"] == "secret-key" and wo_settings.load()["workspace"] == "Ops"
    ch = client.put("/api/system/channels", json={"smtp_host": "mail.example.com", "smtp_password": "pw"}, headers=h).json()
    assert "smtp_password" in ch["changed"] and client.get("/api/system/channels", headers=h).json()["smtp_password"] == "•••"
    assert client.get("/api/system/update", headers=h).json()["releases"]
    client.put("/api/system/auth", json={"auth_self_register": True}, headers=h)
    assert client.get("/api/auth/options").json()["register"] is True                      # self-registration on and an SMTP host set
    r = client.post("/api/auth/register", json={"email": "new.user@example.com", "password": "Sifre-123456", "name": "New"})
    assert r.status_code == 502                                        # SMTP host set but unreachable: the code cannot be sent
    client.put("/api/system/channels", json={"smtp_host": ""}, headers=h)
    assert client.get("/api/auth/options").json() == {"register": True, "verify": False, "mfa": True}      # no SMTP: registration waits for an admin
    r = client.post("/api/auth/register", json={"email": "new2@example.com", "password": "Sifre-123456"})
    assert r.status_code == 202 and r.json()["approval"] is True
    assert client.post("/api/auth/login", json={"email": "new2@example.com", "password": "Sifre-123456"}).status_code == 401
    uid = [u for u in client.get("/api/users", headers=h).json() if u["email"] == "new2@example.com"][0]
    assert uid["status"] == "pending"
    client.patch(f"/api/users/{uid['id']}", json={"status": "active"}, headers=h)
    assert client.post("/api/auth/login", json={"email": "new2@example.com", "password": "Sifre-123456"}).status_code == 200
    client.put("/api/system/auth", json={"auth_self_register": False}, headers=h)
    assert client.get("/api/auth/options").json()["register"] is False and client.post("/api/auth/register", json={"email": "new3@example.com", "password": "Sifre-123456"}).status_code == 403
    client.put("/api/system/auth", json={"auth_self_register": True}, headers=h)
    assert client.get("/api/live/lines?agent=x&source=y", headers=h).json() == []


def test_restored_features_api(client):
    """Endpoints behind the features restored from Streamlit: samples, mapping upload, incident feedback / related / prompt, noise audit shape,
    dataset actions, discoveries, prune, readme, doc upload."""
    h = token(client)
    key = client.get("/api/datasets", headers=h).json()[0]["id"]
    assert [s["name"] for s in client.get("/api/datasets/samples", headers=h).json()] == ["alarm_storm.zip", "demo_mixed.zip", "sap_logs.zip"]
    r = client.post("/api/datasets/samples/alarm_storm.zip", headers=h)
    assert r.status_code == 202
    jid = r.json()["id"]
    for _ in range(600):
        j = client.get(f"/api/datasets/jobs/{jid}", headers=h).json()
        if j["state"] != "running":
            break
        time.sleep(0.2)
    assert j["state"] == "done", j
    storm = j["dataset"]
    na = client.get(f"/api/datasets/{storm}/noise", headers=h).json()
    assert na["heat"] and na["heat"]["services"] and na["heat"]["cells"] and "rows" in na and "demoted" in na
    inc = client.get(f"/api/datasets/{key}/incidents", headers=h).json()[0]["id"]
    fb = client.post(f"/api/datasets/{key}/incidents/{inc}/feedback", json={"verdict": "down", "correct": "__noise__", "comment": "test"}, headers=h).json()
    assert fb["ok"] and fb["proposals"] >= 1 and client.get("/api/knowledge/rules?status=proposed", headers=h).json()
    assert isinstance(client.get(f"/api/datasets/{key}/incidents/{inc}/related", headers=h).json(), list)
    assert "root cause" in client.get(f"/api/datasets/{key}/incidents/{inc}/prompt", headers=h).text.lower()
    assert client.post(f"/api/datasets/{key}/incidents/{inc}/explain", headers=h).status_code == 400          # no LLM configured
    acts = client.get(f"/api/datasets/{key}/actions", headers=h).json()
    assert acts and all(a["incident_id"].startswith("INC") for a in acts)
    files = [("files", ("demo.zip", (ROOT / "samples" / "demo_mixed.zip").read_bytes(), "application/zip"))]
    assert client.post("/api/datasets", files=files, data={"mapping": "{not json"}, headers=h).status_code == 400
    assert client.post("/api/datasets", files=files, data={"mapping": '{"message": "msg"}'}, headers=h).status_code == 202
    assert client.get("/api/live/discoveries", headers=h).json() == {}
    assert client.post("/api/system/snapshots/prune?keep=10", headers=h).json()["removed"] == 0
    assert "Watchover" in client.get("/api/system/readme", headers=h).text
    up = client.post("/api/knowledge/docs/upload", files=[("file", ("runbook.md", b"# Pool\n\nRestart the pool when 90% used.", "text/markdown"))], headers=h)
    assert up.status_code == 201 and up.json()["ids"]
    assert client.post("/api/datasets/fetch", json={"kind": "http", "url": "http://127.0.0.1:1/x"}, headers=h).status_code == 400


def test_sources_draft_test_and_dataset_from_source(client):
    """Pull sources: a draft can be tested without saving, a saved source can be pulled into a dataset; unreachable hosts are reported, not 500s."""
    h = token(client)
    draft = {"name": "es-test", "kind": "elasticsearch", "url": "http://127.0.0.1:9", "selector": "logs-*", "auth": "none", "lookback_min": 5}
    r = client.post("/api/sources/test", json=draft, headers=h)
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["sample"] == []
    assert client.get("/api/sources/kinds", headers=h).json()["loki"]["label"] == "Grafana Loki"
    sid = client.post("/api/sources", json=draft, headers=h).json()["id"]
    r = client.post("/api/datasets/from-source", json={"id": sid, "minutes": 30}, headers=h)
    assert r.status_code == 400 and "fetch failed" in r.json()["detail"]
    assert client.post("/api/datasets/from-source", json={"id": 999999}, headers=h).status_code == 404
    client.delete(f"/api/sources/{sid}", headers=h)
