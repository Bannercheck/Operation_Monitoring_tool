"""Shared fixtures for the Go actuator (goagent/) and the Python server: canonical JSON, template semantic hashes and the
closed-typed parameter validators must agree byte for byte (docs/design/action-agent.md §5.4, §5.5, §9.3).

    python tests/act_parity.py      rewrites tests/fixtures/act_canonical.jsonl and tests/fixtures/act_params_parity.jsonl

test_act_goagent.py checks that the files equal what the Python code produces now; goagent/internal/act/*_test.go checks
that the Go code produces the same."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT / "src"))

from watchover.act import params as P  # noqa: E402
from watchover.act.templates import load_packs, semantic_sha  # noqa: E402
from watchover.act.types import canonical, sha256_hex  # noqa: E402

CANON_CASES = [
    {"b": 1, "a": [3, 2, 1], "ç": "ş", "Z": None, "t": True, "f": False},
    {"s": "quote \" backslash \\ slash / tab \t nl \n cr \r bs \b ff \f nul \u0000 us \u001f del \u007f"},
    {"html": "<a href='x'>&amp;</a>", "ls": "  ", "emoji": "📦", "tr": "İşçi ığdır ÖĞÜ"},
    {"ints": [0, -1, 7, 4194304, 5368709120, 2 ** 70, -(2 ** 63)]},
    {"floats": [1.5, 0.1, -2.25, 1e-07, 1e-05, 0.0001, 123456.789, 1e16, 1.5e16, 2.0, -0.0, 3e22]},
    {"nested": {"z": {"y": [{"x": 1, "w": [None, "", {}]}]}, "a": []}},
    {"keys": {"b": 1, "B": 2, "a": 3, "_": 4, "é": 5, "e": 6, "10": 7, "9": 8}},
]

S_PATH = {"type": "path", "roots": ["/var/log"]}
S_ROOT = {"type": "path", "roots": ["/"]}
S_INT = {"type": "int", "min": 1, "max": 30}
S_ENUM = {"type": "enum", "values": ["a", "b", "7"]}
S_UNIT = {"type": "unit", "pattern": "^(tomcat|orders|billing)[A-Za-z0-9@._-]{0,40}\\.service$"}
PARAM_CASES = [
    (S_ENUM, "a"), (S_ENUM, "c"), (S_ENUM, 7), (S_ENUM, True), (S_ENUM, ""), (S_ENUM, None), (S_ENUM, ["a"]),
    ({"type": "enum", "values_from": "host:/etc/logrotate.d"}, "orders"), ({"type": "enum", "values_from": "x"}, "-x"),
    ({"type": "enum", "values_from": "x"}, "a b"), (S_ENUM, "ａ"), (S_ENUM, "а"), (S_ENUM, "a​"), (S_ENUM, "b\n"),
    (S_INT, 7), (S_INT, "7"), (S_INT, " 7 "), (S_INT, 0), (S_INT, 31), (S_INT, "7.5"), (S_INT, 7.5), (S_INT, True), (S_INT, "abc"),
    (S_INT, "-3"), (S_INT, "1234567890123"), ({"type": "int"}, -5), ({"type": "int", "min": 0}, 10 ** 11),
    ({"type": "duration", "min": 60, "max": 86400}, "30m"), ({"type": "duration"}, "30x"), ({"type": "duration", "min": 60}, "10s"),
    ({"type": "size", "max": 1073741824}, "500M"), ({"type": "size", "max": 1073741824}, "2G"), ({"type": "size"}, "5T"),
    (S_PATH, "/var/log"), (S_PATH, "/var/log/app/"), (S_PATH, "/var/log//app"), (S_PATH, "/var/log/./app"), (S_PATH, "/var/log/../etc"),
    (S_PATH, "var/log"), (S_PATH, "-rf"), (S_PATH, "/var/log/*.gz"), (S_PATH, "/var/log/{a}"), (S_PATH, "~root"), (S_PATH, "/etc"),
    (S_PATH, "/var/logs"), (S_PATH, "/var/log\nx"), (S_PATH, "/var/log/ﬁle"), (S_PATH, ""), (S_PATH, "/" + "a" * 300),
    (S_PATH, "//var/log"), (S_PATH, "///var/log"), (S_PATH, "/var/log\\app"), (S_PATH, 7), (S_ROOT, "/anything/at/all"),
    ({"type": "path"}, "/var/log"), (S_PATH, "/var/log/Ünïcode/ş"), (S_PATH, "/var/log/á"),
    ({"type": "ident"}, "orders.service"), ({"type": "ident"}, "-x"), ({"type": "ident"}, "a b"), ({"type": "ident"}, "x" * 65),
    ({"type": "ident", "pattern": "^ord.*$"}, "orders"), ({"type": "ident", "pattern": "^ord.*$"}, "billing"),
    (S_UNIT, "orders.service"), (S_UNIT, "orders@2.service"), (S_UNIT, "sshd.service"), (S_UNIT, "orders"), ({"type": "unit"}, "-a.service"),
    ({"type": "iis_pool"}, "My Pool"), ({"type": "iis_pool"}, "a/b"), ({"type": "win_service"}, "W3SVC"), ({"type": "win_service"}, "a b"),
    ({"type": "sap_sid"}, "PR1"), ({"type": "sap_sid"}, "pr1"), ({"type": "sap_nr"}, "00"), ({"type": "sap_nr"}, "0"),
    ({"type": "pid"}, {"pid": 4121, "start": "12345:abcd1234"}), ({"type": "pid"}, {"pid": 4121, "start": 99}),
    ({"type": "pid"}, {"pid": 1, "start": 5}), ({"type": "pid"}, 4121), ({"type": "pid"}, {"pid": 10, "start": True}),
    ({"type": "pid"}, {"pid": 10, "start": "a b"}), ({"type": "pid"}, {"pid": "4121", "start": "1:2"}), ({"type": "pid"}, {"pid": 10}),
    ({"type": "url"}, "http://localhost:8080/health"), ({"type": "url"}, "https://web-prod-07/x?a=1"), ({"type": "url"}, "http://evil.example/"),
    ({"type": "url"}, "ftp://x"), ({"type": "url"}, "http://127.0.0.1"),
    ({"type": "str"}, "x"), ({"type": "string"}, "x"), ({}, "x"),
]
CTX = {"host": "web-prod-07"}


def canonical_rows():
    rows = []
    for obj in CANON_CASES:
        rows.append({"kind": "object", "in": obj, "canonical": canonical(obj), "sha256": sha256_hex(canonical(obj))})
    for fname, pack in sorted(load_packs(str(ROOT / "agent_actions")).items()):
        for t in pack.get("templates", []):
            rows.append({"kind": "template", "pack": fname, "in": t, "sha256": semantic_sha(t)})
    return rows


def param_rows():
    rows = []
    for spec, value in PARAM_CASES:
        try:
            out = P.validate(spec, value, CTX, local=False)
            rows.append({"spec": spec, "value": value, "ok": True, "out": out})
        except P.ParamError as e:
            rows.append({"spec": spec, "value": value, "ok": False, "code": e.code})
    return rows


def dump(rows) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)


if __name__ == "__main__":
    (FIX / "act_canonical.jsonl").write_text(dump(canonical_rows()), encoding="utf-8")
    (FIX / "act_params_parity.jsonl").write_text(dump(param_rows()), encoding="utf-8")
    print("written")
