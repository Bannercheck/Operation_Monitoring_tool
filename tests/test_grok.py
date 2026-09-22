"""Grok engine, packs, user patterns and the format detector's grok step."""
import json

from watchover import grok
from watchover.format_detector import detect_format
from watchover.parsers import PARSERS


def test_expand_and_match():
    assert grok.match("%{IP:ip} %{WORD:verb}", "10.0.0.1 GET /x") == {"ip": "10.0.0.1", "verb": "GET"}
    assert grok.match("%{NUMBER:n:int}", "size=42") == {"n": "42"}
    r = grok.test_pattern("%{TIMESTAMP_ISO8601:timestamp} %{LOGLEVEL:severity} %{GREEDYDATA:message}", "2026-09-22 09:15:03 ERROR boom\nnope\n")
    assert r["ok"] and r["matched"] == 1 and r["total"] == 2 and r["fields"] == ["message", "severity", "timestamp"]
    assert grok.test_pattern("%{NOPE:x}", "a")["ok"] is False
    assert all(grok.compile_pattern(lp.pattern) for lp in grok.LINES)


def test_detect_and_parse_pack(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path))
    text = ("1790068442.003    117 10.0.4.12 TCP_MISS/200 1432 GET http://shop.example.com/a ali HIER_DIRECT/10.0.9.8 application/json\n"
            "1790068445.120  30001 10.0.4.13 TCP_MISS/504 512 POST http://shop.example.com/b ali HIER_DIRECT/10.0.9.9 text/html\n")
    assert detect_format(text)[0] == "grok"
    obs = list(PARSERS["grok"].parse(text, "squid.log"))
    assert [o.severity for o in obs] == ["INFO", "ERROR"] and obs[0].service == "squid" and obs[0].message.startswith("http://") and obs[0].timestamp.year == 2026


def test_user_pattern_wins_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path))
    lp = grok.LinePattern("MYAPP", r"^%{TIMESTAMP_ISO8601:ts} \[%{WORD:lvl}\] %{WORD:app} :: %{GREEDYDATA:msg}$", "myapp", {"timestamp": "ts", "severity": "lvl", "service": "app", "message": "msg"}, "", False, True, "abc123")
    grok.save_user_patterns([lp])
    assert json.loads((tmp_path / "grok.json").read_text())[0]["name"] == "MYAPP"
    text = "2026-09-22T09:15:03Z [WARN] billing :: retry 3/5\n2026-09-22T09:15:04Z [ERROR] billing :: failed\n"
    best, share = grok.detect(text.splitlines())
    assert best and best.name == "MYAPP" and share == 1.0
    obs = list(PARSERS["grok"].parse(text, "app.log"))
    assert (obs[0].severity, obs[0].service, obs[0].message) == ("WARN", "billing", "retry 3/5") and obs[1].severity == "ERROR"
