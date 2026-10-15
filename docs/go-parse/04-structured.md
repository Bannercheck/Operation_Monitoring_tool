# Go parse port, part 04: structured formats

Scope: `parsers/syslog_parser.py`, `access_parser.py`, `w3c_parser.py`, `kv_parser.py`, `cef_parser.py`,
`winevt_parser.py`, `csv_parser.py`, `json_parser.py`, `jsonl_parser.py`, plus the parts of `pipeline.py` that touch
them. Every parser is a pure function `records(text) -> [(line_no, record)]`; `Parser.parse` then hands the records to
`common.records_to_observations` (see [01-pipeline §9](01-pipeline.md)). This document specifies **only** what each
parser emits (the ordered record dicts and their `line_no`). Timestamp *parsing*, severity mapping, role mapping and
the canonical JSON writer are in [01-pipeline](01-pipeline.md) and [02-normalize](02-normalize.md); they are referenced,
not repeated.

Helper names used below come from [01-pipeline §1](01-pipeline.md): `pyWS`, `PS` (Python `\s`), `PNS` (Python `\S`),
`pyStrip`, `pySplitLines`, `pyLower`, `pyIsDigit`, `pyFloat`, `pyFloatRepr`. In Go regex text below, `PS`/`PNS` are
literal placeholders for those character classes, `W` stands for `\p{L}\p{N}_` (Python `\w`, used inside `[...]`) and
`D` for `\p{Nd}` (Python `\d`). Build the Go patterns with string concatenation.

---

## 0. Rules that apply to every parser in this slice

### 0.1 Records are ordered maps

Every record is a Python `dict`; **insertion order is observable** and Go must keep it (an ordered map: `[]key` +
`map[key]value`). It matters for:

* the report `keys` list (first 50 records, keys in first-seen order) — written to the golden report line;
* `auto_map` (role choice can depend on key order, see 02 §10);
* the message fallback `" ".join(f"{k}={v}" for k, v in rec.items())` when no message role is found (01 §9);
* `raw` for the non-line-oriented parsers (`winevt`, `json`), which is `json.dumps(rec, ensure_ascii=False,
  default=str)` **in insertion order** with `", "` / `": "` separators (01 §2.1 string escaping; floats per 01 §1.7;
  `NaN`/`Infinity`/`-Infinity` literals; ints of any size verbatim).

Python dict semantics Go must copy: assigning an existing key replaces the value **in place** (position unchanged);
`dict.update(other)` does the same per key and appends new keys in `other`'s order; `pop(k)` removes the key (a later
re-assignment appends at the end); a dict comprehension/literal with duplicate keys keeps the **first position** and the
**last value**; `setdefault(k, v)` appends only when absent.

### 0.2 Line numbering and `raw`

* `line_oriented = True` (all parsers here except `winevt` and `json`): `Parser.parse` computes
  `lines = pySplitLines(text)` and `raw = lines[line_no - 1]` if `0 < line_no <= len(lines)`, else
  `json.dumps(rec)` as above. `line_no` is 1-based over `pySplitLines(text)` for every parser **except `csv`** (see §7).
* `line_oriented = False` (`winevt`, `json`): `line_no` is the 1-based index of the record, and `raw = json.dumps(rec)`.
* Blank-line test, everywhere it says "blank": `pyStrip(ln) == ""`.

### 0.3 The parser does not crash-proof itself

Several malformed inputs raise a Python exception inside `records()`. Nothing in `_parse_one` / `ingest` catches it,
so the **whole ingest call fails** (an upload fails; in `live.py` the batch is dropped by its caller). These are listed
per parser as "raises". Go must return an error for the same inputs (do not silently skip), otherwise outputs differ.
None of them occurs in the goldens.

### 0.4 `records()` is called twice

`_parse_one` iterates `parser.records(probe)` for the first 50 records (`probe = text[:400_000]` code points for
line-oriented parsers, whole text otherwise) and then `parser.parse(text)` iterates again from scratch. All state in
these parsers is local to one `records()` call (W3C `fields`/`software`), so the two runs are independent.
No parser in this slice reads `FAMILY_HINT` / `FAMILY_SEEN`, profiles or scenario constants itself (those act in
`_parse_one` / `records_to_observations`, see §11).

### 0.5 Kinds

| parser | `name` | `kind` | `line_oriented` |
|---|---|---|---|
| SyslogParser | `syslog` | `log` | yes |
| AccessParser | `access` | `log` | yes |
| W3cParser | `w3c` | `log` | yes |
| KvParser | `kv` | `log` | yes |
| CefParser | `cef` | `event` | yes |
| WinEvtParser | `winevt` | `event` | **no** |
| CsvParser | `csv` | `event` | yes |
| JsonParser | `json` | `event` | **no** |
| JsonlParser | `jsonl` | `event` | yes |

(`records_to_observations` may override kind to `alert` / `incident`, 01 §9.) `use_scale = parser != "syslog"`, so
syslog numeric severities are read as syslog levels, every other parser's as a 1–10 / 0–5 scale (02 §6).

### 0.6 Value types emitted

| parser | value types in records |
|---|---|
| syslog, w3c, kv, cef, winevt | `str` only |
| access | `str`; `float` (`duration_s`, `upstream_duration_s`); `int` (`duration_us`, `duration_ms`, `upstream_duration_ms`); `bool` (`ts_is_start`, always `True`) |
| csv | `str`, or `None` for a header column missing from a short row |
| json, jsonl | anything JSON gives after `flatten`: `str`, `int` (arbitrary size), `float` (incl. `nan`/`inf` from `NaN`/`Infinity`/`1e400`), `bool`, `None`, `list` (never flattened, may contain dicts). Nested dicts are flattened to dotted keys (02 §11). |

Attribute filtering (`v not in (None, "")`) happens later in `records_to_observations`; parsers keep empty strings.

---

## 1. `syslog_parser.py` — `SyslogParser.records(text)`

### 1.1 Regexes (verbatim) and RE2 status

From `format_detector.py`:

```python
_SYSLOG_TS = r"(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}(?:\s\d{4})?\s\d{2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2}T[\d:.+\-Z]+)"
_NOT_LEVEL = r"(?!(?i:TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERR|ERROR|CRIT|CRITICAL|FATAL|SEVERE|INF|WRN|FTL|DBG|VERBOSE)\b)"
SYSLOG_RE = re.compile(r"^(?:<(?P<pri>\d+)>)?" + _SYSLOG_TS + r"\s+(?P<host>" + _NOT_LEVEL + r"\S+)\s+(?P<prog>[^:\[\s]+)(?:\[(?P<pid>\d+)\])?:\s*(?P<msg>.*)$")
SYSLOG_NOPROG_RE = re.compile(r"^(?:<(?P<pri>\d+)>)?" + _SYSLOG_TS + r"\s+(?P<host>" + _NOT_LEVEL + r"[A-Za-z][\w.\-]*)\s+(?P<msg>.*)$")
RFC5424_RE = re.compile(r"^<(?P<pri>\d+)>1 (?P<ts>\S+) (?P<host>\S+) (?P<app>\S+) (?P<pid>\S+) (?P<msgid>\S+) (?P<sd>-|(?:\[.*?\])+) ?(?P<msg>.*)$")
```

From `syslog_parser.py`:

```python
ASA_RE = re.compile(r"^%(?P<vendor>ASA|FTD|PIX|FWSM)-(?P<sev>\d)-(?P<id>\d+)$")
SD_RE = re.compile(r'\[([\w@.\-]+)((?:\s+[\w.\-]+="(?:[^"\\]|\\.)*")*)\]')
SD_KV_RE = re.compile(r'([\w.\-]+)="((?:[^"\\]|\\.)*)"')
PAN_TYPES = {"TRAFFIC": "INFO", "THREAT": "WARN", "SYSTEM": "INFO", "CONFIG": "INFO", "USERID": "INFO", "HIPMATCH": "INFO", "GLOBALPROTECT": "INFO"}
PAN_SEV = {"critical": "CRITICAL", "high": "ERROR", "medium": "WARN", "low": "INFO", "informational": "INFO"}
```

