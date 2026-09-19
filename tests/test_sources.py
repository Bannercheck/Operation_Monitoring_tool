"""Pull sources: fake Elasticsearch / Loki / Splunk / Graylog / HTTP servers, field mapping, cursors, dedupe, poller → live store."""
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from watchover import sources as S
from watchover.knowledge import Knowledge
from watchover.live import LiveStore

UTC = timezone.utc
NOW = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)


class Fake(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def _reply(self, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        Fake.seen.append(("GET", self.path, dict(self.headers)))
        if "/loki/api/v1/query_range" in self.path:
            ns = int(NOW.timestamp() * 1e9)
            return self._reply({"data": {"result": [{"stream": {"host": "web-01", "app": "nginx", "level": "error"},
                                                     "values": [[str(ns), "upstream timed out"], [str(ns + 1000), '{"msg":"json line","service":"api"}']]}]}})
        if "/api/search/universal/absolute" in self.path:
            return self._reply({"messages": [{"message": {"message": "disk 91%", "source": "db-01", "level": 3, "timestamp": NOW.isoformat(), "facility": "kern"}}]})
        if self.path.startswith("/events"):
            return self._reply({"data": {"events": [{"time": int(NOW.timestamp()), "hostname": "app-02", "severity": "warn", "text": "queue depth 900", "program": "worker"}]}})
        if self.path.startswith("/plain"):
            return self._reply(b"Sep 19 10:00:00 app-03 sshd[1]: error: auth failed\n", "text/plain")
        self.send_response(404); self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); body = self.rfile.read(n)
        Fake.seen.append(("POST", self.path, dict(self.headers), body))
        if self.path.endswith("/_search"):
            q = json.loads(body)
            since = q["query"]["range"]["@timestamp"]["gt"]
            hits = [{"_source": {"@timestamp": NOW.isoformat(), "message": "connection refused", "host": {"name": "db-01"}, "service": {"name": "postgres"}, "log": {"level": "error"}}},
                    {"_source": {"@timestamp": (NOW + timedelta(seconds=5)).isoformat(), "message": "slow query 2.1s", "agent": {"hostname": "db-02"}, "log": {"level": "warn"}}}]
            hits = [h for h in hits if h["_source"]["@timestamp"] > since]
            return self._reply({"hits": {"hits": hits}})
        if self.path.endswith("/services/search/jobs/export"):
            lines = [json.dumps({"result": {"_raw": "ERROR payment failed", "host": "pay-01", "sourcetype": "app", "_time": NOW.isoformat()}}), json.dumps({"preview": False})]
            return self._reply("\n".join(lines).encode())
        self.send_response(404); self.end_headers()


def _server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_fetchers_map_fields_and_cursors():
    srv, base = _server()
    since = NOW - timedelta(minutes=5)
    try:
        es = S.Source(None, "es", "elasticsearch", base, "logs-*", "apikey", "", "k3y")
        rows, cur = S.fetch(es, since)
        assert [r["host"] for r in rows] == ["db-01", "db-02"] and rows[0]["service"] == "postgres" and rows[0]["level"] == "error"
        assert cur == (NOW + timedelta(seconds=5)).isoformat()
        assert Fake.seen[-1][2]["Authorization"] == "ApiKey k3y" and "/logs-*/_search" in Fake.seen[-1][1]
        assert S.fetch(es, S._parse_ts(cur))[0] == []                        # cursor moved: nothing new

        lk = S.Source(None, "loki", "loki", base, '{app="nginx"}', headers="X-Scope-OrgID: t1")
        rows, cur = S.fetch(lk, since)
        assert rows[0]["host"] == "web-01" and rows[0]["service"] == "nginx" and rows[1]["service"] == "api" and rows[1]["msg"] == "json line"
        assert {k.lower(): v for k, v in Fake.seen[-1][2].items()}["x-scope-orgid"] == "t1" and S._parse_ts(cur) > NOW

        sp = S.Source(None, "splunk", "splunk", base, "index=main", "bearer", "", "tok")
        rows, cur = S.fetch(sp, since)
        assert rows[0]["host"] == "pay-01" and rows[0]["service"] == "app" and "payment failed" in rows[0]["msg"]
        assert b"search+index%3Dmain" in Fake.seen[-1][3] and Fake.seen[-1][2]["Authorization"] == "Bearer tok"

        gl = S.Source(None, "gl", "graylog", base, "*", "apikey", "", "abc")
        rows, cur = S.fetch(gl, since)
        assert rows[0]["host"] == "db-01" and rows[0]["service"] == "kern" and rows[0]["level"] == "3"
        assert Fake.seen[-1][2]["Authorization"].startswith("Basic ")

        ht = S.Source(None, "api", "http", base + "/events?since={since}", "data.events")
        rows, cur = S.fetch(ht, since)
        assert rows[0]["host"] == "app-02" and rows[0]["service"] == "worker" and rows[0]["level"] == "warn" and rows[0]["ts"] == NOW.isoformat()
        assert "since=2026-09-19T09" in Fake.seen[-1][1]
        rows, _ = S.fetch(S.Source(None, "txt", "http", base + "/plain"), since)
        assert rows[0]["raw"] and "auth failed" in rows[0]["msg"]
    finally:
        srv.shutdown(); srv.server_close()


def test_store_and_poller(tmp_path):
    srv, base = _server()
    kb = Knowledge(str(tmp_path / "k.db")); store = S.SourceStore(kb); live = LiveStore()
    try:
        src = store.add(S.Source(None, "es-prod", "elasticsearch", base, "logs-*", env="PROD", site="IST", interval=5, lookback_min=60 * 24 * 365))
        assert src.id and store.list()[0].name == "es-prod" and store.get(src.id).env == "PROD"
        import pytest
        with pytest.raises(ValueError):
            store.add(S.Source(None, "ES-PROD", "elasticsearch", base))
        p = S.Poller(store, live)
        assert p.poll_one(src) == 2
        obs = live.snapshot()
        assert len(obs) == 2 and {o.host for o in obs} == {"db-01", "db-02"} and obs[0].environment == "PROD" and obs[0].attributes["agent"] == "es-prod" and obs[0].attributes["site"] == "IST"
        src = store.get(src.id)
        assert src.events == 2 and src.polls == 1 and src.last_ok and not src.last_err and S._parse_ts(src.cursor) == NOW + timedelta(seconds=5)
        assert p.poll_one(src) == 0 and len(live.snapshot()) == 2                 # cursor: no duplicates
        store.update(src.id, url="http://127.0.0.1:1")                              # unreachable → error recorded, poller survives
        p.due.clear(); p.start(); import time; time.sleep(3)
        assert "Error" in (store.get(src.id).last_err or "") and p.thread.is_alive()
        p.stop.set()
        ok, msg, sample = S.test_source(store.get(src.id))
        assert not ok and "Error" in msg
        store.delete(src.id); assert store.list() == []
    finally:
        srv.shutdown(); srv.server_close()


def test_row_from_record_variants():
    r = S.row_from_record({"ts": 1758276000000, "lvl": "INFO", "server": "x1", "logger": "core", "line": "hello"})
    assert r["ts"].startswith("2025-09-19T") and r["level"] == "INFO" and r["host"] == "x1" and r["service"] == "core" and r["msg"] == "hello"
    r = S.row_from_record({"foo": 1})
    assert r["msg"] == '{"foo": 1}' and r["host"] == ""
