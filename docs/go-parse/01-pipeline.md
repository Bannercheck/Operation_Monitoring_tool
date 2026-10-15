# Go parse port, part 01: pipeline entry points and orchestration

Scope: everything between "raw bytes arrive" and "list of canonical observations + per-file report", except the
individual parsers' `records()` implementations, `normalize.*` (timestamp / severity / environment / auto_map internals),
`grok.detect`, `sap_parser.sap_score` and `parsers/family.py` patterns, which are separate slices. Where this slice calls
into those, the contract that this slice relies on is stated.

Python sources covered (all under `src/watchover/`):

| File | Functions |
|---|---|
| `loader.py` | `decode`, `iter_bytes`, `_Budget`, `iter_path` |
| `pipeline.py` | `ingest_bytes`, `ingest`, `_workers`, `_parse_one`, `_sharepoint_uls`, `dedupe`, `fill_missing_timestamps`, `ingest_path` |
| `format_detector.py` | all regexes, `detect_delimiter`, `detect_format` |
| `parsers/__init__.py` | `PARSERS` registry |
| `parsers/base.py` | `Parser.parse` |
| `parsers/common.py` | `records_to_observations`, `HEAD`, `MISSING_TS` |
| `parsers/family.py` | only the two contextvars `FAMILY_HINT` / `FAMILY_SEEN` and `detect_family`'s hint handling |
| `profiles.py` | `signature`, `lookup`, `remember` (the parse-time profile store) |
| `stamp.py` | `file_id` (the only part used by parsing) |
| `tables.py` | `is_side_table`, `read_table` |
| `models.py` | `Observation`, `SEV_RANK` |
| `live.py` | `LiveStore.ingest` / `_ingest` up to and including `ingest_bytes`, and the `_detect` hint cache |
| `scenario/__init__.py` | constants read by this slice: `MAPPING`, `SIDE_TABLES`, `DEDUP_KEY`, `DEDUP_PREFER` |
| `scripts/parse_golden.py` | how the goldens were produced (whole run vs live run) |

Verification done while writing this spec (so the facts below are measured, not guessed):

* Re-running `scripts/parse_golden.py` today (2026-10-15, fresh `WATCHOVER_HOME`) reproduces all 240 golden files
  byte-for-byte (compared decompressed).