| regex | RE2 as is? | Go rewrite |
|---|---|---|
| `RFC5424_RE` | yes, after class fixes | `^<(?P<pri>D+)>1 (?P<ts>PNS+) (?P<host>PNS+) (?P<app>PNS+) (?P<pid>PNS+) (?P<msgid>PNS+) (?P<sd>-\|(?:\[.*?\])+) ?(?P<msg>.*)$` |
| `SYSLOG_RE` | **no** (`_NOT_LEVEL` negative lookahead + `\b`) | regex without the lookahead + host post-check (§1.2) |
| `SYSLOG_NOPROG_RE` | **no** (same) | same approach |
| `ASA_RE` | yes | `^%(?P<vendor>ASA\|FTD\|PIX\|FWSM)-(?P<sev>D)-(?P<id>D+)$` |
| `SD_RE` | yes, after class fixes | `\[([W@.\-]+)((?:PS+[W.\-]+="(?:[^"\\]\|\\.)*")*)\]` |
| `SD_KV_RE` | yes, after class fixes | `([W.\-]+)="((?:[^"\\]\|\\.)*)"` |

Leftmost-first submatch semantics in Go equal Python's backtracking result for all of these (no backreferences). In
`RFC5424_RE` the `sd` group prefers `-`; otherwise it takes one or more `[...]` blocks, each ending at the **first** `]`
(an escaped `\]` inside an SD value ends the block early; the rest goes to `msg` — identical in both engines). `$` is
end-of-string because lines contain no `\n`.

### 1.2 `_NOT_LEVEL` rewrite (exact)

Go patterns (no lookahead):

```
TS     = (?P<ts>[A-Z][a-z]{2}PS+D{1,2}(?:PSD{4})?PSD{2}:D{2}:D{2}|D{4}-D{2}-D{2}T[D:.+\-Z]+)
SYSLOG = ^(?:<(?P<pri>D+)>)?TS PS+(?P<host>PNS+)PS+(?P<prog>[^:\[pyWS]+)(?:\[(?P<pid>D+)\])?:PS*(?P<msg>.*)$     (no space between TS and PS+)
NOPROG = ^(?:<(?P<pri>D+)>)?TS PS+(?P<host>[A-Za-z][W.\-]*)PS+(?P<msg>.*)$
```

After a successful Go match, **reject the match** (treat as no match) if `isLevelWord(host)`:

```go
var levelWords = set("trace","debug","info","notice","warn","warning","err","error","crit","critical",
                     "fatal","severe","inf","wrn","ftl","dbg","verbose")
// fold one rune the way Python re.IGNORECASE does for these ASCII words
func foldI(r rune) rune {
    switch r { case 0x130, 0x131: return 'i'; case 0x17F: return 's'; case 0x212A: return 'k' }
    if r < 128 { return unicode.ToLower(r) }
    return -1 // any other non-ASCII rune cannot match an ASCII letter
}
func isLevelWord(host string) bool {
    // leading run of Python \w ([\p{L}\p{N}_]) characters of host
    var b strings.Builder
    for _, r := range host {
        if !(unicode.IsLetter(r) || unicode.IsNumber(r) || r == '_') { break }
        f := foldI(r); if f < 0 { return false }
        b.WriteRune(f)
    }
    return levelWords[b.String()]
}
```

Why this is exact: the lookahead sits at the fixed host start (the host start cannot move: `ts` cannot backtrack to end
on a non-space, and the host must follow the whitespace run). `WORD\b` matches iff the maximal `\w` run starting there
equals WORD case-insensitively (every alternative ends in a word character, so a longer run never has a boundary after
a shorter word). Python `re.I` lets `i` match `ı`/`İ`, `s` match `ſ`, `k` match `K` (01 §1.5); no level word contains
`k` but the fold is kept for safety. Measured: hosts `INFO`, `info-svc`, `İNFO` are rejected; `ERRORS` is accepted.

The rejection matters in goldens: lines rejected only because of `_NOT_LEVEL` exist in `dynamics_365_batch.log`,
`mut__dynamics_365_batch.log`, `mut__python_traceback.log` (SYSLOG_RE) and `mut__java_stacktrace.log`,
`mut__plain_app_text.log`, `mut__timestamp_variants.log`, `vmware_esxi.log`, `mut__vmware_esxi.log` (NOPROG). These
files are detected as `text`, so the effect is through `detect_format` (01 §8), but the same regexes must be used here.

### 1.3 Algorithm

```
for i, ln in enumerate(pySplitLines(text), start=1):
    if blank(ln): continue
    1. RFC 5424:  m = RFC5424_RE.match(ln)
       rec = {"timestamp": m.ts, "host": m.host, "program": m.app if m.app != "-" else "", "message": m.msg}
       if m.pid and m.pid != "-":     rec["pid"] = m.pid
       if m.msgid and m.msgid != "-": rec["msgid"] = m.msgid
       rec["severity"] = str(int(m.pri) % 8)
       for each SD_RE match sd over (m.sd or ""):            # m.sd == "-" gives no match
           for (k, v) in SD_KV_RE.findall(sd.group(2)):
               rec[f"{sd.group(1)}.{k}"] = v                   # value raw: \" and \\ stay escaped
       if rec["message"] starts with U+FEFF: drop that one code point
       yield i, rec; continue
    2. BSD:  m = SYSLOG_RE.match(ln)   (with §1.2 host check)
       rec = {"timestamp": m.ts, "host": m.host, "program": m.prog, "message": m.msg}
       if m.pid: rec["pid"] = m.pid
       if m.pri: rec["severity"] = str(int(m.pri) % 8)
       if a = ASA_RE.match(m.prog):
           rec.update({"program": lower(a.vendor), "severity": a.sev, "msgid": a.id})
       yield i, rec; continue
    3. no program:  m = SYSLOG_NOPROG_RE.match(ln)   (with §1.2 host check)
       rec = {"timestamp": m.ts, "host": m.host, "message": m.msg}
       if m.pri: rec["severity"] = str(int(m.pri) % 8)
       if pan = _pan(m.msg): rec.update(pan)
       yield i, rec
    4. otherwise: no record (line dropped)
```

Details:

* `int(pri) % 8`: `pri` is unbounded `\d+` (Python big int, Unicode digits allowed, leading zeros fine:
  `<0013>` → `"5"`). Exact Go: take the digit values (`unicode` digit value of each `\p{Nd}` rune) of the **last three
  digits** as a number and `% 8` (1000 ≡ 0 mod 8). The result is a one-character string `"0"`..`"7"`.
* Key order RFC 5424: `timestamp, host, program, message, [pid], [msgid], severity, <sd keys in order>`. Duplicate SD
  keys overwrite in place. `program` is always present (possibly `""`). The BOM strip changes the value only.
* Key order BSD: `timestamp, host, program, message, [pid], [severity]`, then for ASA `program` (replaced in place),
  `severity` (in place if a `<pri>` existed, else appended), `msgid` (appended). `program` becomes `asa`/`ftd`/`pix`/
  `fwsm` (ASCII lower), `severity` the single digit from the id (`%ASA-4-…` → `"4"`).
* `msg` (`.*` after `:\s*`) has leading whitespace already consumed by `\s*` (Python whitespace set). Trailing
  whitespace is kept.
* The ISO alternative of `ts` accepts anything in `[\d:.+\-Z]+`, e.g. `2026-10-14T09:30:00.987Z`. Combined with
  `host = \S+` this makes "ISO-timestamp + word + colon" text lines parse as syslog, e.g.
  `2026-10-14T09:30:00.987Z Ax32Serv: Error: …` → host `Ax32Serv:`, program `Error`, message `…` and
  `2026-10-14T09:30:00.003Z Logon       Error: 604223, …` → host `Logon`, program `Error`
  (`mut__dynamics_ax_aos.log`, `mut__sqlserver_errorlog.log` whole runs; the latter also has 193 NOPROG lines).
* BSD timestamps are returned as text, e.g. `Sep 22 09:14:02` (no year → current year, `+00:00`, 02 §3) and
  `Sep 22 2026 09:15:10` (Cisco, with year).
* iptables / haproxy / postfix / sudo / cron lines get **no special parsing** here: they are plain BSD records; the
  whole text after `prog:` is `message` (e.g. `[812231.112] [UFW BLOCK] IN=eth0 …`, `deploy : TTY=pts/0 ; …`).

### 1.4 `_pan(msg)` — Palo Alto PAN-OS CSV payload

