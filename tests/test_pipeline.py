import io, zipfile
from pathlib import Path

import pytest

from watchover import analysis as an
from watchover.actions import ActionStore
from watchover.format_detector import detect_format
from watchover.loader import iter_bytes
from watchover.normalize import auto_map, normalize_severity, parse_timestamp
from watchover.parsers import PARSERS
from watchover.pipeline import ingest, ingest_path
from watchover.profiler import profile, profile_text

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
    from watchover.compare import compare
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


def test_environment_and_origin_roles():
    from watchover.normalize import auto_map, infer_environment, normalize_environment
    m = auto_map(["ts", "level", "msg", "env", "source"], [{"env": "Production", "source": "kafka"}])
    assert m["environment"] == "env" and m["origin"] == "source"
    m2 = auto_map(["ts", "msg", "stage"], [{"stage": "staging"}, {"stage": "prod"}])
    assert m2["environment"] == "stage"
    m3 = auto_map(["ts", "msg", "x"], [{"x": "test"}, {"x": "dev"}, {"x": "prod"}])   # value-based guess
    assert m3["environment"] == "x"
    assert normalize_environment("PRD") == "prod" and normalize_environment("prod-eu-1") == "prod" and normalize_environment("Test") == "test"
    assert infer_environment("prd-api-01") == "prod" and infer_environment("dev-worker-3") == "dev" and infer_environment("payment-api") == ""
    o = parse("csv", "time,level,environment,source,message\n2026-09-16 14:00:00,error,Test,kafka-consumer,timeout\n2026-09-16 14:01:00,info,Prod,app,ok\n")
    assert (o[0].environment, o[0].origin, o[1].environment) == ("test", "kafka-consumer", "prod")
    obs, rep = ingest_path(str(ROOT / "samples" / "demo_mixed.zip"))
    p = profile(obs, rep)
    assert p["environments"]["prod"]["events"] > 500 and 0 < p["environments"]["prod"]["error_rate"] < 1
    a = an.Analysis(obs, rep)
    assert "prod" in a.incidents[0].origin["environments"]


def test_error_map_from_demo():
    from watchover.graph import build_map, to_dot, dependency_edges
    obs, rep = ingest_path(str(ROOT / "samples" / "demo_mixed.zip"))
    a = an.Analysis(obs, rep)
    deps = dependency_edges(obs)
    assert deps[("checkout-api", "payment-api")] >= 30
    m = build_map(a, a.incidents[0].id)
    ids = {n["id"] for n in m["nodes"]}
    assert {"checkout-api", "payment-api", "postgres"} <= ids
    assert m["root"] == ["postgres"] and m["chain"][0] == "postgres" and "payment-api" in m["chain"]
    assert any(e["from"] == "checkout-api" and e["to"] == "payment-api" for e in m["deps"])
    assert m["hosts"] and all(h["env"] for h in m["hosts"])
    dot = to_dot(m, {"root": "KÖK NEDEN"})
    assert dot.startswith("digraph") and "KÖK NEDEN" in dot and '"checkout-api" -> "payment-api"' in dot
    build_map(a, None, env="nope")   # unknown env never raises


def test_alert_storm_density_clustering():
    """S-A1 layout: side tables, dedupe, density clustering, card budget, noise precision on the synthetic storm."""
    import json
    obs, rep = ingest_path(str(ROOT / "samples" / "alarm_storm.zip"))
    assert len(obs) == 3000 and [r for r in rep if r["file"] == "alarms.csv"][0]["dedup_dropped"] == 3000
    assert [r for r in rep if r["file"] == "service_dependencies.csv"][0]["kind"] == "table"
    a = an.Analysis(obs, rep)
    assert a.mode == "density" and len(a.dependencies) == 32 and len(a.inventory) >= 50
    assert 3 <= len(a.incidents) <= 15
    truth = json.loads((ROOT / "samples" / "alarm_storm_truth.json").read_text())
    noise_ids = [o.attributes["alarm_id"] for o in a.noise_obs]
    assert sum(1 for i in noise_ids if truth[i] == "noise") / len(noise_ids) >= 0.95
    by_label = {}
    for inc in a.incidents:
        ids = [o.attributes["alarm_id"] for sid in inc.signal_ids for o in a.signal_by_id[sid].observations]
        top = max(set(truth[i] for i in ids), key=lambda l: sum(1 for i in ids if truth[i] == l))
        by_label[top] = inc
    root_a = a.signal_by_id[by_label["A"].root_cause_signal]
    assert root_a.services == ["payment-db"] and by_label["A"].root_cause_alternatives
    na = a.noise_audit()
    assert na["eliminated"] + na["on_cards"] == 3000 and set(na["totals"]) <= {"n_baseline", "n_small", "n_demoted"}


