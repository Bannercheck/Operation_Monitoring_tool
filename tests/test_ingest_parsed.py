"""WATCHOVER_GO_PARSE equivalence: a batch parsed by the Go receiver and handed over as a v=2 spool record must leave the
dashboard in exactly the state the same raw batch leaves it in through LiveStore.ingest: the same events into the ring,
rollups, history/trace hook and flows, the same metric rows, the same per-stream hint.

Two producers of v=2 records are compared against ingest():
  * Python's own canonical rendering of ingest_bytes (the format the Go parsers match byte for byte, receiver/parse goldens);
  * the real Go parse stage (`wo-receiver parse-spool`), when a Go toolchain is installed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / "receiver" / "parse" / "testdata" / "inputs"
GO = shutil.which("go") or ("/usr/local/go/bin/go" if Path("/usr/local/go/bin/go").exists() else None)
# inputs that exercise the post-parse branches (06-integration.md section 12) plus one of each common format
CASES = ["sim__simulate_flows.jsonl", "sim__simulate_batch.jsonl", "sim__simulate_traces.jsonl", "mut__dynamics_ax_aos.log",
         "mut__dynamics_nav_bc.log", "demo__app.jsonl", "demo__db.log", "demo__checkout.log", "mut__syslog_rfc3164.log",
         "mut__nginx_combined.log", "mut__oracle_ebs_concurrent.log", "mut__logfmt.log", "mut__csv_events.csv", "sap__dev_w0",
         "mut__windows_event_xml.log", "storm__alarms.json"]
FIXED_NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


class _FixedDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW if tz is None else FIXED_NOW.astimezone(tz)


@pytest.fixture(autouse=True)
def _frozen(monkeypatch, tmp_path):
    import watchover.live as wl
    monkeypatch.setattr(wl, "datetime", _FixedDT)                     # the arrival-time fallback must be the same in both stores
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))


def metrics_input() -> bytes:
    """Agent metric records (live.metric_of -> metric rows, not events): no golden input carries them."""
    import random
    from watchover.live import simulate_metrics
    rng, state = random.Random(3), {}
    return b"".join(simulate_metrics(rng, state, incident=i % 4 == 0) for i in range(30))


def read_input(name: str) -> bytes:
    return metrics_input() if name == "metrics.jsonl" else (INPUTS / name).read_bytes()


def batches(data: bytes, n: int = 40) -> list[bytes]:
    lines = data.split(b"\n")
    out = [b"\n".join(lines[i:i + n]) for i in range(0, len(lines), n)]
    return [c for c in out if c.strip()]


def canon_line(o) -> str:
    """The v=2 body line: scripts/parse_golden.py canon() with sorted top-level keys, attributes in insertion order."""
    d = {"ts": o.timestamp.isoformat(), "message": o.message, "kind": o.kind, "severity": o.severity, "service": o.service,
         "host": o.host, "environment": o.environment, "origin": o.origin, "attributes": o.attributes, "source": o.source,
         "line_no": o.line_no, "parser": o.parser, "parser_confidence": round(float(o.parser_confidence), 4), "raw": o.raw}
    return json.dumps({k: d[k] for k in sorted(d)}, ensure_ascii=False, default=str)


class Recorder:
    """A LiveStore with every post-parse output captured."""

    def __init__(self):
        from watchover.live import LiveStore
        self.store = LiveStore(spool=None)
        self.events, self.flows, self.rolled = [], [], []
        self.store.on_ingest = lambda ev: self.events.extend(ev)
        self.store.on_flows = lambda fl: self.flows.extend(fl)
        add = self.store.rollup.add
        self.store.rollup.add = lambda ev: (self.rolled.extend(ev), add(ev))
        self.counts = []

    @staticmethod
    def obs_key(o) -> tuple:
        return (o.timestamp.isoformat(), o.timestamp.utcoffset(), o.message, o.kind, o.severity, o.service, o.host, o.environment, o.origin,
                json.dumps(o.attributes, ensure_ascii=False, default=repr), o.source, o.line_no, o.parser, o.raw,     # order matters (metric_of)
                round(float(o.parser_confidence), 4))

    def state(self) -> dict:
        s = self.store
        return {"ring": [self.obs_key(o) for o in s.buf], "packed": [s._pack(o, "x") for o in s.buf], "events": [self.obs_key(o) for o in self.events],
                "rolled": [self.obs_key(o) for o in self.rolled], "flows": [self.obs_key(o) for o in self.flows], "metrics": list(s.metrics),
                "metric_hosts": {k: sorted(v) for k, v in s.metric_hosts.items()}, "received": s.received, "detect": s._detect,
                "counts": self.counts,
                "rollup": repr({k: v for k, v in vars(s.rollup).items() if not k.startswith("_") and k not in ("lock", "started", "add_ms") and not callable(v)})}


AGENT = dict(agent="lt-host", env="prod", site="ist", agent_id=7)


def v2_header(name: str, rep: dict, data: bytes, body: bytes, n: int) -> dict:
    fmt = rep.get("format")
    hint_fmt = "jsonl" if fmt == "json" and data.strip().count(b"\n") == 0 and data.lstrip()[:1] == b"{" else fmt
    return {"v": 2, "kind": "parsed", "path": "/ingest", "file": name, "agent": AGENT["agent"], "agent_id": AGENT["agent_id"], "env": AGENT["env"],
            "site": AGENT["site"], "ts": "2026-10-15T12:00:00Z", "len": len(body), "n": n, "raw_len": len(data), "format": fmt,
            "family": rep.get("family", ""), "confidence": rep.get("confidence", 0.9), "hint_format": hint_fmt,
            "report": {k: v for k, v in rep.items() if k not in ("sha", "table")}}


@pytest.mark.parametrize("name", CASES + ["metrics.jsonl"])
def test_ingest_parsed_equivalence(name, monkeypatch, tmp_path):
    """Store A parses raw batches; a producer with its own hint loop renders each batch the way the Go stage does and store B
    ingests the v=2 records. Profiles and normalizer state are shared by the producer and A in the same order (producer first)."""
    from watchover.pipeline import ingest_bytes
    data = read_input(name)
    a, b = Recorder(), Recorder()
    hints: dict = {}
    for chunk in batches(data):
        key = (AGENT["agent"], name)                                   # the producer: live.py's hint loop, like the Go stage
        hint = hints.get(key)
        if hint is not None:
            hint["n"] += 1
            if hint["n"] % 200 == 0:
                hint = None
        monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "producer"))
        obs, rep = ingest_bytes(name, chunk, hint=dict(hint) if hint else None)
        if rep and rep[0].get("format") not in (None, "table") and (hint is None or hint.get("n", 0) % 200 == 0):
            hdr0 = v2_header(name, rep[0], chunk, b"", 0)
            hints[key] = {"format": hdr0["hint_format"], "confidence": rep[0].get("confidence", 0.9), "family": rep[0].get("family", ""), "n": 1}
        body = "".join(canon_line(o) + "\n" for o in obs).encode()
        monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "a"))
        a.counts.append(a.store.ingest(name, chunk, **AGENT))
        monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "b"))
        b.counts.append(b.store.ingest_parsed(body, v2_header(name, rep[0], chunk, body, len(obs))))
    sa, sb = a.state(), b.state()
    assert sa["received"] > 0 and len(sa["rollup"]) > 200
    if name == "sim__simulate_flows.jsonl":
        assert sa["flows"]
    if name == "metrics.jsonl":
        assert sa["metrics"] and sa["metric_hosts"]
    for k in sa:
        assert sa[k] == sb[k], f"{name}: {k} differs"


def test_ingest_parsed_hint_and_profile(monkeypatch, tmp_path):
    """A v=2 record advances the stream hint like a parsed batch (json -> jsonl from hint_format) and remembers the shape's
    profile once a minute, as pipeline._parse_one would."""
    from watchover import profiles
    from watchover.live import LiveStore
    s = LiveStore(spool=None)
    rep = {"format": "json", "family": "", "confidence": 0.95, "file": "a.json", "keys": ["ts", "msg"], "roles": {"timestamp": "ts", "message": "msg", "host": None}}
    h = {"v": 2, "kind": "parsed", "agent": "h1", "file": "a.json", "format": "json", "family": "", "confidence": 0.95, "hint_format": "jsonl", "report": rep}
    assert s.ingest_parsed(b"", h) == 0
    assert s._detect[("h1", "a.json")] == {"format": "jsonl", "confidence": 0.95, "family": "", "n": 1}
    sig = profiles.signature("json", ["ts", "msg"])
    assert profiles.load()[sig]["mapping"] == {"timestamp": "ts", "message": "msg"}
    s.ingest_parsed(b"", h)
    assert s._detect[("h1", "a.json")]["n"] == 2 and profiles.load()[sig]["hits"] == 1     # debounced


@pytest.fixture(scope="module")
def binary(tmp_path_factory):
    if GO is None:
        pytest.skip("no Go toolchain")
    out = tmp_path_factory.mktemp("bin") / "wo-receiver"
    subprocess.run([GO, "build", "-o", str(out), "."], cwd=ROOT / "receiver", check=True, capture_output=True, timeout=600)
    return out


def raw_segment(name: str, data: bytes, agent: str) -> bytes:
    out = bytearray()
    for chunk in batches(data):
        h = {"v": 1, "path": "/ingest", "file": name, "ip": "10.0.0.9", "ts": "2026-10-15T12:00:00Z", "len": len(chunk), "identity": "token",
             "agent": agent, "agent_id": 7, "env": "prod", "site": "ist"}
        out += json.dumps(h, separators=(",", ":")).encode() + b"\n" + chunk + b"\n"
    return bytes(out)


def test_go_stage_equivalence(binary, monkeypatch, tmp_path):
    """The real Go parse stage: the same raw segment is consumed raw by store A and Go-parsed (pseg) by store B."""
    from watchover.spool_consumer import SpoolConsumer
    da, db = tmp_path / "raw", tmp_path / "go"
    da.mkdir(); db.mkdir()
    seg = b"".join(raw_segment(n, (INPUTS / n).read_bytes(), "lt-" + n.split(".")[0]) for n in CASES)
    (da / "seg-0000000000000000001.wo").write_bytes(seg)
    (db / "seg-0000000000000000001.raw").write_bytes(seg)
    r = subprocess.run([str(binary), "parse-spool", str(db)], capture_output=True, timeout=300, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    st = json.loads(r.stdout)
    assert st["parse_batches_parsed"] > 0
    assert not list(db.glob("seg-*")) and (db / "pseg-0000000000000000001.wo").exists()
    a, b = Recorder(), Recorder()
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "a"))
    ca = SpoolConsumer(str(da), a.store)
    ca.run_once()
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "b"))
    cb = SpoolConsumer(str(db), b.store)
    cb.run_once()
    assert ca.records == cb.records == sum(len(batches((INPUTS / n).read_bytes())) for n in CASES)
    assert cb.parsed_records == st["parse_batches_parsed"] and cb.raw_records == st["parse_batches_forwarded"]
    sa, sb = a.state(), b.state()
    for k in sa:
        if k != "counts":
            assert sa[k] == sb[k], f"{k} differs"


def all_inputs() -> list[Path]:
    corpus = [p for p in sorted((ROOT / "samples" / "corpus").iterdir()) if p.suffix != ".md" and p.is_file()]
    return corpus + sorted(INPUTS.iterdir())


def test_go_stage_bytes_all_inputs(binary, monkeypatch, tmp_path):
    """Every golden input, 40-line batches, one stream each, through the real stage with one worker (so the shared profile
    store and timestamp caches see the batches in the same order as here): each v=2 record carries exactly the body and
    hint fields Python computes for that batch, attribute order included (the goldens sort attribute keys)."""
    from watchover.pipeline import ingest_bytes
    files = all_inputs()
    d = tmp_path / "spool"; d.mkdir()
    (d / "seg-0000000000000000001.raw").write_bytes(b"".join(raw_segment(p.name, p.read_bytes(), "a" + str(i)) for i, p in enumerate(files)))
    r = subprocess.run([str(binary), "parse-spool", str(d)], capture_output=True, timeout=600,
                       env={"PATH": "/usr/bin:/bin", "WO_PARSE_WORKERS": "1"})
    assert r.returncode == 0, r.stderr
    data = (d / "pseg-0000000000000000001.wo").read_bytes()
    recs, i = [], 0
    while i < len(data):
        nl = data.index(b"\n", i)
        h = json.loads(data[i:nl]); n = int(h["len"])
        recs.append((h, data[nl + 1:nl + 1 + n])); i = nl + 1 + n + 1
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "py"))
    k, bad, forwarded = 0, [], 0
    for p in files:
        hint = None
        for chunk in batches(p.read_bytes()):
            if hint is not None:
                hint["n"] += 1
                if hint["n"] % 200 == 0:
                    hint = None
            obs, rep = ingest_bytes(p.name, chunk, hint=dict(hint) if hint else None)
            if rep and rep[0].get("format") not in (None, "table") and (hint is None or hint.get("n", 0) % 200 == 0):
                hint = {"format": v2_header(p.name, rep[0], chunk, b"", 0)["hint_format"], "confidence": rep[0].get("confidence", 0.9),
                        "family": rep[0].get("family", ""), "n": 1}
            h, body = recs[k]; k += 1
            assert h["file"] == p.name
            if h["v"] != 2:
                forwarded += 1
                continue
            want = "".join(canon_line(o) + "\n" for o in obs).encode()
            exp = v2_header(p.name, rep[0], chunk, want, len(obs))
            if body != want or any(h[f] != exp[f] for f in ("format", "family", "confidence", "hint_format", "n")):
                bad.append(p.name)
    assert k == len(recs)
    assert not bad, f"{len(bad)} batches differ: {sorted(set(bad))}"
    assert forwarded == 0


def test_shadow_diff(binary, monkeypatch, tmp_path):
    """Shadow mode end to end: the stage writes raw + parsed pairs, scripts/parse_shadow_diff.py finds no difference, and a
    tampered Go body is reported."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("parse_shadow_diff", ROOT / "scripts" / "parse_shadow_diff.py")
    psd = importlib.util.module_from_spec(spec); spec.loader.exec_module(psd)
    d = tmp_path / "spool"; d.mkdir()
    seg = b"".join(raw_segment(n, read_input(n), "lt-" + n) for n in CASES[:8])
    seg += json.dumps({"v": 1, "path": "/rum", "app": "x", "ts": "2026-10-15T12:00:00Z", "len": 2}).encode() + b"\n{}\n"
    (d / "seg-0000000000000000001.wo").write_bytes(seg)
    (d / "seg-0000000000000000001.shd").write_bytes(seg)
    r = subprocess.run([str(binary), "parse-spool", str(d)], capture_output=True, timeout=300, env={"PATH": "/usr/bin:/bin", "WATCHOVER_GO_PARSE": "shadow"})
    assert r.returncode == 0, r.stderr
    assert (d / "seg-0000000000000000001.wo").read_bytes() == seg and not (d / "seg-0000000000000000001.shd").exists()
    shadow = d / "shadow-0000000000000000001.wo"
    out = tmp_path / "diff.json"
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    assert psd.main(["--spool", str(d), "--out", str(out)]) == 0
    rep = json.loads(out.read_text())
    assert rep["compared"] == rep["raw_batches"] == sum(len(batches(read_input(n))) for n in CASES[:8]) and rep["mismatched"] == 0
    data = shadow.read_bytes()
    shadow.write_bytes(data.replace(b'"severity": "ERROR"', b'"severity": "FATAL"', 1))
    assert psd.main(["--spool", str(d), "--out", str(out)]) == 1
    rep = json.loads(out.read_text())
    assert rep["mismatched"] == 1 and rep["compared"] == rep["raw_batches"] and "FATAL" in rep["samples"][0]["problems"][0]["go"]