```
if not msg.startswith("1,") or msg.count(",") < 20: return None
f = msg.split(",")                              # plain split, NO csv quoting
typ, sub = pyStrip(f[3]), pyStrip(f[4])
rec = {"service": "pan-os", "pan_type": typ, "pan_subtype": sub,
       "message": pyStrip(f"{typ} {sub} {f[7]} -> {f[8]}")}
if typ == "THREAT":
    sev = first PAN_SEV[pyLower(pyStrip(x))] for x in f[30:40] if pyLower(pyStrip(x)) in PAN_SEV, default "WARN"
    rec["severity"] = sev
    threat = first x.strip('"') for x in f[30:36] if x.startswith('"'), default ""
    rec["message"] = pyStrip(f"{typ} {sub} {f[7]} -> {f[8]} {threat}")
else:
    rec["severity"] = PAN_TYPES.get(typ, "INFO")
if len(f) > 1 and pyStrip(f[1]): rec["timestamp"] = pyStrip(f[1])
return rec
```

* `f[30:40]` / `f[30:36]` are Python slices (clamped, may be empty). `x.strip('"')` strips any number of `"` from both
  ends. A threat name containing commas is cut at the first comma (`"A, B"` → `A`).
* `pyLower` must be the Python one (`İ` → `i̇`, so `"İNFORMATIONAL"` is **not** a PAN_SEV key in Python).
* Merge: `rec.update(pan)`. Final key order with no `<pri>`: `timestamp, host, message, service, pan_type, pan_subtype,
  severity` (`timestamp`/`message` replaced in place). With `<pri>`: `timestamp, host, message, severity, service,
  pan_type, pan_subtype`.
* The PAN timestamp is `2026/09/22 09:14:02` (naive → `+00:00`).
* Golden: `paloalto_csv.log` (TRAFFIC + THREAT/critical with quoted threat name), `mut__paloalto_csv.log`.

### 1.5 Golden coverage (syslog)

| branch | inputs |
|---|---|
| RFC 5424, SD params, `-` app/pid/msgid, `-` SD | `syslog_rfc5424.log`, `mut__syslog_rfc5424.log` |
| BSD with pid / without pid | `syslog_rfc3164.log`, `postfix.log` (`postfix/smtpd[1123]`), `sudo_cron.log`, `haproxy.log`, `iptables_kernel.log`, `demo__db.log` and their `mut__` versions |
| Cisco ASA (with year) | `cisco_asa.log`, `mut__cisco_asa.log` |
| ISO-ts "syslog" lines | `mut__dynamics_ax_aos.log`, `mut__sqlserver_errorlog.log` |
| NOPROG + PAN TRAFFIC/THREAT | `paloalto_csv.log`, `mut__paloalto_csv.log` |
| NOPROG non-PAN | `mut__sqlserver_errorlog.log` |

**Not covered:** any `<PRI>` on a BSD/NOPROG line (0 lines), U+FEFF BOM after an RFC 5424 header (the sample has the
literal text `BOM`), ASA vendors other than `ASA`, PAN types other than TRAFFIC/THREAT, PAN THREAT without a severity
word in fields 30–39, SD values with escapes, multiple SD blocks, Unicode digits/whitespace.

---

## 2. `access_parser.py` — `AccessParser.records(text)`

### 2.1 Regexes (verbatim)

```python
CLF_RE = re.compile(r'^(?P<client>\S+) (?P<ident>\S+) (?P<user>\S+) \[(?P<ts>[^\]]+)\] "(?P<request>[^"]*)" (?P<status>\d{3}|-) (?P<bytes>\S+)'
                    r'(?: "(?P<referer>[^"]*)" "(?P<agent>[^"]*)")?(?: (?P<extra>.*))?$')
VHOST_RE = re.compile(r'^(?P<vhost>[\w.\-]+)(?::\d+)? (?=\S+ \S+ \S+ \[)')
ALB_RE = re.compile(r"^(?P<proto>https?|h2|ws|wss|tls|tcp) (?P<ts>\S+) (?P<elb>\S+) (?P<client>\S+) (?P<target>\S+) (?P<t_req>\S+) (?P<t_tgt>\S+) (?P<t_resp>\S+) "
                    r"(?P<status>\S+) (?P<tstatus>\S+) (?P<rbytes>\S+) (?P<sbytes>\S+) \"(?P<request>[^\"]*)\" \"(?P<agent>[^\"]*)\" \S+ \S+ (?P<tg>\S+) \"[^\"]*\" \"(?P<domain>[^\"]*)\"")
ENVOY_RE = re.compile(r'^\[(?P<ts>[^\]]+)\] "(?P<request>[A-Z]+ \S+ [^"]*)" (?P<status>\d{3}|-) (?P<flags>\S+) (?:\S+ )?(?P<rx>\d+|-) (?P<tx>\d+|-) '
                      r'(?P<dur>\d+|-) (?P<up>\d+|-) "(?P<xff>[^"]*)" "(?P<agent>[^"]*)" "(?P<reqid>[^"]*)" "(?P<authority>[^"]*)" "(?P<upstream>[^"]*)"')
EXTRA_KV = re.compile(r'(?P<k>[A-Za-z_][\w.\-]*)=(?P<v>"[^"]*"|\S+)')
UUIDISH = re.compile(r'^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$|^[0-9a-fA-F]{16,64}$')
# inside parse_extra:  re.fullmatch(r"\d+\.\d+", t)   and   re.fullmatch(r"\d+", t)
```

| regex | RE2 as is? | Go rewrite |
|---|---|---|
| `CLF_RE` | yes after class fixes | `^(?P<client>PNS+) (?P<ident>PNS+) (?P<user>PNS+) \[(?P<ts>[^\]]+)\] "(?P<request>[^"]*)" (?P<status>D{3}\|-) (?P<bytes>PNS+)(?: "(?P<referer>[^"]*)" "(?P<agent>[^"]*)")?(?: (?P<extra>.*))?$` |
| `VHOST_RE` | **no** (lookahead) | `^(?P<vhost>[W.\-]+)(?::D+)? ` then require `^PNS+ PNS+ PNS+ \[` to match `ln[end:]` (exact: the prefix match is unique — a shorter vhost/port would be followed by neither `:` nor space) |
| `ALB_RE` | yes after class fixes (`\"` is just `"`) | replace every `\S` by `PNS`, `\"` by `"` |
| `ENVOY_RE` | yes after class fixes | replace `\S`→`PNS`, `\d`→`D` (`[A-Z]` stays ASCII) |
| `EXTRA_KV` | yes | `(?P<k>[A-Za-z_][W.\-]*)=(?P<v>"[^"]*"\|PNS+)` |
| `UUIDISH` | yes, as is (ASCII hex) | unchanged |
| `\d+\.\d+`, `\d+` fullmatch | yes | `^D+\.D+$`, `^D+$` |

`ALB_RE` and `ENVOY_RE` are anchored only at the start (`.match`): trailing fields after the last group are ignored.
In `ENVOY_RE` the optional `(?:\S+ )?` is tried first and backtracks if the rest fails — Go leftmost-first gives the
same groups (probed: `503 UF 0 91 3 - "-" …` → rx `0`, tx `91`, dur `3`, up `-`).

### 2.2 Algorithm

```
for i, ln in enumerate(pySplitLines(text), 1):
    if blank(ln): continue
    vhost = ""
    if VHOST matches (§2.1 rewrite): vhost, ln = m.vhost, ln[m.end():]       # port dropped
    if m = CLF_RE.match(ln):        → CLF record, yield
    elif m = ALB_RE.match(ln):      → ALB record, yield
    elif m = ENVOY_RE.match(ln):    → Envoy record, yield
    (else no record)
```

`status_severity(st) = "ERROR" if st.startswith("5") else "WARN" if st.startswith("4") else "INFO"` (so `"-"` → INFO).

**CLF / combined** (key order as listed):

```
st = m.status
rec = {"timestamp": m.ts, "client": m.client, "request": m.request, "status": st, "bytes": m.bytes,
       "message": f"{m.request} {st}", "severity": status_severity(st), "service": vhost or "http"}
if m.user and m.user != "-": rec["user"] = m.user
if referer group participated (Python: m["referer"] is not None):  rec["referer"] = m.referer; rec["agent"] = m.agent
if m.extra (non-empty string): parse_extra(m.extra, rec)
```

`referer`/`agent` are kept even when `"-"` (an empty `""` is later dropped from attributes). `ident` is never stored.
Go: "participated" = submatch index `>= 0` (an empty-but-participating group `""` still sets both keys).

**AWS ALB**:

```
st = m.status if m.status != "-" else m.tstatus
rec = {"timestamp": m.ts, "client": m.client.split(":")[0], "target": m.target, "request": m.request, "status": st,
       "message": f"{m.request} {st}",
       "severity": "ERROR" if m.status == "-" or st.startswith("5") else status_severity(st),
       "service": m.domain if m.domain not in ("", "-") else m.elb,
       "host": m.target.split(":")[0] if m.target != "-" else "",
       "latency_s": m.t_tgt,            # string, e.g. "0.116", "-1"
       "agent": m.agent}
```

`split(":")[0]` = text before the first `:` (whole string if none).

**Envoy / Istio**:

```
st = m.status if m.status != "-" else "0"
rec = {"timestamp": m.ts, "request": m.request, "status": st, "message": f"{m.request} {st}",
       "severity": "ERROR" if st.startswith("5") or st == "0" else status_severity(st),
       "service": m.authority.split(":")[0] if m.authority not in ("", "-") else "envoy",
       "envoy.flags": m.flags, "agent": m.agent, "ts_is_start": True}
if m.dur != "-": rec["duration_ms"] = int(m.dur)
if m.up  != "-": rec["upstream_duration_ms"] = int(m.up)
if m.reqid not in ("", "-"):    rec["x-request-id"] = m.reqid
if m.upstream not in ("", "-"): rec["upstream_host"] = m.upstream
```

`int()` of `\d+`: arbitrary size, leading zeros dropped (`"007"` → `7`), Unicode digits by digit value. In JSON it is
an integer literal; keep a big-int/decimal-string for values beyond int64. (Python 3.11 raises `ValueError` for > 4300
digits — "raises", §0.3.) `ts_is_start` is the JSON literal `true` in attributes.

### 2.3 `parse_extra(extra, rec)` (mutates `rec`)

```
rest = []
for m in EXTRA_KV.finditer(extra):                 # 1. key=value pairs, in order
    rec[pyLower(m.k)] = m.v.strip('"')             #    may OVERWRITE status/message/severity/... in place
floats = 0
for tok in pySplit(EXTRA_KV.sub(" ", extra)):      # 2. what is left, split on Python whitespace runs
    t = tok.strip('"')
    if fullmatch(D+\.D+, t):
        rec["duration_s" if floats == 0 else "upstream_duration_s"] = pyFloat(t); floats += 1
    elif fullmatch(D+, t) and "duration_s" not in rec and "duration_us" not in rec:
        rec["duration_us"] = int(t)
    elif UUIDISH.match(t) and "request_id" not in rec:
        rec["request_id"] = t
    elif t not in ("-", ""):
        rest.append(tok)                           # original token, quotes kept
if rest: rec["extra"] = " ".join(rest)
```

Exact consequences (probed):

* The 3rd and later floats overwrite `upstream_duration_s` again (last wins).
* An integer token after `duration_s`/`duration_us` exists is not a duration; it can still become `request_id` if it is
  16–64 hex digits, otherwise goes to `extra`.
* Order matters: `007 0.000 …` gives `duration_us: 7` **and** `duration_s: 0.0` (the int came first).
* `key=value` pairs from `extra` overwrite earlier keys in place: `status=999` replaces `status` but `message` keeps the
  original `"… 200"`; a pair named `duration_s` blocks the int rule.
* `EXTRA_KV.sub(" ", …)` replaces each non-overlapping match by one space (Go `ReplaceAllString` has the same match
  sequence).
* Float values print via `pyFloatRepr` in canonical JSON (`0.117`, `30.001`, `0.0`).

### 2.4 Golden coverage (access)

| branch | inputs |
|---|---|
| CLF common, `user` set | `apache_access_clf.log`, `mut__apache_access_clf.log` |
| combined + bare float extra (`duration_s`) | `nginx_combined.log`, `mut__nginx_combined.log` |
| ALB (`status` `-` → `tstatus`, domain service) | `aws_alb_access.log`, `mut__aws_alb_access.log` |

**Not covered:** VHOST prefix, Envoy (no input), `extra` with `k=v` pairs, `duration_us`, `request_id`, second float,
`extra` remainder, ALB `target == "-"`, ALB domain `-` (→ elb), CLF status `-`.

---

## 3. `w3c_parser.py` — `W3cParser.records(text)`

No regex. State is local: `fields = []`, `software = ""`.

```
for i, ln in enumerate(pySplitLines(text), 1):
    if blank(ln): continue
    if ln.startswith("#"):                               # only a '#' in column 0
        if ln.startswith("#Fields:"):    fields = pySplit(ln[8:])          # Python whitespace split
        elif ln.startswith("#Software:"): software = pyStrip(ln[10:])
        continue
    if not fields: continue
    vals = ln.split(" ")                                  # SINGLE space: "a  b" → ["a", "", "b"]
    rec = {k: v for k, v in zip(fields, vals) if v != "-"}   # zip stops at the shorter; dup field: first position, last value
    if "date" in rec and "time" in rec:
        rec["timestamp"] = f"{rec.pop('date')} {rec.pop('time')}"     # both removed, timestamp appended
    elif "date-time" in rec:
        rec["timestamp"] = rec.pop("date-time")
    st = rec.get("sc-status") or rec.get("status") or ""
    method = rec.get("cs-method", "")
    uri = rec.get("cs-uri-stem", rec.get("cs-uri", ""))      # cs-uri only if cs-uri-stem absent (e.g. it was "-")
    rec["message"] = " ".join(x for x in (method, uri, st) if x) or ln
    if st: rec["severity"] = status_severity(st)
    if "s-ip" in rec or "s-computername" in rec:
        rec["host"] = rec.get("s-computername") or rec["s-ip"]   # raises KeyError if s-computername == "" and no s-ip
    rec["service"] = rec.get("s-sitename") or ("iis" if "Internet Information Services" in software else "w3c")
    yield i, rec
```

* A later `#Fields:` replaces the column list mid-file; `software` persists.
* Values are never stripped; `""` values (double spaces) are kept in the record and dropped later as attributes.
* `line_no` counts the `#` lines (first data row of `iis_w3c.log` is line 5).
* "raises": a row whose `s-computername` value is `""` with no `s-ip` (e.g. a leading space) → `KeyError`.
* Keys that can be overwritten by the assignments above if they are also W3C field names: `message`, `severity`,
  `host`, `service`, `timestamp` (assignment in place).
* Timestamp text `2026-09-22 06:14:02` (naive → `+00:00`). If only `time` exists, no `timestamp` key is made and
  `time` stays a field (auto_map may pick it; a bare time parses to Jan 1 of the current year, 02 §3).

Golden: `iis_w3c.log` (whole and live, 3 rows, `date`+`time`, `s-ip` host, `iis` service). `mut__iis_w3c.log` is
**not** W3C (its `#Fields` header is lost in mutation; detected as `sap`). **Not covered:** `date-time`,
`s-computername`, `s-sitename`, `cs-uri`, `status`, second `#Fields`, the `w3c` service fallback, rows without status.

---

## 4. `kv_parser.py` — `KvParser.records(text)`

### 4.1 Regexes

```python
KV_RE = re.compile(r'([\w.\-@]+)=("(?:[^"\\]|\\.)*"|\S+)')          # format_detector.py
# from text_parser.py:
MON = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
DOW = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
TS_RE = re.compile(r"^\[?("
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z| ?[+\-]\d{2}(?::?\d{2})?| UTC)?"
    r"|\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
    r"|\d{2}[./-]\d{2}[./-]\d{4}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?: ?[AP]M)?"
    r"|\d{1,2}-" + MON.upper() + r"-\d{4} \d{2}:\d{2}:\d{2}"
    r"|\d{1,2}-" + MON + r"-\d{4} \d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
    r"|\d{2} " + MON + r" \d{4} \d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"|" + MON + r" \d{1,2} \d{4} \d{2}:\d{2}:\d{2}"
    r"|" + DOW + r" " + MON + r" +\d{1,2} \d{2}:\d{2}:\d{2} \d{4}"
    r"|" + DOW + r", \d{1,2} " + MON + r" \d{4} \d{2}:\d{2}:\d{2}(?: [+\-]\d{4})?"
    r"|" + MON + r" \d{1,2}, \d{4} \d{1,2}:\d{2}:\d{2} (?:AM|PM)"
    r"|\d{8}T\d{6}(?:\.\d+)?Z?"
    r"|\d{10}(?:\.\d{1,6})?(?=\s)"
    r"|\d{13}(?=\s)"
    r")\]?:?\s*")
LEVEL_RE = re.compile(r"^\[?(TRACE|DEBUG|INFO|INFORMATION|NOTICE|NOTE|SYSTEM|WARN(?:ING)?|ERR(?:OR)?|CRIT(?:ICAL)?|FATAL|SEVERE|EMERG(?:ENCY)?|ALERT|PANIC|CONFIG|FINE(?:R|ST)?|INF|WRN|FTL|DBG|VRB)\]?[:\s]+", re.I)
LOGGER_RE = re.compile(r"^\[?([A-Za-z][\w.\-]{2,})\]?:\s+")
```

