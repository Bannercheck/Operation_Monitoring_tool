# Go parse workers with byte-for-byte parity: implementation plan

Lead plan for moving `pipeline.ingest_bytes` (the parse step of `LiveStore._ingest`) into the Go receiver.
It builds on the six specs in this directory and does not repeat them. Section references use these short names:

| ref | file | scope |
|---|---|---|
| **P01** | `01-pipeline.md` | Python semantics helpers, canonical JSON, loader, `_parse_one`, detector, `records_to_observations`, dedupe/fill/sort, profiles, live hint cache |
| **P02** | `02-normalize.md` | `parse_timestamp`, severity, environment, `auto_map`, `flatten`, `enrich`, profiles, scenario constants |
| **P03** | `03-text-family-grok.md` | `parse_head`, `TextParser`, families, grok library and `GrokParser` (incl. a verified Go reference implementation) |
| **P04** | `04-structured.md` | syslog, access, w3c, kv, cef/leef, winevt, csv, json, jsonl |
| **P05** | `05-sap.md` | `SapParser`, `sap_score` |
| **P06** | `06-integration.md` | receiver spool, parse stage, v=2 records, Python fast path, flag, metrics, load test |

Ground facts (checked while writing this plan):

* Module `watchover/receiver` (`receiver/go.mod` currently says `go 1.22`; installed toolchain is go1.24.7). WP1 bumps the
  directive to `go 1.24` (needed for `iter.Seq2` and `for range` over funcs). Standard library only; no third-party module.
* Goldens: 240 files = 120 inputs (54 in `samples/corpus/` without `README.md`, 66 in `receiver/parse/testdata/inputs/`;
  the dir listing shows 67 entries incl. a non-input) x {whole, live}. Generator: `scripts/parse_golden.py`. It processes
  `samples/corpus` sorted, then `testdata/inputs` sorted; per file whole first, then live; one shared temp
  `WATCHOVER_HOME` (learned profiles accumulate across files).
* Live golden loop (`parse_golden.py` lines 99-114): `data.split(b"\n")`, batches of 40 lines joined with `\n`, skip a
  batch when `chunk.strip()` (ASCII bytes strip) is empty, hint counter `n += 1`, `n % 200 == 0` drops the hint, re-hint
  from `r[0]` unless format is `None`/`"table"`, json->jsonl single-object rule. The live golden header holds only the
  first 3 batch reports (`reps[:3]`).
* Format/family of every golden input (whole run, `report[0]`):

| format[:family] | golden inputs |
|---|---|
| access | apache_access_clf.log aws_alb_access.log nginx_combined.log + mut__ of each |
| cef | cef_arcsight.log leef_qradar.log + mut__ of each |
| csv | csv_events.csv demo__alerts.csv mut__csv_events.csv storm__alarms.csv |
| grok | bind9_query.log dockerd.log nagios.log rabbitmq.log squid_access.log vmware_esxi.log + mut__ of each |
| json | alertmanager.json aws_cloudtrail.json baseline.json otel_logs.json storm__alarms.json |
| jsonl | demo__app.jsonl docker_jsonfile.jsonl ecs_json.jsonl gelf.jsonl journald.jsonl mongodb.jsonl mut__docker_jsonfile.jsonl mut__ecs_json.jsonl mut__gelf.jsonl mut__journald.jsonl mut__mongodb.jsonl sim__simulate_batch.jsonl sim__simulate_flows.jsonl sim__simulate_traces.jsonl |
| kv | fortigate_kv.log logfmt.log + mut__ of each |
| sap | sap__SLOG2638.PRD.lst sap__bootstrap.jvm sap__class_prefetch.lst sap__defaultTrace.0.trc sap__dev_w0 sap__indexserver_sapprd01.30003.000.trc sap__sapjvm_gc.jvm sap__sm21_export.file sap__std_server0.out mut__iis_w3c.log mut__kafka_log4j.log |
| syslog | cisco_asa.log demo__db.log haproxy.log iptables_kernel.log paloalto_csv.log postfix.log sudo_cron.log syslog_rfc3164.log syslog_rfc5424.log mut__cisco_asa.log mut__dynamics_ax_aos.log mut__haproxy.log mut__iptables_kernel.log mut__paloalto_csv.log mut__postfix.log mut__sqlserver_errorlog.log mut__sudo_cron.log mut__syslog_rfc3164.log mut__syslog_rfc5424.log |
| text (no family) | demo__checkout.log kubernetes_cri.log plain_app_text.log timestamp_variants.log tomcat_juli.log mut__kubernetes_cri.log mut__oracle_alert.log mut__plain_app_text.log mut__timestamp_variants.log mut__tomcat_juli.log |
| text:dotnet | dotnet_serilog.log mut__dotnet_serilog.log |
| text:dynamics-ax | dynamics_365_batch.log dynamics_ax_aos.log mut__dynamics_365_batch.log |
| text:dynamics-nav | dynamics_nav_bc.log mut__dynamics_nav_bc.log |
| text:golang | golang_log.log mut__golang_log.log |
| text:java | java_stacktrace.log mut__java_stacktrace.log |
| text:kafka | kafka_log4j.log |
| text:mysql | mysql_error.log mut__mysql_error.log |
| text:nginx | nginx_error.log mut__nginx_error.log |
| text:oracle | oracle_alert.log |
| text:oracle-ebs | oracle_ebs_concurrent.log oracle_ebs_fnd_debug.log + mut__ of each |
| text:postgres | postgres.log mut__postgres.log |
| text:python | python_traceback.log mut__python_traceback.log |
| text:redis | redis.log mut__redis.log |
| text:sqlserver | sqlserver_errorlog.log |
| w3c | iis_w3c.log |
| winevt | windows_event_xml.log mut__windows_event_xml.log |

Live batches can have a different format than the whole run (e.g. later batches of `storm__alarms.json` and
`baseline.json` are detected as `text`, P04 §11). Allow-listing is therefore decided per **batch** format:family
(section 2.4).

---

## 1. Package layout (`receiver/parse/`)

Two Go packages plus the receiver main package:

* `watchover/receiver/parse/py` — Python semantics: strings, regex fragments, values, ordered dicts, JSON decode/encode,
  datetimes. No knowledge of logs.
* `watchover/receiver/parse` — the port of `src/watchover` parsing (detector, parsers, normalize, pipeline, hint cache).
* `watchover/receiver` (package `main`) — parse stage, spool v=2 writer, flag, metrics.

Rules for all files: no package-level mutable state except the explicitly listed caches (`Normalizer`), which are
mutex-guarded; all compiled `*regexp.Regexp` are package-level `var` built with `regexp.MustCompile` at init (read-only,
goroutine-safe); every regex string is built from the fragments in `py/regex.go`, never with raw `\d \w \s \S \b`.

### 1.1 `parse/py` package

#### `py/text.go` — implements P01 §1.1-1.6, P02 §0
```go
const WS = "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \u0085                 　" // 29 code points
func IsSpace(r rune) bool
func Strip(s string) string                 // str.strip()
func LStrip(s string) string
func RStrip(s string) string
func StripChars(s, chars string) string     // str.strip(chars)
func Split(s string) []string               // str.split() (no args)
func SplitLines(s string) []string          // str.splitlines() (P01 §1.2)
func Lower(s string) string                 // full Unicode lower: İ -> "i̇", final sigma rule (P01 §1.4)
func Upper(s string) string
func IsDigitStr(s string) bool              // str.isdigit() (Nd + No digits like superscripts)
func IsDecimalStr(s string) bool            // str.isdecimal() / what int() accepts (Nd only)
func Atoi(s string) (*big.Int, error)       // int(str): Python strip, optional sign, '_' rules, Nd digits -> ASCII; error == ValueError
func ParseFloat(s string) (float64, error)  // float(str): Nd digits, inf/nan/infinity case-insensitive, '_' rules
func Len(s string) int                      // code points
func Slice(s string, i, j int) string       // s[i:j] in code points, Python negative/clamp semantics
func IsWord(r rune) bool                    // Python \w: unicode.IsLetter||IsDigit(Nd)||IsNumber||'_'  (exact set per P01 §1.5)
func FoldEqual(r1, r2 rune) bool            // Python re.I equivalence incl. i/I/ı/İ, s/S/ſ, k/K/K
func HasFoldPrefix(s, word string) (n int, ok bool) // P01 §8.1 notLevel helper
```