* Re-running it with `profiles.lookup` forced to return `None` produces identical observation lines for all 240
  files; only the report field `"profile"` changes. So learned profiles affect only the report in the goldens (details in
  [Profiles](#profiles-learned-column-mappings)).
* Line-coverage and branch probes were run over every golden input (whole and live) to fill the coverage tables at the
  end.
* The Go RE2 rewrites in 8.1 and the `LEVEL_WORD_RE` rewrite in 9 (with `notLevel`) were compiled with Go 1.24 and
  compared with Python on all 61 743 lines of every golden input plus 14 hand-written edge cases (`ınfo`, `İNFOx`,
  `WARNINGS`, `<Eventé`, `<Event>xmlns=`, `ſevere`, Kelvin sign, ...): match booleans, `KV_RE` match counts and all
  `SYSLOG_RE` captures were identical (0 diffs).

---

## 0. Call graph

```
live.LiveStore.ingest(name, data, agent, ...)            # HTTP POST /ingest body, name = basename(X-File-Name)[:200]
 └─ _ingest: hint = _detect[(agent, name)] (+ refresh rule)
     └─ pipeline.ingest_bytes(name, data, hint=hint)
         └─ pipeline.ingest(loader.iter_bytes(name, data), mapping=None, progress=None, hint)
             ├─ items = list(iter_bytes(...))             # [(file name, decoded text)], archives expanded
             ├─ for each item: _parse_one(fname, text, mapping, progress, base, span, hint)
             │    ├─ tables.is_side_table(fname) → table report, no observations
             │    ├─ format: hint["format"] or format_detector.detect_format(text)
             │    ├─ parser = PARSERS[fmt]; FAMILY_HINT/FAMILY_SEEN contextvars
             │    ├─ head = first 50 records of parser.records(probe) → keys, profile lookup, auto_map → roles (report only)
             │    ├─ rows = list(parser.parse(text, fname, mapping, conf))
             │    │      └─ Parser.parse → common.records_to_observations(records, ...)
             │    ├─ profiles.remember(...)
             │    ├─ SharePoint ULS post-processing (csv with EventID+Correlation+Area)
             │    └─ report dict
             ├─ dedupe(observations, report)              # scenario.DEDUP_KEY = "alarm_id"
             ├─ fill_missing_timestamps(observations)
             └─ observations.sort(key=timestamp)          # stable
```

`ingest_bytes(name, data, mapping=None, progress=None, hint=None)` is literally `ingest(iter_bytes(name, data), mapping, progress, hint)`.
`ingest_path(path)` is `ingest(iter_path(path), mapping)` (no hint).

---

## 1. Python semantics the Go port needs as helpers

Many rules below say "Python strip", "Python lower", "Python `\s`". Implement these once in Go and use them everywhere
in this slice (and the other slices).

### 1.1 Whitespace (`str.isspace`, `str.strip`, regex `\s`)

Python 3.11 `str.isspace()` / `str.strip()` / `str.split()` (no args) / regex `\s` on `str` all use the same set of
29 code points (measured):

```
U+0009 U+000A U+000B U+000C U+000D U+001C U+001D U+001E U+001F U+0020 U+0085 U+00A0 U+1680
U+2000-U+200A U+2028 U+2029 U+202F U+205F U+3000
```

Go differences: `strings.TrimSpace` / `unicode.IsSpace` do **not** include U+001C..U+001F; RE2 `\s` is only
`[\t\n\f\r ]` (no `\v`, no Unicode). Define:

```go
const pyWS = `\t\n\x0b\x0c\r\x1c-\x1f \x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}`
// regex fragments
const PS  = `[` + pyWS + `]`   // Python \s
const PNS = `[^` + pyWS + `]`  // Python \S
func pyIsSpace(r rune) bool    // membership in the set above
func pyStrip(s string) string  // strings.TrimFunc(s, pyIsSpace); also pyLStrip / pyRStrip
```

`bytes.strip()` (used on raw bytes in `live.py` / `parse_golden.py`) is different: it strips only ASCII
`b" \t\n\r\x0b\x0c"`.

### 1.2 `str.splitlines()`

Line boundaries (measured): `\n`, `\r`, `\r\n` (one boundary), `\x0b`, `\x0c`, `\x1c`, `\x1d`, `\x1e`, `\x85`, `\u2028`, `\u2029`.
`\x1f` is whitespace but **not** a line boundary. Separators are removed; a trailing separator does not produce a final
empty element; `""` gives `[]`; `"\n"` gives `[""]`; `"a\n\nb"` gives `["a", "", "b"]`.

```go
func pySplitLines(s string) []string {
    var out []string
    start := 0
    for i := 0; i < len(s); {
        r, w := utf8.DecodeRuneInString(s[i:])
        switch r {
        case '\n', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e', 0x85, 0x2028, 0x2029:
            out = append(out, s[start:i]); i += w; start = i
        case '\r':
            out = append(out, s[start:i]); i += w
            if i < len(s) && s[i] == '\n' { i++ }
            start = i
        default:
            i += w
        }
    }
    if start < len(s) { out = append(out, s[start:]) }
    return out
}
```

### 1.3 Code-point slicing

Python `text[:400_000]` and `s[:80]` slice by **code points**, not bytes. Go must cut after N runes
(`utf8.DecodeRuneInString` loop), never `s[:N]`.

### 1.4 `str.lower()`

Python uses the full Unicode lowercase mapping plus the Final_Sigma context rule. Differences from Go
`strings.ToLower` that matter:

* `U+0130` (İ) lowercases to **two** code points `"i\u0307"` in Python; Go gives `"i"`. So in Python
  `"İNVENTORY".lower()` does **not** contain `"inventory"`, `"ALERT"`-style checks behave the same only for ASCII.
* `U+03A3` (Σ) becomes `ς` at the end of a word (preceded by a cased letter, not followed by one), else `σ`; Go always `σ`.
* Unicode version: Python 3.11 = Unicode 14.0.0, Go 1.24 = Unicode 15.0. Ignore (no affected characters in practice).

```go
func pyLower(s string) string // rune loop: 0x130 -> "i\u0307"; 0x3A3 -> final-sigma rule; else unicode.ToLower(r)
```

Used in this slice by: `iter_bytes` (extension tests), `tables.is_side_table`, `stamp.file_id`,
`records_to_observations` (`source.lower()`), `_sharepoint_uls` (`c.lower()`).

### 1.5 Character classes in Python regexes (str patterns, no `re.ASCII`)

Measured on Python 3.11.17 over all code points:

| Python | exact Go RE2 equivalent |
|---|---|
| `\d` | `\p{Nd}` |
| `\D` | `\P{Nd}` |
| `\w` | `[\p{L}\p{N}_]` (exactly equal: `\w` == `str.isalnum() or "_"`) |
| `\W` | `[^\p{L}\p{N}_]` |
| `\s` / `\S` | `PS` / `PNS` above |
| `\b` | boundary between `[\p{L}\p{N}_]` and not-that (or string edge). RE2 `\b` is ASCII-only: **never use RE2 `\b`**; use the explicit rewrites given per regex. |
| `.` | any code point except `\n` (same as Go without `(?s)`) |
| `$` (no MULTILINE) | end of string **or before a final `\n`**. All lines in this slice come from `splitlines()` and contain no `\n`, so `$` == Go `$` (`\z`). |

Case-insensitive matching (`re.I` / `(?i:...)`): Python's ignorecase lets `I`/`i` also match `ı` (U+0131) and `İ` (U+0130),
`S`/`s` match `ſ` (U+017F) and `K`/`k` match `K` (U+212A). Go `(?i)` handles `ſ` and `K` but **not** `ı`/`İ` (measured:
`(?i)info` does not match `"ınfo"` in Go, does in Python). Rule for every case-insensitive Go regex in this port: write
each letter `I`/`i` as `[Iiıİ]` (i.e. `[Ii\x{130}\x{131}]`). Likewise, under `re.I` the class `[A-Za-z]` also contains
`ı İ ſ K`; Go `(?i)[A-Za-z]` contains `ſ K` but not `ı İ`, so write it as `[A-Za-z\x{130}\x{131}\x{17f}\x{212a}]`.

Submatch semantics: Go's `regexp` uses leftmost-first (Perl-like) preference, so for every regex here that RE2 can
compile, capture groups equal Python's backtracking result. Only lookaround / backreference / possessive constructs
need rewriting.

### 1.6 Other Python builtins used on values

* `str.isdigit()`: non-empty and every char is `\p{Nd}` **or** one of the Numeric_Type=Digit code points:
  `U+00B2-U+00B3 U+00B9 U+1369-U+1371 U+19DA U+2070 U+2074-U+2079 U+2080-U+2089 U+2460-U+2468 U+2474-U+247C U+2488-U+2490 U+24EA U+24F5-U+24FD U+24FF U+2776-U+277E U+2780-U+2788 U+278A-U+2792 U+10A40-U+10A43 U+10E60-U+10E68 U+11052-U+1105A U+1F100-U+1F10A`
  (measured; `isdecimal()` == `\p{Nd}` exactly).
* `str.isalpha()`: non-empty and every char in `\p{L}` (Lu, Ll, Lt, Lm, Lo). Go `unicode.IsLetter` is the same set.
* `float(s)`: accepts any `\p{Nd}` digits (e.g. `float("١٢.٥") == 12.5`), surrounding Python whitespace, `_` separators
  between digits, `inf`/`nan` spellings. Normalise Nd digits to ASCII (`unicode.Digit`-value) before `strconv.ParseFloat`.
* `str(x)` of record values (used for message/service/host/origin and `" ".join(f"{k}={v}")`):
  `str` → itself; `int` → decimal; `bool` → `"True"`/`"False"`; `None` → `"None"` (only reachable in the `k=v` join);
  `float` → Python repr (1.7); `dict`/`list` → Python repr (`{'a': 1}`, `['x', 2]`; single quotes, `True`, `None`) —
  no golden input produces nested values in these positions (parsers flatten).
* `round(x, n)` on floats: correctly rounded from the exact binary value, ties-to-even on exact ties.
  Go equivalent: `strconv.ParseFloat(strconv.FormatFloat(x, 'f', n, 64), 64)` (measured identical for 0.605→0.6,
  0.615→0.61, 0.625→0.62, 0.125→0.12).
* Truthiness: `""`, `0`, `0.0`, `False`, `None`, empty containers are falsy. `x or ""` therefore turns `0`, `0.0`
  and `False` into `""`.
* `v not in (None, "")`: `True` for `0`, `False`, `0.0` (Python `==` comparison: `0 == ""` is False). Only `None` and
  the empty string are excluded.

### 1.7 Python float repr (used by `str(float)` and by `json.dumps`)

Python `repr(float)`: shortest round-trip digits (same digits as Go `strconv.FormatFloat(f, 'e', -1, 64)`); let
`decpt = exponent + 1` (position of the decimal point). If `-4 < decpt <= 16` print fixed notation with at least one
fractional digit (`1.0`, `100000.0`, `0.0001`, `245.12`, `1000000000000000.0`, `-0.0`); otherwise scientific with the
shortest mantissa, no forced `.0`, exponent sign and at least two digits (`1e+16`, `1e-05`, `3e-07`,
`1.2345678901234567e+19`). Specials: `str()` gives `nan`, `inf`, `-inf`; `json.dumps` gives `NaN`, `Infinity`,
`-Infinity`. Go's `json.Marshal` differs (`1`, `10000000000000000`, `0.00001`, `3e-7`): **do not use it**.

```go
func pyFloatRepr(f float64) string {
    e := strconv.FormatFloat(f, 'e', -1, 64)          // "d.ddde±XX"
    mant, exp := splitE(e); decpt := exp + 1
    if decpt > -4 && decpt <= 16 {
        s := strconv.FormatFloat(f, 'f', -1, 64)
        if !strings.ContainsAny(s, ".") { s += ".0" }
        return s
    }
    return mant + "e" + sign(exp) + twoDigitsMin(abs(exp))  // Go's 'e' output already has this form
}
```

---

## 2. Canonical golden output (what parity means)

From `scripts/parse_golden.py`:

```python
def canon(o):
    return {"ts": o.timestamp.isoformat(), "message": o.message, "kind": o.kind, "severity": o.severity,
            "service": o.service, "host": o.host, "environment": o.environment, "origin": o.origin,
            "attributes": o.attributes, "source": o.source, "line_no": o.line_no, "parser": o.parser,
            "parser_confidence": round(float(o.parser_confidence), 4), "raw": o.raw}
# file = gzip text: json.dumps({"report": report}, sort_keys=True, ensure_ascii=False, default=str) + "\n"
#                   then json.dumps(canon(o), sort_keys=True, ensure_ascii=False, default=str) + "\n" per observation
```

The report entries are written without the keys `"sha"` and `"table"`.

### 2.1 JSON encoding rules (Python `json.dumps(..., sort_keys=True, ensure_ascii=False, default=str)`)

* Separators: `", "` between items and `": "` between key and value. Single line. No trailing spaces.
* Keys sorted at every nesting level by code point (Go: byte-wise compare of UTF-8 strings gives the same order).
* Strings: `"` → `\"`, `\` → `\\`, `\n` `\r` `\t` `\b` `\f` → short escapes, every other char < U+0020 → `\u00xx`
  (lowercase hex). **Everything else is written raw**, including U+007F, U+2028, U+2029, `<`, `>`, `&` and all non-ASCII.
  (Go `json.Marshal` escapes `<>&` and U+2028/2029: write a custom encoder.)
* `int` → decimal (arbitrary precision; values come from JSON input, so int64 is not guaranteed: keep `json.Number`
  text for big integers). `bool` → `true`/`false`. `None` → `null`. `float` → 1.7 (`NaN`/`Infinity` literals allowed).
* `default=str`: any non-JSON value is written as the JSON string of `str(value)` (not reached by goldens: all
  attribute values in the goldens are `str`, `int`, `bool` or `float`; no nested dict/list values; counted:
  100297 str, 6791 int, 2197 bool, 1911 float).
* Python keeps int vs float distinct: `100.0` is written `100.0`, `100` is written `100`. Go must carry the original
  JSON number kind from parsing (an int-looking JSON literal is a Python `int`; anything with `.`/`e`/`E` is a `float`).

### 2.2 `ts` format (`datetime.isoformat()`)

`YYYY-MM-DDTHH:MM:SS[.ffffff]±HH:MM`:
* microseconds printed (exactly 6 digits) only when non-zero;
* the offset is always printed and is the datetime's **own** offset (goldens contain `+00:00` and `+03:00`; the
  pipeline never converts to UTC); UTC is `+00:00`, never `Z`; an offset with seconds would print `±HH:MM:SS`.
* Go: `t.Format("2006-01-02T15:04:05")` + (`fmt.Sprintf(".%06d", t.Nanosecond()/1000)` if non-zero) +
  `t.Format("-07:00")`. Keep microsecond precision only (Python datetimes have no nanoseconds; round/truncate exactly as
  the timestamp slice specifies).

Measured over all goldens: ts shapes are only `YYYY-MM-DDTHH:MM:SS+HH:MM` (19489) and `...SS.ffffff+HH:MM` (15405).

### 2.3 `parser_confidence` and report `confidence`

* Observation: `round(float(conf), 4)` written as Python float (`1.0`, `0.95`, `0.61`, `1.2`, `1.23`).
* Report entry `"confidence"`: the raw `conf` value (detector's value or `float(hint["confidence"])`), **not** rounded
  to 4. It is a Python float except the table branch (`1.0`) — all are written as floats (`1.0`, never `1`).

### 2.4 Ordering

* Whole run: observations in the order returned by `ingest` (stable sort by timestamp instant, see 6.4).
* Live run: the concatenation of each batch's `ingest` result (each batch sorted on its own).

### 2.5 Report entry keys (event file)

`confidence`, `family` (`""` when none), `file`, `format`, `keys` (list of record keys in first-seen order over the first
50 probe records), `kind` (kind of the first parsed row, `"-"` if no rows), `profile` (`""` or a 16-hex profile id),
`roles` (object with the seven role names → key name or `null`), `rows` (count before dedupe), plus `dedup_dropped`
(int) only when dedupe dropped rows of that file. `sha` (stamp.file_id) exists in Python but is stripped from goldens.

---

## 3. `loader.decode(data: bytes) -> str | None`

```python
def decode(data):
    if b"\x00" in data[:4096] and not data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return None
    for enc in ("utf-8-sig", "utf-16", "latin-1"):
        try: return data.decode(enc)
        except UnicodeDecodeError: continue
    return data.decode("utf-8", errors="replace")   # unreachable: latin-1 never fails
```

Exact semantics for Go:

1. **Binary guard**: if any `0x00` byte occurs in the first 4096 bytes **and** the first two bytes are not
   `FF FE` / `FE FF`, the file is binary → return "no text" (the file is silently dropped: no observations, **no report
   entry**). A BOM-less UTF-16 file therefore is dropped; a UTF-8 file containing NUL early is dropped.
2. **utf-8-sig**: strict UTF-8 (reject overlong forms, surrogates U+D800-DFFF, > U+10FFFF, truncated sequences — same as
   Go `utf8.Valid`), after removing **one** leading `EF BB BF` if present. Interior U+FEFF are kept. No newline
   translation.
3. **utf-16** (only if step 2 failed): if the data starts with `FF FE` → little-endian, BOM removed; `FE FF` → big-endian,
   BOM removed; **no BOM → little-endian** (native order on x86/arm64 hosts; Go must hard-code LE). Fails (→ step 4) if
   the remaining length is odd, or a high surrogate is not followed by a low surrogate, or a low surrogate appears alone.
   Consequence to reproduce faithfully: **any non-UTF-8 input with an even byte count and no bad surrogate pairs is decoded
   as UTF-16LE mojibake** (e.g. cp1254/latin-1 Turkish text of even length becomes CJK-looking text). Example measured:
   `b"caf\xe9 ok!"` (8 bytes) → `'慣\ue966漠Ⅻ'`. Only odd-length data or data with invalid surrogates reaches latin-1.
   A UTF-32LE file with BOM (`FF FE 00 00`) passes the NUL guard and decodes as UTF-16 with NUL characters.
4. **latin-1**: each byte → the code point of the same value. Always succeeds.

Golden coverage: every golden input is valid UTF-8 without BOM (checked) — steps 1, 3, 4 and the BOM strip are **not
covered**.

## 4. `loader.iter_bytes(name, data, _budget=None)` → `[(name, text)]`

Checks in this exact order, on `lower = pyLower(name)`:

1. `lower.endswith(".zip")` → open as ZIP (Python `zipfile`). For each entry in **central-directory order**
   (`infolist()`):
   * skip if `is_dir()` (name ends with `/`) or the name starts with `__MACOSX` (case-sensitive, raw name);
   * if the declared uncompressed size `file_size > 512 MiB` → raise `ArchiveTooBig` (whole ingest fails);
   * recurse: `iter_bytes(info.filename, zf.read(info), budget)` — the recursion uses the **full inner path** (e.g.
     `logs/app.log`) as the name, so nested archives, `.gz` members and binary-extension filtering apply recursively,
     and `source` of the resulting observations is that inner path.
   * Name decoding: if general-purpose flag bit 11 (0x800) is set the raw name is UTF-8, **otherwise it is decoded as
     cp437** (even if the bytes happen to be valid UTF-8). Go's `archive/zip` leaves non-flagged names as raw bytes:
     convert with a cp437 table to match.
   * Errors (corrupt archive, encrypted member, CRC mismatch, unsupported compression) raise and abort the ingest
     (`live.py` HTTP handler → 400 `{"error": str(e)}`).
2. `lower.endswith((".tar.gz", ".tgz", ".tar"))` → `tarfile.open(mode="r:*")` (transparent gzip / bz2 / xz / none).
   For each member in archive order (`getmembers()`): skip unless `isfile()` (regular file types: REGTYPE `'0'`,
   AREGTYPE `'\0'`, CONTTYPE `'7'`, GNU sparse); declared `size > 512 MiB` → `ArchiveTooBig`; recurse with `m.name`.
3. `lower.endswith(".gz")` → `gzip.decompress(data)` (handles multi-member concatenated gzip; Go `gzip.Reader` with
   multistream on matches), `budget.take(len(raw))`, then recurse with `name[:-3]` (the **original-case** name minus its
   last 3 code points). Note the recursion then calls `budget.take` again for the same bytes (double count; harmless,
   keep the same limits). A `.gz` name whose body is not gzip raises (`BadGzipFile`) → ingest fails.
4. `Path(lower).suffix in BINARY_EXT` → nothing yielded. `BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".pdf",
   ".parquet", ".xlsx", ".xls", ".db", ".sqlite", ".pkl", ".so"}`. `Path.suffix` = the last `.xxx` of the final `/`
   component; a name that starts with a dot and has no other dot (`.pdf`) has suffix `""`; `x.` has suffix `""`.
5. `budget.take(len(data))`, `text = decode(data)`; yield `(name, text)` if not `None`.

`_Budget.take(n)`: `members += 1`; `members > 20000` → `ArchiveTooBig("too many files in archive (> 20000)")`;
`n > 512 MiB` → `ArchiveTooBig`; `total += n`; `total > 1 GiB` → `ArchiveTooBig`. One budget per top-level
`iter_bytes` call.

Other extensions (`.bz2`, `.xz`, `.7z`, `.rar`) are treated as plain files (usually dropped by the NUL guard).
`iter_path(path)`: directory → every file under it, `sorted(rglob("*"))`, name = path relative to the directory; file →
`iter_bytes(basename, bytes)`.

Live path: agents post raw bytes; `name = Path(X-File-Name or "agent.log").name[:200] or "agent.log"` (base name only).
A name ending in `.zip`/`.gz`/`.tar...` triggers archive handling of the POST body.

Golden coverage: only branch 5 with UTF-8 text. Branches 1-4, the budget errors and cp437 names are **not covered**.

---

## 5. `pipeline.ingest(files, mapping=None, progress=None, hint=None)`

```python
mapping = mapping or scenario.MAPPING or None        # scenario.MAPPING == {}  → None in practice
items = list(files)
total_bytes = sum(len(t) for _, t in items) or 1     # code points; progress only
n = _workers(items)
...parallel path or sequential path → results = [(rows, rep) per item, in item order]
observations = concat(rows); report = [rep ...]
observations = dedupe(observations, report)
fill_missing_timestamps(observations)
observations.sort(key=lambda o: o.timestamp)
return observations, report
```

* **Parallel path** (`_workers(items) > 1`): only when `WATCHOVER_PARALLEL != "0"` and `len(items) >= 3` and total text
  length `>= 32 MiB` and (`WATCHOVER_PARALLEL == "1"` or `os.cpu_count() >= 8`); workers `= max(1, min(len(items),
  cpus-2, 8))`. Results are re-assembled in input order, so output is identical to sequential, **except** that the
  parallel call does **not** pass `hint` (nor progress) to `_parse_one`, and profiles are remembered in worker processes
  (race on `profiles.json`). Any exception in the pool silently falls back to the sequential path (which does pass the
  hint). Go: always behave like the sequential path, but if exact parity with this quirk is wanted, ignore `hint` when
  the same parallel conditions hold. Not covered by goldens (all inputs are single files).
* `progress` only reports; it never changes output.
* `ingest` with zero items (all files binary/dropped) returns `([], [])`.

## 6. `pipeline._parse_one(fname, text, mapping, progress=None, base=0.0, span=1.0, hint=None)`

Step by step:

1. **Side table**: `if tables.is_side_table(fname)` → `rows = tables.read_table(fname, text)` and return
   `([], {"file": fname, "format": "table", "confidence": 1.0, "rows": len(rows), "kind": "table",
   "keys": list(rows[0].keys()) if rows else [], "roles": {}, "table": rows, "sha": file_id(fname, text)})`.
   No `family`, no `profile` keys. (See 11.)
2. **Format**: `if hint and hint.get("format") in PARSERS:` → `fmt = hint["format"]`, `conf = float(hint.get("confidence", 0.9))`
   (no detection at all). Otherwise `fmt, conf = detect_format(text)`. (`hint` falsy = `None` or `{}`.)
3. `parser = PARSERS[fmt]`.
4. **Family contextvars**: `tok_hint = FAMILY_HINT.set(hint["family"]) if hint and "family" in hint else None` — note the
   family hint is set whenever the key exists, **even if it is `""`** and even if the format hint was not used.
   `tok_seen = FAMILY_SEEN.set(None)`.
5. **Probe**: `probe = text[:400_000]` (first 400 000 **code points**) if `parser.line_oriented` else `text`.
   `head = [r for _, r in islice(parser.records(probe), 50)]` (first 50 records). Generators: the probe run may stop
   before the parser has consumed everything; any `detect_family` call made during it is overwritten by the full run.
6. `keys`: union of record keys in first-seen order over `head`.
7. **Profile lookup**: `learned = None`; if `mapping is None and keys and fmt in ("json","jsonl","csv","kv","grok")`:
   `learned = profiles.lookup(fmt, keys)`; if found, `mapping = dict(learned["mapping"])` (this mapping is then also used
   by the full parse). See [Profiles](#profiles-learned-column-mappings).
8. `roles = auto_map(keys, head, mapping)` — only used for the report and for `remember`; the parse computes its own
   roles from the full text's first 50 records (same thing unless a line-oriented file is longer than 400 000 code points
   and a record straddles the cut).
9. **Parse**: `rows = list(parser.parse(text, fname, mapping, conf))` (progress variant produces the same rows).
10. **Remember**: if `keys and fmt in (json, jsonl, csv, kv, grok)`:
    `profiles.remember(fmt, keys, {k: v for k, v in roles.items() if v}, name=fname, source="user" if (mapping and not learned) else "auto")`;
    exceptions swallowed.
11. **SharePoint ULS**: `if fmt == "csv" and {"EventID", "Correlation", "Area"} <= {pyStrip(k) for k in keys}:` →
    `_sharepoint_uls(rows)` (section 7).
12. `family = FAMILY_SEEN.get()`; reset both contextvars (`FAMILY_HINT.reset(tok_hint)` only if it was set).
13. Return `rows` and the report
    `{"file": fname, "format": fmt, "confidence": conf, "rows": len(rows), "family": family or "",
      "kind": rows[0].kind if rows else "-", "keys": keys, "roles": roles, "profile": (learned or {}).get("id", ""),
      "sha": stamp.file_id(fname, text)}`.

Go design for step 4/12: no globals. Pass `familyHint *string` explicitly into the parser (only `TextParser` calls
`detect_family`, `text_parser.py:114`), and have the parser return the decided family. Semantics to keep:
`detect_family(lines)`: if a hint is set (non-nil, possibly `""`) return it and record it as seen; otherwise detect and
record. The report's `family` is the last recorded value of the **full** parse, `""` if the parser never asked
(every non-text format).

### PARSERS registry (`parsers/__init__.py`)

`PARSERS = {p.name: p for p in (JsonParser(), JsonlParser(), CsvParser(), SyslogParser(), SapParser(), KvParser(), TextParser(), AccessParser(), CefParser(), WinEvtParser(), W3cParser(), GrokParser())}`

| name | class | `line_oriented` | default `kind` |
|---|---|---|---|
| json | JsonParser | **False** | event |
| jsonl | JsonlParser | True | event |
| csv | CsvParser | True | event |
| syslog | SyslogParser | True | log |
| sap | SapParser | True | log |
| kv | KvParser | True | log |
| text | TextParser | True | log |
| access | AccessParser | True | log |
| cef | CefParser | True | event |
| winevt | WinEvtParser | **False** | event |
| w3c | W3cParser | True | log |
| grok | GrokParser | True | log |

The registry is stateless (parser instances hold no per-call state). `hint["format"]` must be one of these 12 names
to be honoured; `"table"` never reaches the hint (excluded in `live.py`).

### `Parser.parse` (`parsers/base.py`)

```python
def parse(self, text, source, mapping=None, confidence=1.0):
    lines = text.splitlines() if self.line_oriented else None
    yield from records_to_observations(self.records(text), source, self.name, self.kind, mapping, confidence, lines)