(`MON.upper()` uppercases the month names only; `(?:` is unchanged.)

| regex | RE2 as is? | Go rewrite |
|---|---|---|
| `KV_RE` | yes after class fixes | `([W.\-@]+)=("(?:[^"\\]\|\\.)*"\|PNS+)` |
| `TS_RE` | **no** (`(?=\s)` in the two epoch alternatives) | see below |
| `LEVEL_RE` | **no as is** (`re.I` + ı/İ, `\s`) | `(?i)` + every `I`/`i` written `[Ii\x{130}\x{131}]`; `[:\s]` → `[:pyWS]` |
| `LOGGER_RE` | yes after class fixes | `^\[?([A-Za-z][W.\-]{2,})\]?:PS+` |

`TS_RE` rewrite (exact, because the suffix `\]?:?\s*` can always match empty, the first alternative that matches group 1
wins):

1. `TS_A` = the same regex with the two epoch alternatives removed, `\d`→`D`, final `\s*`→`PS*`. If it matches, use it.
2. Else `TS_E10 = ^\[?(D{10}(?:\.D{1,6})?)` (greedy) and require the next rune to be Python whitespace; on success the
   match end is after `\]?:?PS*` applied at that point, i.e. after the following whitespace run.
3. Else `TS_E13 = ^\[?(D{13})` with the same whitespace requirement.

(Greedy is exact for step 2: if the longest fraction is not followed by whitespace, a shorter fraction or no fraction is
followed by a digit or `.`, which also fails.) `TS_RE` itself belongs to the text slice; kv needs only `m[1]` and
`m.end()`.

`LEVEL_RE` under `re.I`: Python case-folds `ſ`→`s`, `K`→`k`, `ı`/`İ`→`i`; Go `(?i)` handles `ſ`, `K` but not `ı`/`İ`
(01 §1.5). Note `[:\s]+` is greedy and consumes all following colons/whitespace.

### 4.2 Algorithm

```
for i, ln in enumerate(pySplitLines(text), 1):          # NOTE: no blank-line skip (a blank line yields nothing anyway)
    rec = {}; rest = ln
    if m = TS_RE.match(rest):     rec["timestamp"], rest = m[1], rest[m.end():]
    if m = LEVEL_RE.match(rest):  rec["severity"],  rest = m[1], rest[m.end():]    # level text as written, e.g. "info"
    if (m = LOGGER_RE.match(rest)) and "=" not in m[1]:                            # "=" test is always true
                                  rec["service"],   rest = m[1], rest[m.end():]
    pairs = KV_RE.findall(rest)                                                    # [(k, v), ...]
    if len(pairs) >= 2 or (pairs and rec):
        for k, v in pairs (in order; dict comprehension → dup key keeps first position, last value; then rec.update):
            val = v[1:-1].replace('\\"', '"') if v.startswith('"') else v.rstrip(",;")
            rec[k] = val           # overwrites timestamp/severity/service in place if a pair has that key
        if "message" not in rec and "msg" not in rec and "type" in rec and ("clientId" in rec or "client_id" in rec):
            rec["message"] = f"{rec['type']} client={rec.get('clientId') or rec.get('client_id')}"
        yield i, rec
```

Exact value rules:

* Quoted value: strip **exactly one** char at each end, then replace `\"` by `"`; other escapes (`\\`, `\n`) stay as
  written.
* Unquoted value: strip trailing `,` and `;` characters (any number): `type=LOGIN,` → `LOGIN` (Keycloak).
* Unterminated quote: the quoted alternative fails, `\S+` matches a token starting with `"`; it still "starts with
  `"`" so `[1:-1]` cuts the first and the **last** character: `msg="hello world` → `hell` (probed). Copy this.
* A single pair with no prefix fields is not a record (`only=1` → dropped). A line with ≥1 pair after a parsed
  timestamp/level/service is a record.
* Keycloak message: `rec.get('clientId') or rec.get('client_id')` — empty `clientId` falls back to `client_id`; if both
  are falsy/missing the text is `client=None` (Python `str(None)`), or `client=` + the value of a present empty one.
  Values here are always strings.
* Values are always `str`. `rec` key order: prefix fields first, then pairs in first-seen order.

Golden: `logfmt.log`, `mut__logfmt.log` (`ts=` lines: TS_RE/LEVEL/LOGGER prefixes do **not** match), `fortigate_kv.log`,
`mut__fortigate_kv.log` (quoted values; `date` + `time` stay separate keys — the role mapper picks `time`, which parses
as `2026-01-01T09:14:05+00:00`; that is 02's behaviour, not kv's). **Not covered:** timestamp/level/logger prefix,
unterminated quote, `,;` stripping, Keycloak message, single-pair-with-prefix lines.

---

## 5. `cef_parser.py` — `CefParser.records(text)` (CEF and LEEF)

### 5.1 Regexes

```python
CEF_HEAD = re.compile(r"^(?P<prefix>.*?)CEF:(?P<ver>\d+)\|")
LEEF_HEAD = re.compile(r"^(?P<prefix>.*?)LEEF:(?P<ver>\d+(?:\.\d+)?)\|")
SYSLOG_PREFIX = re.compile(r"^(?:<\d+>)?(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2}T[\d:.+\-Z]+)\s+(?P<host>\S+)\s*$")
CEF_KV = re.compile(r"(\w+)=((?:\\.|[^\\=])*?)(?=\s+\w+=|$)")
CEF_SEV = lambda v: "CRITICAL" if v >= 9 else "ERROR" if v >= 7 else "WARN" if v >= 4 else "INFO"
```

| regex | RE2 as is? | Go |
|---|---|---|
| `CEF_HEAD` | yes | `^(?P<prefix>.*?)CEF:(?P<ver>D+)\|` (lazy prefix = first occurrence) |
| `LEEF_HEAD` | yes | `^(?P<prefix>.*?)LEEF:(?P<ver>D+(?:\.D+)?)\|` |
| `SYSLOG_PREFIX` | yes after class fixes | `^(?:<D+>)?(?P<ts>[A-Z][a-z]{2}PS+D{1,2}PSD{2}:D{2}:D{2}\|D{4}-D{2}-D{2}T[D:.+\-Z]+)PS+(?P<host>PNS+)PS*$` |
| `CEF_KV` | **no** (lookahead) | hand-written scanner, below |

`CEF_KV.findall(ext)` exact Go equivalent (`isW` = Python `\w`, `isWS` = Python whitespace):

```go
// lookahead (?=\s+\w+=|$) at position j
func kvBoundary(s []rune, j int) bool {
    if j == len(s) { return true }
    if !isWS(s[j]) { return false }
    q := j; for q < len(s) && isWS(s[q]) { q++ }
    r := q; for r < len(s) && isW(s[r]) { r++ }
    return r > q && r < len(s) && s[r] == '='            // maximal \w run directly followed by '='
}
func cefKV(s []rune) (out [][2]string) {
    for k := 0; k < len(s); {
        // try a match starting at k
        e := k; for e < len(s) && isW(s[e]) { e++ }       // (\w+) greedy; a shorter key is never followed by '='
        if e > k && e < len(s) && s[e] == '=' {
            j := e + 1; ok := false
            for {
                if kvBoundary(s, j) { ok = true; break }   // lazy: stop at the first boundary
                if j < len(s) && s[j] == '\\' && j+1 < len(s) && s[j+1] != '\n' { j += 2; continue }   // \\.
                if j < len(s) && s[j] != '\\' && s[j] != '=' { j++; continue }                         // [^\\=]
                break                                       // '=' or a lone trailing '\' : no match at k
            }
            if ok { out = append(out, [2]string{string(s[k:e]), string(s[e+1:j])}); k = j; continue }
        }
        k++                                                 // findall: retry one position later
    }
    return
}
```

Consequences (probed): a key may start inside a word (`xk=v` → `xk`; after a failed `abc=foo=bar` the scan finds
`foo=bar`); a value cannot contain an unescaped `=` (the whole `abc=` pair is skipped); a value ending in a lone `\`
at end of line makes that pair vanish (`baz=1 tail\` → no `baz`); values may contain spaces
(`cs1=Web Application Attack`); empty values are possible (`end=` → `""`).

### 5.2 `_split_header(s, n)` (verbatim semantics)

```
out, cur, i = [], [], 0
while i < len(s) and len(out) < n:
    c = s[i]
    if c == "\\" and i + 1 < len(s): cur.append(s[i+1]); i += 2; continue     # ANY escaped char is unescaped
    if c == "|": out.append("".join(cur)); cur = []; i += 1; continue
    cur.append(c); i += 1
