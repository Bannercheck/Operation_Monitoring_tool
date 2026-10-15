"""Stored log lines, format v2 (docs/design/logs.md section 4): what `live_batches` keeps so the log explorer can drill into a
line after it left the ring, page through a range exactly once, and prune rows before decompressing them.

    v1 (old rows)  payload = JSONL text of `LiveStore._pack` dicts; every reader still understands it
    v2             pz = zlib(level 1) of the JSONL; each line adds q (seq), ln (line number), raw (scrubbed), at (attributes after the
                   privacy transform), tr (trace id), prs (parser), mt (message cut); the row records seq_min / seq_max, a severity
                   mask, its single host and file (or '') so scans skip rows without decompressing them. One row spans < 1 minute.

Privacy (privacy.py, KVKK): session / user / client-address attributes are stored as their pseudonyms (the same tokens the
operations screen shows), secret-named keys as "***", every other string scrubbed; the raw line never shows a value that was
pseudonymised in the attributes. `msg` is stored as before (other readers use it); the explorer always shows it scrubbed.
"""
from __future__ import annotations

import hashlib
import json
import re
import zlib
from datetime import datetime, timezone
from typing import Iterator, Optional

from . import privacy
from .rollup import IP_LIKE, LATENCY_RE, PSEUDO, SES_ROLES, session_of

UTC = timezone.utc
ROW_SPAN_S = 60                               # one v2 row spans less than a minute of line time (a scan bounds its index walk by it)
SEVS = ("DEBUG", "INFO", "WARN", "ERROR", "CRITICAL")
SEV_BIT = {s: 1 << i for i, s in enumerate(SEVS)}
CFG = {"raw": True, "msg_max": 16384, "raw_max": 2048, "attr_max": 2048, "attr_keys": 32, "key_max": 64, "val_max": 256, "trace_max": 64}
DROP = {"agent", "site", "agent_id", "_no_ts", "_ses"}
SECRET_KEYS = {"password", "passwd", "pwd", "parola", "sifre", "şifre", "token", "secret", "api_key", "api-key", "apikey", "authorization",
               "cookie", "set-cookie", "jwt", "access_token", "refresh_token", "client_secret"}
V1_ID_BASE = 1_000_000                         # lines of v1 rows (no seq) get the id -(row id * V1_ID_BASE + index)
_SCRUB: dict = {}
# Every text privacy.scrub can change contains one of these (the mandatory core of each of its patterns: a secret keyword, bearer,
# the '@' of an e-mail, IBAN letters + check digits + account digits, 11+ digits of a TCKN / card, the 5xx xxx xx xx core of a mobile
# number). A text without any of them is returned as it is: ISO timestamps and status codes no longer cost seven substitutions.
_KW = ("password", "passwd", "pwd", "parola", "sifre", "token", "secret", "apikey", "api_key", "api-key", "authorization", "bearer")
# privacy.SECRET is case-insensitive the way `re` folds case: İ, I and ı match i, ſ (long s) matches s, the Kelvin sign matches k (lower()
# handles it); ş / Ş fold to s so "sifre" covers "şifre"
_FOLD = str.maketrans({"İ": "i", "I": "i", "ı": "i", "Ş": "s", "ş": "s", "ſ": "s"})
_IBAN = re.compile(r"[A-Z]{2}\d{2}(?:[A-Z0-9]{11}|\s?\d{4})")
_RUN = re.compile(r"[\d\s\-()+]{10,}")                       # no nested quantifier: linear, C speed
_PHONE_CORE = re.compile(r"5\d{2}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")
_NODIGIT = str.maketrans("", "", "0123456789")
# ASCII text, one bytes.translate: a digit -> "0", a character of a digit run ([\s\-()+], exactly what `re` matches in ASCII) -> " ",
# A-Z -> "A", "@" -> "@", anything else -> ".". "AA00" then marks a possible IBAN start, and a run of [\d\s\-()+] holding 10+ digits (the
# least a mobile number, a TCKN or a card has) becomes "0" x 10 once the separators are removed.
_MAP = bytes((ord("0") if re.match(r"\d", chr(c)) else ord(" ") if re.match(r"[\s\-()+]", chr(c)) else ord("A") if "A" <= chr(c) <= "Z"
              else ord("@") if c == 64 else ord(".")) if c < 128 else ord(".") for c in range(256))


