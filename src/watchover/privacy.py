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