def test_export_profiles(monkeypatch, tmp_path):
    """Go parse on/shadow: the consumer publishes <home>/profiles.json plus scenario.MAPPING into the spool, only on change;
    off: nothing is exported."""
    from watchover import profiles
    from watchover.live import LiveStore
    from watchover.spool_consumer import SpoolConsumer
    monkeypatch.setenv("WATCHOVER_GO_PARSE", "1")
    profiles.remember("jsonl", ["ts", "msg"], {"timestamp": "ts", "message": "msg"}, name="a.jsonl")
    c = SpoolConsumer(str(tmp_path / "spool"), LiveStore(spool=None))
    assert c.go_parse == "on" and c.export_profiles() and not c.export_profiles()
    d = json.loads((tmp_path / "spool" / "profiles.json").read_text())
    assert d["_scenario_mapping"] == {} and d[profiles.signature("jsonl", ["ts", "msg"])]["source"] == "auto"
    monkeypatch.setenv("WATCHOVER_GO_PARSE", "0")
    assert SpoolConsumer(str(tmp_path / "s2"), LiveStore(spool=None)).go_parse == "off"
    monkeypatch.setenv("WATCHOVER_GO_PARSE", "shadow")
    assert SpoolConsumer(str(tmp_path / "s3"), LiveStore(spool=None)).go_parse == "shadow"


