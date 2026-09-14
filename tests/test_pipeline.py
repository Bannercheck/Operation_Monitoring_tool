import io, zipfile
from pathlib import Path

import pytest

from signal_sprint import analysis as an
from signal_sprint.actions import ActionStore
from signal_sprint.format_detector import detect_format
from signal_sprint.loader import iter_bytes
from signal_sprint.normalize import auto_map, normalize_severity, parse_timestamp
from signal_sprint.parsers import PARSERS
from signal_sprint.pipeline import ingest, ingest_path
from signal_sprint.profiler import profile, profile_text

ROOT = Path(__file__).resolve().parents[1]
JSONL = ('{"ts":"2026-09-16T14:31:00Z","level":"error","service":"payment-api","host":"api-01","msg":"DB timeout user=1","trace":{"id":"abc"}}\n'
         '{"ts":"2026-09-16T14:32:10Z","level":"info","service":"payment-api","msg":"ok"}\n')
CSV = "time,severity,alert,host\n2026-09-16 14:35:00,critical,PAYMENT_FAILURE,prd-01\n2026-09-16 14:36:00,warning,CPU_HIGH,prd-02\n"
SYSLOG = "Sep 16 14:31:02 db-01 postgres[123]: latency increased\n<11>Sep 16 14:31:05 db-01 postgres[123]: connection reset\n"
KV = 'ts=2026-09-16T14:33:00Z level=warn svc=checkout msg="HTTP 500 for /pay" req=1\nts=2026-09-16T14:34:00Z level=warn svc=checkout msg="HTTP 500 for /pay" req=2\n'
TEXT = "2026-09-16 14:40:01,123 ERROR com.acme.Checkout: NullPointerException\n2026-09-16 14:40:02 INFO started\nno timestamp here\n"


def parse(fmt, text, name="f"):
    return list(PARSERS[fmt].parse(text, name))


def test_detect_formats():
    assert detect_format(JSONL)[0] == "jsonl" and detect_format('[{"a": 1}]')[0] == "json"
    assert detect_format(CSV)[0] == "csv" and detect_format(SYSLOG)[0] == "syslog"
    assert detect_format(KV)[0] == "kv" and detect_format(TEXT)[0] == "text"


def test_parsers():
    o = parse("jsonl", JSONL, "app.jsonl")[0]
    assert (o.severity, o.service, o.host, o.attributes, o.ref) == ("ERROR", "payment-api", "api-01", {"trace.id": "abc"}, "app.jsonl:1")
    o = parse("csv", CSV, "alerts.csv")
    assert [x.severity for x in o] == ["CRITICAL", "WARN"] and o[0].message == "PAYMENT_FAILURE" and o[0].kind == "alert"
    o = parse("syslog", SYSLOG)
    assert (o[0].host, o[0].service, o[0].message, o[1].severity) == ("db-01", "postgres", "latency increased", "ERROR")
    o = parse("kv", KV)[0]
    assert (o.service, o.severity, o.message, o.attributes) == ("checkout", "WARN", "HTTP 500 for /pay", {"req": "1"})
    o = parse("text", TEXT)
    assert (o[0].severity, o[0].service, o[0].message) == ("ERROR", "com.acme.Checkout", "NullPointerException")
    assert o[2].timestamp == o[1].timestamp


def test_auto_map_by_values_and_explicit_mapping():
    rows = [{"when": "2026-09-16 14:00:00", "lvl": "error", "what": "disk full on web-01"}]
    m = auto_map(list(rows[0]), rows)
    assert m["timestamp"] == "when" and m["severity"] == "lvl" and m["message"] == "what"
    o = list(PARSERS["csv"].parse("a,b,c\n2026-09-16 14:00:00,disk full,web-01\n", "x", {"timestamp": "a", "message": "b", "host": "c"}))[0]
    assert (o.message, o.host, o.timestamp.hour) == ("disk full", "web-01", 14)


