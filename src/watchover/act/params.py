"""Parameter types of command templates (docs/design/action-agent.md §5.5): a closed set, the same code on the server and
(as a standard-library twin inside agent.py, parity-tested) on the host actuator. There is no free-string type (I1).

    validate({"type": "path", "roots": ["/var/log"]}, "/var/log/app")        -> "/var/log/app"
    validate({"type": "int", "min": 1, "max": 30}, "7")                      -> 7
    validate({"type": "path", "roots": ["/var/log"]}, "/var/log/../etc")     -> ParamError("dotdot")

`local=True` is the actuator's extra pass: realpath + root re-check (symlink escape), `must_exist`, `dir`."""
from __future__ import annotations

import ntpath
import os
import posixpath
import re
import unicodedata
from typing import Any, Dict, Optional

TYPES = ("enum", "int", "duration", "size", "path", "ident", "unit", "win_service", "iis_pool", "sap_sid", "sap_nr", "pid", "url", "host")
IDENT_FAMILY = ("ident", "unit", "win_service", "iis_pool", "sap_sid", "sap_nr")      # may follow a literal prefix in one argv element
FREE_STRING_NAMES = ("str", "string", "text", "any", "raw", "cmd", "command", "shell", "args")    # refused by lint (I1)

RX = {
    "ident": re.compile(r"^[A-Za-z0-9_.:@-]{1,64}$"),
    "unit": re.compile(r"^[A-Za-z0-9@._-]{1,120}\.service$"),
    "win_service": re.compile(r"^[A-Za-z0-9_.-]{1,80}$"),
    "iis_pool": re.compile(r"^[A-Za-z0-9 ._-]{1,64}$"),
    "sap_sid": re.compile(r"^[A-Z][A-Z0-9]{2}$"),
    "sap_nr": re.compile(r"^\d{2}$"),
    "duration": re.compile(r"^(\d{1,9})([smhd])$"),
    "size": re.compile(r"^(\d{1,12})([KMG])$"),
    "enum_dyn": re.compile(r"^[A-Za-z0-9_.@][A-Za-z0-9_.@-]{0,63}$"),
}
DUR = {"s": 1, "m": 60, "h": 3600, "d": 86400}
SIZE = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}
GLOB = set("*?[]{}")
INVISIBLE = re.compile("[\u0000-\u001f\u007f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
# look-alike letters (Cyrillic / Greek / dotless i) that would let a value read like an allowed one
LOOKALIKE = re.compile("[\u0370-\u03ff\u0400-\u04ff\u0131\u017f\u0251\u0269\u028b\u2113]")
PID_MAX = 4194304


class ParamError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def _text(v) -> str:
    if isinstance(v, bool) or v is None or isinstance(v, (dict, list, tuple)):
        raise ParamError("type", "a text value is required")
    s = str(v)
    if INVISIBLE.search(s):
        raise ParamError("control_char", "NUL, newline, control, zero-width or bidi character")
    if unicodedata.normalize("NFKC", s) != s or LOOKALIKE.search(s):
        raise ParamError("lookalike", "Unicode look-alike or compatibility character")
    return s


def _int(v, spec) -> int:
    if isinstance(v, bool):
        raise ParamError("type", "integer required")
    if isinstance(v, int):
        n = v
    elif isinstance(v, str) and re.fullmatch(r"-?\d{1,12}", v.strip()):
        n = int(v.strip())
    else:
        raise ParamError("type", "integer required")
    lo, hi = spec.get("min"), spec.get("max")
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        raise ParamError("bounds", f"{n} outside [{lo}, {hi}]")
    return n


def _windows(spec) -> bool:
    roots = spec.get("roots") or []
    return bool(roots) and all(re.match(r"^[A-Za-z]:\\", r) for r in roots)


def _path(v, spec, local: bool) -> str:
    s = _text(v)
    if not s or len(s) > 255:
        raise ParamError("length", "1..255 characters")
    if s.startswith("-"):
        raise ParamError("leading_dash")
    if "~" in s:
        raise ParamError("tilde")
    if GLOB & set(s):
        raise ParamError("glob", "* ? [ ] { } are not allowed")
    win = _windows(spec)
    mod = ntpath if win else posixpath
    parts = re.split(r"[\\/]", s)
    if ".." in parts:
        raise ParamError("dotdot")
    if win:
        if not re.match(r"^[A-Za-z]:\\", s) or "/" in s or '"' in s or "^" in s:
            raise ParamError("not_absolute", "a Windows path like C:\\inetpub\\logs")
    elif not s.startswith("/") or "\\" in s:
        raise ParamError("not_absolute")
    norm = mod.normpath(s)
    roots = spec.get("roots") or []
    if not roots:
        raise ParamError("no_roots", "the template declares no roots")

    def under(p: str) -> bool:
        for r in roots:
            rn = mod.normpath(r)
            if win:
                p2, r2 = p.lower(), rn.lower()
            else:
                p2, r2 = p, rn
            if p2 == r2 or p2.startswith(r2.rstrip("\\/") + ("\\" if win else "/")) or r2 in ("/", ):
                return True
        return False

    if not under(norm):
        raise ParamError("outside_roots", f"allowed: {', '.join(roots)}")
    if local:
        real = os.path.realpath(norm)
        if not under(real):
            raise ParamError("symlink_escape", f"{norm} resolves outside the roots")
        if spec.get("must_exist") and not os.path.exists(real):
            raise ParamError("missing", norm)
        if spec.get("dir") and os.path.exists(real) and not os.path.isdir(real):
            raise ParamError("not_dir", norm)
    return norm


def _pattern(s: str, spec) -> None:
    pat = spec.get("pattern")
    if pat and not re.fullmatch(pat, s):
        raise ParamError("pattern", f"does not match {pat}")


def validate(spec: Dict[str, Any], value, ctx: Optional[Dict[str, Any]] = None, local: bool = False):
    """The normalised value, or ParamError(code). `ctx` carries what the server knows: case host, storm siblings, the
    inventory health_url, a connector's allowed hosts."""
    t = (spec or {}).get("type")
    ctx = ctx or {}
    if t not in TYPES:
        raise ParamError("unknown_type", f"{t!r} (there is no free-string type)")
    if t == "enum":
        s = _text(value)
        vals = spec.get("values")
        if vals is not None:
            if s not in [str(x) for x in vals]:
                raise ParamError("not_in_enum", s)
        elif not RX["enum_dyn"].match(s):               # values listed on the host (values_from): the actuator checks membership
            raise ParamError("charset", s)
        return s
    if t == "int":
        return _int(value, spec)
    if t in ("duration", "size"):
        s = _text(value)
        m = RX[t].match(s)
        if not m:
            raise ParamError("format", "like 30m / 7d" if t == "duration" else "like 500M / 2G")
        n = int(m.group(1)) * (DUR if t == "duration" else SIZE)[m.group(2)]
        lo, hi = spec.get("min"), spec.get("max")
        if (lo is not None and n < lo) or (hi is not None and n > hi):
            raise ParamError("bounds", f"{n} outside [{lo}, {hi}]")
        return s
    if t == "path":
        return _path(value, spec, local)
    if t in IDENT_FAMILY:
        s = _text(value)
        if s.startswith("-"):
            raise ParamError("leading_dash")
        if not RX[t].match(s):
            raise ParamError("charset", f"{t}: {s[:40]}")
        _pattern(s, spec)
        return s
    if t == "pid":
        if not isinstance(value, dict) or "pid" not in value or "start" not in value:
            raise ParamError("pid_pair", "a pid travels with its process start time: {pid, start}")
        pid = _int(value["pid"], {"min": 2, "max": PID_MAX})
        start = value["start"]
        if isinstance(start, bool) or not (isinstance(start, int) or (isinstance(start, str) and re.fullmatch(r"[0-9A-Za-z:._-]{1,64}", start))):
            raise ParamError("pid_start", "process start time required")
        return {"pid": pid, "start": start}
    if t == "url":
        s = _text(value)
        m = re.fullmatch(r"(https?)://([A-Za-z0-9.-]{1,253})(:\d{1,5})?(/[A-Za-z0-9._~/%-]*)?(\?[A-Za-z0-9._~=&%-]*)?", s)
        if not m:
            raise ParamError("format", "http(s)://host[:port]/path")
        host = m.group(2).lower()
        allowed = {h.lower() for h in [ctx.get("host", ""), "localhost", "127.0.0.1"] + list(ctx.get("allowed_hosts") or []) if h}
        health = (ctx.get("health_url") or "").strip()
        if s != health and host not in allowed:
            raise ParamError("url_host", f"{host} is not the case host, its health_url or an allowlisted host")
        return s
    if t == "host":
        s = _text(value)
        allowed = {ctx.get("case_host", "")} | set(ctx.get("siblings") or [])
        if s not in allowed or not s:
            raise ParamError("host_scope", "only the case host or a storm sibling")
        if ctx.get("connector_hosts") is not None and s not in set(ctx["connector_hosts"]):
            raise ParamError("connector_host", "the host is not in the connector's allowed-host set")
        return s
    raise ParamError("unknown_type", t)


def check_rendered(element: str) -> str:
    """A rendered argv element: never NUL or newline (§5.5)."""
    if "\x00" in element or "\n" in element or "\r" in element:
        raise ParamError("control_char", "rendered element contains NUL or newline")
    return element