def _runs(t: str) -> bool:
    """A run of [\\d\\s\\-()+] with 11+ digits (TCKN, card) or a mobile number core. Digits are what `re` calls \\d, any Unicode decimal
    digit: privacy.scrub masks a TCKN written in Arabic-Indic or full-width digits too."""
    for m in _RUN.finditer(t):
        run = m.group(0)
        nd = len(run) - len(run.translate(_NODIGIT)) if run.isascii() else sum(1 for c in run if c.isdecimal())
        if nd >= 11 or _PHONE_CORE.search(run):
            return True
    return False


# The same map over UTF-8 bytes: a lead byte that can start a non-ASCII digit or white space (U+0085, U+00A0, Arabic-Indic and NKo digits,
# anything from U+0800 on) -> "!" (the text then takes the exact check); every other non-ASCII byte (Turkish and other Latin letters,
# Greek, Cyrillic ...) -> "." like any character that cannot be part of a masked number.
_DANGER = {0xC0 | (c >> 6) for c in range(0x80, 0x800) if re.match(r"[\d\s]", chr(c))} | set(range(0xE0, 0x100))
_MAP8 = bytes(_MAP[c] if c < 128 else ord("!") if c in _DANGER else ord(".") for c in range(256))


def _kw(lo: str) -> bool:
    return ("passw" in lo or "pwd" in lo or "token" in lo or "secret" in lo or "bearer" in lo or "authorization" in lo or "parola" in lo
            or "sifre" in lo or "apikey" in lo or "api_key" in lo or "api-key" in lo)


def maybe_pii(t: str) -> bool:
    """False only when privacy.scrub cannot change t: a superset of every pattern it masks (tests/test_logsearch.py fuzzes it against
    privacy.scrub). One bytes.translate of the UTF-8 text and a few substring searches; the regexes run only on a candidate."""
    m = t.encode("utf-8", "surrogatepass").translate(_MAP8)
    if b"@" in m:
        return True
    if t.isascii():
        if _kw(t.lower()):
            return True
    else:
        if "İ" in t or "ı" in t or "ſ" in t:                 # the only letters `re` folds to i / s that lower() does not
            t2 = t.replace("İ", "i").replace("ı", "i").replace("ſ", "s").lower()
        else:
            t2 = t.lower()
        if _kw(t2) or "şifre" in t2:
            return True
        if b"!" in m:
            return _IBAN.search(t) is not None or _runs(t)
    if b"AA00" in m and _IBAN.search(t):
        return True
    return b"0000000000" in m.replace(b" ", b"") and _runs(t)


def configure(cfg: dict) -> None:
    """settings.py: logs_raw, logs_attr_max_bytes, logs_msg_max_bytes."""
    CFG["raw"] = bool(cfg.get("logs_raw", True))
    CFG["attr_max"] = max(256, int(cfg.get("logs_attr_max_bytes", 2048) or 2048))
    CFG["msg_max"] = max(1024, int(cfg.get("logs_msg_max_bytes", 16384) or 16384))


def scrub_text(s: str) -> str:
    """privacy.scrub with a one-regex precheck (identical result)."""
    if not s or not maybe_pii(s):
        return s or ""
    return privacy.scrub(s)


def scrub(s: str) -> str:
    """privacy.scrub, memoised for short repeated values (thread names, status texts, paths)."""
    if len(s) > 200:
        return scrub_text(s)
    r = _SCRUB.get(s)
    if r is None:
        r = scrub_text(s)
        if len(_SCRUB) > 100_000:
            _SCRUB.clear()
        _SCRUB[s] = r
    return r


_IBAN_I = re.compile(privacy.IBAN.pattern, re.I)


def scrub_template(t: str) -> str:
    """A message template as shown (groups, line detail): templates are lower-cased and only digits are masked, so e-mails,
    secrets and IBANs would show in clear; privacy.scrub plus a case-insensitive IBAN mask."""
    return _IBAN_I.sub("[iban]", scrub(t)) if t else ""


def utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def iso_s(ts: datetime) -> str:
    return utc(ts).astimezone(UTC).isoformat(timespec="seconds")


def group_key(template: str, service: str) -> str:
    """Stable key of a message pattern (template x service), as group:<key> in the query language."""
    return hashlib.sha1(f"{template}\x00{service or '-'}".encode("utf-8", "replace")).hexdigest()[:12]