def test_export_profiles_presence(monkeypatch, tmp_path):
    """The receiver parses only while <spool>/profiles.json exists: the consumer rewrites a deleted export even when the
    profiles did not change, and with WATCHOVER_GO_PARSE=0 it removes a stale export so a receiver left on forwards raw."""
    from watchover.live import LiveStore
    from watchover.spool_consumer import SpoolConsumer
    monkeypatch.setenv("WATCHOVER_GO_PARSE", "1")
    spool = tmp_path / "spool"
    c = SpoolConsumer(str(spool), LiveStore(spool=None))
    assert c.export_profiles() and not c.export_profiles()
    (spool / "profiles.json").unlink()
    assert c.export_profiles() and (spool / "profiles.json").exists()
    monkeypatch.setenv("WATCHOVER_GO_PARSE", "0")
    off = SpoolConsumer(str(spool), LiveStore(spool=None), interval=0.01)
    off.start()
    off.stop()
    assert not (spool / "profiles.json").exists()


def test_go_stage_reads_parse_state(binary, monkeypatch, tmp_path):
    """The real export read by the real stage: with this process's defaults (profiles, scenario constants, no user grok
    patterns) Go parses; a user grok pattern (it changes Python's format detection, Go does not port them) or an edited
    parse-path scenario constant sends every batch to Python. Before, Go ignored both and emitted its own parse."""
    from watchover import grok, scenario
    from watchover.live import LiveStore
    from watchover.spool_consumer import SpoolConsumer
    monkeypatch.setenv("WATCHOVER_GO_PARSE", "1")
    names = ["mut__plain_app_text.log", "mut__syslog_rfc3164.log", "demo__app.jsonl"]
    seg = b"".join(raw_segment(n, (INPUTS / n).read_bytes(), "lt-" + n) for n in names)
    nb = sum(len(batches((INPUTS / n).read_bytes())) for n in names)

    def run(d: Path) -> dict:
        d.mkdir()
        c = SpoolConsumer(str(d), LiveStore(spool=None))
        assert c.export_profiles()
        (d / "seg-0000000000000000001.raw").write_bytes(seg)
        r = subprocess.run([str(binary), "parse-spool", str(d)], capture_output=True, timeout=300, env={"PATH": "/usr/bin:/bin"})
        assert r.returncode == 0, r.stderr
        return json.loads(r.stdout)

    st = run(tmp_path / "plain")
    assert st["parse_batches_parsed"] == nb and st["parse_forward_reason_scenario"] == 0
    grok.save_user_patterns([grok.LinePattern("MY_APP", r"^%{TIMESTAMP_ISO8601:ts} %{GREEDYDATA:msg}$", "myapp",
                                              {"timestamp": "ts", "message": "msg"}, "", False, True, "MY_APP")])
    st = run(tmp_path / "grok")
    assert st["parse_batches_parsed"] == 0 and st["parse_forward_reason_usergrok"] + st["parse_forward_reason_sticky"] == nb
    grok.save_user_patterns([])
    monkeypatch.setattr(scenario, "DEDUP_KEY", "event_id")
    st = run(tmp_path / "scenario")
    assert st["parse_batches_parsed"] == 0 and st["parse_forward_reason_scenario"] == nb
