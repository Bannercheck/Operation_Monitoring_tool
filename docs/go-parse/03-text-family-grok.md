# Go parse port, part 03: free-text logs (text parser, log families, grok)

Scope: everything that turns a free-text log file into `(line_no, record)` rows once `detect_format` (part 01, 8.3) has
picked `text` or `grok`: the line-head parser, multi-line folding, log-family detection and its contextvars, the grok
pattern library, grok detection and the grok record builder. What happens to the rows afterwards
(`records_to_observations`, `auto_map`, `parse_timestamp`, severity / environment normalisation, dedupe, sort) is in
part 01 (section 9, 10) and part 02; this document states the contract it relies on where it matters.

Python sources covered (all under `src/watchover/`):

| File | Covered |
|---|---|
| `parsers/text_parser.py` | every regex, `parse_head`, `TextParser.records` |
| `parsers/family.py` | `FAMILIES`, `STRONG`, `FAMILY_HINT`, `FAMILY_SEEN`, `detect_family`, `_detect_family` |
| `grok.py` | `BASE`, `LINES`, `LinePattern`, `expand`, `_SUB`, `_cache`, `compile_pattern`, `guard_regex`, `_RISKY`, `user_patterns`, `all_patterns`, `detect` (`match`, `test_pattern`, `coverage_of`, `save_user_patterns` are UI helpers, not on the parse path) |
| `parsers/grok_parser.py` | `STATUS_SEV`, `GrokParser.records` |

Other modules that reuse pieces of this slice (their Go ports must use the RE2 forms given here):
`parsers/json_parser.py:enrich` calls `parse_head(txt.rstrip("\n"))` on a JSON record's `log` / `message` / `msg` /
`short_message` text (docker json-file, CRI, GELF); `parsers/kv_parser.py` uses `TS_RE.match`, `LEVEL_RE.match`,
`LOGGER_RE.match`; `grok_parser.py` uses `CONT_RE`. All of these regexes stay correct on strings that contain internal
`\n` (the JSON case): Python `$` only differs from Go `$` before a *final* `\n`, which `rstrip("\n")` removed.

Verification done while writing this spec (so the facts below are measured, not guessed):

* An instrumented copy of `parse_head`, `TextParser.records`, `detect_family` and `GrokParser.records` was run over every
  golden input exactly as `scripts/parse_golden.py` runs them (whole run + 40-line live run with the hint); its output
  was byte-identical to all 240 golden files, and every branch it took was recorded per input. Section 11 lists the
  result; section 12 lists what no golden reaches.
* The Go code in sections 1, 2, 4.1 and 5.7 was compiled with Go 1.24.7 and compared with Python 3.11.17 on the 19 759
  distinct lines of all golden inputs (`samples/corpus/*` + `receiver/parse/testdata/inputs/*`) plus ~170 hand-written
  edge cases (Unicode digits, `ı`/`İ`/`ſ`, U+00A0 / U+001F whitespace, `ValueErroré`, `port 22é`, `<1é2>`, epoch
  without trailing whitespace, ...). Compared: the full `parse_head` result (record keys **in order**, values, `rest`),
  `CONT_RE` match boolean, every family pattern's search boolean, and for each of the 27 grok `LINES` the match boolean
  plus every named group (value or "did not participate"). **0 differences.** Removing any one of the three grok
  rewrites of 5.7.3 (SSHD port, SYSLOGFACILITY, DHCPD) produces differences on the edge cases, so each is necessary.

---

## 0. Conventions and helpers

Part 01, section 1 defines the Python-semantics helpers used here: `pyStrip` / `pyIsSpace` (29-code-point Python
whitespace), `pySplitLines`, code-point slicing, `PS` / `PNS` (Python `\s` / `\S` as RE2 classes), `\p{Nd}` for `\d`,
`[\p{L}\p{N}_]` for `\w`, and the `re.I` rule (`I`/`i` must be written `[Iiıİ]` in Go). They are not repeated.

Additional rules for this slice:

* **Python `\b`** is a Unicode word boundary (word = `[\p{L}\p{N}_]`); RE2 `\b` is ASCII. Every Python `\b` below is
  either (a) proven redundant in its context and dropped, (b) left as RE2 `\b` because both neighbouring characters
  are forced to be ASCII by the surrounding pattern (then ASCII and Unicode boundaries agree), or (c) rewritten:
  for a **boolean search**, `\bX` (X starting with a word char) becomes `(?:^|[^\w])X` and `X\b` (X ending with a word
  char) becomes `X(?:[^\w]|$)` — these consume one extra character, which is harmless when only "did it match" is used.
  Where a span or a capture is used, the specific rewrite is given and proven.
* **Lookarounds** (`(?=`, `(?!`, `(?<=`, `(?<!`): RE2 has none. Each one in this slice is rewritten individually below.
* **Leftmost-first**: Go `regexp` picks the same match and the same submatches as Python's backtracking engine for
  every pattern without lookarounds / backreferences, including alternation order and greedy/lazy preference. That is
  what makes a 1:1 port of the remaining regexes exact.
* **Ordered records**: Python dicts keep insertion order and the order is observable (report `keys`, `auto_map`
  tie-breaking, part 01 section 6). Records here are ordered maps; assigning an existing key keeps its position.
  The Go helper type used in the reference code:

```go
type kv struct{ K string; V any }
type rec []kv
func (r *rec) set(k string, v any) { for i := range *r { if (*r)[i].K == k { (*r)[i].V = v; return } }; *r = append(*r, kv{k, v}) }
func (r rec) has(k string) bool    { for _, e := range r { if e.K == k { return true } }; return false }
```

* **Value types produced by this slice**: every record value is a `str`, except `_partial` (`bool`), `_unmatched`
  (`bool`) and `_lines` (`int`). No floats, no nested values, no `None` values are ever stored in a record (grok groups
  that did not participate are filtered out; groups that matched the empty string are kept as `""`).
* **String operations used** (exact Go equivalents): `ln.strip()` / `rest.strip()` → `pyStrip`;
  `m[0].strip(" :-")` → `strings.Trim(s, " :-")` (only those three characters; a tab is **not** stripped);
  `ln.split("]")[0].lstrip("[")` → `strings.TrimLeft(strings.SplitN(ln, "]", 2)[0], "[")`;
  `m[1].upper()` on the PostgreSQL level (the group is case-sensitive ASCII, so it is a no-op);
  `m[1].endswith(".go")`; `m[1].count(":") == 3`; `m[1][:19]` and `rest[:120]` are **code-point** slices;
  `int(m[1])` on `\d{1,2}` accepts any `\p{Nd}` digits (Unicode digit value, not just ASCII).
  No `str.lower()` / `casefold()` is used anywhere in this slice; case-insensitivity only comes from `re.I` in
  `LEVEL_RE` and `DYNAMICS_RE`.

### 0.1 `pyRe`: mechanical translation of Python class escapes

All Go regexes below are written in Python syntax and passed through this translator, which replaces `\d`, `\w`, `\s`,
`\S` (outside and inside `[...]`) by their exact Python-3.11 equivalents and copies everything else. It panics on
escapes this slice never uses (`\D`, `\W`, `\B`, `\A`, `\Z`) so a future pattern cannot silently change meaning.
`\b` is copied unchanged (ASCII in RE2) and must only remain where rule (b) above holds.

```go
// pyre.go
package main

import "strings"

// Python 3.11 str-regex whitespace set (str.isspace) as an RE2 class body.
const pyWS = `\t\n\x0b\x0c\r\x1c-\x1f \x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}`
const pyWc = `\p{L}\p{N}_` // Python \w as an RE2 class body

// pyRe rewrites the Python class escapes \d \w \s \S (outside and inside [...]) into RE2 equivalents.
// Everything else is copied verbatim (\b stays ASCII: callers must only leave \b where both neighbours are ASCII-constrained).
func pyRe(p string) string {
	var b strings.Builder
	in := false
	for i := 0; i < len(p); i++ {
		c := p[i]
		if c == '\\' && i+1 < len(p) {
			n := p[i+1]
			i++
			switch n {
			case 'd':
				b.WriteString(`\p{Nd}`)
			case 'w':
				if in {
					b.WriteString(pyWc)
				} else {
					b.WriteString(`[` + pyWc + `]`)
				}
			case 's':
				if in {
					b.WriteString(pyWS)
				} else {
					b.WriteString(`[` + pyWS + `]`)
				}
			case 'S':
				if in {
					panic(`\S inside a class`)
				}
				b.WriteString(`[^` + pyWS + `]`)
			case 'D', 'W', 'B', 'A', 'Z':
				panic(`unsupported escape \` + string(n))
			default:
				b.WriteByte('\\')
				b.WriteByte(n)
			}
			continue
		}
		if c == '[' && !in {
			in = true
			b.WriteByte('[')
			if i+1 < len(p) && p[i+1] == '^' {
				b.WriteByte('^')
				i++
			}
			if i+1 < len(p) && p[i+1] == ']' { // Python: ']' first in a class is a literal
				b.WriteString(`\]`)
				i++
			}
			continue
		}
		if c == ']' && in {
			in = false
		}
		b.WriteByte(c)
	}
	return b.String()
}
```

---

## 1. Regex inventory: `parsers/text_parser.py`

Module constants (verbatim):

```python
MON = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
DOW = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
REDIS_MARK = {"#": "WARN", "*": "INFO", "-": "INFO", ".": "DEBUG"}
PG_LEVEL = {"LOG": "INFO", "DETAIL": "DETAIL", "STATEMENT": "STATEMENT", "HINT": "HINT", "CONTEXT": "CONTEXT", "QUERY": "STATEMENT"}
```

`MON.upper()` (used in `TS_RE`) is `(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)` (uppercasing `(?:` is a no-op).
No regex in this file uses `re.X`, `re.M`, `re.S`; two use `re.I` (`LEVEL_RE`, `DYNAMICS_RE`).

### 1.1 `TS_RE` — **not RE2 as is** (lookahead `(?=\s)` in the two epoch alternatives)

Python (after string concatenation; one alternative per line here for reading, the real pattern has no newlines):

```text
^\[?(
  \d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z| ?[+\-]\d{2}(?::?\d{2})?| UTC)?        # A1 ISO / PG ' +03' / Serilog ' +03:00'
 |\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:[.,]\d+)?                                               # A2 2026/09/16 02:14:07 (Go, nginx)
 |\d{2}[./-]\d{2}[./-]\d{4}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?: ?[AP]M)?                        # A3 16.09.2026 / 09/16/2026 02:14:07 PM
 |\d{1,2}-(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)-\d{4} \d{2}:\d{2}:\d{2}           # A4 22-SEP-2026 09:14:02 (EBS), no fraction
 |\d{1,2}-(?:Jan|Feb|...|Dec)-\d{4} \d{2}:\d{2}:\d{2}(?:[.,]\d+)?                               # A5 22-Sep-2026 09:14:02.003 (Tomcat)
 |\d{2} (?:Jan|...|Dec) \d{4} \d{2}:\d{2}:\d{2}(?:\.\d+)?                                       # A6 22 Sep 2026 09:14:02.003 (Redis)
 |(?:Jan|...|Dec) \d{1,2} \d{4} \d{2}:\d{2}:\d{2}                                               # A7 Sep 22 2026 09:14:02
 |(?:Mon|...|Sun) (?:Jan|...|Dec) +\d{1,2} \d{2}:\d{2}:\d{2} \d{4}                              # A8 Wed Sep 16 02:14:07 2026
 |(?:Mon|...|Sun), \d{1,2} (?:Jan|...|Dec) \d{4} \d{2}:\d{2}:\d{2}(?: [+\-]\d{4})?             # A9 RFC 2822
 |(?:Jan|...|Dec) \d{1,2}, \d{4} \d{1,2}:\d{2}:\d{2} (?:AM|PM)                                  # A10 Sep 16, 2026 2:14:07 AM
 |\d{8}T\d{6}(?:\.\d+)?Z?                                                                       # A11 20260922T091700Z
 |\d{10}(?:\.\d{1,6})?(?=\s)                                                                    # E10 epoch seconds
 |\d{13}(?=\s)                                                                                  # E13 epoch milliseconds
)\]?:?\s*
```

Exact pattern string (`TS_RE.pattern`, verbatim):

```text
^\[?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z| ?[+\-]\d{2}(?::?\d{2})?| UTC)?|\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:[.,]\d+)?|\d{2}[./-]\d{2}[./-]\d{4}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?: ?[AP]M)?|\d{1,2}-(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)-\d{4} \d{2}:\d{2}:\d{2}|\d{1,2}-(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-\d{4} \d{2}:\d{2}:\d{2}(?:[.,]\d+)?|\d{2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4} \d{2}:\d{2}:\d{2}(?:\.\d+)?|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2} \d{4} \d{2}:\d{2}:\d{2}|(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +\d{1,2} \d{2}:\d{2}:\d{2} \d{4}|(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4} \d{2}:\d{2}:\d{2}(?: [+\-]\d{4})?|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2}, \d{4} \d{1,2}:\d{2}:\d{2} (?:AM|PM)|\d{8}T\d{6}(?:\.\d+)?Z?|\d{10}(?:\.\d{1,6})?(?=\s)|\d{13}(?=\s))\]?:?\s*
```

Semantics to keep:

* Anchored at the start; an optional `[` before and optional `]` and `:` after the timestamp, then any whitespace.
  The `]` is optional independently of the `[` (`2026-09-22 09:14:02] x` also strips the `]`).
* Alternatives are tried in order; inside A1 the zone alternatives are tried `Z`, then ` ?[+-]hh(:?mm)?`, then
  ` UTC`. A space before the offset is part of the timestamp (`"2026-09-22 09:14:31.001 +03"`).
* Fractions: `[.,]\d+` unlimited digits (A1, A2, A3, A5, A11 `\.\d+`, A6 `\.\d+`); A4 and A7 take none, so
  `22-SEP-2026 09:14:02:003` matches A4 up to `:02`, then `:?` eats the next `:` and `003] ...` stays in the message
  (golden `oracle_ebs_fnd_debug.log`: message `"003] [ICM] Started concurrent manager ..."`).
* `Z` wins over a following offset: `2026-10-14T09:30:00.215Z +03 [2231] FATAL: ...` captures `...215Z`, eats the
  space, and leaves `+03 [2231] FATAL: ...` (so the PostgreSQL rule no longer matches: golden `mut__postgres.log`).
* E10/E13 require **at least one whitespace character after the digits** (Python `\s`); a line that is only
  `1790068560`, or `1790068560]: x`, does not match them. E10 accepts 1–6 fraction digits only (7 digits → no match).
* Capture group 1 is the timestamp string; `m.end()` is after the trailing whitespace.

Go rewrite (exact): move the epoch alternatives out of group 1, give each its own group, and replace the lookahead by
consuming the whitespace. For E10/E13 the lookahead guarantees the next character is whitespace, so in Python
`\]?` and `:?` match empty and `\s*` consumes the whole whitespace run; `\s+` in Go consumes the same run.
Alternative order is preserved (A1..A11 first), so the selected alternative is identical.

```go
tsRE = regexp.MustCompile(pyRe(`^\[?(?:(` + A1_to_A11 + `)\]?:?\s*|(\d{10}(?:\.\d{1,6})?)\s+|(\d{13})\s+)`))
// group 1, 2 or 3 (whichever participated) == Python m[1]; match end == Python m.end()
```

### 1.2 `LEVEL_RE` — RE2 after the `re.I` fix

```text
^\[?(TRACE|DEBUG|INFO|INFORMATION|NOTICE|NOTE|SYSTEM|WARN(?:ING)?|ERR(?:OR)?|CRIT(?:ICAL)?|FATAL|SEVERE|EMERG(?:ENCY)?|ALERT|PANIC|CONFIG|FINE(?:R|ST)?|INF|WRN|FTL|DBG|VRB)\]?[:\s]+      flags: re.I
```

Group 1 keeps the **original spelling** (`"Warning"`, `"System"`, `"Note"`, `"error"`, `"INF"`); normalisation to the
canonical severity is part 02. Needs `\]?` then at least one `:` or whitespace: `ERROR` at end of line, `WARNx` do not
match; `WARN:x` does (rest `x`). Order matters only through backtracking (`INFORMATION: x` ends as `INFORMATION`).
Go: `(?i)` plus every `I` written `[Iiıİ]` (`ſ` for `S` and `K` for `K` are already handled by Go `(?i)`),
`[:\s]` → `[:` + pyWS + `]` (by `pyRe`).

### 1.3 `LOGGER_RE` — RE2 after class translation

```text
^\[?([A-Za-z][\w.\-]{2,})\]?:\s+
```

Group 1 is a logger / app name of at least 3 characters followed by `:` and whitespace. Used only when the name does not
end with `.go` (Go `file.go: msg` lines). `main.go:42: x` does not match at all (`:` is not in the class and `42` is
not whitespace).

### 1.4 `APP_BRACKET_RE`, `THREAD_BRACKET_RE` — RE2 after class translation

```text
APP_BRACKET_RE     ^\[([A-Za-z][\w.\-]{1,40})\]\s*          "[payment-api]" (2..41 chars, no spaces)
THREAD_BRACKET_RE  ^\[[^\]]{1,60}\]\s*                      any bracket of 1..60 non-']' code points
```

### 1.5 `THREAD_ID_RE` — **not RE2 as is** (lookahead)

```text
^\d+\s+(?=\[)                                               MySQL "12 [Warning]"
```