def test_helpers():
    assert parse_timestamp("2026-09-16T14:31:00Z").minute == 31
    assert parse_timestamp(1789310000000).year == 2026
    assert parse_timestamp("16.09.2026 14:31:00").day == 16
    assert parse_timestamp("nonsense") is None
    assert normalize_severity("P1") == "CRITICAL" and normalize_severity("3") == "ERROR"
    assert an.template_of("DB timeout for user=123 ip=10.4.2.1 request=898123 id=8f1e2d3c4b5a69788f1e2d3c") == \
        "db timeout for user=<n> ip=<ip> request=<n> id=<hex>"


def test_zip_and_targz_ingest():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("alerts.csv", CSV); zf.writestr("logs/app.jsonl", JSONL); zf.writestr("db.log", SYSLOG)
    obs, report = ingest(iter_bytes("b.zip", buf.getvalue()))
    assert len(obs) == 6 and {r["format"] for r in report} == {"csv", "jsonl", "syslog"}
    assert obs == sorted(obs, key=lambda o: o.timestamp)
    import tarfile
    tbuf = io.BytesIO()
    with tarfile.open(fileobj=tbuf, mode="w:gz") as tf:
        data = KV.encode(); info = tarfile.TarInfo("kv.log"); info.size = len(data); tf.addfile(info, io.BytesIO(data))
    obs, _ = ingest(iter_bytes("b.tar.gz", tbuf.getvalue()))
    assert len(obs) == 2 and obs[0].parser == "kv"


@pytest.fixture(scope="module")
def demo():
    obs, report = ingest_path(str(ROOT / "samples" / "demo_mixed.zip"))
    return an.Analysis(obs, report), profile(obs, report)


def test_profile(demo):
    _, p = demo
    txt = profile_text(p)
    assert p["records"] == 916 and "I found: 4 files" in txt and "Detected entities" in txt
    assert p["probable_sources"]["alert"] == 3 and p["time_range"]["minutes"] > 40


def test_signals_and_burst(demo):
    a, _ = demo
    assert a.funnel()["reduction"] > 50
    bg = next(s for s in a.signals if s.template.startswith("post <path> <n> in"))
    assert bg.count > 400 and bg.burst_score < 0.1
    spike = next(s for s in a.signals if "connection timeout" in s.template)
    assert spike.burst_score >= 0.9 and spike.baseline_rate == 0
    assert spike.why()["reason"].startswith("same normalized") and spike.why()["confidence"] == 1.0


def test_incident_chain_root_cause_and_rationale(demo):
    a, _ = demo
    inc = a.incidents[0]
    root = a.signal_by_id[inc.root_cause_signal]
    assert "duration" in root.template and root.services == ["postgres"]
    assert {"payment-api", "checkout-api", "postgres"} <= set(inc.affected_services)
    assert inc.severity == "critical" and len(inc.signal_ids) >= 4 and inc.timeline[0]["role"] == "root cause"
    assert abs(sum(f.contribution for f in inc.factors) - inc.score) < 1e-6
    assert inc.links and inc.recommendations
    assert len(a.incidents) == 2 and "no space" in a.signal_by_id[a.incidents[1].root_cause_signal].template
    md = an.postmortem_md(inc, a.signal_by_id)
    assert md.startswith("# Postmortem INC-1") and "## Why this decision" in md and "db.log:" in md
    assert "[db.log:" in an.llm_prompt(inc, a.signal_by_id)


def test_action_store(tmp_path):
    st = ActionStore(str(tmp_path / "a.db"))
    a = st.create("INC-1", "Investigate DB connectivity degradation", "P1", "Database Team", "Check pool exhaustion", "S7")
    assert a["status"] == "open" and a["priority"] == "P1"
    assert st.update(a["id"], status="in_progress")["status"] == "in_progress"
    with pytest.raises(ValueError):
        st.update(a["id"], status="bogus")
    assert st.list("INC-1")[0]["owner"] == "Database Team"
    st.delete(a["id"]); assert st.list() == []