```

`records(text)` yields `(line_no, record_dict)`; `line_no` is 1-based into `pySplitLines(text)` for line-oriented
parsers, a record index for json/winevt. No parser overrides `parse`.

---

## 7. `pipeline._sharepoint_uls(rows)` (includes the "Request (GET:...)" post-processing)

Triggered only from step 11 (csv format with stripped keys ⊇ `EventID`, `Correlation`, `Area`). For each observation
`o`, in order:

1. `cols = [pyStrip(c) for c in (o.raw or "").split("\t")]` (split on TAB only, empty parts kept).
2. `lvl` = the first `c` in `cols` with `pyLower(c)` in `ULS_LEVELS`; if found `o.severity = ULS_LEVELS[pyLower(lvl)]`.
   `ULS_LEVELS = {"critical": "CRITICAL", "unexpected": "ERROR", "monitorable": "WARN", "high": "INFO", "medium": "INFO", "verbose": "DEBUG", "verboseex": "DEBUG"}`.
   Any column can hit (including the message column if it is exactly e.g. `High`). `_sev_src` is **not** updated.
3. `area = pyStrip(str(o.attributes.get("Area", "")))` — `""` if `Area` became a role (not in attributes).
4. `cat = cols[i]` for the first `i > 0` with `cols[i-1] == area`, else `""` (only computed if `area`).
5. If `area`: `o.service = ("sharepoint:" + cat if cat else "sharepoint")[:80]` (80 code points).
6. Every attribute whose value is a `str` is replaced by `pyStrip(value)` (all keys, including `severity_raw` and
   `_sev_src`; non-str values untouched). Done for every row regardless of `area`.
7. `re.search(r"Execution Time=(\d+(?:\.\d+)?)", o.message or "")` → `o.attributes["duration_ms"] = float(m[1])`.
   Go: `Execution Time=(\p{Nd}+(?:\.\p{Nd}+)?)`; convert Nd digits to ASCII before `ParseFloat`. Stored as float
   (JSON `245.12`, `3.0`).
8. `re.search(r"Request \((GET|POST|PUT|DELETE|PATCH|HEAD):(\S+?)\)", o.message or "")` →
   `o.attributes["http.method"] = m[1]`, `o.attributes["url"] = m[2]`. Go (RE2 supports lazy):
   `Request \((GET|POST|PUT|DELETE|PATCH|HEAD):(PNS+?)\)` — `PNS` from 1.1. Case-sensitive.

Golden coverage: **none** (no golden CSV has `EventID`+`Correlation`+`Area`). Write a dedicated Go+Python fixture.

---

## 8. `format_detector`

### 8.1 Regexes (verbatim) and RE2 status

`_SYSLOG_TS = r"(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}(?:\s\d{4})?\s\d{2}:\d{2}:\d{2}|\d{4}-\d{2}-\d{2}T[\d:.+\-Z]+)"`
`_NOT_LEVEL = r"(?!(?i:TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERR|ERROR|CRIT|CRITICAL|FATAL|SEVERE|INF|WRN|FTL|DBG|VERBOSE)\b)"`

| # | Name | Python pattern (verbatim) | RE2 as is? |
|---|---|---|---|
| R1 | `SYSLOG_RE` | `^(?:<(?P<pri>\d+)>)?` + `_SYSLOG_TS` + `\s+(?P<host>` + `_NOT_LEVEL` + `\S+)\s+(?P<prog>[^:\[\s]+)(?:\[(?P<pid>\d+)\])?:\s*(?P<msg>.*)$` | **No** (negative lookahead; Unicode `\b \d \s \S`) |
| R2 | `SYSLOG_NOPROG_RE` | `^(?:<(?P<pri>\d+)>)?` + `_SYSLOG_TS` + `\s+(?P<host>` + `_NOT_LEVEL` + `[A-Za-z][\w.\-]*)\s+(?P<msg>.*)$` | **No** (lookahead; Unicode `\w`) |
| R3 | `RFC5424_RE` | `^<(?P<pri>\d+)>1 (?P<ts>\S+) (?P<host>\S+) (?P<app>\S+) (?P<pid>\S+) (?P<msgid>\S+) (?P<sd>-\|(?:\[.*?\])+) ?(?P<msg>.*)$` | compiles; needs Unicode classes |
| R4 | `CEF_RE` | `^(?:.*?\s)?(?:CEF:\d+\|\|LEEF:\d+(?:\.\d+)?\|)` | compiles; needs Unicode classes |
| R5 | `CLF_RE` | `^\S+ \S+ \S+ \[\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2} [+\-]\d{4}\] "` | compiles; needs Unicode classes |
| R6 | `ALB_RE` | `^(?:https?\|h2\|ws\|wss\|tls\|tcp) \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z \S+ ` (trailing space) | compiles; needs Unicode classes |
| R7 | `ENVOY_RE` | `^\[\d{4}-\d{2}-\d{2}T[\d:.]+Z?\] "[A-Z]+ \S+ [^"]*" (?:\d{3}\|-) \S+ ` (trailing space) | compiles; needs Unicode classes |
| R8 | `WINEVT_RE` | `^\s*<Event\b[^>]*xmlns=` | compiles but RE2 `\b` is ASCII → **rewrite** |
| R9 | `W3C_RE` | `^#Fields:\s` | compiles; `\s` → `PS` |
| R10 | `CRI_LINE_RE` | `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z (?:stdout\|stderr) [FP] ` (trailing space) | compiles; `\d` → `\p{Nd}` |
| R11 | `KV_RE` | `([\w.\-@]+)=("(?:[^"\\]\|\\.)*"\|\S+)` | compiles; Unicode `\w \S` |