Go: `^\d+\s+\[` and use `end-1` as the match end (the `[` is one byte). Exact: `\s+` is greedy and `[` is not
whitespace, so the only possible end is right before the `[`.

### 1.6 `CODE_RE` — RE2 after class translation

```text
^[A-Z]{2,5}-\d{3,7}$                                        MySQL "MY-010116"
```

Applied to an `APP_BRACKET_RE` group (never contains `\n`, so Python `$` == Go `$`).

### 1.7 `KV_TS_RE` — RE2 after class translation

```text
^(?:time|ts|timestamp|date|@timestamp)="?([^"\s]+)"?\s+
```

Only the capture is used; `rest` is **not** advanced (the `time="..."` text stays in the message).

### 1.8 `CRI_RE` — RE2 after class translation

```text
^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z) (stdout|stderr) ([FP]) 
```

(note the trailing space). Groups: timestamp, stream, `F`/`P` flag.

### 1.9 `REDIS_RE` — **not RE2 as is** (lookahead)

```text
^(\d+):([MCSX]) (?=\d{2} [A-Z][a-z]{2} \d{4})
```

Go: `^(\d+):([MCSX]) \d{2} [A-Z][a-z]{2} \d{4}` and take the end as `end(group 2) + 1` (the space). Exact: everything
after group 2 is fixed-width.

### 1.10 `CONT_RE` — **not RE2 as is** (Unicode `\b`); only ever used as a boolean `match`

```text
^(\s|\tat |at [\w$.]+\(|\.\.\. \d+ (?:more|common frames)|Caused by:|Traceback \(most|  File \"|[\w$.]+(?:Exception|Error)(?::|\b)|System\.[\w.]+:|ORA-\d{5}|APP-FND-\d{5}|\*\*\*|\+-{10,}|-{10,}$)
```

A continuation line: starts with whitespace (so `\tat ` is redundant), a Java/.NET frame, `... N more`, `Caused by:`,
a Python traceback header or frame, an exception class name (`...Exception` / `...Error` followed by `:` or a word
boundary), `System.X:`, `ORA-` + 5 digits (no boundary: `ORA-825343` matches), `APP-FND-` + 5 digits, `***`,
`+----------` (10+ dashes), or a line of 10+ dashes only.
The `\b` after `Exception|Error` follows a word character, so it means "next character is not a Unicode word
character, or end". Go (boolean, exact):

```text
^(?:\s|\tat |at [\w$.]+\(|\.\.\. \d+ (?:more|common frames)|Caused by:|Traceback \(most|  File "|[\w$.]+(?:Exception|Error)(?::|[^\w]|$)|System\.[\w.]+:|ORA-\d{5}|APP-FND-\d{5}|\*\*\*|\+-{10,}|-{10,}$)
```

(then `pyRe`). `ValueErroré x` → no match in both; `ValueError` alone → match in both.

### 1.11 `EMBEDDED_TS_RE` — RE2 after class translation (used with `search`)

```text
(?:\*\*(?:Starts|Ends)\*\*|Current system time is |^\[)?(\d{2}-[A-Z]{3}-\d{4} \d{2}:\d{2}:\d{2}(?::\d{3})?)\]?
```

`^` inside the optional group is "start of string" in both engines. `search` = leftmost match, which Go
`FindStringSubmatchIndex` reproduces (including which prefix alternative is taken), so `m.end()` is identical.
Applied to `rest[:120]` (first 120 code points); byte offsets into the prefix are offsets into `rest`.

### 1.12 `APP_NAME_RE` — **not RE2 as is** (lookahead)

```text
^([a-z][a-z0-9]*(?:-[a-z0-9]+)+)(?=[\s:])
```

Go: `^([a-z][a-z0-9]*(?:-[a-z0-9]+)+)[\s:]`, use group 1 only (`rest` is not advanced). Exact: the group can only
contain `[a-z0-9-]`, so a shorter group would be followed by one of those characters, never by whitespace or `:`;
the only candidate is the maximal hyphenated run, in both engines.

### 1.13 `PG_RE` — RE2 after class translation

```text
^\[\d+(?:-\d+)?\] (?:[\w.\-]+@[\w.\-]+ )?(LOG|ERROR|FATAL|PANIC|WARNING|NOTICE|INFO|DEBUG\d?|DETAIL|STATEMENT|HINT|CONTEXT|QUERY):\s+
```

Must start at `rest` position 0, i.e. right after the timestamp: `[pid]` or `[pid-line]`, optional `user@db `,
level, `:` and whitespace (PostgreSQL writes two spaces). Case-sensitive.

### 1.14 `SEV_NUM_RE` — **not RE2 as is** (Unicode `\b`; used with `search` and its group)

```text
\bSeverity: (\d{1,2})\b
```

Go (exact, group 1 identical): `(?:^|[^\w])Severity: (\d{1,2})(?:[^\w]|$)`. Proof: Python matches at the first
position p where `Severity: ` is preceded by a non-word char (or p=0) and followed by 1–2 digits whose run ends
there (backtracking to 1 digit puts the boundary between two digits, which fails). The Go pattern matches starting at
p−1 (or 0) for exactly the same p, and the first such p is the same. `Severity: 145` → no match in either;
`xSeverity: 14` → no match. The digits may be any `\p{Nd}` (`int()` takes their Unicode values).

### 1.15 `DYNAMICS_RE` — RE2 after the `re.I` fix

```text
^(?:Microsoft\.Dynamics\.[\w.]+|Ax32Serv|NavServer)\s*[:\-]\s*            flags: re.I
```

Go: `(?i)^(?:M[Iiıİ]crosoft\.Dynam[Iiıİ]cs\.[\w.]+|Ax32Serv|NavServer)\s*[:\-]\s*` (then `pyRe`).
The service is `m[0].strip(" :-")` (whole match, trimmed of spaces, colons and hyphens only), so the original case is
kept (`"Ax32Serv"`, `"Microsoft.Dynamics.AX.Batch.BatchRun"`); a trailing `.` stays
(`Microsoft.Dynamics.AX.: x` → `"Microsoft.Dynamics.AX."`); `Ax32Serv\t: x` → `"Ax32Serv\t"` (tab kept).

### 1.16 Go reference implementation (verified, 0 differences)

```go
// text.go
package main

import (
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"
)

type kv struct {
	K string `json:"k"`
	V any    `json:"v"`
}
type rec []kv

func (r *rec) set(k string, v any) {
	for i := range *r {
		if (*r)[i].K == k {
			(*r)[i].V = v
			return
		}
	}
	*r = append(*r, kv{k, v})
}
func (r rec) has(k string) bool {
	for _, e := range r {
		if e.K == k {
			return true
		}
	}
	return false
}

const iI = `[Iiıİ]` // Python re.I view of the letter I

var (
	MON  = `(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)`
	MONU = `(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)`
	DOW  = `(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)`
	tsA  = `\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z| ?[+\-]\d{2}(?::?\d{2})?| UTC)?` +
		`|\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?:[.,]\d+)?` +
		`|\d{2}[./-]\d{2}[./-]\d{4}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?: ?[AP]M)?` +
		`|\d{1,2}-` + MONU + `-\d{4} \d{2}:\d{2}:\d{2}` +
		`|\d{1,2}-` + MON + `-\d{4} \d{2}:\d{2}:\d{2}(?:[.,]\d+)?` +
		`|\d{2} ` + MON + ` \d{4} \d{2}:\d{2}:\d{2}(?:\.\d+)?` +
		`|` + MON + ` \d{1,2} \d{4} \d{2}:\d{2}:\d{2}` +
		`|` + DOW + ` ` + MON + ` +\d{1,2} \d{2}:\d{2}:\d{2} \d{4}` +
		`|` + DOW + `, \d{1,2} ` + MON + ` \d{4} \d{2}:\d{2}:\d{2}(?: [+\-]\d{4})?` +
		`|` + MON + ` \d{1,2}, \d{4} \d{1,2}:\d{2}:\d{2} (?:AM|PM)` +
		`|\d{8}T\d{6}(?:\.\d+)?Z?`
	tsRE   = regexp.MustCompile(pyRe(`^\[?(?:(` + tsA + `)\]?:?\s*|(\d{10}(?:\.\d{1,6})?)\s+|(\d{13})\s+)`))
	levelRE = regexp.MustCompile(pyRe(`(?i)^\[?(TRACE|DEBUG|` + iI + `NFO|` + iI + `NFORMAT` + iI + `ON|NOT` + iI + `CE|NOTE|SYSTEM|WARN(?:` + iI + `NG)?|ERR(?:OR)?|CR` + iI + `T(?:` + iI + `CAL)?|FATAL|SEVERE|EMERG(?:ENCY)?|ALERT|PAN` + iI + `C|CONF` + iI + `G|F` + iI + `NE(?:R|ST)?|` + iI + `NF|WRN|FTL|DBG|VRB)\]?[:\s]+`))
	loggerRE        = regexp.MustCompile(pyRe(`^\[?([A-Za-z][\w.\-]{2,})\]?:\s+`))
	appBracketRE    = regexp.MustCompile(pyRe(`^\[([A-Za-z][\w.\-]{1,40})\]\s*`))
	threadBracketRE = regexp.MustCompile(pyRe(`^\[[^\]]{1,60}\]\s*`))
	threadIDRE      = regexp.MustCompile(pyRe(`^\d+\s+\[`)) // consumed '[' is given back: end-1
	codeRE          = regexp.MustCompile(pyRe(`^[A-Z]{2,5}-\d{3,7}$`))
	kvTsRE          = regexp.MustCompile(pyRe(`^(?:time|ts|timestamp|date|@timestamp)="?([^"\s]+)"?\s+`))
	criRE           = regexp.MustCompile(pyRe(`^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z) (stdout|stderr) ([FP]) `))
	redisRE         = regexp.MustCompile(pyRe(`^(\d+):([MCSX]) \d{2} [A-Z][a-z]{2} \d{4}`)) // rest starts at end of group 2 + 1
	contRE          = regexp.MustCompile(pyRe(`^(?:\s|\tat |at [\w$.]+\(|\.\.\. \d+ (?:more|common frames)|Caused by:|Traceback \(most|  File "|[\w$.]+(?:Exception|Error)(?::|[^\w]|$)|System\.[\w.]+:|ORA-\d{5}|APP-FND-\d{5}|\*\*\*|\+-{10,}|-{10,}$)`))
	embTsRE         = regexp.MustCompile(pyRe(`(?:\*\*(?:Starts|Ends)\*\*|Current system time is |^\[)?(\d{2}-[A-Z]{3}-\d{4} \d{2}:\d{2}:\d{2}(?::\d{3})?)\]?`))
	appNameRE       = regexp.MustCompile(pyRe(`^([a-z][a-z0-9]*(?:-[a-z0-9]+)+)[\s:]`))
	pgRE            = regexp.MustCompile(pyRe(`^\[\d+(?:-\d+)?\] (?:[\w.\-]+@[\w.\-]+ )?(LOG|ERROR|FATAL|PANIC|WARNING|NOTICE|INFO|DEBUG\d?|DETAIL|STATEMENT|HINT|CONTEXT|QUERY):\s+`))
	sevNumRE        = regexp.MustCompile(pyRe(`(?:^|[^\w])Severity: (\d{1,2})(?:[^\w]|$)`))
	dynamicsRE      = regexp.MustCompile(pyRe(`(?i)^(?:M` + iI + `crosoft\.Dynam` + iI + `cs\.[\w.]+|Ax32Serv|NavServer)\s*[:\-]\s*`))
)

var pgLevel = map[string]string{"LOG": "INFO", "DETAIL": "DETAIL", "STATEMENT": "STATEMENT", "HINT": "HINT", "CONTEXT": "CONTEXT", "QUERY": "STATEMENT"}
var redisMark = map[byte]string{'#': "WARN", '*': "INFO", '-': "INFO", '.': "DEBUG"}

func pyIsSpace(r rune) bool {
	switch {
	case r >= 0x09 && r <= 0x0d, r >= 0x1c && r <= 0x20, r == 0x85, r == 0xa0, r == 0x1680, r >= 0x2000 && r <= 0x200a,
		r == 0x2028, r == 0x2029, r == 0x202f, r == 0x205f, r == 0x3000:
		return true
	}
	return false
}
func pyStrip(s string) string { return strings.TrimFunc(s, pyIsSpace) }
func firstRunes(s string, n int) string {
	i := 0
	for k := 0; k < n && i < len(s); k++ {
		_, w := utf8.DecodeRuneInString(s[i:])
		i += w
	}
	return s[:i]
}
func pyInt(s string) int { // int() of a run of \p{Nd} digits
	n := 0
	for _, r := range s {
		n = n*10 + digitValue(r)
	}
	return n
}
func digitValue(r rune) int {
	if r >= '0' && r <= '9' {
		return int(r - '0')
	}
	z := r // Nd digits come in aligned runs of ten (0..9): value = distance from the run start, mod 10
	for unicode.Is(unicode.Nd, z-1) {
		z--
	}
	return int(r-z) % 10
}

// matchTS: Python TS_RE.match -> (group 1, end)
func matchTS(s string) (string, int, bool) {
	m := tsRE.FindStringSubmatchIndex(s)
	if m == nil {
		return "", 0, false
	}
	for g := 1; g <= 3; g++ {
		if m[2*g] >= 0 {
			return s[m[2*g]:m[2*g+1]], m[1], true
		}
	}
	panic("unreachable")
}

func parseHead(ln string) (rec, string) {
	var r rec
	rest := ln
	if m := criRE.FindStringSubmatchIndex(rest); m != nil {
		r.set("timestamp", rest[m[2]:m[3]])
		r.set("stream", rest[m[4]:m[5]])
		r.set("_partial", rest[m[6]:m[7]] == "P")
		rest = rest[m[1]:]
		if ts, end, ok := matchTS(rest); ok {
			r.set("timestamp", ts)
			rest = rest[end:]
		}
	} else if m := redisRE.FindStringSubmatchIndex(rest); m != nil {
		r.set("pid", rest[m[2]:m[3]])
		r.set("role", rest[m[4]:m[5]])
		rest = rest[m[5]+1:]
		if ts, end, ok := matchTS(rest); ok {
			r.set("timestamp", ts)
			rest = rest[end:]
		}
		if len(rest) >= 2 && rest[1] == ' ' {
			if sev, ok := redisMark[rest[0]]; ok {
				r.set("severity", sev)
				rest = rest[2:]
			}
		}
	} else if ts, end, ok := matchTS(rest); ok {
		r.set("timestamp", ts)
		rest = rest[end:]
	} else if m := kvTsRE.FindStringSubmatchIndex(rest); m != nil {
		r.set("timestamp", rest[m[2]:m[3]])
	}
	if m := threadIDRE.FindStringIndex(rest); m != nil {
		rest = rest[m[1]-1:]
	}
	if m := pgRE.FindStringSubmatchIndex(rest); m != nil {
		lvl := strings.ToUpper(rest[m[2]:m[3]]) // ASCII only: the group is case-sensitive ASCII
		sev, ok := pgLevel[lvl]
		if !ok {
			sev = lvl
		}
		r.set("severity", sev)
		rest = rest[m[1]:]
		r.set("pid", strings.TrimLeft(strings.SplitN(ln, "]", 2)[0], "["))
	} else if m := levelRE.FindStringSubmatchIndex(rest); m != nil {
		r.set("severity", rest[m[2]:m[3]])
		rest = rest[m[1]:]
		for it := 0; it < 3; it++ {
			b := appBracketRE.FindStringSubmatchIndex(rest)
			if b != nil && codeRE.MatchString(rest[b[2]:b[3]]) {
				r.set("code", rest[b[2]:b[3]])
				rest = rest[b[1]:]
			} else if b != nil && !r.has("service") {
				r.set("service", rest[b[2]:b[3]])
				rest = rest[b[1]:]
			} else if t := threadBracketRE.FindStringIndex(rest); t != nil {
				rest = rest[t[1]:]
			} else {
				break
			}
		}
	}
	if !r.has("severity") {
		if m := sevNumRE.FindStringSubmatch(rest); m != nil {
			n := pyInt(m[1])
			s := "INFO"
			if n >= 19 {
				s = "CRITICAL"
			} else if n >= 11 {
				s = "ERROR"
			}
			r.set("severity", s)
		}
	}
	if !r.has("timestamp") {
		pre := firstRunes(rest, 120)
		if m := embTsRE.FindStringSubmatchIndex(pre); m != nil {
			ts := pre[m[2]:m[3]]
			if strings.Count(ts, ":") == 3 {
				ts = firstRunes(ts, 19)
			}
			r.set("timestamp", ts)
			if strings.HasPrefix(rest, "[") {
				rest = pyStrip(rest[m[1]:])
			}
		}
	}
	if !r.has("service") {
		if m := dynamicsRE.FindStringIndex(rest); m != nil {
			r.set("service", strings.Trim(rest[:m[1]], " :-"))
			rest = rest[m[1]:]
		} else if m := loggerRE.FindStringSubmatchIndex(rest); m != nil && !strings.HasSuffix(rest[m[2]:m[3]], ".go") {
			r.set("service", rest[m[2]:m[3]])
			rest = rest[m[1]:]
		} else if m := appNameRE.FindStringSubmatchIndex(rest); m != nil {
			r.set("service", rest[m[2]:m[3]])
		} else if m := appBracketRE.FindStringSubmatchIndex(rest); m != nil {
			r.set("service", rest[m[2]:m[3]])
			rest = rest[m[1]:]
		}
	}
	return r, pyStrip(rest)
}
```

---

## 2. `parse_head(ln) -> (fields, rest)`

Pure function of one line (no state). Exact order of checks (Python `if/elif` structure):

1. `rest = ln`, `rec = {}`.
2. **Head prefix**, first that matches wins (`if / elif` chain):
   1. `CRI_RE.match(rest)` → `rec["timestamp"] = g1`, `rec["stream"] = g2`, `rec["_partial"] = (g3 == "P")` (bool),
      `rest = rest[end:]`; then if `TS_RE.match(rest)` → `rec["timestamp"] = ts` (overwrites the value, key stays first),
      `rest = rest[end:]` (the application's own timestamp is preferred).
   2. elif `REDIS_RE.match(rest)` → `rec["pid"] = g1`, `rec["role"] = g2`, `rest` after `"<pid>:<role> "`;
      then if `TS_RE.match(rest)` → `timestamp`, advance; then if `rest[:1]` is one of `# * - .` **and** `rest[1:2] == " "`
      → `rec["severity"] = REDIS_MARK[...]` (`#`→`WARN`, `*`→`INFO`, `-`→`INFO`, `.`→`DEBUG`), `rest = rest[2:]`.
   3. elif `TS_RE.match(rest)` → `rec["timestamp"] = ts`, `rest = rest[end:]`.
   4. elif `KV_TS_RE.match(rest)` → `rec["timestamp"] = g1`; `rest` unchanged.