#### `py/regex.go` — implements P01 §1.5, P03 §0.1, P05 §2
```go
const (
    D   = `\p{Nd}`                                  // Python \d
    W   = `[\p{L}\p{N}_]`                           // Python \w (verify class with P01 §1.5 measurement)
    NW  = `[^\p{L}\p{N}_]`                          // Python \W
    PS  = `[` + wsClass + `]`                       // Python \s
    PNS = `[^` + wsClass + `]`                      // Python \S
    I   = `[iIıİ]`                                  // i under Python re.I
    LW  = `[A-Za-z\x{130}\x{131}\x{17f}\x{212a}]`   // [A-Za-z] under re.I
    LNW = `[^A-Za-z\x{130}\x{131}\x{17f}\x{212a}]`
)
func Translate(pyPattern string) string // pyRe from P03 §0.1: maps \d \w \W \s \S inside and outside [...]; panics on lookaround/\b (those are hand-written)
func MustCompile(pyPattern string) *regexp.Regexp // regexp.MustCompile(Translate(p))
```
Unit test `TestTranslateRejects`: every pattern containing `(?=`, `(?!`, `(?<`, `\b`, `\Z`, `(?P=`, `*+`, `++`, `(?>`
must panic in `Translate` (forces the hand rewrite of section 5).

#### `py/value.go` — attribute/record value model (P01 §2.1, P04 §0.1, §0.6)
```go
type Kind uint8
const ( None Kind = iota; Bool; Int; Float; Str; List; Map )

type Value struct {
    K   Kind
    B   bool
    I   int64     // Kind==Int and Big==nil
    Big *big.Int  // Kind==Int and value outside int64 (JSON ints of any size, Python int())
    F   float64   // Kind==Float (NaN/±Inf allowed; encoder decides)
    S   string    // Kind==Str
    L   []Value   // Kind==List
    M   *Dict     // Kind==Map
}
func S(s string) Value; func Int(i int64) Value; func F(f float64) Value; func B(b bool) Value; var Null Value
func (v Value) Str() string      // Python str(v): None->"None", True->"True", float repr, list/dict repr (P01 §1.6)
func (v Value) Truthy() bool     // Python bool(v)
func (v Value) IsNoneOrEmpty() bool // v is None or ""  (the `v not in (None, "")` test)
func (v Value) HashKey() string  // Python hash equality: 1, 1.0, True collide (used by column_severity memo, P02 §6)

// Dict is a Python dict: insertion ordered, Set on an existing key replaces in place (keeps position),
// Del removes and keeps the order of the rest.
type Dict struct { keys []string; vals []Value; idx map[string]int }
func NewDict(n int) *Dict
func (d *Dict) Get(k string) (Value, bool)
func (d *Dict) Set(k string, v Value)
func (d *Dict) SetDefault(k string, v Value) Value
func (d *Dict) Del(k string) (Value, bool)   // dict.pop
func (d *Dict) Keys() []string
func (d *Dict) Len() int
func (d *Dict) Range(f func(k string, v Value) bool)
func (d *Dict) Clone() *Dict
```

#### `py/json.go` — decode (P04 §8, P01 §8.3 validity) and canonical encode (P01 §2.1, §1.7)
```go
// Loads = json.loads: order kept, duplicate key -> first position last value, int vs float kept (big ints),
// NaN/Infinity/-Infinity accepted, 1e400 -> +Inf, lone surrogates kept (WTF-8 bytes), only " \t\n\r" as outer whitespace,
// ints with > 4300 digits -> ErrPyRaise (ValueError), nesting depth > Python's recursion limit -> ErrPyRaise.
func Loads(s string) (Value, error)
func Valid(s string) bool                       // detector's "is JSON" check (no allocation of the tree)
func FloatRepr(f float64) string                // P01 §1.7
// Dumps = json.dumps(v, ensure_ascii=False, default=str, sort_keys=sortKeys) with ", " / ": " separators.
func Dumps(v Value, sortKeys bool) (string, error) // error ErrNaN when a float is NaN/Inf and the caller forbids it
func AppendString(dst []byte, s string) []byte  // P01 §2.1 escaping: only " \ and < 0x20; raw U+2028/2029 <>&
```

#### `py/datetime.go` — P01 §2.2, P02 §3.9
```go
type DateTime struct {
    T     time.Time // microsecond precision; location = time.FixedZone("", offsetSeconds) or UTC
    Naive bool      // Python naive datetime (no tzinfo); isoformat prints no offset
}
func (d DateTime) ISO() string        // YYYY-MM-DDTHH:MM:SS[.ffffff][±HH:MM[:SS]]
func (d DateTime) Before(o DateTime) bool // instant compare; naive vs aware never happens (P01 §10.3), panics in tests
var Epoch = DateTime{T: time.Unix(0, 0).UTC()} // MISSING_TS
```

### 1.2 `parse` package (port of `src/watchover`)

#### `types.go` — P01 §2, §6, §13; P03 §4.4
```go
type Observation struct {
    TS               py.DateTime `json:"ts"`                // written with DateTime.ISO()
    Message          string      `json:"message"`
    Kind             string      `json:"kind"`              // log|event|alert|incident
    Severity         string      `json:"severity"`          // SEV_RANK names (P02 §2.1)
    Service          string      `json:"service"`
    Host             string      `json:"host"`
    Environment      string      `json:"environment"`
    Origin           string      `json:"origin"`
    Attributes       *py.Dict    `json:"attributes"`        // values: str, int (int64/big), float, bool, None; nested never in goldens
    Source           string      `json:"source"`
    LineNo           int         `json:"line_no"`
    Parser           string      `json:"parser"`
    ParserConfidence float64     `json:"parser_confidence"` // unrounded; canonical writes round(x, 4) as float repr
    Raw              string      `json:"raw"`
}
// The json tags document the canonical key names only; encoding is done by canon.go, never encoding/json.

type Roles struct{ Timestamp, Severity, Service, Environment, Origin, Host, Message string; Has [7]bool } // P02 §10 role order
type Report struct {
    File         string
    Format       string
    Confidence   float64   // raw, not rounded; always written as float repr
    Rows         int
    Family       string    // "" when none
    Kind         string    // "-" when no rows
    Keys         []string  // first-seen order over the first 50 probe records
    Roles        Roles     // written as object, absent role -> null
    Profile      string    // "" or 16-hex id
    DedupDropped int       // written only when > 0
    // Sha, Table: Python-only; not produced (side tables are forwarded, section 6)
}

type Hint struct {
    Format     string  // must be a key of Registry to be honoured
    Confidence float64 // default 0.9
    Family     string
    HasFamily  bool    // Python: "family" in hint (true for every cached hint, even when Family == "")
    N          int
}

type Record struct { LineNo int; Rec *py.Dict }

// FamilyCtx replaces the FAMILY_HINT / FAMILY_SEEN contextvars (P01 §6 step 4/12, P03 §4.4).
type FamilyCtx struct {
    Hint *string // nil = unset; non-nil "" = hint present and empty (disables detection)
    Seen *string // set by detectFamily; nil -> report family ""
}

type Parser interface {
    Name() string
    Kind() string          // default kind
    LineOriented() bool
    // Records mirrors the Python generator: yield returns false to stop (the 50-record probe).
    // An error is a Python exception that escaped records() (ErrPyRaise wrapping the Python exception name).
    Records(text string, fc *FamilyCtx, yield func(Record) bool) error
}

var (
    ErrPyRaise  = errors.New("python would raise")    // whole ingest fails in Python -> Go forwards raw
    ErrForward  = errors.New("not handled in go")     // archive, side table, non-UTF-8, unverified dateutil shape, NaN
)
```

