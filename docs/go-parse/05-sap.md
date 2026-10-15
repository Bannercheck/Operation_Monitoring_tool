# Go parse port, part 05: SAP log family (`parsers/sap_parser.py`)

Scope: everything in `src/watchover/parsers/sap_parser.py` (constants, every regex, `_iso`, `_sev_from_words`,
`sap_score`, `SapParser.records`, `SapParser._finish_nwj`, `_svc_from_logger`) and how `format_detector.detect_format`
routes text to it. What happens to the records afterwards (`records_to_observations`, `auto_map`, `parse_timestamp`,
`column_severity`, environment inference, dedupe, timestamp fill, sort) belongs to slices 01 and 02 and is only
summarised here where SAP output depends on it.

Helpers used below without redefinition come from 01 §1: `pyWS`, `PS` (Python `\s`), `PNS` (Python `\S`),
`pyStrip` / `pyRStrip`, `pySplitLines`, `pyLower`, code-point slicing, the Python `round()` helper (01 §1.6), and the
ordered-map rules of 04 §0.1 (insertion order observable; update of an existing key keeps its position; `pop` removes;
`setdefault` appends only when absent).

Verification done while writing this spec (measured, not guessed):

* Every Go regex in §2 was compiled with **Go 1.24.7** and compared with the Python original (CPython 3.11, same usage:
  `match` / `search` / `fullmatch` / `findall` / boolean `search`) on **19 668 distinct lines**: every line of every file
  in `receiver/parse/testdata/inputs/` and `samples/corpus/`, plus 85 hand-written edge lines (Arabic-Indic digits,
  U+00A0 / U+2003 whitespace, `ſid`, `faıled`, `TİMEOUT`, `é20260916021500`, `12.09.20261`, `45msé`, `FINER:` …).
  Compared values: match/no-match and **every capture group** (Python `None` vs Go index `-1` kept distinct).
  Result: **0 differences** for all 33 patterns.
* Line coverage of `sap_parser.py` was traced over the 9 `sap__*` inputs plus the two non-SAP inputs that the detector
  routes to `sap` (`mut__kafka_log4j.log`, `mut__iis_w3c.log`), whole-file and in 40-line batches (§9).
* Edge behaviour (availability rounding, TP lines, LOGHEADER meta, multi-line NW Java, JUL with PM) was probed by
  running `SapParser().records()` on hand-written text; outputs are quoted verbatim in the sections below.

---

## 0. Module overview and global state

* `SapParser.name = "sap"`, `kind = "log"`, `line_oriented = True` (inherited). `raw = lines[line_no-1]` of
  `pySplitLines(text)` (04 §0.2).
* **One stateful line parser.** All state lives in local variables of one `records(text)` call: `cur_ts`, `cur_date`,
  `meta`, `pending_jul`, `nwj`, `jvmprop`, `classes`. Nothing survives between calls. `records()` is called twice per file
  (probe on `text[:400_000]` code points for the first 50 records, then the full parse; 04 §0.4); both calls are
  independent.
* **Module-level state:** only `_svc_from_logger` has `@lru_cache(maxsize=4096)`. It is a pure function; the cache has
  no observable effect. Go: plain function (optionally memoised).
* No contextvars, no profiles (`fmt == "sap"` is not in the profile formats list of `_parse_one`), no scenario constants,
  no `datetime.now()`. `FAMILY_SEEN` is never set by this parser, so the report `family` is always `""`.
* `python-dateutil` is **not** used in this file. The parser emits timestamps as **strings** (or `None`); they are
  parsed later by `normalize.parse_timestamp` (02 §3). The only datetime arithmetic in the file is
  `datetime.fromisoformat` in the availability branch (§4.5).
* Live mode: every 40-line batch is a separate `records()` call (01 §14) — `cur_ts`, `meta`, an open NW Java record,
  a JUL header, the JVM property snapshot and the class list are **all lost at batch boundaries** (§8).

---

## 1. Constants (verbatim)

```python
MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}  # Jan=1 .. Dec=12
DAYS = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
MON  = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"

HANA_LEVEL = {"i": "INFO", "w": "WARN", "e": "ERROR", "f": "CRITICAL", "d": "DEBUG"}
NWJ_LEVEL  = {"error": "ERROR", "warning": "WARN", "info": "INFO", "debug": "DEBUG", "path": "DEBUG", "fatal": "CRITICAL", "plain": "INFO"}
JUL_LEVEL  = {"severe": "ERROR", "warning": "WARN", "info": "INFO", "config": "DEBUG", "fine": "DEBUG", "finer": "DEBUG", "finest": "DEBUG"}
SM21_TYPE_LEVEL = {"0": "INFO", "1": "INFO", "2": "INFO", "3": "WARN", "4": "WARN", "5": "ERROR", "6": "ERROR",
                   "7": "ERROR", "8": "CRITICAL", "9": "CRITICAL"}
FAMILY_MARKERS = [DEV_TS_RE, TRC_HEAD_RE, HANA_RE, NWJ_START_RE, JUL_HEAD_RE, GC_UNIFIED_RE, GC_CLASSIC_RE, TP_RE,
                  ALOG_RE, SM21_RE, AVAIL_RE, CLASS_LINE_RE]
STRONG_MARKERS = [TRC_HEAD_RE, LOGHEADER_RE, JVMPROP_HEAD_RE]
```

Lookup keys are exact (ASCII); `SM21_TYPE_LEVEL` is keyed by the **captured character**, so a non-ASCII `\d` digit
(e.g. `٨`) is not a key and gives the default `INFO`.

---

## 2. Regex inventory (verbatim Python, RE2 status, exact Go rewrite)

None of the patterns uses `re.X`, backreferences, possessive/atomic groups, conditionals or `\Z`. Only
`DATE_ONLY_RE` fails to compile in RE2 (lookbehind + lookahead). **But almost all of them are semantically different in
RE2 if copied verbatim**, because Python `str` patterns make `\d`, `\s`, `\S`, `\w`, `\b` Unicode-aware (01 §1.5) and
`re.I` lets `i` match `ı`/`İ`. Copying them as-is would agree on every golden line (all ASCII) but not on Turkish /
Unicode input. The rewrites below are exact (0 diffs on 19 668 lines, see top).

Go building blocks (in addition to `pyWS`, `PS`, `PNS` from 01 §1.1):

```go
const D    = `\p{Nd}`                 // Python \d
const W    = `[\p{L}\p{N}_]`          // Python \w
const NW   = `[^\p{L}\p{N}_]`         // Python \W
const DAYS = `(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)`
const MON  = `(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)`
const I    = `[iIıİ]`                 // a letter i/I under Python re.I
```

Python `re.match` = prefix match → Go pattern starts with `^` and uses `FindStringSubmatchIndex`. Python
`fullmatch` → Go `^…$` (Go `$` without `(?m)` is end of text). `search` → unanchored. All inputs are single lines from
`splitlines()` (no `\n`), so Python `$` == Go `$`. Capture groups: Go `regexp` uses leftmost-first (backtracking-
equivalent) submatch selection, so lazy `+?`, optional groups and alternations capture exactly what Python captures.
Python returns `None` for a group that did not participate; Go returns index `-1` — keep the distinction (the code
uses `m[k] or ""` in places, which treats both the same, but `if ms:` in `_iso` also treats `""` as absent).