(In the table `\|` is a literal `|` of the regex, escaped for markdown.) R1, R2, R3 are also imported by
`syslog_parser.py` (captures used there), `detect_delimiter` by `csv_parser.py`; the rewrites below keep the captures.

Go rewrites (`PS`, `PNS` from 1.1; `D` = `\p{Nd}`; `W` = `[\p{L}\p{N}_]`). Names of groups kept (Go supports
`(?P<name>...)`):

```go
const tsAlt = `(?P<ts>[A-Z][a-z]{2}` + PS + `+\p{Nd}{1,2}(?:` + PS + `\p{Nd}{4})?` + PS + `\p{Nd}{2}:\p{Nd}{2}:\p{Nd}{2}` +
              `|\p{Nd}{4}-\p{Nd}{2}-\p{Nd}{2}T[\p{Nd}:.+\-Z]+)`
var R1 = regexp.MustCompile(`^(?:<(?P<pri>\p{Nd}+)>)?` + tsAlt + PS + `+(?P<host>` + PNS + `+)` + PS +
          `+(?P<prog>[^:\[` + pyWS + `]+)(?:\[(?P<pid>\p{Nd}+)\])?:` + PS + `*(?P<msg>.*)$`)
var R2 = regexp.MustCompile(`^(?:<(?P<pri>\p{Nd}+)>)?` + tsAlt + PS + `+(?P<host>[A-Za-z][\p{L}\p{N}_.\-]*)` + PS + `+(?P<msg>.*)$`)
// R1/R2 match only if additionally notLevel(line[hostStart:]) is true (the removed lookahead):
var levelWords = []string{"TRACE","DEBUG","INFO","NOTICE","WARN","WARNING","ERR","ERROR","CRIT","CRITICAL",
                          "FATAL","SEVERE","INF","WRN","FTL","DBG","VERBOSE"}
func notLevel(s string) bool {
    for _, w := range levelWords {
        if n, ok := pyFoldPrefix(s, w); ok {            // case-insensitive per 1.5 (I~i~ı~İ, S~s~ſ, K~k~K), rune-wise
            if n == len(s) { return false }             // \b at end of string
            r, _ := utf8.DecodeRuneInString(s[n:])
            if !isPyWord(r) { return false }            // \b: previous char (last letter of w) is a word char
        }
    }
    return true
}
```

Why the post-check is exact: in both R1 and R2 the start of `host` is forced (the timestamp alternatives cannot end
anywhere else — the BSD form is rigid, the ISO class `[\d:.+\-Z]` contains no whitespace and must be followed by `\s+`;
`\s+` is followed by a non-space), so if the lookahead fails at that position Python has no other way to match; the
alternation inside the lookahead is tried for every word (`WARN` failing `\b` on `WARNING` still lets `WARNING`
succeed), which `notLevel` reproduces by testing every word.

```go
var R3 = regexp.MustCompile(`^<(?P<pri>\p{Nd}+)>1 (?P<ts>` + PNS + `+) (?P<host>` + PNS + `+) (?P<app>` + PNS + `+) (?P<pid>` + PNS +
          `+) (?P<msgid>` + PNS + `+) (?P<sd>-|(?:\[.*?\])+) ?(?P<msg>.*)$`)
var R4 = regexp.MustCompile(`^(?:.*?` + PS + `)?(?:CEF:\p{Nd}+\||LEEF:\p{Nd}+(?:\.\p{Nd}+)?\|)`)
var R5 = regexp.MustCompile(`^` + PNS + `+ ` + PNS + `+ ` + PNS + `+ \[\p{Nd}{2}/[A-Z][a-z]{2}/\p{Nd}{4}:\p{Nd}{2}:\p{Nd}{2}:\p{Nd}{2} [+\-]\p{Nd}{4}\] "`)
var R6 = regexp.MustCompile(`^(?:https?|h2|ws|wss|tls|tcp) \p{Nd}{4}-\p{Nd}{2}-\p{Nd}{2}T\p{Nd}{2}:\p{Nd}{2}:\p{Nd}{2}\.\p{Nd}+Z ` + PNS + `+ `)
var R7 = regexp.MustCompile(`^\[\p{Nd}{4}-\p{Nd}{2}-\p{Nd}{2}T[\p{Nd}:.]+Z?\] "[A-Z]+ ` + PNS + `+ [^"]*" (?:\p{Nd}{3}|-) ` + PNS + `+ `)
// R8: \b after "Event" means: next char is not a word char (end of string cannot be followed by xmlns=),
// and it cannot be '>' (then [^>]* is empty and "xmlns=" would have to start at '>').
var R8 = regexp.MustCompile(`^` + PS + `*<Event[^\p{L}\p{N}_>][^>]*xmlns=`)
var R9 = regexp.MustCompile(`^#Fields:` + PS)
var R10 = regexp.MustCompile(`^\p{Nd}{4}-\p{Nd}{2}-\p{Nd}{2}T\p{Nd}{2}:\p{Nd}{2}:\p{Nd}{2}(?:\.\p{Nd}+)?Z (?:stdout|stderr) [FP] `)
var R11 = regexp.MustCompile(`([\p{L}\p{N}_.\-@]+)=("(?:[^"\\]|\\.)*"|` + PNS + `+)`)
```

`match` (Python `re.match`) = anchored at start: all patterns except R11 begin with `^`, so `MatchString` is fine.
R11 is used with `findall` → count of non-overlapping leftmost-first matches = `len(R11.FindAllStringIndex(ln, -1))`.

### 8.2 `detect_delimiter(lines)` (also used by `csv_parser`)

```python
for d in ("\t", ",", ";", "|"):
    try:
        counts = [len(row) for row in csv.reader(lines[:30], delimiter=d)]
    except csv.Error:
        continue
    if counts and min(counts) >= 2 and len(set(counts)) <= 2:
        return d
return None
```

`csv.reader` here is CPython 3.11's `_csv` reader with the `excel` dialect (quotechar `"`, doublequote on, no
escapechar, `skipinitialspace` off, `strict` off) fed with a **list of strings** (one per line, no line terminators).
Go's `encoding/csv` does not behave the same; implement this state machine to get the per-row field counts:

```
state = START_RECORD; field = ""; fields = []
for each input line L (in order):
    for each code point c of L, then one EOL sentinel:
        process(c)
    if state == START_RECORD: emit(fields) ; fields = []      # a record is complete
    # otherwise (unclosed quoted field) continue with the NEXT line, appending NOTHING for the line break
at end of input: if field != "" or state == IN_QUOTED_FIELD: save field; emit(fields)   # strict=False

process(c):
  START_RECORD: if c == EOL: return (empty line → emits an empty record [] ; cannot happen here: lines are non-blank)
                state = START_FIELD; fallthrough
  START_FIELD:  if c == EOL: save field; state = START_RECORD
                elif c == '"': state = IN_QUOTED_FIELD
                elif c == delim: save field (empty)
                else: field += c; state = IN_FIELD
  IN_FIELD:     if c == EOL: save; state = START_RECORD
                elif c == delim: save; state = START_FIELD
                else: field += c
  IN_QUOTED_FIELD: if c == EOL: (nothing)  elif c == '"': state = QUOTE_IN_QUOTED_FIELD  else: field += c
  QUOTE_IN_QUOTED_FIELD: if c == '"': field += '"'; state = IN_QUOTED_FIELD
                elif c == delim: save; state = START_FIELD
                elif c == EOL: save; state = START_RECORD
                else: field += c; state = IN_FIELD          # strict=False: text after a closing quote is kept
save: append field to fields, field = ""
field size limit: adding a char when len(field) already == 131072 raises csv.Error → this delimiter is skipped
```

(`\r`/`\n` inside a line would lead to EAT_CRNL and a `csv.Error`, but lines come from `splitlines()` and contain none.)
Measured examples: `['a,"b,c', 'd,e', 'f']` → `[['a', 'b,cd,ef']]` (one record: the quoted field swallows the next lines
without separators); `['a,"b"x,c', '"q""q",z']` → `[['a','bx','c'], ['q"q','z']]`. CPython 3.11 does not reject NUL.

Then `counts and min(counts) >= 2 and len(set(counts)) <= 2` — at most two distinct field counts.

### 8.3 `detect_format(text) -> (format, confidence)`

Exact order:

1. `lines = [ln for ln in pySplitLines(text[:400_000 code points])[:200] if pyStrip(ln) != ""]` — the **first 200
   physical lines**, then blank ones removed (the docstring's "first 200 non-empty lines" is inaccurate).
   If `lines` is empty → `("text", 0.0)`.
2. `first = pyLStrip(lines[0])`. If `first` starts with `[`, or (`first` starts with `{` and (`len(lines) == 1` or
   `pyRStrip(first)` does not end with `}`)): try `json.loads(text)` on the **entire** text (not the prefix); success →
   `("json", 0.95)`; `JSONDecodeError` → continue.
3. `ok = count(ln in lines where json.loads(ln) succeeds and is a dict)`. If `ok/len(lines) > 0.8` →
   `("jsonl", round(ok/len(lines), 2))`.
4. If any of `lines[:10]` matches R9 → `("w3c", 0.95)`.
5. If `count(R8 matches)/len > 0.6` → `("winevt", 0.95)`.
6. `cef = count(R4)`; `cef/len > 0.6` → `("cef", round(cef/len, 2))`.
7. `acc = count(R5 or R6 or R7)`; `> 0.6` → `("access", round(acc/len, 2))`.
8. `count(R10)/len > 0.6` → `("text", 0.9)` (Kubernetes CRI).
9. `hits = count(R1 or R3)`; `> 0.6` → `("syslog", round(hits/len, 2))`.
10. `hits2 = count(R2)` (counted independently, **overlapping** with `hits`); `(hits+hits2)/len > 0.6` →
    `("syslog", round((hits+hits2)/len, 2))`. **The confidence can exceed 1.0** (golden: `mut__sqlserver_errorlog.log`
    whole `1.2`, live `1.23`).
11. `sap = sap_score(lines)` (sap slice); `sap >= 0.5` → `("sap", sap)` (value as returned, not re-rounded here).
12. `lp, share = grok.detect(lines)` (grok slice; default `min_share=0.6`); `lp is not None and share >= 0.6` →
    `("grok", share)`.
13. `d = detect_delimiter(lines)`; if `d` and `len(lines) >= 2`: `header` = the single record of the 8.2 state
    machine run on `[lines[0]]` with `d` (an unclosed quote runs to the end of the line); `alpha` = number of header
    fields `h` where `pyStrip(h).replace("_", "").replace(" ", "")` is non-empty and all-`\p{L}`;
    `alpha/len(header) > 0.6` → `("csv", 0.9)`. Otherwise fall through.
14. `kv = count(lines with len(R11.findall(ln)) >= 2)`; `> 0.6` → `("kv", round(kv/len, 2))`.
15. `("text", 0.5)`.

All ratios are float64 divisions of two ints, compared with strict `>` (or `>=` where written). `round` per 1.6.

JSON validity for steps 2/3 must follow Python `json.loads`:
* whitespace allowed around the value: only `space \t \n \r` (a line with a leading U+00A0 or `\x0c` is invalid);
* extra literals accepted as values: `NaN`, `Infinity`, `-Infinity`;
* control characters U+0000-U+001F inside strings are invalid (Python `strict=True`) — same as Go;
* `"\ud800"` escapes (lone surrogates) are valid;
* duplicate keys valid; numbers: ASCII-digit JSON grammar, any magnitude (`1e400` is valid → `inf`);
* nesting deeper than the Python recursion limit raises `RecursionError`, which is **not** caught → the whole ingest
  fails (practically unreachable; Go may treat as invalid);
* a value preceded by U+FEFF is invalid (JSONDecodeError).
Go: write a small validator (or `json.Valid` after rewriting `NaN`/`Infinity` tokens outside strings) — do not use
`json.Unmarshal` into `float64` (rejects `1e400`).

---

## 9. `common.records_to_observations(records, source, parser, kind, mapping=None, confidence=1.0, lines=None)`

```python
records = iter(records)
head = list(islice(records, 50))                     # HEAD = 50
keys = union of rec keys over head, first-seen order
roles = auto_map(keys, [r for _, r in head], mapping)
used = {v for v in roles.values() if v}               # distinct key names used by any of the 7 roles
```

**Kind** (once per call): `if "alert" in pyLower(source) or roles["message"] in ("alert", "alertname"): kind = "alert"`
`elif "incident" in pyLower(source): kind = "incident"` else the parser's default kind. `source` is the file name
(inner archive path included). Role key comparison is exact/case-sensitive.

Roles: `r_ts, r_msg, r_sev, r_svc, r_host = roles["timestamp"], roles["message"], roles["severity"], roles["service"], roles["host"]`,
`r_env, r_origin = roles.get("environment"), roles.get("origin")`. Role names (auto_map / ROLE_HINTS order):
`timestamp, severity, service, environment, origin, host, message`.

`use_scale = parser != "syslog"`; `env_of = {}` (per call cache); `n_lines = len(lines) if lines else 0`;
`last_ts = None`.

Per record `(line_no, rec)` over head then the rest:

1. **Timestamp**: `ts = parse_timestamp(rec.get(r_ts)) if r_ts else None`; `missing = ts is None`;
   `ts = ts or last_ts or MISSING_TS` (`MISSING_TS = 1970-01-01T00:00:00+00:00`); if not missing: `last_ts = ts`.
   (The carried-forward value never survives `ingest`: see 10.2. Implement it anyway for `_no_ts` bookkeeping.)
   `parse_timestamp` contract (normalize slice): returns a tz-aware datetime or None; naive results get UTC;
   explicit offsets are **kept** (not converted); numbers: `v > 1e12` → milliseconds else seconds; year-less formats use
   the **current year** (`datetime(datetime.now().year, 1, 1)` captured at import) — the goldens were generated in 2026
   and must be compared with "current year" pinned to 2026 in Go (inject the clock). It memoises per distinct string
   (pure cache, no behavioural effect).
2. **Message**: `msg = str(rec.get(r_msg) or "")` if `r_msg` else `" ".join(f"{k}={v}" for k, v in rec.items())`
   (record insertion order, `str()` of values per 1.6). Falsy values (`0`, `False`, `0.0`) become `""`.
3. **Severity**: `sev_raw = rec.get(r_sev) if r_sev else None`. If `sev_raw not in (None, "")`:
   `sev = column_severity(sev_raw, use_scale=use_scale)`, source `"field"`. Else `sev = severity_from_text(msg)`,
   `sev_src = "text" if LEVEL_WORD_RE.search(msg) else "hint" if sev != "INFO" else "default"`.
   (`column_severity`, `severity_from_text` belong to the normalize slice.)
4. `service = str(rec.get(r_svc) or "") if r_svc else ""`; `host = str(rec.get(r_host) or "") if r_host else ""`
   (non-str values go through `str()`: golden `baseline.json` has float host/service → `"100.0"`).
5. **Environment**: `env = normalize_environment(rec.get(r_env)) if r_env else ""`. If `env` is falsy:
   `env = env_of.get((host, service))`; if `None`: `env = env_of[(host, service)] = infer_environment(host, service, source)`;
   if still falsy: `env = infer_environment(msg)` (not cached).
6. **Attributes**:
   `attrs = {k: v for k, v in rec.items() if k not in used and v not in (None, "")} if len(rec) > len(used) else {}`
   — **if the record has no more keys than the number of distinct used keys, attributes are empty even when the record
   has unused keys** (because some role keys are absent from this record). Covered by 12 golden inputs (see 13).
   Key order = record order (irrelevant for canonical output, which is sorted). Values keep their types.
7. `if sev_raw is not None and pyIsDigitStr(pyStrip(str(sev_raw))): attrs["severity_raw"] = sev_raw` — original value
   and type (`"4"` str in `cisco_asa.log`, `2` int in `storm__alarms.json`, `gelf.jsonl`). Note: `str(True)` is not
   digits; `str(3.0)` is `"3.0"` → not digits; `""` is not digits; `" 4 "` is.
8. `if missing: attrs["_no_ts"] = True` (removed again in 10.2).
9. `if sev_src != "field": attrs["_sev_src"] = sev_src` — this **is** in the golden attributes (`text`, `hint`,
   `default`).
10. Emit `Observation(timestamp=ts, message=msg, kind=kind, severity=sev, service=service, host=host, environment=env,
    origin=str(rec.get(r_origin) or "") if r_origin else "", attributes=attrs, source=source, line_no=line_no,
    parser=parser, parser_confidence=confidence, raw=...)` with
    `raw = lines[line_no-1] if 0 < line_no <= n_lines else json.dumps(rec, ensure_ascii=False, default=str)`.
    The JSON fallback (used by `json` and `winevt`, which pass `lines=None`) uses **record insertion order (not
    sorted)**, separators `", "`/`": "`, the string escaping of 2.1 and float repr of 1.7. Measured example
    (`storm__alarms.json`): `{"alarm_id": "ALM-00001", "timestamp": "2026-09-10T01:30:01+03:00", ..., "severity": 2, ...}`.

`LEVEL_WORD_RE` (normalize.py, used in step 3):
`(?<![A-Za-z])(TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERR(?:OR)?|CRIT(?:ICAL)?|FATAL|ALERT|EMERG(?:ENCY)?|SEVERE|PANIC|INF|WRN|FTL|DBG|VRB)(?![A-Za-z])` with `re.I`.
**Not RE2** (lookbehind + lookahead). For the boolean `search` used here this rewrite is exact:

```go
const lw = `[A-Za-z\x{130}\x{131}\x{17f}\x{212a}]`          // Python re.I view of [A-Za-z]
const lnw = `[^A-Za-z\x{130}\x{131}\x{17f}\x{212a}]`
var levelWordSearch = regexp.MustCompile(`(?i)(?:^|` + lnw + `)` +
   `(?:TRACE|DEBUG|[Iıİ]NFO|NOT[Iıİ]CE|WARN(?:[Iıİ]NG)?|ERR(?:OR)?|CR[Iıİ]T(?:[Iıİ]CAL)?|FATAL|ALERT|EMERG(?:ENCY)?|SEVERE|PAN[Iıİ]C|[Iıİ]NF|WRN|FTL|DBG|VRB)` +
   `(?:` + lnw + `|$)`)
```

(Equivalence: a Python match at position p exists iff this pattern matches starting at p-1 or at `^`; RE2 tries every
start, so the boolean is identical. If another slice needs the match **span** of LEVEL_WORD_RE, use the capture inside
instead of the whole match.)

---

## 10. Post-parse steps in `ingest`

### 10.1 `dedupe(observations, report)`

`key = scenario.DEDUP_KEY` (`"alarm_id"`); `DEDUP_PREFER = "json"`. If `key` falsy → return input unchanged.

```
keep = {}, order = [], dropped = {}, passthrough = []
for o in observations (concatenated parse order):
    k = o.attributes.get(key)
    if k is None or k == "": passthrough.append(o); continue          # only None / "" pass through (0/False do not)
    k = str(k)                                                         # str(): 1 → "1", 1.0 → "1.0", True → "True"
    if k not in keep: keep[k] = o; order.append(k)
    else:
        cur = keep[k]
        if o.parser == "json" and cur.parser != "json":
            dropped[cur.source] += 1; keep[k] = o                      # replaced, but keeps the FIRST position in order
        else:
            dropped[o.source] += 1
if not dropped: return observations                                    # original list and order
for entry in report: if entry["file"] in dropped: entry["dedup_dropped"] = dropped[entry["file"]]
return passthrough + [keep[k] for k in order]                          # changes relative order (matters for ties in the stable sort)
```

Dedupe runs over **all files of one ingest call** (e.g. `alarms.json` + `alarms.csv` in one zip), and per batch in live.
Since `alarm_id` must be in `attributes`, an `alarm_id` that became a role (e.g. message) is never deduplicated.
`report` entries for side tables have `file` too (never in `dropped`: they produce no observations).
Golden coverage: the key-present path runs for `storm__alarms.csv` (whole, live) and `storm__alarms.json` (whole); no
golden has a duplicate, so the drop / prefer / reorder / `dedup_dropped` branch is **not covered**.

### 10.2 `fill_missing_timestamps(observations)`

```
real = [o.timestamp for o in observations if not o.attributes.get("_no_ts")]
fallback = min(real) if real else (observations[0].timestamp if observations else None)
if fallback is None: return
for o in observations:
    if o.attributes.pop("_no_ts", False): o.timestamp = fallback
```

* `min` compares instants; among equal instants the **first** in list order wins and its offset is what gets copied
  (Go: keep the first strictly-smaller).
* `_no_ts` is always removed (no golden attribute contains it).
* So every row without a parseable time gets the earliest real timestamp of the whole ingest call (all files of the
  upload; per batch in live), not the previous row's time. If no row has a real time, all get `1970-01-01T00:00:00+00:00`
  (golden: `mut__iis_w3c.log` 241 rows, `sap__class_prefetch.lst`, `baseline.json` live, `storm__alarms.json` live 1127
  rows). Empty list → return (golden: `mut__csv_events.csv` live, a header-only last batch).

### 10.3 Sort

`observations.sort(key=lambda o: o.timestamp)`: stable; compares aware datetimes by instant (offset-normalised).
Go: `sort.SliceStable(obs, func(i, j int) bool { return obs[i].TS.Before(obs[j].TS) })` with microsecond-precision
times. Rows with equal instants (including different offsets) keep their pre-sort order.

---

## 11. `tables` (side tables)

### `is_side_table(fname)`

`base = pyLower(fname.rsplit("/", 1)[-1])` (only `/` separates; a backslash path stays whole); true if any pattern of
`scenario.SIDE_TABLES = [r"depend", r"bagimlilik", r"inventory", r"envanter", r"sozlugu", r"brifing", r"readme"]` is found
by `re.search`. All are plain lowercase literals → Go `strings.Contains(base, p)` is exact (RE2 also fine).
Examples: `README.md`, `service_dependencies.csv`, `host_inventory.json`, `VERI_SOZLUGU.md` are tables.

### `read_table(fname, text) -> list[dict]`

1. `t = pyLStrip(text)`; empty → `[]`.
2. If `t` starts with `[` or `{`: `data = json.loads(t)`; if dict → the first value (insertion order) that is a list, else
   `[]`; return the dict elements of that list (values keep JSON types). **Any** exception → `[]`.
3. If `pyLower(fname)` ends with `.md` or `.txt` → `[]` (checked only after the JSON attempt).
4. `dialect = csv.Sniffer().sniff(t[:2000 code points], delimiters=",;\t|")`; `csv.Error` → `csv.excel`.
5. `rows = list(csv.DictReader(io.StringIO(t), dialect=dialect))` and return
   `[{pyStrip(k or ""): pyStrip(v or "") for k, v in r.items()} for r in rows]`.
   DictReader: header = first record; short rows get `None` for missing columns (→ `""`); **long rows put the extra
   fields in a list under key `None`, and `(v or "").strip()` on a list raises `AttributeError` → the whole ingest
   fails**. Records that are empty lists (blank lines) are skipped by DictReader. `io.StringIO` + csv reader splits
   records on `\r`, `\n`, `\r\n` only (not the other `splitlines` boundaries).
   Duplicate stripped header names: later column wins.

`csv.Sniffer` (CPython **3.11.17** `Lib/csv.py`; this patch release uses possessive quantifiers and differs from older
3.11.x) is **not RE2-portable**: `_guess_quote_and_delimiter` uses backreferences `(?P=quote)`, `(?P=delim)`, a
negative lookahead `(?!(?P=quote))`, possessive `*+`/`++`, a lookbehind `(?<=%(delim)s)`, `re.DOTALL | re.MULTILINE`
and Unicode `\w`. Its four patterns (body = `(?:(?P=quote){2}|(?!(?P=quote)).)*+`):

```
(?P<delim>[^\w\n"\'])(?P<space> ?)(?P<quote>["\'])BODY(?P=quote)(?P=delim)
(?:^|\n)(?P<quote>["\'])BODY(?P=quote)(?P<delim>[^\w\n"\'])(?P<space> ?)
(?P<delim>[^\w\n"\'])(?P<space> ?)(?P<quote>["\'])BODY(?P=quote)(?:$|\n)
(?:^|\n)(?P<quote>["\'])BODY(?P=quote)(?:$|\n)
doublequote check: (?:(?<=DELIM)|^)SPACE QUOTE((?:QUOTE QUOTE|[^QUOTE]++)*+)QUOTE(?:DELIM|$)   (MULTILINE; SPACE = ' *+' unless delim == ' ')
```

followed by `_guess_delimiter` (per-chunk ASCII character frequency modes, consistency threshold 1.0 → 0.9 in 0.01
steps, `preferred = [',', '\t', ';', ' ', ':']` tie-break; restricted to `delimiters=",;\t|"`). A Go port must be a
hand-written scanner reproducing CPython 3.11.17's `Sniffer.sniff` line by line (for every quote char `"`/`'` and
delimiter candidate, simulate the four patterns with explicit loops instead of regexes), plus a DictReader with the
dialect's `quotechar`, `doublequote`, `skipinitialspace`. Golden coverage: **none** (no golden file name matches
`SIDE_TABLES`; `samples/corpus/README.md` is excluded by `parse_golden.py`). Recommendation: implement in Go only with a
dedicated fixture set generated by Python (or keep side tables on the Python path), and mark it as a parity risk.

Side tables report `rows`/`keys`/`table`; `table` and `sha` are excluded from goldens; downstream (`dependencies`,
`inventory`) is not part of parsing.

---

## 12. `stamp.file_id(name, text)` (report `sha`, excluded from goldens)

```
base = pyLower(name.replace("\\", "/").rsplit("/", 1)[-1])
body = text.replace("\r\n", "\n").replace("\r", "\n")
sha256(utf8(base) + b"\0" + utf8(body)).hexdigest()[:10]
```

Only `\r\n`/`\r` are normalised (not the other splitlines boundaries). `engine_id`, `scenario_id`, `git_rev` are
`lru_cache`d process constants unrelated to parse output.

## 13. `models`

```python
SEV_RANK = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3, "CRITICAL": 4}
@dataclass Observation: timestamp: datetime; message: str; kind="log"; severity="INFO"; service=""; host="";
    environment=""; origin=""; attributes: dict = {}; source=""; line_no=0; parser=""; parser_confidence=1.0;
    template=""; fingerprint=""; raw=""          # ref property = f"{source}:{line_no}"