# ---------------------------------------------------------------- privacy transform
_KI: dict = {}          # attribute name as sent -> (skip, stored key, session role, secret-named): one lower() per distinct name, not per line
_NUM = (int, float, bool)


def _kinfo(k) -> tuple:
    ks = str(k)
    lk = ks.lower()
    ki = (k in DROP or (k.__class__ is str and k.startswith("discovery_")), ks[:CFG["key_max"]], SES_ROLES.get(lk, ""), lk in SECRET_KEYS)
    if len(_KI) > 20_000:
        _KI.clear()
    _KI[k] = ki
    return ki


def transform_attrs(a: dict) -> tuple:
    """(attributes after the privacy transform, dropped count, [(original, pseudonym)] for the raw line)."""
    at: dict = {}
    repl: list = []
    dropped = 0
    size = 0
    vmax = CFG["val_max"]
    for k, v in a.items():
        if v is None:
            continue
        ki = _KI.get(k) or _kinfo(k)
        if ki[0]:
            continue
        role = ki[2]
        pv = None
        if role:
            ps = None                                         # the prefix when the value is replaced by its pseudonym
            if role in ("s", "S"):
                ps, src_v = "s", str(v)[:128]
            elif role == "u":
                ps, src_v = "u", str(v)
            elif role == "R" and not PSEUDO.match(str(v)):
                ps, src_v = "u", str(v)
            elif role == "c" or (role == "a" and IP_LIKE.match(str(v))):
                ps, src_v = "c", str(v)
            if ps is not None:
                pv = privacy.pseudonym(src_v, ps)
                if len(str(v)) >= 3 and pv:
                    repl.append((str(v), pv))
                if not pv:
                    continue
            elif role == "x":
                parts = [x.strip() for x in str(v).split(",") if x.strip()]
                pv = ", ".join(privacy.pseudonym(x, "c") for x in parts)
                repl.extend((x, privacy.pseudonym(x, "c")) for x in parts if len(x) >= 3)
        if pv is None:
            if ki[3]:
                pv = "***"
            elif v.__class__ is str:
                pv = scrub(v[:vmax * 4])[:vmax]
            elif v.__class__ in _NUM:
                pv = v
            else:
                pv = scrub(json.dumps(v, ensure_ascii=False, default=str)[:vmax * 4])[:vmax]
        key = ki[1]
        cost = len(key) + (len(pv) if pv.__class__ is str else 8)
        if len(at) >= CFG["attr_keys"] or size + cost > CFG["attr_max"]:
            dropped += 1
            continue
        size += cost
        at[key] = pv
    return at, dropped, repl


def transform_raw(raw: str, msg: str, repl: list) -> tuple:
    """(stored raw or None, state): state 'same' when raw equals the message, 'absent' when over the cap or switched off."""
    if not raw:
        return None, "same"
    msg = msg or ""
    if raw == msg:
        return None, "same"
    rs, ms = raw.strip(), msg.strip()                         # equal after whitespace normalisation needs equal first and last characters
    if rs[:1] == ms[:1] and rs[-1:] == ms[-1:] and " ".join(raw.split()) == " ".join(msg.split()):
        return None, "same"
    if not CFG["raw"] or len(raw) > CFG["raw_max"] * 2:
        return None, "absent"
    for orig, ps in sorted(repl, key=lambda x: -len(x[0])):
        raw = raw.replace(orig, ps)
    raw = scrub_text(raw)
    if len(raw) > CFG["raw_max"]:
        return None, "absent"
    return raw, "stored"


_TR = []


