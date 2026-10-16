"""Per-server threshold policies: the most specific policy wins per metric, a level must last for_min minutes before anyone is told,
warn escalates to crit, the cooldown holds repeats back, a value back under the warning level less 5 % sends 'resolved' and closes the
anomaly; alerts go out by webhook (Teams / Slack / JSON) through the same dispatch as e-mail and SMS; the API respects teams."""
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from watchover.anomaly import AnomalyTracker
from watchover.history import History
from watchover.knowledge import Knowledge
from watchover.live import LiveStore
from watchover.notify import Notifier
from watchover.thresholds import Thresholds, webhook_payload

UTC = timezone.utc


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def dispatch(self, rule, key, title, body, force=False):
        self.sent.append({"rule": rule, "key": key, "title": title, "body": body})
        return {"title": title, "ok": 1}


def _metrics(live, values):
    now = datetime.now(UTC).isoformat()
    live.ingest("m.jsonl", ("\n".join(json.dumps({"ts": now, "host": h, "env": "prod", "cpu": v, "memory": 40}) for h, v in values.items()) + "\n").encode(), agent="agent")


@pytest.fixture
def T(tmp_path):
    kb = Knowledge(str(tmp_path / "k.db")); History(kb)
    live = LiveStore(spool=None)
    an = AnomalyTracker(kb, live, learning_days=0)
    n = FakeNotifier()
    t = Thresholds(kb, live, n, an, globals_fn=lambda: {"cpu": 85.0, "memory": 90.0, "disk": 90.0, "gpu": 95.0})
    live.threshold_fn = t.limit_for
    return t, live, n, an, kb


def test_most_specific_policy_wins(T):
    t, live, *_ = T
    _metrics(live, {"prd-sap-app-01": 50, "prd-sap-app-02": 50, "prd-web-01": 50, "dev-x-01": 50})
    t.save({"scope": "all", "metric": "cpu", "warn": 60, "crit": 70})
    t.save({"scope": "env", "scope_value": "prod", "metric": "cpu", "warn": 65, "crit": 75})
    t.save({"scope": "pool", "scope_value": "prd-sap-app", "metric": "cpu", "warn": 70, "crit": 80})
    t.save({"scope": "glob", "scope_value": "prd-sap-app-0?", "metric": "cpu", "warn": 72, "crit": 82})
    t.save({"scope": "host", "scope_value": "prd-sap-app-02", "metric": "cpu", "warn": 90, "crit": 95})
    eff = lambda h, env="prod": t.effective(h, env)["cpu"]           # noqa: E731
    assert eff("prd-sap-app-02")["scope"] == "host" and eff("prd-sap-app-01")["scope"] == "glob"
    assert eff("prd-web-01")["scope"] == "env" and eff("dev-x-01", "dev")["scope"] == "all"
    assert t.effective("prd-web-01", "prod")["memory"]["scope"] == "global"          # no policy: System › Settings
    assert t.limit_for("prd-sap-app-02", "cpu", "prod") == 90 and t.limit_for("prd-web-01", "memory", "prod") == 90.0
    for bad in ({"scope": "host", "metric": "cpu", "warn": 1}, {"scope": "all", "metric": "nope", "warn": 1}, {"scope": "all", "metric": "cpu"},
                {"scope": "all", "metric": "cpu", "warn": 90, "crit": 80}, {"scope": "all", "metric": "cpu", "warn": 120}, {"scope": "all", "metric": "cpu", "warn": 5, "channels": "fax"}):
        with pytest.raises(ValueError):
            t.save(bad)


def test_sustain_escalate_cooldown_and_resolve(T):
    t, live, n, an, kb = T
    t.save({"scope": "host", "scope_value": "prd-db-01", "metric": "cpu", "warn": 70, "crit": 90, "for_min": 3, "channels": "webhook", "targets": "noc", "cooldown_min": 30})
    now = time.time()
    _metrics(live, {"prd-db-01": 75})
    assert t.evaluate(now) == [] and t.state()[0]["level"] == "warn"               # beyond, but not for 3 minutes yet
    assert t.evaluate(now + 120) == []
    sent = t.evaluate(now + 190)
    assert len(sent) == 1 and "prd-db-01 cpu 75%" in sent[0]["title"] and n.sent[-1]["rule"]["channels"] == "webhook" and n.sent[-1]["rule"]["targets"] == "noc"
    assert n.sent[-1]["rule"]["severity"] == "medium" and any("prd-db-01" in a["title"] for a in an.list("open"))
    assert t.evaluate(now + 300) == []                                               # same level inside the cooldown: quiet
    _metrics(live, {"prd-db-01": 93})
    sent = t.evaluate(now + 360)
    assert len(sent) == 1 and n.sent[-1]["rule"]["severity"] == "critical"           # escalation goes out at once
    _metrics(live, {"prd-db-01": 68})                                                 # under 70 but inside the 5 % band (66.5)
    assert t.evaluate(now + 420) == [] and t.state()
    _metrics(live, {"prd-db-01": 50})
    sent = t.evaluate(now + 480)
    assert len(sent) == 1 and "Resolved" in sent[0]["title"] and t.state() == [] and not any("prd-db-01" in a["title"] for a in an.list("open"))