#### `registry.go` — P01 §6 "PARSERS registry"
```go
var Registry = map[string]Parser{ /* json jsonl csv syslog sap kv text access cef winevt w3c grok */ }
var RegistryOrder = []string{"json","jsonl","csv","syslog","sap","kv","text","access","cef","winevt","w3c","grok"}
```

#### `decode.go` — P01 §3, §4
```go
// Decode returns the text of a single /ingest body. Exact for: UTF-8 (with UTF-8 BOM strip).
// Everything else (archives by magic/extension, NUL guard, UTF-16, latin-1 fallback, even-length mojibake) -> ErrForward.
func Decode(name string, data []byte) (string, error)
```

#### `detect.go` — P01 §8 (R1-R11, `detect_delimiter`, `detect_format`)
```go
func DetectDelimiter(lines []string) (delim rune, err error)            // err = ErrPyRaise for csv.Error paths
func DetectFormat(text string, n *Normalizer) (format string, conf float64, err error)
func notLevel(s string) bool                                           // removed lookahead of SYSLOG_RE/SYSLOG_NOPROG_RE
var syslogRE, syslogNoProgRE, rfc5424RE, cefRE, clfRE, albRE, envoyRE, winevtRE, w3cRE, criLineRE, kvRE *regexp.Regexp
```
Calls `sapScore` (sap.go) and `grokDetect` (grok.go) — signatures fixed in those files.

#### `observations.go` — P01 §9
```go
func recordsToObservations(n *Normalizer, recs []Record, source, parser, kind string, mapping *Mapping, conf float64, lines []string) ([]Observation, error)
var levelWordSearch *regexp.Regexp // P01 §9 rewrite
```

#### `pipeline.go` — P01 §5, §6, §7, §10
```go
type Options struct {
    Hint     *Hint
    Profiles ProfileStore // nil = empty store
    Norm     *Normalizer  // required (process-global caches + injected year)
}
// IngestBytes == pipeline.ingest_bytes for ONE file (the live receiver never has multi-file payloads; archives -> ErrForward).
func IngestBytes(name string, data []byte, o Options) ([]Observation, []Report, error)
func parseOne(fname, text string, mapping *Mapping, o Options) ([]Observation, Report, error)
func sharepointULS(obs []Observation)                       // P01 §7 (no golden; unit tests only)
func dedupe(obs []Observation, rep []Report) []Observation   // P01 §10.1 (scenario DEDUP_KEY "alarm_id", prefer "json")
func fillMissingTimestamps(obs []Observation)               // P01 §10.2
func sortObservations(obs []Observation)                    // sort.SliceStable by instant, P01 §10.3
```
The multi-file `ingest` and its process-pool path are **not** ported (live batches are single files; the pool drops the
hint anyway, P01 risks).

#### `hint.go` — P01 §14, P06 §7.1, `parse_golden.py` live loop
```go
type StreamKey struct{ Agent, File string }
type HintCache struct { mu sync.Mutex; m map[StreamKey]*Hint } // one global map; clear-all when len > 5000
// Next returns the hint to use for this batch (nil = detect) and increments N (live.py semantics:
// n%200==0 means "treat as absent for this batch"; the stale entry stays and is replaced only by a non-empty/non-table report).
func (c *HintCache) Next(k StreamKey) *Hint
// Learn applies the post-parse rule (json->jsonl single-object rule with ASCII bytes.Trim(" \t\n\r\v\f")).
func (c *HintCache) Learn(k StreamKey, used *Hint, rep []Report, body []byte)
// GoldenLive replays scripts/parse_golden.py exactly (hint := nil at n%200==0; skip whitespace-only chunks; 40-line split on '\n').
func GoldenLive(name string, data []byte, batch int, o Options) (obs []Observation, reps []Report, batchFormats []string, err error)
```
Note: `parse_golden.py` sets `hint = None` at `n % 200 == 0`, `live.py` keeps the stale entry (P01 §14). Both differ only
after 200 batches; goldens never reach it. `GoldenLive` follows the script, `HintCache` follows `live.py`.

#### `normalize.go`, `timestamp.go`, `dateutil.go`, `strptime.go`, `severity.go`, `environment.go`, `automap.go`, `flatten.go` — P02
```go
type Normalizer struct {
    Year        int                 // datetime.now().year at import; goldens: 2026
    mu          sync.Mutex
    tsCache     map[string]tsResult // cleared at 200_000 (P02 §3.1)
    formatCache map[string]int      // shape -> _FAST_FORMATS index; never cleared (P02 §3.6)
    sevCache    map[sevKey]string   // cap 50_000, no clear (P02 §6)
    envCache    map[[3]string]string
    Strict      bool                // true in the receiver: unverified dateutil shapes -> ErrForward
}
func NewNormalizer(year int) *Normalizer
func (n *Normalizer) ParseTimestamp(v py.Value) (py.DateTime, bool, error)   // P02 §3; error = ErrPyRaise/ErrForward
func (n *Normalizer) parseTimestampStr(s string) (py.DateTime, bool, error) // §3.3
func fromISO(s string) (py.DateTime, bool)                                   // §3.4 (3.11 fromisoformat)
func strptime(s string, f int) (py.DateTime, bool)                          // §3.5, directive regexes verbatim
func shape(s string) string                                                  // §3.6
func dateutilParse(s string, year int) (py.DateTime, bool, error)          // §3.7
func (n *Normalizer) ColumnSeverity(v py.Value, useScale bool) (string, error) // §6
func NormalizeSeverity(v py.Value) (string, error)                          // §5
func SeverityFromText(s string) string                                      // §7
func NormalizeEnvironment(v py.Value) (string, error)                       // §8 (KeyError on ı/ſ tokens -> ErrPyRaise)
func (n *Normalizer) InferEnvironment(texts ...string) (string, error)      // §9
func normKey(k string) string                                               // §10.1
type Mapping struct{ m *py.Dict } // role -> key
func AutoMap(keys []string, sample []*py.Dict, mapping *Mapping) Roles     // §10
func Flatten(d *py.Dict, prefix string) *py.Dict                           // §11
// Constants verbatim (P02 §2): severityMap, roleHints (ordered), envMap, fastFormats, sevRank
```

#### `profiles.go` — P01 "Profiles", P02 §12, P06 §7.2
```go
type Profile struct { ID, Format, Source string; Keys []string; Mapping *Mapping }
type ProfileStore interface {
    Lookup(format string, keys []string) *Profile
    Remember(format string, keys []string, roles Roles, name, source string)
}
func Signature(format string, keys []string) string // sha1(fmt + "|" + ",".join(sorted(keys))).hexdigest()[:16]
type MemStore struct{ ... }                         // golden tests: starts empty, Remember implemented exactly (P02 §12)
type FileSnapshot struct{ ... }                     // receiver: read-only profiles.json (mtime reload); Remember no-op
func (f *FileSnapshot) HasUser(format string, keys []string) bool // user profile -> batch forwarded
```

#### Parser files
| file | implements | spec |
|---|---|---|
| `text.go` | `TS_RE` .. `DYNAMICS_RE`, `parseHead`, `TextParser` | P03 §1-3, §7 |
| `family.go` | `families` (24, ordered), `strong`, `detectFamily(lines, fc)` | P03 §4 |
| `grok.go` | `grokBase`, `grokLines` (27, Go-expanded sources), `expand`, `grokDetect(lines []string) (*linePattern, float64)` | P03 §5 |
| `grok_parser.go` | `GrokParser`, `statusSev` | P03 §6 |
| `syslog.go` | `SyslogParser`, `pan` | P04 §1 |
| `access.go` | `AccessParser`, `parseExtra`, `statusSeverity` | P04 §2 |
| `w3c.go` | `W3cParser` | P04 §3 |
| `kv.go` | `KvParser` | P04 §4 |
| `cef.go` | `CefParser`, `splitHeader`, `cefKV` scanner | P04 §5 |
| `winevt.go` | `WinEvtParser`, `eventScan`, minimal expat-compatible XML reader | P04 §6 |
| `csvparse.go` | CPython `_csv` reader state machine, `CsvParser` | P04 §7 |
| `json.go` | `JsonParser`, `findRecords`, `otelRecords`, `enrich` | P04 §8, P02 §11.1 |
| `jsonl.go` | `JsonlParser` | P04 §9 |
| `sap.go` | `SapParser`, `sapScore(lines []string) float64`, `iso`, `sevFromWords`, `svcFromLogger`, `finishNWJ` | P05 |