out.append(s[i:] if len(out) == n else "".join(cur))    # rest is RAW (no unescaping) when n fields were found
return out
```

Indexes are code points. A trailing lone `\` is kept literally. Returns `n+1` elements only if `n` pipes were found;
otherwise fewer, and the tuple unpacking in `records` **raises** `ValueError` (§0.3; e.g. `CEF:0|a|b`).

### 5.3 Algorithm

```
for i, ln in enumerate(pySplitLines(text), 1):
    if blank(ln): continue
    if m = CEF_HEAD.match(ln):
        vendor, product, version, sig, name, sev, ext = _split_header(ln[m.end():], 6)
        rec = {"vendor": vendor, "product": product, "version": version, "signature": sig,
               "message": name, "service": pyStrip(product) or vendor}
        if pyIsDigit(pyStrip(sev)):  rec["severity"] = CEF_SEV(int(sev))      # int() tolerates surrounding whitespace
        elif pyStrip(sev):           rec["severity"] = pyStrip(sev)           # e.g. "High" (mapped later by 02)
        pairs = {k: pyStrip(v.replace("\\=", "=").replace("\\n", " ")) for k, v in CEF_KV.findall(ext)}
    elif m = LEEF_HEAD.match(ln):
        vendor, product, version, name, rest = _split_header(ln[m.end():], 4)
        rec = {"vendor": vendor, "product": product, "version": version, "message": name,
               "service": pyStrip(product) or vendor}
        delim = "\t"
        if (not rest.startswith("|")) and len(rest) > 1 and rest[0] != "\t" and "|" in rest[:8] and rest[0] != " ":
            delim, rest = rest.split("|", 1)[0], rest.split("|", 1)[1]        # LEEF 2.0 delimiter field, taken literally
        elif rest.startswith("|"):
            rest = rest[1:]
        pairs = {}
        for part in rest.split(delim):                  # plain substring split (delim may be multi-char, e.g. "x09")
            if "=" in part: k, v = part.split("=", 1); pairs[pyStrip(k)] = pyStrip(v)
        if "sev" in pairs and pyIsDigit(pairs["sev"]): rec["severity"] = CEF_SEV(int(pairs["sev"]))
    else:
        continue
    if m.prefix and (sp = SYSLOG_PREFIX.match(pyStrip(m.prefix))):
        rec.setdefault("host", sp.host); rec.setdefault("syslog_ts", sp.ts)
    ts = pairs.get("rt") or pairs.get("devTime") or pairs.get("end") or pairs.get("start") or rec.get("syslog_ts")
    if ts: rec["timestamp"] = ts
    host = pairs.get("dvchost") or pairs.get("identHostName") or pairs.get("dvc") or pairs.get("devname") or rec.get("host")
    if host: rec["host"] = host
    if pairs.get("msg"): rec["message"] = f"{rec['message']}: {pairs['msg']}"
    rec.update({k: v for k, v in pairs.items() if k not in rec})        # pairs never overwrite parser keys
    yield i, rec
```

Details:

* CEF is tested first; a line containing both uses CEF. The `m` used for `prefix` is the header that matched.
* `service = pyStrip(product) or vendor` — the vendor is **not** stripped.
* `CEF_SEV(int(x))`: `>=9` CRITICAL, `>=7` ERROR, `>=4` WARN, else INFO. `pyIsDigit` is true for some non-decimal
  digits (superscripts) on which `int()` raises `ValueError` ("raises"). Go: if all runes are `\p{Nd}` convert by
  digit value; if `pyIsDigit` but not all `\p{Nd}`, return an error.
* CEF value post-processing order: `\=` → `=`, then literal `\n` (backslash + n) → one space, then `pyStrip`. Other
  escapes stay (`suser=CORP\\ayse` keeps two backslashes, as in `cef_arcsight.log`).
* `pairs` is a dict: duplicate keys keep the first position and the last value.
* `ts` / `host` use Python `or`: an empty string is skipped.
* `msg` concatenation uses the header name (`"Malware Detected: invoice.exe quarantined"`); the `msg` pair is also kept
  as an attribute. Pair keys equal to existing record keys (`message`, `severity`, `host` when set, `timestamp` when set,
  `vendor`, …) are dropped; a `host` pair **is** added when no host was set before (probed).
* Key order: header keys, `[severity]`, `[host, syslog_ts]` (prefix), `[timestamp]`, `[host]` (appended here if not
  set by the prefix), pair keys not yet present (pair order).
* LEEF 2.0 test is evaluated left to right with short-circuit (`len(rest) > 1` guards `rest[0]`). Quirk to copy: a LEEF
  1.0 attribute list with a `|` in its first 8 chars is mis-read as a 2.0 delimiter (`cat=a|b\tsev=x` → delimiter
  `cat=a`, one pair `"b\tsev": "x"`).
* Timestamps are raw strings (`Sep 22 2026 09:14:02` from `rt`/`devTime`; or the syslog prefix ts); see 02 §3.

Golden: `cef_arcsight.log`, `mut__cef_arcsight.log` (CEF 0, numeric sev 1/3/4/5/9, `rt`, `dvchost`, escaped `\\`);
`leef_qradar.log`, `mut__leef_qradar.log` (LEEF 2.0 with `^` delimiter, LEEF 1.0 tab, `sev`, `identHostName`,
`devTime`, `msg`). **Not covered:** syslog prefix, `syslog_ts`, `end`/`start`/`dvc`/`devname`, non-digit CEF severity,
escaped `|` or `=` in headers/values, `\n` escapes, `|`-prefixed LEEF rest, lone-backslash values.

---

## 6. `winevt_parser.py` — `WinEvtParser.records(text)` (not line-oriented)

### 6.1 Regex

```python
EVENT_RE = re.compile(r"<Event\b.*?</Event>", re.S)
LEVEL = {"1": "CRITICAL", "2": "ERROR", "3": "WARN", "4": "INFO", "5": "DEBUG", "0": "INFO"}
```

RE2 can compile `(?s)<Event\b.*?</Event>` but Go `\b` is ASCII-only. Exact Go: scan for `"<Event"`; accept the
occurrence only if the following rune is absent (end of text) or not Python `\w`; then find the first `"</Event>"` at or
after the end of `"<Event"`; the chunk is `[start, end_of_</Event>)`; continue scanning after the chunk. If an occurrence
is rejected or has no closing tag, continue scanning at start+1. (`<Event</Event>` is a valid chunk in Python; keep it.)

### 6.2 Algorithm

```
for i, chunk in enumerate(all chunks, 1):          # i counts chunks, including those that fail to parse
    try: ev = ElementTree.fromstring(chunk)
    except ParseError: continue
    rec = {}; fields = {}
    for node in ev.iter():                          # document order, root included
        tag = node.tag.rsplit("}", 1)[-1]           # local name ("{ns}EventID" → "EventID")
        txt = pyStrip(node.text or "")              # text before the first child element
        if tag == "Provider":       rec["provider"] = node.get("Name", "")
        elif tag == "TimeCreated":  rec["timestamp"] = node.get("SystemTime", "")
        elif tag in ("EventID","Level","Task","Channel","Computer","Keywords","Opcode") and txt:
            rec[{"EventID": "event_id", "Level": "level", "Computer": "host"}.get(tag, tag.lower())] = txt
        elif tag == "Data":
            fields[node.get("Name") or f"data{len(fields) + 1}"] = txt
    rec["severity"] = LEVEL.get(rec.get("level", ""), "INFO")
    rec["service"]  = rec.get("provider") or rec.get("channel") or "windows"
    rec["message"]  = " ".join(x for x in (f"EventID {rec.get('event_id', '')}", rec.get("provider", ""),
                                           *(f"{k}={v}" for k, v in fields.items())) if x)
    rec.update({k: v for k, v in fields.items() if k not in rec})
    yield i, rec