3. `THREAD_ID_RE.match(rest)` → drop the leading thread id (`rest` now starts at `[`).
4. **Level**:
   * `PG_RE.match(rest)` → `lvl = g1.upper()`; `rec["severity"] = PG_LEVEL.get(lvl, lvl)` (so `LOG`→`INFO`,
     `QUERY`→`STATEMENT`, `DETAIL`/`STATEMENT`/`HINT`/`CONTEXT` kept, others as written: `ERROR`, `FATAL`, `PANIC`,
     `WARNING`, `NOTICE`, `INFO`, `DEBUG`, `DEBUG1`..`DEBUG9`); `rest = rest[end:]`;
     `rec["pid"] = ln.split("]")[0].lstrip("[")` — computed from the **original line**, so when a timestamp precedes
     the bracket the "pid" is `"2026-09-22 09:14:31.001 +03 [2231"` (this is what every `postgres.log` golden row has
     in `attributes.pid`).
   * elif `LEVEL_RE.match(rest)` → `rec["severity"] = g1` (original spelling), `rest = rest[end:]`, then up to **3**
     iterations:
     * `m = APP_BRACKET_RE.match(rest)`;
     * if `m` and `CODE_RE.match(m[1])` → `rec["code"] = m[1]` (a later code overwrites the value, key position kept),
       advance;
     * elif `m` and `"service"` not in `rec` → `rec["service"] = m[1]`, advance;
     * elif `THREAD_BRACKET_RE.match(rest)` → advance past it (thread names, `[KafkaServer id=1]`, a second
       `[name]` once a service is set);
     * else stop.
5. If no `severity` yet and `SEV_NUM_RE.search(rest)` → `n = int(g1)`; severity `CRITICAL` if `n >= 19`, `ERROR` if
   `n >= 11`, else `INFO`. `rest` unchanged.
6. If no `timestamp` yet and `EMBEDDED_TS_RE.search(rest[:120])` → `rec["timestamp"] = g1[:19] if g1.count(":") == 3
   else g1` (drops `:mmm` milliseconds); and **only if `rest` starts with `[`**: `rest = rest[m.end():].strip()`.
   Otherwise `rest` keeps the text (`"**Starts**22-SEP-2026 09:14:02"` stays the message).
7. If no `service` yet, first that matches:
   1. `DYNAMICS_RE.match(rest)` → `rec["service"] = m[0].strip(" :-")`, `rest = rest[end:]`;
   2. elif `LOGGER_RE.match(rest)` **and** `not g1.endswith(".go")` → `service = g1`, advance;
   3. elif `APP_NAME_RE.match(rest)` → `service = g1`, **`rest` not advanced** (`"payment-api gateway timeout"` keeps
      the name in the message);
   4. elif `APP_BRACKET_RE.match(rest)` → `service = g1`, advance.
8. Return `(rec, rest.strip())` (Python whitespace).

Key insertion order (observable through the report `keys`): keys appear in the order in which the steps above first
set them — step 2 (`timestamp, stream, _partial` | `pid, role[, timestamp][, severity]` | `timestamp`), step 4
(`severity, pid` for PostgreSQL | `severity` then `code` / `service` in bracket order: MySQL `[MY-010116] [Server]`
gives `code, service`), step 5 (`severity`), step 6 (`timestamp`, so `ERROR x 22-SEP-2026 09:14:02` gives
`severity, timestamp`), step 7 (`service`). Overwrites (`timestamp` in the CRI branch, repeated `code`) keep the first
position.

### 2.1 Examples (Python output; all are in the goldens)

| input:line | line | fields | rest |
|---|---|---|---|
| `dotnet_serilog.log`:1 | `2026-09-22 09:14:02.003 +03:00 [INF] Now listening on: https://[::]:5001` | `{"timestamp": "2026-09-22 09:14:02.003 +03:00", "severity": "INF"}` | `Now listening on: https://[::]:5001` |
| `dotnet_serilog.log`:4 | `System.Net.Http.HttpRequestException: The operation was canceled.` | `{"service": "System.Net.Http.HttpRequestException"}` | `The operation was canceled.` |
| `dotnet_serilog.log`:5 | `   at System.Net.Http.HttpClient.SendAsync(HttpRequestMessage request, CancellationToken c…` | `{}` | `at System.Net.Http.HttpClient.SendAsync(HttpRequestMessage r…` |
| `dynamics_365_batch.log`:1 | `2026-09-22T06:14:02.003Z INFO  Microsoft.Dynamics.AX.Batch.BatchRun: Batch job 4412 'Posti…` | `{"timestamp": "2026-09-22T06:14:02.003Z", "severity": "INFO", "service": "Microsoft.Dynamics.AX.Batch.BatchRun"}` | `Batch job 4412 'Posting: ledger journals' started on AOS D36…` |
| `dynamics_365_batch.log`:3 | `   at Dynamics.AX.Application.LedgerJournalPost.run() in xppSource//LedgerJournalPost.xpp:…` | `{}` | `at Dynamics.AX.Application.LedgerJournalPost.run() in xppSou…` |
| `dynamics_ax_aos.log`:3 | `2026-09-22 09:15:03 Ax32Serv: Error: Microsoft.Dynamics.AX.Kernel.CLRInterop - SqlExceptio…` | `{"timestamp": "2026-09-22 09:15:03", "service": "Ax32Serv"}` | `Error: Microsoft.Dynamics.AX.Kernel.CLRInterop - SqlExceptio…` |
| `dynamics_nav_bc.log`:2 | `2026-09-22 09:14:05 [NavServer] Session 88213 opened for user CORP\ali, company CRONUS Tür…` | `{"timestamp": "2026-09-22 09:14:05", "service": "NavServer"}` | `Session 88213 opened for user CORP\ali, company CRONUS Türki…` |
| `golang_log.log`:1 | `2026/09/22 09:14:02 main.go:42: starting order-service on :8080` | `{"timestamp": "2026/09/22 09:14:02"}` | `main.go:42: starting order-service on :8080` |
| `golang_log.log`:4 | `time="2026-09-22T09:16:00+03:00" level=info msg="user login ok" user=ali service=auth-api …` | `{"timestamp": "2026-09-22T09:16:00+03:00"}` | `time="2026-09-22T09:16:00+03:00" level=info msg="user login …` |
| `java_stacktrace.log`:1 | `2026-09-22 09:15:03.500 ERROR [payment-api] [http-nio-8080-exec-12] c.e.p.PaymentControlle…` | `{"timestamp": "2026-09-22 09:15:03.500", "severity": "ERROR", "service": "payment-api"}` | `c.e.p.PaymentController - charge failed order=88213` |
| `java_stacktrace.log`:2 | `java.net.SocketTimeoutException: Read timed out` | `{"service": "java.net.SocketTimeoutException"}` | `Read timed out` |
| `java_stacktrace.log`:3 | `	at java.base/java.net.SocketInputStream.socketRead0(Native Method)` | `{}` | `at java.base/java.net.SocketInputStream.socketRead0(Native M…` |
| `kafka_log4j.log`:1 | `[2026-09-22 09:14:02,003] INFO [KafkaServer id=1] started (kafka.server.KafkaServer)` | `{"timestamp": "2026-09-22 09:14:02,003", "severity": "INFO"}` | `started (kafka.server.KafkaServer)` |
| `kafka_log4j.log`:2 | `[2026-09-22 09:15:03,500] WARN [ReplicaFetcher replicaId=1, leaderId=2, fetcherId=0] Error…` | `{"timestamp": "2026-09-22 09:15:03,500", "severity": "WARN"}` | `Error in response for fetch request (kafka.server.ReplicaFet…` |
| `kafka_log4j.log`:5 | `[2026-09-22 09:15:10,000] ERROR [ReplicaManager broker=1] Error processing append operatio…` | `{"timestamp": "2026-09-22 09:15:10,000", "severity": "ERROR"}` | `Error processing append operation on partition orders-3 (kaf…` |
| `kubernetes_cri.log`:1 | `2026-09-22T06:14:02.003412Z stdout F INFO  order-service started on port 8080` | `{"timestamp": "2026-09-22T06:14:02.003412Z", "stream": "stdout", "_partial": false, "severity": "INFO", "service": "order-service"}` | `order-service started on port 8080` |
| `kubernetes_cri.log`:3 | `2026-09-22T06:15:03.501000Z stderr P Traceback (most recent call last):` | `{"timestamp": "2026-09-22T06:15:03.501000Z", "stream": "stderr", "_partial": true}` | `Traceback (most recent call last):` |
| `mysql_error.log`:1 | `2026-09-22T06:14:02.003412Z 0 [System] [MY-010116] [Server] /usr/sbin/mysqld (mysqld 8.0.3…` | `{"timestamp": "2026-09-22T06:14:02.003412Z", "severity": "System", "code": "MY-010116", "service": "Server"}` | `/usr/sbin/mysqld (mysqld 8.0.36) starting as process 4471` |
| `mysql_error.log`:2 | `2026-09-22T06:15:03.500981Z 12 [Warning] [MY-010055] [Server] IP address '192.168.7.4' cou…` | `{"timestamp": "2026-09-22T06:15:03.500981Z", "severity": "Warning", "code": "MY-010055", "service": "Server"}` | `IP address '192.168.7.4' could not be resolved: Name or serv…` |
| `nginx_error.log`:1 | `2026/09/22 09:15:03 [error] 771#771: *4412 upstream timed out (110: Connection timed out) …` | `{"timestamp": "2026/09/22 09:15:03", "severity": "error"}` | `771#771: *4412 upstream timed out (110: Connection timed out…` |
| `oracle_alert.log`:1 | `2026-09-22T09:14:02.112000+03:00` | `{"timestamp": "2026-09-22T09:14:02.112000+03:00"}` | `` |
| `oracle_alert.log`:2 | `Thread 1 advanced to log sequence 4412 (LGWR switch)` | `{}` | `Thread 1 advanced to log sequence 4412 (LGWR switch)` |
| `oracle_ebs_concurrent.log`:9 | `Current system time is 22-SEP-2026 09:14:02` | `{"timestamp": "22-SEP-2026 09:14:02"}` | `Current system time is 22-SEP-2026 09:14:02` |
| `oracle_ebs_concurrent.log`:13 | `**Starts**22-SEP-2026 09:14:02` | `{"timestamp": "22-SEP-2026 09:14:02"}` | `**Starts**22-SEP-2026 09:14:02` |
| `oracle_ebs_concurrent.log`:20 | `22-SEP-2026 09:15:10 APP-FND-01388: Cannot read value for profile option FND_SITE_NAME in …` | `{"timestamp": "22-SEP-2026 09:15:10", "service": "APP-FND-01388"}` | `Cannot read value for profile option FND_SITE_NAME in routin…` |
| `oracle_ebs_fnd_debug.log`:2 | `[22-SEP-2026 09:14:05:120] [OACORE] WARNING: session pool oacore_server1 at 90% capacity (…` | `{"timestamp": "22-SEP-2026 09:14:05"}` | `120] [OACORE] WARNING: session pool oacore_server1 at 90% ca…` |
| `plain_app_text.log`:2 | `2026-09-22 09:14:05 INFO  POST /api/v1/orders 201 118ms` | `{"timestamp": "2026-09-22 09:14:05", "severity": "INFO"}` | `POST /api/v1/orders 201 118ms` |
| `plain_app_text.log`:3 | `2026-09-22 09:15:03 ERROR payment-api gateway timeout order=88213` | `{"timestamp": "2026-09-22 09:15:03", "severity": "ERROR", "service": "payment-api"}` | `payment-api gateway timeout order=88213` |
| `postgres.log`:3 | `2026-09-22 09:15:03.500 +03 [4471] shop@prd ERROR:  deadlock detected` | `{"timestamp": "2026-09-22 09:15:03.500 +03", "severity": "ERROR", "pid": "2026-09-22 09:15:03.500 +03 [4471"}` | `deadlock detected` |
| `postgres.log`:4 | `2026-09-22 09:15:03.500 +03 [4471] shop@prd DETAIL:  Process 4471 waits for ShareLock on t…` | `{"timestamp": "2026-09-22 09:15:03.500 +03", "severity": "DETAIL", "pid": "2026-09-22 09:15:03.500 +03 [4471"}` | `Process 4471 waits for ShareLock on transaction 88213; block…` |
| `python_traceback.log`:1 | `2026-09-22 09:15:03,500 ERROR payment_api.charge: gateway call failed order=88213` | `{"timestamp": "2026-09-22 09:15:03,500", "severity": "ERROR", "service": "payment_api.charge"}` | `gateway call failed order=88213` |
| `python_traceback.log`:2 | `Traceback (most recent call last):` | `{}` | `Traceback (most recent call last):` |
| `redis.log`:2 | `1200:M 22 Sep 2026 09:15:03.500 # Client id=88213 addr=10.0.4.13:51423 laddr=10.0.9.40:637…` | `{"pid": "1200", "role": "M", "timestamp": "22 Sep 2026 09:15:03.500", "severity": "WARN"}` | `Client id=88213 addr=10.0.4.13:51423 laddr=10.0.9.40:6379 fd…` |
| `redis.log`:4 | `1200:C 22 Sep 2026 09:15:10.020 * DB saved on disk` | `{"pid": "1200", "role": "C", "timestamp": "22 Sep 2026 09:15:10.020", "severity": "INFO"}` | `DB saved on disk` |
| `sqlserver_errorlog.log`:2 | `2026-09-22 09:14:05.20 Logon       Error: 18456, Severity: 14, State: 8.` | `{"timestamp": "2026-09-22 09:14:05.20", "severity": "ERROR"}` | `Logon       Error: 18456, Severity: 14, State: 8.` |
| `sqlserver_errorlog.log`:4 | `2026-09-22 09:15:10.00 spid12s     SQL Server has encountered 3 occurrence(s) of I/O reque…` | `{"timestamp": "2026-09-22 09:15:10.00"}` | `spid12s     SQL Server has encountered 3 occurrence(s) of I/…` |
| `timestamp_variants.log`:1 | `22.09.2026 09:14:02 INFO order-service started` | `{"timestamp": "22.09.2026 09:14:02", "severity": "INFO", "service": "order-service"}` | `order-service started` |
| `timestamp_variants.log`:2 | `22/09/2026 09:14:05 INFO POST /api/v1/orders 201` | `{"timestamp": "22/09/2026 09:14:05", "severity": "INFO"}` | `POST /api/v1/orders 201` |
| `timestamp_variants.log`:3 | `09/22/2026 09:15:03 AM ERROR payment-api gateway timeout order=88213` | `{"timestamp": "09/22/2026 09:15:03 AM", "severity": "ERROR", "service": "payment-api"}` | `payment-api gateway timeout order=88213` |
| `timestamp_variants.log`:5 | `1790068560 INFO auth-api user login ok user=ali` | `{"timestamp": "1790068560", "severity": "INFO", "service": "auth-api"}` | `auth-api user login ok user=ali` |
| `timestamp_variants.log`:6 | `Tue, 22 Sep 2026 09:16:44 +0300 CRITICAL payment-api circuit breaker open` | `{"timestamp": "Tue, 22 Sep 2026 09:16:44 +0300", "severity": "CRITICAL", "service": "payment-api"}` | `payment-api circuit breaker open` |
| `timestamp_variants.log`:7 | `20260922T091700Z INFO auth-api session refreshed user=ali` | `{"timestamp": "20260922T091700Z", "severity": "INFO", "service": "auth-api"}` | `auth-api session refreshed user=ali` |
| `tomcat_juli.log`:2 | `22-Sep-2026 09:15:03.500 SEVERE [http-nio-8080-exec-12] org.apache.catalina.core.StandardW…` | `{"timestamp": "22-Sep-2026 09:15:03.500", "severity": "SEVERE", "service": "http-nio-8080-exec-12"}` | `org.apache.catalina.core.StandardWrapperValve.invoke Servlet…` |
| `tomcat_juli.log`:3 | `	java.net.SocketTimeoutException: Read timed out` | `{}` | `java.net.SocketTimeoutException: Read timed out` |

