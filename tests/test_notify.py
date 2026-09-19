"""E-mail / SMS alerting: recipients, groups, target resolution, rules, cooldown, evaluation against a live store."""
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from watchover import notify as wo_notify
from watchover.knowledge import Knowledge
from watchover.live import LiveStore


@pytest.fixture
def kb(tmp_path):
    return Knowledge(str(tmp_path / "kb.db"))


def test_recipients_groups_resolve(kb):
    n = wo_notify.Notifier(kb)
    n.add_group("ops", "ops-team@corp.com")
    n.add_recipient("Ayşe Yılmaz", "ayse@corp.com", "+905551112233", "ops, dba")
    n.add_recipient("Mehmet", "", "+905550000000", "dba")
    with pytest.raises(ValueError):
        n.add_recipient("nobody")
    emails, phones = n.resolve("ops, @dba, cto@corp.com, +90 555 999")
    assert emails == ["ayse@corp.com", "cto@corp.com", "ops-team@corp.com"]
    assert phones == ["+905550000000", "+905551112233", "+90555999"]
    assert n.resolve("ayşe yılmaz") == (["ayse@corp.com"], ["+905551112233"])
    n.update_recipient(n.recipients()[0]["id"], enabled=False)
    assert n.resolve("ops") == (["ops-team@corp.com"], [])
    n.delete_group(n.groups()[0]["id"])
    assert n.groups() == []


def test_dispatch_logs_failure_and_cooldown(kb):
    calls = []
    n = wo_notify.Notifier(kb, lambda: {"email": {"host": "smtp.invalid", "port": 1}, "sms": {}})
    n.add_recipient("A", "a@corp.com")
    rid = n.add_rule("r", "errors", 5, "prod", "high", "email,sms", "A", cooldown_min=30)
    rule = n.rules()[0]
    assert rule["id"] == rid and rule["env"] == "prod"
    # SMTP unreachable -> failure logged, not raised
    row = n.dispatch(rule, "errors:prod", "boom", "body")
    assert row["ok"] == 0 and "email" in row["detail"]
    assert n.alerts()[0]["title"] == "boom"
    # successful sends are deduplicated within the cooldown
    assert n.dispatch(rule, "errors:prod", "boom", "body") == {"skipped": "cooldown"}      # a failed attempt also waits out the cooldown
    wo_notify.send_email = lambda cfg, to, subject, text, html=None: calls.append(to)
    row = n.dispatch(rule, "errors:prod", "boom", "body", force=True)
    assert row["ok"] == 1 and calls == [["a@corp.com"]]
    assert n.dispatch(rule, "errors:prod", "boom", "body") == {"skipped": "cooldown"}
    assert n.dispatch(rule, "errors:prod", "boom", "body", force=True)["ok"] == 1
    with pytest.raises(ValueError):
        n.add_rule("x", "nope")
    n.update_rule(rid, enabled=False); assert n.rules()[0]["enabled"] == 0
    n.delete_rule(rid); assert n.rules() == []