```

`template`/`fingerprint` are filled by analysis, not by parsing, and are not in the canonical output. Severity values
produced by parsing are exactly the five SEV_RANK keys. `kind` values seen in goldens: `log`, `event`, `alert`
(`incident` possible but not covered).

## Profiles (learned column mappings)

`profiles.py` persists `<WATCHOVER_HOME or ./data>/profiles.json` and is read/written on **every** `_parse_one` for
formats `json, jsonl, csv, kv, grok` with non-empty `keys`:

* `signature(fmt, keys) = sha1(utf8(fmt + "|" + ",".join(sorted(str(k) for k in keys)))).hexdigest()[:16]`
  (sorted by code point).
* `lookup` returns the stored entry if it exists and its `mapping` is non-empty.
* `remember(fmt, keys, mapping, name, source)`: no-op if `keys` empty or the cleaned mapping
  `{k: v for k, v in mapping.items() if k in ROLES and v}` is empty (ROLES = the seven role names); a `"user"` entry is
  never overwritten by `"auto"`; otherwise stores `{"id": sig, "format", "keys": sorted(keys)[:60], "mapping": clean, ...}`.
* `parse_golden.py` sets `WATCHOVER_HOME` to a fresh temp dir **only if unset** (`os.environ.setdefault`), then processes
  corpus files (sorted) and then `receiver/parse/testdata/inputs` (sorted), each as whole then live batches, all in one
  process → profiles learned from earlier files/batches appear as `"profile": "<sig>"` in later reports
  (e.g. `mut__bind9_query.log` whole has profile `1b2857143ff21121` learned from `bind9_query.log`; most live reports of
  the 5 formats carry the profile learned by the whole run of the same file).
* Measured: forcing `lookup → None` leaves every golden observation identical; only the report `profile` field changes.
  In general a learned mapping **can** change roles (value-based auto_map guesses are replaced by the remembered
  choices), so Go should implement the store as an in-memory `map[sig]mapping` scoped to the test run (same file order as
  `parse_golden.py`) and apply it exactly like Python: `mapping = learned.mapping` → `auto_map(keys, head, mapping)` in
  both `_parse_one` and `records_to_observations`. For production, persist or not as the receiver design decides; flag
  that a user-made profile (`source="user"`) overrides auto roles.

---

## 14. Live path: `LiveStore.ingest` / `_ingest` and the `_detect` hint cache

```python
def _ingest(self, name, data, agent, env, site, agent_id):
    key = (agent, name)                       # agent = token's agent name or X-Agent header or client IP, [:120]
    hint = self._detect.get(key)
    if hint is not None:
        hint["n"] += 1                        # mutates the cached dict in place
        if hint["n"] % 200 == 0:              # a stream can change shape: re-detect now and then
            hint = None                       # local only: the cache entry keeps n == 200
    obs, rep = ingest_bytes(name, data, hint=hint)
    if rep and rep[0].get("format") not in (None, "table") and (hint is None or hint.get("n", 0) % 200 == 0):
        fmt = rep[0]["format"]
        if fmt == "json" and data.strip().count(b"\n") == 0 and data.lstrip()[:1] == b"{":
            fmt = "jsonl"                     # one object in the first batch of a stream is one JSON line
        self._detect[key] = {"format": fmt, "confidence": rep[0].get("confidence", 0.9),
                             "family": rep[0].get("family", ""), "n": 1}
        if len(self._detect) > 5000:
            self._detect.clear()
    ... (post-processing of obs: arrival-time stamps, agent attribute, BAD_HOST, enrichment, persistence — not part of the parse goldens)
