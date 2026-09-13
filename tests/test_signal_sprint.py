import io, zipfile
from pathlib import Path
import pytest
import signal_sprint as ss

ROOT = Path(__file__).resolve().parents[1]
JSONL = ('{"ts":"2026-09-16T14:31:00Z","level":"error","service":"payment-api","host":"api-01","msg":"DB timeout user=1","trace":{"id":"abc"}}\n'
         '{"ts":"2026-09-16T14:32:10Z","level":"info","service":"payment-api","msg":"ok"}\n')
CSV = "time,severity,alert,host\n2026-09-16 14:35:00,critical,PAYMENT_FAILURE,prd-01\n2026-09-16 14:36:00,warning,CPU_HIGH,prd-02\n"
SYSLOG = "Sep 16 14:31:02 db-01 postgres[123]: latency increased\n<11>Sep 16 14:31:05 db-01 postgres[123]: connection reset\n"
KV = 'ts=2026-09-16T14:33:00Z level=warn svc=checkout msg="HTTP 500 for /pay" req=1\nts=2026-09-16T14:34:00Z level=warn svc=checkout msg="HTTP 500 for /pay" req=2\n'
PLAIN = "2026-09-16 14:40:01,123 ERROR com.acme.Checkout: NullPointerException\n2026-09-16 14:40:02 INFO started\nno timestamp here\n"


def parse(fmt, text, name="f"):
    return list(ss.to_observations(ss.PARSERS[fmt](text), name, fmt))


def test_detect_formats():
    assert ss.detect_format(JSONL)[0] == "jsonl"
    assert ss.detect_format('[{"a": 1}]')[0] == "json"
    assert ss.detect_format(CSV)[0] == "csv"
    assert ss.detect_format(SYSLOG)[0] == "syslog"
    assert ss.detect_format(KV)[0] == "kv"
    assert ss.detect_format(PLAIN)[0] == "plain"


def test_parsers():
    o = parse("jsonl", JSONL, "app.jsonl")[0]
    assert (o.severity, o.service, o.host, o.attributes, o.ref) == ("ERROR", "payment-api", "api-01", {"trace.id": "abc"}, "app.jsonl:1")
    o = parse("csv", CSV)
    assert [x.severity for x in o] == ["CRITICAL", "WARN"] and o[0].message == "PAYMENT_FAILURE" and o[0].line_no == 2
    o = parse("syslog", SYSLOG)
    assert (o[0].host, o[0].service, o[0].message, o[1].severity) == ("db-01", "postgres", "latency increased", "ERROR")
    o = parse("kv", KV)[0]
    assert (o.service, o.severity, o.message, o.attributes) == ("checkout", "WARN", "HTTP 500 for /pay", {"req": "1"})
    o = parse("plain", PLAIN)
    assert (o[0].severity, o[0].service, o[0].message) == ("ERROR", "com.acme.Checkout", "NullPointerException")
    assert o[2].timestamp == o[1].timestamp


def test_explicit_mapping():
    o = list(ss.to_observations(ss.PARSERS["csv"]("when,what,who\n2026-09-16 14:00:00,disk full,web-01\n"), "x", "csv",
                                {"timestamp": "when", "message": "what", "host": "who"}))[0]
    assert (o.message, o.host, o.timestamp.hour) == ("disk full", "web-01", 14)


def test_helpers():
    assert ss.parse_timestamp("2026-09-16T14:31:00Z").minute == 31
    assert ss.parse_timestamp(1789310000000).year == 2026
    assert ss.parse_timestamp("nonsense") is None
    assert ss.normalize_severity("P1") == "CRITICAL" and ss.normalize_severity("3") == "ERROR"
    assert ss.template_of("DB timeout for user=123 ip=10.4.2.1 request=898123 id=8f1e2d3c4b5a69788f1e2d3c") == \
        "db timeout for user=<n> ip=<ip> request=<n> id=<hex>"


def test_zip_ingest():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("alerts.csv", CSV); zf.writestr("logs/app.jsonl", JSONL); zf.writestr("db.log", SYSLOG)
    obs, report = ss.ingest(ss.iter_files("b.zip", buf.getvalue()))
    assert len(obs) == 6 and {r["format"] for r in report} == {"csv", "jsonl", "syslog"}
    assert obs == sorted(obs, key=lambda o: o.timestamp)


@pytest.fixture(scope="module")
def demo():
    path = ROOT / "samples" / "demo_mixed.zip"
    if not path.exists():
        import runpy; runpy.run_path(str(ROOT / "samples" / "make_demo.py"))
    return ss.analyze_path(str(path))


def test_demo_reduction_and_signals(demo):
    o = demo.overview()
    assert o["raw_events"] > 800 and o["signals"] < 15 and o["reduction"] > 50
    bg = next(s for s in demo.signals if s.template.startswith("post <path> <n> in"))
    assert bg.count > 400 and bg.burst_score < 0.1  # steady traffic is not a burst
    spike = next(s for s in demo.signals if "connection timeout" in s.template)
    assert spike.burst_score >= 0.9 and spike.baseline_rate == 0


def test_demo_incidents_chain_and_root_cause(demo):
    inc = demo.incidents[0]
    root = demo.signal_by_id[inc.root_cause_signal]
    assert "duration" in root.template and root.services == ["postgres"]  # DB latency is the origin
    assert {"payment-api", "checkout-api", "postgres"} <= set(inc.affected_services)
    assert inc.severity == "critical" and len(inc.signal_ids) >= 4
    assert [t["role"] for t in inc.timeline][0] == "root cause"
    assert abs(sum(f.contribution for f in inc.factors) - inc.score) < 1e-6
    assert all(ref.split(":")[0] in {"db.log", "app.jsonl", "checkout.log", "alerts.csv"} for ref in inc.evidence)
    disk = demo.incidents[1]
    assert "no space" in demo.signal_by_id[disk.root_cause_signal].template and disk.affected_hosts == ["worker-02"]
    assert len(demo.incidents) == 2  # steady cache-miss warnings are not an incident


def test_postmortem_and_prompt(demo):
    inc = demo.incidents[0]
    md = ss.postmortem_md(inc, demo.signal_by_id)
    assert md.startswith("# Postmortem INC-1") and "## Why this decision" in md and "db.log:" in md
    assert "[db.log:" in ss.claude_prompt(inc, demo.signal_by_id)


def test_action_store(tmp_path):
    st = ss.ActionStore(str(tmp_path / "a.db"))
    a = st.create("INC-1", "Failover db-01", "tayfur")
    assert a["status"] == "open"
    assert st.update(a["id"], status="done")["status"] == "done"
    with pytest.raises(ValueError):
        st.update(a["id"], status="bogus")
    st.delete(a["id"])
    assert st.list() == []