def trace_of(a: dict, msg: str, raw: str) -> str:
    """The trace id a line names: traceparent > trace > request attribute, else a traceparent or trace/request id in the text."""
    if not _TR:
        from .traces import ID_KEYS, MSG_IDS, TRACEPARENT
        _TR.extend((ID_KEYS, MSG_IDS, TRACEPARENT, {}))
    ID_KEYS, MSG_IDS, TRACEPARENT, roles = _TR
    ids: dict = {}
    for k, v in a.items():
        role = roles.get(k)
        if role is None:
            role = ID_KEYS.get(str(k).lower(), "")
            if len(roles) < 20_000:
                roles[k] = role
        if role in ("traceparent", "trace", "request") and v not in (None, "", "-") and role not in ids:
            ids[role] = str(v)
    tp = ids.get("traceparent")
    if tp:
        parts = tp.split("-")
        if len(parts) >= 3:
            return parts[1][:CFG["trace_max"]]
    if ids.get("trace") or ids.get("request"):
        return (ids.get("trace") or ids.get("request"))[:CFG["trace_max"]]
    text = raw or msg or ""
    if "00-" in text:
        m = TRACEPARENT.search(text)
        if m:
            return m.group(1)
    if msg and ("d=" in msg or "d:" in msg or "D=" in msg or "D:" in msg):   # MSG_IDS needs "...id" followed by = or :
        m = MSG_IDS.search(msg)
        if m:
            return m.group(2)[:CFG["trace_max"]]
    return ""


def pack(o, core: dict) -> dict:
    """v2 stored line: the v1 core (LiveStore._pack) plus seq, line number, raw, attributes, trace, parser."""
    d = core
    msg = d.get("msg") or ""
    if len(msg) > CFG["msg_max"]:
        d["msg"] = msg[:CFG["msg_max"]]
        d["mt"] = 1
    seq = getattr(o, "seq", 0)
    if seq:
        d["q"] = seq
    if o.line_no and o.line_no > 0:
        d["ln"] = int(o.line_no)
    a = o.attributes or {}
    at, dropped, repl = transform_attrs(a) if a else ({}, 0, [])
    if at:
        d["at"] = at
    if dropped:
        d["atx"] = dropped
    raw, state = transform_raw(o.raw or "", msg, repl)
    if raw is not None:
        d["raw"] = raw
    elif state == "absent":
        d["rx"] = 1
    tr = trace_of(a, msg, o.raw or "") if a or msg else ""
    if tr:
        d["tr"] = tr
    if o.parser:
        d["prs"] = o.parser[:40]
    return d


_ENC = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), default=str).encode   # json.dumps with these options, built once


def encode_rows(events: list, agent: str, env: str, received_iso: str, core_fn, stale_before: Optional[float] = None) -> list:
    """Rows for live_batches (v2): events grouped by the minute of their stamp, JSONL compressed. Lines stamped before
    `stale_before` (older than the retention when they arrive: a wrong clock, a replayed file) go to one row bounded by the
    arrival second instead: a row bounded by its stamps was pruned at once, so a line the live tail had just shown had no detail
    a few seconds later. No range of the explorer ever lists them (their stamps are outside every kept range)."""
    groups: dict = {}
    for o in events:
        t = o.timestamp
        if t.tzinfo is None:
            t = t.replace(tzinfo=UTC)
        e = t.timestamp()
        b = e // ROW_SPAN_S if stale_before is None or e >= stale_before else -1.0
        g = groups.get(b)
        if g is None:
            g = groups[b] = []
        g.append((e, t, o))
    rows = []
    enc = _ENC
    sev_bit = SEV_BIT
    for b in sorted(groups):
        items = groups[b]
        lines = []
        mask = 0
        lo = hi = items[0]
        h0, s0 = items[0][2].host or "", items[0][2].source or ""
        one_h = one_s = True
        smin = smax = 0
        for it in items:
            o = it[2]
            lines.append(enc(pack(o, core_fn(o, agent))))
            mask |= sev_bit.get(o.severity, 2)
            if one_h and (o.host or "") != h0:
                one_h = False
            if one_s and (o.source or "") != s0:
                one_s = False
            if it[0] < lo[0]:
                lo = it
            elif it[0] > hi[0]:
                hi = it
            q = o.seq
            if q:
                if not smin or q < smin:
                    smin = q
                if q > smax:
                    smax = q
        pz = zlib.compress("\n".join(lines).encode("utf-8"), 1)
        t0, t1 = (iso_s(lo[1]), iso_s(hi[1])) if b >= 0 else (received_iso, received_iso)
        rows.append((t0, t1, received_iso, agent or "", env or "", len(items), "", 2, pz, smin, smax,
                     mask, h0 if one_h else "", s0 if one_s else ""))
    return rows