def test_sms_http_gateway(kb):
    got = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            got["path"], got["auth"] = self.path, self.headers.get("Authorization")
            got["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = {"preset": "http", "url": f"http://127.0.0.1:{srv.server_port}/send", "method": "POST", "auth": "bearer", "token": "sek", "body": '{"to": "{to}", "text": "{msg}"}', "content_type": "application/json"}
    assert wo_notify.send_sms(cfg, "+90555", 'hello "ops"') == "ok"
    assert got["auth"] == "Bearer sek" and got["body"] == {"to": "+90555", "text": 'hello "ops"'}
    n = wo_notify.Notifier(kb, lambda: {"sms": cfg})
    ok, msg = n.test_channel("sms", "+1")
    assert ok and msg == "ok"
    srv.shutdown()


def test_engine_evaluates_live_feed(kb):
    ls = LiveStore()
    sent = []
    wo_notify.send_email = lambda cfg, to, subject, text, html=None: sent.append(subject)
    n = wo_notify.Notifier(kb, lambda: {"email": {"host": "smtp.corp"}, "sms": {}})
    n.add_recipient("Ops", "ops@corp.com")
    n.add_rule("errors", "errors", 2, "", "high", "email", "Ops")
    n.add_rule("slo", "slo", 0, "", "critical", "email", "Ops")
    n.add_rule("cpu", "metric", 0, "", "high", "email", "Ops")
    lines = "\n".join(json.dumps({"level": "error", "msg": f"HTTP 500 upstream {i}", "host": "prd-api-01", "service": "payment-api"}) for i in range(30)) + "\n"
    ls.ingest("app.jsonl", lines.encode(), agent="t")
    ls.ingest("metrics.jsonl", json.dumps({"host": "prd-api-01", "cpu": 97, "memory": 40}).encode() + b"\n", agent="t")
    eng = wo_notify.AlertEngine(n, ls)
    out = eng.evaluate()
    titles = [o.get("title", "") for o in out]
    assert any("ERROR+ per minute" in x for x in titles) and any("below SLO" in x for x in titles) and any("cpu 97%" in x for x in titles)
    assert all(o.get("ok") == 1 for o in out) and len(sent) == 3
    assert eng.evaluate() and all(o.get("skipped") == "cooldown" for o in eng.evaluate())
    assert eng.runs == 3 and eng.last_run


def test_turkish_gateways_number_format_and_rejection(monkeypatch):
    assert wo_notify.format_number("+90 555 111 22 33", "digits") == "905551112233"
    assert wo_notify.format_number("0555 111 2233", "e164") == "+905551112233"
    assert wo_notify.format_number("5551112233", "national") == "05551112233"
    assert wo_notify.format_number("0090 555 111 2233", "digits") == "905551112233"
    seen = {}

    class R:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return self.body

    def fake_open(req, timeout=0):
        seen["url"], seen["body"], seen["ctype"] = req.full_url, req.data, req.headers.get("Content-type")
        return R(seen.pop("reply", b"ok"))
    monkeypatch.setattr(wo_notify.urllib.request, "urlopen", fake_open)
    # fixed preset: the catalogue URL wins over cfg, the number is written as digits, Netgsm's 200-with-error-code is a rejection
    cfg = {"preset": "netgsm", "url": "http://evil", "user": "850", "password": "p w", "from": "WATCHOVER"}
    seen["reply"] = b"00 1234567"
    assert wo_notify.send_sms(cfg, "+90 555 111 22 33", "merhaba dünya").startswith("00")
    assert seen["url"].startswith("https://api.netgsm.com.tr/") and "gsmno=905551112233" in seen["url"] and "password=p%20w" in seen["url"]
    seen["reply"] = b"30"
    with pytest.raises(RuntimeError):
        wo_notify.send_sms(cfg, "+905551112233", "x")
    # XML gateway escapes the text; JSON gateway carries the digits number
    seen["reply"] = b"$123"
    wo_notify.send_sms({"preset": "mutlucell", "user": "u", "password": "p", "from": "ORG"}, "05551112233", 'a<b & "c"')
    assert b"<metin>a&lt;b &amp; &quot;c&quot;</metin><nums>905551112233</nums>" in seen["body"] and seen["ctype"] == "text/xml"
    wo_notify.send_sms({"preset": "verimor", "user": "u", "password": "p", "from": "ORG"}, "+905551112233", "m")
    assert json.loads(seen["body"]) == {"username": "u", "password": "p", "source_addr": "ORG", "messages": [{"msg": "m", "dest": "905551112233"}]}
    # operator presets need the contract's endpoint
    with pytest.raises(RuntimeError):
        wo_notify.send_sms({"preset": "turkcell", "user": "u", "password": "p"}, "+905551112233", "m")
    for k, pre in wo_notify.SMS_PRESETS.items():
        assert pre["fields"] and pre.get("number") in ("e164", "digits", "national"), k
