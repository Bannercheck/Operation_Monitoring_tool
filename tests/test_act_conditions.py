"""WP4a: `when` / `expect` expressions (§6.4): parser without eval, lint of unknown names, fail-closed evaluation."""
import pytest

from watchover.act.conditions import ConditionError, check, evaluate, parse, refs

CTX = {"steps": {"d1": {"use_pct": 93.4, "mount": "/", "ran": True, "ok": True}, "d3": {"deleted_open_bytes": 2147483648, "ran": True},
                 "a1": {"out": {"list_id": "L1"}, "ran": False}},
       "case": {"env": "prod", "severity": "high", "source": "threshold"}, "inventory": {"health_url": "https://x/health"},
       "captures": {"use_pct": 72, "files": 412, "bytes": 6_100_000_000}}


@pytest.mark.parametrize("expr,want", [
    ("steps.d1.use_pct >= 85", True), ("steps.d1.use_pct < 80", False), ('steps.d1.mount == "/"', True),
    ("steps.d3.deleted_open_bytes > 1073741824", True), ("use_pct < 80", True),
    ("files <= 10000 and bytes >= 104857600", True), ("not steps.a1.ran", True), ("steps.d1.ok and steps.d1.ran", True),
    ('case.env in ["prod", "production"]', True), ('case.severity == "low" or case.env == "prod"', True),
    ("inventory.health_url", True), ("inventory.missing", False), ("steps.zz.x > 1", False), ("missing_capture < 80", False),
    ("(use_pct < 80 or files > 1) and not (bytes < 10)", True), ("steps.d1.use_pct != 93.4", False),
])
def test_evaluate(expr, want):
    assert evaluate(expr, CTX) is want


@pytest.mark.parametrize("bad", ["__import__('os')", "steps.d1.use_pct >", "a == b", "1 < 2", "steps.d1.use_pct ; rm", "x or", "(a < 1",
                                 "a <> 1", "a < 'single'"])
def test_parse_errors(bad):
    with pytest.raises(ConditionError):
        parse(bad)


def test_lint_unknown_names():
    assert check("steps.d1.use_pct >= 85", steps={"d1"}) == []
    assert check("steps.d9.use_pct >= 85", steps={"d1"}) == ["unknown step in steps.d9.use_pct"]
    assert check("steps.d1.nope > 1", steps={"d1"}, captures={"d1": ["use_pct"]}) == ["unknown capture steps.d1.nope"]
    assert check("steps.d1.ran", steps={"d1"}, captures={"d1": []}) == []
    assert check("case.owner == \"x\"") == ["unknown case field case.owner"]
    assert check("use_pct < 80", bare={"use_pct"}) == []
    assert check("use_pct < 80") == ["unknown name use_pct"]
    assert check("files <=") and check("files <=")[0].startswith("parse")
    assert refs(parse("a < 1 and steps.x.y == 2")) == {"a", "steps.x.y"}
    with pytest.raises(ConditionError):
        parse("x" * 600)