def test_log_based_metrics_and_breach_list(T):
    t, live, n, *_ = T
    now = datetime.now(UTC).isoformat()
    lines = [json.dumps({"ts": now, "host": "prd-api-01", "level": "error" if i % 3 == 0 else "info", "service": "api", "msg": f"GET /x {200 + i} ms"}) for i in range(30)]
    live.ingest("a.jsonl", ("\n".join(lines) + "\n").encode(), agent="prd-api-01")
    _metrics(live, {"prd-web-01": 88})
    v = t.values()
    assert abs(v["prd-api-01"]["error_rate"] - 33.333) < 0.01 and v["prd-api-01"]["errors_per_min"] == 2.0 and v["prd-api-01"]["p95_ms"] and v["prd-api-01"]["silent_min"] < 1
    t.save({"scope": "all", "metric": "error_rate", "warn": 10, "crit": 50, "for_min": 0})
    t.save({"scope": "host", "scope_value": "prd-web-01", "metric": "cpu", "warn": 95, "crit": 99})
    assert [s["title"] for s in t.evaluate()] == ["Uyarı / Warning · prd-api-01 error_rate 33% (≥ 10%)"]
    assert live.metric_stats(15)["breaches"] == []                                  # 88 % is under this host's own 95 % (global 85 would breach)
    pv = t.preview({"scope": "glob", "scope_value": "prd-*", "metric": "cpu", "warn": 80, "crit": 90})
    assert pv["covered"] == 2 and pv["wins"] == 1 and pv["beyond"] == []              # prd-web-01 keeps its host policy


def test_webhook_channel_posts_teams_card(tmp_path):
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((self.headers.get("Authorization"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200); self.end_headers()

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        kb = Knowledge(str(tmp_path / "k.db"))
        n = Notifier(kb, lambda: {"webhook": {"url": f"http://127.0.0.1:{srv.server_port}/hook", "kind": "teams", "secret": "s3"}})
        row = n.dispatch({"id": None, "severity": "critical", "channels": "webhook", "targets": "", "cooldown_min": 0}, "k1", "prd-db-01 cpu 93%", "body", force=True)
        assert row["ok"] == 1 and "webhook:HTTP 200" in row["detail"]
        auth, card = got[0]
        assert auth == "Bearer s3" and card["@type"] == "MessageCard" and card["themeColor"] == "F87171" and card["title"] == "prd-db-01 cpu 93%"
        assert webhook_payload("slack", "low", "t", "b")["attachments"][0]["color"] == "#34D399" and webhook_payload("x", "high", "t", "b")["severity"] == "high"
        n2 = Notifier(kb, lambda: {"webhook": {}})
        assert "no webhook URL" in n2.dispatch({"severity": "high", "channels": "webhook"}, "k2", "t", "b", force=True)["detail"]
    finally:
        srv.shutdown()


def test_thresholds_api_and_team_scope(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    a = create_app(); svc = a.state.services
    svc.kb._exec("UPDATE users SET must_change=0")
    svc.users.register("db@corp.com", "Str0ng!Passw0rd", role="operator")
    _metrics(svc.live, {"prd-db-01": 92, "prd-web-01": 70})
    with TestClient(a) as c:
        login = lambda e, p: {"Authorization": "Bearer " + c.post("/api/auth/login", json={"email": e, "password": p}).json()["token"]}   # noqa: E731
        admin = login("admin@watchover.local", "Test-Yonetici-Parola-42")
        r = c.post("/api/thresholds", json={"scope": "pool", "scope_value": "prd-db", "metric": "cpu", "warn": 80, "crit": 95, "for_min": 0, "channels": "email,webhook", "targets": "noc"}, headers=admin)
        assert r.status_code == 201, r.text
        assert c.post("/api/thresholds", json={"scope": "host", "metric": "cpu", "warn": 1}, headers=admin).status_code == 400
        pv = c.post("/api/thresholds/preview", json={"scope": "all", "metric": "cpu", "warn": 60, "crit": 90}, headers=admin).json()
        assert pv["covered"] == 2 and pv["wins"] == 1 and [b["host"] for b in pv["beyond"]] == ["prd-web-01"]
        ov = {h["host"]: h for h in c.get("/api/thresholds/overview", headers=admin).json()["hosts"]}
        assert ov["prd-db-01"]["metrics"]["cpu"]["source"]["scope"] == "pool" and ov["prd-web-01"]["metrics"]["cpu"]["source"]["scope"] == "global"
        c.post("/api/thresholds/evaluate", headers=admin)
        assert [s["host"] for s in c.get("/api/thresholds/state", headers=admin).json()] == ["prd-db-01"]
        c.post("/api/teams", json={"name": "Web", "rules": [{"field": "host", "op": "glob", "value": "prd-web-*"}], "members": ["db@corp.com"]}, headers=admin)
        op = login("db@corp.com", "Str0ng!Passw0rd")
        assert [h["host"] for h in c.get("/api/thresholds/overview", headers=op).json()["hosts"]] == ["prd-web-01"]
        assert c.get("/api/thresholds/state", headers=op).json() == []
        assert c.post("/api/thresholds", json={"scope": "all", "metric": "cpu", "warn": 50}, headers=op).status_code == 403
        pid = r.json()["id"]
        assert c.delete(f"/api/thresholds/{pid}", headers=admin).status_code == 204 and c.get("/api/thresholds", headers=admin).json() == []
