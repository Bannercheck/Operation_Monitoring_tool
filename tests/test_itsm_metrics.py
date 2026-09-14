"""ITSM fetchers against fake ServiceNow / Jira servers, ticket correlation, metric ingestion and SLO math."""
import json
import random
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from signal_sprint.itsm import Ticket, correlate, demo_tickets, fetch_generic, fetch_jira, fetch_servicenow
from signal_sprint.live import LiveStore, simulate_batch, simulate_metrics

UTC = timezone.utc


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body):
        data = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/now/table/incident"):
            assert self.headers.get("Authorization", "").startswith("Basic ")
            assert "sysparm_query=active%3Dtrue" in self.path
            self._send({"result": [{"number": "INC001", "short_description": "payment-api timeouts", "description": "db-01 slow", "priority": "1",
                                    "state": "In Progress", "sys_created_on": "2026-09-16 14:33:00", "cmdb_ci": {"display_value": "payment-api"},
                                    "assigned_to": {"display_value": "SRE"}, "sys_id": "abc"}]})
        elif self.path.startswith("/rest/api/3/search"):
            assert self.headers.get("Authorization") == "Bearer tok"
            self._send({"issues": [{"key": "OPS-7", "fields": {"summary": "Checkout 500s", "description": {"content": [{"content": [{"text": "checkout-api errors"}]}]},
                                                                  "priority": {"name": "High"}, "status": {"name": "Open"}, "created": "2026-09-16T14:34:00.000+0000",
                                                                  "assignee": {"displayName": "Ayşe"}, "components": [{"name": "checkout-api"}]}}]})
        elif self.path.startswith("/tickets"):
            self._send({"data": {"items": [{"id": 9, "subject": "Disk full on worker-01", "opened_at": "2026-09-16T14:10:00Z", "prio": "P3", "state": "new"}]}})
        else:
            self._send({})


def serve():
    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_fetchers():
    srv, base = serve()
    try:
        sn = fetch_servicenow(base, "u", "p")
        assert sn[0].id == "INC001" and sn[0].priority == "P1" and sn[0].service == "payment-api" and sn[0].created.minute == 33
        jr = fetch_jira(base, token="tok")
        assert jr[0].id == "OPS-7" and "checkout-api errors" in jr[0].description and jr[0].service == "checkout-api"
        gn = fetch_generic(base + "/tickets", {}, {"title": "subject", "created": "opened_at", "priority": "prio", "status": "state"}, "data.items")
        assert gn[0].title == "Disk full on worker-01" and gn[0].priority == "P3" and gn[0].created.hour == 14
    finally:
        srv.shutdown()


def test_correlation_ranks_related_tickets():
    now = datetime.now(UTC)
    tickets = demo_tickets(now)
    signals = [{"id": "L1", "template": "connection timeout to db-<n> after <n>ms", "services": ["payment-api"], "hosts": ["prd-api-01"],
                "severity": "ERROR", "onset": now - timedelta(minutes=7), "count": 40}]
    breaches = [{"metric": "cpu", "host": "db-01", "value": 93.0, "threshold": 85}]
    incidents = [{"id": "INC-1", "title": "x", "services": ["search-api"], "hosts": [], "started_at": now - timedelta(minutes=120)}]
    ranked = correlate(tickets, signals, breaches, incidents)
    assert ranked[0].id == "INC0012345" and ranked[0].relevance >= 0.8 and "L1" in ranked[0].related
    cpu = next(x for x in ranked if x.id == "INC0012346")
    assert "cpu@db-01" in cpu.related and any("93%" in r for r in cpu.reasons)
    search = next(x for x in ranked if x.id == "INC0012340")
    assert "INC-1" in search.related
    vpn = next(x for x in ranked if x.id == "REQ0004410")
    assert vpn.relevance == 0.0 and ranked[-1].relevance <= ranked[0].relevance


def test_metrics_and_slo():
    store = LiveStore()
    rng = random.Random(3)
    state = {}
    for _ in range(3):
        store.ingest("metrics.jsonl", simulate_metrics(rng, state, incident=True), "sim")
    store.ingest("events.jsonl", simulate_batch(rng, 60, incident=False), "sim")
    ms = store.metric_stats(15)
    assert ms["samples"] == 15 * 3 and ms["summary"]["cpu"]["hosts"] == 5 and ms["summary"]["disk"]["avg"] is not None
    assert all(len(store.metrics) for _ in [0]) and not any(o.attributes.get("cpu") for o in store.snapshot())  # metrics are not events
    slo = store.slo(15)
    assert slo["total"] == 60 and 0.8 <= slo["availability"] <= 1.0 and slo["p95_ms"] is not None
    assert slo["slo"]["availability"] == 0.999 and isinstance(slo["slo_ok"], bool)
    store.ingest("m2.jsonl", b'{"ts":"%s","metric":"cpu","value":97,"host":"db-01"}\n' % datetime.now(UTC).isoformat().encode(), "sim")
    assert any(b["host"] == "db-01" and b["metric"] == "cpu" for b in store.metric_stats(15)["breaches"])