Each parser type: `type XParser struct{}`; methods `Name() string`, `Kind() string`, `LineOriented() bool`,
`Records(text string, fc *FamilyCtx, yield func(Record) bool) error`.

#### `canon.go` — P01 §2
```go
// CanonLine = json.dumps(canon(o), sort_keys=True, ensure_ascii=False, default=str). Error ErrForward on NaN/Inf.
func CanonLine(o Observation) ([]byte, error)
// CanonReport = json.dumps({"report": reps}, sort_keys=True, ...) without sha/table.
func CanonReport(reps []Report) []byte
```

#### `allow.go` — section 2.4
```go
//go:embed testdata/allow.txt
var allowTxt string
func Allowed(format, family string) bool // parsed once at init
```

#### Tests (`*_test.go`)
`parity_test.go` (section 2), plus per-WP unit tests listed in section 3.

---

## 2. Parity: comparison rules and test design

### 2.1 What must be identical

Every golden file line, after Go writes it with `CanonReport` / `CanonLine`, must be **byte-identical** to the golden line.
This is the gate. Byte identity already implies the rules below; they are spelled out so a mismatch report is readable
and so nobody "fixes" the comparator instead of the code:

1. **Line count and order** identical (whole: one `ingest` call; live: concatenation of batch results).
2. **Keys**: the 14 observation keys, the report keys of P01 §2.5. Key order is irrelevant semantically but the canonical
   encoder sorts keys at every level, so it is identical anyway (attribute key insertion order never matters for the
   golden; it **does** matter internally: `raw` JSON fallback, k=v message join, `auto_map`, report `keys`).
3. **Strings**: code-point identical; escaping per P01 §2.1 (`<>&`, U+2028/2029, non-ASCII raw).
4. **Numbers**: the JSON kind must match: Python `int` -> digits without `.`/`e`; Python `float` -> `FloatRepr`
   (`1.0`, `0.95`, `1e-05`, `1e+16`). An `int` 2 and a `float` 2.0 are a mismatch. Big ints written in full.
   `parser_confidence` = `FloatRepr(pyRound4(conf))`. Python `round(x, 4)` rounds the exact binary value correctly
   (half-even only on exact ties); `strconv.ParseFloat(strconv.FormatFloat(x, 'f', 4, 64), 64)` has the same semantics.
   Unit cases: the golden values `1.0`, `0.95`, `0.61`, `0.57`, `1.2`, `1.23`. Report `confidence` is not rounded.
5. **Timestamps**: compared as instants **and** with identical ISO rendering: same instant, same UTC offset, microseconds
   present iff non-zero, `+00:00` never `Z`. (Goldens contain only aware stamps with `+00:00`/`+03:00`.)
6. **Booleans / null**: `true`/`false`/`null`; Python `True` attribute is `true`, never `1`.
7. **Report line**: identical including `profile`, when the test runs **all** inputs in generator order with one
   `MemStore` (2.2). If the run is filtered (`-run TestGolden/x`), `profile` is masked to `""` on both sides because the
   learned-profile history differs (P02 risks: 14 `mut__*.whole` reports differ in isolation).
8. The live golden report contains only the first three batch reports; compare those three.

### 2.2 `TestGolden`

```go
func TestGolden(t *testing.T)
```
1. Input list = `samples/corpus/*` (sorted, excluding `*.md`) then `receiver/parse/testdata/inputs/*` (sorted): exactly the
   generator order. Paths resolved relative to the test file (`../../samples/corpus`).
2. One `Normalizer` (`Year: 2026`, `Strict: false`) and one `MemStore` for the whole test, processed sequentially in the
   generator order, for each file: whole (`IngestBytes(name, data, Options{})`), then live (`GoldenLive(name, data, 40, ...)`).
   Sharing reproduces format-cache and learned-profile order dependence (P02 §1).
3. For each (input, mode): read `testdata/golden/<input>.<mode>.jsonl.gz`, encode Go output, compare line by line.
   `t.Run(input+"/"+mode, ...)` subtests.
4. On mismatch report: input, mode, Go count vs golden count, first 5 differing line indexes, and for each a field diff
   (decode both lines with `encoding/json` `UseNumber` into ordered maps, list keys whose canonical text differs, show
   both values). Also any `ErrPyRaise`/`ErrForward` returned.
5. Result classification: `PASS`, `FAIL`, `ERR`. Subtests of inputs listed in `allow.txt` call `t.Errorf` on non-PASS;
   others call `t.Logf` and `t.Skip` (so CI stays green while ports land, but progress is visible).
6. Summary at the end (`t.Cleanup` on the parent): table per format:family with pass/total, and per-batch format
   inventory from `GoldenLive` (`batchFormats`). With `-parity.out=FILE` the summary is written to FILE.
7. Flags: `-parity.input=glob` filter (masks profile, 2.1 rule 7), `-parity.year=2026`.

### 2.3 Other harness tests

* `TestAllowConsistency`: every `format[:family]` listed in `allow.txt` must have **all** of its golden inputs listed and
  passing in both modes, and must not occur as a batch format in any failing live replay (otherwise a batch of an
  allowed format would be wrong in production).
* `TestCanonRoundTrip`: for every golden line, `Loads` then `Dumps(sortKeys=true)` reproduces the line byte for byte
  (validates the encoder/decoder independently of the parsers; first thing WP1 makes green).
* `TestDeterminism`: run `TestGolden`'s pipeline twice in one process and with `GOMAXPROCS=1` vs 8: identical bytes.
* `FuzzIngestNoPanic`: fuzz `IngestBytes` on corpus seeds; must never panic (errors allowed).
* Differential tests against Python (opt-in, `-tags pydiff`, need `python3` and `PYTHONPATH=src`): run
  `scripts/parse_golden.py`-style probes for regex tables (P01 §8, P03 §1.16, P05 §2.1 already did this offline),
  `parse_timestamp` on the P02 probe list, dateutil on random tokens. Not part of default `go test`.

### 2.4 `testdata/allow.txt`

Format (one entry per line, `#` comments):
```
# format[:family]   input file (golden basename)     modes
kv                  fortigate_kv.log                  whole,live
kv                  mut__fortigate_kv.log             whole,live
kv                  logfmt.log                        whole,live
kv                  mut__logfmt.log                   whole,live
```
* Entries are added by the WP that made them pass, never edited by hand to hide a failure.
* `Allowed(format, family)` is true iff the pair (or for `text`/`grok`, `format:family`; family `""` is its own key
  `text:`) appears in `allow.txt` and `TestAllowConsistency` holds. Runtime env `WATCHOVER_GO_PARSE_FORMATS` /
  `WATCHOVER_GO_PARSE_DENY` can only narrow it (P06 §6).
* The receiver decides per batch after Go parsing: if the batch's `report[0].format[:family]` is not allowed, the record is
  forwarded raw (v=1) — but the hint cache is still advanced (section 4.4).

Expected order of allow-listing: jsonl, kv, access, cef, syslog, csv, w3c, winevt, grok, json, text families, sap.

---

## 3. Work packages

All WPs work in `receiver/parse/` (+ `receiver/` for WP7) on **disjoint files**. WP1 lands first and creates every file of
section 1 as a compiling stub (exact signatures, bodies `return nil, ErrForward` / zero values) so the others only replace
bodies in their own files. Shared files (`types.go`, `py/*`) are owned by WP1; changes go through WP1's owner.