| # | Name | Python (verbatim) | Usage | RE2 as is? |
|---|---|---|---|---|
| R1 | `DEV_TS_RE` | `rf"^([A-Z*])\s{{1,3}}{DAYS}\s+({MON})\s+(\d{{1,2}})\s+(\d{{2}}):(\d{{2}}):(\d{{2}})(?::(\d{{3}}))?\s+(\d{{4}})\s*$"` → `^([A-Z*])\s{1,3}(?:Mon\|…\|Sun)\s+((?:Jan\|…\|Dec))\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})(?::(\d{3}))?\s+(\d{4})\s*$` | match | syntax yes; `\s`,`\d` Unicode |
| R2 | `DEV_LINE_RE` | `^([A-Z*])\s{1,3}(\S.*)$` | match | syntax yes; `\s`,`\S` |
| R3 | `DEV_LEVEL_RE` | `^\*\*\*\s*(ERROR\|WARNING\|LOG)\s*(?:([A-Z0-9]{3}))?\s*=>\s*(.*)$` | match | syntax yes; `\s` |
| R4 | `THR_RE` | `^\[Thr\s+(\d+)\]\s*` | match (uses `.end()`) | syntax yes; `\s`,`\d` |
| R5 | `TRC_HEAD_RE` | `^trc file:\s*"([^"]+)",\s*trc level:\s*(\d+),\s*release:\s*"([^"]+)"` | match | syntax yes; `\s`,`\d` |
| R6 | `DEV_META_RE` | `^(sid\|sysno\|pid\|node\s*name\|hostname\|profile\|relno\|patchno)\s+(.+)$`, `re.I` | match | syntax yes; `\s`, `re.I` i-folding |
| R7 | `HANA_RE` | `^\[(\d+)\]\{(-?\d+)\}\[(-?\d+)/(-?\d+)\]\s+(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+([iwefd])\s+(\S+)\s+(\S+?)\((\d+)\)\s*:\s*(.*)$` | match | syntax yes (lazy `+?` fine); `\s`,`\S`,`\d` |
| R8 | `NWJ_START_RE` | `^#(\d\.\d)[\s\x08]?#(\d{4})\s(\d{2})\s(\d{2})\s(\d{2}):(\d{2}):(\d{2})(?::(\d{3}))?#([+\-]\d{4}\|[^#]*)#([^#]*)#([^#]*)#` | match (uses `.end()`) | syntax yes; `\s`,`\d` |
| R9 | `LOGHEADER_RE` | `^<!--([A-Z]+)\[(.*?)\]/-->\s*$` | match | syntax yes; `\s` |
| R10 | `AVAIL_RE` | `^(Available\|Unavailable)\s+(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2}):(\d{2})\s*-\s*(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2}):(\d{2})\s*$` | match | syntax yes; `\s`,`\d` (the two literal spaces stay literal) |
| R11 | `JVMPROP_HEAD_RE` | `rf"^#\s*created at\s+{DAYS}\s+({MON})\s+(\d{{1,2}})\s+(\d{{2}}):(\d{{2}}):(\d{{2}})\s+(\d{{4}})\s*$"` | match | syntax yes; `\s`,`\d` |
| R12 | `JVMPROP_KV_RE` | `^([A-Za-z][\w.]*)\s*=\s*(.*)$` | match | syntax yes; `\w`,`\s` |
| R13 | `CLASS_LINE_RE` | `^[\w$/.\-]+\.class\s*$` | match | syntax yes; `\w`,`\s` |
| R14 | `SID_PATH_RE` | `/usr/sap/([A-Z][A-Z0-9]{2})/([A-Z]+\d{2})` | search | syntax yes; `\d` |
| R15 | `JUL_HEAD_RE` | `rf"^({MON})\s(\d{{1,2}}),\s(\d{{4}})\s(\d{{1,2}}):(\d{{2}}):(\d{{2}})\s(AM\|PM)\s+(\S+)(?:\s+(\S+))?\s*$"` | match | syntax yes; `\s`,`\S`,`\d` |
| R16 | `JUL_LINE_RE` | `^(SEVERE\|WARNING\|INFO\|CONFIG\|FINE\|FINER\|FINEST):\s?(.*)$` | match | syntax yes; `\s` |
| R17 | `GC_UNIFIED_RE` | `^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+\-]\d{4}\|Z)?)\]((?:\[[^\]]*\])*)\s*(.*)$` | match | syntax yes; `\d`,`\s` |
| R18 | (inline) GC tags | `\[([^\]]*)\]` | `re.findall` on group 2 of R17 | **yes, exact** |
| R19 | `GC_CLASSIC_RE` | `^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+\-]\d{4}\|Z)?):\s+[\d.]+:\s+\[(.*)$` | match | syntax yes; `\d`,`\s` |
| R20 | `GC_PAUSE_RE` | `(\d+(?:\.\d+)?)\s*ms\b` | search | syntax yes; **`\b` Unicode** |
| R21 | (inline) GC secs | `(\d+(?:\.\d+)?)\s*secs` | search | syntax yes; `\d`,`\s` |
| R22 | `TP_RE` | `^(\d)\s?([A-Z])?([A-Z]{3}\d{3})[X ]?\s?(.*)$` | match | syntax yes; `\d`,`\s` |
| R23 | `TP_DATE_RE` | `(\d{2})\.(\d{2})\.(\d{4})\s*-\s*(\d{2}):(\d{2}):(\d{2})` | search | syntax yes; `\d`,`\s` |
| R24 | `TP_STAMP_RE` | `\b(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\b` | search | syntax yes; **`\b` Unicode** |
| R25 | `ALOG_RE` | `^(START\|STOP\|ERR\|WRN)\s+(\S+)\s+(\S+)?\s*(\S+)\s+(\d{4})\s+(\d{14})\s+(\S+)?\s*(\S+)?` | match | syntax yes; `\s`,`\S`,`\d` |
| R26 | `SM21_RE` | `^(\d{2}:\d{2}:\d{2})\s+(DIA\|UPD\|UP2\|BTC\|ENQ\|SPO\|RD\|MS\|DP\|GW\|IC\|\w{2,3})\s+(\d{3})?\s*(\d{3})?\s*(\S*)\s+(\S*)\s+([A-Z][A-Z0-9])\s+(\d)\s+(.*)$` | match | syntax yes; `\d`,`\s`,`\S`,`\w` |
| R27 | `DATE_ONLY_RE` | `(?<!\d)(\d{2})\.(\d{2})\.(\d{4})(?!\d)` | search | **NO** (lookbehind, lookahead) |
| R28 | `ERR_WORDS` | `\b(error\|failed\|failure\|abort\|dump\|exception\|rc\s*=\s*(?:8\|12\|16)\|exit code\s*:\s*\"?(?:8\|12\|16)\|cannot\|unable\|not found\|denied\|timeout\|shortdump)\b`, `re.I` | boolean search | syntax yes; **`\b` Unicode, `re.I` i-folding**, `\s` |
| R29 | `WARN_WORDS` | `\b(warning\|retry\|deprecated\|slow\|rc\s*=\s*4\|exit code\s*:\s*\"?4)\b`, `re.I` | boolean search | same as R28 |
| R30 | `TZ_RE` | `[+\-]\d{4}` | `fullmatch` | syntax yes; `\d` |
| R31 | `NWJ_CATEGORY_RE` | `[A-Z]{2,3}(?:-[A-Z0-9]+)+` | `fullmatch` | **yes, exact** (ASCII only) |
| R32 | `NWJ_MSGID_RE` | `com\.sap\.[\w.]+\.\d{4,}` | `fullmatch` | syntax yes; `\w`,`\d` |
| R33 | `NWJ_USER_RE` | `[A-Za-z][\w.\-]*` | `fullmatch` | syntax yes; `\w` |

(`\|` in the table is a Markdown escape of `|`.)

### 2.1 Exact Go patterns (all verified, 0 diffs)