---

## 3. `TextParser.records(text)` (`name = "text"`, `kind = "log"`, `line_oriented = True`)

```python
lines = text.splitlines()
sample = [ln for ln in lines[:300] if ln.strip()]                 # first 300 physical lines, blanks removed
with_ts = sum(1 for ln in sample if "timestamp" in parse_head(ln)[0])
fold = with_ts >= max(1, 0.3 * len(sample))                       # float compare
family = detect_family(sample)                                    # section 4.3 (honours FAMILY_HINT)
prev = None
for i, ln in enumerate(lines, 1):                                 # i = 1-based physical line number
    if not ln.strip(): continue                                   # blank / whitespace-only lines vanish
    rec, rest = parse_head(ln)
    # (a) PostgreSQL detail lines belong to the record above, whatever their timestamp
    if rec.get("severity") in ("DETAIL", "STATEMENT", "HINT", "CONTEXT") and prev is not None:
        prev.message += f"\n{rec['severity']}: {rest}"            # no strip of the result
        prev._lines = prev.get("_lines", 1) + 1
        continue
    # (b) untimestamped line folds into the record above
    if "timestamp" not in rec and prev is not None and (fold or CONT_RE.match(ln) or prev.get("_partial")):
        if not prev.get("_partial"):
            prev.message = (prev.message + "\n" + ln.strip()).strip()
        else:                                                     # CRI 'P' (partial) record: glue without newline
            prev.message = prev.message + ln.strip()
        prev._lines = prev.get("_lines", 1) + 1
        prev._partial = rec.get("_partial", False)                # always False here (see below)
        continue
    # (c) a new record
    rec["message"] = rest
    if family and not rec.get("service"):
        rec["service"] = family
    if prev is not None: yield prev
    prev = (i, rec)
if prev is not None: yield prev
```

Rules the Go port must keep exactly:

* `fold`: `float64(withTS) >= math.Max(1, 0.3*float64(len(sample)))` (IEEE, same as Python). Empty sample →
  `fold = false` (and no records anyway). The `timestamp` test counts **any** `timestamp` key, including a `KV_TS_RE`
  or `EMBEDDED_TS_RE` one. Parse results of the sample can be cached: `parse_head` is pure.
* The fold test reads the **raw line** for `CONT_RE` (not `rest`), and folds `ln.strip()` (the whole stripped line,
  level words and all), never `rest`.
* When `fold` is false (a file where < 30 % of the first 300 non-blank lines carry a timestamp), only lines matching
  `CONT_RE` (or following a partial record) fold; other untimestamped lines become their own records
  (goldens: `mut__oracle_ebs_concurrent.log` whole and some live batches; live batches of `mut__java_stacktrace.log`,
  `mut__python_traceback.log`, `baseline.json`, `storm__alarms.json`).
* An untimestamped line with `prev is None` (first event of the text) starts a record without timestamp
  (`oracle_ebs_concurrent.log` line 1 `+-----...`). It is not dropped.
* The outer `.strip()` in the newline fold matters when the previous message is empty: a timestamp-only line
  (`oracle_alert.log` line 1 `2026-09-22T09:14:02.112000+03:00`, message `""`) followed by
  `Thread 1 advanced ...` gives message `"Thread 1 advanced ..."`, not `"\nThread 1 ..."`.
* `_partial`: only a CRI `P` line sets it `True`, and only on a record **with** a timestamp; a line that folds has no
  timestamp and therefore no CRI prefix, so `prev._partial` is set to `False` after every fold. Consequences: (1) a
  `P` record is glued (no newline) only to an *untimestamped* next line; a following CRI line always has a timestamp
  and starts a new record, so in the goldens `_partial` stays `true` on `P` records (`kubernetes_cri.log` line 3);
  (2) every record that absorbed at least one non-PG continuation gets `_partial: False` appended after `_lines`
  (that is why `_partial: false` appears next to `_lines` in many golden attributes). The PG-detail path (a) never
  touches `_partial`.
* Key order of a record: parse-head keys, `message`, then `service` (family default, only if the record has no truthy
  service), then `_lines`, then `_partial` (if not already present). Report `keys` example `postgres.log`:
  `["timestamp", "severity", "pid", "message", "service", "_lines"]`.
* `family` default service: applied when `family != ""` and the record has no service (parse_head never produces an
  empty service string, so "no service" == key absent). It is applied when the record is created, before folding.
* Line numbers count every physical line of `pySplitLines(text)`, blank ones included; a record's `line_no` is its first
  line, and `raw` (part 01, 9 step 10) is that first physical line only, even for a folded multi-line message.

What `records_to_observations` then does with these records (part 01, 9): roles come from `auto_map` over the first 50
records (part 02); for text records the roles found in the goldens are `timestamp`, `message`, `service` and, when any
of the first 50 records has one, `severity`. Every other key (`stream`, `_partial`, `pid`, `role`, `code`, `_lines`)
becomes an attribute **only when the record has more keys than the number of distinct role keys in use** (part 01, 9
step 6): e.g. `kubernetes_cri.log` line 3 (`timestamp, stream, _partial, message` = 4 keys, 4 roles in use) has no
`stream`/`_partial` attributes, while line 1 (5 keys) has both. Attribute values keep their type: `_lines` int,
`_partial` bool, the rest strings.

---

## 4. `parsers/family.py`

### 4.1 `FAMILIES` (order matters: earlier wins ties) and their RE2 forms

Python (verbatim, each family has a list of patterns, all used with `search`, case-sensitive):