```
WP1 core+detector+pipeline ──┬─► WP2 normalize ─┬─► (golden passes for all parsers)
                             ├─► WP3 text/family/grok
                             ├─► WP4 line-structured parsers
                             ├─► WP5 document parsers
                             ├─► WP6 SAP
                             └─► WP7 integration (can start with stubs; enables formats as allow.txt grows)
```
Golden passes need WP1 + WP2 + the parser's WP. WP3's `parseHead` / `TS_RE` / `LEVEL_RE` / `LOGGER_RE` are used by WP4 (kv)
and WP5 (json `enrich`): WP3 delivers those first (day 1) as exported-within-package functions:
`parseHead(ln string) (*py.Dict, string)`, `tsRE`, `levelRE`, `loggerRE`, `contMatch(ln string) bool`.

### WP1 — core, Python semantics, canonical JSON, detector, pipeline, hint cache, harness
* Files: `go.mod` (go 1.24), `py/text.go`, `py/regex.go`, `py/value.go`, `py/json.go`, `py/datetime.go`, `types.go`,
  `registry.go`, `decode.go`, `detect.go`, `observations.go`, `pipeline.go`, `hint.go`, `canon.go`, `allow.go`,
  `testdata/allow.txt`, `parity_test.go`, plus stubs of all other files.
* Spec: P01 §1-§10, §13-§16; P02 §0, §14; P04 §0; P06 §7.1.
* Goldens: none alone; owns `TestCanonRoundTrip` (all 240 files) and the harness.
* Unit tests: `SplitLines` table (P01 §1.2), `Lower` (`İ`, `Σ` final), `Strip` (`\x1c`), `IsDigitStr` (superscript 2),
  `FloatRepr` (1.0, 1e-05, 1e+16, 1.2345678901234567e+19, -0.0), `Loads` (dup keys, NaN, 1e400, big int, surrogate),
  `AppendString` (U+2028, `<`), `DateTime.ISO`, `notLevel` (I/ı/İ, `WARNING`, `ERRx`), detector R1-R11 on P01 §8 edge
  cases, `DetectFormat` on first 200 physical lines then non-blank, syslog confidence > 1.0 (1.2, 1.23), attributes-empty
  rule (P01 §9 step 6), `fillMissingTimestamps` (min with first-wins offset, epoch fallback, empty), `dedupe` (all
  uncovered branches of P01 §10.1), stable sort with equal instants/different offsets, `HintCache` (200/399/598 re-detect,
  `""` family applied, json->jsonl rule, 5000 clear, whitespace-only body), `GoldenLive` chunking (`b"\n"` split, skip).

### WP2 — normalize, timestamps, auto_map, flatten, profiles
* Files: `normalize.go`, `timestamp.go`, `strptime.go`, `dateutil.go`, `severity.go`, `environment.go`, `automap.go`,
  `flatten.go`, `profiles.go`.
* Spec: P02 §1-§13, §15-§16; P01 "Profiles".
* Goldens: unlocks all; first targets jsonl (`gelf.jsonl`, `journald.jsonl`, `ecs_json.jsonl`, `mongodb.jsonl`,
  `docker_jsonfile.jsonl` + mut__, `demo__app.jsonl`, `sim__*.jsonl`) together with WP5; dateutil cases
  `bind9_query.log`, `fortigate_kv.log`, `tomcat_juli.log`.
* Unit tests: the ~140 probed `parse_timestamp` inputs of P02 §3 as a table (exact ISO results), 13/14/16/19-digit epochs
  with big.Rat + half-even micro rounding, all 26 `_FAST_FORMATS` (incl. uncovered 1-6, 9, 13, 16, 18, 21-23, 25-26),
  format-cache order dependence (d/m vs m/d), `_ts_cache` clear at 200 000, year injection, Feb 29, exceptions that
  escape (P02 §3.10 -> `ErrPyRaise`), dateutil grammar on the 2 495 free-text values (all None) plus the probed shapes
  (`123456789`, `1.2.3`), `ENV_TOKEN_RE` scanner with ı/İ/ſ/K boundaries, KeyError path, `[:20]` truncation,
  `column_severity` 1/1.0/True memo collision, `auto_map` P02 §10.3 examples and duplicate/padded keys, `flatten`
  collisions, `Signature`, `MemStore.Remember` vs P02 §12 (ids in `mut__logfmt.log` live report: `5d7e874a6060f57b`).

### WP3 — free text: text parser, families, grok
* Files: `text.go`, `family.go`, `grok.go`, `grok_parser.go`.
* Spec: P03 entire (reuse its verified Go reference code in §1.16 and §5.7.4).
* Goldens: all `text` and `text:*` inputs of the table above (35 inputs) and all 12 `grok` inputs.
  Lock-in cases: `postgres.log` pid includes the timestamp, `oracle_ebs_fnd_debug.log` message starts `003] [ICM]`,
  `mut__postgres.log` (`Z +03`), `bind9_query.log` -> `2026-01-01`, `dynamics_365_batch.log` -> `dynamics-ax`,
  `mut__oracle_alert.log` no family, `mut__oracle_ebs_concurrent.log` live 118 vs whole 158 rows.
* Unit tests: P03 §2.1 `parseHead` examples (key order + rest), the ~170 Unicode edge lines of P03 (convert the offline
  differential set into a table: `ValueErroré`, `port 22é`, `<1é2>`, U+00A0, U+001F), family ties and strong/weak
  thresholds, empty family hint disables detection, grok detect sampling difference (first 120 non-blank vs non-blank of
  first 120), DHCPD removed (0 hits), each grok override necessity test, uncovered branches of P03 §11.

### WP4 — line-structured parsers: syslog, access, w3c, kv, cef/leef
* Files: `syslog.go`, `access.go`, `w3c.go`, `kv.go`, `cef.go`.
* Spec: P04 §0-§5, §10, §12; uses `syslogRE`/`rfc5424RE`/`kvRE` from `detect.go` and `tsRE`/`levelRE`/`loggerRE` from WP3.
* Goldens: 19 `syslog`, 6 `access`, `iis_w3c.log` (w3c), 4 `kv`, 4 `cef` inputs (table above).
* Unit tests: P04 §1.2 `_NOT_LEVEL`, PAN TRAFFIC/THREAT, ASA vendor variants, `<PRI>` on BSD lines, U+FEFF in 5424, VHOST
  rewrite, Envoy branch, `parseExtra` overwrite order, kv `msg="hello world` -> `hell`, `,;` stripping, Keycloak
  `client=None`, `cefKV` scanner vs findall table, LEEF 1.0 `|` misread, multi-char LEEF delimiter `x09`, W3C KeyError
  path, all P04 §12 exception paths -> `ErrPyRaise`.

### WP5 — document parsers: json, jsonl, csv, winevt
* Files: `json.go`, `jsonl.go`, `csvparse.go`, `winevt.go`.
* Spec: P04 §0, §6-§9, §12; P02 §11 (`Flatten`, `enrich`); P01 §7 (`sharepointULS` is in WP1's `pipeline.go`, WP5 supplies
  test data).
* Goldens: 14 `jsonl`, 5 `json`, 4 `csv`, 2 `winevt` inputs. Note live `storm__alarms.json` / `baseline.json` batches
  route to `text` (needs WP3 too).
* Unit tests: CPython `_csv` state machine (quotes, doublequote, lone `\r` -> `ErrPyRaise`, 131 072 field limit,
  multi-line fields, short/long rows -> DictReader `None` key / restval), `line_no = row index + 1` with mismatched `raw`,
  leading blank line -> header empty, `findRecords` deep search, OTLP `instrumentationLibraryLogs`, non-dict body,
  winevt attribute whitespace normalisation, duplicate attribute / unbound prefix rejection, CDATA/entities, multi-line
  events, `raw` = `json.dumps(rec)` insertion order.

