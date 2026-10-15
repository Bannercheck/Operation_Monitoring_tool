"""WP4a: parameter types (§5.5) — closed set, no free string (I1, server side), path rules, symlink escape, pid pairs."""
import os

import pytest

from watchover.act.params import FREE_STRING_NAMES, TYPES, ParamError, check_rendered, validate

PATH = {"type": "path", "roots": ["/var/log"]}


def code(spec, value, ctx=None, **kw):
    with pytest.raises(ParamError) as e:
        validate(spec, value, ctx, **kw)
    return e.value.code


def test_no_free_string_type():
    assert not set(TYPES) & set(FREE_STRING_NAMES)
    for t in FREE_STRING_NAMES + ("", None):
        assert code({"type": t}, "rm -rf /") == "unknown_type"


@pytest.mark.parametrize("value,expect", [
    ("/var/log/../etc/shadow", "dotdot"), ("/var/log/a\x00b", "control_char"), ("/var/log/a\nb", "control_char"),
    ("-/var/log", "leading_dash"), ("/var/log/*.log", "glob"), ("/var/log/{a,b}", "glob"), ("/var/log/a?", "glob"),
    ("~/logs", "tilde"), ("var/log", "not_absolute"), ("/etc/passwd", "outside_roots"), ("/var/logx", "outside_roots"),
    ("/var/log\u2025/x", "lookalike"), ("/vаr/log/app", "lookalike"), ("/var/log/app\u200b", "control_char"),
    ("/var/log/\u202eexe.gol", "control_char"), ("／var／log", "lookalike"), ("/var/log/" + "a" * 260, "length"),
])
def test_path_rejections(value, expect):
    assert code(PATH, value) == expect


def test_path_ok_and_normalised():
    assert validate(PATH, "/var/log/app/") == "/var/log/app"
    assert validate(PATH, "/var/log") == "/var/log"
    assert validate(PATH, "/var/log/sipariş") == "/var/log/sipariş"       # Turkish letters are not look-alikes
    assert code({"type": "path"}, "/var/log") == "no_roots"
    win = {"type": "path", "roots": ["C:\\inetpub\\logs"]}
    assert validate(win, "C:\\inetpub\\logs\\W3SVC1") == "C:\\inetpub\\logs\\W3SVC1"
    assert code(win, "C:\\Windows\\System32") == "outside_roots"
    assert code(win, 'C:\\inetpub\\logs\\a"b') == "not_absolute"


def test_symlink_escape_on_the_host(tmp_path):
    root = tmp_path / "logs"
    (root / "app").mkdir(parents=True)
    (tmp_path / "secret").mkdir()
    os.symlink(tmp_path / "secret", root / "evil")
    spec = {"type": "path", "roots": [str(root)], "must_exist": True}
    assert validate(spec, str(root / "evil")) == str(root / "evil")          # the server cannot see the host's file system
    assert code(spec, str(root / "evil"), local=True) == "symlink_escape"   # the actuator's realpath pass refuses it
    assert validate(spec, str(root / "app"), local=True) == str(root / "app")
    assert code(spec, str(root / "missing"), local=True) == "missing"
    (root / "file.log").write_text("x")
    assert code(dict(spec, dir=True), str(root / "file.log"), local=True) == "not_dir"


def test_int_bounds_and_types():
    spec = {"type": "int", "min": 1, "max": 30}
    assert validate(spec, 7) == 7 and validate(spec, "7") == 7
    assert code(spec, 0) == "bounds" and code(spec, 31) == "bounds"
    assert code(spec, True) == "type" and code(spec, "7; rm") == "type" and code(spec, 7.5) == "type"
    assert validate({"type": "duration", "max": 86400}, "30m") == "30m"
    assert code({"type": "duration", "max": 3600}, "2h") == "bounds"
    assert code({"type": "size"}, "500") == "format"
    assert validate({"type": "size", "min": 1, "max": 2 * 1024 ** 3}, "500M") == "500M"


def test_ident_family():
    unit = {"type": "unit", "pattern": "^(tomcat|orders)[A-Za-z0-9@._-]{0,40}\\.service$"}
    assert validate(unit, "orders-api.service") == "orders-api.service"
    assert code(unit, "sshd.service") == "pattern"
    assert code(unit, "orders;reboot.service") == "charset"
    assert code(unit, "-orders.service") == "leading_dash"
    assert code(unit, "оrders.service") == "lookalike"                     # Cyrillic о
    assert validate({"type": "iis_pool"}, "Orders Pool") == "Orders Pool"
    assert code({"type": "iis_pool"}, 'Orders"Pool') == "charset"
    assert code({"type": "win_service"}, "a^b") == "charset"
    assert validate({"type": "sap_sid"}, "PRD") == "PRD" and code({"type": "sap_sid"}, "prd") == "charset"
    assert validate({"type": "sap_nr"}, "00") == "00" and code({"type": "sap_nr"}, "0") == "charset"
    assert code({"type": "ident"}, "a b") == "charset"


def test_enum_dash_only_from_values():
    e = {"type": "enum", "values": ["-d", "-f"]}
    assert validate(e, "-d") == "-d"                                         # a leading dash only as an enum value
    assert code(e, "-x") == "not_in_enum"
    dyn = {"type": "enum", "values_from": "host:/etc/logrotate.d"}
    assert validate(dyn, "nginx") == "nginx" and code(dyn, "../passwd") == "charset" and code(dyn, "-f") == "charset"


def test_pid_pair_rule():
    p = {"type": "pid", "pid_exe": "java"}
    assert validate(p, {"pid": 4121, "start": 99812}) == {"pid": 4121, "start": 99812}
    assert code(p, 4121) == "pid_pair"
    assert code(p, {"pid": 4121}) == "pid_pair"
    assert code(p, {"pid": 1, "start": 5}) == "bounds"
    assert code(p, {"pid": 5000000, "start": 5}) == "bounds"
    assert code(p, {"pid": 4121, "start": True}) == "pid_start"


def test_url_and_host_scope():
    u = {"type": "url"}
    ctx = {"host": "web-01", "health_url": "https://orders.corp/health", "allowed_hosts": ["probe.corp"]}
    assert validate(u, "http://web-01:8080/health", ctx) == "http://web-01:8080/health"
    assert validate(u, "https://orders.corp/health", ctx) == "https://orders.corp/health"
    assert code(u, "http://evil.example/x", ctx) == "url_host"
    assert code(u, "file:///etc/passwd", ctx) == "format"
    h = {"type": "host"}
    assert validate(h, "web-01", {"case_host": "web-01"}) == "web-01"
    assert code(h, "db-01", {"case_host": "web-01"}) == "host_scope"
    assert code(h, "web-01", {"case_host": "web-01", "connector_hosts": ["web-02"]}) == "connector_host"


def test_rendered_element():
    assert check_rendered("ok") == "ok"
    with pytest.raises(ParamError):
        check_rendered("a\nb")