def test_sap_log_family():
    """SAP logs of any extension (.log .lst .jvm .out .trc .file or none): dev traces, HANA, NetWeaver Java, JUL, GC, tp / transport, SM21."""
    obs, rep = ingest_path(str(ROOT / "samples" / "sap_logs.zip"))
    by_file = {r["file"].rsplit("/", 1)[-1]: r for r in rep}
    assert set(by_file) == {"dev_w0", "indexserver_sapprd01.30003.000.trc", "defaultTrace.0.trc", "std_server0.out", "sapjvm_gc.jvm", "R3trans.log", "SLOG2638.PRD.lst", "sm21_export.file",
                            "available.log", "bootstrap.jvm", "class_prefetch.lst", "deploy.0.log"}
    assert all(r["format"] == "sap" for r in by_file.values())
    o = {(x.source.rsplit("/", 1)[-1], x.line_no): x for x in obs}
    dev = o[("dev_w0", 15)]                       # timestamp inherited from the "M Wed Sep 16 02:14:09:501 2026" line
    assert dev.timestamp.strftime("%H:%M:%S") == "02:14:09" and dev.severity == "ERROR" and dev.service == "sap-prd" and dev.host == "sapprd01"
    assert dev.message.startswith("ThHdlReconnect") and o[("dev_w0", 16)].severity == "WARN" and o[("dev_w0", 18)].attributes["sap.thread"] == "140222"
    assert o[("dev_w0", 14)].attributes["sap.msg_code"] == "Q0I" and o[("dev_w0", 14)].severity == "INFO"
    assert ("dev_w0", 3) not in o                  # "*  ACTIVE TRACE LEVEL" decoration is skipped
    hana = o[("indexserver_sapprd01.30003.000.trc", 3)]
    assert hana.severity == "ERROR" and hana.service == "hana-memory" and hana.attributes["sap.source"] == "MemoryManager.cpp:00789" and hana.timestamp.microsecond == 1
    assert o[("indexserver_sapprd01.30003.000.trc", 4)].severity == "CRITICAL"
    nwj = o[("defaultTrace.0.trc", 1)]
    assert nwj.severity == "ERROR" and nwj.service == "sap-deploy" and nwj.message.startswith("Deployment of application") and nwj.attributes["sap.category"] == "BC-JAS-DPL"
    assert nwj.timestamp.utcoffset().total_seconds() == 3 * 3600 and o[("defaultTrace.0.trc", 3)].severity == "WARN"
    jul = o[("std_server0.out", 3)]
    assert jul.severity == "ERROR" and jul.service == "sap-dbpool" and jul.message.startswith("Cannot create JDBC pool") and jul.timestamp.strftime("%H:%M:%S") == "02:14:20"
    gc = o[("sapjvm_gc.jvm", 2)]
    assert gc.severity == "ERROR" and gc.attributes["gc.pause_ms"] == "6123.456" and o[("sapjvm_gc.jvm", 1)].severity == "INFO"
    assert o[("sapjvm_gc.jvm", 3)].severity == "ERROR" and o[("sapjvm_gc.jvm", 3)].attributes["gc.pause_ms"] == "7235"
    tp = o[("R3trans.log", 5)]
    assert tp.severity == "ERROR" and tp.attributes["sap.msg_code"] == "ETW125" and tp.timestamp.strftime("%d.%m %H:%M:%S") == "16.09 02:14:35"
    assert o[("R3trans.log", 6)].severity == "ERROR"      # exit code 12
    assert o[("SLOG2638.PRD.lst", 2)].severity == "ERROR" and o[("SLOG2638.PRD.lst", 2)].attributes["sap.rc"] == "0008"
    sm = o[("sm21_export.file", 3)]
    assert sm.severity == "CRITICAL" and sm.service == "sap-upd" and sm.attributes["sap.user"] == "BATCHUSR" and sm.timestamp.strftime("%Y-%m-%d %H:%M:%S") == "2026-09-16 02:14:51"
    assert o[("sm21_export.file", 4)].severity == "INFO"
    # real NetWeaver 7.50 ListFormatter (deploy.0.log excerpt: "#2.0<BS>#" version, <!--LOGHEADER-->, 3-line records ending at a blank line)
    dep = o[("deploy.0.log", 13)]
    assert dep.severity == "INFO" and dep.service == "sap-deployment" and dep.message.startswith("[Server 00 00_85596] (301) :Operation undeploy")
    assert dep.attributes["sap.sid"] == "TPD" and dep.attributes["sap.msg_id"] == "com.sap.ASJ.dpl_dc.000564" and dep.attributes["sap.user"] == "Administrator"
    err = o[("deploy.0.log", 26)]
    assert err.severity == "ERROR" and "Entered illegal state" in err.message and "sdu file path" in err.message and by_file["deploy.0.log"]["rows"] == 5
    assert not any(x.message.startswith("<!--") for x in obs)                    # log headers are meta, not events
    # sapstartsrv availability log, SAP JVM property snapshot, class prefetch list
    av = o[("available.log", 1)]
    assert av.severity == "ERROR" and av.attributes["avail.duration_min"] == 43 and av.timestamp.strftime("%d.%m.%Y %H:%M:%S") == "24.10.2021 18:51:33" and o[("available.log", 2)].severity == "INFO"
    jvm = o[("bootstrap.jvm", 1)]
    assert by_file["bootstrap.jvm"]["rows"] == 1 and jvm.service == "sap-tpd" and jvm.attributes["jvm.version"] == "1.8.0_241" and jvm.timestamp.strftime("%Y-%m-%d %H:%M:%S") == "2021-10-24 18:52:12"
    assert by_file["class_prefetch.lst"]["rows"] == 1 and o[("class_prefetch.lst", 1)].attributes["jvm.class_count"] == 12
    # the SAP files run through the normal engine
    a = an.Analysis(obs, rep)
    assert a.funnel()["raw_events"] == len(obs) and sum(1 for x in obs if x.severity in ("ERROR", "CRITICAL")) >= 10
    # non-SAP text is untouched, and the text parser now reads the extra timestamp shapes
    assert detect_format(TEXT)[0] == "text" and detect_format(SYSLOG)[0] == "syslog"
    t = parse("text", "Wed Sep 16 02:14:07 2026 ERROR foo: bar\n16.09.2026 02:14:08 WARN baz\nSep 16, 2026 2:14:09 AM SEVERE: qux\n")
    assert [x.timestamp.strftime("%H:%M:%S") for x in t] == ["02:14:07", "02:14:08", "02:14:09"] and [x.severity for x in t] == ["ERROR", "WARN", "CRITICAL"]


