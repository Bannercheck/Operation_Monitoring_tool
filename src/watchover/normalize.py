"""Normalisation: timestamps (dateutil), severity, and the schema auto-mapper (column -> role)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from dateutil import parser as dtparser

from .models import SEV_RANK

UTC = timezone.utc

SEVERITY_MAP = {
    "trace": "DEBUG", "debug": "DEBUG", "info": "INFO", "informational": "INFO", "notice": "INFO", "ok": "INFO",
    "warn": "WARN", "warning": "WARN", "error": "ERROR", "err": "ERROR", "critical": "CRITICAL", "crit": "CRITICAL",
    "fatal": "CRITICAL", "alert": "CRITICAL", "emerg": "CRITICAL", "emergency": "CRITICAL", "severe": "CRITICAL",
    "high": "ERROR", "medium": "WARN", "low": "INFO", "p1": "CRITICAL", "p2": "ERROR", "p3": "WARN", "p4": "INFO",
    "firing": "ERROR", "resolved": "INFO", "major": "ERROR", "minor": "WARN",
    "path": "DEBUG", "config": "DEBUG", "fine": "DEBUG", "finer": "DEBUG", "finest": "DEBUG", "plain": "INFO",   # NetWeaver / JUL
    "e": "ERROR", "w": "WARN", "i": "INFO", "d": "DEBUG", "f": "CRITICAL",                                       # HANA trace letters
    "inf": "INFO", "wrn": "WARN", "ftl": "CRITICAL", "dbg": "DEBUG", "vrb": "DEBUG", "err": "ERROR",              # Serilog / .NET
    "panic": "CRITICAL", "information": "INFO", "verbose": "DEBUG", "audit_success": "INFO", "audit_failure": "WARN",
    "unknown": "INFO", "success": "INFO", "failed": "ERROR", "failure": "ERROR",
}
ROLE_HINTS = {
    "timestamp": ("timestamp", "time", "ts", "@timestamp", "datetime", "date", "event_time", "created_at", "logged_at", "t", "start_time",
                  "__realtime_timestamp", "timeunixnano", "eventtime", "t.$date", "systemtime", "rt", "devtime", "starttime", "observedtimeunixnano", "timecreated",
                  "startsat", "starts_at", "activeat", "active_at", "firedat", "opened_at", "created", "zaman", "tarih", "olusturma zamani",
                  "olusturma_zamani", "olusturmazamani", "acilis zamani", "kayit zamani"),
    "severity": ("severity", "level", "loglevel", "log_level", "priority", "sev", "labels.severity", "log.level", "severitytext", "levelname", "s",
                 "syslog.severity", "severity_label", "seviye", "oncelik", "onem", "kritiklik", "status"),
    "service": ("service", "app", "application", "logger", "module", "job", "program", "svc", "service_name", "service.name", "syslog_identifier",
                "_systemd_unit", "eventsource", "c", "logger_name", "appname", "app_name", "container_name", "kubernetes.container_name", "_service",
                "provider", "provider_name", "deviceproduct", "product", "alertname", "labels.job", "labels.service", "servis", "uygulama", "sistem", "kaynak sistem"),
    "environment": ("environment", "env", "stage", "tier", "deployment", "labels.env", "labels.environment", "ortam", "cevre", "asama"),
    "origin": ("source", "origin", "error_source", "component", "subsystem", "category", "layer", "error_type", "exception", "labels.component",
               "kaynak", "bilesen", "hata kaynagi", "kategori", "katman"),
    "host": ("host", "hostname", "node", "instance", "server", "pod", "container", "machine", "labels.instance", "_hostname", "host.name", "computer",
             "dvchost", "devname", "identhosthame", "identhostname", "kubernetes.pod_name", "sourcehost", "sunucu", "makine", "cihaz"),
    "message": ("message", "msg", "text", "description", "log", "summary", "title", "body", "line", "event", "alert", "short_message", "full_message",
                "body.stringvalue", "eventname", "errormessage", "name", "annotations.summary", "annotations.description", "ozet", "aciklama", "mesaj", "baslik", "konu"),
}
ENV_MAP = {"prod": "prod", "production": "prod", "prd": "prod", "live": "prod", "canli": "prod", "uretim": "prod",
           "test": "test", "tst": "test", "testing": "test", "qa": "qa", "uat": "uat", "acceptance": "uat", "kabul": "uat",
           "dev": "dev", "development": "dev", "develop": "dev", "gelistirme": "dev", "local": "dev", "sandbox": "dev",
           "staging": "staging", "stage": "staging", "stg": "staging", "preprod": "staging", "pre-prod": "staging", "canary": "staging",
           "dr": "dr", "disaster": "dr"}
ENV_TOKEN_RE = re.compile(r"(?<![a-z0-9])(prod|production|prd|test|tst|qa|uat|dev|development|staging|stage|stg|preprod|canary|sandbox|live)(?![a-z0-9])", re.I)
LEVEL_WORD_RE = re.compile(r"(?<![A-Za-z])(TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERR(?:OR)?|CRIT(?:ICAL)?|FATAL|ALERT|EMERG(?:ENCY)?|SEVERE|PANIC|INF|WRN|FTL|DBG|VRB)(?![A-Za-z])", re.I)
SYSLOG_TS_RE = re.compile(r"^[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2}$")
_DEFAULT = datetime(datetime.now().year, 1, 1)


_FAST_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S,%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S",
    "%d/%b/%Y:%H:%M:%S %z", "%b %d %H:%M:%S", "%Y%m%d%H%M%S", "%b %d %Y %H:%M:%S", "%d %b %Y %H:%M:%S.%f", "%d %b %Y %H:%M:%S",
    "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S %p", "%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z",
    "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%d-%b-%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%Y.%m.%d %H:%M:%S",
)
_format_cache: dict[str, str] = {}   # shape signature -> strptime format that worked last time


def _shape(s: str) -> str:
    """Signature of a timestamp string: digits -> 9, letters -> a, so equal-shaped strings share a format."""
    return re.sub(r"[A-Za-z]", "a", re.sub(r"\d", "9", s))


def _from_iso(s: str) -> datetime | None:
    """datetime.fromisoformat fast path; on 3.9/3.10 it needs 'Z' -> '+00:00' and no more than 6 fraction digits."""
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    if "." in s:
        m = re.match(r"^(.*?\.\d{6})\d+(.*)$", s)
        if m:
            s = m[1] + m[2]
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


_ts_cache: dict[str, datetime | None] = {}
_TS_CACHE_MAX = 200_000


def parse_timestamp(value: Any) -> datetime | None:
    """Parse epoch s/ms, ISO, common log formats, syslog; dateutil only as a last resort. Always tz-aware (naive -> UTC).
    Results are memoised per distinct string: a log carries the same second thousands of times."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return datetime.fromtimestamp(v / 1000 if v > 1e12 else v, tz=UTC)
    s = str(value).strip()
    if not s:
        return None
    hit = _ts_cache.get(s, _MISS)
    if hit is not _MISS:
        return hit
    if len(_ts_cache) >= _TS_CACHE_MAX:
        _ts_cache.clear()
    dt = _parse_timestamp_str(s)
    _ts_cache[s] = dt
    return dt