```

* Keys: `event_id`, `level`, `task`, `channel`, `host`, `keywords`, `opcode`. Later elements of the same tag overwrite
  in place. `Data` without/with empty `Name` gets `data<N>` where N = number of fields so far + 1; a `Data` element with
  empty text still creates a field `""`. `Data` anywhere in the tree counts (EventData or UserData).
* `"EventID "` (with trailing space) is always non-empty, so a missing event id gives `"EventID  Provider…"` (two
  spaces). Field values in the message are the stripped texts.
* Data fields whose names equal record keys (`host`, `provider`, `message`, …) appear only in the message.
* `timestamp` is `SystemTime` verbatim (7 fractional digits, `2026-09-22T06:14:02.0000000Z` → parsed by 02 §3 to
  `…06:14:02+00:00`); missing attribute gives `""` (no time).
* `raw` = `json.dumps(rec)` in insertion order (golden `windows_event_xml.log`).

XML parsing must reproduce expat via `xml.etree.ElementTree.fromstring`:

* namespaces → `{uri}local` tags (only the local part is used); attributes looked up by plain name (`Name`,
  `SystemTime`) — an attribute with a namespace prefix would not match;
* entity and character references decoded; only the 5 predefined entities exist (any other → ParseError → chunk
  skipped); CDATA content is text; comments and processing instructions are dropped and the text around them joins
  into `.text`;
* `\r\n` and lone `\r` in text become `\n` (Go `encoding/xml` does the same); attribute values are normalized by expat
  (tab/CR/LF → space) — Go `encoding/xml` does **not** do this; normalize attribute values yourself;
* malformed XML (mismatched tags, duplicate attributes, invalid characters, unbound namespace prefix, text after root)
  → ParseError → skip. Go must use a strict decoder and additionally reject duplicate attributes and undeclared
  prefixes to match.

Golden: `windows_event_xml.log`, `mut__windows_event_xml.log` (one event per line, namespaced, `Level` 0/2/4,
`Data Name=`). **Not covered:** unnamed `Data`, missing `Provider`/`EventID`, ParseError chunks, pretty-printed
multi-line events, entity/CDATA/comment handling, Data names colliding with record keys. In live mode a multi-line
event split across 40-line batches produces no chunk in either batch.

---

## 7. `csv_parser.py` — `CsvParser.records(text)`

```
lines = [ln for ln in pySplitLines(text) if not blank(ln)]
d = detect_delimiter(lines) or ","                                   # 01 §8.2 (first 30 lines, \t , ; |)
for i, row in enumerate(csv.DictReader(io.StringIO(text), delimiter=d), start=2):
    yield i, {pyStrip(k): v for k, v in row.items() if k is not None}