```

Exact behaviour Go must reproduce:

* Cache key: `(agent, file base name)`. Cache value: `format`, `confidence` (as reported), `family` (`""` when none),
  counter `n`.
* Batch k of a stream (1-based, counting only batches that reach `_ingest`): batch 1 detects and stores `n = 1`; batch k
  sees `n = k` after the increment; when `n % 200 == 0` the batch is parsed **without** hint (full detection) and a fresh
  entry with `n = 1` replaces it. Re-detection therefore happens on batches 200, 399, 598, 797, 996, … (period 199
  after the first). Golden: `storm__alarms.json` live has 1126 batches → re-detections at 200, 399, 598, 797, 996.
* The second half of the store condition (`hint.get("n", 0) % 200 == 0`) is dead code (such a hint was nulled).
* If the re-detection batch yields **no report** (all content binary/dropped) or a `"table"` report, the cache entry is
  not replaced and keeps `n == 200`; the next batch increments to 201 and **uses the old hint again** until `n == 400`.
* With a hint, `_parse_one` skips detection entirely (`fmt = hint["format"]`, `conf = float(hint["confidence"])`), and
  sets the family hint even when it is `""` → a text stream whose first batch found no family never gets one until the
  next re-detection. The same hint is applied to **every file** inside a multi-file payload (zip/tar), and only `rep[0]`
  (the first file) decides the cached hint.
* Single-object JSON → `jsonl` rule: applied only when storing a new hint, on the **raw request bytes**: `data.strip()`
  (ASCII whitespace) contains no `\n` and `data.lstrip()` starts with `{`. Gzip/zip bodies or a UTF-8 BOM before `{`
  defeat it. The stored confidence stays the detector's (0.95). The batch itself is still parsed as `json`; later
  batches are parsed as `jsonl` with `parser_confidence` 0.95.
* A whitespace-only body: `parse_golden.py` skips such chunks (`if not chunk.strip(): continue`, no counter increment);
  the HTTP handler only rejects an empty body (`Content-Length <= 0` / no data). A whitespace-only POST reaches
  `ingest_bytes`, `detect_format` returns `("text", 0.0)`, the text parser yields nothing, and the hint
  `{"format": "text", "confidence": 0.0, ...}` is cached. Not covered by goldens.
* `len(self._detect) > 5000` → the whole cache is cleared (after inserting).
* Thread safety: `_ingest` runs on a `ThreadingHTTPServer` thread; the dict is shared without a lock (Python GIL makes
  single operations atomic). Go: guard with a mutex; the counter increment and replace must be atomic per key to keep the
  199-batch period.
* Exceptions from `ingest_bytes` (bad zip/gzip, `ArchiveTooBig`, `RecursionError`, DictReader `AttributeError`) become
  HTTP 400 `{"error": str(e)}`; the cache was already incremented.

`parse_golden.py` live emulation (what the `.live.jsonl.gz` files contain): `lines = data.split(b"\n")`; for
`i in range(0, len(lines), 40)`: `chunk = b"\n".join(lines[i:i+40])` (so chunks carry no trailing `\n` except when the
slice includes the final empty element; `\r` stays at line ends of CRLF files such as `storm__alarms.csv`); skip
whitespace-only chunks; same hint logic as above with `chunk` in place of `data`; `name = p.name` for every batch; the
observations of all batches are concatenated; the report line holds the first three batch reports (`reps[:3]`,
`{}` for a batch without report), minus `sha`/`table`. `line_no` restarts at 1 in every batch.

Golden-visible live effects worth testing first:
* header-less CSV batches with the csv hint (`mut__csv_events.csv`, `storm__alarms.csv`: 39 rows per 40-line batch —
  the first line of each later batch is taken as header);
* first-batch detection differing from the whole-file detection (`mut__dynamics_ax_aos.log`: whole `syslog` 0.61, live
  `text`/`dynamics-ax`; `mut__kafka_log4j.log`: whole `sap` 0.64, live `sap` 0.57; `mut__sqlserver_errorlog.log`: 1.2 vs
  1.23);
* multi-line JSON documents split into batches (`storm__alarms.json`, `baseline.json`: first batch `text` 0.5, all-epoch
  timestamps).

---

## 15. Global and stateful behaviour — what Go must do

| State | Python | Effect on output | Go |
|---|---|---|---|
| `scenario.MAPPING` | `{}` → `mapping=None` | none | constant nil mapping (allow override) |
| `scenario.SIDE_TABLES` | 7 literals | table branch | constant list |
| `scenario.DEDUP_KEY`/`DEDUP_PREFER` | `"alarm_id"` / `"json"` | dedupe | constants |
| `FAMILY_HINT` / `FAMILY_SEEN` contextvars | set/reset around each `_parse_one` | report `family`, text parser default service | explicit parameters/return values |
| profiles store | `profiles.json` on disk, read on every lookup | report `profile` (goldens); roles in general | in-memory map per run (see Profiles) |
| `_detect` hint cache | per `LiveStore`, keyed `(agent, name)` | format/confidence/family of later batches | mutex-guarded map with identical counter rules |
| `parse_timestamp` `_ts_cache` | memo per string, cleared when full | none | optional cache |
| current year (`normalize._DEFAULT`) | `datetime.now().year` at import | year-less timestamps | injectable clock; pin 2026 for the goldens (regenerate goldens after New Year) |
| `WATCHOVER_HOME` | profile store location; `parse_golden` uses a temp dir only if unset | profiles | ensure the golden harness starts empty |
| `WATCHOVER_PARALLEL`, `os.cpu_count()` | parallel ingest | only hint dropped in parallel path | sequential |
| `stamp.engine_id` etc. | `lru_cache` constants | none for parsing | n/a |

---

## 16. Coverage: which golden inputs exercise which branch

(Measured with line tracing and branch probes over every input, whole + live.)

| Branch | Inputs |
|---|---|
| decode utf-8 (no BOM) | all inputs |
| detect → json 0.95 | `alertmanager.json`, `aws_cloudtrail.json`, `otel_logs.json`, `baseline.json` (whole), `storm__alarms.json` (whole) |
| json attempt fails → continue | `baseline.json` live, `storm__alarms.json` live, others starting with `[`/`{` |
| detect → jsonl | `demo__app.jsonl`, `docker_jsonfile.jsonl`, `ecs_json.jsonl`, `gelf.jsonl`, `journald.jsonl`, `mongodb.jsonl`, their `mut__*`, `sim__simulate_batch.jsonl`, `sim__simulate_flows.jsonl`, `sim__simulate_traces.jsonl` |
| detect → w3c | `iis_w3c.log` |
| detect → winevt | `windows_event_xml.log`, `mut__windows_event_xml.log` |
| detect → cef | `cef_arcsight.log`, `leef_qradar.log` (+ `mut__`) |
| detect → access | `apache_access_clf.log`, `aws_alb_access.log`, `nginx_combined.log` (+ `mut__`) |
| detect → text 0.9 (CRI) | `kubernetes_cri.log`, `mut__kubernetes_cri.log` |
| detect → syslog (hits) | `cisco_asa.log`, `demo__db.log`, `haproxy.log`, `iptables_kernel.log`, `postfix.log`, `sudo_cron.log`, `syslog_rfc3164.log`, `syslog_rfc5424.log`, `mut__dynamics_ax_aos.log` (whole, 0.61), most `mut__` syslog files |
| detect → syslog (hits + hits2, can be > 1) | `paloalto_csv.log`, `mut__paloalto_csv.log`, `mut__sqlserver_errorlog.log` (1.2 / 1.23) |
| detect → sap | all `sap__*`, `mut__iis_w3c.log` (0.51), `mut__kafka_log4j.log` (0.64) |
| detect → grok | `bind9_query.log`, `dockerd.log`, `nagios.log`, `rabbitmq.log`, `squid_access.log`, `vmware_esxi.log` (+ `mut__`) |
| detect_delimiter returns a delimiter / csv 0.9 | `csv_events.csv`, `demo__alerts.csv`, `mut__csv_events.csv`, `storm__alarms.csv` |
| detect → kv | `fortigate_kv.log`, `logfmt.log` (+ `mut__`) |
| detect → text 0.5 | `demo__checkout.log`, `dotnet_serilog.log`, `dynamics_*`, `golang_log.log`, `java_stacktrace.log`, `kafka_log4j.log`, `mysql_error.log`, `nginx_error.log`, `oracle_*`, `plain_app_text.log`, `postgres.log`, `python_traceback.log`, `redis.log`, `sqlserver_errorlog.log`, `timestamp_variants.log`, `tomcat_juli.log` (+ `mut__`) |
| hint path in `_parse_one` | every multi-batch live run (57 inputs; e.g. `demo__app.jsonl`, `mut__*`, `storm__*`, `sim__simulate_batch.jsonl`) |
| hint refresh at n % 200 | `storm__alarms.json` live only |
| family hint non-empty reused | live of `mut__dotnet_serilog.log`, `mut__golang_log.log`, `mut__postgres.log`, `mut__redis.log`, `mut__nginx_error.log`, `mut__mysql_error.log`, `mut__oracle_ebs_*`, `mut__python_traceback.log`, `mut__java_stacktrace.log`, `mut__dynamics_*` |
| family hint `""` reused | live of `mut__plain_app_text.log`, `mut__oracle_alert.log`, `mut__timestamp_variants.log`, `mut__tomcat_juli.log`, `demo__checkout.log` |
| profile lookup hit (report `profile`) | `mut__bind9_query.log`, `mut__csv_events.csv`, `mut__docker_jsonfile.jsonl`, `mut__dockerd.log`, `mut__ecs_json.jsonl`, `mut__fortigate_kv.log`, `mut__gelf.jsonl`, `mut__journald.jsonl`, `mut__logfmt.log`, `mut__mongodb.jsonl`, `mut__nagios.log`, `mut__rabbitmq.log`, `mut__squid_access.log`, `mut__vmware_esxi.log` (whole and live) and the live runs of their corpus originals |
| kind = alert | `alertmanager.json` (role message `alertname`), `demo__alerts.csv`, `oracle_alert.log`, `mut__oracle_alert.log` (file name) |
| `sev_src` field / text / hint / default | all present (25185 / 2261 / 1786 / 5662 rows) |
| `severity_raw` | str: `cisco_asa.log`, `syslog_rfc5424.log`, `journald.jsonl`, `storm__alarms.csv` (+ `mut__`); int: `gelf.jsonl`, `mut__gelf.jsonl`, `storm__alarms.json`; float sev field: `baseline.json` |
| `_no_ts` rows → fallback | `mut__dotnet_serilog.log`, `mut__iis_w3c.log`, `mut__java_stacktrace.log`, `mut__kafka_log4j.log`, `mut__oracle_alert.log`, `mut__oracle_ebs_concurrent.log`, `oracle_ebs_concurrent.log`, `mut__python_traceback.log`, `mut__tomcat_juli.log` (live), `sap__class_prefetch.lst`, `baseline.json` (live), `storm__alarms.json` (live) |
| fill with no observations | `mut__csv_events.csv` live (header-only last batch) |
| attrs suppressed (`len(rec) <= len(used)` with unused keys) | `docker_jsonfile.jsonl`, `ecs_json.jsonl`, `gelf.jsonl`, `kubernetes_cri.log`, `mut__docker_jsonfile.jsonl`, `mut__ecs_json.jsonl`, `mut__gelf.jsonl`, `mut__java_stacktrace.log`, `mut__kafka_log4j.log`, `mut__kubernetes_cri.log`, `mut__python_traceback.log`, `mut__tomcat_juli.log` |
| environment from message (`infer_environment(msg)`) | 94 inputs (most) |
| non-str host/service via `str()` | `baseline.json` (float → `"100.0"`) |
| raw = JSON dump of record | `alertmanager.json`, `aws_cloudtrail.json`, `baseline.json`, `otel_logs.json`, `storm__alarms.json`, `windows_event_xml.log`, `mut__windows_event_xml.log` |
| dedupe key present, no duplicates | `storm__alarms.csv`, `storm__alarms.json` |

### Branches NOT covered by any golden

* `loader`: ZIP, TAR, GZ, binary extension skip, NUL guard, UTF-8 BOM strip, UTF-16 (BOM / BOM-less mojibake),
  latin-1 fallback, `_Budget` limits, cp437 zip names.
* `ingest`: parallel path; multi-file uploads (cross-file dedupe and cross-file timestamp fallback).
* `_parse_one`: side-table branch and `tables.read_table` (JSON and Sniffer/DictReader paths); SharePoint ULS
  (`_sharepoint_uls`, Execution Time and `Request (GET:...)` extraction); user-sourced profiles; `remember` exception;
  `progress` callback.
* `detect_format`: empty input → `("text", 0.0)`; csv rejected by the header-alpha test (falls to kv/text);
  `detect_delimiter` `csv.Error` (field > 131072 chars); quoted-field-across-lines merging.
* `records_to_observations`: message built from `k=v` join (no message role); falsy non-empty message values (`0`,
  `False`); non-str message; kind = `incident`; `severity_raw` from Unicode digits.
* `dedupe`: duplicates, `json` preference replacement, reorder, `dedup_dropped`.
* live: single-object JSON → `jsonl` rule actually used by a later batch (rule fires for `aws_cloudtrail.json`,
  `otel_logs.json` but they have only one batch); stale hint reuse after an empty/table re-detection; 5000-entry cache
  clear; multi-file payload sharing one hint; whitespace-only POST caching `text`/0.0.
* Case-folding edge cases (`ı`, `İ`, `ſ`, `K`) in `LEVEL_WORD_RE` / `_NOT_LEVEL`, Unicode digits/whitespace in the
  detector regexes, `pyLower` of `İ`/`Σ` in file names.

Recommended extra fixtures (generate goldens with the same Python code): a zip with `alarms.json` + `alarms.csv`
(duplicates → dedupe), a `.gz` file, a UTF-8-BOM file, a UTF-16LE-BOM file, an even-length cp1254 file (UTF-16
mojibake path), an odd-length cp1254 file (latin-1 path), a SharePoint ULS TSV with header `Timestamp\tProcess\tTID\tArea\tCategory\tEventID\tLevel\tMessage\tCorrelation`,
`inventory.csv` / `dependencies.json` side tables, a syslog file whose host column is `INFO`/`ınfo`, and a live stream
whose first batch is one JSON object followed by more JSONL batches.