### WP6 — SAP
* Files: `sap.go` (and `sapScore` used by `detect.go`).
* Spec: P05 entire (33 regexes with verified rewrites in §2.1).
* Goldens: the 9 `sap__*` inputs + `mut__iis_w3c.log`, `mut__kafka_log4j.log` (live confidence 0.57 vs whole 0.64).
* Unit tests: P05 §9 "Branches NOT covered" (probed outputs are quoted in §4-§5): availability log incl. `ValueError` ->
  `ErrPyRaise`, tp/R3trans, JUL PM/12 AM, GC rounding half-even, DEV_META swallowing `Profile parameter`, NW Java
  blank-line termination, end-of-text records with `line_no` 1, stable class-list sort, `svcFromLogger` cases.

### WP7 — receiver integration and Python fast path
* Files: `receiver/parsestage.go`, `receiver/parsestage_test.go`, small edits in `receiver/main.go` (config, start
  stage, `/health` fields, `/metrics`), `src/watchover/live.py` (`_parse`/`_accept` split, `ingest_parsed`,
  `_note_hint`), `src/watchover/spool_consumer.py` (glob, `_handle` v=2), `api/services.py` (flag), new
  `scripts/parse_shadow_diff.py`, `tests/test_ingest_parsed.py`. Python edits follow CLAUDE.md (README section + AI_LOG
  line per commit).
* Spec: P06 §3-§13; section 4 below.
* Tests: `TestParseStageOrderAndFallback`, `TestParseStageResume`, `TestParsedHeaderFields`, `TestHintCacheParity`,
  Python `test_ingest_parsed_equivalence` (P06 §13); load test plan P06 §11.

---

## 4. Integration

### 4.1 Stage wiring (`receiver/parsestage.go`, `main.go`)

```go
type parseMode int // 0 off, 1 shadow, 2 on — from WATCHOVER_GO_PARSE ("", "0", "shadow", "1")
type parseStage struct {
    cfg      config
    mode     parseMode
    hints    *parse.HintCache
    norm     *parse.Normalizer          // NewNormalizer(time.Now().Year()), Strict=true
    profiles *parse.FileSnapshot        // <spool>/profiles.json, mtime reload every 2 s
    workers  int                        // WO_PARSE_WORKERS, default GOMAXPROCS
    timeout  time.Duration              // WO_PARSE_BATCH_TIMEOUT, default 2s
    stats    parseStats                 // atomics, section 4.5
}
func newParseStage(cfg config) *parseStage
func (p *parseStage) run(ctx context.Context)              // reader -> shard workers -> ordered emitter
func (p *parseStage) handle(h rawHeader, body []byte) (hdr []byte, out []byte, reason string) // reason=="" -> v=2
```
* `main()`: if mode != off, `go stage.run(ctx)` after the spool writer starts. The HTTP path is unchanged.
* Reader: closed `seg-*.wo` oldest first, resume at `seg-<ns>.gpos`; assigns `seq`; shard =
  `fnv32a(agent + "\x00" + file) % workers`; bounded channels (256) and 256 MB in-flight cap.
* Worker: `recover()` around `handle` -> raw (reason `panic`); per-batch timeout -> raw (reason `timeout`, the late
  result is discarded; the hint cache was already advanced in order since workers are per-shard sequential).
* Emitter: reorder buffer by `seq`; writes `pseg-<ns>.tmp`, rotates like `spool.rotate` (`segBytes`, age), fsync,
  rename to `.wo`; then deletes the raw segment and its `.gpos` when every record of it is in a closed pseg.
* Shadow: writes `pseg-shadow-<ns>.wo` and leaves `seg-*.wo` for Python; raw segment deletion stays with Python.
  The stage must therefore read shadow input **before** Python deletes it: in shadow mode the stage reads open/closed
  raw segments by copying records on rotation (hook in `spool.rotate`: send closed segment name to the stage channel).
* Backlog for 503 = `seg-*.wo` + `pseg-*.wo` sizes.

### 4.2 Per-batch decision (`handle`)

```
if h.path != "/ingest"                      -> raw (reason "path")
k := StreamKey{h.agent || "unknown", h.file || "agent.log"}
hint := hints.Next(k)                        // ALWAYS, also when forwarded (keeps lockstep with Python _detect)
if profiles.scenarioMapping                  -> raw ("scenario"), hints.LearnFromPython? no: see 4.4
obs, rep, err := parse.IngestBytes(h.file, body, Options{Hint: hint, Profiles: profiles, Norm: norm})
hints.Learn(k, hint, rep, body)              // also on forward, using Go's report (same as Python would compute)
switch {
case err == ErrForward / ErrPyRaise         -> raw ("decode"|"pyraise"|"dateutil"|...)
case profiles.HasUser(fmt, rep[0].Keys)     -> raw ("profile")
case !parse.Allowed(fmt, family) || envDeny -> raw ("format")
case any NaN/Inf attribute                  -> raw ("nan")
default                                     -> v=2
}
```
If Go returned an error, `Learn` is skipped and the cache entry is **invalidated** for this key (Python's
`_detect` behaviour after an exception is "no update"; Go cannot know the report, so it drops the entry and Python's
`_note_hint` is not called for raw records). The divergence is limited to the next batch of that stream: Python still holds
its entry and uses it, Go re-detects. Mitigation: on any error Go marks the stream `sticky-raw` until its next re-detect
(n%200==0) so all of that stream's batches are parsed by Python and Go's hint never matters. Counted in
`parse_forward_reason_sticky`.

### 4.3 Exact v=2 record format

Framing unchanged: `<header JSON>\n<body (len bytes)>\n`.

Header: `json.Marshal` of
```go
type parsedHeader struct {
    V             int             `json:"v"`              // 2
    Kind          string          `json:"kind"`           // "parsed"
    Path          string          `json:"path"`           // "/ingest"
    File          string          `json:"file"`
    Agent         string          `json:"agent"`
    AgentID       json.RawMessage `json:"agent_id,omitempty"` // copied verbatim from v=1 (absent if absent)
    Env           *string         `json:"env,omitempty"`
    Site          *string         `json:"site,omitempty"`
    IP            string          `json:"ip"`
    Identity      json.RawMessage `json:"identity"`       // copied verbatim
    TS            string          `json:"ts"`             // v=1 receive time, unchanged
    Len           int             `json:"len"`            // bytes of the NDJSON body
    N             int             `json:"n"`              // observation count
    RawLen        int             `json:"raw_len"`        // original body length
    Format        string          `json:"format"`
    Family        string          `json:"family"`
    Confidence    float64         `json:"confidence"`
    Report        json.RawMessage `json:"report"`         // CanonReport entry 0 object (without sha/table)
    ParserVersion string          `json:"parser_version"` // "<pkg semver>+<sha1[:12] of sorted golden file hashes>"
}
```
Copying rule: every key of the v=1 header map is preserved with its original JSON value; the struct above lists the
keys the receiver writes today (`v, path, file, ip, ts, len, identity, agent, agent_id, env, site`). Implementation:
decode v=1 header into `map[string]json.RawMessage`, overwrite `v`, `len`, add the new keys, re-marshal (map ->
sorted keys, fine for Python `json.loads`).

Body: for each observation in `IngestBytes` order, `CanonLine(o)` + `\n` (exactly the golden line format). `n=0` ->
empty body, record still emitted.

Raw fallback: the original v=1 header bytes and body bytes, unchanged.

### 4.4 Python side

* `spool_consumer.py`: `pending()` globs `pseg-*.wo` when `WATCHOVER_GO_PARSE == "1"` (drains remaining `seg-*.wo`
  first after a switch), `_handle`: `if h.get("v") == 2 and h.get("kind") == "parsed": store.ingest_parsed(body, h)`.
  Name slicing for pseg (`[5:24]` instead of `[4:23]`).
* `live.py`: split `_ingest` into `_parse` + `_accept` (no behaviour change, covered by existing tests), add
  `ingest_parsed` (P06 §5) and `_note_hint(agent, name, h)` = the `_detect` update with `rep[0] = {format, confidence,
  family}` and body = the **raw** single-object rule input: Go puts the post-rule format in the header (it computed it
  from the raw body), so Python stores `h["format"]` as is and must not reapply the json->jsonl rule.