def test_burst_score_is_span_independent():
    """A signal in a 4-year log must not materialise one entry per quiet minute (was 60 s+ on a real deploy.log)."""
    from datetime import datetime, timedelta, timezone
    from watchover.models import Observation, Signal
    t0 = datetime(2021, 1, 1, tzinfo=timezone.utc)
    obs = [Observation(timestamp=t0 + timedelta(minutes=i), message="x") for i in range(10)]
    sig = Signal(id="S1", fingerprint="f", template="x", severity="INFO", count=10, services=[], hosts=[], entities=set(),
                 first_seen=obs[0].timestamp, last_seen=obs[-1].timestamp, onset=obs[0].timestamp, observations=obs)
    import time
    t = time.time(); an.score_burst(sig, 4 * 365 * 24 * 60); assert time.time() - t < 0.05
    assert sig.baseline_rate == 0.0 and sig.peak_rate == 1.0 and sig.burst_score == 0.0      # ratio 1/(0+1) = 1 -> no burst
    obs2 = obs + [Observation(timestamp=t0 + timedelta(minutes=3, seconds=s), message="x") for s in range(1, 10)]
    sig.observations = obs2; an.score_burst(sig, 10)
    assert sig.peak_rate == 10.0 and sig.baseline_rate == 1.0 and sig.burst_score == 0.444