INSERT_V2 = ("INSERT INTO live_batches (ts_min, ts_max, received_at, agent, env, n, payload, v, pz, seq_min, seq_max, sevmask, host, src) "
             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
ROW_COLS = "id, ts_min, ts_max, received_at, agent, env, n, payload, v, pz, seq_min, seq_max, sevmask, host, src"


def migrate(db) -> bool:
    """Add the v2 columns and indexes to live_batches (idempotent). False when the table cannot be changed (v1 rows only)."""
    try:
        cols = set(db.columns("live_batches"))
        for c, t in (("v", "INTEGER DEFAULT 1"), ("pz", db.blob), ("seq_min", "INTEGER DEFAULT 0"), ("seq_max", "INTEGER DEFAULT 0"),
                     ("sevmask", "INTEGER DEFAULT 31"), ("host", "TEXT DEFAULT ''"), ("src", "TEXT DEFAULT ''")):
            if c not in cols:
                db._exec(f"ALTER TABLE live_batches ADD COLUMN {c} {t}")
        db._exec("CREATE INDEX IF NOT EXISTS live_batches_seq ON live_batches(seq_min)")
        db._exec("CREATE INDEX IF NOT EXISTS live_batches_host ON live_batches(host, ts_max)")
        db._exec("CREATE INDEX IF NOT EXISTS live_batches_agent ON live_batches(agent, ts_max)")
        # the scan's keyset (ts_max, ts_min, id): ts_min is checked inside the index, a row is fetched only when it can hold lines
        db._exec(f"CREATE INDEX IF NOT EXISTS live_batches_tsmm ON live_batches(ts_max, ts_min{', id' if getattr(db, 'pg', False) else ''})")
        db._exec("DROP INDEX IF EXISTS live_batches_ts")       # (ts_max) is a prefix of the index above: one B-tree less per inserted row
        return True
    except Exception:  # noqa: BLE001 - a read-only or foreign table keeps working as v1
        return False


def last_seq(db) -> int:
    try:
        rows = db._exec("SELECT seq_max FROM live_batches ORDER BY seq_min DESC LIMIT 64")
        return max((int(r["seq_max"] or 0) for r in rows), default=0)
    except Exception:  # noqa: BLE001
        return 0


def has_v1(db) -> bool:
    try:
        return bool(db._exec("SELECT 1 AS x FROM live_batches WHERE v = 1 OR v IS NULL LIMIT 1"))
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------- reading rows
def row_bytes(r: dict) -> bytes:
    pz = r.get("pz")
    if pz:
        return zlib.decompress(bytes(pz))
    p = r.get("payload") or ""
    return p.encode("utf-8") if isinstance(p, str) else bytes(p)


def row_dicts(r: dict) -> Iterator[dict]:
    """Every stored line of a row (v1 or v2) as its packed dict."""
    loads = json.loads
    for ln in row_bytes(r).split(b"\n"):
        if ln:
            try:
                yield loads(ln)
            except ValueError:
                continue


def line_id(d: dict, row_id: int, idx: int) -> int:
    q = d.get("q")
    return int(q) if q else -(int(row_id) * V1_ID_BASE + idx)


# ---------------------------------------------------------------- line views (one predicate for the ring and the database)
def _epoch(iso: str) -> float:
    t = datetime.fromisoformat(iso)
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).timestamp()


class LineView:
    """What a compiled query reads: core fields as attributes, the rest computed on demand."""
    __slots__ = ("sev", "svc", "host", "env", "src", "agent", "kind", "org", "msg", "ln", "seq", "ts", "ctx", "_c")

    def pool(self) -> str:
        return self.ctx.pool_of(self.host or "-") if self.ctx is not None else "-"

    def group(self) -> str:
        c = self._c
        g = c.get("g")
        if g is None:
            from .analysis import template_of
            g = c["g"] = group_key(template_of(self.msg or ""), self.svc or "-")
        return g

    def template(self) -> str:
        from .analysis import template_of
        return template_of(self.msg or "")

    def duration(self):
        from .traces import DUR_KEYS, _dur_value, _f
        at = self.attrs()
        if at:
            low = {str(k).lower(): v for k, v in at.items()}
            for k, mult in DUR_KEYS:
                v = low.get(k)
                if v not in (None, "", "-"):
                    x = (_f(v) * mult) if mult is not None and _f(v) is not None else _dur_value(v)
                    if x is not None:
                        return x
        m = LATENCY_RE.search(self.msg) if self.msg and "ms" in self.msg else None
        return float(m[1]) if m else None

    def attr(self, key: str):
        at = self.attrs()
        if not at:
            return None
        v = at.get(key)
        if v is None and key not in at:
            lk = key.lower()
            for k, x in at.items():
                if k.lower() == lk:
                    return x
        return v

    def session(self) -> str:
        s = self.ses()
        return s[0] if s else ""

    def user(self) -> str:
        s = self.ses()
        return s[2] if s and len(s) > 2 else ""

    def client(self) -> str:
        s = self.ses()
        return s[3] if s and len(s) > 3 else ""

    def shown(self) -> str:
        """The message as the explorer shows it (privacy.scrub): free-text search matches this, never the clear message."""
        c = self._c
        s = c.get("m")
        if s is None:
            s = c["m"] = scrub(self.msg) if self.msg else ""
        return s

    def env_name(self) -> str:
        """The environment the explorer filters by and shows: the host's environment as the live counters know it (the facet
        cube counts by host, so counts and listed lines agree), else the line's own, else "unknown"."""
        if self.ctx is not None:
            return self.ctx.env_of(self.host or "-", self.env)
        return self.env or "unknown"