* `profiles.remember` for v=2: called with `report.keys/roles` from the header, debounced to once per minute per
  signature (affects only `hits/last`).
* `profiles.json` + `scenario.MAPPING` export: Python writes `<spool>/profiles.json` (atomic rename) whenever the store
  changes, with a top-level `"_scenario_mapping": {}`; Go reads it (`FileSnapshot`).
* `fromisoformat` round-trips `DateTime.ISO()`; `parser_confidence` arrives rounded to 4 decimals (accepted, unused
  after `_accept`).

### 4.5 Flag and metrics

* `WATCHOVER_GO_PARSE`: unset/`0` off, `shadow`, `1` on; read by receiver and Python (same compose env).
  `WATCHOVER_GO_PARSE_FORMATS`, `WATCHOVER_GO_PARSE_DENY`, `WO_PARSE_WORKERS`, `WO_PARSE_BATCH_TIMEOUT`.
* `/health` additions (ints): `parse_enabled`, `parse_mode`, `parse_batches_parsed`, `parse_batches_forwarded`,
  `parse_forward_reason_{format,profile,panic,timeout,nan,decode,pyraise,dateutil,scenario,sticky,path}`,
  `parse_observations`, `parse_lines_in`, `parse_bytes_in`, `parse_ns_total`, `parse_backlog_bytes`,
  `parse_backlog_segments`, `parse_pseg_backlog_bytes`, `parse_lag_ms`, `parse_hint_streams`; map
  `parse_by_format` (`{"kv": {"parsed": n, "forwarded": n, "obs": n}}`). `/metrics` gains
  `watchover_receiver_parse_batches{format=..,result=parsed|forwarded}` lines.
* Python `SpoolConsumer.stats()`: `parsed_records`, `raw_records`, `ingest_parsed_ms_avg`.
* Rollout: shadow >= 24 h with 0 mismatches per format (`parse_shadow_diff.py`) before the runtime allow list includes it;
  load test per P06 §11.

---

## 5. RE2 conversion table (consolidated)

Fragments (from `py/regex.go`): `D`=`\p{Nd}`, `W`=`[\p{L}\p{N}_]`, `NW`=`[^\p{L}\p{N}_]`, `PS`/`PNS` = Python `\s`/`\S`
(29 code points, incl. `\v`, `\x1c-\x1f`, `\x85`, `\xa0`, U+2000-200A ...), `I`=`[iIıİ]`, `LW`/`LNW` = `[A-Za-z]` under
`re.I`. Global rules (apply to every pattern below and every pattern marked "class only"):

| Python construct | Go handling |
|---|---|
| `\d` `\w` `\W` `\s` `\S` (str patterns are Unicode) | `D` `W` `NW` `PS` `PNS`, also inside `[...]` (merge class contents) |
| `\b` (Unicode word boundary) | never RE2 `\b` (ASCII); consuming `(?:^|NW)` / `(?:NW|$)` for boolean or group-only uses; hand scanner when the match span matters |
| `re.I` | `(?i)` plus every `i` as `I`, every `[A-Za-z]` as `LW` (Go already folds `ſ`, `K`) |
| `(?=X)` / `(?!X)` / `(?<=X)` / `(?<!X)` | per-pattern rewrite below (consume + adjust span, post-check, or scanner) |
| `re.match` | `^` prefix + `FindStringSubmatchIndex` |
| `re.fullmatch` | `^(?:...)$` |
| `$` | Go `$` (all inputs come from `splitlines()`, no trailing `\n`) |
| group not participating (`None`) | index `-1`; keep distinct from `""` where code distinguishes |
| `findall` | `FindAllStringSubmatch(s, -1)` (non-overlapping leftmost-first, same as Python) |
| `str.lower()` before matching | `py.Lower` (not `strings.ToLower`) |
| `re.X`, backrefs, possessive, atomic, conditionals, `\Z` | **none occur** in parsing code except `csv.Sniffer` (not ported: side tables forwarded) and grok `(?>` (expanded to `(?:`) |

Per-pattern table (only patterns needing more than the global rules; everything else = "class only"):

| Pattern (where) | Problem | Go rewrite / code | Spec |
|---|---|---|---|
| `SYSLOG_RE`, `SYSLOG_NOPROG_RE` (format_detector, syslog parser) | `_NOT_LEVEL` negative lookahead with `(?i:...)` and `\b` | drop lookahead; after match `notLevel(line[hostStart:])` with Python fold + Unicode `\b`; exact because host start is forced | P01 §8.1, P04 §1.2 |
| `WINEVT_RE` `^\s*<Event\b[^>]*xmlns=` | Unicode `\b` | `^PS*<Event[^\p{L}\p{N}_>][^>]*xmlns=` | P01 §8.1 |
| `KV_RE`, `RFC5424_RE`, `CEF_RE`, `CLF_RE`, `ALB_RE`, `ENVOY_RE`, `W3C_RE`, `CRI_LINE_RE` | class only | R3-R7, R9-R11 Go forms verbatim in P01 §8.1 | P01 §8.1 |
| `LEVEL_WORD_RE` (normalize) | lookbehind/lookahead, `re.I` | boolean: `(?i)(?:^|LNW)(?:TRACE|DEBUG|[Iıİ]NFO|...)(?:LNW|$)` (P01 §9); span users: scanner | P01 §9, P02 §4.2 |
| `ENV_TOKEN_RE` (normalize) | lookbehind/lookahead, `re.I` | hand scanner; boundary class treats ı/İ/ſ/K as letters; tokens with `i` match ı/İ; KeyError on ı/ſ/İ tokens -> `ErrPyRaise` | P02 §4.1 |
| `ERROR_HINT_RE`, `WARN_HINT_RE` (normalize) | Unicode `\b`, `re.I` | consuming boundaries; special rule for `\b[A-Za-z_.$]*(?:Exception|Error)\b: ` | P02 §4.3 |
| `_shape`, `_from_iso`, epoch, strptime regexes (normalize) | class only (`(?-i:Z)` OK in RE2) | `D`, `PS`; fullmatch anchors | P02 §3.4-3.6 |
| `TS_RE` (text, kv) | `(?=\s)` in epoch alternatives | non-epoch alternatives first, then `(D{10}(?:\.D{1,6})?)PS` / `(D{13})PS` as separate groups; match end unchanged | P03 §1.1, P04 §4.1 |
| `THREAD_ID_RE` `^\d+\s+(?=\[)` | lookahead | `^D+PS+\[` and end-1 | P03 §1.5 |
| `REDIS_RE` | lookahead | match fixed-width date literally; rest starts at end(g2)+1 | P03 §1.9 |
| `APP_NAME_RE` `(?=[\s:])` | lookahead | consume `[pyWS:]`, use group 1 (run is maximal) | P03 §1.12 |
| `CONT_RE` `(?::|\b)` | Unicode `\b` | `(?::|NW|$)` boolean only | P03 §1.10 |
| `SEV_NUM_RE` `\bSeverity: (\d{1,2})\b` | Unicode `\b` | `(?:^|NW)Severity: (D{1,2})(?:NW|$)`; `int()` on Nd digits | P03 §1.14 |
| `LEVEL_RE`, `DYNAMICS_RE` (text, kv) | `re.I` i-folding | `I` for every i, `[:PS]` | P03 §1.2, §1.15 |
| family patterns (24 families) | Unicode `\b` | `(?:^|NW)X` / `X(?:NW|$)`; drop redundant trailing `\b` of `\bSysOperation\w*\b`, `\bDMF\w*\b`; dotnet class has a back-quote (no raw string) | P03 §4.1 |
| grok `BASE10NUM`/`BASE16NUM`/`IPV4`/`TIME` lookarounds | lookbehind/lookahead (TIME `(?!<` typo = no-op) | dropped, proven redundant in all built-in LINES | P03 §5.7.1-5.7.2 |
| grok `WORD`, `HOSTNAME`, `MONTH` `\b` | Unicode `\b` | `W+`; `\.?`; drop `\b` (neighbours force boundary) | P03 §5.7.2 |
| grok `SYSLOGFACILITY` | `\b` between digits | `<(?P<facility>[0-9]+)[^\w\n](?P<priority>[0-9]+)>` (with `W` class) | P03 §5.7.3 |
| grok `SSHD_AUTH` trailing port | `\b` at end | `port (?P<port>[1-9][0-9]*)(?:NW|$)` | P03 §5.7.3 |
| grok `DHCPD` | never matches in Python | remove from Go list | P03 §5.7.3 |
| grok `POSINT`/`NONNEGINT` | ASCII `\b` | keep Go `\b` (neighbours always ASCII literals) | P03 §5.7.2 |
| grok user patterns (`grok.json`) | arbitrary Python regex | not supported: if `grok.json` exists, grok batches forwarded | P03 §5.5 |
| `VHOST_RE` (access) | lookahead `(?=\S+ \S+ \S+ \[)` | match `^([W.\-]+)(?::D+)?` then test `^PNS+ PNS+ PNS+ \[` on the remainder | P04 §2.1 |
| `parse_extra` fullmatch `\d+\.\d+`, `\d+` | class only | `^D+\.D+$`, `^D+$` | P04 §2.3 |
| `CEF_KV` (cef) | lookahead + lazy value | hand scanner reproducing findall (key may start mid-word, retry at +1) | P04 §5.1 |
| `CEF_SEV`, `SYSLOG_PREFIX`, `CEF_HEAD`, `LEEF_HEAD` | class only | P04 §5.1 | P04 §5.1 |
| `EVENT_RE` `<Event\b.*?</Event>` re.S | Unicode `\b`, DOTALL | manual scan: `<Event` + next rune not `W` -> first `</Event>` | P04 §6.1 |
| `DATE_ONLY_RE` (sap) | lookbehind/lookahead | `(?:^|\P{Nd})(D{2})\.(D{2})\.(D{4})(?:$|\P{Nd})` groups 1-3 | P05 §2 R27 |
| `ERR_WORDS`, `WARN_WORDS` (sap) | Unicode `\b`, `re.I` | `(?:^|NW)(?i:...)(?:$|NW)` with `I`, boundaries outside `(?i:)` | P05 §2 R28-29 |
| `GC_PAUSE_RE` (sap) | trailing `\b` | `ms(?:$|NW)`, group 1 unchanged | P05 §2 R20 |
| `TP_STAMP_RE` (sap) | `\b` both sides | consumed `NW` or `^`/`$` | P05 §2 R24 |
| `DEV_META_RE` (sap) | `re.I` | `I` in sid/pid/profile; key = `py.Lower` + replace ASCII space | P05 §2 R6 |
| `NWJ_START_RE` (sap) | class `[\s\x08]` | `[pyWS\x08]` | P05 §2 R8 |
| `TZ_RE`, `NWJ_CATEGORY_RE`, `NWJ_MSGID_RE`, `NWJ_USER_RE` (sap) | fullmatch | `^...$` | P05 §2 |
| SharePoint `Execution Time=(\d+(?:\.\d+)?)`, `Request \((GET|...):(\S+?)\)` | class only | `D`, `PNS`; Nd -> ASCII before `ParseFloat` | P01 §7 |
| `BAD_HOST` (live.py) | — | stays in Python | P06 |
| `csv.Sniffer` (tables) | backrefs, lookaround, possessive | not ported; side tables forwarded raw | P01 §11 |

