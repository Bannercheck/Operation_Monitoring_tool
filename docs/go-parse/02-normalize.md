# 02 — Field normalisation (normalize.py, profiles.py, scenario constants)

Spec for porting `src/watchover/normalize.py`, `src/watchover/profiles.py` and the parse-relevant constants of
`src/watchover/scenario/__init__.py` to Go with byte-for-byte parity against the goldens in
`receiver/parse/testdata/golden/`.

Reference runtime (the one that produced the goldens): **CPython 3.11.17**, **python-dateutil 2.9.0.post0**, C locale
(`locale.getlocale(LC_TIME) == (None, None)`), process time zone UTC. Every behaviour below was confirmed by running the
Python code (probe scripts, not part of the repo). "Golden files" means the inputs in `receiver/parse/testdata/inputs/*`
and `samples/corpus/*`; a golden is `<input>.whole.jsonl.gz` / `<input>.live.jsonl.gz`.

Verification done for this spec:

* Re-running `scripts/parse_golden.py` into a scratch directory reproduces all 240 goldens byte for byte (deterministic).
* Running **every input in its own fresh process** (fresh caches, fresh empty profile store) reproduces every golden
  observation line byte for byte. The **only** differences are the `"profile"` field of the report line in 14
  `mut__*.whole` goldens (see [profiles](#profilespy--learned-column-mappings)). So none of the process-global caches change
  any golden observation; they are still specified exactly because they change results on other inputs.

---

## 0. Python primitives the Go port must emulate

These are used all over this slice. Getting them wrong silently breaks parity on non-ASCII data.

| Python | Exact semantics | Go |
|---|---|---|
| `str.strip()` (no args) | Removes leading/trailing chars with `str.isspace()`: `\t \n \v \f \r` space, `\x1c \x1d \x1e \x1f`, `\x85`, `\xa0`, ` `, ` `-` `, ` `, ` `, ` `, ` `, `　`. | `strings.TrimFunc(s, pyIsSpace)` with `pyIsSpace(r) = unicode.IsSpace(r) \|\| (r >= 0x1c && r <= 0x1f)`. `strings.TrimSpace` is **wrong** (misses `\x1c`-`\x1f`). |
| `str.lower()` | Full Unicode lowercase mapping. Differences from Go `strings.ToLower`: `U+0130 'İ'` → `"i̇"` (two runes); Greek capital sigma `Σ` → final `ς` when preceded by a cased letter and not followed by one (Final_Sigma). Everything else = simple mapping. `'ı'` (U+0131) stays `'ı'`. | `pyLower`: per rune `unicode.ToLower`, except `U+0130` → `"i̇"` and the Final_Sigma rule. Do **not** use `unicode.TurkishCase`. |
| `str.upper()` | Full mapping (`ß`→`SS`, `ŉ`→`ʼN`, ligatures…). Only used in `normalize_severity` where the result is compared against `DEBUG/INFO/WARN/ERROR/CRITICAL`; the only case that matters is `'ı'.upper() == 'I'` and `'i̇'.upper() == 'İ'`, which Go `strings.ToUpper` reproduces. | `strings.ToUpper` is sufficient for this use. |
| `str.isdigit()` | True if non-empty and every char has Numeric_Type Digit or Decimal (ASCII `0-9`, all `\p{Nd}`, plus superscripts `²³¹`, circled digits `①`…). | ASCII-only `[0-9]+` is a documented divergence; exact = `\p{Nd}` or `No` digits. Note `int()` then **raises** for non-`Nd` digits (e.g. `"²"`), see edge cases. |
| `int(s)` on a digit string | Accepts any `\p{Nd}` digits (`"١٧٩٠٠٦٨٤٤٢"` → 1790068442). | Convert with `unicode.Digit` value per rune if you support non-ASCII digits. |
| `float(str(v))` (in `auto_map`'s `numeric`) | Accepts surrounding whitespace (strip as above), sign, digits with single `_` between digits (`"1_000"`), decimal point, exponent, `inf`/`infinity`/`nan` in any case with optional sign, Unicode `Nd` digits. Rejects `"0x10"`, `""`, `"1."`? (**accepts** `"1."` and `".5"`). | Custom: trim, reject leading/trailing/double `_` and `_` next to `.`/`e`, strip `_`, then `strconv.ParseFloat`; special-case `inf`, `infinity`, `nan` (case-insensitive, signed). `strconv.ParseFloat` also accepts `0x1p-2` and `"Inf"` forms; reject hex explicitly. |
| `str(v)` for record values | `str` → itself; `int` → decimal; `float` → `repr` (see [float formatting](#14-canonical-json-and-float-formatting)); `True`/`False` → `"True"`/`"False"`; `None` → `"None"`; `list` → Python `repr` (`"['a', 1, None, True]"`); dicts never appear at top level after `flatten`, but can appear inside lists. | Implement `pyStr(any) string`. Lists: Python repr of each element joined by `", "` inside `[ ]`, strings repr'd with single quotes (double quotes if the string contains `'` and no `"`), escapes `\\ \n \r \t` and `\xNN`/`\uNNNN` for non-printables. |
| `len(s)` | Number of code points. | `utf8.RuneCountInString`. |
| `s[:n]` | First `n` code points. | Slice by runes, not bytes. |
| `dict` iteration | Insertion order; re-assigning an existing key keeps its original position. | Use an ordered map (slice of keys + map). |
| `max(iterable, key=f)` | Returns the **first** element with the maximal key (later equal keys do not replace). `default=` when empty. | Strict `>` comparison when scanning. |

Record values reaching this slice (`rec.get(...)`) can be: `string`, `int` (Python int, arbitrary size), `float`,
`bool`, `None`, `list` (from JSON; lists of dicts are not flattened). In goldens the attribute value types are
`str` (103 files), `int` (25), `bool` (22, e.g. `dotnet_serilog.log`, `java_stacktrace.log`, `storm__alarms.json`),
`float` (4: `baseline.json`, `nginx_combined.log`, `mut__nginx_combined.log`, `sim__simulate_traces.jsonl`).
**No list values occur in any golden** (uncovered).

Python `bool` is a subclass of `int`: everywhere `isinstance(v, (int, float))` is tested, `True`/`False` take the numeric branch.

---

## 1. Global and stateful behaviour (summary)

| State | Where | Key / value | Bound | Effect on output | What Go must do |
|---|---|---|---|---|---|
| `_ts_cache` | `parse_timestamp` | stripped input string → `datetime\|None` | when `len >= 200_000` the whole dict is **cleared** before inserting | Freezes the first result for a string (results depend on `_format_cache` state and on the current year at first parse) | Process-global map with the same clear rule, guarded by a mutex. |
| `_format_cache` | `_parse_timestamp_str` | `_shape(s)` → last strptime format that worked | unbounded, never cleared | **Order-dependent results**: e.g. fresh `"01/02/2026 10:00:00"` → `2026-02-01T10:00:00+00:00`, but after `"12/25/2026 10:00:00"` was parsed the same string gives `2026-01-02T10:00:00+00:00`. Same for `%I...%p` vs `%H...%p` (`"09/22/2026 09:15:03 PM"` → 21:15 fresh, 09:15 after a `"... 21:15:03 PM"`). Not exercised by any golden. | Process-global, identical update rule (see §3.6). |
| `_sev_cache` | `column_severity` | `(value, use_scale)` → severity; value used as-is if `str/int/float` (incl. `bool`) else `str(value)` | stops inserting at 50 000 entries (never cleared) | Python hash equality makes `1`, `1.0`, `True` the **same key**: fresh `column_severity(True)` = `INFO`, but after `column_severity(1)` (= `DEBUG` via scenario scale) it returns `DEBUG`; likewise `1.0`. No golden has bool/float severities except `baseline.json` (`100.0` → `INFO`). | Key numbers (int/float/bool) by numeric value, strings by string, others by `pyStr`; same cap. |
| `_env_cache` | `infer_environment` | tuple of the first 3 args → env | stops inserting at 50 000 | none (pure function memo) | Optional. |
| `_DEFAULT` | module import | `datetime(now().year, 1, 1)` (local time) | — | default date for dateutil (year-less / date-less strings) | Inject a "current year" (goldens: **2026**). |
| `datetime.now().year` | `%b %d %H:%M:%S` branch | year at parse time (local) | — | Year of BSD syslog stamps | Same injected year. |
| Learned profiles | `pipeline._parse_one` via `profiles.lookup/remember` | `<home>/profiles.json` | — | Can force column roles; changes report `"profile"` | §12. |
| `scenario.*` | `column_severity`, `pipeline` | module constants (mutable at runtime by `knowledge.apply_rules`, but not the parse-relevant ones) | — | numeric severity scale, mapping, side tables, dedup | §13. |
| Process time zone | dateutil `_build_tzaware` (`res.tzname in time.tzname` → `tzlocal()`) | — | — | A tz abbreviation equal to the host's `time.tzname` becomes local time | Treat as UTC (goldens were generated with a UTC-like zone; `time.tzname == ('Universal','Universal')`/`('UTC','UTC')`). |

---

## 2. Constants (verbatim)

### 2.1 `SEV_RANK` (models.py)

```python
SEV_RANK = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3, "CRITICAL": 4}
```

### 2.2 `SEVERITY_MAP` (normalize.py) — 53 distinct keys, keys are already lower-case

```python
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
```

(`"err"` appears twice with the same value.)

### 2.3 `ROLE_HINTS` (normalize.py) — order of roles AND of hints matters

Role iteration order: `timestamp, severity, service, environment, origin, host, message`.
No hint string appears in two roles; every hint is already in `_norm_key` form (lower-case ASCII).

```python
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
```

### 2.4 `ENV_MAP` (normalize.py)

```python
ENV_MAP = {"prod": "prod", "production": "prod", "prd": "prod", "live": "prod", "canli": "prod", "uretim": "prod",
           "test": "test", "tst": "test", "testing": "test", "qa": "qa", "uat": "uat", "acceptance": "uat", "kabul": "uat",
           "dev": "dev", "development": "dev", "develop": "dev", "gelistirme": "dev", "local": "dev", "sandbox": "dev",
           "staging": "staging", "stage": "staging", "stg": "staging", "preprod": "staging", "pre-prod": "staging", "canary": "staging",
           "dr": "dr", "disaster": "dr"}
```

### 2.5 `_FAST_FORMATS` (normalize.py) — tried in this order

```
 1 %Y-%m-%d %H:%M:%S          10 %Y/%m/%d %H:%M:%S           19 %a, %d %b %Y %H:%M:%S %z
 2 %Y-%m-%d %H:%M:%S.%f       11 %d/%b/%Y:%H:%M:%S %z        20 %Y-%m-%d %H:%M:%S.%f %z
 3 %Y-%m-%d %H:%M:%S,%f       12 %b %d %H:%M:%S              21 %Y-%m-%d %H:%M:%S %z
 4 %Y-%m-%dT%H:%M:%S          13 %Y%m%d%H%M%S                22 %Y-%m-%dT%H:%M:%S.%f%z
 5 %Y-%m-%dT%H:%M:%S.%f       14 %b %d %Y %H:%M:%S           23 %Y-%m-%dT%H:%M:%S%z
 6 %Y-%m-%d %H:%M             15 %d %b %Y %H:%M:%S.%f        24 %d-%b-%Y %H:%M:%S
 7 %d/%m/%Y %H:%M:%S          16 %d %b %Y %H:%M:%S           25 %d-%m-%Y %H:%M:%S
 8 %d.%m.%Y %H:%M:%S          17 %m/%d/%Y %I:%M:%S %p        26 %Y.%m.%d %H:%M:%S
 9 %m/%d/%Y %H:%M:%S          18 %m/%d/%Y %H:%M:%S %p
```

### 2.6 Dead code

`SYSLOG_TS_RE = re.compile(r"^[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2}$")` is defined but **never used** anywhere in
`src/`. Do not port. `_TS_CACHE_MAX = 200_000`, `_MISS` sentinel are implementation details of §3.

---

## 3. `parse_timestamp(value) -> datetime | None`

Called by `parsers/common.records_to_observations` for every record (`rec.get(r_ts)` when a timestamp role exists)
and by `auto_map` while guessing the timestamp column (§10.4). Output is always tz-aware; naive results get UTC.
The golden `ts` field is `o.timestamp.isoformat()` (§3.9). A `None` result is handled by the caller (last good
timestamp or the `MISSING_TS` placeholder + `_no_ts` attribute; that is in the parsers/common slice).

### 3.1 Top-level algorithm (exact order)

```
parse_timestamp(value):
  1. value is None                      -> None
  2. isinstance(value, (int, float))    -> numeric path (§3.2)   [bool included: True -> 1, False -> 0]
     (no caching on this path)
  3. s = str(value).strip()             (pyStr + pyStrip; lists/dicts become their repr and almost always fail)
  4. s == ""                            -> None
  5. if s in _ts_cache: return cached value (may be None)
  6. if len(_ts_cache) >= 200000: _ts_cache.clear()
  7. dt = _parse_timestamp_str(s); _ts_cache[s] = dt; return dt
```

Exceptions are **not** caught on paths 2 and 7's digit/epoch branches (see §3.10).

### 3.2 Numeric values (`int`, `float`, `bool`)

```
v = float(value)                                   # Python int -> float is correctly rounded (half-even)
secs = v / 1000 if v > 1e12 else v                 # strictly greater: 1e12 itself stays "seconds"
return datetime.fromtimestamp(secs, tz=UTC)
```

`fromtimestamp(d)` with a float (CPython `_PyTime_ObjectToTimeval`, ROUND_HALF_EVEN), reproduced exactly by:

```go
ip, frac := math.Modf(d)
us := math.RoundToEven(frac * 1e6)
if us >= 1e6 { us -= 1e6; ip += 1 } else if us < 0 { us += 1e6; ip -= 1 }
t := time.Unix(int64(ip), int64(us)*1000).UTC()
```

(verified against `datetime.fromtimestamp` on 300 000 random seconds / millis / micros values: 0 mismatches).
Valid range: year 1..9999, otherwise Python raises `ValueError`/`OverflowError`/`OSError` (not caught).

| input (type) | result |
|---|---|
| `0` | `1970-01-01T00:00:00+00:00` |
| `1` / `True` | `1970-01-01T00:00:01+00:00` |
| `False` | `1970-01-01T00:00:00+00:00` |
| `-5` | `1969-12-31T23:59:55+00:00` |
| `100.0` | `1970-01-01T00:01:40+00:00` (`baseline.json` auto_map probe) |
| `1790068442` | `2026-09-22T09:14:02+00:00` (`gelf.jsonl`: `1790068560`) |
| `1790068442.5` | `2026-09-22T09:14:02.500000+00:00` (`gelf.jsonl`: `1790068621.5`, `1790068503.1` → `.100000`) |
| `1790068442000` | `2026-09-22T09:14:02+00:00` |
| `1790068442003.7` | `2026-09-22T09:14:02.003700+00:00` |
| `1000000000001` | `2001-09-09T01:46:40.001000+00:00` |
| `1000000000000` | **raises** `ValueError: year 33658 is out of range` |
| `1790068503500000000` (int ns) | **raises** `ValueError: year 56726982 is out of range` (only strings get the ns branch) |
| `float('nan')` | **raises** `ValueError` |

Goldens: numeric path exercised by `gelf.jsonl`, `mut__gelf.jsonl` (int + float `timestamp`), `baseline.json` (float, only as an auto_map probe).

### 3.3 `_parse_timestamp_str(s)` — exact order of checks

`s` is already stripped and non-empty.

```
A. if s.isdigit():
     len 13 -> fromtimestamp(int(s) / 1000)              (Python int/int true division, correctly rounded)
     len 10 -> fromtimestamp(int(s))                      (int, exact)
     len 16 -> fromtimestamp(int(s) / 1_000_000)          (journald __REALTIME_TIMESTAMP)
     len 19 -> fromtimestamp(int(s) / 1_000_000_000)      (OTel timeUnixNano)
     len 14 -> strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
     other lengths: fall through to B
B. if re.fullmatch(r"\d{8}T\d{6}(?:\.\d+)?Z?", s):
     return strptime(s[:15], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)     # fraction dropped, 'Z' ignored
C. if re.fullmatch(r"\d{10}\.\d+", s):
     return fromtimestamp(float(s), tz=UTC)
D. dt = None
   if re.search(r" [+\-]\d{2}$", s): s = s + "00"         # "… +03" -> "… +0300" (PostgreSQL)
E. if len(s) >= 19 and s[4] == "-" and s[7] == "-":       # code-point indices
     dt = _from_iso(s)                                    (§3.4)
F. if dt is None:
     shape = _shape(s)
     fmt = _format_cache.get(shape)
     if fmt: try dt = strptime(s, fmt) except ValueError: dt = None
     if dt is None:
        for fmt in _FAST_FORMATS:
           try: dt = strptime(s, fmt); _format_cache[shape] = fmt; break
           except ValueError: continue
     if dt is not None and fmt == "%b %d %H:%M:%S":
        dt = dt.replace(year=datetime.now().year)
G. if dt is None:
     s2 = s.replace(",", ".", 1) if re.search(r"\d,\d{3}$", s) else s      # FIRST comma in the string, not the last one
     dayfirst = bool(re.match(r"\d{2}[./]\d{2}[./]\d{4}", s))              # anchored at start only
     try: dt = dateutil.parser.parse(s2, default=_DEFAULT, dayfirst=dayfirst)
     except (ValueError, OverflowError, TypeError): return None
H. return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
```

Notes:

* Step A's `int(s)/10**k` is an exact rational rounded once to the nearest double. For 16- and 19-digit values Go
  must **not** do `float64(n)/1e9` (double rounding: `n` itself is not exactly representable). Use
  `new(big.Rat).SetFrac(big.NewInt(n), big.NewInt(1e9)).Float64()` (correctly rounded), then the §3.2 `fromtimestamp`
  algorithm. For 13 digits `float64(n)/1000` is already exact-then-rounded-once (n < 2^53), but using big.Rat everywhere is simplest.
  Example of why rounding matters: `"9999999999999"` → `2286-11-20T17:46:39.999001+00:00` (not `.999000`);
  `"1790068503123456789"` → `…:03.123457+00:00`.
* Step A len 14 strptime can fail (e.g. month 13: `"20261322000000"`) → `ValueError` **propagates** (not caught) → crash.
  The epoch lengths cannot overflow (max 19 digits / 1e9 ≈ year 2286). `int()` on non-`Nd` digit strings (`"²…"`) raises.
* Step B: `\d` is Unicode in Python; `s[:15]` keeps `YYYYMMDDTHHMMSS`. Lower-case `t`/`z` do **not** match the
  `fullmatch` (case-sensitive), but `"20260922t091700z"` still parses later via dateutil to the same value.
  Example: `"20260922T091700.123Z"` → `2026-09-22T09:17:00+00:00` (fraction dropped!).
* Step C: exactly 10 digits before the dot and ≥1 after. `"1790068442."` and `"179006844.5"` do not match and end as `None`.
  `"1790068442.0034567"` → `2026-09-22T09:14:02.003457+00:00` (float rounding half-even).
* Step D changes `s` for all later steps (E/F/G) but the cache key is the original string.
* Step E gate: strings shorter than 19 code points never reach `fromisoformat` (`"2026-09-22T09:14"` goes to F/G).
* Step F `fmt` variable: if the cached format succeeded, `fmt` is that format; if the loop ran, `fmt` is the last
  format tried. The year replacement therefore applies exactly when the successful format is `%b %d %H:%M:%S`.
  `dt.replace(year=…)` can raise for Feb 29 in a non-leap current year — unreachable, because strptime with
  default year 1900 already rejects `Feb 29` (→ falls through to dateutil, which in 2026 also fails → `None`).

### 3.4 `_from_iso(s)` — CPython 3.11 `datetime.fromisoformat`

```
if s ends with "Z" or "z": s = s[:-1] + "+00:00"
if "." in s:
    m = re.match(r"^(.*?\.\d{6})\d+(.*)$", s)      # truncate the FIRST '.' fraction that has > 6 digits
    if m: s = m[1] + m[2]
try: return datetime.fromisoformat(s)   except ValueError: return None
```

RE2: `^(.*?\.\d{6})\d+(.*)$` is RE2-compatible (non-greedy is supported); use `(?s)` only if newlines are possible
(they are not after strip of a single field, but Python `.` does not match `\n`, so do **not** add `(?s)`). `\d` → `[0-9]` (or `\p{Nd}`).
Note `.*?` + `\.\d{6}` finds the first `.` followed by ≥ 7 digits (leftmost-first, same in Go).

`fromisoformat` grammar actually accepted in 3.11 for strings that passed the gate (`len >= 19`, `s[4]=='-'`, `s[7]=='-'`):

* Date `YYYY-MM-DD` (4-digit year, 2-digit month/day, validated: `2026-02-30…` → error).
* Separator: **any single character** at index 10 (`"2026-09-22x09:14:02"` and `"2026-09-22é09:14:02"` both parse).
  Two spaces (`"2026-09-22  09:14:02"`) → error (then strptime/dateutil handle it).
* Time: `HH`, `HH:MM`, `HH:MM:SS`, `HHMM`, `HHMMSS`, optional fraction introduced by `.` or `,` with 1..N digits
  (digits beyond 6 are truncated by CPython itself, e.g. `"2026-09-22 09:14:02,1234567"` → `.123456`; an **empty**
  fraction `"…02.+03:00"` is accepted). Hour 24 → error. Second 60 → error.
* Offset: `Z` handled above; `+HH`, `+HHMM`, `+HH:MM`, `+HH:MM:SS`, `+HH:MM:SS.ffffff`, `-…` forms. A single space
  between seconds and offset is tolerated **only when there is no fraction** (`"2026-09-22T09:14:02 +03:00"` → ok,
  `"2026-09-22 09:14:02.003 +03:00"` → error → falls to strptime format 20). `+3` → error.
* Any trailing garbage → error.
* Result naive when no offset.

Probed values (fresh caches; this is the final `parse_timestamp` output):

| input | result | branch |
|---|---|---|
| `2026-09-22T09:14:02` | `2026-09-22T09:14:02+00:00` | E |
| `2026-09-22 09:14:02` | `2026-09-22T09:14:02+00:00` | E |
| `2026-09-22T09:14:02Z` / `…z` | `2026-09-22T09:14:02+00:00` | E |
| `2026-09-22T09:14:02.1Z` | `2026-09-22T09:14:02.100000+00:00` | E |
| `2026-09-22T09:14:02.12Z` | `2026-09-22T09:14:02.120000+00:00` | E |
| `2026-09-22T09:14:02.1234567Z` | `2026-09-22T09:14:02.123456+00:00` (truncate) | E |
| `2026-09-22T09:14:02.0000000Z` | `2026-09-22T09:14:02+00:00` | E (`windows_event_xml.log`) |
| `2026-09-22T09:14:02.123456789+03:00` | `2026-09-22T09:14:02.123456+03:00` | E |
| `2026-09-22T09:14:02.000+03:00` | `2026-09-22T09:14:02+03:00` | E |
| `2026-09-22T09:14:02.5+03:00` | `2026-09-22T09:14:02.500000+03:00` | E |
| `2026-09-22T09:14:02+0300` | `2026-09-22T09:14:02+03:00` | E |
| `2026-09-22T09:14:02+03` | `2026-09-22T09:14:02+03:00` | E |
| `2026-09-22T09:14:02-03` | `2026-09-22T09:14:02-03:00` | E |
| `2026-09-22T09:14:02-00:00` | `2026-09-22T09:14:02+00:00` | E |
| `2026-09-22T09:14:02+05:30:15` | `2026-09-22T09:14:02+05:30:15` | E |
| `2026-09-22T09:14:02+03:00:30.5` | `2026-09-22T09:14:02+03:00:30.500000` | E |
| `2026-09-22T09:14:02.123-0330` | `2026-09-22T09:14:02.123000-03:30` | E |
| `2026-09-22T09:14:02,123` | `2026-09-22T09:14:02.123000+00:00` | E |
| `2026-09-22 09:14:02,123` | `2026-09-22T09:14:02.123000+00:00` | E |
| `2026-09-22 09:14:02,1234` | `2026-09-22T09:14:02.123400+00:00` | E |
| `2026-09-22x09:14:02` | `2026-09-22T09:14:02+00:00` | E |
| `2026-09-22T09:14:02 +03:00` | `2026-09-22T09:14:02+03:00` | E |
| `2026-09-22 09:14:31.001 +03` | `2026-09-22T09:14:31.001000+03:00` | D then F fmt 20 (`postgres.log`) |
| `2026-09-22 09:14:31 +03` | `2026-09-22T09:14:31+03:00` | D then E |
| `2026-09-22T09:14:31 -05` | `2026-09-22T09:14:31-05:00` | D then E |
| `2026-09-22 09:14:31.001+03` | `2026-09-22T09:14:31.001000+03:00` | E |
| `2026-09-22 09:14:02.003 +03:00` | `2026-09-22T09:14:02.003000+03:00` | F fmt 20 (`dotnet_serilog.log`) |
| `2026-09-22 09:14:02 +0300` | `2026-09-22T09:14:02+03:00` | F fmt 21 |
| `2026-09-22T09:14:02+3` | `2026-09-22T09:14:02+03:00` | G (dateutil) |
| `2026-09-22T24:00:00` | `None` | |
| `2026-09-22T09:14:60` | `None` | |
| `2026-02-30T09:14:02` | `None` | |
| `2026-09-22T09:14:02.123.456` | `None` | |
| `2026-09-22T09:14:02Zjunk` / `… extra` | `None` | |
| `2026-09-22T09:14:02+03:00[Europe/Istanbul]` | `None` | |
| `2026-09-22  09:14:02` | `2026-09-22T09:14:02+00:00` | F fmt 1 (`\s+`) |
| `2026-09-22T09:14` (len 16) | `2026-09-22T09:14:00+00:00` | G |
| `2026-09-22 09:14` | `2026-09-22T09:14:00+00:00` | F fmt 6 |
| `2026-09-22T0914` / `2026-09-22T091402` / `2026-09-22T09` | `…T09:14:00` / `…T09:14:02` / `…T09:00:00` `+00:00` | G |
| `2026-09-22` | `2026-09-22T00:00:00+00:00` | G |
| `2026-9-22 09:14:02` | `2026-09-22T09:14:02+00:00` | F fmt 1 (`%m` accepts 1 digit) |
| `0001-01-01T00:00:00` | `0001-01-01T00:00:00+00:00` | E |
| `2026-09-22T09:14:02+24:00` | returns a datetime with a ≥24h dateutil `tzoffset`; **`isoformat()` then raises** `ValueError` downstream | G |

### 3.5 strptime emulation (CPython `_strptime`, C locale)

`datetime.strptime(s, fmt)` = build a regex from `fmt`, `re.match` it against `s` with `re.IGNORECASE`, require the
match to cover the **whole** string (`len(s) != found.end()` → "unconverted data remains" `ValueError`), convert the
groups, build a `datetime` (validation errors → `ValueError`).

Regex construction (`TimeRE.pattern`):

1. Escape `\ . ^ $ * + ? ( ) { } [ ] |` in the format with a backslash (in our formats only `.` occurs).
2. Replace every run of whitespace in the format with `\s+` (Python `\s`: Unicode whitespace, includes `\v`, `\x1c`-`\x1f`, `\x85`, `\xa0`, …).
3. Replace each `%X` with its directive regex:

| directive | Python regex (verbatim) | value |
|---|---|---|
| `%d` | `(?P<d>3[0-1]\|[1-2]\d\|0[1-9]\|[1-9]\| [1-9])` | int (note the `" [1-9]"` alternative) |
| `%f` | `(?P<f>[0-9]{1,6})` | right-pad with `0` to 6 digits → microseconds |
| `%H` | `(?P<H>2[0-3]\|[0-1]\d\|\d)` | int |
| `%I` | `(?P<I>1[0-2]\|0[1-9]\|[1-9])` | int, combined with `%p` |
| `%m` | `(?P<m>1[0-2]\|0[1-9]\|[1-9])` | int |
| `%M` | `(?P<M>[0-5]\d\|\d)` | int |
| `%S` | `(?P<S>6[0-1]\|[0-5]\d\|\d)` | int (60/61 then fail in `datetime()` → ValueError) |
| `%Y` | `(?P<Y>\d\d\d\d)` | int |
| `%z` | `(?P<z>[+-]\d\d:?[0-5]\d(:?[0-5]\d(\.\d{1,6})?)?\|(?-i:Z))` | `Z` → UTC; else sign, HH, MM, optional SS and fraction → fixed offset |
| `%b` | `(?P<b>jan\|feb\|mar\|apr\|may\|jun\|jul\|aug\|sep\|oct\|nov\|dec)` (case-insensitive via flag) | month 1..12 |
| `%a` | `(?P<a>mon\|tue\|wed\|thu\|fri\|sat\|sun)` | parsed and **ignored** (no consistency check with the date) |
| `%p` | `(?P<p>am\|pm)` | with `%I`: `am` → `12`→0; `pm` → +12 unless 12. With `%H`: **ignored** |

Missing fields default to year 1900, month 1, day 1, time 0. `%z` result: `timezone(timedelta(...))`, so `+0000`/`-0000`
both render `+00:00`. The `%z` regex: if the 4th char is `:` the colons are removed before conversion; seconds and
fraction optional.

RE2 compatibility: everything is RE2-compatible except two notes: `(?-i:Z)` is supported by RE2 as `(?-i:Z)`;
`\d`/`\s` are ASCII in RE2 — for exact parity use `\p{Nd}` and the explicit Python whitespace class
`[\t\n\v\f\r \x{1c}-\x{1f}\x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]`.
Go `regexp` uses leftmost-first semantics with the same alternation priority as Python's backtracking engine, so the
captured groups are identical (e.g. `"202609220917"` with format 13 → `M="1"`, `S="7"` → `2026-09-22T09:01:07+00:00`).
Case-insensitivity difference: Python `re.I` also equates `i`↔`ı`(U+0131)↔`İ`(U+0130) and `s`↔`ſ`; Go `(?i)` equates
`s`↔`ſ` and `k`↔`K`(U+212A) but **not** the dotted/dotless i. Only `fri` contains an `i` (`%a`, format 19). Divergence
uncovered and negligible; can be fixed by writing `fr[iIıİ]`.

Probed strptime-branch results (fresh caches):

| input | format | result |
|---|---|---|
| `22/09/2026 09:14:05` | 7 | `2026-09-22T09:14:05+00:00` (`timestamp_variants.log`) |
| `1/2/2026 10:00:00` | 7 | `2026-02-01T10:00:00+00:00` |
| `01/02/2026 10:00:00` | 7 (fresh) | `2026-02-01T10:00:00+00:00` — **9** if the shape cache holds 9 |
| `12/25/2026 10:00:00` | 9 | `2026-12-25T10:00:00+00:00` (and caches 9 for the shape) |
| `22.09.2026 09:14:02` | 8 | `2026-09-22T09:14:02+00:00` (`timestamp_variants.log`) |
| `2026/09/22 09:14:02` | 10 | `2026-09-22T09:14:02+00:00` (`golang_log.log`, `nginx_error.log`) |
| `22/Sep/2026:09:14:02 +0300` | 11 | `2026-09-22T09:14:02+03:00` (`apache_access_clf.log`, `mut__apache_access_clf.log`) |
| `22/sep/2026:09:14:02 +03:00` | 11 | `2026-09-22T09:14:02+03:00` |
| `Sep 22 09:14:02` | 12 + year | `2026-09-22T09:14:02+00:00` (`haproxy.log`, `postfix.log`, `syslog_rfc3164.log`, `iptables_kernel.log`, `demo__db.log`, `mut__haproxy.log`) |
| `Sep  2 09:14:02`, `SEP 22 09:14:02` | 12 + year | `2026-09-02T09:14:02+00:00`, `2026-09-22T09:14:02+00:00` |
| `Feb 29 10:00:00` | — | `None` (1900 not leap; dateutil in 2026 also fails) |
| `20260922 091700` | 13? no → G | `2026-09-22T09:17:00+00:00` (dateutil) |
| `Sep 22 2026 09:14:02` | 14 | `2026-09-22T09:14:02+00:00` (`cef_arcsight.log`, `cisco_asa.log`) |
| `22 Sep 2026 09:14:02.003` | 15 | `2026-09-22T09:14:02.003000+00:00` (`redis.log`) |
| `22 Sep 2026 09:14:02` | 16 | `2026-09-22T09:14:02+00:00` |
| `09/22/2026 09:15:03 AM` | 17 | `2026-09-22T09:15:03+00:00` (`timestamp_variants.log`) |
| `09/22/2026 09:15:03 PM` | 17 | `2026-09-22T21:15:03+00:00` |
| `09/22/2026 21:15:03 PM` | 18 | `2026-09-22T21:15:03+00:00` (`%p` ignored) |
| `Tue, 22 Sep 2026 09:16:44 +0300` | 19 | `2026-09-22T09:16:44+03:00` (`timestamp_variants.log`) |
| `22-SEP-2026 09:14:02` | 24 | `2026-09-22T09:14:02+00:00` (`oracle_ebs_concurrent.log`, `oracle_ebs_fnd_debug.log`) |
| `22-09-2026 09:14:02` | 25 | `2026-09-22T09:14:02+00:00` |
| `2026.09.22 09:14:02` | 26 | `2026-09-22T09:14:02+00:00` |
| `202609220917` | 13 | `2026-09-22T09:01:07+00:00` |

### 3.6 `_shape(s)` and the format cache

```python
def _shape(s): return re.sub(r"[A-Za-z]", "a", re.sub(r"\d", "9", s))
```

RE2: fine. Python `\d` is Unicode digits → use `\p{Nd}` for exactness; `[A-Za-z]` is ASCII (the regex has no
`re.I`). Update rule: the cache entry is written **only** when the ordered loop succeeds (a successful cached format
does not rewrite it; a failing cached format leaves it until the loop finds another one). Shapes where two formats can
both succeed (the source of order dependence): `%d/%m/%Y …` vs `%m/%d/%Y …` (formats 7/9) and `%I…%p` vs `%H…%p`
(formats 17/18). No golden triggers this (instrumented run: 0 strings where the cached format differed from the first
format that would succeed). Golden run end state: `_format_cache` has 12 shapes, `_ts_cache` 6 837 strings (far below
the 200 000 clear threshold).

### 3.7 dateutil fallback (step G)

Call: `dateutil.parser.parse(s2, default=datetime(Y,1,1), dayfirst=D)` with `fuzzy=False`, `yearfirst=False`,
`ignoretz=False`, no `tzinfos`. Pre-processing done by our code:

* `re.search(r"\d,\d{3}$", s)` → replace the **first** `,` in the string by `.` (`"Sep 22, 2026 09:14:02,123"` becomes
  `"Sep 22. 2026 09:14:02,123"` — still parses to `2026-09-22T09:14:02.123000+00:00`).
* `dayfirst = re.match(r"\d{2}[./]\d{2}[./]\d{4}", s)` (prefix match, no end anchor).

Exercised in the goldens (all successful dateutil results that reached an observation):

| input | result | files |
|---|---|---|
| `09:14:02.003` (time only, millis) | `2026-01-01T09:14:02.003000+00:00` | `bind9_query.log`, `mut__bind9_query.log` |
| `09:14:05` (time only) | `2026-01-01T09:14:05+00:00` | `fortigate_kv.log`, `mut__fortigate_kv.log` |
| `22-Sep-2026 09:14:02.003` | `2026-09-22T09:14:02.003000+00:00` | `tomcat_juli.log`, `mut__tomcat_juli.log` |

In addition, `auto_map`'s timestamp guess (§10.4) sends **2 495 distinct non-timestamp strings** through dateutil in the
golden run (mostly `storm__alarms.csv` and `mut__csv_events.csv` live batches, whose header-less chunks are mapped by
values): every one with ≥ 8 characters **failed** (e.g. `"ALM-00054"`, `"user-01"`, `"Clock drift 11ms on stage-02"`,
`"gateway timeout order=231849"`, `"Disk usage 85% on /data (order-01)"`). Four short ones succeeded (`"1"`, `"2"`, `"3"` → day of
January) but are irrelevant because the guess requires `len(str(v)) >= 8`. So in the goldens the Go fallback must
(a) parse the three shapes above and (b) return `None` for free text.

Full probe table (fresh caches) to use as unit-test vectors for the Go fallback:

| input | result |
|---|---|
| `09:14:02`, `09:14:02,123`, `09:14`, `12:00 PM` | `2026-01-01T09:14:02+00:00`, `2026-01-01T09:14:02.123000+00:00`, `2026-01-01T09:14:00+00:00`, `2026-01-01T12:00:00+00:00` |
| `09:14:02.1234567` | `2026-01-01T09:14:02.123456+00:00` (dateutil truncates fractions to 6 digits) |
| `22-Sep-2026 09:14:02,123` | `2026-09-22T09:14:02.123000+00:00` |
| `Sep 22 09:14:02,123` | `2026-09-22T09:14:02.123000+00:00` (no year → default year) |
| `Sep 22`, `September 22 09:14:02` | `2026-09-22T00:00:00+00:00`, `2026-09-22T09:14:02+00:00` |
| `Sep 22, 2026 9:14:02 AM` | `2026-09-22T09:14:02+00:00` |
| `Mon Sep 22 09:14:02 2026`, `Mon Sep 22 09:14:02 UTC 2026` | `2026-09-22T09:14:02+00:00` |
| `Tue, 22 Sep 2026 09:16:44 GMT` | `2026-09-22T09:16:44+00:00` |
| `2026-09-22T09:14:02 UTC` / `GMT` | `+00:00` (tzutc) |
| `2026-09-22T09:14:02 EST` / `CET` | `2026-09-22T09:14:02+00:00` (**unknown abbreviations are dropped**, then UTC) |
| `01.02.2026 10:00` (dayfirst) | `2026-02-01T10:00:00+00:00` |
| `10/09/2026`, `10.09.2026` (dayfirst) | `2026-09-10T00:00:00+00:00` |
| `09/22/2026` (dayfirst requested but 22 > 12 → swapped) | `2026-09-22T00:00:00+00:00` |
| `20260922`, `20260922t091700z` | `2026-09-22T00:00:00+00:00`, `2026-09-22T09:17:00+00:00` |
| `123456789` | `1234-05-06T07:08:09+00:00` (!) |
| `1`, `7`, `12`, `13`, `31` | `2026-01-01`, `2026-01-07`, `2026-01-12`, `2026-01-13`, `2026-01-31` `T00:00:00+00:00` |
| `32`, `60`, `99` | `2032-01-01`, `2060-01-01`, `1999-01-01` (2-digit year pivot: within 50 years of now) |
| `100`, `200`, `2026`, `1999` | `0100-01-01`, `0200-01-01`, `2026-01-01`, `1999-01-01` |
| `1.2.3` | `2003-01-02T00:00:00+00:00` (M.D.Y, 2-digit year) |
| `3.5` | `2026-01-03T00:00:00+00:00` |
| `0`, `12345`, `12345678`, `0.5`, `10.0.0.1`, `192.168.1.10`, `v1.2`, `yesterday`, `noon`, `N/A`, `-`, `INFO`, `ALM-00041` | `None` |
| `22 Eylül 2026 09:14`, `22 Eyl 2026`, `22 Ekim 2026` | `None` (**no Turkish month names are understood**) |

Recommendation for Go: port dateutil's `_timelex.split`, `parser._parse` (with `parserinfo` English JUMP/WEEKDAYS/
MONTHS/HMS/AMPM/UTCZONE/PERTAIN tables), `_ymd.resolve_ymd` and `_build_naive`/`_build_tzaware` for the fixed options
above (≈600 lines), and use the table above plus the 2 495 failing strings (dump them from a golden run) as tests.
A reduced grammar covering only `HH:MM[:SS[.f+]]`, `DD-Mon-YYYY HH:MM:SS[.f]` and "everything else → None" matches the
goldens but diverges on the extra shapes listed above. tz rules to keep: `UTC`, `GMT`, `Z`, `z` → UTC; numeric
`+HHMM`/`+HH:MM`/`+HH` → fixed offset; any other alphabetic tz name → ignored (naive → UTC).

### 3.8 Turkish / locale month names

Neither strptime (C locale) nor dateutil (English `parserinfo`) knows Turkish month names (`Eylül`, `Eyl`, `Ekim`, …):
such strings return `None`. There is no locale handling anywhere in this slice.

### 3.9 Output formatting (`datetime.isoformat()`)

`YYYY-MM-DDTHH:MM:SS` (year zero-padded to 4 digits: `0100-01-01T…`), then `.ffffff` (always 6 digits) **only if
microsecond ≠ 0**, then the offset `±HH:MM`, plus `:SS` if the offset has seconds and `.ffffff` if it has microseconds
(`+05:30:15`, `+03:00:30.500000`). UTC renders `+00:00` (never `Z`). Offsets are kept as parsed — no conversion to UTC
(`2026-09-22T09:14:02+03:00` stays `+03:00`).

Go: `t.Format("2006-01-02T15:04:05")` + manual fraction + manual offset (Go's `Z07:00` prints `Z` for UTC — do not use it).
Years < 1000 need manual zero padding check (Go pads to 4 already). Offset with seconds needs manual formatting.

### 3.10 Exceptions that escape `parse_timestamp`

Python raises (and the whole ingest of that file fails) for: numeric values out of `datetime` range (incl. int
`1000000000000` and ns-sized ints), `nan`/`inf` floats, 14-digit strings that are not a valid date
(`"20261322000000"`), digit strings with non-decimal digits (`"²…"`), and a later crash on `isoformat()` for
dateutil offsets ≥ 24h. None occurs in the goldens. Go: return an error up the stack (parity = file fails) or document
a deliberate divergence (treat as `None`).

### 3.11 Golden coverage of `parse_timestamp` branches

(Because of the string memo only the first file to parse a given string is listed for string branches.)

| branch | goldens |
|---|---|
| numeric int/float | `gelf.jsonl`, `mut__gelf.jsonl`, `baseline.json` (probe) |
| A len 10 | `nagios.log` (`1790068442`) |
| A len 13 | **not covered** |
| A len 16 | `journald.jsonl` (`1790068442000000`) |
| A len 19 | `otel_logs.json` (`1790068503500000000` → `…09:15:03.500000+00:00`) |
| A len 14 | **not covered** |
| B compact ISO | `timestamp_variants.log` (`20260922T091700Z`) |
| C epoch with fraction | `squid_access.log` (`1790068442.003` → `.003000`) |
| D `+HH` padding | `postgres.log` |
| E fromisoformat, naive | 29 files incl. `csv_events.csv`, `demo__*`, `dockerd.log`, `iis_w3c.log`, `java_stacktrace.log`, `kafka_log4j.log`, `mongodb.jsonl`, `sap__*`, `sim__*`, `storm__*`, `timestamp_variants.log` |
| E fromisoformat with `Z` | 33 files incl. `alertmanager.json`, `aws_alb_access.log`, `aws_cloudtrail.json`, `docker_jsonfile.jsonl`, `kubernetes_cri.log`, `mysql_error.log`, `syslog_rfc5424.log`, most `mut__*` |
| E fraction > 6 digits | `windows_event_xml.log` |
| F formats 8, 7, 10, 11, 12, 14, 15, 17, 19, 20, 24 | see §3.5 table |
| F formats 1–6, 9, 13, 16, 18, 21–23, 25, 26 | **not covered** |
| F year replacement | the format-12 files above |
| G dateutil | `bind9_query.log`, `fortigate_kv.log`, `tomcat_juli.log` (+ `mut__` twins); failing probes in `storm__alarms.csv`, `mut__csv_events.csv` |
| `None` from a real timestamp column | only via auto_map probes (`storm__alarms.csv`, `mut__csv_events.csv` live) |
| format-cache order dependence, `_ts_cache` clear | **not covered** |

---

## 4. Regex inventory (all regexes in this slice)

| # | Name / place | Python (verbatim) | RE2 as-is? | Go rewrite |
|---|---|---|---|---|
| R1 | `ENV_TOKEN_RE` | `(?<![a-z0-9])(prod\|production\|prd\|test\|tst\|qa\|uat\|dev\|development\|staging\|stage\|stg\|preprod\|canary\|sandbox\|live)(?![a-z0-9])`, `re.I` | **No** (lookbehind/lookahead) | §4.1 |
| R2 | `LEVEL_WORD_RE` | `(?<![A-Za-z])(TRACE\|DEBUG\|INFO\|NOTICE\|WARN(?:ING)?\|ERR(?:OR)?\|CRIT(?:ICAL)?\|FATAL\|ALERT\|EMERG(?:ENCY)?\|SEVERE\|PANIC\|INF\|WRN\|FTL\|DBG\|VRB)(?![A-Za-z])`, `re.I` | **No** | §4.2 |
| R3 | `ERROR_HINT_RE` | `(ORA-\d{5}\|APP-FND-\d{5}\|FRM-\d{5}\|TNS-\d{5}\|Traceback \(most recent call last\)\|\b[A-Za-z_.$]*(?:Exception\|Error)\b: \|deadlock detected\|Out of memory\|\bsegfault\b\|\bpanic:\|\bkernel panic\|\bStack overflow\b\|\bBACKUP failed\b\|\bLogin failed\b\|\bAssertion failure\b\|\bcircuit breaker open\b)`, `re.I` | syntax yes, **semantics no** (`\b`, `\d` Unicode in Python) | §4.3 |
| R4 | `WARN_HINT_RE` | `(\btimed out\b\|\btimeout\b\|\bretry\b\|\bretrying\b\|\bdeferred\b\|\bdenied\b\|\brefused\b\|\bslow query\b\|\bunreachable\b\|\bdegraded\b\|\bdeprecated\b)`, `re.I` | syntax yes, semantics no (`\b`) | §4.3 |
| R5 | `_shape` | `\d` then `[A-Za-z]` | yes | `\p{Nd}` for `\d` |
| R6 | `_from_iso` | `^(.*?\.\d{6})\d+(.*)$` | yes | `[0-9]`/`\p{Nd}` |
| R7 | step B | `\d{8}T\d{6}(?:\.\d+)?Z?` (fullmatch) | yes | anchor `^…$` |
| R8 | step C | `\d{10}\.\d+` (fullmatch) | yes | anchor `^…$` |
| R9 | step D | ` [+\-]\d{2}$` (search) | yes (`$` = end; Python `$` also matches before a final `\n`, impossible after strip) | `\z` or `$` |
| R10 | step G comma | `\d,\d{3}$` (search) | yes | same |
| R11 | step G dayfirst | `\d{2}[./]\d{2}[./]\d{4}` (re.match = prefix) | yes | `^…` |
| R12 | strptime | §3.5 | yes (with class notes) | §3.5 |
| R13 | `SYSLOG_TS_RE` | unused | — | do not port |

All patterns have **no `re.X`, no backreferences, no possessive/atomic groups, no conditionals, no `\Z`**.
The only incompatible constructs are the lookarounds in R1/R2 and the Unicode `\b` in R3/R4.

### Python `re.I` case-folding facts that matter here

Python's case-insensitive matching (sre, Unicode str patterns) treats as equal: `i`, `I`, `ı` (U+0131), `İ` (U+0130);
`s`, `S`, `ſ` (U+017F); `k`, `K`, `K` (U+212A Kelvin); `µ`/`μ`. Go `(?i)` folds `s/ſ` and `k/K` but **not** `ı`/`İ`.
So inside case-insensitive classes and literals, every `i` must become `[iIıİ]` (and `[A-Za-z]` must be extended with
`ıİſ\x{212A}`) to reproduce Python on Turkish text — the target data set is Turkish.

### 4.1 `ENV_TOKEN_RE` rewrite

Python semantics: find the leftmost position where a token matches (alternatives tried in the listed order — `prod`
before `production`, `staging` before `stage`, `preprod` after `prd`…), with the char before not in
`[a-z0-9]` (case-insensitively, i.e. ASCII letters/digits + `ı İ ſ K`) and the char after likewise not.
Note: because alternatives are ordered, `"production"` matches `prod`? No — `prod` is followed by `u` (a letter), so the
lookahead fails and the engine backtracks to the next alternative `production`. Result capture is the token text as written in the input.

Go (RE2 has no lookaround). Use a consuming pattern and check boundaries in code, scanning all start positions:

Exact approach: a hand-written matcher.

```go
var envTokens = []string{"prod","production","prd","test","tst","qa","uat","dev","development","staging","stage","stg","preprod","canary","sandbox","live"}
func isEnvBoundaryRune(r rune) bool { // Python re.I [a-z0-9]
    return r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == 'ı' || r == 'İ' || r == 'ſ' || r == 0x212A
}
// envTokenSearch returns the matched substring (as in the input) or "".
func envTokenSearch(s string) string {
    rs := []rune(s)
    for i := range rs {                       // leftmost start
        if i > 0 && isEnvBoundaryRune(rs[i-1]) { continue }
        for _, tok := range envTokens {       // alternation order
            n := len([]rune(tok))
            if i+n > len(rs) || !foldEqPy(rs[i:i+n], tok) { continue }
            if i+n < len(rs) && isEnvBoundaryRune(rs[i+n]) { continue }
            return string(rs[i : i+n])
        }
    }
    return ""
}
// foldEqPy: rune-wise equality under Python re.I: ASCII case-insensitive, plus 'i' matches ı/İ, 's' matches ſ, 'k' matches U+212A.
```

The caller then does `ENV_MAP[m.lower()]`. **Python crashes** here when the match contains `ı` or `ſ` (or `İ`): e.g.
`normalize_environment("productıon")` → `KeyError: 'productıon'`, `infer_environment("lıve-1")` → `KeyError: 'lıve'`,
`"ſtage"` → `KeyError`. (`İ` lowercases to `i̇`, also a `KeyError`.) Go: either propagate an error (parity) or — recommended,
documented divergence — map with ASCII-folded token. Not covered by goldens.

### 4.2 `LEVEL_WORD_RE` rewrite

Same structure: leftmost start position whose previous rune is not `[A-Za-z]` under Python `re.I` (ASCII letters +
`ı İ ſ K`); alternatives in order `TRACE, DEBUG, INFO, NOTICE, WARNING|WARN (WARN(?:ING)? is greedy: tries WARNING first), ERROR|ERR, CRITICAL|CRIT,
FATAL, ALERT, EMERGENCY|EMERG, SEVERE, PANIC, INF, WRN, FTL, DBG, VRB`; next rune must not be a letter. Digits, `_`, punctuation, non-Latin letters are boundaries
(`"5Error: x"` → `Error`, `"foo_ERROR bar"` → `ERROR`, `"INFO_x"` → `INFO`, `"çError: x"` → `Error`, `"ERRORS"` → no match,
`"xINFO"` → no match). Optional-suffix semantics: `WARN(?:ING)?` matches `WARNING` if possible, else `WARN` — but if
`WARNING` is followed by a letter, the engine backtracks to `WARN`, which is then followed by `I` → fails too. Equivalent
expanded token list (order matters): `TRACE, DEBUG, INFO, NOTICE, WARNING, WARN, ERROR, ERR, CRITICAL, CRIT, FATAL, ALERT,
EMERGENCY, EMERG, SEVERE, PANIC, INF, WRN, FTL, DBG, VRB`. Case-insensitive with the Python fold (`crıtıcal` and `ınf` match).

A Go RE2 alternative that is exact for existence and position: `(?i)(?:^|[^A-Za-zıİſ\x{212A}])(TRACE|…|VRB)(?:[^A-Za-zıİſ\x{212A}]|$)`
with every `I` in the alternation replaced by `[Iıİ]` — but RE2 `FindStringSubmatchIndex` returns the leftmost match whose
*consumed prefix* starts earliest, which equals Python's leftmost token start, and the alternation priority is the same.
Use the hand-written scanner above if in doubt.

Golden coverage: R2 used in `severity_from_text` and in `parsers/common.py` (`_sev_src`). Level-word path hit by
20 files (`baseline.json`, `demo__db.log`, `dynamics_ax_aos.log`, `dynamics_nav_bc.log`, `golang_log.log`,
`nagios.log`, `oracle_alert.log`, `oracle_ebs_concurrent.log`, `oracle_ebs_fnd_debug.log`, `syslog_rfc3164.log` and their `mut__` twins, `mut__dotnet_serilog.log`, `mut__postgres.log`).

### 4.3 `ERROR_HINT_RE` / `WARN_HINT_RE` rewrite

Only used as boolean `search`. Python `\b` is Unicode-aware: word chars are `[\p{L}\p{N}_]` (Python `str.isalnum()` or `_`;
combining marks `Mn/Mc` are **not** word chars). Go `\b` is ASCII-only → `"çtimeout"`: Python no match (`ç` is a word char),
Go with `\b` would match. Python `\d` (`ORA-\d{5}`) also matches Unicode digits.

Exact Go approach for `\bWORD\b` alternatives (first and last chars are ASCII word chars, so `\b` means "previous/next
rune is not a Python word char"):

```go
const pyW = `\p{L}\p{N}_`
// WARN_HINT_RE (boolean): any alternative with non-word context on both sides
var warnHint = regexp.MustCompile(`(?i)(?:^|[^` + pyW + `])(?:timed out|timeout|retry|retrying|deferred|denied|refused|slow query|unreachable|degraded|deprecated)(?:[^` + pyW + `]|$)`)
```

Because the result is boolean, consuming the boundary rune is harmless. Case folding: these words contain `i`
(`retrying`, `denied`, `unreachable`, `deprecated`, `timed`, `timeout`): replace each `i` by `[iIıİ]` for exact Python
parity. Also `\p{N}` vs Python numeric: Python `isalnum` includes all `N*` categories → `\p{N}` is right; Python `isalpha`
= `L*` → `\p{L}` is right.

`ERROR_HINT_RE` alternatives:

* `ORA-\d{5}`, `APP-FND-\d{5}`, `FRM-\d{5}`, `TNS-\d{5}`: plain substring (case-insensitive: `ora-01555` matches), no
  boundary; `\d` → `\p{Nd}`.
* `Traceback \(most recent call last\)`, `deadlock detected`, `Out of memory`: plain, case-insensitive.
* `\bsegfault\b`, `\bkernel panic`, `\bStack overflow\b`, `\bBACKUP failed\b`, `\bLogin failed\b`,
  `\bAssertion failure\b`, `\bcircuit breaker open\b`: boundary on the sides marked `\b` only (e.g. `\bkernel panic`
  has no right boundary → `"kernel panicked"` matches; `\bpanic:` likewise).
* `\b[A-Za-z_.$]*(?:Exception|Error)\b: ` — the right `\b` is always true (`n`/`r` followed by `:`). Left `\b` sits
  before an optional run of `[A-Za-z_.$]` (under `re.I`: plus `ıİſK`). Exact boolean rule: for each occurrence of
  `(?i)(exception|error): ` at index q, let `a` be the start of the maximal run of class chars ending at q; the regex
  matches iff some position p in `[a, q]` is a word boundary (`isWord(rune before p) != isWord(rune at p)`, outside the
  string = non-word). Examples: `java.lang.NullPointerException: x` ✓, `my.Error: x` ✓, `error: x` ✓,
  `5Error: x` ✗ (digit before, run cannot include it), `çError: x` ✗ in Python (Go ASCII `\b` would say ✓).
  Note: R2 is checked first, so `"5Error: x"` / `"çError: x"` still become ERROR via the level word.

Golden coverage: error hints 14 files (`docker_jsonfile.jsonl`, `kubernetes_cri.log`, `oracle_alert.log`,
`sqlserver_errorlog.log`, `syslog_rfc3164.log`, `mut__python_traceback.log`, `mut__tomcat_juli.log`, …); warn hints 12 files
(`docker_jsonfile.jsonl`, `haproxy.log`, `postfix.log`, `storm__alarms.csv`, `storm__alarms.json`, …). Unicode-boundary
cases: **not covered**.

---

## 5. `normalize_severity(value) -> str`

```
if value is None: return "INFO"
s = pyLower(pyStrip(pyStr(value)))
if s in SEVERITY_MAP: return SEVERITY_MAP[s]
if s.isdigit(): return {0:"CRITICAL",1:"CRITICAL",2:"CRITICAL",3:"ERROR",4:"WARN"}.get(int(s), "INFO")   # syslog PRI scale
up = s.upper(); return up if up in SEV_RANK else "INFO"
```

* `str(3.0)` = `"3.0"` → not digit → `"3.0".upper()` → `INFO`. `str(True)` = `"true"` → `INFO`. `"-1"` → `INFO`. `"10"` → `INFO`.
* `"CRİTİCAL"` → lower `"cri̇ti̇cal"` (combining dots) → `INFO`; Go `strings.ToLower` would give `critical` → wrong.
* `"crıtıcal"` → upper `"CRITICAL"` → `CRITICAL`.
* `isdigit` + `int` on `"²"` raises `ValueError` (crash).

## 6. `column_severity(value, use_scale=True) -> str`

```
key = (value if isinstance(value, (str,int,float)) else str(value), use_scale)
if key in _sev_cache: return it                                         # see §1 for hash-equality quirk
s = pyLower(pyStrip(pyStr(value))) if value is not None else ""
out = scenario.SEVERITY_MAP[s] if use_scale and s in scenario.SEVERITY_MAP else normalize_severity(value)
if len(_sev_cache) < 50000: _sev_cache[key] = out
```

`scenario.SEVERITY_MAP = {"5": "CRITICAL", "4": "ERROR", "3": "WARN", "2": "INFO", "1": "DEBUG"}` (S-A1 1–5 scale).
`use_scale` is `False` only when the parser name is `"syslog"` (`parsers/common.py: use_scale = parser != "syslog"`).
Callers only call it when the raw value is not `None`/`""`.

| value | use_scale=True | use_scale=False |
|---|---|---|
| `"5"`, `"4"`, `"3"`, `"2"`, `"1"` | CRITICAL, ERROR, WARN, INFO, DEBUG | INFO, WARN, ERROR, CRITICAL, CRITICAL |
| `"0"` / `"6"` / `"7"` | CRITICAL / INFO / INFO | same |
| `3` (int) | WARN | ERROR |
| `3.0` (float) | INFO fresh (`"3.0"` is not in the scale and not a digit string); WARN if `3` was memoised first (`3.0 == 3`) | INFO fresh; ERROR if `3` was memoised first |
| `" Warning "`, `"E"`, `"p1"`, `"AUDIT_FAILURE"`, `"Firing"`, `"notice"` | WARN, ERROR, CRITICAL, WARN, ERROR, INFO | same |
| `"Note"`, `"System"` (MySQL) | INFO (default) | INFO |
| `True` | INFO fresh; DEBUG if `1` was seen first | |

Go must key the memo by numeric value (int/float/bool equal when numerically equal) to reproduce whichever came first.

Golden coverage: scenario scale str (`journald.jsonl`, `storm__alarms.csv`, `mut__journald.jsonl`), scale int
(`gelf.jsonl`, `storm__alarms.json`), digit fallback str with scale (`journald.jsonl` `"6"`), digit int (`gelf.jsonl` `6`),
use_scale=False digits (`cisco_asa.log`, `syslog_rfc5424.log` + `mut__`), map (78 files), default (`mysql_error.log`
`System`/`Note`; `baseline.json` `100.0`). Bool severities and cache collisions: **not covered**.

## 7. `severity_from_text(text) -> str`

```
m = LEVEL_WORD_RE.search(text);  if m: return normalize_severity(m[1])
if ERROR_HINT_RE.search(text): return "ERROR"
if WARN_HINT_RE.search(text): return "WARN"
return "INFO"
```

`normalize_severity` of the matched word: `INF`→INFO, `WRN`→WARN, `FTL`→CRITICAL, `DBG`/`VRB`→DEBUG, `PANIC`→CRITICAL,
`ALERT`/`EMERG`/`EMERGENCY`/`SEVERE`/`FATAL`→CRITICAL, `NOTICE`→INFO, `TRACE`→DEBUG; Turkish-dotless variants like
`crıtıcal` → CRITICAL via `upper()`, `ınf` → `INFO` (upper `INF` not in SEV_RANK → default INFO).

Caller (`parsers/common.py`) also records `attrs["_sev_src"]`: `"text"` if `LEVEL_WORD_RE.search(msg)` else `"hint"` if
the result ≠ INFO else `"default"` (not set when the severity came from a column: `"field"`). Also `attrs["severity_raw"] =
sev_raw` (original type kept: int stays int, str stays str) when `str(sev_raw).strip().isdigit()`.

## 8. `normalize_environment(value) -> str`

```
if value is None: return ""
v = pyLower(pyStrip(pyStr(value)));  if not v: return ""
if v in ENV_MAP: return ENV_MAP[v]
m = ENV_TOKEN_RE.search(v)
return ENV_MAP[m[1].lower()] if m else v[:20]          # first 20 code points of the lower-cased value
```

| value | result |
|---|---|
| `Production`, `PRD`, `PROD`, `"  Staging "` | `prod`, `prod`, `prod`, `staging` |
| `prod-eu`, `prod_eu`, `x-live-y`, `İstanbul-prod` | `prod` (token) |
| `preprod`, `pre-prod` | `staging` (map) |
| `prod1` | `prod1` (digit after → no token; raw) |
| `eu-west-1`, `DR-site` | `eu-west-1`, `dr-site` (raw; `dr` is in ENV_MAP but not in the token regex) |
| `disaster`, `dr` | `dr` |
| `Canli` / `canlı` | `prod` / `canlı` (dotless ı not translated here) |
| `"a"*30` | 20 × `a` |
| `0`, `1.5`, `True` | `"0"`, `"1.5"`, `"true"` |
| `productıon`, `ſtage` | **KeyError** (see §4.1) |

Golden coverage: map branch and empty value (`alertmanager.json`, `ecs_json.jsonl`, `gelf.jsonl`, `sap__dev_w0`, `sim__*`,
`storm__alarms.csv`, `storm__alarms.json`, `mut__ecs_json.jsonl`, `mut__gelf.jsonl`). Token branch, raw/truncation
branch: **not covered**.

## 9. `infer_environment(*texts) -> str`

```
head = texts[:3]
hit = _env_cache.get(head) or compute: first txt in head (in order, skipping falsy) where ENV_TOKEN_RE.search(txt)
      matches -> ENV_MAP[m[1].lower()], else ""            (memo, cap 50 000)
if hit: return hit
for txt in texts[3:]: same search; first hit returned
return ""
```

No lower-casing/stripping of the input (the regex is case-insensitive). Callers (`parsers/common.py`):
`infer_environment(host, service, source)` memoised per `(host, service)` pair per file; if empty,
`infer_environment(msg)` (msg becomes `head` of a 1-tuple). Examples: `("prd-api-01","","")` → `prod`;
`("web01","checkout","app-prod.log")` → `prod` (from the source file name); `("PROD_DB","","")` → `prod`;
`("db.prod.local","","")` → `prod`; `("ıprod","","")` → `""` (ı is a boundary-blocking letter in Python);
`("prod1","","")` → `""`; message `"deploy to staging done"` → `staging`.

Golden coverage: host/service/source hit — 26 files (`alertmanager.json`, `cef_arcsight.log`, `csv_events.csv`,
`demo__alerts.csv`, `demo__app.jsonl`, `ecs_json.jsonl`, `gelf.jsonl`, `journald.jsonl`, `leef_qradar.log`, `logfmt.log`,
`otel_logs.json`, `sap__SLOG2638.PRD.lst` (file name!), `sap__dev_w0`, `sudo_cron.log`, `syslog_rfc3164.log`,
`syslog_rfc5424.log`, `mut__*`); message hit — 20 files (`dynamics_ax_aos.log`, `dynamics_nav_bc.log`, `golang_log.log`,
`haproxy.log`, `nagios.log`, `oracle_alert.log`, `sqlserver_errorlog.log`, `vmware_esxi.log`, `sap__defaultTrace.0.trc`,
`sap__std_server0.out`, `storm__alarms.json`, `mut__*`).

## 10. `auto_map(keys, sample=None, mapping=None) -> dict[role, key|None]`

`keys`: ordered list of distinct record keys (order of first appearance over the head records). `sample`: list of record
dicts (up to 50 head records). `mapping`: explicit `{role: key}` (from the caller, `scenario.MAPPING`, or a learned profile).

### 10.1 `_norm_key(k)`

```python
_TR = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
def _norm_key(k): return k.translate(_TR).lower().strip()
```

Order: translate (12 single-rune substitutions) → `pyLower` → `pyStrip`. Examples: `"Oluşturma Zamanı"` →
`"olusturma zamani"`, `" LEVEL "` → `"level"`, `"İŞLEM"` → `"islem"`, `"Önem"` → `"onem"`. Keys must be strings.

### 10.2 Hint phase (per role, in role order)

```
lowered = {_norm_key(k): k for k in keys}          # ordered; duplicate normalised keys: LAST original key wins,
                                                   # but the entry keeps the position of the first one
for role, hints in ROLE_HINTS.items():
    if mapping and mapping.get(role) in keys: out[role] = mapping[role]; continue     # exact original key required
    cands = [lowered[h] for h in hints if h in lowered]                                 # hint order, not key order
    hit = cands[0] if cands else None
    if len(cands) > 1 and sample:
        hit = max(cands, key=lambda k: (filled(k), -cands.index(k)))
        # filled(k) = number of sample rows r with r.get(k) not in (None, "")
        # ties -> earliest hint; equal tuples impossible
    if hit is None:   # suffix match
        hit = first k (iterating lowered in order) such that lk.endswith(sep + h) for any h in hints, sep in ("_", ".", "-", " ")
    out[role] = hit
```

Notes:

* `mapping.get(role) in keys` — membership in the **original** key list; a mapping value not present is ignored and the role is resolved normally.
* `r.get(k) not in (None, "")` uses Python equality: `0`, `False`, `[]` count as filled; only `None` and `""`.
* Roles are **not exclusive** in this phase: the same key may serve two roles (only the value phase uses `taken`).
  No hint is shared between roles, but suffix matches can coincide (e.g. key `x_time_source`… `endswith("_source")` → origin).
* `cands.index(k)` returns the first index of `k`; because `lowered` maps each normalised key to one original key, two hints
  can resolve to the same original key only if they normalise equal (impossible, hints are unique).
* Duplicate normalised keys example: `keys=["Level","level","msg"]` → `lowered={"level":"level","msg":"msg"}` → severity = `"level"`.

### 10.3 Examples (probed)

* `["severity","level","msg"]` with rows `{"severity":"", "level":"ERROR"}`, `{"severity":"WARN","level":"ERROR"}` → severity
  `"level"` (filled 2 vs 1).
* `["app_time","x"]`, empty sample → timestamp `"app_time"` (suffix `_time`), message `None`.

### 10.4 Value phase (only if `sample` is non-empty; order: timestamp, severity, environment, message)

```
taken = {v for v in out.values() if v}
share(k, pred) = (count of vals satisfying pred) / len(vals),  vals = [r.get(k) for r in sample if r.get(k) not in (None, "")];  0.0 if no vals

timestamp (if None):  pred(v) = parse_timestamp(v) is not None and len(str(v)) >= 8
     best = max over keys not in taken (key order) by share; assign if share(best) > 0.8; add to taken
severity  (if None):  pred(v) = str(v).lower() in SEVERITY_MAP          # NO strip here
environment (if None): pred(v) = str(v).strip().lower() in ENV_MAP
message   (if None):  numeric(v) = float(str(v)) succeeds
     cands = [k for k in keys if k not in taken and share(k, numeric) < 0.5]
     out["message"] = max(cands, key=lambda k: sum(len(str(r.get(k, ""))) for r in sample), default=None)
```

Details that change results:

* `max(..., default=None)` returns the **first** key with the highest share, even if that share is 0 — then the `> 0.8`
  test fails and nothing is assigned.
* Threshold is strictly `> 0.8`.
* `parse_timestamp` is evaluated for every non-empty value before the length check (its memo is populated; exceptions
  from it would propagate). Values with `len(str(v)) < 8` never count, whatever they parse to — Go may skip parsing them.
* Message length uses `str(r.get(k, ""))`: missing key → 0, `None` → 4 (`"None"`), `True` → 4, `False` → 5, floats by repr,
  lists by repr; length in code points. Example: `keys=["a","b"]`, rows `{"a":"12","b":"x"}`, `{"a":"13","b":None}` →
  `a` excluded (numeric share 1.0), message `b` (1 + 4 = 5).
* Message candidates are not filtered by emptiness: a key whose values are all empty has share 0.0 → candidate with length 0.
* `numeric` accepts `"1_000"`, `"inf"`, `"nan"`, `" 12 "`, `"1e5"`, `".5"`, `"1."` (see §0).
* Severity predicate is not stripped: `" warn"` does not count. Single letters `e w i d f` and words like `ok`, `success`,
  `failed`, `high`, `low`, `unknown`, `path`, `config`, `plain` make a column look like severity.

Probed: `keys=["a","b","c","d"]`, 5 × `{"a":"2026-09-22T09:14:02Z","b":"warn","c":"Prod","d":"hello world"}` →
`timestamp=a` (value), `severity=b` (value), `service=c` (hint `"c"`!), `environment=None` (c taken), `message=d`.
`keys=["n","m"]`, `{"n":"1_000","m":"inf"}` → severity `m` (`"inf"` ∈ SEVERITY_MAP), message `None` (n numeric, m taken).

### 10.5 Golden coverage of auto_map paths

| path | goldens |
|---|---|
| explicit/learned mapping | 37 files for timestamp (learned profiles in live batches and `mut__` twins), e.g. `alertmanager.json`, `csv_events.csv`, `demo__app.jsonl`, `bind9_query.log` |
| exact single hint | ~100 files |
| multi-candidate by fill count | timestamp: `cef_arcsight.log`, `fortigate_kv.log`, `leef_qradar.log`; severity: `alertmanager.json`, `apache_access_clf.log`, `aws_alb_access.log`, `nginx_combined.log`, `windows_event_xml.log`, `leef_qradar.log`; service: `cef_arcsight.log`, `demo__alerts.csv`, `journald.jsonl`; host: `cef_arcsight.log`, `leef_qradar.log`; message: `alertmanager.json`, `aws_cloudtrail.json`, `gelf.jsonl`, `leef_qradar.log` |
| multi-candidate where a later hint wins | message `log` in `docker_jsonfile.jsonl`; severity `level` in `fortigate_kv.log` |
| suffix | `baseline.json` (`alertmanager.json.timestamp`, `.level`, `.host`, `.service`), `gelf.jsonl` (`_env`), `storm__alarms.json` (`tags.ortam`), `sap__bootstrap.jvm` (`sap.instance`), `sap__dev_w0` (`sap.component`) |
| value: timestamp | `storm__alarms.csv`, `mut__csv_events.csv` (live, header-less batches) |
| value: severity | `aws_cloudtrail.json` (`responseElements.ConsoleLogin` = `Success`/`Failure`), `mut__csv_events.csv` |
| value: environment | `storm__alarms.csv` (live), `sap__dev_w0` (`sap.sid` — values like `PRD`) |
| value: message | `baseline.json`, `fortigate_kv.log` (`srcip`), `mut__csv_events.csv`, `storm__alarms.csv` |
| message `None` | `mut__csv_events.csv` (one live batch) |
| duplicate normalised keys, Turkish keys, keys with surrounding whitespace | **not covered** |

## 11. `flatten(d, prefix="") -> dict`

```python
def flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict): out.update(flatten(v, f"{prefix}{k}."))
        else: out[f"{prefix}{k}"] = v
    return out
```

* Recursive, unlimited depth, separator `.`, keys used as-is (JSON keys are strings).
* Empty dict values vanish (`{"e": {}}` contributes nothing).
* Lists are kept as values (dicts inside lists are **not** flattened).
* Collisions: later writes overwrite earlier ones but keep the first insertion position:
  `{"a": {"b": {"c": 1}, "d": [{"x": 1}]}, "a.b": {"c": 2}, "e": {}, "f": None}` →
  `{"a.b.c": 2, "a.d": [{"x": 1}], "f": None}`.
* Go: decode JSON with an order-preserving decoder (`json.Decoder.Token` stream), `UseNumber` to keep Python int vs float
  distinction (`1` stays int, `1.0` stays float `1.0`; a Python int can exceed int64 — use big.Int or keep the literal).
  Duplicate keys inside one JSON object: Python `json.loads` keeps the **last** value at the **first** position — same rule.

Used by `parsers/json_parser.py` and `parsers/jsonl_parser.py` (`enrich(flatten(r))`). Dotted keys appear in 23 goldens
(`alertmanager.json`, `aws_cloudtrail.json`, `ecs_json.jsonl`, `mongodb.jsonl` (`t.$date`, `attr.*`), `sap__*`, `sim__simulate_traces.jsonl`, …).

### 11.1 `json_parser.enrich(rec)` (runs on the flattened record)

```
if any(k in rec for k in ("severity","level","log.level","severityText","PRIORITY","s")): return rec   # case-sensitive keys
txt = first rec[k] for k in ("log","message","msg","short_message") that is a str
if not txt: return rec                                   # also when the string is ""
head, rest = text_parser.parse_head(txt.rstrip("\n"))    # see the text-parser slice
if head.get("severity"):
    rec["severity"] = head["severity"]
    if head.get("service") and not any(k in rec for k in ROLE_HINTS["service"]): rec["service"] = head["service"]
    if rest: rec["message"] = rest
return rec
```

New keys are appended at the end of the record (order matters for `keys` and attributes). Example:
`{"log": "2026-09-22 09:14:02 ERROR app: boom\n", "stream": "stderr"}` → adds `severity="ERROR"`, `service="app"`,
`message="boom"`. `rstrip("\n")` strips only `\n` characters. Note the service-hint check uses the raw hint strings
against raw (not normalised) keys.

---

## 12. `profiles.py` — learned column mappings

Storage: `<home>/profiles.json`, `home = $WATCHOVER_HOME` (or `./data` if unset/empty; created if missing). Format: one JSON
object, `json.dumps(d, ensure_ascii=False, indent=1)`, UTF-8:

```json
{
 "<sig>": {
  "id": "<sig>", "format": "jsonl", "keys": ["<sorted keys, at most 60>"],
  "mapping": {"timestamp": "ts", "message": "msg", ...},     // only ROLES with truthy values
  "name": "<file name, max 120 chars>", "source": "auto" | "user", "hits": 3,
  "first": "2026-10-15T09:30:00+00:00", "last": "2026-10-15T09:31:12+00:00"
 }
}
```

`ROLES = ("timestamp","severity","service","host","message","environment","origin")`.

`signature(fmt, keys) = sha1((fmt + "|" + ",".join(sorted(str(k) for k in keys))).encode("utf-8")).hexdigest()[:16]`
(Python `sorted` on str = code-point order = Go `sort.Strings` on valid UTF-8; keys containing `,` can collide — harmless).

`lookup(fmt, keys)`: load (missing/invalid file → `{}`), return the record for the signature only if its `mapping` is
non-empty, else `None`.

`remember(fmt, keys, mapping, name, source)`: no-op if `keys` empty or the cleaned mapping is empty. Under a process lock:
if an existing record has `source=="user"` and the new source is not `"user"`, only `hits+1` and `last` are updated.
Otherwise the record is replaced: `mapping` = cleaned new mapping, `name` = new name or old name, `[:120]`;
`hits` = old+1; `first` kept; `last` = now (UTC, `isoformat(timespec="seconds")`). Exceptions are swallowed by the caller.

When it changes parsing (`pipeline._parse_one`): only if the caller passed **no** mapping (`scenario.MAPPING` is `{}` so
this is the default), the probe found keys, and the detected format is one of `json, jsonl, csv, kv, grok`:

1. `learned = lookup(fmt, keys)`; if found, `mapping = dict(learned["mapping"])` is passed to **both** `auto_map` (report
   `roles`) and `parser.parse` → `records_to_observations` → `auto_map` (observation roles). Since the signature includes the
   full key set, every learned key exists, so learned roles always win; roles absent from the learned mapping are still
   resolved by hints/values.
2. After parsing, `remember(fmt, keys, roles-with-values, name=fname, source="user" if (mapping and not learned) else "auto")`
   — always `"auto"` in the default configuration, so each new parse **overwrites** the mapping with the current roles
   (which include the learned ones; it can only grow).
3. Report field `"profile"`: `learned["id"]` or `""`.

Effects in the goldens: `parse_golden.py` sets `WATCHOVER_HOME` to a fresh temp dir **only if unset**
(`os.environ.setdefault`), then processes corpus files (sorted) and then `receiver/parse/testdata/inputs` (sorted), each
whole then live, in one process sharing one profile store. Consequences:

* Live runs: batch 1 remembers, later batches of the same shape look it up → learned mapping (observations identical to
  an un-learned run in all goldens).
* `mut__X` whole runs reuse the profile remembered for the corpus file `X` when the key set is identical. Isolated runs
  give identical observations; only the report `"profile"` differs. The 14 goldens whose first line carries a non-empty
  `"profile"` because of an earlier file: `mut__bind9_query.log`, `mut__csv_events.csv`, `mut__docker_jsonfile.jsonl`,
  `mut__dockerd.log`, `mut__ecs_json.jsonl`, `mut__fortigate_kv.log`, `mut__gelf.jsonl`, `mut__journald.jsonl`,
  `mut__logfmt.log`, `mut__mongodb.jsonl`, `mut__nagios.log`, `mut__rabbitmq.log`, `mut__squid_access.log`,
  `mut__vmware_esxi.log` (all `.whole`).

What Go must do: either implement the same store (same file, same JSON layout, so a Go receiver and the Python app sharing
`WATCHOVER_HOME` stay compatible) and replay the files in the generator's order with one store, or compare reports
ignoring `"profile"`. A pre-existing `WATCHOVER_HOME` when generating goldens would change results — always unset it.

---

## 13. Scenario constants used by parsing (`src/watchover/scenario/__init__.py`)

| constant | value | used by | effect |
|---|---|---|---|
| `MAPPING` | `{}` | `pipeline.ingest`: `mapping = mapping or scenario.MAPPING or None` | empty → `None` → auto-mapping + learned profiles |
| `SEVERITY_MAP` | `{"5":"CRITICAL","4":"ERROR","3":"WARN","2":"INFO","1":"DEBUG"}` | `column_severity` (§6) | numeric 1–5 scale wins over generic rules when `use_scale` |
| `SIDE_TABLES` | `[r"depend", r"bagimlilik", r"inventory", r"envanter", r"sozlugu", r"brifing", r"readme"]` | `tables.is_side_table`: `re.search(p, basename.lower())` | matching files are not parsed as events (report `format: "table"`, no observations). RE2-compatible. No golden input matches. |
| `DEDUP_KEY` | `"alarm_id"` | `pipeline.dedupe` after all files of one ingest | observations whose `attributes["alarm_id"]` (non-empty, compared as `str`) repeats are dropped, keeping the first, except a later one with `parser == DEDUP_PREFER` replaces a non-preferred one; output = pass-through rows first, then kept rows in first-seen order (then the global stable sort by timestamp). Report gets `dedup_dropped`. **Not triggered in any golden** (each golden ingests one file; no duplicate ids). |
| `DEDUP_PREFER` | `"json"` | same | |
| `DEP_PATTERNS`, `EXTRA_MASKS`, `EXTRA_DEPENDENCY_WORDS`, `DEP_COLUMNS`, `INVENTORY_COLUMNS`, clustering/scoring constants, `RECOMMENDATIONS`, `OWNERS` … | | `analysis.py`, `storm.py`, `graph.py`, `tables.py` | **not used by parsing**; out of scope for the Go parser. |

`knowledge.apply_rules` mutates `NOISE_*` and `EXTRA_DEPENDENCIES` at runtime — not parse-relevant.

---

## 14. Canonical JSON and float formatting

Goldens are written with `json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)`:

* Separators `", "` and `": "`; keys sorted by code point (Go: sort keys as UTF-8 bytes — same order).
* Strings: escape `"` → `\"`, `\` → `\\`, `\n`, `\r`, `\t`, `\b`, `\f`, other `U+0000`–`U+001F` → `\u00xx` (lower-case hex).
  Everything else literal, including `U+007F`, `U+2028`, `U+2029`, `<`, `>`, `&` and non-BMP characters.
  Go `encoding/json` must not be used directly (it escapes `<>&` unless `SetEscapeHTML(false)`, always escapes U+2028/2029,
  and replaces invalid UTF-8).
* `None` → `null`, `True` → `true`; ints in full precision; non-serialisable objects → `str(obj)`.
* Floats → Python `repr`: shortest round-trip digits; fixed notation when the decimal exponent `e` (as in `d.ddd×10^e`)
  satisfies `-4 <= e < 16`, always with at least one fractional digit (`1.0`, `0.117`, `32.5`, `100.0`, `0.0001`,
  `1234567890123456.0`); otherwise exponent notation with sign and at least 2 exponent digits (`1e+16`, `1.5e-05`,
  `1e-05`); `-0.0` → `-0.0`; `nan`/`inf` → `NaN`/`Infinity`/`-Infinity`.

```go
func pyFloatRepr(f float64) string {
    switch { case math.IsNaN(f): return "NaN"; case math.IsInf(f, 1): return "Infinity"; case math.IsInf(f, -1): return "-Infinity" }
    s := strconv.FormatFloat(f, 'e', -1, 64)          // shortest digits, e.g. "1.5e-05"
    mant, expS, _ := strings.Cut(s, "e"); exp, _ := strconv.Atoi(expS)
    if exp < -4 || exp >= 16 {
        sign := "+"; if exp < 0 { sign = "-"; exp = -exp }
        return fmt.Sprintf("%se%s%02d", mant, sign, exp)
    }
    out := strconv.FormatFloat(f, 'f', -1, 64)
    if !strings.ContainsAny(out, ".") { out += ".0" }
    return out
}
```

In the goldens floats appear only in fixed notation (≈ 19 997 values with fraction digits, 18 856 ending in `.0`, mostly
`parser_confidence` = `round(x, 4)` and attributes like `duration_s`/`duration_ms`); exponent notation is **not covered**.

`ts` = `isoformat()` (§3.9).

---

## 15. Branches NOT covered by any golden

* `parse_timestamp`: 13-digit and 14-digit epoch strings; strptime formats 1–6, 9, 13, 16, 18, 21–23, 25, 26; format-cache
  order dependence; `_ts_cache` clearing at 200 000; numeric out-of-range / NaN exceptions; dateutil shapes other than
  time-only and `DD-Mon-YYYY HH:MM:SS.fff`; dateutil tz names; dayfirst handling; Feb 29; non-ASCII digits.
* `column_severity`: bool values, `1`/`1.0`/`True` memo collisions, cache cap; `normalize_severity` on Turkish dotted/dotless
  letters; non-`Nd` digit crash.
* `severity_from_text`: Unicode `\b` and `ı/İ` case-fold cases.
* `normalize_environment`: token branch, raw branch, `[:20]` truncation, KeyError crash on `ı`/`ſ` tokens.
* `infer_environment`: KeyError crash; `ı`/`İ`/`ſ`/Kelvin boundaries.
* `auto_map`: duplicate normalised keys, Turkish key normalisation, whitespace in keys, list values, `None`-length effect
  on message choice beyond simple cases, numeric parsing of `1_000`/`inf`.
* `flatten`: collisions, empty dicts, lists of dicts (no list values in any golden).
* `profiles`: user-sourced profiles, `update`/`delete`, explicit mappings, pre-existing store.
* `scenario`: side tables, dedup.

---

## 16. Go implementation checklist

1. Python string helpers: `pyStrip`, `pyLower`, `pyStr`, `pyIsDigit`, `pyFloatOK`, `pyFloatRepr`, rune-based slicing/len.
2. Clock injection: `nowYear` for `_DEFAULT` and the `%b %d %H:%M:%S` year (goldens: 2026).
3. Process-global, mutex-protected `tsCache` (clear at 200 000) and `formatCache` (shape → format, update only on loop success).
4. Timestamp pipeline exactly as §3.3 with big.Rat epoch division and the modf/half-even `fromtimestamp`.
5. `fromisoformat` 3.11 subset (§3.4), strptime emulation with Python directive regexes (§3.5), dateutil port or subset (§3.7).
6. `isoformat` output (§3.9).
7. Severity: `SEVERITY_MAP`, scenario scale, syslog digit scale, `SEV_RANK` fallback, memo keyed by numeric value.
8. Hand-written lookaround scanners for `ENV_TOKEN_RE` and `LEVEL_WORD_RE` with Python `re.I` folding; Unicode-aware boundaries
   for the hint regexes.
9. `auto_map` with ordered maps, first-max semantics, Python `str()` lengths.
10. Order-preserving JSON decoding + `flatten` + `enrich`.
11. Profile store compatible with `profiles.json`, or ignore the report `"profile"` field in parity tests.