```go
DEV_TS     = `^([A-Z*])`+PS+`{1,3}`+DAYS+PS+`+(`+MON+`)`+PS+`+(`+D+`{1,2})`+PS+`+(`+D+`{2}):(`+D+`{2}):(`+D+`{2})(?::(`+D+`{3}))?`+PS+`+(`+D+`{4})`+PS+`*$`
DEV_LINE   = `^([A-Z*])`+PS+`{1,3}(`+PNS+`.*)$`
DEV_LEVEL  = `^\*\*\*`+PS+`*(ERROR|WARNING|LOG)`+PS+`*(?:([A-Z0-9]{3}))?`+PS+`*=>`+PS+`*(.*)$`
THR        = `^\[Thr`+PS+`+(`+D+`+)\]`+PS+`*`
TRC_HEAD   = `^trc file:`+PS+`*"([^"]+)",`+PS+`*trc level:`+PS+`*(`+D+`+),`+PS+`*release:`+PS+`*"([^"]+)"`
DEV_META   = `(?i)^(s`+I+`d|sysno|p`+I+`d|node`+PS+`*name|hostname|prof`+I+`le|relno|patchno)`+PS+`+(.+)$`
HANA       = `^\[(`+D+`+)\]\{(-?`+D+`+)\}\[(-?`+D+`+)/(-?`+D+`+)\]`+PS+`+(`+D+`{4}-`+D+`{2}-`+D+`{2}`+PS+D+`{2}:`+D+`{2}:`+D+`{2}(?:\.`+D+`+)?)`+PS+`+([iwefd])`+PS+`+(`+PNS+`+)`+PS+`+(`+PNS+`+?)\((`+D+`+)\)`+PS+`*:`+PS+`*(.*)$`
NWJ_START  = `^#(`+D+`\.`+D+`)[`+pyWS+`\x08]?#(`+D+`{4})`+PS+`(`+D+`{2})`+PS+`(`+D+`{2})`+PS+`(`+D+`{2}):(`+D+`{2}):(`+D+`{2})(?::(`+D+`{3}))?#([+\-]`+D+`{4}|[^#]*)#([^#]*)#([^#]*)#`
LOGHEADER  = `^<!--([A-Z]+)\[(.*?)\]/-->`+PS+`*$`
AVAIL      = `^(Available|Unavailable)`+PS+`+(`+D+`{2})\.(`+D+`{2})\.(`+D+`{4}) (`+D+`{2}):(`+D+`{2}):(`+D+`{2})`+PS+`*-`+PS+`*(`+D+`{2})\.(`+D+`{2})\.(`+D+`{4}) (`+D+`{2}):(`+D+`{2}):(`+D+`{2})`+PS+`*$`
JVMPROP_HEAD = `^#`+PS+`*created at`+PS+`+`+DAYS+PS+`+(`+MON+`)`+PS+`+(`+D+`{1,2})`+PS+`+(`+D+`{2}):(`+D+`{2}):(`+D+`{2})`+PS+`+(`+D+`{4})`+PS+`*$`
JVMPROP_KV = `^([A-Za-z][\p{L}\p{N}_.]*)`+PS+`*=`+PS+`*(.*)$`
CLASS_LINE = `^[\p{L}\p{N}_$/.\-]+\.class`+PS+`*$`
SID_PATH   = `/usr/sap/([A-Z][A-Z0-9]{2})/([A-Z]+`+D+`{2})`
JUL_HEAD   = `^(`+MON+`)`+PS+`(`+D+`{1,2}),`+PS+`(`+D+`{4})`+PS+`(`+D+`{1,2}):(`+D+`{2}):(`+D+`{2})`+PS+`(AM|PM)`+PS+`+(`+PNS+`+)(?:`+PS+`+(`+PNS+`+))?`+PS+`*$`
JUL_LINE   = `^(SEVERE|WARNING|INFO|CONFIG|FINE|FINER|FINEST):`+PS+`?(.*)$`
GC_UNIFIED = `^\[(`+D+`{4}-`+D+`{2}-`+D+`{2}T`+D+`{2}:`+D+`{2}:`+D+`{2}(?:\.`+D+`+)?(?:[+\-]`+D+`{4}|Z)?)\]((?:\[[^\]]*\])*)`+PS+`*(.*)$`
GC_TAG     = `\[([^\]]*)\]`                        // FindAllStringSubmatch(.., -1), take [1] of each
GC_CLASSIC = `^(`+D+`{4}-`+D+`{2}-`+D+`{2}T`+D+`{2}:`+D+`{2}:`+D+`{2}(?:\.`+D+`+)?(?:[+\-]`+D+`{4}|Z)?):`+PS+`+[\p{Nd}.]+:`+PS+`+\[(.*)$`
GC_PAUSE   = `(`+D+`+(?:\.`+D+`+)?)`+PS+`*ms(?:$|`+NW+`)`              // see 2.2
GC_SECS    = `(`+D+`+(?:\.`+D+`+)?)`+PS+`*secs`
TP         = `^(`+D+`)`+PS+`?([A-Z])?([A-Z]{3}`+D+`{3})[X ]?`+PS+`?(.*)$`
TP_DATE    = `(`+D+`{2})\.(`+D+`{2})\.(`+D+`{4})`+PS+`*-`+PS+`*(`+D+`{2}):(`+D+`{2}):(`+D+`{2})`
TP_STAMP   = `(?:^|`+NW+`)(`+D+`{4})(`+D+`{2})(`+D+`{2})(`+D+`{2})(`+D+`{2})(`+D+`{2})(?:$|`+NW+`)`   // see 2.2
ALOG       = `^(START|STOP|ERR|WRN)`+PS+`+(`+PNS+`+)`+PS+`+(`+PNS+`+)?`+PS+`*(`+PNS+`+)`+PS+`+(`+D+`{4})`+PS+`+(`+D+`{14})`+PS+`+(`+PNS+`+)?`+PS+`*(`+PNS+`+)?`
SM21       = `^(`+D+`{2}:`+D+`{2}:`+D+`{2})`+PS+`+(DIA|UPD|UP2|BTC|ENQ|SPO|RD|MS|DP|GW|IC|`+W+`{2,3})`+PS+`+(`+D+`{3})?`+PS+`*(`+D+`{3})?`+PS+`*(`+PNS+`*)`+PS+`+(`+PNS+`*)`+PS+`+([A-Z][A-Z0-9])`+PS+`+(`+D+`)`+PS+`+(.*)$`
DATE_ONLY  = `(?:^|\P{Nd})(`+D+`{2})\.(`+D+`{2})\.(`+D+`{4})(?:$|\P{Nd})`   // see 2.2
ERR_WORDS  = `(?:^|`+NW+`)(?i:error|fa`+I+`led|fa`+I+`lure|abort|dump|except`+I+`on|rc`+PS+`*=`+PS+`*(?:8|12|16)|ex`+I+`t code`+PS+`*:`+PS+`*"?(?:8|12|16)|cannot|unable|not found|den`+I+`ed|t`+I+`meout|shortdump)(?:$|`+NW+`)`
WARN_WORDS = `(?:^|`+NW+`)(?i:warn`+I+`ng|retry|deprecated|slow|rc`+PS+`*=`+PS+`*4|ex`+I+`t code`+PS+`*:`+PS+`*"?4)(?:$|`+NW+`)`
TZ         = `^[+\-]`+D+`{4}$`
NWJ_CATEGORY = `^[A-Z]{2,3}(?:-[A-Z0-9]+)+$`
NWJ_MSGID  = `^com\.sap\.[\p{L}\p{N}_.]+\.`+D+`{4,}$`
NWJ_USER   = `^[A-Za-z][\p{L}\p{N}_.\-]*$`
```

### 2.2 Why the lookaround / `\b` rewrites are exact

* **R27 `DATE_ONLY_RE`** (only used with `.search` and groups 1–3). Python returns the leftmost position `p` where two
  digits start, preceded by a non-digit or the string start, and the 4-digit year is not followed by a digit. The Go
  pattern consumes the preceding non-digit (`\P{Nd}`) or anchors at `^`; RE2 tries every start, the leftmost Go match
  starts at `p-1` (or at 0 via `^`, which is tried first), so groups 1–3 are the same digits. Never use the whole-match
  span. Examples (measured, Python and Go agree): `12.09.2026` → `12`,`09`,`2026`; `112.09.2026` → **no match** (the
  `12` is preceded by a digit and no other start works); `12.09.20261` → no match; `x12.09.2026y` → match;
  `١٢.09.2026` (Arabic-Indic) → match, groups `١٢`,`09`,`2026`.
* **R20 `GC_PAUSE_RE`**: `\b` after `ms` = next char is not Unicode `\w` or end. All alternatives of the boundary are
  after the last capture, so group 1 is unchanged (Python and Go both prefer the leftmost start, then the longest greedy
  `\d+`, then the optional fraction). `12.5msx 3ms` → group `3`; `45msé` → no match; `45ms,` → `45`.
* **R24 `TP_STAMP_RE`**: both `\b`s sit next to digits (which are `\w`), so `\b` before = previous char not `\w`
  (or start), after = next char not `\w` (or end). `é20260916021500 ` → Python no match (`é` is `\w`), RE2 `\b` would
  match — hence the rewrite. Groups 1–6 identical.
* **R28/R29 `ERR_WORDS`/`WARN_WORDS`**: used only as a boolean (`.search(...)` truthiness), every alternative starts
  and ends with a word character, so `\b…\b` = "non-word or edge on both sides". Inside `(?i:…)` every `i` is written
  `[iIıİ]` because Python `re.I` matches `ı`/`İ` for `i` and Go does not (`db faıled now` → ERROR in Python;
  `TİMEOUT` too). Go `(?i)` already folds `s`↔`ſ` (`ſlow` → WARN in both) and `k`↔`K` (no `k` in these words).
  Keep the boundary classes **outside** the `(?i:…)` group.
* **R6 `DEV_META_RE`** `re.I`: same `i` treatment; the `key` computed afterwards is `pyLower(group1).replace(" ", "")`
  so a match on `ſid`/`sıd`/`SİD` still **consumes the line** (`continue`) but sets nothing (§4.11).

---

## 3. Small helpers

### 3.1 `_iso(y, mo, d, h, mi, s, ms=None) -> str`

```python
return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}T{int(h):02d}:{int(mi):02d}:{int(s):02d}" + (f".{int(ms):03d}" if ms else "")
```

* Arguments are regex captures (str) or ints (`MONTHS[...]`, the computed JUL hour). `int()` accepts any `\p{Nd}`
  digits (Arabic-Indic etc.) and the output is always ASCII. Go: `pyInt(s)` = sum of `unicode` digit values of each rune
  (every Nd range in Unicode is a run of aligned 0–9 blocks, so `value = (r - rangeLo) % 10` over `unicode.Nd` ranges;
  unit-test against Python for a few scripts).
* Zero-padding: `%04d` / `%02d`; values are never larger than the field (regex limits the digit count) **except** that
  nothing validates ranges: `Sep 31`, hour `25`, month `13` (TP dates) are emitted as such.
* `ms`: the fraction is appended only when `ms` is truthy. A captured `"000"` is truthy → `.000`. `None` and `""` → no
  fraction. Output `.%03d` of `int(ms)`; `ms` is always exactly 3 digits where used.
* No timezone. Invalid dates are not rejected here; `parse_timestamp` later returns `None` for them
  (measured: `'2026-09-31T02:14:07'` → `None`, `'2026-02-30T01:00:00.123'` → `None`) and the row becomes `_no_ts` (01 §10.2).
  Exception: the availability branch calls `datetime.fromisoformat` itself and **raises** (§4.5).

### 3.2 `_sev_from_words(text, default="INFO") -> str`

`"ERROR"` if `ERR_WORDS` finds anything, else `"WARN"` if `WARN_WORDS` does, else `default`. ERR is checked first.
Examples (measured): `rc = 12` → ERROR; `rc=128` → no ERR (`\b` after `12` fails, `128` not in the list) → not WARN
either → default; `exit code : "8"` → ERROR; `IOException` → no match (`exception` is not at a word boundary);
`_error`/`error_`/`1error` → no match (`_` and digits are `\w`); `x-error-y` → ERROR; `not  found` (two spaces) → no
match (literal single space); `dumps` → no match.

### 3.3 `_svc_from_logger(logger) -> str` (memoised, pure)

```python
parts = [p for p in logger.split(".") if p]
if len(parts) >= 3 and parts[:2] == ["com", "sap"]:
    rest = [p for p in parts[2:] if p.lower() not in ("engine", "services", "service", "core", "impl", "server", "interfaces", "jee", "tc")]
    return "sap-" + (rest[0].lower() if rest else parts[-1].lower())
if len(parts) >= 2:
    return parts[1].lower()
return logger.lower()
```

* Split on literal `.`, drop empty parts. `"com"`/`"sap"` comparison is case-sensitive. The skip list compares
  `pyLower(p)`. All lowercasing is `pyLower` (01 §1.4: `İ` → `i̇`).
* Examples: `com.sap.engine.services.deploy` → `sap-deploy`; `com.sap.engine.core.Framework` → `sap-framework`;
  `com.sap.engine.services.dbpool.deploy.ContainerImpl` → `sap-dbpool`; `com.sap.engine.core.cluster` → `sap-cluster`;
  `com.sap.engine.services` → `rest` empty → `sap-services` (`parts[-1]`); `com.acme.Foo` → `acme`; `Foo` → `foo`;
  `.x` → parts `["x"]` → `logger.lower()` = `.x`.

---

## 4. `SapParser.records(text)` — exact algorithm

```text
cur_ts = None; cur_date = None; meta = {} (ordered); pending_jul = None; nwj = None; jvmprop = None; classes = []
for i, ln in enumerate(pySplitLines(text), start=1):
    s = pyRStrip(ln)
    ... steps 4.1 – 4.17 in this exact order; the first one that "continue"s or yields ends the line ...
after the loop: 4.18 (flush NW Java), 4.19 (JVM properties record), 4.20 (class-list record)
```

`c0 = s[0]` is the first **code point** of `s` (after the blank check, `s` is non-empty).

### 4.1 Blank line

`if pyStrip(s) == ""`: if an NW Java record is open (`nwj`), yield it (`nwj.line_no`, `_finish_nwj(rec)`) and set
`nwj = None`. Then `continue`. Blank lines never reach any other branch.

### 4.2 NW Java continuation (non-`#` line while a record is open)

`if nwj and c0 != "#"`: append `s` (right-stripped line) to `nwj.rec["_body"]`; `continue`. No other check runs —
a `<!--…-->`, a HANA line, anything, is swallowed into the open record until a blank line, a new `#2.0` start, or EOF.

### 4.3 `#` lines

`if c0 == "#"`:

1. `m = NWJ_START.match(s)`; if it matches:
   * if `nwj` is open, yield it first (its own line number).
   * `ts = _iso(g2, g3, g4, g5, g6, g7, g8)` (year, month, day, h, m, s, optional ms).
   * `tz = g9 if TZ.fullmatch(g9 or "") else ""` (group 9 always participates; it is `[+\-]\d{4}` or any `[^#]*`).
   * record (key order matters):
     `timestamp = ts + (tz[:3] + ":" + tz[3:] if tz else "")` (`+0300` → `+03:00`; `tz[:3]` is code-point slicing of
     a 5-rune string),
     `severity = NWJ_LEVEL.get(pyLower(pyStrip(g10 or "")), "INFO")`,
     `origin = pyStrip(g11 or "")`,
     `sap.format = "nw-java"`,
     `_body = [s[m.end():]]` (the rest of the line after the 6th `#`, possibly `""`),
     then `rec.update({k: v for k, v in meta.items() if k != "host"})` (meta keys appended in meta order).
   * if `s.endswith("#") and s.count("#") >= 8` → single-line record: yield `(i, _finish_nwj(rec))`.
     else → `nwj = (i, rec)` (open).
   * `continue`.
2. no start match, but `nwj` open → append `s` to `_body`; `continue`.
3. otherwise fall through to 4.4 (a `#` line with no open record: JVM header, separators, IIS `#Software:` comments …).

### 4.4 `<!--NAME[...]/-->` file headers (`c0 == "<"`)

`if c0 == "<" and (m := LOGHEADER.match(s))`:
* `g1 == "NAME"` → `meta["sap.log_name"] = g2`; if `SID_PATH.search(g2)` → `meta["sap.sid"] = sid`,
  `meta["sap.instance"] = inst` (assigned in that order).
* `g1 in ("FORMATTER", "ENGINEVERSION")` → `meta["sap." + pyLower(g1)] = g2` (`sap.formatter`, `sap.engineversion`).
* any other name (`LOGHEADER`, …) → nothing.
* always `continue` (never an event).

Probed: `<!--NAME[/usr/sap/TPD/J00/work/deploy.log]/-->` then `<!--FORMATTER[com.sap.FOO]/-->` give every later NW Java
record `sap.log_name`, `sap.sid = TPD`, `sap.instance = J00`, `sap.formatter = com.sap.FOO`.

### 4.5 JVM property snapshot header

`if c0 == "#" and "created at" in s and (m := JVMPROP_HEAD.match(s))`:
`jvmprop = {"timestamp": _iso(g6, MONTHS[g1], g2, g3, g4, g5), "severity": "INFO", "sap.format": "jvm-properties", "_props": {}}`
(replaces any previous snapshot — only the **last** header's properties survive); `continue`.
(`"created at"` substring test is a pure shortcut; it does not change the result.)

### 4.6 JVM property line

`if jvmprop is not None and (m := JVMPROP_KV.match(s))`: `jvmprop["_props"][g1] = pyStrip(g2)` (duplicate key:
value replaced, first position kept); `continue`. Active for **every later line of the text** once a header was seen.

### 4.7 Class list line

`if s.endswith(".class") and CLASS_LINE.match(s)`: `classes.append(pyStrip(s))`; `continue`. (`endswith` is a
shortcut; `s` is right-stripped so `\s*$` is moot; the regex forbids leading whitespace so the strip is a no-op.)

### 4.8 Availability log (`c0` is `A` or `U`)

`if c0 in "AU" and (m := AVAIL.match(s))`:

```python
start = _iso(g4, g3, g2, g5, g6, g7)          # dd.mm.yyyy → yyyy-mm-ddTHH:MM:SS
end   = _iso(g10, g9, g8, g11, g12, g13)
mins  = round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60)
yield i, {"timestamp": start, "severity": "ERROR" if g1 == "Unavailable" else "INFO", "service": "sap-availability",
          "message": f"{g1} for {mins} min ({g5}:{g6}:{g7} - {g8}.{g9}.{g10} {g11}:{g12}:{g13})",
          "avail.state": pyLower(g1), "avail.end": end, "avail.duration_min": mins, "sap.format": "availability"}
continue
```

* `mins` is a Python **int** (`round()` of a float with no ndigits → ties-to-even): 30 s → `0`, 90 s → `2`, 150 s → `2`.
  Go: `int(math.RoundToEven(seconds / 60))` with `seconds` an exact integer (naive datetimes, no DST). Negative
  durations (end before start) are allowed (`round(-0.5) == 0`, `-1.5 → -2`).
* Message uses the **raw captured strings** of the end date/time and start time (not `_iso` output).
* **Raises** `ValueError` (whole ingest fails, 04 §0.3) when either date is invalid for `fromisoformat` (day 31 in a
  30-day month, Feb 29 in a non-leap year, hour ≥ 24, minute/second ≥ 60, month 0/13, day 0). Go must return an error.
* Probed: `Available 24.10.2021 18:51:33 - 24.10.2021 19:34:33` →
  `{'timestamp': '2021-10-24T18:51:33', 'severity': 'INFO', 'service': 'sap-availability', 'message': 'Available for 43 min (18:51:33 - 24.10.2021 19:34:33)', 'avail.state': 'available', 'avail.end': '2021-10-24T19:34:33', 'avail.duration_min': 43, 'sap.format': 'availability'}`.

### 4.9 HANA trace

`if m := HANA.match(s)`:
`yield i, {"timestamp": g5.replace(" ", "T"), "severity": HANA_LEVEL.get(g6, "INFO"), "service": "hana-" + pyLower(g7), "origin": g7, "message": pyStrip(g10), "sap.thread": g1, "sap.connection": g2, "sap.source": g8 + ":" + g9, "sap.format": "hana"}`; `continue`.

* Only the ASCII space between date and time is replaced; if the separator was another Python-whitespace char (tab,
  U+00A0 …) it stays and `parse_timestamp` handles it (measured: `'2026-09-16\t02:14:10.123456'` → `2026-09-16 02:14:10.123456+00:00`).
* Fraction of any length; `parse_timestamp` truncates beyond 6 digits (`.1234567` → `.123456`).
* `g8` is the lazy `(\S+?)` before `(digits)`: `TrexNet.cpp(00123)` → `TrexNet.cpp:00123` (leading zeros kept).
* Group 6 is one of `iwefd` (regex), so `HANA_LEVEL.get` never defaults.

### 4.10 JVM GC — unified logging

`if m := GC_UNIFIED.match(s)`:

```python
tags  = findall(GC_TAG, g2)                                    # list of str, may be empty
level = first t in tags with t in ("info","warning","error","debug","trace"), else "info"
pause = GC_PAUSE.search(g3)
sev   = {"warning": "WARN", "error": "ERROR", "debug": "DEBUG", "trace": "DEBUG"}.get(level, "INFO")
if pause and float(pause[1]) >= 1000:  sev = "ERROR" if float(pause[1]) >= 5000 else "WARN"
elif "Full GC" in g3 or "Pause Full" in g3:  sev = "WARN"
rec = {"timestamp": g1, "severity": sev, "service": "jvm-gc", "message": pyStrip(g3), "sap.format": "jvm-gc",
       "gc.tags": ",".join(t for t in tags if t not in (level,) and not t.endswith("s"))}
if pause: rec["gc.pause_ms"] = pause[1]                        # the captured STRING, e.g. "45.123"
yield i, rec; continue
```

* Exact comparisons on tag text (case-sensitive). `gc.tags` drops **every** tag equal to `level` and every tag ending in
  `s` (uptime `10.001s`, but also `os`, `class`, `ms`…). With no tags → `""` (later dropped from attributes because `""`).
* `float(pause[1])` accepts Unicode digits: normalise Nd → ASCII before `strconv.ParseFloat`. Thresholds compare
  floats (`>= 1000`, `>= 5000`). The Full-GC test is an `elif`: it runs whenever there is no pause or the pause is
  `< 1000` ms (so a short Full GC is WARN); a pause `>= 1000` decides alone (WARN or ERROR).
* `timestamp = g1` verbatim (`2026-09-16T02:14:22.000+0300`, `…Z`, no-offset forms); `parse_timestamp` keeps the
  offset (`+0300` → `+03:00`; `.000` → isoformat drops zero microseconds → `2026-09-16T02:14:22+03:00`).
* Lines like Kafka `[2026-10-14T09:30:00.660Z] INFO [GroupCoordinator 1]: …` match with zero tags (the `INFO` word
  is outside brackets) → `level = "info"`, severity INFO, `gc.tags = ""` (golden `mut__kafka_log4j.log`, 155 rows).

### 4.11 JVM GC — classic

`if m := GC_CLASSIC.match(s)`:

```python
pause = GC_SECS.search(g2)
sev = "WARN" if "Full GC" in g2 else "INFO"
if pause and float(pause[1]) >= 1.0:  sev = "ERROR" if float(pause[1]) >= 5.0 else "WARN"
rec = {"timestamp": g1, "severity": sev, "service": "jvm-gc", "message": "[" + pyStrip(g2), "sap.format": "jvm-gc"}
if pause: rec["gc.pause_ms"] = str(round(float(pause[1]) * 1000))
yield i, rec; continue
```

* `gc.pause_ms` is a **string** of an int: `round()` without ndigits → ties-to-even on the float product.
  Go: `strconv.FormatInt(int64(math.RoundToEven(f*1000)), 10)` with `f` parsed from the (Nd-normalised) capture;
  `7.2345678 * 1000 = 7234.5678` → `"7235"` (golden).
* Message: `"[" + pyStrip(g2)` (`g2` is everything after the first `[`).

### 4.12 `java.util.logging` header line

`if m := JUL_HEAD.match(s)`:
`h = pyInt(g4) % 12 + (12 if g7 == "PM" else 0)` (12 AM → 0, 12 PM → 12, `13 PM` → 13);
`pending_jul = (i, _iso(g3, MONTHS[g1], g2, h, g5, g6), g8, g9 or "")`; `cur_ts = pending_jul.ts`; `continue`.
No record is emitted for the header itself. A new header overwrites an unpaired one.

### 4.13 `java.util.logging` level line

`if pending_jul and (m := JUL_LINE.match(s))`: take `(ln_no, ts, logger, method)` from `pending_jul`, set
`pending_jul = None`, and

`yield ln_no, {"timestamp": ts, "severity": JUL_LEVEL.get(pyLower(g1), "INFO"), "origin": logger, "service": _svc_from_logger(logger), "message": pyStrip(g2), "sap.method": method, "sap.format": "java-util-logging"}`; `continue`.

* `line_no` (and therefore `raw`) is the **header** line.
* `pending_jul` is **not** cleared by intervening non-matching lines: a header followed by unrelated lines still pairs
  with the next `SEVERE:`/`INFO:`… line, however far. Without a pending header, a `SEVERE: …` line falls through to
  4.16 / 4.17 (becomes an `other` record).
* `sap.method` may be `""` (header without method). Probed: `Sep 16, 2026 12:14:18 PM com.acme.Foo` + `SEVERE: boom` →
  `{'timestamp': '2026-09-16T12:14:18', 'severity': 'ERROR', 'origin': 'com.acme.Foo', 'service': 'acme', 'message': 'boom', 'sap.method': '', 'sap.format': 'java-util-logging'}`.

### 4.14 ABAP developer trace

In this order:

1. `if m := TRC_HEAD.match(s)`: `meta.update({"sap.trc_file": g1, "sap.trc_level": g2, "sap.release": g3})`; `continue`.
2. `if m := DEV_TS.match(s)`: `cur_ts = _iso(g8, MONTHS[g2], g3, g4, g5, g6, g7)`; `continue` (timestamp-only line).
3. **Separator**: `if set(pyStrip(s)) <= set("-=*_# ")`: `continue` — every code point of the stripped line is one of
   `- = * _ #` or ASCII space (`#====`, `-----`, `* * *`, `***`). Note `***` alone is a separator, not an error line.
4. `if m := DEV_LINE.match(s)`: `comp = g1`, `body = g2`:
   * `if comp == "*" and not body.startswith("***")`: `continue` (`*  ACTIVE TRACE LEVEL 1` decoration).
   * `if mm := DEV_META.match(body)`: `key = pyLower(mm1).replace(" ", "")` (only ASCII spaces removed: `node\tname`
     stays `node\tname`), `val = pyStrip(mm2)`:
     `sid` → `meta["sap.sid"] = val`; `pid` → `meta["sap.pid"] = val`; `nodename`/`hostname` → `meta["host"] = val`;
     `profile` → `meta.setdefault("host", val.rsplit("_", 1)[-1])` then `meta["sap.instance"] = val.rsplit("/", 1)[-1]`;
     any other key (`sysno`, `relno`, `patchno`, or a non-ASCII-folded spelling) → nothing. **Always `continue`.**
     Consequence: any trace text line whose body starts with one of these words followed by whitespace (e.g.
     `M  Profile parameter …`, `M  pid 4711 started`) is silently consumed (`Profile parameter x` matches; probed).
   * `thr = ""`; `if mt := THR.match(body)`: `thr = mt1`, `body = body[mt.end():]`.
   * `sev, code = "INFO", ""`; `if ml := DEV_LEVEL.match(body)`:
     `sev = {"ERROR": "ERROR", "WARNING": "WARN", "LOG": "INFO"}[ml1]`, `code = ml2 or ""`, `body = ml3`;
     if `ml1 == "LOG"`: `sev = _sev_from_words(body, "INFO")`.
     `elif body.startswith("***")`: `sev = "ERROR"` (body unchanged, e.g. `***ERROR=>` without the expected spacing is
     still matched by DEV_LEVEL; `*** something` is ERROR).
   * `if not cur_ts and not body: continue`.
   * record, key order:
     `timestamp = cur_ts` (may be `None`), `severity`, `message = pyStrip(body)`, `sap.component = comp`,
     `sap.format = "dev-trace"`,
     `service = "sap-" + pyLower(meta["sap.sid"])` if `meta.get("sap.sid")` (truthy) else
     `"sap-" + pyLower(meta.get("sap.trc_file", "kernel").split(".")[0])`,
     then every meta item except `host` (meta order),
     then `host` if `meta.get("host")` truthy, `sap.thread` if `thr`, `sap.msg_code` if `code`.
   * `yield i, rec`; `continue`.

Golden `sap__dev_w0` walk-through: line 1 TRC_HEAD; line 2 separator; lines 3–4 `*` decoration; line 5 DEV_TS
(`.123`); line 6 `B  create_con …` → service `sap-dev_w0` (no sid yet), meta = trc_file/level/release; lines 7–11 META
(`sysno`, `sid`, `relno`, `profile` → host `sapprd01`, instance `PRD_D00_sapprd01`; `pid`); line 12 INFO; line 13
DEV_TS; line 14 `*** LOG Q0I=>` → code `Q0I`, `_sev_from_words` → INFO; lines 15–17 ERROR/WARNING/ERROR; line 18
`[Thr 140222]` + ERROR.

### 4.15 SM21 system-log export

`if m := SM21.match(s)`:

```python
ts = f"{cur_date[2]}-{cur_date[1]}-{cur_date[0]}T{g1}" if cur_date else None   # RAW captured strings, no int()
yield i, {"timestamp": ts, "severity": SM21_TYPE_LEVEL.get(g8, "INFO"), "service": "sap-" + pyLower(g2),
          "message": pyStrip(g9), "sap.wp_type": g2, "sap.wp_no": g3 or "", "sap.client": g4 or "",
          "sap.user": g5, "sap.tcode": g6, "sap.msg_code": g7 + g8, "sap.format": "sm21"}
continue
```

`cur_date` comes only from 4.17 (date-only lines). `sap.user`/`sap.tcode` may be `""` (`\S*`). Golden: `BY 4` → WARN,
`R6 8` → CRITICAL, `EP 2` → INFO.

### 4.16 Transport ALOG / SLOG

`if m := ALOG.match(s)`: `st = g6` (14 digits); `ts = _iso(st[0:4], st[4:6], st[6:8], st[8:10], st[10:12], st[12:14])`
(code-point slices);
`yield i, {"timestamp": ts, "severity": {"ERR": "ERROR", "WRN": "WARN"}.get(g1, "INFO"), "service": "sap-" + pyLower(g4 or "") + "-transport", "message": pyStrip(s), "sap.step": g2, "sap.rc": g5, "sap.format": "transport"}`; `continue`.
(`START`/`STOP` → INFO. Golden `sap__SLOG2638.PRD.lst`: `sap.rc` `"0004"` / `"0008"`, string with leading zeros.)

### 4.17 tp / R3trans / SUM

`if m := TP.match(s)`:

```python
body = g4
if md := TP_DATE.search(body):   cur_ts = _iso(md3, md2, md1, md4, md5, md6)
elif ms_ := TP_STAMP.search(body): cur_ts = _iso(*ms_.groups())          # yyyy mm dd HH MM SS
sev = "ERROR" if (g2 or "") == "E" else "WARN" if (g2 or "") == "W" else _sev_from_words(body)
yield i, {"timestamp": cur_ts, "severity": sev, "service": "sap-" + pyLower(g3[:3]), "message": pyStrip(body),
          "sap.msg_code": g3, "sap.level": g1, "sap.format": "tp"}
continue
```

The new `cur_ts` applies to this line and every later line (dev, other, class list). Probed:
`4 ETW000  date&time   : 16.09.2026 - 02:14:07` → ts `2026-09-16T02:14:07`, INFO, `sap-etw`, message
`date&time   : 16.09.2026 - 02:14:07`; `2EETW125 SQL error "8" during import` → ERROR (`E` flag), `ETW125`, level `2`;
`1 ETP111 exit code : "8"` → ERROR via words, `sap-etp`; `4 ETW000 R3trans version 6.26 20260916021500` → ts
`2026-09-16T02:15:00`.

### 4.18 Date-only lines

`if (md := DATE_ONLY.search(s)) and len(pyStrip(s)) <= 40` (length in **code points**): `cur_date = (md1, md2, md3)`
(raw strings, dd, mm, yyyy); `continue`. Lines longer than 40 fall through to 4.19. (Golden: `System log  PRD  16.09.2026`.)

### 4.19 Anything else → `other`

`yield i, {"timestamp": cur_ts, "severity": _sev_from_words(s), "message": pyStrip(s), "sap.format": "other"}` plus,
only if `meta.get("sap.sid")` is truthy, a final key `"service": "sap-" + pyLower(meta["sap.sid"])`.
`_sev_from_words` runs on `s` (right-stripped, left whitespace kept — irrelevant for the boolean). This branch also
receives `#` comment lines that are not NW Java / JVM / separators (golden `mut__iis_w3c.log`: `#Software: …`,
`#Version: 1.0`), unpaired `SEVERE:` lines, stack-trace lines, etc.

### 4.20 End of text (in this order)

1. **NW Java flush**: if `nwj` open → yield `(nwj.line_no, _finish_nwj(rec))`.
2. **JVM property record**, if `jvmprop is not None` (even with zero properties):
   ```python
   pr = jvmprop.pop("_props"); home = pr.get("home", "")
   if ms_ := SID_PATH.search(home):
       jvmprop["service"], jvmprop["sap.sid"], jvmprop["sap.instance"] = "sap-" + pyLower(sid), sid, inst
   jvmprop["message"] = pyStrip(f"{pr.get('name', 'JVM')} {pr.get('version', '')} (vm {pr.get('vmVersion', '')}, {pr.get('type', '')}, {pr.get('cpu', '')}) home={home}")
   jvmprop.update({"jvm." + k: v for k, v in pr.items()})           # property insertion order
   yield 1, jvmprop
   ```
   Key order: `timestamp, severity, sap.format, [service, sap.sid, sap.instance], message, jvm.*`. `line_no = 1` →
   `raw = lines[0]` (first line of the text, e.g. the `#====` separator in `sap__bootstrap.jvm`). Timestamp naive.
   Golden message: `SAP Java Server VM 1.8.0_241 (vm 8.1.061 10.0.2+000, server, amd64) home=/usr/sap/TPD/J00/exe/sapjvm_8`.
3. **Class-list record**, if `classes` non-empty:
   ```python
   pk = {}   # ordered: package -> count, first-seen order
   for c in classes: k = c.rsplit("/", 1)[0] if "/" in c else c; pk[k] = pk.get(k, 0) + 1
   top = ", ".join(f"{k} ({n})" for k, n in sorted(pk.items(), key=lambda kv: -kv[1])[:5])   # STABLE sort, ties keep first-seen order
   yield 1, {"timestamp": cur_ts, "severity": "DEBUG", "service": "sap-jvm",
             "message": f"class prefetch list: {len(classes)} classes; {top}", "sap.format": "class-list",
             "jvm.class_count": len(classes)}                                                   # int
   ```
   Go: `sort.SliceStable` by descending count. `cur_ts` is whatever the last timestamp-setting line left (usually
   `None` → `_no_ts` → fallback, golden `1970-01-01T00:00:00+00:00` because nothing else in the ingest had a time).

Because these end records have `line_no = 1` and are yielded last, they come after all line records in parse order;
the final stable sort by timestamp (01 §10.3) decides their position.

---

## 5. `SapParser._finish_nwj(rec) -> rec`

```python
body = "\n".join(rec.pop("_body"))
fields = [pyStrip(f) for f in body.split("#")]
non_empty = [f for f in fields if f]
rec["message"] = non_empty[-1] if non_empty else pyStrip(body)
lead = [f for f in fields[:12] if f]
for f in lead:
    if NWJ_CATEGORY.fullmatch(f):              rec.setdefault("sap.category", f)
    elif NWJ_MSGID.fullmatch(f):               rec.setdefault("sap.msg_id", f)
    elif "~" in f:                             rec.setdefault("sap.application", f)
    elif f.startswith("com.sap.") and "sap.logger" not in rec:  rec["sap.logger"] = f
for idx, f in enumerate(fields[1:12], 1):
    if f.startswith("com.sap.") and f == rec.get("sap.logger") and idx + 1 < len(fields):
        if NWJ_USER.fullmatch(fields[idx + 1]): rec["sap.user"] = fields[idx + 1]
        break
loc = rec.get("origin", "")
if loc.startswith("/"):  rec["service"] = "sap-" + pyLower(loc.strip("/").rsplit("/", 1)[-1])
elif loc:                rec["service"] = _svc_from_logger(loc)
return rec
```

Details:
* `split("#")` is a plain literal split (keeps empty fields). `pyStrip` removes `\n` too, so a field spanning lines keeps
  only **inner** newlines: probed multi-line record (`#2.0 #… #XYZ#Fatal##` + `#line two` + `at foo.bar(Baz.java:1)`)
  → `message = "line two\nat foo.bar(Baz.java:1)"`, no tz (`XYZ` is not `[+-]dddd`), severity CRITICAL, origin `""`
  → **no `service` key**.
* `fields[:12]` / `fields[1:12]` are list slices (fields 0..11 / 1..11). `fields[idx+1]` may be index 12.
* The `break` sits inside the `if`, so the loop stops at the first field that equals the logger; the user is set only if
  the next field fully matches `NWJ_USER` (not empty, starts with an ASCII letter).
* `loc.strip("/")` strips only `/` characters; `/System/Server/Deployment` → `sap-deployment`.
* Final key order for golden `defaultTrace`: `timestamp, severity, origin, sap.format, [meta…], message, sap.category,
  sap.logger, sap.user, service` (the `_body` key is removed, so `message` is appended at the end).
* Golden `sap__defaultTrace.0.trc` record 1: start line has 6 `#` → multi-line; line 2 (starts with `#`) appended;
  line 3 is a new start → record 1 yielded with line_no 1; fields: `["", "BC-JAS-DPL", "deploy", "C0000A000123", "12345",
  "", "com.sap.engine.services.deploy.server.DeployServiceImpl", "Guest", …, "Deployment of application sap.com/tc~mobile~app failed: Connection to database PRD lost", ""]`
  → category `BC-JAS-DPL`, logger `…DeployServiceImpl`, user `Guest`, service `sap-deploy` (from origin
  `com.sap.engine.services.deploy`); `sap.com/tc~mobile~app` is beyond field 12, so no `sap.application`. Last record is
  flushed at EOF (4.20).
* Probed single-line record with every lead type:
  `#2.0 #2026 09 16 02:14:15:100#+0300#Error#/System/Server/Deployment#com.sap.ASJ.dpl_dc.001028#BC-JAS-DPL#tc~bl~deploy_controller#com.sap.engine.Foo#Administrator#0#text here#`
  (11 `#`, ends with `#`) → `sap.msg_id = com.sap.ASJ.dpl_dc.001028`, `sap.category = BC-JAS-DPL`,
  `sap.application = tc~bl~deploy_controller`, `sap.logger = com.sap.engine.Foo`, `sap.user = Administrator`,
  `service = sap-deployment`, message `text here`, timestamp `2026-09-16T02:14:15.100+03:00`.
  Note the msg id is tested **before** the `com.sap.` logger rule, so it is never taken as the logger.

---

## 6. `sap_score(lines) -> float` and routing (`format_detector`)

```python
if not lines: return 0.0
if any(r.match(ln) for ln in lines[:20] for r in STRONG_MARKERS): return 1.0
hits = 0
for ln in lines:
    if any(r.match(ln) for r in FAMILY_MARKERS) or JUL_LINE_RE.match(ln) or ln.startswith("#"):
        hits += 1
    elif DEV_LINE_RE.match(ln) and (ln.startswith(("M ", "A ", "B ", "C ", "N ", "S ", "X ", "Y ", "I ", "G ", "E ", "* ", "T ")) or "***" in ln):
        hits += 0.6
return round(hits / len(lines), 2)
```

* `lines` = `detect_format`'s sample: non-blank lines (`pyStrip(ln) != ""`) among the first 200 `pySplitLines` of the
  first 400 000 code points (01 §8.3). Lines are **not** stripped; the regexes are applied with prefix `match`.
* STRONG check: only the first 20 sample lines, markers `TRC_HEAD`, `LOGHEADER`, `JVMPROP_HEAD` (golden: `dev_w0`,
  `bootstrap.jvm` → 1.0).
* `FAMILY_MARKERS` order is irrelevant (`any`). `JUL_LINE` counts even without a header; every line starting with `#`
  counts.
* The 0.6 branch requires `DEV_LINE` and (a literal two-char prefix from the tuple — capital letter + **ASCII space** —
  or `***` anywhere).
* Arithmetic: accumulate in `float64` in line order (`+1` / `+0.6`); integers are exact, so Go float64 accumulation
  reproduces Python's int→float promotion. Then Python `round(x, 2)` (01 §1.6 helper, not `math.Round`).
* Routing: `detect_format` reaches SAP only after json, jsonl (> 0.8), w3c (`#Fields:` in first 10), winevt, cef, access,
  CRI, syslog (> 0.6), syslog-noprog all failed; then `if sap >= 0.5: return ("sap", sap)` — the score is the parser
  confidence (`parser_confidence` = `round(conf, 4)` in the golden). Below 0.5 it continues to grok/csv/kv/text.
* Live: the first batch's detection result (`format = "sap"`, its confidence) is cached and reused for later batches
  (01 §14), so later batches are parsed as SAP with the first batch's confidence regardless of content.
* Golden scores: `sap__SLOG2638.PRD.lst` 1.0 (ALOG), `sap__class_prefetch.lst` 1.0 (CLASS_LINE), `sap__defaultTrace.0.trc`
  1.0 (NWJ start + `#` lines), `sap__indexserver…trc` 1.0, `sap__sapjvm_gc.jvm` 1.0, `sap__std_server0.out` 1.0
  (JUL head + JUL lines), `sap__sm21_export.file` 0.75 (3 of 4), `mut__kafka_log4j.log` whole 0.64 / live 0.57
  (GC_UNIFIED on `[…Z]` lines), `mut__iis_w3c.log` whole 0.51 / live 0.5 (`#` lines; the IIS file is *not* caught by the
  w3c rule because its `#Fields:` line is not among the first 10 sample lines).

---

## 7. What the records become (interaction with slices 01/02)

* Roles are chosen by `auto_map` over the first 50 records' keys (02 §10). Measured roles in the goldens:
  `timestamp → "timestamp"`, `severity → "severity"`, `message → "message"`, `service → "service"` (when present),
  `origin → "origin"` (NW Java, HANA, JUL), and two non-obvious ones: **`sap__dev_w0`**: `environment → "sap.sid"`,
  `host → "host"`, `origin → "sap.component"`; **`sap__bootstrap.jvm`**: `host → "sap.instance"` (golden host `J00`).
  Go must reuse the generic `auto_map`; do not hard-code these.
* `severity` values emitted: `INFO`, `WARN`, `ERROR`, `CRITICAL`, `DEBUG` → `column_severity` keeps them; `_sev_src`
  is never added (severity is always a non-empty field).
* `timestamp` strings emitted (all go through `parse_timestamp`, 02 §3):
  | branch | form | result |
  |---|---|---|
  | dev trace, JUL, ALOG, TP, availability, JVM props | `YYYY-MM-DDTHH:MM:SS[.mmm]` naive | UTC, `.mmm` → `.mmm000` |
  | NW Java | `YYYY-MM-DDTHH:MM:SS[.mmm][±HH:MM]` | offset kept (`+03:00`) |
  | HANA | `YYYY-MM-DDTHH:MM:SS[.f+]` | UTC, >6 fraction digits truncated |
  | GC unified / classic | raw `YYYY-MM-DDTHH:MM:SS[.f+][±HHMM\|Z]` | offset kept, `Z` → `+00:00`, `.000` → no fraction |
  | SM21 | `{yyyy}-{mm}-{dd}T{HH:MM:SS}` from raw captures | UTC |
  | any, when nothing set the time | `None` | `_no_ts` → earliest real ts of the ingest call, else `1970-01-01T00:00:00+00:00` |
  Invalid calendar values from `_iso` (dev `Sep 31`, TP month 13…) → `parse_timestamp` → `None` → `_no_ts`.
  No year-less SAP format exists, so the "current year" clock does not affect this slice.
* Attribute value types: all `str`, except **`avail.duration_min` (int)** and **`jvm.class_count` (int)**; no floats,
  bools, `None` or nested values are ever produced. Canonical JSON: ints plain. Empty strings (`gc.tags`, `sap.wp_no`,
  `sap.method`, `sap.user`…) are dropped by the `v not in (None, "")` filter. Remember the 01 §9 rule: when
  `len(rec) <= len(used roles)` attributes are `{}` — e.g. `other` records without `service` in a file whose roles are
  timestamp/severity/service/message (golden `mut__kafka_log4j.log`, 85 rows with `{}`).
* Exact record key orders (needed for report `keys`, `auto_map`, and attribute iteration):

  | `sap.format` | keys in order |
  |---|---|
  | `nw-java` | `timestamp, severity, origin, sap.format, <meta except host>, message, <sap.category/sap.msg_id/sap.application/sap.logger in lead order>, [sap.user], [service]` |
  | `availability` | `timestamp, severity, service, message, avail.state, avail.end, avail.duration_min, sap.format` |
  | `hana` | `timestamp, severity, service, origin, message, sap.thread, sap.connection, sap.source, sap.format` |
  | `jvm-gc` (unified) | `timestamp, severity, service, message, sap.format, gc.tags, [gc.pause_ms]` |
  | `jvm-gc` (classic) | `timestamp, severity, service, message, sap.format, [gc.pause_ms]` |
  | `java-util-logging` | `timestamp, severity, origin, service, message, sap.method, sap.format` |
  | `dev-trace` | `timestamp, severity, message, sap.component, sap.format, service, <meta except host>, [host], [sap.thread], [sap.msg_code]` |
  | `sm21` | `timestamp, severity, service, message, sap.wp_type, sap.wp_no, sap.client, sap.user, sap.tcode, sap.msg_code, sap.format` |
  | `transport` | `timestamp, severity, service, message, sap.step, sap.rc, sap.format` |
  | `tp` | `timestamp, severity, service, message, sap.msg_code, sap.level, sap.format` |
  | `other` | `timestamp, severity, message, sap.format, [service]` |
  | `jvm-properties` | `timestamp, severity, sap.format, [service, sap.sid, sap.instance], message, jvm.<prop>…` |
  | `class-list` | `timestamp, severity, service, message, sap.format, jvm.class_count` |

  `meta` is itself ordered by first insertion: possible keys `sap.log_name, sap.sid, sap.instance, sap.formatter,
  sap.engineversion, sap.trc_file, sap.trc_level, sap.release, sap.pid, host` in whatever order the lines set them
  (re-assignment keeps position, `setdefault("host")` appends only once).

---

## 8. Stateful behaviour and what Go must do

| State (per `records()` call) | Set by | Read by | Notes |
|---|---|---|---|
| `cur_ts` | DEV_TS (4.14.2), JUL header (4.12), TP date/stamp (4.17) | dev records, TP records, `other`, class list | **not** set by NW Java, HANA, GC, SM21, ALOG, availability, JVM header |
| `cur_date` | date-only lines (4.18) | SM21 only | tuple of raw strings |
| `meta` | TRC_HEAD, DEV_META, LOGHEADER | NW Java (all but host), dev records, `other` (service only) | never reset inside a call |
| `pending_jul` | JUL header | next JUL level line anywhere later | cleared only on pairing / overwritten by next header |
| `nwj` | NW Java start | `#` lines and non-`#` lines until blank / new start / EOF | swallows everything non-blank |
| `jvmprop` | JVM header (latest wins) | every later `key = value` line | one record at EOF |
| `classes` | class lines | one record at EOF | |
| `_svc_from_logger` cache | — | — | pure; optional in Go |

Go design: a `sapState` struct created per `records()` call; ordered maps for `meta`, records and `_props`;
`pending_jul` as a pointer. No globals. Live batches each get fresh state (identical to Python because `live`/
`parse_golden` call `ingest_bytes` per batch). Consequences in live mode (not covered by goldens, all inputs that are
multi-batch contain no cross-line SAP structure): a dev trace batch without its `trc file:`/`sid` lines gets service
`sap-kernel` and `cur_ts = None` until its first timestamp line; an NW Java record cut by a batch boundary becomes two
records (the tail lines starting with `#` → `other`, unless they match something else); a JUL header at the end of a batch
emits nothing and its level line becomes `other`; a class list is summarised per batch.

---

## 9. Golden coverage

Inputs routed to `sap` (whole and live): the 9 `receiver/parse/testdata/inputs/sap__*` files (copies of
`samples/sap/*`) plus `mut__kafka_log4j.log` and `mut__iis_w3c.log`. All `sap__*` files are ≤ 40 lines, so their live
golden equals the whole golden (one batch). No file in `samples/corpus/` is routed to SAP.

| Branch | Golden inputs |
|---|---|
| STRONG marker → 1.0 | `sap__dev_w0` (TRC_HEAD), `sap__bootstrap.jvm` (JVMPROP_HEAD) |
| score by hits | all other `sap__*`, `mut__kafka_log4j.log`, `mut__iis_w3c.log` |
| score `+0.6` dev-line branch | **none** |
| 4.1 blank line closes NW Java | **none** (no blank lines in any SAP input) |
| 4.2 non-`#` continuation | **none** |
| 4.3 NW Java start, multi-line, `#` continuation, flush on next start, flush at EOF | `sap__defaultTrace.0.trc` |
| 4.3 single-line NW Java (`#` count ≥ 8 and trailing `#`) | **none** |
| 4.3 `#` line falling through | `sap__bootstrap.jvm` (header/separators), `mut__iis_w3c.log` (`#Software`, `#Version` → `other`) |
| 4.4 LOGHEADER (NAME / FORMATTER / ENGINEVERSION / other) | **none** |
| 4.5–4.6 JVM header + properties, 4.20.2 record with SID path | `sap__bootstrap.jvm` |
| 4.7 class lines, 4.20.3 class-list record (`cur_ts None`) | `sap__class_prefetch.lst` |
| 4.8 availability (incl. rounding, `ValueError`) | **none** |
| 4.9 HANA `i`/`w`/`e`/`f` | `sap__indexserver_sapprd01.30003.000.trc` (`d` not covered) |
| 4.10 GC unified: pause < 1000 (INFO), ≥ 5000 (ERROR), no tags / no pause | `sap__sapjvm_gc.jvm`, `mut__kafka_log4j.log` |
| 4.10 pause 1000–5000 (WARN), Full-GC elif (WARN), level tags warning/error/debug/trace | **none** |
| 4.11 GC classic, secs ≥ 5 (ERROR), pause_ms rounding | `sap__sapjvm_gc.jvm` (secs < 1, 1–5 s, no `secs` not covered) |
| 4.12–4.13 JUL header + INFO/SEVERE/WARNING, `com.sap` logger services | `sap__std_server0.out` (PM hour, method-less header, unpaired header, CONFIG/FINE* not covered) |
| 4.14 TRC_HEAD, DEV_TS (with ms), separator, `*` decoration, META sid/sysno/relno/profile/pid, THR, LOG/ERROR/WARNING, service before/after sid | `sap__dev_w0` |
| 4.14 META nodename/hostname, `***` without `=>` (`elif` ERROR), skip `not cur_ts and not body`, DEV_TS without ms, LOG with error/warn words | **none** |
| 4.15 SM21 with `cur_date` | `sap__sm21_export.file` (`cur_date` missing → `None` not covered) |
| 4.16 ALOG STOP / ERR | `sap__SLOG2638.PRD.lst` (WRN / START not covered) |
| 4.17 tp / R3trans (date, stamp, E/W flags) | **none** |
| 4.18 date-only line | `sap__sm21_export.file` |
| 4.19 `other` with `cur_ts None`, no service | `mut__kafka_log4j.log` (85 rows, attrs suppressed), `mut__iis_w3c.log` (241 rows, all `1970` fallback) |
| 4.19 `other` with sid service / with time | **none** |
| `_sev_from_words` → ERROR / WARN | **none** (only default paths are reached) |
| `_finish_nwj`: category, logger, user, service via `_svc_from_logger` | `sap__defaultTrace.0.trc` |
| `_finish_nwj`: msg id, application, `/`-origin service, empty origin, empty body | **none** |
| `_svc_from_logger` non-`com.sap` / single-part | **none** |
| live: multi-batch with cached sap hint | `mut__kafka_log4j.log`, `mut__iis_w3c.log` (6 batches each) |

### Branches NOT covered by any golden (write unit tests from the probed outputs in §4–§5)

* `sap_score` 0.6 branch and the `not lines` → 0.0 branch.
* LOGHEADER meta (`sap.log_name`, `sap.sid`/`sap.instance` from the path, `sap.formatter`, `sap.engineversion`).
* NW Java: blank-line termination, non-`#` continuation lines (stack traces), single-line records, `sap.msg_id`,
  `sap.application`, `/System/...` origin services, records without tz, empty origin (no `service`).
* Availability log entirely (including the int `avail.duration_min`, ties-to-even rounding and the `ValueError`).
* tp / R3trans / SUM lines entirely (`cur_ts` from `dd.mm.yyyy - HH:MM:SS` and 14-digit stamps).
* HANA `d` level; GC level tags, 1000–5000 ms WARN, Full-GC WARN, classic < 1 s / 1–5 s.
* JUL: PM/12 AM conversion, header without method, unpaired / overwritten headers, CONFIG/FINE/FINER/FINEST.
* Dev trace: `nodename`/`hostname`, `***` without `=>`, LOG lines with error/warn words, lines before any timestamp with
  empty body, `sap-kernel` default service, META swallowing ordinary text.
* SM21 without a preceding date line (`timestamp None`); ALOG `WRN`/`START`.
* All Unicode-specific behaviour (§2): non-ASCII digits, Python-only whitespace, `ı`/`İ` folding, `\b` next to non-ASCII
  letters, `pyLower` of `İ` in service names.