def test_real_world_shapes_nested_json_turkish_csv_kv_prefix():
    """Alertmanager-style nested JSON, Turkish-header CSV with dd.mm.yyyy, kv log with leading time/level/logger."""
    import json
    alerts = json.dumps({"status": "success", "data": {"alerts": [
        {"labels": {"alertname": "DBLatencyHigh", "severity": "warning", "job": "postgres", "instance": "db-01:9187"},
         "annotations": {"summary": "p99 latency 6.2s on db-01"}, "activeAt": "2026-09-16T14:31:20Z"}]}})
    csv_tr = "Kayıt No;Oluşturma Zamanı;Öncelik;Uygulama;Özet\nINC1;16.09.2026 14:36:00;P1;payment-api;Ödeme hataları %38\n"
    kv = '2026-09-16 14:32:00 ERROR notification-service: broker=kafka-01 msg="timeout publishing to db-01 after 5000ms"\n'
    no_ts = '{"level":"info","msg":"no time here"}\n'
    obs, report = ingest(iter([("alerts.json", alerts), ("incidents.csv", csv_tr), ("svc.log", kv), ("nots.jsonl", no_ts)]))
    by = {o.source: o for o in obs}
    assert by["alerts.json"].timestamp.minute == 31 and by["alerts.json"].severity == "WARN" and by["alerts.json"].service == "postgres"
    assert by["alerts.json"].message.startswith("p99 latency") and by["alerts.json"].host == "db-01:9187"
    assert by["incidents.csv"].timestamp.day == 16 and by["incidents.csv"].severity == "CRITICAL" and by["incidents.csv"].service == "payment-api"
    assert by["svc.log"].timestamp.minute == 32 and by["svc.log"].severity == "ERROR" and by["svc.log"].service == "notification-service"
    assert by["svc.log"].message.startswith("timeout publishing")
    assert by["nots.jsonl"].timestamp == min(o.timestamp for o in obs) and by["nots.jsonl"].timestamp.year == 2026  # no epoch-0
    assert (max(o.timestamp for o in obs) - min(o.timestamp for o in obs)).total_seconds() < 3600


def test_compare_two_datasets():
    from signal_sprint.compare import compare
    import io, zipfile
    obs_a, rep_a = ingest_path(str(ROOT / "samples" / "demo_mixed.zip"))
    a = an.Analysis(obs_a, rep_a)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("app.jsonl", JSONL); zf.writestr("db.log", SYSLOG)
    obs_b, rep_b = ingest(iter_bytes("b.zip", buf.getvalue()))
    b = an.Analysis(obs_b, rep_b)
    c = compare(a, profile(obs_a, rep_a), b, profile(obs_b, rep_b))
    k = {r["metric"]: r for r in c["kpis"]}
    assert k["raw_events"]["a"] == 916 and k["raw_events"]["b"] == 4 and k["raw_events"]["delta"] == -912
    assert c["summary"]["only_a"] == 13 and c["summary"]["only_b"] >= 3 and c["summary"]["shared"] == 0
    assert {r["dataset"] for r in c["timeline"]} == {"A", "B"} and len(c["incidents_a"]) == 2
    c2 = compare(a, profile(obs_a, rep_a), a, profile(obs_a, rep_a))
    assert c2["summary"]["shared"] == 13 and all(r["delta"] == 0 for r in c2["kpis"] if r["delta"] is not None)


def test_incident_origin_timing_recovery(demo):
    a, _ = demo
    inc = a.incidents[0]
    assert inc.origin["files"]["app.jsonl"] == 60 and "postgres" in inc.origin["services"] and "syslog" in inc.origin["parsers"]
    assert inc.timing["first_signal"].endswith("14:31:00+00:00") and inc.timing["duration_s"] == 295 and inc.timing["error_count"] > 90
    assert inc.recovery["kind"] == "restart" and inc.recovery["evidence"].startswith("db.log:") and "ready to accept" in inc.recovery["what"]
    assert a.incidents[1].recovery["kind"] in ("stopped", "self_healed")
    o = a.obs_by_ref[inc.recovery["evidence"]]
    assert o.raw.startswith("Sep 16 14:36:20 db-01 postgres") and a.obs_by_ref["app.jsonl:540"].raw.startswith("{")
