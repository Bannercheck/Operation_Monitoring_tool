"""Personal data never leaves the sensor as itself (KVKK / GDPR by default).

* `pseudonym(value)`  user ids, e-mails, personnel numbers -> a stable, keyed token (HMAC-SHA256 with this installation's key):
  the same person stays the same token across sessions and apps, nobody can reverse it, and another installation computes
  different tokens. The key lives in <WATCHOVER_HOME>/.pseudo.key (0600) or WATCHOVER_PSEUDO_KEY.
* `scrub(text)`       Turkish ID numbers (TCKN, with checksum), phone numbers, e-mails, IBANs, card numbers (Luhn), bearer tokens
  and password / token fields are masked in any free text (log message, URL, error, click label).
* `path_template(p)`  /api/employee/12345/payslips?month=3 -> /api/employee/{id}/payslips: identifiers and query strings
  never reach storage, so an HR app's URLs carry no personnel number.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path

_KEY: bytes | None = None


def _key() -> bytes:
    global _KEY
    if _KEY is None:
        env = os.environ.get("WATCHOVER_PSEUDO_KEY", "")
        if env:
            _KEY = env.encode()
        else:
            p = Path(os.environ.get("WATCHOVER_HOME", "") or "data") / ".pseudo.key"
            try:
                if p.exists():
                    _KEY = p.read_bytes().strip()
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    k = secrets.token_hex(32).encode()
                    p.write_bytes(k)
                    try:
                        os.chmod(p, 0o600)
                    except OSError:
                        pass
                    _KEY = k
            except OSError:                                    # read-only home: a per-process key keeps tokens unlinkable to people
                _KEY = secrets.token_hex(32).encode()
    return _KEY


def pseudonym(value: str | None, prefix: str = "u") -> str:
    v = (value or "").strip().lower()
    if not v or v in ("-", "anonymous", "null", "none"):
        return ""
    return f"{prefix}_{hmac.new(_key(), v.encode(), hashlib.sha256).hexdigest()[:16]}"


def _tckn_ok(d: str) -> bool:
    if len(d) != 11 or d[0] == "0" or not d.isdigit():
        return False
    n = [int(c) for c in d]
    return (sum(n[0:9:2]) * 7 - sum(n[1:8:2])) % 10 == n[9] and sum(n[:10]) % 10 == n[10]


def _luhn_ok(d: str) -> bool:
    s, alt = 0, False
    for c in reversed(d):
        x = int(c)
        if alt:
            x = x * 2 - 9 if x > 4 else x * 2
        s += x; alt = not alt
    return s % 10 == 0


EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
IBAN = re.compile(r"\bTR\d{2}(?:\s?\d{4}){5}\s?\d{2}\b|\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
PHONE = re.compile(r"(?<![\w])(?:\+?90[\s\-]?)?\(?0?5\d{2}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?![\w])")
DIGITS = re.compile(r"(?<![\w])\d[\d \-]{9,22}\d(?![\w])")
SECRET = re.compile(r"(?i)\b(password|passwd|pwd|parola|sifre|şifre|token|secret|api[_-]?key|authorization)\b(\s*[=:]\s*|\"\s*:\s*\")(Bearer\s+)?[^\s&\",;]+")
BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-~+/]{12,}=*")


def _digits(m: re.Match) -> str:
    raw = m.group(0); d = re.sub(r"\D", "", raw)
    if len(d) == 11 and _tckn_ok(d):
        return "[TCKN]"
    if 13 <= len(d) <= 19 and _luhn_ok(d):
        return "[KART]"
    return raw


def scrub(text: str | None) -> str:
    if not text:
        return text or ""
    t = SECRET.sub(lambda m: m.group(1) + m.group(2) + "***", text)
    t = BEARER.sub("Bearer ***", t)
    t = EMAIL.sub("[E-POSTA]", t)
    t = IBAN.sub("[IBAN]", t)
    t = PHONE.sub("[TEL]", t)
    t = DIGITS.sub(_digits, t)
    return re.sub(r"(?<!\d)(\d{11})(?!\d)", lambda m: "[TCKN]" if _tckn_ok(m.group(1)) else m.group(1), t)


SEG_ID = re.compile(r"^(?:\d+|[0-9a-fA-F]{8}-[0-9a-fA-F\-]{27,}|[0-9a-fA-F]{12,}|[A-Za-z0-9_\-]{20,}|.*@.*|[A-Z]{2}\d{2}[A-Z0-9]{10,})$")


def path_template(path: str | None, max_segments: int = 8) -> str:
    """URL path with identifiers replaced by {id} and the query string dropped."""
    if not path:
        return ""
    p = path.split("?", 1)[0].split("#", 1)[0]
    if "://" in p:
        p = "/" + p.split("://", 1)[1].split("/", 1)[-1] if "/" in p.split("://", 1)[1] else "/"
    segs = [("{id}" if SEG_ID.match(s) or re.search(r"\d{4,}", s) else s) for s in p.split("/")]
    out = "/".join(segs[: max_segments + 1])
    return out if out.startswith("/") else "/" + out


# ================================================================ strict scrubbing for the action agent (R12, C34, K6)
# `scrub()` above stays exactly as it is for ingest. Everything that may become evidence, a prompt, a training example, an audit
# payload or a memory lesson in the action agent goes through `scrub_strict()` (free text) or `scrub_fields()` (structured output,
# field by field, so numbers stay numbers). The host actuator embeds the same rules; tests/fixtures/privacy_parity.jsonl is the
# fixture both must reproduce byte for byte. docs/design/action-agent.md §10.8.
import bisect as _bisect
import json as _json
import math as _math
import sys as _sys
import unicodedata as _ud
from typing import NamedTuple as _NamedTuple

ZW_BIDI = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")

# Latin / Cyrillic / Greek look-alikes folded to their Latin skeleton (a subset of Unicode TR39 confusables.txt for these scripts)
CONFUSABLES = {
    # Cyrillic lower / upper
    "а": "a", "в": "b", "е": "e", "ё": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ї": "i", "ј": "j",
    "ѕ": "s", "ԁ": "d", "һ": "h", "ӏ": "l", "ԛ": "q", "ԝ": "w", "к": "k", "м": "m", "н": "h", "т": "t", "п": "n", "г": "r",
    "ь": "b", "ɡ": "g",
    "А": "A", "В": "B", "Е": "E", "Ё": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X",
    "У": "Y", "Ѕ": "S", "І": "I", "Ї": "I", "Ј": "J", "Ԁ": "D", "Ԛ": "Q", "Ԝ": "W", "Ӏ": "I",
    # Greek lower / upper
    "α": "a", "β": "b", "γ": "y", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
    "ω": "w", "η": "n", "μ": "u",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T",
    "Υ": "Y", "Χ": "X",
    # Latin variants that NFKC keeps
    "ı": "i", "ȷ": "j", "ſ": "s", "ɑ": "a", "ɩ": "i", "ʋ": "v", "ℓ": "l",
}
_CONF_TABLE = str.maketrans(CONFUSABLES)


class Folded(_NamedTuple):
    text: str
    hits: tuple          # "zero_width_bidi" when invisible / direction controls were removed, "confusable" when look-alikes were folded


def fold(text, confusables: bool = True) -> Folded:
    """NFKC, invisible and bidi controls removed (recorded), and optionally the Latin skeleton of look-alike letters. The
    skeleton is for matching only (quarantine patterns); a text shown to people keeps its own letters."""
    if not text:
        return Folded(text or "", ())
    hits = []
    t = _ud.normalize("NFKC", text)
    if ZW_BIDI.search(t):
        t = ZW_BIDI.sub("", t)
        hits.append("zero_width_bidi")
    if confusables:
        s = t.translate(_CONF_TABLE)
        if s != t:
            hits.append("confusable")
        t = s
    return Folded(t, tuple(hits))


def skeleton(text) -> str:
    return fold(text, confusables=True).text


_SECRET_WORDS = (r"pass(?:word|wd)?|pwd|secret|token|api[_-]?key|private[_-]?key|client[_-]?secret|access[_-]?key|"
                 r"keystore[_-]?pass\w*|truststore[_-]?pass\w*")
STRICT_RULES = (
    ("pem", re.compile(r"-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)-----.*?(?:-----END [A-Z0-9 ]*-----|\Z)", re.S), "[PEM]"),
    ("url_userinfo", re.compile(r"(\b[A-Za-z][\w+.-]*://)[^/\s:@]+:[^@\s/]+@"), r"\1[KIMLIK]@"),
    ("oracle_userpass", re.compile(r"(?i)(jdbc:oracle:\w+:|sqlplus\s+)[^/\s@]+/[^@\s]+@"), r"\1[KIMLIK]@"),
    ("env_secret", re.compile(r"(?i)([A-Z0-9_.-]*(?:" + _SECRET_WORDS + r")\s*[=:]\s*)\S+"), r"\1[GIZLI]"),
    # -p only in lower case: mysql's password flag; "-P" is a port there and df's POSIX flag
    ("flag_secret", re.compile(r"((?:\s|^)(?:-p|(?i:--pass(?:word)?|--token|--secret|--api-key))(?:\s+|=))\S+", re.M), r"\1[GIZLI]"),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[GIZLI]"),
    ("jwt", re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"), "[GIZLI]"),
    ("session", re.compile(r"(?i)((?:jsessionid|sessionid|sid|session|cookie|set-cookie)[\w-]*\s*[=:]\s*)[^;\s]{12,}"), r"\1[GIZLI]"),
)
ENTROPY_RUN = re.compile(r"[A-Za-z0-9+/=_-]{20,}")
ENTROPY_BITS = 4.0


def _shannon(s: str) -> float:
    n = len(s)
    counts = {}
    for c in s:
        counts[c] = counts.get(c, 0) + 1
    return -sum(k / n * _math.log2(k / n) for k in counts.values())


def _high_entropy(s: str) -> bool:
    """Unlabelled tokens: both cases and digits (hex hashes and our own ids are single-case) and >= 4.0 bits per character."""
    return (any(c.islower() for c in s) and any(c.isupper() for c in s) and any(c.isdigit() for c in s)
            and _shannon(s) >= ENTROPY_BITS)


def scrub_strict(text) -> str:
    """Everything `scrub` masks plus host-side credentials: URL userinfo, env / property and flag passwords, keystore passwords,
    PEM blocks (also unterminated), AWS keys, JWTs, session cookies and unlabelled high-entropy tokens; after NFKC folding."""
    if not text:
        return text or ""
    t = fold(str(text), confusables=False).text
    for _name, rx, rep in STRICT_RULES:
        t = rx.sub(rep, t)
    t = scrub(t)
    return ENTROPY_RUN.sub(lambda m: "[GIZLI]" if _high_entropy(m.group(0)) else m.group(0), t)


SECRET_KEY = re.compile(r"(?i)(" + _SECRET_WORDS + r"|credential|cookie|authorization)")
NUMBER_TEXT = re.compile(r"^-?\d+(?:[.,]\d+)?$")
IDENT_TEXT = re.compile(r"^[A-Za-z0-9_.:@-]{1,64}$")
HOME_SEG = re.compile(r"^(/home/|/Users/|C:\\Users\\)([^/\\]+)", re.I)
FIELD_TYPES = ("number", "enum", "ident", "path", "text")


def scrub_fields(fields, schema: dict | None = None):
    """Structured output scrubbed field by field (C34). Numbers stay numbers (`"bytes_before": 5368709120` is never [TEL], a
    16-digit size is never [KART]); a field declared number / enum / ident / path skips the text patterns (a path only gets its
    /home/<user> segment pseudonymised); `text` and undeclared strings go through `scrub_strict`; a string under a secret-like
    key (password, token, api_key, ...) is masked whatever it holds. Nested dicts and lists keep their shape."""
    schema = schema or {}

    def one(key, v, typ):
        if v is None or isinstance(v, (bool, int, float)):
            return v
        if isinstance(v, dict):
            sub = typ if isinstance(typ, dict) else {}
            return {k: one(k, x, sub.get(k)) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [one(key, x, typ) for x in v]
        s = str(v)
        if key and SECRET_KEY.search(str(key)) and typ not in ("number", "enum"):
            return "[GIZLI]" if s else s
        if typ == "number" and NUMBER_TEXT.match(s.strip()):
            return s
        if typ == "enum":
            return s if IDENT_TEXT.match(s) else scrub_strict(s)
        if typ == "ident" and IDENT_TEXT.match(s):
            return s
        if typ == "path" and "\n" not in s and "\x00" not in s:
            return HOME_SEG.sub(lambda m: m.group(1) + pseudonym(m.group(2)), s)
        return scrub_strict(s)

    return one(None, fields, schema)


# Planted host credentials (§10.8 layer 4): none of these may survive scrub_strict, and the canary gate (WP14) refuses any
# training dataset in which one appears. tests/fixtures/privacy_canaries.txt holds them inside realistic command output.
CANARIES = (
    "Canary-Pg-7f3a", "Canary-Ora-91", "Canary-Env-22", "Canary-Ks-5", "Canary-Flag-8", "Canary-Oidc-3",
    "AKIACANARY0000000001", "CanarySess0123456789abcdef", "MIIEowIBAAKCAQEAcanaryRSAprivateKeyBodyLine0123456789",
)


def canary_hits(text) -> list:
    """The canaries present in a text (or in any string of a JSON-able object)."""
    s = text if isinstance(text, str) else _json.dumps(text, ensure_ascii=False, default=str)
    return [c for c in CANARIES if c in s]


_BASE_RULES = (("secret", SECRET), ("bearer", BEARER), ("email", EMAIL), ("iban", IBAN), ("phone", PHONE))


def rule_hits(text) -> list:
    """(rule, line number, start column) for every strict rule that fires; used by `python -m watchover.privacy check`."""
    out = []
    for no, line in enumerate((text or "").splitlines() or [""], 1):
        if ZW_BIDI.search(line):
            out.append(("zero_width_bidi", no, ZW_BIDI.search(line).start()))
    folded = fold(text or "", confusables=False).text
    lines = folded.splitlines()
    starts, pos = [], 0
    for line in lines:
        starts.append(pos); pos += len(line) + 1

    def line_of(i):
        k = max(0, _bisect.bisect_right(starts, i) - 1)
        return k + 1, (i - starts[k]) if starts else 0

    for name, rx, _rep in STRICT_RULES:
        for m in rx.finditer(folded):
            ln, col = line_of(m.start()); out.append((name, ln, col))
    for name, rx in _BASE_RULES:
        for m in rx.finditer(folded):
            ln, col = line_of(m.start()); out.append((name, ln, col))
    for m in DIGITS.finditer(folded):
        if _digits(m) != m.group(0):
            ln, col = line_of(m.start()); out.append(("tckn_card", ln, col))
    for m in ENTROPY_RUN.finditer(folded):
        if _high_entropy(m.group(0)):
            ln, col = line_of(m.start()); out.append(("entropy", ln, col))
    return sorted(out, key=lambda x: (x[1], x[2], x[0]))


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="watchover.privacy", description="strict scrubbing check: which rules fire, by line")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="list the strict-scrub hits of a file by rule (values are never printed)")
    c.add_argument("file")
    c.add_argument("--show", action="store_true", help="also print each hit line after scrub_strict")
    a = ap.parse_args(argv)
    text = Path(a.file).read_text(encoding="utf-8", errors="replace")
    hits = rule_hits(text)
    by = {}
    for name, ln, _col in hits:
        by.setdefault(name, []).append(ln)
    for name in sorted(by):
        lines = sorted(set(by[name]))
        print(f"{name:<16} {len(by[name]):>5}  lines {', '.join(map(str, lines[:20]))}{' …' if len(lines) > 20 else ''}")
    left = canary_hits(scrub_strict(text))
    print(f"total {len(hits)} hits in {len({ln for _, ln, _ in hits})} lines · canaries left after scrub_strict: {len(left)}")
    if a.show:
        for ln in sorted({ln for _, ln, _ in hits}):
            print(f"{ln:>6}: {scrub_strict(text.splitlines()[ln - 1])[:200]}")
    return 1 if left else 0


if __name__ == "__main__":
    _sys.exit(main())
