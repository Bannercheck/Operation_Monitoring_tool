import io
import zipfile
from datetime import datetime

from app.ingest.detect import detect_format
from app.ingest.pipeline import ingest
from app.ingest.common import parse_timestamp, normalize_severity, resolve_mapping
from app.ingest.parsers import PARSERS

JSONL = (
    '{"ts":"2026-09-16T14:31:00Z","level":"error","service":"payment-api","host":"api-01","msg":"DB timeout user=1","trace":{"id":"abc"}}\n'
    '{"ts":"2026-09-16T14:32:10Z","level":"info","service":"payment-api","msg":"ok"}\n'
)
JSON_ARRAY = '[{"timestamp": 1789310000000, "severity": "WARNING", "message": "disk 91%"}]'
CSV = "time,severity,alert,host\n2026-09-16 14:35:00,critical,PAYMENT_FAILURE,prd-01\n2026-09-16 14:36:00,warning,CPU_HIGH,prd-02\n"
TSV = "ts\tlevel\tmsg\n2026-09-16 14:35:00\tERROR\tboom\n2026-09-16 14:36:00\tINFO\tfine\n"
SYSLOG = "Sep 16 14:31:02 db-01 postgres[123]: latency increased\n<11>Sep 16 14:31:05 db-01 postgres[123]: connection reset\n"
KV = 'ts=2026-09-16T14:33:00Z level=warn svc=checkout msg="HTTP 500 for /pay" req=1\nts=2026-09-16T14:34:00Z level=warn svc=checkout msg="HTTP 500 for /pay" req=2\n'
PLAIN = "2026-09-16 14:40:01,123 ERROR com.acme.Checkout: NullPointerException\n2026-09-16 14:40:02 INFO started\nno timestamp here\n"


def test_detect_each_format():
    assert detect_format(JSONL)[0] == "jsonl"
    assert detect_format(JSON_ARRAY)[0] == "jsonl"
    assert detect_format(CSV)[0] == "csv"
    assert detect_format(TSV)[0] == "csv"
    assert detect_format(SYSLOG)[0] == "syslog"
    assert detect_format(KV)[0] == "kv"
    assert detect_format(PLAIN)[0] == "plain"


def test_jsonl_parse_roles_and_attributes():
    obs = list(PARSERS["jsonl"].parse(JSONL, "app.jsonl"))
    assert len(obs) == 2
    o = obs[0]
    assert o.severity == "ERROR" and o.service == "payment-api" and o.host == "api-01"
    assert o.message == "DB timeout user=1"
    assert o.attributes == {"trace.id": "abc"}
    assert o.ref == "app.jsonl:1"
    assert o.timestamp.hour == 14 and o.timestamp.minute == 31


def test_json_array_epoch_millis():
    o = list(PARSERS["jsonl"].parse(JSON_ARRAY, "a.json"))[0]
    assert o.severity == "WARN" and o.timestamp.year == 2026


def test_csv_and_tsv():
    obs = list(PARSERS["csv"].parse(CSV, "alerts.csv"))
    assert [o.severity for o in obs] == ["CRITICAL", "WARN"]
    assert obs[0].host == "prd-01" and obs[0].line_no == 2
    assert obs[0].attributes["alert"] == "PAYMENT_FAILURE"
    obs = list(PARSERS["csv"].parse(TSV, "x.tsv"))
    assert obs[0].message == "boom" and obs[0].severity == "ERROR"


def test_syslog():
    obs = list(PARSERS["syslog"].parse(SYSLOG, "db.log"))
    assert len(obs) == 2
    assert obs[0].host == "db-01" and obs[0].service == "postgres"
    assert obs[0].message == "latency increased"
    assert obs[1].severity == "ERROR"  # <11> -> severity 3


def test_kv():
    obs = list(PARSERS["kv"].parse(KV, "kv.log"))
    assert obs[0].service == "checkout" and obs[0].severity == "WARN"
    assert obs[0].message == "HTTP 500 for /pay"
    assert obs[0].attributes == {"req": "1"}


def test_plain_fallback_carries_last_timestamp():
    obs = list(PARSERS["plain"].parse(PLAIN, "app.log"))
    assert obs[0].severity == "ERROR" and obs[0].service == "com.acme.Checkout"
    assert obs[0].message == "NullPointerException"
    assert obs[2].timestamp == obs[1].timestamp


def test_explicit_mapping_overrides_heuristics():
    text = "when,what,who\n2026-09-16 14:00:00,disk full,web-01\n"
    obs = list(PARSERS["csv"].parse(text, "x.csv", mapping={"timestamp": "when", "message": "what", "host": "who"}))
    assert obs[0].message == "disk full" and obs[0].host == "web-01" and obs[0].timestamp.hour == 14


def test_pipeline_zip_sorted_and_reported():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("alerts.csv", CSV)
        zf.writestr("logs/app.jsonl", JSONL)
        zf.writestr("db.log", SYSLOG)
    obs, report = ingest("bundle.zip", buf.getvalue())
    assert len(obs) == 6
    assert {r["format"] for r in report} == {"csv", "jsonl", "syslog"}
    assert all(r["rows"] > 0 for r in report)
    assert obs == sorted(obs, key=lambda o: o.timestamp)


def test_helpers():
    assert parse_timestamp("2026-09-16T14:31:00Z").minute == 31
    assert parse_timestamp("16/09/2026 14:31:00").day == 16
    assert parse_timestamp("nonsense") is None
    assert normalize_severity("Warning") == "WARN" and normalize_severity("P1") == "CRITICAL"
    assert resolve_mapping(["event_time", "log_level", "text"])["timestamp"] == "event_time"