```

`line_no` is **row index + 1**, not the physical line. Blank lines (skipped by DictReader) and multi-line quoted fields
shift it, and `raw = pySplitLines(text)[line_no-1]` then shows a different physical line (copy this). Probed:
`a,b / 1 / 1,2,3 / (blank) / 4,5` → rows at 2, 3, 4 (the last is physically line 5).

### 7.1 `csv.DictReader` semantics to reproduce

* Input iteration: `io.StringIO(text)` yields lines split on `\n` **only** (no newline translation); the csv state
  machine treats `\r`, `\n`, `\r\n` as record ends.
* Header: the **first row returned by the reader**, even an empty one. A text starting with a blank line gives
  `fieldnames == []` and every record is `{}` (probed: `(2, {}), (3, {})`).
* Each later row: rows equal to `[]` (blank lines) are skipped without consuming an index. A whitespace-only line is
  a row `[" "]`, not skipped.
* `dict(zip(fieldnames, row))`: duplicate header names → first position, last value. Extra values go to key `None`
  (`restkey`) and are dropped by the parser. Missing values → `None` (`restval`).
* Keys are then `pyStrip`ped; two headers that strip to the same name collapse (first position, last value). Values are
  never stripped.
* Dialect `excel` + delimiter `d`: `quotechar='"'`, `doublequote=True`, `skipinitialspace=False`, `strict=False`,
  `escapechar=None`, `field_size_limit=131072`. State machine (CPython `_csv.c`):
  * a field starting with `"` is quoted; inside it, `""` is a literal `"`, delimiters and newlines are literal;
  * after the closing quote, any non-delimiter, non-newline character is appended to the field and the field continues
    unquoted (`"q""r"s` → `q"rs`);
  * a `"` inside an unquoted field is literal;
  * end of input inside a quoted field: Python 3.11 with `strict=False` ends the field and the record (no error);
  * **raises** `csv.Error`: a lone `\r` followed by more characters on the same `\n`-line outside quotes ("new-line
    character seen in unquoted field"), or a field longer than 131072 chars.
  * NUL characters are accepted in 3.11.
  Go's `encoding/csv` differs (quotes in unquoted fields, `LazyQuotes`, `\r` handling, trimming): write this state
  machine.
* Values are `str` or `None`.

Pipeline fix-up (01 §7): if the format is `csv` and `{"EventID","Correlation","Area"} ⊆ {pyStrip(k) for k in keys}`,
`_sharepoint_uls(rows)` rewrites severity/service/attributes afterwards. No golden input triggers it.

Golden: `csv_events.csv`, `mut__csv_events.csv`, `demo__alerts.csv`, `storm__alarms.csv` (comma, no quotes).
Live: every batch after the first has no header line, so its first data row becomes the header (39 rows per 40-line
batch, keys are values of that row) — present in `mut__csv_events.csv.live` and `storm__alarms.csv.live`.
**Not covered:** other delimiters, quoting, multi-line fields, short/long rows, duplicate or padded headers, blank lines.

---

## 8. `json_parser.py`

### 8.1 `JsonParser.records(text)` (not line-oriented)

```
obj = json.loads(text)                                 # raises on invalid JSON (detect_format checked it, 01 §8.3)
if isinstance(obj, dict) and "resourceLogs" in obj: rows = otel_records(obj)
else: rows = find_records(obj) or ([obj] if isinstance(obj, dict) else [])
for i, r in enumerate(rows, 1):
    if isinstance(r, dict): yield i, enrich(flatten(r))   # i counts non-dict elements too
```

Python `json.loads` facts Go must copy (order-preserving decoder required):

* objects keep key order; duplicate keys → first position, last value (probed);
* integer literals → `int` of unbounded size (Python 3.11 limit: > 4300 digits raises `ValueError`); literals with
  `.`/`e`/`E` → `float` (`1e400` → `inf`); `NaN`, `Infinity`, `-Infinity` accepted;
* strings: standard escapes; lone surrogate escapes (`"\ud800"`) are accepted and kept as a lone surrogate code point;
* leading/trailing JSON whitespace (` \t\n\r` only) allowed; anything else after the value raises.
* A text starting with U+FEFF raises (`Unexpected UTF-8 BOM`); the loader strips BOMs before (01 §3).

`find_records(obj, depth=0)`:

```
if list: return obj if obj and isinstance(obj[0], dict) else None      # only the FIRST element is checked
if dict and depth < 4:
    for v in obj.values():                                              # in key order
        found = find_records(v, depth + 1)
        if found: return found                                          # non-empty list
return None
```

Lists inside lists are not searched. A list is found at most 4 dict levels deep (`{"a":{"b":{"c":{"d":[{…}]}}}}` is
found, one level deeper is not — probed). `[1, {"a":1}]` → `None` → `[]` (top-level list) → no records. A dict without a
list of dicts is itself the single record (`baseline.json` → 1 record with dotted keys).

### 8.2 `otel_records(obj)`

```
rows = []
for rl in obj.get("resourceLogs", []):                       # null → TypeError ("raises")
    res = _otel_attrs(rl.get("resource", {}).get("attributes"))
    for sl in rl.get("scopeLogs", []) or rl.get("instrumentationLibraryLogs", []):   # empty/missing scopeLogs → legacy key
        for lr in sl.get("logRecords", []):
            body = lr.get("body", {})
            rows.append({**res,
                "timestamp": lr.get("timeUnixNano") or lr.get("observedTimeUnixNano"),
                "severity":  lr.get("severityText") or lr.get("severityNumber"),
                "message":   next(iter(body.values()), "") if isinstance(body, dict) else body,
                **_otel_attrs(lr.get("attributes"))})
return rows

_otel_attrs(items): out = {}
    for a in items or []:
        v = a.get("value", {})
        out[a.get("key", "")] = next(iter(v.values()), None) if isinstance(v, dict) else v
    return out
```

* Key order: resource attribute keys, then `timestamp`, `severity`, `message` (in place if a resource attribute had
  that name), then log-record attribute keys (overwriting in place on collision).
* `or` is Python truthiness: `a or b` returns `a` if truthy, else `b` (the last operand, even if falsy). So
  `timeUnixNano: "0"` is kept as `"0"`; `severityText: ""` with `severityNumber: 0` gives `0` (int); both missing gives
  `None` (probed: `{'timestamp': '0', 'severity': 0, 'message': ''}`).
* The value of an attribute is the **first** value of the OTLP value object (`{"stringValue": "x"}` → `"x"`,
  `{"intValue": "42"}` → `"42"` string as sent, `{"doubleValue": 1.5}` → `1.5`, `{}` → `None`,
  `{"kvlistValue": {...}}` → a dict that `flatten` later expands, `{"arrayValue": {"values": [...]}}` → dict →
  `k.values` list).
* `timeUnixNano` stays a string (`"1790068503500000000"`) and is parsed by 02 §3 (epoch-ns digits →
  `2026-09-22T09:15:03.500000+00:00`).
* Then each row goes through `enrich(flatten(row))` like any other.

Golden: `otel_logs.json` (scopeLogs, string attrs, severityText).

### 8.3 `enrich(rec)` (also used by jsonl)

Specified in 02 §11.1. Summary for completeness:

```
if any(k in rec for k in ("severity","level","log.level","severityText","PRIORITY","s")): return rec
txt = first rec[k] for k in ("log","message","msg","short_message") if isinstance(rec.get(k), str), else None
if not txt: return rec                                  # "" also returns
head, rest = parse_head(txt.rstrip("\n"))               # only "\n" stripped; parse_head = text slice
if head.get("severity"):
    rec["severity"] = head["severity"]
    if head.get("service") and not any(k in rec for k in ROLE_HINTS["service"]): rec["service"] = head["service"]
    if rest: rec["message"] = rest                      # may ADD "message" when txt came from "log"
return rec
```

`parse_head` itself has no global state (it does not touch `FAMILY_SEEN`).

### 8.4 Golden coverage (json)

`alertmanager.json` (top-level list), `aws_cloudtrail.json` (`Records` list under a dict), `baseline.json` (dict fallback,
float values `100.0`), `otel_logs.json` (OTLP), `storm__alarms.json` (top-level list of 3000, int severities). Live:
`storm__alarms.json` and `baseline.json` are multi-line documents → their 40-line batches are not JSON and are detected
as `text` (01); single-line documents are one batch. **Not covered:** deep nesting search, non-dict list elements,
`instrumentationLibraryLogs`, OTLP non-string values, body as non-dict, `NaN`/big ints, duplicate keys.

---

## 9. `jsonl_parser.py` — `JsonlParser.records(text)`

```
for i, ln in enumerate(pySplitLines(text), 1):
    ln = pyStrip(ln)
    if not ln: continue
    try: r = json.loads(ln)
    except json.JSONDecodeError: continue                 # bad lines are skipped silently
    if isinstance(r, dict): yield i, enrich(flatten(r))   # arrays / scalars skipped
```

* `line_no` = physical line (1-based); `raw` = the **unstripped** physical line.
* `json.loads` semantics as in §8.1 (Python whitespace was already stripped; JSON itself only allows ` \t\n\r` around
  the value, so a line with e.g. a trailing U+00A0 inside is invalid → skipped; but `pyStrip` removed outer U+00A0).
* Errors that are not `JSONDecodeError` (int > 4300 digits → `ValueError`; nesting depth → `RecursionError`) are
  **not** caught → "raises".
* Probed: `{"a": NaN, "b": 1e400, "c": 10000000000000000000000}` → `nan`, `inf`, big int (canonical JSON `NaN`,
  `Infinity`, `10000000000000000000000`).

Golden: `demo__app.jsonl`, `docker_jsonfile.jsonl` (enrich from `log`), `ecs_json.jsonl` (`log.level`, nested →
dotted), `gelf.jsonl` (`short_message`), `journald.jsonl` (`PRIORITY`), `mongodb.jsonl` (`s`), `sim__simulate_batch.jsonl`,
`sim__simulate_flows.jsonl`, `sim__simulate_traces.jsonl` and the `mut__` variants. **Not covered:** invalid lines,
non-dict lines, NaN/Infinity, big ints, list values.

---

## 10. Timestamp strings produced by this slice → `parse_timestamp`

Parsers return timestamps as raw text; `records_to_observations` calls `parse_timestamp` (02 §3). Probed results
(current year 2026; naive values get `+00:00`; offsets are kept, never converted):

| parser | example text | result |
|---|---|---|
| syslog BSD | `Sep 22 09:14:02` | `2026-09-22T09:14:02+00:00` (current year) |
| syslog Cisco | `Sep 22 2026 09:15:10` | `2026-09-22T09:15:10+00:00` |
| syslog 5424 | `2026-09-22T09:15:03.500+03:00` | `2026-09-22T09:15:03.500000+03:00` |
| PAN | `2026/09/22 09:14:02` | `2026-09-22T09:14:02+00:00` |
| CLF | `22/Sep/2026:09:14:02 +0300` | `2026-09-22T09:14:02+03:00` |
| ALB | `2026-09-22T06:14:02.003412Z` | `2026-09-22T06:14:02.003412+00:00` |
| W3C | `2026-09-22 06:14:02` | `2026-09-22T06:14:02+00:00` |
| CEF/LEEF | `Sep 22 2026 09:14:02` | `2026-09-22T09:14:02+00:00` |
| winevt | `2026-09-22T06:14:02.0000000Z` | `2026-09-22T06:14:02+00:00` |
| OTLP | `1790068503500000000` | `2026-09-22T09:15:03.500000+00:00` |
| kv (fortigate `time`) | `09:14:05` | `2026-01-01T09:14:05+00:00` |

Rows whose value does not parse carry the previous row's time (or the dataset minimum, 01 §10.2).

---

## 11. Global / stateful behaviour and pipeline fix-ups touching these formats

* **None inside the parsers.** All regexes are module constants; W3C header state is per call.
* **Profiles** (01 "Profiles"): for `json`, `jsonl`, `csv`, `kv` the report keys are looked up/remembered in
  `profiles.json` under `WATCHOVER_HOME`; `parse_golden.py` runs all files in one process, so later reports carry
  learned `profile` ids. Measured: does not change any golden observation; Go keeps an in-memory store in the same file
  order.
* **Scenario constants** (02 §13): `scenario.MAPPING` (used as `mapping` when the caller passes none) and
  `scenario.DEDUP_KEY` / `DEDUP_PREFER` (dedupe after parsing, 01 §10.1) — the latter dedupes e.g. `storm__alarms.json`
  vs `storm__alarms.csv` only when both are in one ingest call (never in the goldens: one file per call).
* **`normalize` caches** (`_ts_cache`, `_format_cache`, `_sev_cache`): memoization only; see 02 for the one
  order-dependent case.
* **Pipeline fix-ups**: `_sharepoint_uls` for csv (§7); `fill_missing_timestamps` (`_no_ts`) and the final stable sort
  by timestamp (01 §10); `json` vs `jsonl` hint promotion in `parse_golden.py` live runs (a one-line `{…}` chunk
  detected as `json` is hinted as `jsonl` for later batches).
* **Live goldens** (`<input>.live.jsonl.gz`): text split on `b"\n"` into 40-line batches; `line_no` restarts at 1 per
  batch; formats here are fixed by the hint after batch 1 (refreshed every 200 batches). Format-specific effects: CSV
  header only in batch 1 (§7), W3C `#Fields` only in batch 1, multi-line JSON/XML documents broken.

---

## 12. Exceptions that escape (Go must return an error, not skip)

| parser | input | Python error |
|---|---|---|
| cef | CEF header with < 6 pipes after `CEF:n|`, LEEF with < 4 | `ValueError` (unpack) |
| cef | `sev` / LEEF `sev` that `isdigit()` but is not decimal (`²`) | `ValueError` (`int`) |
| w3c | `s-computername` present and `""`, no `s-ip` | `KeyError` |
| csv | lone `\r` mid-line outside quotes; field > 131072 chars | `csv.Error` |
| json | invalid JSON; `resourceLogs: null`; non-dict `resource`/scope/log record in OTLP | `JSONDecodeError` / `AttributeError` / `TypeError` |
| json, jsonl | integer literal > 4300 digits; extreme nesting | `ValueError` / `RecursionError` |
| access | `\d+` duration > 4300 digits | `ValueError` |

None are reached by any golden input.