class DictLine(LineView):
    """A stored line (packed dict); `received` is the row's arrival epoch."""
    __slots__ = ("d", "received")

    def __init__(self, d: dict, rid: int = 0, idx: int = 0, received: float = 0.0, ts: float = 0.0, ctx=None):
        self.d = d
        self.sev = d.get("sev") or "INFO"
        self.svc = d.get("svc") or ""
        self.host = d.get("host") or ""
        self.env = d.get("env") or ""
        self.src = d.get("src") or ""
        self.agent = d.get("agent") or "-"
        self.kind = d.get("kind") or "log"
        self.org = d.get("org") or ""
        self.msg = d.get("msg") or ""
        self.ln = d.get("ln") or 0
        self.seq = line_id(d, rid, idx)
        self.ts = ts
        self.received = received
        self.ctx = ctx
        self._c = {}

    def attrs(self) -> dict:
        return self.d.get("at") or {}

    def raw_text(self) -> str:
        return self.d.get("raw") or self.shown()                 # no stored raw: the raw line is the message, as shown

    def raw_state(self) -> str:
        if "raw" in self.d:
            return "stored"
        if self.d.get("rx") or ("q" not in self.d):
            return "absent"
        return "same"

    def trace(self) -> str:
        return self.d.get("tr") or ""

    def ses(self):
        s = self.d.get("ses")
        return tuple(s) if s else None

    def packed(self) -> dict:
        return self.d


class ObsLine(LineView):
    """A ring line (Observation); the privacy transform runs only when attributes, raw or trace are read."""
    __slots__ = ("o", "_p", "received")

    def __init__(self, o, ctx=None, received: float = 0.0):
        self.o = o
        self.sev = o.severity or "INFO"
        self.svc = o.service or ""
        self.host = o.host or ""
        self.env = o.environment or ""
        self.src = o.source or ""
        a = o.attributes
        self.agent = a.get("agent", "-") if a else "-"
        self.kind = o.kind or "log"
        self.org = o.origin or ""
        self.msg = o.message or ""
        self.ln = o.line_no or 0
        self.seq = getattr(o, "seq", 0)
        self.ts = utc(o.timestamp).timestamp()
        self.received = received
        self.ctx = ctx
        self._c = {}
        self._p = None

    def packed(self) -> dict:
        if self._p is None:
            from .live import LiveStore
            self._p = pack(self.o, LiveStore._pack(self.o, self.agent))
        return self._p

    def attrs(self) -> dict:
        if not self.o.attributes:
            return {}
        return self.packed().get("at") or {}

    def raw_text(self) -> str:
        return self.packed().get("raw") or self.shown()

    def raw_state(self) -> str:
        p = self.packed()
        return "stored" if "raw" in p else "absent" if p.get("rx") else "same"

    def trace(self) -> str:
        if self._p is not None:
            return self._p.get("tr") or ""
        t = self._c.get("tr")                                 # list views and the tail: no full privacy transform for the trace id
        if t is None:
            t = self._c["tr"] = trace_of(self.o.attributes or {}, self.msg, self.o.raw or "")
        return t

    def ses(self):
        a = self.o.attributes
        return session_of(a) if a else None