---

## 6. Risks, mitigations, and what stays in Python

| # | Risk | Mitigation |
|---|---|---|
| 1 | Go `encoding/json` formatting differs (separators, `<>&`, U+2028, float, int vs float) | own encoder `py.Dumps`/`CanonLine`; `TestCanonRoundTrip` over all 240 goldens is WP1's first gate |
| 2 | Python Unicode `\d \w \s \b`, `re.I` ı/İ folding, `str.lower`/`strip`/`splitlines` | `py` helpers + fragments only; `Translate` panics on unsupported constructs; Unicode edge tables from P01/P03/P05 as unit tests |
| 3 | dateutil fallback (no full port verified) | WP2 ports `_timelex`/`_parse` for the shapes in the probe list; `Normalizer.Strict` in the receiver returns `ErrForward` for any string that reaches dateutil and is outside the verified grammar -> batch goes to Python |
| 4 | Process-global `_format_cache`/`_ts_cache` make results order-dependent | one shared mutex-guarded `Normalizer` per process; golden test replays generator order; production divergence (Go and Python have different histories) is limited to ambiguous d/m vs m/d strings -> shadow diff per format before enabling |
| 5 | Current year for year-less stamps | `Normalizer.Year` injected; tests pin 2026; regenerate goldens after New Year (documented in P06 §10) |
| 6 | Python exceptions fail the whole ingest | every such path returns `ErrPyRaise` -> forward raw, Python reproduces its own behaviour; listed exception paths are unit-tested (P02 §3.10, P04 §12, P05 availability) |
| 7 | Learned profiles change output / user profiles | Go reads `profiles.json` snapshot, applies `auto` mappings exactly, forwards when a `user` profile or non-empty `scenario.MAPPING` exists; Python keeps `remember` (debounced) |
| 8 | Hint cache drift between Go and Python (restart, errors) | `HintCache.Next` runs for every /ingest batch incl. forwarded; `sticky-raw` streams after errors; optional `hints.json` import on start; first-batch divergence after a Go restart documented and counted |
| 9 | NaN/Infinity attribute values | encoder error -> forward raw (`nan` reason) |
| 10 | Non-UTF-8 input, archives, BOM-less UTF-16 mojibake, cp437 zip names | `Decode` handles UTF-8 (+BOM) only; everything else `ErrForward` |
| 11 | Side tables, csv.Sniffer, DictReader AttributeError | `tables.is_side_table` emulated (`py.Lower` + 7 literals) -> forward raw before parsing |
| 12 | CSV `line_no`/`raw` mismatch, Go `encoding/csv` incompatibility | hand-written CPython `_csv` state machine (WP5) |
| 13 | Ordered dict semantics (report keys, auto_map, k=v message, raw JSON) | `py.Dict` everywhere records are built; no `map` iteration in parser code (lint: `go vet` + review rule) |
| 14 | Windows XML parser differences (expat vs `encoding/xml`) | minimal custom reader per P04 §6 or forward on any construct outside the tested subset (entities, CDATA, DTD) |
| 15 | Parse slowness or panic blocks ingestion | per-batch timeout + `recover` -> raw; HTTP path untouched; reorder buffer memory cap |
| 16 | Allow list drifting from reality | `allow.txt` embedded; `TestAllowConsistency` in CI; runtime env can only narrow |
| 17 | Golden coverage gaps (P01 §16, P02 §15, P03 §11, P04 per section, P05 §9) | unit tests from the probed Python outputs quoted in the specs; shadow mode on real traffic >= 24 h per format |
| 18 | Receiver truncates agent/file names by bytes (can split UTF-8) | existing bug, fix separately in WP7: truncate on rune boundary (same for v=1 and v=2) |
| 19 | `parser_confidence` rounded to 4 in v=2 objects | not read after `_accept`; documented |
| 20 | Parallel `ingest` path drops hint in Python | not ported; receiver batches are single files |

Stays in Python (never in Go):
* Everything after parsing in `LiveStore._ingest` (timestamp horizon, agent attr, `BAD_HOST`, host fallback, enricher,
  env/site/agent_id, `flow.dir`, `metric_of`, ring, rollups, `_persist`, `on_ingest`, JSONL spool) — reused via `_accept`.
* `profiles.remember` and all writes to `profiles.json`; user profile application.
* Side tables (`tables.read_table`, `csv.Sniffer`), archives, non-UTF-8 decoding, the upload (`Datasets`) path and the
  multi-file/process-pool `ingest`.
* User grok patterns (`grok.json`), `scenario.MAPPING` != {}.
* Any batch where Go returns an error, finds NaN/Inf, or whose format:family is not allow-listed.