_MISS = object()


def _parse_timestamp_str(s: str) -> datetime | None:
    if s.isdigit():
        if len(s) == 13:
            return datetime.fromtimestamp(int(s) / 1000, tz=UTC)
        if len(s) == 10:
            return datetime.fromtimestamp(int(s), tz=UTC)
        if len(s) == 16:                                                    # microseconds (journald __REALTIME_TIMESTAMP)
            return datetime.fromtimestamp(int(s) / 1_000_000, tz=UTC)
        if len(s) == 19:                                                    # nanoseconds (OpenTelemetry timeUnixNano)
            return datetime.fromtimestamp(int(s) / 1_000_000_000, tz=UTC)
        if len(s) == 14:                                                    # 20260922091700
            return datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    if re.fullmatch(r"\d{8}T\d{6}(?:\.\d+)?Z?", s):                          # 20260922T091700Z (compact ISO)
        return datetime.strptime(s[:15], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    if re.fullmatch(r"\d{10}\.\d+", s):
        return datetime.fromtimestamp(float(s), tz=UTC)
    dt = None
    if re.search(r" [+\-]\d{2}$", s):                                        # PostgreSQL "2026-09-22 09:14:31.001 +03": strptime wants +0300
        s = s + "00"
    if len(s) >= 19 and s[4] == "-" and s[7] == "-":
        dt = _from_iso(s)
    if dt is None:
        shape = _shape(s)
        fmt = _format_cache.get(shape)
        if fmt:
            try:
                dt = datetime.strptime(s, fmt)
            except ValueError:
                dt = None
        if dt is None:
            for fmt in _FAST_FORMATS:
                try:
                    dt = datetime.strptime(s, fmt)
                    _format_cache[shape] = fmt
                    break
                except ValueError:
                    continue
        if dt is not None and fmt == "%b %d %H:%M:%S":
            dt = dt.replace(year=datetime.now().year)
    if dt is None:
        try:
            dt = dtparser.parse(s.replace(",", ".", 1) if re.search(r"\d,\d{3}$", s) else s, default=_DEFAULT,
                                dayfirst=bool(re.match(r"\d{2}[./]\d{2}[./]\d{4}", s)))
        except (ValueError, OverflowError, TypeError):
            return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


_sev_cache: dict[tuple, str] = {}


def column_severity(value: Any, use_scale: bool = True) -> str:
    """Severity taken from a dataset column: the scenario's own numeric scale wins (S-A1: 1-5), then the generic rules.
    use_scale=False for parsers whose numbers already have a fixed meaning (syslog PRI). Memoised per distinct value."""
    key = (value if isinstance(value, (str, int, float)) else str(value), use_scale)
    hit = _sev_cache.get(key)
    if hit is not None:
        return hit
    from . import scenario
    s = str(value).strip().lower() if value is not None else ""
    out = scenario.SEVERITY_MAP[s] if use_scale and s in scenario.SEVERITY_MAP else normalize_severity(value)
    if len(_sev_cache) < 50_000:
        _sev_cache[key] = out
    return out


def normalize_severity(value: Any) -> str:
    if value is None:
        return "INFO"
    s = str(value).strip().lower()
    if s in SEVERITY_MAP:
        return SEVERITY_MAP[s]
    if s.isdigit():
        return {0: "CRITICAL", 1: "CRITICAL", 2: "CRITICAL", 3: "ERROR", 4: "WARN"}.get(int(s), "INFO")
    up = s.upper()
    return up if up in SEV_RANK else "INFO"


def normalize_environment(value: Any) -> str:
    """'Production' / 'PRD' / 'prod-eu' -> 'prod'; unknown values are kept lower-cased."""
    if value is None:
        return ""
    v = str(value).strip().lower()
    if not v:
        return ""
    if v in ENV_MAP:
        return ENV_MAP[v]
    m = ENV_TOKEN_RE.search(v)
    return ENV_MAP[m[1].lower()] if m else v[:20]


_env_cache: dict[tuple, str] = {}


def infer_environment(*texts: str) -> str:
    """Guess the environment from host / service names or the message ('prd-api-01' -> prod, 'dev-worker' -> dev).
    The leading (host, service, source) part is memoised; the message is only searched when they give nothing."""
    head = texts[:3]
    hit = _env_cache.get(head)
    if hit is None:
        hit = ""
        for txt in head:
            if txt:
                m = ENV_TOKEN_RE.search(txt)
                if m:
                    hit = ENV_MAP[m[1].lower()]; break
        if len(_env_cache) < 50_000:
            _env_cache[head] = hit
    if hit:
        return hit
    for txt in texts[3:]:
        if txt:
            m = ENV_TOKEN_RE.search(txt)
            if m:
                return ENV_MAP[m[1].lower()]
    return ""


ERROR_HINT_RE = re.compile(r"(ORA-\d{5}|APP-FND-\d{5}|FRM-\d{5}|TNS-\d{5}|Traceback \(most recent call last\)|\b[A-Za-z_.$]*(?:Exception|Error)\b: |deadlock detected|Out of memory|"
                           r"\bsegfault\b|\bpanic:|\bkernel panic|\bStack overflow\b|\bBACKUP failed\b|\bLogin failed\b|\bAssertion failure\b|\bcircuit breaker open\b)", re.I)
WARN_HINT_RE = re.compile(r"(\btimed out\b|\btimeout\b|\bretry\b|\bretrying\b|\bdeferred\b|\bdenied\b|\brefused\b|\bslow query\b|\bunreachable\b|\bdegraded\b|\bdeprecated\b)", re.I)


def severity_from_text(text: str) -> str:
    """Level word when the line has one; otherwise conservative hints (ORA-xxxxx, tracebacks, deadlocks -> ERROR; timeouts, denials -> WARN)."""
    m = LEVEL_WORD_RE.search(text)
    if m:
        return normalize_severity(m[1])
    if ERROR_HINT_RE.search(text):
        return "ERROR"
    if WARN_HINT_RE.search(text):
        return "WARN"
    return "INFO"


_TR = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


def _norm_key(k: str) -> str:
    return k.translate(_TR).lower().strip()


def auto_map(keys: list[str], sample: list[dict] | None = None, mapping: dict | None = None) -> dict[str, str | None]:
    """Schema auto-mapper: which key plays timestamp / severity / service / host / message.

    Order: explicit mapping > exact name hint > suffix hint > value-based guess (timestamp-looking, level-looking, longest text).
    """
    lowered = {_norm_key(k): k for k in keys}
    out: dict[str, str | None] = {}
    for role, hints in ROLE_HINTS.items():
        if mapping and mapping.get(role) in keys:
            out[role] = mapping[role]
            continue
        cands = [lowered[h] for h in hints if h in lowered]
        hit = cands[0] if cands else None
        if len(cands) > 1 and sample:                                   # "severity" on one row, "level" on every row: take the one that is filled
            hit = max(cands, key=lambda k: (sum(1 for r in sample if r.get(k) not in (None, "")), -cands.index(k)))
        if hit is None:
            hit = next((k for lk, k in lowered.items()
                        if any(lk.endswith(sep + h) for h in hints for sep in ("_", ".", "-", " "))), None)
        out[role] = hit
    if sample:
        taken = {v for v in out.values() if v}
        def share(k, pred):
            vals = [r.get(k) for r in sample if r.get(k) not in (None, "")]
            return sum(1 for v in vals if pred(v)) / len(vals) if vals else 0.0
        if out["timestamp"] is None:
            best = max((k for k in keys if k not in taken), key=lambda k: share(k, lambda v: parse_timestamp(v) is not None and len(str(v)) >= 8), default=None)
            if best and share(best, lambda v: parse_timestamp(v) is not None and len(str(v)) >= 8) > 0.8:
                out["timestamp"] = best; taken.add(best)
        if out["severity"] is None:
            best = max((k for k in keys if k not in taken), key=lambda k: share(k, lambda v: str(v).lower() in SEVERITY_MAP), default=None)
            if best and share(best, lambda v: str(v).lower() in SEVERITY_MAP) > 0.8:
                out["severity"] = best; taken.add(best)
        if out["environment"] is None:
            best = max((k for k in keys if k not in taken), key=lambda k: share(k, lambda v: str(v).strip().lower() in ENV_MAP), default=None)
            if best and share(best, lambda v: str(v).strip().lower() in ENV_MAP) > 0.8:
                out["environment"] = best; taken.add(best)
        if out["message"] is None:
            def numeric(v):
                try:
                    float(str(v)); return True
                except ValueError:
                    return False
            cands = [k for k in keys if k not in taken and share(k, numeric) < 0.5]
            best = max(cands, key=lambda k: sum(len(str(r.get(k, ""))) for r in sample), default=None)
            out["message"] = best
    return out


def flatten(d: dict, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out