```text
postgres      \] (?:[\w@]+ )?(?:LOG|ERROR|FATAL|DETAIL|STATEMENT|HINT|WARNING|PANIC):  
mysql         \[MY-\d{6}\]
              \bmysqld\b
sqlserver     ^\S+ \S+ +(?:spid\d+s?|Logon|Backup|Server) +
              Microsoft SQL Server
oracle-ebs    \bAPP-FND-\d{5}\b|\bConcurrent Manager\b|\*\*Starts\*\*|\bRequest ID:|\bFNDLIBR\b|\bAPPLCSF\b|\bOACORE\b|\bWLS_FORMS\b
oracle        \bORA-\d{5}\b|\bTNS-\d{5}\b|\bLGWR\b|\bARC\d\b|\balert_\w+\.log\b|advanced to log sequence
dynamics-ax   Microsoft\.Dynamics\.AX|\bAx32Serv\b|\bAOS\b.*(?:session|instance)|Dynamics AX
dynamics-365  Microsoft\.Dynamics\.(?:365|Ax\.Xpp|Platform)|\bD365\b|\bSysOperation\w*\b|Dynamics 365|\bBatchJob\b|\bDMF\w*\b
dynamics-nav  Microsoft Dynamics NAV|Business Central|\bNavServer\b|\bMicrosoft\.Dynamics\.Nav\b|\bNAV Server\b|\bBC\d{2}\b
sap           \bSAP\b|\bABAP\b|\bNetWeaver\b|\bHANA\b|\bSM21\b|\bdisp\+work\b
redis         ^\d+:[MCSX] \d{2} \w{3} \d{4}
nginx         \bnginx\b|^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} \[\w+\] \d+#\d+:
apache        \[(?:core|mpm_\w+|ssl|proxy\w*|rewrite):\w+\] \[pid \d+
kafka         \(kafka\.[\w.]+\)|\bKafkaServer\b|\bReplicaManager\b
zookeeper     \(org\.apache\.zookeeper
elasticsearch \[o\.e\.[\w.]+\]|\bElasticsearch\b
haproxy       \bhaproxy\[\d+\]
postfix       \bpostfix/\w+\[\d+\]
sshd          \bsshd\[\d+\]
iis           ^#Software: Microsoft Internet Information Services
windows       <Event xmlns=|EventID|Event ID
java          ^\s+at [\w$.]+\([\w.]*:\d+\)|^[\w.]+Exception: |\bcommon frames omitted\b
python        ^Traceback \(most recent call last\):|^  File \"[^\"]+\", line \d+
dotnet        ^\s+at [\w.]+\.[\w`]+\(.*\) in .*\.cs:line \d+|^System\.[\w.]+Exception
golang        ^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} \w+\.go:\d+:|^goroutine \d+ \[
```

(The postgres pattern ends with **two** spaces after the colon.)

RE2 status: no lookarounds; the only problem is Unicode `\b` (plus the usual `\d \w \s \S` translation). Every `\b`
sits next to a word character inside the pattern, so the boolean rewrites of section 0 apply. Two trailing `\b` are
redundant and dropped: `\bSysOperation\w*\b` and `\bDMF\w*\b` (the greedy `\w*` always ends at a word boundary;
a shorter `\w*` would put the boundary between two word characters). `.` (in `.*`) is "not `\n`" in both engines.
In Go source, the dotnet class contains a back-quote, so that one pattern cannot be a raw-string literal as written.

```go
// family.go
package main

import "regexp"

// \bX -> (?:^|[^\w])X  ;  X\b -> X(?:[^\w]|$)   (boolean search only)
const lb = `(?:^|[^\w])`
const rb = `(?:[^\w]|$)`

type famDef struct {
	name string
	pats []*regexp.Regexp
}

func mk(ps ...string) []*regexp.Regexp {
	out := []*regexp.Regexp{}
	for _, p := range ps {
		out = append(out, regexp.MustCompile(pyRe(p)))
	}
	return out
}

var families = []famDef{
	{"postgres", mk(`\] (?:[\w@]+ )?(?:LOG|ERROR|FATAL|DETAIL|STATEMENT|HINT|WARNING|PANIC):  `)},
	{"mysql", mk(`\[MY-\d{6}\]`, lb+`mysqld`+rb)},
	{"sqlserver", mk(`^\S+ \S+ +(?:spid\d+s?|Logon|Backup|Server) +`, `Microsoft SQL Server`)},
	{"oracle-ebs", mk(lb + `APP-FND-\d{5}` + rb + `|` + lb + `Concurrent Manager` + rb + `|\*\*Starts\*\*|` + lb + `Request ID:|` + lb + `FNDLIBR` + rb + `|` + lb + `APPLCSF` + rb + `|` + lb + `OACORE` + rb + `|` + lb + `WLS_FORMS` + rb)},
	{"oracle", mk(lb + `ORA-\d{5}` + rb + `|` + lb + `TNS-\d{5}` + rb + `|` + lb + `LGWR` + rb + `|` + lb + `ARC\d` + rb + `|` + lb + `alert_\w+\.log` + rb + `|advanced to log sequence`)},
	{"dynamics-ax", mk(`Microsoft\.Dynamics\.AX|` + lb + `Ax32Serv` + rb + `|` + lb + `AOS` + rb + `.*(?:session|instance)|Dynamics AX`)},
	{"dynamics-365", mk(`Microsoft\.Dynamics\.(?:365|Ax\.Xpp|Platform)|` + lb + `D365` + rb + `|` + lb + `SysOperation\w*|Dynamics 365|` + lb + `BatchJob` + rb + `|` + lb + `DMF\w*`)},
	{"dynamics-nav", mk(`Microsoft Dynamics NAV|Business Central|` + lb + `NavServer` + rb + `|` + lb + `Microsoft\.Dynamics\.Nav` + rb + `|` + lb + `NAV Server` + rb + `|` + lb + `BC\d{2}` + rb)},
	{"sap", mk(lb + `SAP` + rb + `|` + lb + `ABAP` + rb + `|` + lb + `NetWeaver` + rb + `|` + lb + `HANA` + rb + `|` + lb + `SM21` + rb + `|` + lb + `disp\+work` + rb)},
	{"redis", mk(`^\d+:[MCSX] \d{2} \w{3} \d{4}`)},
	{"nginx", mk(lb + `nginx` + rb + `|^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} \[\w+\] \d+#\d+:`)},
	{"apache", mk(`\[(?:core|mpm_\w+|ssl|proxy\w*|rewrite):\w+\] \[pid \d+`)},
	{"kafka", mk(`\(kafka\.[\w.]+\)|` + lb + `KafkaServer` + rb + `|` + lb + `ReplicaManager` + rb)},
	{"zookeeper", mk(`\(org\.apache\.zookeeper`)},
	{"elasticsearch", mk(`\[o\.e\.[\w.]+\]|` + lb + `Elasticsearch` + rb)},
	{"haproxy", mk(lb + `haproxy\[\d+\]`)},
	{"postfix", mk(lb + `postfix/\w+\[\d+\]`)},
	{"sshd", mk(lb + `sshd\[\d+\]`)},
	{"iis", mk(`^#Software: Microsoft Internet Information Services`)},
	{"windows", mk(`<Event xmlns=|EventID|Event ID`)},
	{"java", mk(`^\s+at [\w$.]+\([\w.]*:\d+\)|^[\w.]+Exception: |` + lb + `common frames omitted` + rb)},
	{"python", mk(`^Traceback \(most recent call last\):|^  File "[^"]+", line \d+`)},
	{"dotnet", mk(`^\s+at [\w.]+\.[\w` + "`" + `]+\(.*\) in .*\.cs:line \d+|^System\.[\w.]+Exception`)},
	{"golang", mk(`^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} \w+\.go:\d+:|^goroutine \d+ \[`)},
}
```

### 4.2 `STRONG`

```python
STRONG = {"oracle-ebs", "dynamics-ax", "dynamics-365", "dynamics-nav", "sap", "iis", "redis", "postgres", "mysql",
          "sqlserver", "kafka", "haproxy", "postfix", "java", "python", "dotnet", "golang"}
```

Not strong (need >= 30 % of the sample): `oracle`, `nginx`, `apache`, `zookeeper`, `elasticsearch`, `sshd`, `windows`.

### 4.3 `detect_family(lines)` and `_detect_family(lines)`

```python
def detect_family(lines):
    hinted = FAMILY_HINT.get()
    if hinted is not None:            # includes "" : an empty hint disables detection
        FAMILY_SEEN.set(hinted); return hinted
    fam = _detect_family(lines); FAMILY_SEEN.set(fam); return fam

def _detect_family(lines):
    sample = [ln for ln in lines[:200] if ln.strip()]
    if not sample: return ""
    best, best_hits = "", 0
    for fam, pats in FAMILIES:                       # list order
        hits = sum(1 for ln in sample if any(p.search(ln) for p in pats))   # a line counts once per family
        if hits > best_hits:                         # strict: the earlier family keeps a tie
            best, best_hits = fam, hits
    if not best: return ""
    if best in STRONG and best_hits >= 1: return best
    return best if best_hits / len(sample) >= 0.3 else ""
```

* The text parser passes its `sample` (non-blank lines among the first 300), so `_detect_family` looks at the first
  **200 non-blank lines within the first 300 physical lines**.
* Only the family with the most hits is considered. If it is weak and below 30 %, the result is `""` even when a
  strong family has fewer hits (no fallback to the runner-up). Golden: `mut__oracle_alert.log` (oracle 25/200, mutated
  `ORA-` codes have 6 digits so `\bORA-\d{5}\b` misses most lines) → `""`.
* A strong family wins with a single hit as long as it has the maximum. Goldens: `dotnet_serilog.log`
  `{dotnet: 2, java: 1}` → dotnet; `kafka_log4j.log` `{kafka: 4, java: 2}` → kafka;
  `dynamics_365_batch.log` `{dynamics-ax: 4, dynamics-365: 3}` → **dynamics-ax** (no golden ever yields
  `dynamics-365`); `oracle_ebs_concurrent.log` `{oracle-ebs: 5, oracle: 2}` → oracle-ebs.
* `best_hits / len(sample) >= 0.3`: float64 division, `>=`.

### 4.4 `FAMILY_HINT` / `FAMILY_SEEN` contextvars

`FAMILY_HINT` (default `None`) is set by `pipeline._parse_one` to `hint["family"]` whenever the hint dict has a
`family` key (part 01, 6 step 4) — in the live path after the first batch of a stream, including `""`.
`FAMILY_SEEN` is reset to `None` before parsing and read after it; it becomes the report's `family` (`None` → `""`).
Only `TextParser.records` calls `detect_family`, so `family` is `""` in the report of every non-text format.

Go: no globals. Pass `familyHint *string` into the text parser; return the decided family next to the rows.
Semantics: hint non-nil → use it verbatim (even `""`), no detection; else detect. The probe run of `_parse_one`
(first 50 records of `text[:400000]`, used only for the report `keys` / `roles`) also calls it, but the full run's
value is the one reported. The probe's family (and therefore the `service` key of its records) can only differ from the
full run's when the 400 000-code-point cut falls inside the first 300 lines; otherwise one detection serves both.

### 4.5 How a family changes parsing

Only two effects:

1. The text parser assigns `service = family` to every new record that has no service of its own (`postgres`,
   `redis`, `nginx`, `golang`, `oracle-ebs`, ... in the goldens). Records whose head yielded a service keep it
   (`dynamics_*`: `Ax32Serv`, `NavServer`, `Microsoft.Dynamics.AX.Batch.BatchRun`; `mysql`: `Server`, `InnoDB`;
   `java`: `payment-api`; `python`: `payment_api.charge`).
2. The report's `family` field (part 01, 6 step 13), and through it the live hint of later batches.

The family does not select regexes, does not change folding, severity or timestamps. (`grok` line packs have their own
`family` field, section 5.2, which plays the same "default service" role for grok records but is unrelated to
`FAMILIES`.)

### 4.6 Family per golden input (format `text`)

| family | inputs (whole; live identical unless noted) |
|---|---|
| `dotnet` | `dotnet_serilog.log`, `mut__dotnet_serilog.log` |
| `dynamics-ax` | `dynamics_365_batch.log`, `dynamics_ax_aos.log`, `mut__dynamics_365_batch.log`; `mut__dynamics_ax_aos.log` live only (whole run is `syslog`) |
| `dynamics-nav` | `dynamics_nav_bc.log`, `mut__dynamics_nav_bc.log` |
| `golang` | `golang_log.log`, `mut__golang_log.log` |
| `java` | `java_stacktrace.log`, `mut__java_stacktrace.log` |
| `kafka` | `kafka_log4j.log` (`mut__kafka_log4j.log` is `sap`) |
| `mysql` | `mysql_error.log`, `mut__mysql_error.log` |
| `nginx` | `nginx_error.log`, `mut__nginx_error.log` (weak family, >= 30 %) |
| `oracle` | `oracle_alert.log` (weak, 4 of 10 lines) |
| `oracle-ebs` | `oracle_ebs_concurrent.log`, `oracle_ebs_fnd_debug.log`, `mut__oracle_ebs_concurrent.log`, `mut__oracle_ebs_fnd_debug.log` |
| `postgres` | `postgres.log`, `mut__postgres.log` |
| `python` | `python_traceback.log`, `mut__python_traceback.log` |
| `redis` | `redis.log`, `mut__redis.log` |
| `sqlserver` | `sqlserver_errorlog.log` (`mut__sqlserver_errorlog.log` is `syslog`) |
| `""` | `plain_app_text.log`, `timestamp_variants.log`, `tomcat_juli.log`, `kubernetes_cri.log`, `demo__checkout.log`, `mut__oracle_alert.log` (below threshold), the `mut__` variants of the first four; live `text` batches of `baseline.json` and `storm__alarms.json` |

---

## 5. `grok.py`

### 5.1 `BASE` (verbatim, insertion order) and 5.2 `LINES` (verbatim)

```text
USERNAME             [a-zA-Z0-9._-]+
USER                 %{USERNAME}
EMAILLOCALPART       [a-zA-Z][a-zA-Z0-9_.+-=:]+
EMAILADDRESS         %{EMAILLOCALPART}@%{HOSTNAME}
INT                  (?:[+-]?(?:[0-9]+))
BASE10NUM            (?<![0-9.+-])(?:[+-]?(?:(?:[0-9]+(?:\.[0-9]+)?)|(?:\.[0-9]+)))
NUMBER               (?:%{BASE10NUM})
BASE16NUM            (?<![0-9A-Fa-f])(?:[+-]?(?:0x)?(?:[0-9A-Fa-f]+))
POSINT               \b(?:[1-9][0-9]*)\b
NONNEGINT            \b(?:[0-9]+)\b
WORD                 \b\w+\b
NOTSPACE             \S+
SPACE                \s*
DATA                 .*?
GREEDYDATA           .*
QUOTEDSTRING         (?:\"(?:\\.|[^\\\"])*\"|'(?:\\.|[^\\'])*'|`(?:\\.|[^\\`])*`)
QS                   %{QUOTEDSTRING}
UUID                 [A-Fa-f0-9]{8}-(?:[A-Fa-f0-9]{4}-){3}[A-Fa-f0-9]{12}
MAC                  (?:%{CISCOMAC}|%{WINDOWSMAC}|%{COMMONMAC})
CISCOMAC             (?:[A-Fa-f0-9]{4}\.){2}[A-Fa-f0-9]{4}
WINDOWSMAC           (?:[A-Fa-f0-9]{2}-){5}[A-Fa-f0-9]{2}
COMMONMAC            (?:[A-Fa-f0-9]{2}:){5}[A-Fa-f0-9]{2}
IPV6                 (?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,7}:|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}|(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}|:(?:(?::[0-9A-Fa-f]{1,4}){1,7}|:)|::(?:ffff(?::0{1,4})?:)?(?:(?:25[0-5]|(?:2[0-4]|1?[0-9])?[0-9])\.){3}(?:25[0-5]|(?:2[0-4]|1?[0-9])?[0-9]))
IPV4                 (?<![0-9])(?:(?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5]))(?![0-9])
IP                   (?:%{IPV6}|%{IPV4})
HOSTNAME             \b(?:[0-9A-Za-z][0-9A-Za-z-]{0,62})(?:\.(?:[0-9A-Za-z][0-9A-Za-z-]{0,62}))*(?:\.?|\b)
IPORHOST             (?:%{IP}|%{HOSTNAME})
HOSTPORT             %{IPORHOST}:%{POSINT}
PATH                 (?:%{UNIXPATH}|%{WINPATH})
UNIXPATH             (/[\w_%!$@:.,+~-]*)+
WINPATH              (?:[A-Za-z]:|\\)(?:\\[^\\?*]*)+
TTY                  (?:/dev/(pts|tty(?:[pq])?)(?:\w+)?/?(?:[0-9]+))
URIPROTO             [A-Za-z]([A-Za-z0-9+\-.]+)+
URIHOST              %{IPORHOST}(?::%{POSINT:port})?
URIPATH              (?:/[A-Za-z0-9$.+!*'(){},~:;=@#%&_\-]*)+
URIPARAM             \?[A-Za-z0-9$.+!*'|(){},~@#%&/=:;_?\-\[\]<>]*
URIPATHPARAM         %{URIPATH}(?:%{URIPARAM})?
URI                  %{URIPROTO}://(?:%{USER}(?::[^@]*)?@)?(?:%{URIHOST})?(?:%{URIPATHPARAM})?
MONTH                \b(?:[Jj]an(?:uary|uar)?|[Ff]eb(?:ruary|ruar)?|[Mm](?:a|ä)?r(?:ch|z)?|[Aa]pr(?:il)?|[Mm]a(?:y|i)?|[Jj]un(?:e|i)?|[Jj]ul(?:y|i)?|[Aa]ug(?:ust)?|[Ss]ep(?:tember)?|[Oo](?:c|k)?t(?:ober)?|[Nn]ov(?:ember)?|[Dd]e(?:c|z)(?:ember)?)\b
MONTHNUM             (?:0?[1-9]|1[0-2])
MONTHNUM2            (?:0[1-9]|1[0-2])
MONTHDAY             (?:(?:0[1-9])|(?:[12][0-9])|(?:3[01])|[1-9])
DAY                  (?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)
YEAR                 (?>\d\d){1,2}
HOUR                 (?:2[0123]|[01]?[0-9])
MINUTE               (?:[0-5][0-9])
SECOND               (?:(?:[0-5]?[0-9]|60)(?:[:.,][0-9]+)?)
TIME                 (?!<[0-9])%{HOUR}:%{MINUTE}(?::%{SECOND})(?![0-9])
DATE_US              %{MONTHNUM}[/-]%{MONTHDAY}[/-]%{YEAR}
DATE_EU              %{MONTHDAY}[./-]%{MONTHNUM}[./-]%{YEAR}
ISO8601_TIMEZONE     (?:Z|[+-]%{HOUR}(?::?%{MINUTE}))
ISO8601_SECOND       (?:%{SECOND}|60)
TIMESTAMP_ISO8601    %{YEAR}-%{MONTHNUM}-%{MONTHDAY}[T ]%{HOUR}:?%{MINUTE}(?::?%{SECOND})?%{ISO8601_TIMEZONE}?
DATE                 %{DATE_US}|%{DATE_EU}
DATESTAMP            %{DATE}[- ]%{TIME}
TZ                   (?:[APMCE][SD]T|UTC|GMT|TRT)
DATESTAMP_RFC822     %{DAY} %{MONTH} %{MONTHDAY} %{YEAR} %{TIME} %{TZ}
DATESTAMP_RFC2822    %{DAY}, %{MONTHDAY} %{MONTH} %{YEAR} %{TIME} %{ISO8601_TIMEZONE}
DATESTAMP_OTHER      %{DAY} %{MONTH} %{MONTHDAY} %{TIME} %{TZ} %{YEAR}
DATESTAMP_EVENTLOG   %{YEAR}%{MONTHNUM2}%{MONTHDAY}%{HOUR}%{MINUTE}%{SECOND}
HTTPDERROR_DATE      %{DAY} %{MONTH} %{MONTHDAY} %{TIME} %{YEAR}
SYSLOGTIMESTAMP      %{MONTH} +%{MONTHDAY} %{TIME}
PROG                 [\x21-\x5a\x5c\x5e-\x7e]+
SYSLOGPROG           %{PROG:program}(?:\[%{POSINT:pid}\])?
SYSLOGHOST           %{IPORHOST}
SYSLOGFACILITY       <%{NONNEGINT:facility}.%{NONNEGINT:priority}>
HTTPDATE             %{MONTHDAY}/%{MONTH}/%{YEAR}:%{TIME} %{INT}
LOGLEVEL             (?:[Aa]lert|ALERT|[Tt]race|TRACE|[Dd]ebug|DEBUG|[Nn]otice|NOTICE|[Ii]nfo?(?:rmation)?|INFO?(?:RMATION)?|[Ww]arn?(?:ing)?|WARN?(?:ING)?|[Ee]rr?(?:or)?|ERR?(?:OR)?|[Cc]rit?(?:ical)?|CRIT?(?:ICAL)?|[Ff]atal|FATAL|[Ss]evere|SEVERE|EMERG(?:ENCY)?|[Ee]merg(?:ency)?)
JAVACLASS            (?:[a-zA-Z$_][a-zA-Z$_0-9]*\.)*[a-zA-Z$_][a-zA-Z$_0-9]*
JAVAFILE             (?:[a-zA-Z$_0-9. -]+)
JAVAMETHOD           (?:(<(?:cl)?init>)|[a-zA-Z$_][a-zA-Z$_0-9]*)
JAVASTACKTRACEPART   %{SPACE}at %{JAVACLASS:class}\.%{JAVAMETHOD:method}\(%{JAVAFILE:file}(?::%{NUMBER:line})?\)
JAVALOGMESSAGE       (?:.*)
CATALINA_DATESTAMP   (?:%{MONTH} %{MONTHDAY}, %{YEAR} %{HOUR}:%{MINUTE}:%{SECOND} (?:AM|PM))
TOMCAT_DATESTAMP     %{YEAR}-%{MONTHNUM}-%{MONTHDAY} %{HOUR}:%{MINUTE}:%{SECOND}(?:,%{INT})? %{ISO8601_TIMEZONE}?
SQUID_STATUS         (?:TCP|UDP|NONE)_[A-Z_]+
NAGIOSTIME           \[%{NUMBER:nagios_epoch}\]
QUANTITY             %{NUMBER}\s?(?:ms|us|ns|s|sec|m|min|h|%|[KMGT]i?B|[kmgt]b|B)
```

LINES (order matters: detection keeps the first pattern on ties):

1. **SYSLOGLINE** — family `syslog`, roles `{"timestamp": "timestamp", "host": "host", "service": "program", "message": "message"}` — BSD syslog line

   ```text
   ^%{SYSLOGTIMESTAMP:timestamp} (?:%{SYSLOGFACILITY} )?%{SYSLOGHOST:host} %{SYSLOGPROG}: %{GREEDYDATA:message}$
   ```

2. **COMMONAPACHELOG** — family `http`, roles `{"timestamp": "timestamp", "message": "request", "status": "response"}` — Apache / nginx common log

   ```text
   ^%{IPORHOST:clientip} %{USER:ident} %{USER:auth} \[%{HTTPDATE:timestamp}\] "(?:%{WORD:verb} %{NOTSPACE:request}(?: HTTP/%{NUMBER:httpversion})?|%{DATA:rawrequest})" %{NUMBER:response} (?:%{NUMBER:bytes}|-)
   ```

3. **COMBINEDAPACHELOG** — family `http`, roles `{"timestamp": "timestamp", "message": "request", "status": "response"}` — Apache / nginx combined log

   ```text
   ^%{IPORHOST:clientip} %{USER:ident} %{USER:auth} \[%{HTTPDATE:timestamp}\] "(?:%{WORD:verb} %{NOTSPACE:request}(?: HTTP/%{NUMBER:httpversion})?|%{DATA:rawrequest})" %{NUMBER:response} (?:%{NUMBER:bytes}|-) %{QS:referrer} %{QS:agent}
   ```

4. **HTTPD_ERRORLOG** — family `apache`, roles `{"timestamp": "timestamp", "severity": "loglevel", "message": "message"}` — Apache httpd error log (2.4)

   ```text
   ^\[%{HTTPDERROR_DATE:timestamp}\] \[(?:%{WORD:module}:)?%{LOGLEVEL:loglevel}\] \[pid %{POSINT:pid}(?::tid %{NUMBER:tid})?\](?: \[client %{IPORHOST:clientip}(?::%{POSINT:clientport})?\])? %{GREEDYDATA:message}$
   ```

5. **TOMCAT_CATALINA** — family `tomcat`, roles `{"timestamp": "timestamp", "service": "class"}` — Tomcat catalina.out header line

   ```text
   ^%{CATALINA_DATESTAMP:timestamp} %{JAVACLASS:class} %{JAVAMETHOD:method}\s*$
   ```

6. **TOMCAT_JULI** — family `tomcat`, roles `{"timestamp": "timestamp", "severity": "level", "service": "class", "message": "message"}` — Tomcat 8.5+ JULI one-line format

   ```text
   ^%{TOMCAT_DATESTAMP:timestamp} %{LOGLEVEL:level} \[%{DATA:thread}\] %{JAVACLASS:class}\.%{JAVAMETHOD:method} %{GREEDYDATA:message}$
   ```

7. **SQUID_ACCESS** — family `squid`, roles `{"timestamp": "timestamp", "message": "request", "status": "response"}` — Squid native access log

   ```text
   ^%{NUMBER:timestamp}\s+%{NUMBER:duration}\s+%{IP:clientip}\s+%{SQUID_STATUS:squid_status}/%{NUMBER:response}\s+%{NUMBER:bytes}\s+%{WORD:verb}\s+%{NOTSPACE:request}\s+%{NOTSPACE:user}\s+%{NOTSPACE:hierarchy}\s+%{NOTSPACE:content_type}
   ```

8. **BIND9_QUERY** — family `bind`, roles `{"severity": "level", "message": "query"}` — BIND 9 query log

   ```text
   ^%{MONTHDAY:day}-%{MONTH:month}-%{YEAR:year} %{TIME:time}(?:\.%{INT:ms})? (?:queries: )?(?:%{LOGLEVEL:level}: )?client(?: @0x[0-9a-f]+)? %{IP:clientip}#%{POSINT:clientport}(?: \(%{DATA:qname}\))?: query: %{NOTSPACE:query} %{WORD:qclass} %{WORD:qtype} %{GREEDYDATA:flags}$
   ```

9. **IPTABLES** — family `iptables`, roles `{"timestamp": "timestamp", "host": "host", "message": "prefix"}` — iptables / ufw / nftables kernel log

   ```text
   ^(?:%{SYSLOGTIMESTAMP:timestamp} %{SYSLOGHOST:host} kernel: )?(?:\[\s*%{NUMBER:uptime}\] )?\[?%{DATA:prefix}\]?\s*IN=%{DATA:in_if} OUT=%{DATA:out_if} (?:MAC=%{DATA:mac} )?SRC=%{IP:src} DST=%{IP:dst} %{GREEDYDATA:rest}$
   ```

10. **NAGIOS_LOG** — family `nagios`, roles `{"timestamp": "nagios_epoch", "message": "message"}` — Nagios / Icinga nagios.log

   ```text
   ^%{NAGIOSTIME} (?:%{WORD:nagios_type}: )?%{GREEDYDATA:message}$
   ```

11. **SSHD_AUTH** — family `sshd`, roles `{"timestamp": "timestamp", "host": "host", "message": "result"}` — sshd authentication result

   ```text
   ^%{SYSLOGTIMESTAMP:timestamp} %{SYSLOGHOST:host} sshd\[%{POSINT:pid}\]: (?:%{WORD:result}) (?:password|publickey) for (?:invalid user )?%{USERNAME:user} from %{IP:src} port %{POSINT:port}(?: ssh2)?
   ```

12. **SUDO** — family `sudo`, roles `{"timestamp": "timestamp", "host": "host", "message": "command"}` — sudo command record

   ```text
   ^%{SYSLOGTIMESTAMP:timestamp} %{SYSLOGHOST:host} sudo(?:\[%{POSINT:pid}\])?:\s+%{DATA:user} : (?:%{DATA:error} ; )?TTY=%{TTY:tty} ; PWD=%{DATA:pwd} ; USER=%{USERNAME:runas} ; COMMAND=%{GREEDYDATA:command}$
   ```

13. **CRON** — family `cron`, roles `{"timestamp": "timestamp", "host": "host", "message": "command"}` — cron job start

   ```text
   ^%{SYSLOGTIMESTAMP:timestamp} %{SYSLOGHOST:host} (?:CRON|crond)\[%{POSINT:pid}\]: \(%{USERNAME:user}\) CMD \(%{GREEDYDATA:command}\)$
   ```

14. **DHCPD** — family `dhcpd`, roles `{"timestamp": "timestamp", "host": "host", "message": "dhcp_op"}` — ISC DHCP lease traffic

   ```text
   ^%{SYSLOGTIMESTAMP:timestamp} %{SYSLOGHOST:host} dhcpd(?:\[%{POSINT:pid}\])?: DHCP(?:%{WORD:dhcp_op}) (?:on|for|from) %{IP:ip}(?: \(%{DATA:client}\))? (?:from|to|via) %{GREEDYDATA:rest}$
   ```

15. **VMWARE_ESXI** — family `esxi`, roles `{"timestamp": "timestamp", "severity": "level", "service": "program", "message": "message"}` — VMware ESXi hostd / vmkernel

   ```text
   ^%{TIMESTAMP_ISO8601:timestamp} (?:%{LOGLEVEL:level}\s+)?%{NOTSPACE:program}\[%{DATA:context}\](?: \[%{DATA:opid}\])?:? %{GREEDYDATA:message}$
   ```

16. **RABBITMQ** — family `rabbitmq`, roles `{"timestamp": "timestamp", "severity": "level", "message": "message"}` — RabbitMQ 3.7+ log

   ```text
   ^%{TIMESTAMP_ISO8601:timestamp} \[%{LOGLEVEL:level}\] <%{DATA:pid}> %{GREEDYDATA:message}$
   ```

17. **DOCKER_DAEMON** — family `dockerd`, roles `{"timestamp": "timestamp", "severity": "level", "message": "message"}` — Docker daemon (logrus) line

   ```text
   ^time="%{TIMESTAMP_ISO8601:timestamp}" level=%{LOGLEVEL:level} msg="%{DATA:message}"(?: %{GREEDYDATA:rest})?$
   ```

18. **EXIM_MAIN** — family `exim`, roles `{"message": "message"}` — Exim mainlog

   ```text
   ^%{YEAR}-%{MONTHNUM}-%{MONTHDAY} %{TIME} (?:\[%{POSINT:pid}\] )?(?:%{NOTSPACE:msgid} )?(?:<=|=>|->|\*\*|==) %{GREEDYDATA:message}$
   ```

19. **DOVECOT** — family `dovecot`, roles `{"timestamp": "timestamp", "host": "host", "message": "message"}` — Dovecot IMAP / POP

   ```text
   ^%{SYSLOGTIMESTAMP:timestamp} %{SYSLOGHOST:host} dovecot: %{WORD:proc}(?:\(%{DATA:user}\))?(?:<%{DATA:session}>)?: %{GREEDYDATA:message}$
   ```

20. **F5_BIGIP** — family `bigip`, roles `{"timestamp": "timestamp", "host": "host", "severity": "level", "service": "program", "message": "message"}` — F5 BIG-IP ltm log

   ```text
   ^%{SYSLOGTIMESTAMP:timestamp} %{SYSLOGHOST:host} %{WORD:level} %{NOTSPACE:program}\[%{POSINT:pid}\]: %{NOTSPACE:msgid}: %{GREEDYDATA:message}$
   ```

21. **JUNIPER_JUNOS** — family `junos`, roles `{"timestamp": "timestamp", "host": "host", "service": "program", "message": "message"}` — Juniper Junos syslog

   ```text
   ^%{SYSLOGTIMESTAMP:timestamp} %{SYSLOGHOST:host} %{NOTSPACE:program}(?:\[%{POSINT:pid}\])?: %{WORD:msgid}: %{GREEDYDATA:message}$
   ```

22. **CHECKPOINT_LEA** — family `checkpoint`, roles `{"timestamp": "timestamp", "host": "host", "message": "message"}` — Check Point gateway syslog

   ```text
   ^%{SYSLOGTIMESTAMP:timestamp} %{SYSLOGHOST:host} (?:CP-GW|CheckPoint) - (?:%{DATA:product}) - (?:%{GREEDYDATA:message})$
   ```

23. **ZEEK_CONN** — family `zeek`, roles `{"timestamp": "timestamp", "message": "proto"}` — Zeek / Bro conn.log (TSV)

   ```text
   ^%{NUMBER:timestamp}\t%{NOTSPACE:uid}\t%{IP:src}\t%{POSINT:src_port}\t%{IP:dst}\t%{POSINT:dst_port}\t%{WORD:proto}\t%{GREEDYDATA:rest}$
   ```

24. **WINDOWS_TEXT_EVENT** — family `windows`, roles `{"timestamp": "timestamp", "severity": "level", "service": "source", "message": "message"}` — Windows event exported as text

   ```text
   ^%{TIMESTAMP_ISO8601:timestamp}\s+(?:%{LOGLEVEL:level})\s+%{POSINT:event_id}\s+%{DATA:source}\s+%{GREEDYDATA:message}$
   ```

25. **RUBY_LOGGER** — family `ruby`, roles `{"timestamp": "timestamp", "severity": "level", "service": "progname", "message": "message"}` — Ruby / Rails Logger

   ```text
   ^%{WORD:level_letter}, \[%{TIMESTAMP_ISO8601:timestamp} #%{POSINT:pid}\] +%{LOGLEVEL:level} -- (?:%{DATA:progname})?: %{GREEDYDATA:message}$
   ```

26. **NODE_WINSTON** — family `node`, roles `{"timestamp": "timestamp", "severity": "level", "service": "label", "message": "message"}` — Node.js winston / pino pretty

   ```text
   ^%{TIMESTAMP_ISO8601:timestamp} \[%{LOGLEVEL:level}\](?: \[%{DATA:label}\])?:? %{GREEDYDATA:message}$
   ```

27. **SAP_CCMS_ALERT** — family `sap-ccms`, roles `{"timestamp": "timestamp", "severity": "level", "service": "mte", "message": "message"}` — SAP CCMS alert export (MTE name with backslashes)

   ```text
   ^%{TIMESTAMP_ISO8601:timestamp}\s+%{LOGLEVEL:level}\s+(?P<mte>\\\\?[\w.-]+\\[^\s]+)\s+%{GREEDYDATA:message}$
   ```

`LinePattern` fields: `name`, `pattern`, `family` (default service), `roles` (role → captured field; roles used:
`timestamp`, `severity`, `host`, `service`, `message`, `status`), `description`, `builtin=True`, `enabled=True`,
`id=""`. All built-ins are enabled.

### 5.3 `expand(pattern, extra=None, depth=0)`

```python
_SUB = re.compile(r"%\{(\w+)(?::(\w+))?(?::(\w+))?\}")
def expand(pattern, extra=None, depth=0):
    if depth > 20: raise ValueError("pattern nests too deep")
    lib = {**BASE, **(extra or {})}
    def repl(m):
        name, fld = m.group(1), m.group(2)            # the optional third part (":type") is ignored
        if name not in lib: raise ValueError(f"unknown pattern %{{{name}}}")
        inner = expand(lib[name], extra, depth + 1)
        return f"(?P<{fld}>{inner})" if fld else f"(?:{inner})"
    out = _SUB.sub(repl, pattern)                     # one left-to-right pass over the original text
    return out.replace("(?>", "(?:")                  # atomic groups (YEAR) become plain groups at every level
```

So `YEAR` = `(?>\d\d){1,2}` is **not atomic** in Python either: after expansion it is `(?:\d\d){1,2}` with normal
backtracking. Every reference becomes a group: `%{X}` → `(?:...)`, `%{X:f}` → `(?P<f>...)`. Named groups appear in the
compiled regex in left-to-right order of their opening parenthesis — that is the order of `m.groupdict()` (and of Go
`SubexpNames()`), which fixes the attribute insertion order. Unnamed capturing groups inside BASE entries
(`URIPROTO`, `UNIXPATH`, `TTY`) are not in `groupdict()`; in Go skip `SubexpNames()` entries that are `""`.
The Go port of `expand` (`grok.go` in 5.7.4) uses the same regex (`\w` there only meets ASCII names) and
`ReplaceAllStringFunc`, which is also a single non-overlapping left-to-right pass.

### 5.4 `compile_pattern`, `_cache`, `guard_regex`, `_RISKY`, `UnsafePattern`

```python
def compile_pattern(pattern, extra=None, trusted=False):
    key = pattern + "\x00" + json.dumps(extra or {}, sort_keys=True)
    rx = _cache.get(key)
    if rx is None:
        expanded = expand(pattern, extra)
        if not trusted: guard_regex(expanded)     # MAX_PATTERN_LEN = 4000; _RISKY = r"\([^()]*[+*][^()]*\)\s*[*+]|\)\s*\+\s*[*+]"
        rx = re.compile(expanded)
        if len(_cache) > 512: _cache.clear()
        _cache[key] = rx
    return rx
```

The parse path always passes `trusted=True` (`detect` and `GrokParser`), so the length/risk guard never runs there.
(It matters that it does not: `TOMCAT_CATALINA` and `TOMCAT_JULI` would be refused by `_RISKY` because `JAVACLASS`
contains `(?:[a-zA-Z$_][a-zA-Z$_0-9]*\.)*`; no built-in exceeds 4000 characters — the longest expansion is
`IPTABLES`, 2910.) Go needs no guard for built-ins: RE2 has no catastrophic backtracking. `_cache` is a pure memo (cleared wholesale above 512 entries): no observable
effect. Go: compile the built-ins once at init.

### 5.5 `user_patterns()`, `all_patterns()`

`user_patterns()` reads `<WATCHOVER_HOME>/grok.json` (a JSON list of `LinePattern` dicts), memoised on the file's
mtime; missing file → `[]`; unreadable / invalid JSON → `[]`; entries without a truthy `pattern` are skipped.
`all_patterns()` = enabled user patterns first (they win ties), then enabled built-ins in `LINES` order.

The goldens are generated with a fresh empty `WATCHOVER_HOME`, so **no user patterns** take part. Go: implement
`all_patterns()` as the built-in list; if user patterns are ported later, translate them with `pyRe` and treat a Go
compile failure (lookarounds etc.) the way Python treats `re.error` in `detect` (skip the pattern) — parity for
arbitrary user regexes cannot be guaranteed and no golden covers it.

### 5.6 `detect(lines, min_share=0.6) -> (LinePattern | None, share)`

```python
sample = [ln for ln in lines[:120] if ln.strip()]           # first 120 elements of `lines`, blanks removed
if not sample: return None, 0.0
best, best_share = None, 0.0
for lp in all_patterns():
    try: rx = compile_pattern(lp.pattern, trusted=True)
    except (re.error, ValueError): continue
    hits = sum(1 for ln in sample if rx.search(ln))           # search, on the unstripped line
    share = hits / len(sample)
    if share > best_share: best, best_share = lp, share       # strict: earlier pattern keeps a tie
return (best, round(best_share, 2)) if best and best_share >= min_share else (None, 0.0)
```

* `best_share >= 0.6` is tested on the **unrounded** share; only the returned share is rounded (part 01, 1.6 `round`).
* Two different callers pass different `lines`: `detect_format` passes its own list (first 200 physical lines with
  blanks already removed → `detect` sees the first 120 **non-blank** lines), while `GrokParser.records` passes
  `text.splitlines()` (→ the non-blank lines among the first 120 **physical** lines). With blank lines in the head of
  a file the two samples differ, and the parser can pick another pattern than the one that made the format `grok`,
  or none (then the file yields **no rows**). No golden has this situation.
* Tie golden: `rabbitmq.log` / `mut__rabbitmq.log`: `RABBITMQ` and `NODE_WINSTON` both match 100 % → `RABBITMQ`
  (earlier in `LINES`).

### 5.7 RE2 port of the grok library

#### 5.7.1 What is not RE2 in the expanded built-ins

| BASE entry | construct | used by detected / any LINES? |
|---|---|---|
| `BASE10NUM` (via `NUMBER`) | `(?<![0-9.+-])` lookbehind | yes: COMMON/COMBINED, HTTPD, SQUID, IPTABLES, NAGIOS, ZEEK |
| `BASE16NUM` | `(?<![0-9A-Fa-f])` lookbehind | not referenced by any LINES |
| `IPV4` (via `IP`, `IPORHOST`, `SYSLOGHOST`) | `(?<![0-9])` ... `(?![0-9])` | yes, many |
| `TIME` | `(?!<[0-9])` (a typo of a lookbehind: it is a negative **lookahead** for the two characters `<digit>`) and `(?![0-9])` | yes: every SYSLOGTIMESTAMP, BIND9, HTTPDATE, EXIM |
| `WORD` | Unicode `\b\w+\b` | yes |
| `HOSTNAME` | Unicode `\b` at both ends | yes |
| `MONTH` | Unicode `\b` at both ends; literal `ä` | yes |
| `POSINT`, `NONNEGINT` | Unicode `\b` at both ends | yes |
| `YEAR` | `(?>...)` atomic — already turned into `(?:` by `expand` | yes |
| `NOTSPACE`, `SPACE`, `QUANTITY`, `UNIXPATH`, `TTY`, `SAP_CCMS_ALERT` mte | Unicode `\S`, `\s`, `\w` (and `\d` in `YEAR`) | translated by `pyRe` |

Everything else (`(?P<name>...)`, `\x21`-style escapes, `[...\-\[\]...]` classes, escaped quotes `\"`, the `+-=`
range in `EMAILLOCALPART`, `{0,62}` counters, the nested `([A-Za-z0-9+\-.]+)+` of `URIPROTO`) compiles unchanged in
Go and matches the same way.

#### 5.7.2 Overrides that are exact for every built-in context (proofs)

Applied by replacing the BASE entry before expansion (Python syntax; `pyRe` runs on the final expansion):

```text
BASE10NUM      (?:[+-]?(?:(?:[0-9]+(?:\.[0-9]+)?)|(?:\.[0-9]+)))
BASE16NUM      (?:[+-]?(?:0x)?(?:[0-9A-Fa-f]+))
WORD           \w+
IPV4           (?:(?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5]))
HOSTNAME       (?:[0-9A-Za-z][0-9A-Za-z-]{0,62})(?:\.(?:[0-9A-Za-z][0-9A-Za-z-]{0,62}))*\.?
MONTH          (?:[Jj]an(?:uary|uar)?|[Ff]eb(?:ruary|ruar)?|[Mm](?:a|ä)?r(?:ch|z)?|[Aa]pr(?:il)?|[Mm]a(?:y|i)?|[Jj]un(?:e|i)?|[Jj]ul(?:y|i)?|[Aa]ug(?:ust)?|[Ss]ep(?:tember)?|[Oo](?:c|k)?t(?:ober)?|[Nn]ov(?:ember)?|[Dd]e(?:c|z)(?:ember)?)
TIME           %{HOUR}:%{MINUTE}(?::%{SECOND})
SYSLOGFACILITY <(?P<facility>[0-9]+)[^\w\n](?P<priority>[0-9]+)>
```

Why each is exact (all LINES start with `^`, so only start position 0 is ever tried):

* **NUMBER lookbehind**: in every LINES context a NUMBER is preceded by `^`, `/`, a space, `[`, `[` + whitespace, or
  `\s+` (which must end on whitespace because a NUMBER cannot start with whitespace). None of these is in
  `[0-9.+-]`, so the lookbehind is always true. (BASE16NUM is unused; same treatment.)
* **IPV4 lookarounds**: every IP is preceded by a non-digit literal (space, `=`, tab, `[client `, `^`) and followed by a
  literal that cannot be a digit (space, `#`, tab, `:`, `]`, `\s`). Hence any octet split that the lookahead would
  reject is rejected by the following literal instead; the set of successful paths and their preference order are
  unchanged, so the spans are identical (also when `IPORHOST` falls back to `HOSTNAME`, e.g. `10.0.0.1234`).
* **TIME**: `(?!<[0-9])` can never fail (the next character is the first digit of `HOUR`, not `<`). The trailing
  `(?![0-9])` is redundant because every TIME is followed by a space or by `(?:\.%{INT:ms})? ` (BIND9) — never by a
  digit. BIND9 consequence (same in both): `SECOND` already eats `.003`, so the `ms` group never participates.
* **WORD** `\b\w+\b`: `\w+` is greedy and a shorter run would leave the trailing `\b` between two word characters, so
  the run is always maximal; in every LINES context except DHCPD the character before is a non-word literal (`"`, `[`,
  space, `\s`, `^`) and the one after is a non-word literal (space, `:`, `(`, `<`, `,`, tab). So `\w+` (Unicode) is
  exact. DHCPD is handled in 5.7.3.
* **HOSTNAME**: the leading `\b` always holds (preceded by `^` or a space/`[client `, first char alphanumeric). The
  trailing `(?:\.?|\b)` is equivalent to `\.?`: `\.?` (which may match empty) is tried first, and the `\b`
  alternative only lands at the same position as the empty `\.?` — so it can never produce a new match.
* **MONTH**: always delimited by `^`, `/`, `-`, or space on the left and by space, `/`, `-` on the right in LINES, so
  both `\b` are implied by the neighbours.
* **SYSLOGFACILITY** `<%{NONNEGINT:facility}.%{NONNEGINT:priority}>`: the digit runs are maximal (shorter runs put
  `\b` between digits); the `.` character X must then satisfy "non-word" (trailing `\b` of `facility`); the leading
  `\b` of `priority` and its trailing `\b` before `>` then hold automatically. So the whole thing is
  `<digits X digits>` with X a non-Unicode-word, non-`\n` character. With RE2's ASCII `\b` an X like `é` would be
  accepted (verified: `Sep 22 09:14:02 <1é2> host prog: m` matches SYSLOGLINE in Go without this override, not in
  Python).
* **POSINT / NONNEGINT** keep RE2 `\b` everywhere else: their remaining uses are bracketed by ASCII literals
  (`[pid]`, `#port`, `:port]`, `\tport\t`, `\s+event_id\s+`, `#pid]`) so the ASCII boundary equals the Unicode
  one, except SSHD's `port` (5.7.3).

#### 5.7.3 Line-level fixes

* **SSHD_AUTH**: the pattern ends with `port %{POSINT:port}(?: ssh2)?` (no `$`), so the character after the port
  digits is free. Python requires it to be a non-word character (or end); RE2's ASCII `\b` accepts `22é` / `22٣`.
  Replace that text with `port (?P<port>[1-9][0-9]*)(?:[^\w]|$)` (the `(?: ssh2)?` contributes nothing to a
  successful match and has no group). The capture stays the digits only.
* **DHCPD**: `DHCP(?:%{WORD:dhcp_op})` puts WORD's leading `\b` between `P` and a word character, which is never a
  boundary — the pattern **cannot match any line in Python** (`DHCPACK on ...` included). With the 5.7.2 WORD it would
  match in Go. Drop DHCPD from the Go list (with 0 hits it can never be selected, so detection is unchanged).
* No other LINE needs a change. `SAP_CCMS_ALERT`'s inline `(?P<mte>\\\\?[\w.-]+\\[^\s]+)` is translated by `pyRe`
  (`[^\s]` → `[^` + pyWS + `]`).

#### 5.7.4 Go reference implementation (verified: 26 compiled patterns × 19 759 lines, 0 differences; DHCPD never matches in Python on any line)

`grok_src.json` here is just `{"base": BASE, "lines": [[name, pattern], ...]}` dumped from Python for the test; a
port would embed the tables of 5.1/5.2.

```go
// grok.go
package main

import (
	"encoding/json"
	"os"
	"regexp"
	"strings"
)

type grokSrc struct {
	Base  map[string]string `json:"base"`
	Lines [][2]string       `json:"lines"`
}

// Go overrides of the Python BASE (still Python syntax: pyRe translates \d \w \s \S afterwards).
var goBase = map[string]string{
	"BASE10NUM":      `(?:[+-]?(?:(?:[0-9]+(?:\.[0-9]+)?)|(?:\.[0-9]+)))`,
	"BASE16NUM":      `(?:[+-]?(?:0x)?(?:[0-9A-Fa-f]+))`,
	"WORD":           `\w+`,
	"IPV4":           `(?:(?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5]))`,
	"HOSTNAME":       `(?:[0-9A-Za-z][0-9A-Za-z-]{0,62})(?:\.(?:[0-9A-Za-z][0-9A-Za-z-]{0,62}))*\.?`,
	"MONTH":          `(?:[Jj]an(?:uary|uar)?|[Ff]eb(?:ruary|ruar)?|[Mm](?:a|ä)?r(?:ch|z)?|[Aa]pr(?:il)?|[Mm]a(?:y|i)?|[Jj]un(?:e|i)?|[Jj]ul(?:y|i)?|[Aa]ug(?:ust)?|[Ss]ep(?:tember)?|[Oo](?:c|k)?t(?:ober)?|[Nn]ov(?:ember)?|[Dd]e(?:c|z)(?:ember)?)`,
	"TIME":           `%{HOUR}:%{MINUTE}(?::%{SECOND})`,
	"SYSLOGFACILITY": `<(?P<facility>[0-9]+)[^\w\n](?P<priority>[0-9]+)>`,
}

// Line-level overrides.
var goLineFix = map[string][2]string{
	"SSHD_AUTH": {`port %{POSINT:port}(?: ssh2)?`, `port (?P<port>[1-9][0-9]*)(?:[^\w]|$)`},
}
var goSkip = map[string]bool{"DHCPD": true} // never matches in Python (\b between "DHCP" and a word char)

var subRE = regexp.MustCompile(`%\{(\w+)(?::(\w+))?(?::(\w+))?\}`)

func expand(p string, lib map[string]string, depth int) string {
	if depth > 20 {
		panic("pattern nests too deep")
	}
	out := subRE.ReplaceAllStringFunc(p, func(s string) string {
		m := subRE.FindStringSubmatch(s)
		body, ok := lib[m[1]]
		if !ok {
			panic("unknown pattern " + m[1])
		}
		inner := expand(body, lib, depth+1)
		if m[2] != "" {
			return "(?P<" + m[2] + ">" + inner + ")"
		}
		return "(?:" + inner + ")"
	})
	return strings.ReplaceAll(out, "(?>", "(?:")
}

type goLine struct {
	name string
	rx   *regexp.Regexp
	src  string
}

func loadGrok(path string) []goLine {
	b, _ := os.ReadFile(path)
	var g grokSrc
	if err := json.Unmarshal(b, &g); err != nil {
		panic(err)
	}
	lib := map[string]string{}
	for k, v := range g.Base {
		lib[k] = v
	}
	for k, v := range goBase {
		lib[k] = v
	}
	var out []goLine
	for _, l := range g.Lines {
		name, pat := l[0], l[1]
		if goSkip[name] {
			continue
		}
		if f, ok := goLineFix[name]; ok {
			if !strings.Contains(pat, f[0]) {
				panic("fix not applicable " + name)
			}
			pat = strings.Replace(pat, f[0], f[1], 1)
		}
		src := pyRe(expand(pat, lib, 0))
		out = append(out, goLine{name, regexp.MustCompile(src), src})
	}
	return out
}
```

#### 5.7.5 Go-expanded sources of the six patterns the goldens detect

These are the exact strings the code above compiles (useful as test fixtures). `\x{...}` classes are the Python
whitespace / word sets.

**BIND9_QUERY** (1740 bytes)

```text
^(?P<day>(?:(?:0[1-9])|(?:[12][0-9])|(?:3[01])|[1-9]))-(?P<month>(?:[Jj]an(?:uary|uar)?|[Ff]eb(?:ruary|ruar)?|[Mm](?:a|ä)?r(?:ch|z)?|[Aa]pr(?:il)?|[Mm]a(?:y|i)?|[Jj]un(?:e|i)?|[Jj]ul(?:y|i)?|[Aa]ug(?:ust)?|[Ss]ep(?:tember)?|[Oo](?:c|k)?t(?:ober)?|[Nn]ov(?:ember)?|[Dd]e(?:c|z)(?:ember)?))-(?P<year>(?:\p{Nd}\p{Nd}){1,2}) (?P<time>(?:(?:2[0123]|[01]?[0-9])):(?:(?:[0-5][0-9]))(?::(?:(?:(?:[0-5]?[0-9]|60)(?:[:.,][0-9]+)?))))(?:\.(?P<ms>(?:[+-]?(?:[0-9]+))))? (?:queries: )?(?:(?P<level>(?:[Aa]lert|ALERT|[Tt]race|TRACE|[Dd]ebug|DEBUG|[Nn]otice|NOTICE|[Ii]nfo?(?:rmation)?|INFO?(?:RMATION)?|[Ww]arn?(?:ing)?|WARN?(?:ING)?|[Ee]rr?(?:or)?|ERR?(?:OR)?|[Cc]rit?(?:ical)?|CRIT?(?:ICAL)?|[Ff]atal|FATAL|[Ss]evere|SEVERE|EMERG(?:ENCY)?|[Ee]merg(?:ency)?)): )?client(?: @0x[0-9a-f]+)? (?P<clientip>(?:(?:(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,7}:|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}|(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}|:(?:(?::[0-9A-Fa-f]{1,4}){1,7}|:)|::(?:ffff(?::0{1,4})?:)?(?:(?:25[0-5]|(?:2[0-4]|1?[0-9])?[0-9])\.){3}(?:25[0-5]|(?:2[0-4]|1?[0-9])?[0-9])))|(?:(?:(?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])))))#(?P<clientport>\b(?:[1-9][0-9]*)\b)(?: \((?P<qname>.*?)\))?: query: (?P<query>[^\t\n\x0b\x0c\r\x1c-\x1f \x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+) (?P<qclass>[\p{L}\p{N}_]+) (?P<qtype>[\p{L}\p{N}_]+) (?P<flags>.*)$
```

**DOCKER_DAEMON** (604 bytes)

```text
^time="(?P<timestamp>(?:(?:\p{Nd}\p{Nd}){1,2})-(?:(?:0?[1-9]|1[0-2]))-(?:(?:(?:0[1-9])|(?:[12][0-9])|(?:3[01])|[1-9]))[T ](?:(?:2[0123]|[01]?[0-9])):?(?:(?:[0-5][0-9]))(?::?(?:(?:(?:[0-5]?[0-9]|60)(?:[:.,][0-9]+)?)))?(?:(?:Z|[+-](?:(?:2[0123]|[01]?[0-9]))(?::?(?:(?:[0-5][0-9])))))?)" level=(?P<level>(?:[Aa]lert|ALERT|[Tt]race|TRACE|[Dd]ebug|DEBUG|[Nn]otice|NOTICE|[Ii]nfo?(?:rmation)?|INFO?(?:RMATION)?|[Ww]arn?(?:ing)?|WARN?(?:ING)?|[Ee]rr?(?:or)?|ERR?(?:OR)?|[Cc]rit?(?:ical)?|CRIT?(?:ICAL)?|[Ff]atal|FATAL|[Ss]evere|SEVERE|EMERG(?:ENCY)?|[Ee]merg(?:ency)?)) msg="(?P<message>.*?)"(?: (?P<rest>.*))?$
```

**NAGIOS_LOG** (139 bytes)

```text
^(?:\[(?P<nagios_epoch>(?:(?:(?:[+-]?(?:(?:[0-9]+(?:\.[0-9]+)?)|(?:\.[0-9]+))))))\]) (?:(?P<nagios_type>[\p{L}\p{N}_]+): )?(?P<message>.*)$
```

**RABBITMQ** (585 bytes)

```text
^(?P<timestamp>(?:(?:\p{Nd}\p{Nd}){1,2})-(?:(?:0?[1-9]|1[0-2]))-(?:(?:(?:0[1-9])|(?:[12][0-9])|(?:3[01])|[1-9]))[T ](?:(?:2[0123]|[01]?[0-9])):?(?:(?:[0-5][0-9]))(?::?(?:(?:(?:[0-5]?[0-9]|60)(?:[:.,][0-9]+)?)))?(?:(?:Z|[+-](?:(?:2[0123]|[01]?[0-9]))(?::?(?:(?:[0-5][0-9])))))?) \[(?P<level>(?:[Aa]lert|ALERT|[Tt]race|TRACE|[Dd]ebug|DEBUG|[Nn]otice|NOTICE|[Ii]nfo?(?:rmation)?|INFO?(?:RMATION)?|[Ww]arn?(?:ing)?|WARN?(?:ING)?|[Ee]rr?(?:or)?|ERR?(?:OR)?|[Cc]rit?(?:ical)?|CRIT?(?:ICAL)?|[Ff]atal|FATAL|[Ss]evere|SEVERE|EMERG(?:ENCY)?|[Ee]merg(?:ency)?))\] <(?P<pid>.*?)> (?P<message>.*)$
```

**SQUID_ACCESS** (2474 bytes)

```text
^(?P<timestamp>(?:(?:(?:[+-]?(?:(?:[0-9]+(?:\.[0-9]+)?)|(?:\.[0-9]+))))))[\t\n\x0b\x0c\r\x1c-\x1f \x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+(?P<duration>(?:(?:(?:[+-]?(?:(?:[0-9]+(?:\.[0-9]+)?)|(?:\.[0-9]+))))))[\t\n\x0b\x0c\r\x1c-\x1f \x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+(?P<clientip>(?:(?:(?:(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,7}:|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}|(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}|:(?:(?::[0-9A-Fa-f]{1,4}){1,7}|:)|::(?:ffff(?::0{1,4})?:)?(?:(?:25[0-5]|(?:2[0-4]|1?[0-9])?[0-9])\.){3}(?:25[0-5]|(?:2[0-4]|1?[0-9])?[0-9])))|(?:(?:(?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])[.](?:[0-1]?[0-9]{1,2}|2[0-4][0-9]|25[0-5])))))[\t\n\x0b\x0c\r\x1c-\x1f \x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+(?P<squid_status>(?:TCP|UDP|NONE)_[A-Z_]+)/(?P<response>(?:(?:(?:[+-]?(?:(?:[0-9]+(?:\.[0-9]+)?)|(?:\.[0-9]+))))))[\t\n\x0b\x0c\r\x1c-\x1f \x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+(?P<bytes>(?:(?:(?:[+-]?(?:(?:[0-9]+(?:\.[0-9]+)?)|(?:\.[0-9]+))))))[\t\n\x0b\x0c\r\x1c-\x1f \x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+(?P<verb>[\p{L}\p{N}_]+)[\t\n\x0b\x0c\r\x1c-\x1f \x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+(?P<request>[^\t\n\x0b\x0c\r\x1c-\x1f \x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+)[\t\n\x0b\x0c\r\x1c-\x1f \x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+(?P<user>[^\t\n\x0b\x0c\r\x1c-\x1f \x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+)[\t\n\x0b\x0c\r\x1c-\x1f \x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+(?P<hierarchy>[^\t\n\x0b\x0c\r\x1c-\x1f \x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+)[\t\n\x0b\x0c\r\x1c-\x1f \x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+(?P<content_type>[^\t\n\x0b\x0c\r\x1c-\x1f \x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+)
```

**VMWARE_ESXI** (838 bytes)

```text
^(?P<timestamp>(?:(?:\p{Nd}\p{Nd}){1,2})-(?:(?:0?[1-9]|1[0-2]))-(?:(?:(?:0[1-9])|(?:[12][0-9])|(?:3[01])|[1-9]))[T ](?:(?:2[0123]|[01]?[0-9])):?(?:(?:[0-5][0-9]))(?::?(?:(?:(?:[0-5]?[0-9]|60)(?:[:.,][0-9]+)?)))?(?:(?:Z|[+-](?:(?:2[0123]|[01]?[0-9]))(?::?(?:(?:[0-5][0-9])))))?) (?:(?P<level>(?:[Aa]lert|ALERT|[Tt]race|TRACE|[Dd]ebug|DEBUG|[Nn]otice|NOTICE|[Ii]nfo?(?:rmation)?|INFO?(?:RMATION)?|[Ww]arn?(?:ing)?|WARN?(?:ING)?|[Ee]rr?(?:or)?|ERR?(?:OR)?|[Cc]rit?(?:ical)?|CRIT?(?:ICAL)?|[Ff]atal|FATAL|[Ss]evere|SEVERE|EMERG(?:ENCY)?|[Ee]merg(?:ency)?))[\t\n\x0b\x0c\r\x1c-\x1f \x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+)?(?P<program>[^\t\n\x0b\x0c\r\x1c-\x1f \x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+)\[(?P<context>.*?)\](?: \[(?P<opid>.*?)\])?:? (?P<message>.*)$
```

---

## 6. `GrokParser.records(text)` (`name = "grok"`, `kind = "log"`, `line_oriented = True`)

```python
STATUS_SEV = lambda st: "ERROR" if st.startswith("5") else "WARN" if st.startswith("4") else "INFO"

lines = text.splitlines()
lp, _share = grok.detect(lines)                 # re-detected on this text (also for every live batch)
if lp is None: return                           # no rows at all
rx = grok.compile_pattern(lp.pattern, trusted=True)
roles = lp.roles or {}
prev = None
for i, ln in enumerate(lines, 1):
    if not ln.strip(): continue
    m = rx.search(ln)                            # on the unstripped line
    if not m:
        if prev is not None and (CONT_RE.match(ln) or ln[:1].isspace()):     # continuation of the event above
            prev.message = (prev.get("message", "") + "\n" + ln.strip()).strip()
            prev._lines = prev.get("_lines", 1) + 1
            continue
        rec = {"message": ln.strip(), "_unmatched": True}
    else:
        caught = {k: v for k, v in m.groupdict().items() if v is not None}   # "" kept
        rec = {}
        for role, fld in roles.items():          # roles dict order (as written in LINES)
            if fld in caught: rec[role] = caught[fld]
        if "status" in rec and "severity" not in rec:
            rec["severity"] = STATUS_SEV(str(rec.pop("status")))              # status removed, severity appended
        elif "status" in rec:
            rec.pop("status")
        rec.setdefault("message", caught.get("message") or ln.strip())
        for k, v in caught.items():              # group order
            if k not in rec and k not in roles.values():
                rec[k] = v
        if "pid" in caught and "pid" not in rec:
            rec["pid"] = caught["pid"]           # only reachable when "pid" is a role *value*: never for built-ins
    if lp.family and not rec.get("service"):
        rec["service"] = lp.family               # also replaces a captured service of ""
    rec["_grok"] = lp.name
    if prev is not None: yield prev
    prev = (i, rec)
if prev is not None: yield prev
```

Details:

* Captured values are always strings; groups that did not participate are dropped (`BIND9_QUERY` `ms`, `NAGIOS_LOG`
  `nagios_type`, optional `DOCKER_DAEMON` `rest`), groups that matched `""` are kept (`DOCKER_DAEMON` line ending in
  `" ` gives `rest = ""`; `records_to_observations` then drops `""` from attributes). In Go, `FindStringSubmatchIndex`
  index `-1` = did not participate, `a == b` = empty match.
* A role field captured as `""` still sets the role (`message = ""` would not be replaced by `setdefault`).
* `STATUS_SEV` tests the first character of the status string only (`"503"` → `ERROR`, `"404"` → `WARN`, anything
  else incl. `""` → `INFO`). Only `SQUID_ACCESS`, `COMMONAPACHELOG`, `COMBINEDAPACHELOG` have a `status` role.
* `k not in rec` compares with role **names** already in the record; `k not in roles.values()` drops every field that
  some role points to, even if that role was not filled.
* Non-matching lines: folded only when they look like a continuation (`CONT_RE` on the raw line or a leading Python
  whitespace character — the latter is already covered by `CONT_RE`'s `\s`); otherwise a record
  `{"message": stripped line, "_unmatched": True, "service": family, "_grok": name}` with no timestamp. `_partial`
  is never set by this parser.
* Record key order: role keys in `roles` order (with `status` replaced by a trailing `severity`), `message` if not a
  role, extra groups in group order, `service` (family), `_grok`; `_lines` after a fold. Golden report keys, e.g.
  `squid_access.log`: `timestamp, message, severity, duration, clientip, squid_status, bytes, verb, user, hierarchy,
  content_type, service, _grok`; `bind9_query.log`: `severity, message, day, month, year, time, clientip, clientport,
  qname, qclass, qtype, flags, service, _grok` (BIND9 has **no** timestamp role: `auto_map` (part 02) then picks the
  `time` field, which holds only `09:14:02.003` → canonical `ts` `2026-01-01T09:14:02.003000+00:00`, January 1st of
  the current year).
* `_grok` (pattern name) is always an attribute in the goldens; so are all extra groups.

Per detected pattern (all goldens, whole = live):

| pattern | inputs | roles → record | extra attributes seen |
|---|---|---|---|
| `BIND9_QUERY` (family `bind`) | `bind9_query.log`, `mut__bind9_query.log` | severity←level, message←query | day, month, year, time, clientip, clientport, qname, qclass, qtype, flags |
| `DOCKER_DAEMON` (`dockerd`) | `dockerd.log`, `mut__dockerd.log` | timestamp, severity←level, message | rest (when non-empty) |
| `NAGIOS_LOG` (`nagios`) | `nagios.log`, `mut__nagios.log` | timestamp←nagios_epoch, message | — (`SERVICE ALERT:` is two words, so `nagios_type` never participates) |
| `RABBITMQ` (`rabbitmq`) | `rabbitmq.log`, `mut__rabbitmq.log` | timestamp, severity←level, message | pid (`0.812.0`) |
| `SQUID_ACCESS` (`squid`) | `squid_access.log`, `mut__squid_access.log` | timestamp, message←request, status←response → severity | duration, clientip, squid_status, bytes, verb, user, hierarchy, content_type |
| `VMWARE_ESXI` (`esxi`) | `vmware_esxi.log`, `mut__vmware_esxi.log` | timestamp, severity←level, service←program, message | context, opid |

---

## 7. Timestamp strings handed to `parse_timestamp`

This slice never parses dates; it passes strings to `normalize.parse_timestamp` (part 02, section 3, which defines the
algorithm, the dateutil fallback, the current-year default and UTC for naive values). The shapes the goldens feed it
from this slice, with the canonical `ts` they produce (probed):

| shape (digits → 9) | example | source | canonical `ts` |
|---|---|---|---|
| `9999-99-99 99:99:99.999 +99:99` | `2026-09-22 09:14:02.003 +03:00` | dotnet (TS A1) | `2026-09-22T09:14:02.003000+03:00` |
| `9999-99-99T99:99:99.999Z` | `2026-10-14T09:30:00.596Z` | `mut__*` (A1) | `2026-10-14T09:30:00.596000+00:00` |
| `9999-99-99 99:99:99` | `2026-09-22 09:14:02` | dynamics, plain (A1) | `2026-09-22T09:14:02+00:00` |
| `9999/99/99 99:99:99` | `2026/09/22 09:14:02` | golang, nginx (A2) | `2026-09-22T09:14:02+00:00` |
| `9999-99-99T99:99:99+99:99` | `2026-09-22T09:16:00+03:00` | golang `time="..."` (KV_TS) | `2026-09-22T09:16:00+03:00` |
| `9999-99-99 99:99:99.999` | `2026-09-22 09:15:03.500` | java (A1) | `2026-09-22T09:15:03.500000+00:00` |
| `9999-99-99 99:99:99,999` | `2026-09-22 09:14:02,003` | kafka, python (A1 with `[ ]`) | `2026-09-22T09:14:02.003000+00:00` |
| `9999-99-99T99:99:99.999999Z` | `2026-09-22T06:14:02.003412Z` | CRI, mysql | `2026-09-22T06:14:02.003412+00:00` |
| `9999-99-99T99:99:99.999999+99:99` | `2026-09-22T09:14:02.112000+03:00` | oracle alert | `2026-09-22T09:14:02.112000+03:00` |
| `99-SEP-9999 99:99:99` | `22-SEP-2026 09:14:02` | EBS (A4, EMBEDDED_TS) | `2026-09-22T09:14:02+00:00` |
| `9999-99-99 99:99:99.999 +99` | `2026-09-22 09:14:31.001 +03` | postgres (A1) | `2026-09-22T09:14:31.001000+03:00` |
| `99 Sep 9999 99:99:99.999` | `22 Sep 2026 09:14:02.003` | redis (A6) | `2026-09-22T09:14:02.003000+00:00` |
| `9999-99-99 99:99:99.99` | `2026-09-22 09:14:02.11` | sqlserver (A1) | `2026-09-22T09:14:02.110000+00:00` |
| `99.99.9999 99:99:99` | `22.09.2026 09:14:02` | timestamp_variants (A3) | `2026-09-22T09:14:02+00:00` |
| `99/99/9999 99:99:99` | `22/09/2026 09:14:05` | timestamp_variants (A3) | `2026-09-22T09:14:05+00:00` (day first, 22 > 12) |
| `99/99/9999 99:99:99 AM` | `09/22/2026 09:15:03 AM` | timestamp_variants (A3) | `2026-09-22T09:15:03+00:00` (month first) |
| `9999-99-99 99:99:99.999999` | `2026-09-22 09:15:10.000123` | timestamp_variants (A1) | `2026-09-22T09:15:10.000123+00:00` |
| `9999999999` | `1790068560` | timestamp_variants (E10) | `2026-09-22T09:16:00+00:00` |
| `Tue, 99 Sep 9999 99:99:99 +9999` | `Tue, 22 Sep 2026 09:16:44 +0300` | timestamp_variants (A9) | `2026-09-22T09:16:44+03:00` |
| `99999999T999999Z` | `20260922T091700Z` | timestamp_variants (A11) | `2026-09-22T09:17:00+00:00` |
| `99-Sep-9999 99:99:99.999` | `22-Sep-2026 09:14:02.003` | tomcat (A5) | `2026-09-22T09:14:02.003000+00:00` |
| `99:99:99.999` | `09:14:02.003` | grok BIND9 `time` | `2026-01-01T09:14:02.003000+00:00` |
| `9999-99-99T99:99:99.999+99:99` | `2026-09-22T09:14:02.003+03:00` | grok DOCKER_DAEMON | `2026-09-22T09:14:02.003000+03:00` |
| `9999999999` | `1790068442` | grok NAGIOS_LOG | `2026-09-22T09:14:02+00:00` |
| `9999-99-99 99:99:99.999` | `2026-09-22 09:14:02.003` | grok RABBITMQ | `2026-09-22T09:14:02.003000+00:00` |
| `9999999999.999` | `1790068442.003` | grok SQUID_ACCESS | `2026-09-22T09:14:02.003000+00:00` |
| `9999-99-99T99:99:99.999Z` | `2026-09-22T06:14:02.003Z` | grok VMWARE_ESXI, DOCKER, RABBITMQ (`mut__`) | `...+00:00` |

Records without a timestamp (folding off, first-line continuation, grok `_unmatched`) get `_no_ts` and then the
neighbour fill of part 01, 10.2.

---

## 8. Whole run vs live run (how the goldens differ)

`scripts/parse_golden.py` writes two goldens per input (part 01, 14):

* **whole**: the full file, no hint → `detect_format`, `detect_family` from the file's first 300 lines.
* **live**: 40-physical-line batches; the first batch is detected normally, later batches get
  `hint = {"format", "confidence", "family", "n"}` from the first batch's report. Effects in this slice:
  * every batch is an independent `records()` call: `fold` is recomputed on the batch, `prev` starts as `None`, so a
    stack-trace continuation that is the first line of a batch becomes its own record (no timestamp) and a record never
    absorbs lines across a batch boundary; line numbers restart at 1 per batch;
  * the family of later batches is the first batch's family, **including `""`** (no detection at all then);
  * grok re-runs `detect` on every batch (no pattern hint exists).
  Measured row counts whole → live: `mut__python_traceback.log` 76 → 87, `mut__java_stacktrace.log` 91 → 94,
  `mut__oracle_alert.log` 95 → 99, `mut__dotnet_serilog.log` 137 → 138, `mut__tomcat_juli.log` 164 → 165,
  `mut__oracle_ebs_concurrent.log` 158 → 118 (smaller batches pass the 30 % timestamp test, so `fold` turns on and
  more lines fold). `mut__dynamics_ax_aos.log`: whole `syslog`, live `text` / `dynamics-ax`. `baseline.json` and
  `storm__alarms.json`: whole `json`, live batches `text` 0.5 (a JSON document cut into pieces) with `fold` off and
  `CONT_RE` folding of the indented lines. All other text/grok inputs: identical row counts.

---

## 9. Global and stateful behaviour — what Go must do

| state | Python | effect on output | Go |
|---|---|---|---|
| `FAMILY_HINT` contextvar | set by `_parse_one` from `hint["family"]` | family / default service of later live batches | explicit `*string` parameter |
| `FAMILY_SEEN` contextvar | written by `detect_family`, read by `_parse_one` | report `family` | return value |
| `grok._cache` | compile memo, cleared above 512 entries | none | compile built-ins once |
| `grok._user_cache` + `<home>/grok.json` | user line packs, mtime-memoised | would change detection; empty in goldens | built-ins only (see 5.5) |
| `normalize` caches (`parse_timestamp` memo, current year) | part 02 | year-less `time` field of BIND9 → current year | inject the clock (pin 2026 for the goldens) |
| profiles (`grok` format takes part in `profiles.lookup/remember`) | part 01 | report `profile` only in the goldens | part 01 |
| scenario constants | none used by these modules | — | — |

`parse_head`, `CONT_RE`, the family regexes and `GrokParser` hold no other state; every call is a pure function of
its input (plus the hint).

---

## 10. Golden coverage: which inputs exercise which branch

Measured by the instrumented run (whole + live). File names without `mut__` imply the `mut__` variant as well unless
noted.

| branch | golden inputs |
|---|---|
| CRI prefix (`F` and `P`) | `kubernetes_cri.log`, `mut__kubernetes_cri.log` |
| Redis prefix + TS A6 + marks `#`, `*` | `redis.log`, `mut__redis.log` |
| TS A1 ISO (space/`T`, `.`/`,` fraction, `Z`, ` +03`, ` +03:00`, `+03:00`) | most text inputs |
| TS with `[`...`]` | `kafka_log4j.log` (A1), `oracle_ebs_fnd_debug.log` (A4, `:003]` left in rest) |
| TS A2 `yyyy/mm/dd` | `golang_log.log`, `nginx_error.log` |
| TS A3 (`dd.mm.yyyy`, `dd/mm/yyyy`, `mm/dd/yyyy AM`) | `timestamp_variants.log` |
| TS A4 `22-SEP-2026` | `oracle_ebs_concurrent.log`, `oracle_ebs_fnd_debug.log` |
| TS A5 `22-Sep-2026 ... .003` | `tomcat_juli.log` |
| TS A9 RFC 2822, A11 basic ISO, E10 epoch | `timestamp_variants.log` |
| `KV_TS_RE` | `golang_log.log` |
| `THREAD_ID_RE` | `mysql_error.log` |
| `PG_RE` LOG / ERROR / FATAL / DETAIL / STATEMENT, `user@db` | `postgres.log` only (the `mut__` lines lose the PG head, 1.1) |
| `LEVEL_RE` values: INFO, ERROR, WARN, WARNING, CRITICAL, SEVERE, INF, ERR, WRN, FTL, Note, System, Warning, error, warn, notice, crit | dotnet, mysql, nginx, python, redis, tomcat, plain, timestamp_variants, java, kafka, CRI, dynamics_365, demo__checkout |
| level loop: code / service / thread / break | code: mysql; service: java, tomcat, mysql (2nd iteration); thread: java (2nd), kafka (1st) |
| `SEV_NUM_RE` → ERROR | `sqlserver_errorlog.log` |
| `EMBEDDED_TS_RE` (`**Starts**`, `**Ends**`, `Current system time is`) | `oracle_ebs_concurrent.log`, `mut__oracle_ebs_concurrent.log` |
| service: DYNAMICS_RE | `dynamics_365_batch.log` (`Microsoft.Dynamics...`), `dynamics_ax_aos.log` (`Ax32Serv`) |
| service: LOGGER_RE | `demo__checkout.log`, dotnet / java / python exception lines, EBS `APP-FND-01388:`, `Cause:`, `Payables:` |
| service: APP_NAME_RE | `plain_app_text.log`, `timestamp_variants.log`, `kubernetes_cri.log` |
| service: APP_BRACKET_RE (after head) | `dynamics_nav_bc.log` (`[NavServer]`) |
| records: `fold` on | every whole text run except `mut__oracle_ebs_concurrent.log`, and most live batches |
| records: `fold` off + `CONT_RE` folding + unfolded untimestamped lines | `mut__oracle_ebs_concurrent.log` (whole + live), live batches of `mut__java_stacktrace.log`, `mut__python_traceback.log`, `baseline.json`, `storm__alarms.json` |
| PG detail fold (DETAIL, STATEMENT; line has a timestamp) | `postgres.log` |
| fold into an empty message (outer strip) | `oracle_alert.log`, `mut__oracle_alert.log` |
| untimestamped first record | `oracle_ebs_concurrent.log`, many live batches (`mut__java_stacktrace`, `mut__python_traceback`, `mut__tomcat_juli`, `mut__dotnet_serilog`, `mut__oracle_alert`) |
| blank lines skipped | `oracle_ebs_concurrent.log` |
| family default service applied / kept own service | see 4.5 |
| family: strong win, weak win (nginx, oracle), below threshold (`mut__oracle_alert.log`), hint non-empty, hint `""` | see 4.3 / 4.6 / part 01 16 |
| grok detect + tie (RABBITMQ over NODE_WINSTON) | `rabbitmq.log` |
| grok status → severity ERROR / WARN / INFO | `squid_access.log` |
| grok non-participating group / empty group | BIND9 `ms`, NAGIOS `nagios_type`, DOCKER `rest` / DOCKER `rest = ""` |
| grok family default service | bind9, dockerd, nagios, rabbitmq, squid (VMWARE has a service role) |

## 11. Branches NOT covered by any golden

The Go port must still implement them exactly as specified above (the verification of section 1/4/5 regexes includes
hand-written cases for most of them, but no golden row checks the record-level behaviour):

* `parse_head`: CRI line with the application's own timestamp after the prefix; Redis line without a timestamp or
  without a mark, marks `-` and `.`; TS alternatives A6 outside Redis, A7, A8, A10, E13, E10 with a fraction, A1 with
  ` UTC`, A1 with a compact `+0300`, A3 with `T` separator or `PM`; PG levels PANIC, WARNING, NOTICE, INFO, DEBUGn,
  HINT, CONTEXT, QUERY and the `[pid-n]` form; several codes in one line; `SEV_NUM_RE` → CRITICAL / INFO;
  `EMBEDDED_TS_RE` with `:mmm` (the `[:19]` cut), with `rest` starting with `[` (the strip branch), or found mid-line;
  `DYNAMICS_RE` for a line starting with `NavServer`; `LOGGER_RE` rejected because the name ends in `.go`
  (`main.go: x`); APP_BRACKET as service when no level was found and nothing else matched (only `[NavServer]` covers
  it).
* `records`: a PG detail line as the first record; HINT / CONTEXT folding; folding into a CRI `P` record (no-newline
  glue) and the "fold only because the previous record is partial" path; a text with no non-blank line.
* `family`: ties between families; every family not listed in 4.6 (`dynamics-365` as a winner, `sap`, `apache`,
  `zookeeper`, `elasticsearch`, `haproxy`, `postfix`, `sshd`, `iis`, `windows`) and the second pattern of `sqlserver`
  (`Microsoft SQL Server`).
* `grok`: any built-in other than the six detected ones being selected (SYSLOGLINE, COMMON/COMBINED, HTTPD, TOMCAT_*,
  IPTABLES, SSHD, SUDO, CRON, EXIM, DOVECOT, F5, JUNIPER, CHECKPOINT, ZEEK, WINDOWS_TEXT_EVENT, RUBY, NODE_WINSTON,
  SAP_CCMS); unmatched lines (`_unmatched`) and continuation folding in `GrokParser`; `status` dropped because a
  severity role exists; `message` defaulted from the `message` group or the whole line; `GrokParser.detect` returning
  `None` (format `grok` but no pattern reaches 60 % on the parser's own sample → zero rows); user patterns.
* Non-ASCII text in any regex position: only `dynamics_nav_bc.log` (`ü`, `Ş`, `—` inside messages) contains non-ASCII
  characters, and none of them sits next to a `\b`, `\w`, `\d` or `\s` decision. The Unicode-exact rewrites above are
  therefore verified only by the hand-written edge cases, not by goldens.
