# Log explorer ("Loglar"): design

Status: design, not implemented. Owner of the page: `/logs` in the SİNYALLER group. The operations screen keeps a compact log
card that links here. Everything below was checked against the code at commit `1acbe0a` plus the uncommitted Go-parse work
(`live.py`, `spool_consumer.py`) and against a live scratch server under load.

The user's request: "Live log kısmı çalışmıyor doğru düzgün. Bu ekran görüntüsündeki gibi bir ekran ile live log incelemek,
görmek istiyorum. Detaylarına girebilmek istiyorum." The reference is a commercial log explorer: a query bar with a small query
language, Live Tail / Save / Alert, a facet sidebar, a result count with a per-level histogram, and a table whose rows expand.

Contents
1. Why the live log view does not work today (defects with evidence)
2. Query language: grammar, fields, parser plan, errors
3. Backend: modules and endpoints with JSON contracts
4. Storage: what the stored batches must keep, caps, cost
5. Performance plan for 100k+ lines/min
6. UI spec
7. Delivery plan and tests

Key decisions at a glance

| Topic | Decision |
|---|---|
| Line identity | every accepted event gets a monotonic `seq` (int64, persisted); `id` = seq. Paging cursor = `(ts, seq)` keyset, opaque |
| Ordering | search: newest first by `(ts, seq)`; live tail: arrival order (`seq`) so late or skewed stamps still show up |
| Live tail transport | cursor polling every 2 s (`after=<seq>`), server caps 500 lines per poll and 250 lines/s; not SSE (reasons in 3.6) |
| Exact counts | pure facet queries (level, service, host, env, pool, source) over the last 60 min come from a new per-minute "facet cube" in `Rollup`; everything else is a bounded scan, flagged `exact:false` with what was scanned |
| Storage | stored batches become v2: zlib-compressed JSONL with `seq`, `line_no`, scrubbed `raw`, pseudonymised `attrs`, `trace`; rows split so one row spans at most 10 min; net disk use falls about 5x despite the extra fields |
| Privacy | messages displayed through `privacy.scrub`; personal attribute values stored and shown only as pseudonyms; `raw` never shows a value that was pseudonymised in `attrs`; free-text terms that look like personal data or secrets are rejected |
| Team scope | every endpoint narrows to the viewer's team hosts first (facets, histogram, detail, context, tail, alerts evaluated with the rule owner's scope) |

---

## 1. Why "live log does not work properly" today

### 1.1 How it was reproduced

Scratch API + web on :8071, receiver on :8672, fed by `scripts/loadtest.py --rate 1700 --agents 300 --heavy 100`
(measured 125,000 lines/min, 300 hosts, 81 services in one minute, about 26 % ERROR+ because of the ERP corpora). Raw tab,
live refresh, paging, host / pool filters, older ranges and the grouped tab were exercised through the API with a probe script
and through the UI with Playwright; ground truth came from reading `live_batches` directly (read-only SQLite).

### 1.2 Defects

| # | Defect | Evidence | Root cause |
|---|---|---|---|
| D1 | **Paging loses most lines.** "Daha eski satırlar" skips lines and stops early. | Fixed range 08:40:00-08:41:00: the database held 32,763 ERROR+ lines (22,879 distinct). Paging at 200/page returned 1,400 lines in 7 pages, then `next: null`; 21,491 distinct lines were unreachable. Page 1 ended at 08:40:59.085, page 2 began at 08:40:58.996: every line in [59.000, 59.085), including about 500 second-precision syslog / access-log lines stamped exactly 08:40:59, was never shown. | `LiveStore.lines_between` reads batches by `id DESC` (arrival order) and stops after `2 x limit` matches ("batches arrive roughly in time order: a margin is enough"). With 300 agents that is false, so "newest 200" is the newest of a random subset. The cursor is the timestamp alone with a strict `<`, so ties at a cursor second are dropped. For `last=` ranges every page also recomputes "now", so the window drifts while paging. |
| D2 | **Results randomly empty.** | The same 1-minute query run 12 times returned 0 lines twice (`scanned: 0`). The paged run above ended on an empty page 7 (`scanned: 0`). | `_ring_covers(s)` compares `s` with `buf[0].timestamp`, the first *inserted* event, not the oldest one. Oracle EBS lines arrive with a 23-day-old stamp (2026-09-22, 183 such lines in 3,000 batches); whenever one sits at `buf[0]`, every range counts as "covered by the ring" and only the ring (about 30 s of data) is scanned. |
| D3 | **Search says "no logs" after looking at 3 % of the range.** | `q=zzzz-not-there`, last 15 min: 11.8 s, `scanned: 41,093` of about 1.5 M lines, 0 lines, `next: null`, so the UI shows "Bu aralık ve filtrede log yok". Quiet host `prd-batch-12`: 97 ERROR+ lines in 6 min; the first page shows 15 and the user must click "older" about 15 times. | `max_batches=4000` is about 25 s of batches at this load. The answer never says the scan was partial. |
| D4 | **Grouped tab (the default) cannot find ids or numbers.** | `GJ-44` (a voucher id): raw tab 22 lines, grouped tab "no logs" (API and UI). | `Rollup.groups` matches `q` against the masked template; numbers and ids are masked out. |
| D5 | **"Ham satırları göster" from a group usually finds nothing.** | 9 of the top 15 WARN+ groups jump to an empty raw list, e.g. `'SysOperationFramework task LedgerJournalPost'`. | The button takes the first three long words without digits and searches them as one contiguous phrase, but they are not adjacent in the message. |
| D6 | **Live refresh tears the list.** | After "older lines" and one 10 s refresh, row 200 is 08:46:11 and row 201 is 08:45:50: about 20 s of lines silently missing. At this volume the 200 newest lines all fall into one second (every row reads the same `HH:MM:SS`); a refresh every 10 s shows 0.1-1 s of a 10 s interval, so about 99 % of lines are never on screen. | The 10 s `refetchInterval` replaces page 1 but keeps the appended older pages; no cursor, no "new lines" marker. |
| D7 | **The expanded row jumps to another line.** | Expanded "08:45:41 prd-dwh-14 SysOperationFramework..." became "08:45:51 prd-sap-db-05 DMFEntityExport..." after the refresh. | `open` stores the row index, not a line id. |
| D8 | **Nothing to drill into.** | The expanded row only wraps the message. `/range/logs` returns 8 fields (ts, severity, host, service, env, message, source, agent). Stored lines keep `ts sev svc host env msg src agent kind` (+ `ses`): no attributes, line number, raw line or trace id. | `LiveStore._pack` drops them; once a line leaves the 50k ring (about 25-30 s at load) its details are gone. |
| D9 | **Lines from servers that log local time without an offset are invisible.** | 10 probe lines (syslog and log4j shapes) written in Türkiye local time were accepted and sit in the ring stamped 11:49Z while now = 08:49Z; last 15 min raw: 0, grouped: 0. The EBS lines with a 23-day-old stamp are likewise never in a live range. | Naive stamps are taken as UTC (`normalize.parse_timestamp`), so TR servers land 3 h in the future; `Rollup.add` drops minutes beyond now+2 and every range ends at now. There is no arrival time per line to fall back on. |
| D10 | **"Tümü" hides DEBUG; only minimum-severity filtering.** | Code: the severity buttons send ERROR / WARN / INFO as a minimum; `SEV_RANK["DEBUG"] = 0`. | No way to show DEBUG, only warnings, or to exclude a level. |
| D11 | **Service filter offers 8 of 81 services.** | `/range/summary` `services` has 8 entries; one minute of load had 81 services. Parser artefacts ("Error", "Critical", "Cause") appear as services. | The dropdown is fed from the summary's top 8. |
| D12 | **Different search semantics per tab, no field syntax.** | Raw: substring of message or host. Grouped: substring of template or service. No OR / NOT / field:value; clicking a host opens the host drawer instead of filtering. | Two ad-hoc filters. |
| D13 | **Files tab is wrong and slow; file tail is seconds deep.** | `/live/files?window=15`: 5.25 s; per-file totals sum to 200,000 while the range held more than 306,000 lines (the newest 200k only, labelled 15 min). The file tail modal (`/live/lines`) showed 116 lines spanning 24 s. | `snapshot(since=)` is capped by `live_window_max_events`; `file_lines` reads the ring only. |
| D14 | **Older ranges say "no logs" instead of "expired".** | 3 days ago, raw: `{"lines": [], "next": null, "scanned": 0}` (no `source`), shown as "Bu aralık ve filtrede log yok"; the grouped tab correctly says "history only". | `RangeView.logs` returns no source / retention info. |
| D15 | **Cannot zoom below a minute; seconds without milliseconds.** | `parse_range` widens any range to at least 1 minute; the time column is `HH:MM:SS`. | |
| D16 | **Slow and quadratic paging on the database.** | `EXPLAIN QUERY PLAN ... ORDER BY id DESC LIMIT 200 OFFSET n` is `SCAN live_batches`; each "older" page re-reads from the newest batch; 0.3-4 s per page under load. Grouped tab for `last=90` / `180`: 2.4-2.5 s (rollup rebuilt from a 1/2 sample; the cache key moves every minute). | Offset paging, no keyset, no batch pruning. |
| D17 | **There is no live tail.** | 10 s polling of the newest 200 lines; no cursor, no rate shown, no new-line counter; "Duraklat" only stops refreshing. | |

D1-D3, D6-D8 and D17 are what the user experiences as "live log does not work properly". The design below removes each of
them; the mapping is in section 7.

---

## 2. Query language

One query string is the single source of truth for every filter on the page: facet clicks, "filter by this value", the ops
card link and saved searches all edit or carry this string. The grammar follows the reference (Datadog/Lucene-like) so
`level:err OR level:crit OR level:fatal OR level:emerg OR level:alert` works as typed.

### 2.1 Grammar (EBNF)

```
query       = ws? [ or_expr ] ws? EOF
or_expr     = and_expr { ws OR ws and_expr }
and_expr    = unary { ws [ AND ws ] unary }               (* juxtaposition is AND *)
unary       = NOT ws unary | "-" unary_nows | primary
unary_nows  = primary                                     (* "-" must touch its operand: -host:x, -"foo", -(a OR b) *)
primary     = "(" ws? or_expr ws? ")" | term
term        = field_term | text_term
field_term  = FIELD ":" value_group
            | FIELD ":"? CMP NUMBER_OR_LEVEL              (* duration_ms>500, duration_ms:>=500, level>=warn *)
value_group = value | "(" ws? value { ws [ OR ws ] value } ws? ")"   (* service:(a OR b) or service:(a b): OR inside a group *)
value       = QUOTED | WORD
text_term   = QUOTED | WORD

FIELD       = ( letter | "_" | "@" ) { letter | digit | "_" | "." | "-" | "@" }   (* immediately followed by ":" or CMP *)
CMP         = ">=" | "<=" | ">" | "<"
OR          = "OR" | "||" | "VEYA"
AND         = "AND" | "&&" | "VE"
NOT         = "NOT" | "DEĞİL"
QUOTED      = '"' { any char except '"' and "\" | escape } '"'
WORD        = { any char except whitespace, '"', "(", ")" | escape }-   (* a word may contain "-", ".", "/", "*", "?" *)
escape      = "\" ( '"' | "\" | "(" | ")" | ":" | "*" | "?" | " " )
```

Rules that the EBNF does not show:

* Operators are recognised only in upper case and only as standalone tokens; `or`, `and`, `not` in lower case are text (a
  warning says so). Turkish aliases `VEYA`, `VE`, `DEĞİL` are accepted for the Turkish UI.
* Precedence: `NOT`/`-` binds tightest, then AND (explicit or implicit), then OR. `a b OR c` = `(a AND b) OR c`.
* `-` is a negation only at the start of a token and directly followed by a term or `(`; `prd-sap-01` is one word.
* `field:` with nothing after it is an error; `field:*` means "the field is present and not empty".
* An empty query matches everything.
* A WORD that contains `:` but whose prefix is not a valid FIELD (e.g. `http://x`) is text. A value containing `:` must be
  quoted (`@url:"http://x/y"`).

### 2.2 Value matching

| Construct | Meaning |
|---|---|
| `service:NavServer` | whole-value match, case-insensitive with Turkish folding (`ı İ I → i`, `ş→s`, `ğ→g`, `ü→u`, `ö→o`, `ç→c` on both sides) |
| `host:prd-sap-*`, `host:prd-??-01` | glob on the whole value: `*` any run, `?` one character; `*` inside quotes is literal |
| `"connection lost"` | phrase: case-insensitive folded substring of the message, spaces included |
| `connection lost` | two terms ANDed: the message contains "connection" and "lost" (in any order) |
| `time*out` | free-text wildcard: regex `time\S*out` (a wildcard never crosses whitespace) |
| `*` alone | match all |
| `duration_ms>500` | numeric comparison; a line whose value is missing or not numeric does not match |
| `level>=warn` | comparison on severity rank: DEBUG < INFO < WARN < ERROR < CRITICAL |

Folding makes `islem` find `İşlem` and `kapali` find `kapalı`; it is applied by compiling the term to a case-insensitive regex
with character classes (`[iıİI]`, `[sş]` ...) only when the term contains a foldable letter, so pure-ASCII searches stay plain
substring scans.

### 2.3 Fields

Built-in fields (aliases in brackets). "Cube" = answerable exactly from the per-minute facet counters (section 5).

| Field | Aliases | Value comes from | Match | Cube |
|---|---|---|---|---|
| `level` | `lvl`, `severity`, `sev` | `Observation.severity` | level aliases (2.4), comparisons | yes |
| `service` | `svc`, `app` | `service` (`-` when empty) | glob | yes |
| `host` | `hostname` | `host` | glob | yes |
| `env` | `environment` | `environment` or `unknown` | glob | yes (host's env) |
| `pool` | | `Rollup.pool_of(host)` (inventory cluster, else name stem) | glob | yes |
| `source` | `file` | `Observation.source` (the shipped file name) | glob | yes |
| `agent` | `sender` | `attributes.agent` | glob | no |
| `kind` | `type` | `log` / `event` / `alert` / `incident` | exact | no |
| `origin` | `component` | `Observation.origin` | glob | no |
| `trace` | `trace_id` | stored `trace` (2.6) | exact or glob | no |
| `session` | `ses` | pseudonymised session id `s_…` | exact; a value that is not `s_…` is pseudonymised first | no |
| `user` | | pseudonymised user `u_…` | exact; raw values pseudonymised first | no |
| `client` | `ip` | pseudonymised client address `c_…` | exact; raw values pseudonymised first | no |
| `message` | `msg` | message | as free text | no |
| `raw` | | stored raw line (scrubbed) | as free text | no |
| `line` | `line_no` | line number in the file | numeric comparisons | no |
| `group` | | template group key (12 hex chars of sha1(template, service)) | exact | via `Rollup.groups` |
| `duration_ms` | `latency_ms`, `elapsed_ms` | first duration attribute (`traces.DUR_KEYS`) else `<n> ms` in the message (same rule as p95) | numeric | no |
| `@key`, or `key` when not a built-in | | `attrs[key]` (stored, privacy-processed) | glob, or numeric with CMP | no |

An unknown bare field is searched as an attribute and returns a warning with the closest built-in names (Levenshtein ≤ 2)
when the key was not seen in the last hour (`/logs/fields`). `@key` always means the attribute even when a built-in has the
same name (`@host` is the host attribute, not the line's host).

### 2.4 Level aliases

| Typed | Means |
|---|---|
| `err`, `error`, `hata` | ERROR |
| `crit`, `critical`, `fatal`, `emerg`, `emergency`, `alert`, `panic`, `kritik` | CRITICAL |
| `warn`, `warning`, `uyari`, `uyarı` | WARN |
| `info`, `information`, `notice`, `ok`, `success`, `bilgi` | INFO |
| `debug`, `trace`, `verbose` | DEBUG |

So the reference query normalises to `level:(ERROR OR CRITICAL)`.

### 2.5 Examples

```
level:error service:nav*                              ERROR lines of every service starting with "nav"
level>=warn -host:test-* "connection lost"            WARN+ outside the test hosts whose message contains the phrase
pool:prd-sap-app (timeout OR deadlock)                SAP app pool lines with either word
@status>=500 source:access.log                        HTTP 5xx from access logs
duration_ms>2000 service:http                         slow requests
session:s_c4a37ea736be305e                            one client session (the token shown everywhere else)
level:(err OR crit) NOT service:(Error OR Critical)   errors, without the parser-artefact services
```

### 2.6 Parser plan (`src/watchover/logquery.py`, standard library only)

1. `tokenize(text) -> list[Tok]`: kinds `LP RP QUOTED WORD FIELD COLON CMP OR AND NOT NEG WS EOF`, each with `pos` and `len`
   (character offsets for underlining). FIELD is recognised by lookahead (`name` immediately followed by `:` or a CMP).
2. `parse(text) -> Node`, recursive descent following the EBNF. Nodes (dataclasses): `Or(items)`, `And(items)`,
   `Not(item)`, `Field(name, op, values, pos)` with `op` in `eq | glob | cmp_gt | cmp_ge | cmp_lt | cmp_le | present`,
   `Text(value, phrase, glob, pos)`, `All()`.
3. `resolve(node, ctx)`: aliases to canonical field names, level aliases to severities, attribute keys, privacy fields
   (`session` / `user` / `client` raw values pseudonymised with `privacy.pseudonym(v, prefix)`), PII check (2.7).
4. `normalize(node) -> str`: canonical text (`level:(ERROR OR CRITICAL) service:nav*`), used for cache keys, cursor hashes
   and saved searches.
5. `plan(node) -> Plan`: `facet_only` (every leaf is on level / service / host / env / pool / source), `fields_used`,
   `top_and` (per-field positive and negative value sets of the top-level conjunction, for facet "selected" marks and
   rewrites), `sev_mask` (severities a match can have, for batch pruning), `hosts` (host set when the query pins hosts),
   `prefilter` (a regex every matching line's stored JSON text must contain, derived from ANDed text terms; `None` when an OR
   branch has no text), `leaves`, `depth`.
6. `compile(node, ctx) -> Callable[[LineView], bool]`: closures over a `LineView` adapter that reads either an `Observation`
   (ring) or a decoded stored dict, so the ring and the database share one predicate.
7. `compile_key(node, ctx) -> Callable[[sev, service, source, host], bool]` for `facet_only` plans; evaluated once per
   distinct cube key per request (memoised).
8. `rewrite(text, op, field, value) -> str` (section 3.10): splices the top-level conjunction by token positions so the
   user's own text stays as typed; falls back to normalized text when the top level is not a conjunction.

Limits: 2,000 characters, 64 leaves, depth 16; a glob on a cube field that expands to more than 500 known values is turned into
a scan predicate (not an error).

### 2.7 Errors and warnings

Errors are HTTP 400 with `{"detail": {"code", "pos", "len", "tr", "en"}}`; the UI underlines `[pos, pos+len)` in the query
bar and shows the message in the current language.

| code | tr | en |
|---|---|---|
| `unclosed_quote` | `Kapanmamış tırnak (sütun {pos}).` | `Unclosed quote (column {pos}).` |
| `unclosed_paren` | `Kapanmamış parantez: ")" eksik.` | `Missing ")".` |
| `stray_paren` | `Fazladan ")" (sütun {pos}).` | `Unexpected ")" (column {pos}).` |
| `empty_group` | `Boş parantez.` | `Empty parentheses.` |
| `missing_value` | `"{field}:" için değer eksik.` | `"{field}:" needs a value.` |
| `dangling_op` | `"{op}" sonrasında bir ifade bekleniyordu.` | `Expected a term after "{op}".` |
| `leading_op` | `"{op}" sorgunun ya da parantezin başında olamaz.` | `"{op}" cannot start a query or group.` |
| `unknown_level` | `Bilinmeyen seviye "{v}". Geçerli: error (err), warn (warning), info (ok), debug, critical (crit, fatal, emerg, alert).` | `Unknown level "{v}". Valid: error (err), warn (warning), info (ok), debug, critical (crit, fatal, emerg, alert).` |
| `bad_number` | `"{field}{op}" bir sayı bekliyor; "{v}" sayı değil.` | `"{field}{op}" expects a number; "{v}" is not one.` |
| `cmp_not_numeric` | `"{op}" yalnız sayısal alanlarla kullanılır (ör. duration_ms>500, level>=warn); "{field}" sayısal değil.` | `"{op}" only works on numeric fields (e.g. duration_ms>500, level>=warn); "{field}" is not numeric.` |
| `group_not` | `Değer grubunun içinde NOT kullanılamaz; -{field}:değer yazın.` | `NOT is not allowed inside a value group; write -{field}:value.` |
| `bad_escape` | `Geçersiz kaçış "\{c}".` | `Invalid escape "\{c}".` |
| `too_long` | `Sorgu çok uzun (en fazla 2000 karakter).` | `Query too long (max 2000 characters).` |
| `too_complex` | `Sorgu çok karmaşık (en fazla 64 koşul, 16 parantez derinliği).` | `Query too complex (max 64 conditions, depth 16).` |
| `pii_term` | `Kişisel veri ya da gizli bilgi (e-posta, TCKN, kart, IBAN, telefon, parola/token) ile arama yapılamaz; oturum ve kullanıcı için takma adları kullanın (session:s_…, user:u_…).` | `Searching for personal data or secrets (e-mail, national id, card, IBAN, phone, password/token) is not allowed; use the pseudonyms (session:s_…, user:u_…).` |

`pii_term`: a free-text or attribute value that `privacy.scrub` would change (EMAIL, IBAN, PHONE, TCKN/Luhn digits, BEARER,
SECRET `key=value`) is refused. Messages are shown scrubbed (4.3); without this rule a search would confirm a masked token or
e-mail and become an oracle.

Warnings (200 with `query.warnings[]`, shown as a dim line under the bar):

| code | tr |
|---|---|
| `unknown_field` | `"{f}" bilinen bir alan değil; öznitelik olarak arandı. "{s}" mi demek istediniz?` |
| `lowercase_op` | `"or" metin olarak arandı; mantıksal VEYA için "OR" yazın.` |
| `history_unsupported` | `Bu aralıkta yalnız sunucu, ortam, pool ve seviye (hata/diğer) filtrelenebilir; "{leaf}" uygulanmadı.` |
| `scan_needed` | `Serbest metin ya da öznitelik araması tarama gerektirir; sayılar tahmini olabilir.` |

---

## 3. Backend

### 3.1 Modules

| File | Content |
|---|---|
| `src/watchover/logquery.py` (new) | tokenizer, parser, AST, normalize, plan, compile, compile_key, rewrite, `QueryError` |
| `src/watchover/logstore.py` (new) | batch codec v2 (`pack`, `unpack`, privacy transform), `persist(db, events, agent, env)` with row splitting and compression, `migrate(db)`, `scan_desc(...)` keyset iterator over rows, `find(seq)`, `context(...)`, v1 row compatibility |
| `src/watchover/logsearch.py` (new) | `LogSearch(live, ranges, history, traces)`: `search`, `histogram`, `facets`, `groups`, `tail`, `line`, `context`, `fields`; scan budgets, scan semaphore, response cache |
| `src/watchover/logwatch.py` (new) | streaming per-rule matchers for alert-from-query; `add_events(events)`; `count(rule, now)` |
| `src/watchover/api/routers/logs.py` (new) | `/api/logs/*` endpoints below |
| `src/watchover/rollup.py` | facet cube `fc` (5.2), service and source id tables, `cube_counts`, `cube_hist`, attribute-key sample |
| `src/watchover/models.py` | `Observation.seq: int = 0` (new last field, default keeps every constructor call valid) |
| `src/watchover/live.py` (shared, Edit-only small hunks) | `self.seq` counter assigned under the lock in `_accept`; `_persist` / `_pack` delegate to `logstore`; `attach_db` calls `logstore.migrate` and restores `self.seq`; `_ring_covers` uses arrival markers (D2 fix, 3.8); arrival time kept per batch |
| `src/watchover/api/services.py` (shared, Edit-only small hunks) | `self.logs = LogSearch(...)`, `self.logwatch = LogWatch(...)`, one line in `_on_ingest`, `self.alerts.logwatch = self.logwatch` |
| `src/watchover/api/__init__.py` | add `logs` to the router tuple |
| `src/watchover/notify.py` | condition `query`, columns `query`, `window_min`, `op`, `owner`; AlertEngine branch |
| `src/watchover/rbac.py` | permissions `page.logs` (Loglar sayfası / Log explorer) and `act.logalert` (Log sorgusundan uyarı kurma / Create alerts from a log query); both in `NEW_PERMS`; operator gets both, viewer gets `page.logs` |

### 3.2 Common request parameters and response fields

* Range: `last` (minutes, 1..576000) or `start` + `end` (ISO 8601, any offset). No 1-minute minimum: the explorer accepts
  ranges down to 1 second (its own `parse_range_s`; the ops screen keeps `timerange.parse_range`). On the first page the
  server pins `end`; every response returns `range: {"start", "end", "live"}` as absolute UTC ISO strings and the client sends
  those on later pages, so paging never drifts (D1).
* `q`: the query (2.1). No separate `env` / `pool` / `host` / `service` / `severity` parameters: the ops card turns its scope
  into query text.
* Team scope: `sc = user_scope(...)`. When not `None`, the allowed host set is ANDed into every predicate and cube walk;
  facet values, group hosts and context lines from other hosts never appear; `line` / `context` for a line on another team's
  host answer 404 (not 403, so existence is not confirmed). Permission: `page.logs` on every endpoint.
* Every answer carries `source` (`ring` | `stored` | `counters` | `history` | `none`), `exact` (bool), `took_ms`, and
  `retention: {"hours": 48, "oldest": "<oldest stored line>"}`.

Line object (used by search, tail, context):

```json
{
  "id": "184467",
  "ts": "2026-10-15T08:44:59.995+00:00",
  "level": "ERROR",
  "service": "NavServer",
  "host": "prd-ebs-12",
  "env": "prod",
  "pool": "prd-ebs",
  "source": "nav_bc.log",
  "message": "SQL Server connection lost: A transport-level error has occurred ...",
  "truncated": false,
  "trace": null,
  "session": "s_c4a37ea736be305e",
  "skew_s": null,
  "cols": {"@status": "500"}
}
```

`message` is `privacy.scrub`bed and cut at 2,000 characters (`truncated: true`; the detail has up to 16 KB). `skew_s` is
arrival minus line time in seconds when it exceeds 120 s (late, stale or future stamps; D9), else `null`. `cols` holds only
the extra columns the request asked for with `cols=agent,@status,@duration_ms`.

### 3.3 `GET /api/logs/search`

Params: `q`, range, `limit` (1..500, default 100), `cursor` (opaque, from `next`), `cols`.

```json
{
  "range": {"start": "2026-10-15T08:30:00+00:00", "end": "2026-10-15T08:45:00+00:00", "live": true},
  "query": {"text": "level:error service:nav*", "normalized": "level:ERROR service:nav*", "facet_only": true, "warnings": []},
  "lines": [ { "...line object..." : "" } ],
  "next": "eyJ2IjoxLCJ0IjoiMjAyNi0xMC0xNVQwODo0NDo1OS45OTUrMDA6MDAiLCJzIjoxODQ0NjcsImgiOiI5ZjJhIn0",
  "total": {"value": 2863, "exact": true, "source": "counters"},
  "scan": {"lines": 41032, "rows": 3980, "ms": 812, "complete": false, "frontier": "2026-10-15T08:44:31+00:00", "stopped_by": "limit"},
  "source": "stored",
  "exact": true,
  "retention": {"hours": 48, "oldest": "2026-10-13T08:45:12+00:00"},
  "took_ms": 845
}
```

* Order: `(ts DESC, seq DESC)`. `next` encodes `{"v":1,"t":<ts of last line>,"s":<seq of last line>,"h":<hash of normalized q + scope + range>}`
  (base64url JSON). The next page returns lines with `(ts, seq) < (t, s)`: every line exactly once, ties included (D1). A
  cursor whose hash does not match the request is `400 {"code": "cursor_mismatch"}`.
* `total`:
  * `{"value": n, "exact": true, "source": "counters"}` for facet-only queries inside the cube window (5.1);
  * `{"value": n, "exact": true, "source": "scan"}` when the scan covered the whole range;
  * `{"value": n, "exact": false, "estimated": true, "basis": {"scanned": 400000, "matched": 3307, "range_lines": 1500000}}`
    when the scan stopped early and the facet part of the query has an exact line count (from the cube) to scale by;
  * `{"value": n, "exact": false, "at_least": true}` otherwise.
* `scan.stopped_by`: `limit` (page full and final), `budget_time`, `budget_lines`, `range_start` (nothing older), `null`.
  When `complete: false` and the page is not full, `next` is still returned: the client offers "Daha geriye tara" (D3).
* Range older than the stored window: `lines: []`, `source: "history"`, `next: null`; the UI shows the retention message
  (D14).

### 3.4 `GET /api/logs/histogram`

Params: `q`, range, `by` (`level` default | `service` | `host` | `source` | `pool`), `top` (for `by` other than level, 1..10, default 5).

Step: `step_s = 60 * timerange.step_minutes(minutes)` for ranges of 15 minutes and longer (the operations timeline's rule:
about 90 bars, at most 126). Shorter ranges use the same rule in seconds,
`step_seconds(sec) = first s in (1, 2, 5, 10, 15, 30, 60) with sec / s <= 126`; sub-minute steps always come from a scan
(the counters are per minute).

```json
{
  "range": {"start": "...", "end": "...", "live": true},
  "step_s": 60,
  "by": "level",
  "keys": ["CRITICAL", "ERROR", "WARN", "INFO", "DEBUG"],
  "buckets": [
    {"t": 1760517000, "total": 1193, "v": {"CRITICAL": 3, "ERROR": 240, "WARN": 50, "INFO": 900, "DEBUG": 0}, "scanned": true}
  ],
  "exact": true,
  "source": "counters",
  "sample": 1,
  "took_ms": 42
}
```

* With `by=service|host|source|pool`: `keys` = the top N values over the range plus `"_other"`.
* Scan-based answers mark buckets beyond the scan frontier `"scanned": false` (drawn hatched, "taranmadı").
* `source: "history"` (older than the stored window): `keys: ["ERROR+", "OTHER"]` from `slo_minute`; only `host`, `env`,
  `pool` and `level>=error` leaves apply, the others are listed in `"unsupported": ["service:nav*"]` (warning
  `history_unsupported`) and the buckets are empty when anything is unsupported, never silently wrong.
* `sample: k` when the answer comes from a 1/k batch sample (stored ranges without cube coverage); counts are scaled and
  `exact: false`.

### 3.5 `GET /api/logs/facets`

Params: `q`, range, `fields` (default `level,service,host,env,pool,source`), `limit` (per field, 1..200, default 12),
`search` (`field:text`, filters that field's values by folded substring; when present only that field is returned).

```json
{
  "range": {"start": "...", "end": "...", "live": true},
  "mode": "multi",
  "facets": {
    "level":   {"values": [{"v": "ERROR", "n": 33328}, {"v": "INFO", "n": 32577}], "distinct": 4, "other": 0, "selected": ["ERROR"], "excluded": []},
    "service": {"values": [{"v": "http", "n": 16405}, {"v": "NavServer", "n": 6740}], "distinct": 81, "other": 12000, "selected": [], "excluded": ["Error"]},
    "host":    {"values": [], "distinct": 0, "other": 0, "selected": [], "excluded": []}
  },
  "exact": true,
  "source": "counters",
  "sample": 1,
  "took_ms": 120
}
```

* `mode: "multi"` (the top level of the query is a conjunction): each field's counts are computed with that field's own
  clauses removed, so the other values of a field stay visible and can be added (the reference's multi-select behaviour).
  `selected` / `excluded` come from `plan.top_and`.
* `mode: "filtered"` (top-level OR across fields): counts use the full query; `selected` / `excluded` are empty.
* Non-facet leaves (text, attributes): counts come from the scanned matching lines, `exact: false`, plus `scan`.
* `search` with no hits: `values: []`, the UI shows "Seçenek yok" ("No options found").

### 3.6 `GET /api/logs/tail` (live tail)

Params: `q`, `after` (seq; omitted on the first call), `limit` (1..500, default 200), `cols`. Header `X-Tab-Id` (random per
browser tab) for the rate limiter.

```json
{
  "lines": [ { "...line object, arrival order, newest first..." : "" } ],
  "after": 191230,
  "matched": 812,
  "returned": 200,
  "dropped": 612,
  "skipped": 0,
  "rate": {"in_per_s": 1742.5, "match_per_s": 38.1},
  "cap_per_s": 250,
  "poll_ms": 2000,
  "query": {"normalized": "level>=WARN", "warnings": []},
  "source": "ring",
  "took_ms": 6
}
```

* First call: the newest `limit` matching lines in the ring and `after` = newest seq.
* Next calls: all events with `seq > after` are tested (arrival order, so a line with a skewed stamp still appears, with
  `skew_s`); at most `min(limit, 500, cap_per_s x seconds since the previous poll)` newest matches are returned, the rest are
  counted in `dropped`.
* `skipped`: events that left the ring before this poll read them (the client was paused longer than the ring holds); the
  cursor jumps to the newest and the UI says so.
* Faster than one poll per second per (user, `X-Tab-Id`): `429 {"detail": {"code": "too_fast", "retry_ms": 600}}`.

Why polling and not SSE:
1. Authentication is a Bearer header. `EventSource` cannot send headers, so the token would have to go into the URL and from
   there into proxy and access logs.
2. Sync FastAPI handlers run in a thread pool (40 threads by default). One SSE connection per viewer holds a thread for its
   lifetime; a NOC wall with 30 screens would starve every other endpoint. An async generator would have to take the
   `LiveStore` thread lock from the event loop.
3. Corporate and reverse proxies in front of the app (nginx, IIS ARR, F5) buffer `text/event-stream` and cut idle
   connections; polling needs no proxy tuning.
4. At 100k+ lines/min nobody reads more than a few dozen lines a second; a 2 s poll capped at 500 lines loses nothing the
   screen could show, and the server-side cap is simpler to enforce per request.
5. Polling composes with react-query, pause, and tab-hidden throttling (`refetchIntervalInBackground: false`).

Cost: one small request every 2 s per viewer; the work is O(new events since the cursor), about 4,000 events per poll at
2,000 lines/s, measured below 10 ms. Revisit (fetch-streamed SSE with the token in a header) above about 100 concurrent tail
viewers.

### 3.7 `GET /api/logs/line/{id}` (detail)

```json
{
  "id": "184467",
  "ts": "2026-10-15T08:44:59.995+00:00",
  "received": "2026-10-15T08:45:00.412+00:00",
  "skew_s": null,
  "level": "ERROR",
  "service": "NavServer",
  "host": "prd-ebs-12",
  "env": "prod",
  "pool": "prd-ebs",
  "source": "nav_bc.log",
  "line_no": 120344,
  "agent": "prd-ebs-12",
  "kind": "log",
  "origin": "",
  "parser": "grok:nav",
  "message": "SQL Server connection lost: ... (scrubbed, up to 16 KB)",
  "message_truncated": false,
  "raw": "2026-10-15 08:44:59.995 ERROR [NavServer] SQL Server connection lost: ...",
  "raw_state": "stored",
  "attrs": {"pid": 4412, "thread": "worker-7", "status": "500", "user": "u_1f0c6a9e2b7d4c11"},
  "attrs_dropped": 0,
  "trace": {"id": "4bf92f3577b34da6a3ce929d0e0e4736", "stored": true, "link": "/traces/4bf92f3577b34da6a3ce929d0e0e4736"},
  "session": {"id": "s_c4a37ea736be305e", "kind": "client", "user": "", "client": "c_dad9de4a5cfef9a3", "rum": null, "link": "/traces?session=s_c4a37ea736be305e"},
  "host_link": {"ops": "/ops?host=prd-ebs-12&start=2026-10-15T08:39:59Z&end=2026-10-15T08:49:59Z"},
  "group": {"key": "a1b2c3d4e5f6", "template": "sql server connection lost: a transport-level error has occurred ...", "count_1h": 6740},
  "source_of_detail": "ring"
}
```

* Lookup: the ring when the seq is still there (index `seq - buf[0].seq`, verified, linear fallback after a purge), else the
  stored row whose `seq_min <= id <= seq_max` (index on `seq_min`). The ring copy goes through the same `logstore.pack`
  privacy transform, so the answer is identical either way.
* `raw_state`: `stored` | `same` (raw equals the message, not stored twice) | `absent` (v1 row or raw cap exceeded).
* `trace.stored`: `SELECT 1 FROM spans WHERE trace_id=? LIMIT 1` (the span store samples traces; the UI says when the trace
  was not kept).
* `session.link`: `/traces?session=` (the trace store already maps an `s_` token to raw ids, `traces.SES_TOKEN`); for
  `kind: "rum"` the link is `/rum/sessions/<rum id>`.
* 404 when the id is unknown, expired, or on a host outside the viewer's team.

### 3.8 `GET /api/logs/context/{id}`

Params: `before` (0..200, default 20), `after` (0..200, default 20), `q` (optional, only to mark matches).

```json
{
  "anchor": "184467",
  "host": "prd-ebs-12",
  "source": "nav_bc.log",
  "before": [ { "...line object, oldest first...": "", "match": false } ],
  "after":  [ { "...line object...": "", "match": true } ],
  "more_before": true,
  "more_after": false,
  "window_s": 1800
}
```

Same host and source, ordered by `(ts, line_no, seq)`, within ±30 minutes of the anchor. Rows are found with the new index
`(agent, ts_max)` (one agent ships one host's files), so the cost is one agent's rows over an hour, not the whole store.

### 3.9 `GET /api/logs/groups` (group similar)

Params: `q`, range, `limit` (1..500, default 100).

```json
{
  "range": {"start": "...", "end": "...", "live": true},
  "groups": [
    {"key": "a1b2c3d4e5f6", "template": "sql server connection lost: a transport-level error has occurred ...", "service": "NavServer",
     "level": "ERROR", "count": 6740, "hosts": 52, "first": "...", "last": "...", "trend": [12, 15, 9],
     "sample_id": "184467", "sample": "SQL Server connection lost: ...", "new": false}
  ],
  "distinct": 305,
  "total": 207736,
  "exact": true,
  "source": "counters",
  "sample": 1
}
```

* Counters path (`Rollup.groups`, exact) when the query only uses level, service, host, env, pool inside the last hour.
* Otherwise `template_of` over the matched lines of a bounded scan (at most 200,000 matched lines, `exact: false`, `scan`).
* Clicking a group adds `group:<key>` to the query (the key is stable: sha1 of template and service), which fixes D4/D5:
  the raw lines of a group are found by the group itself, not by guessed words.

### 3.10 Query helpers

* `GET /api/logs/parse?q=` → `{"ok": true, "normalized": "...", "facet_only": true, "leaves": 3, "warnings": []}` or the
  400 error object. Called by the query bar while typing (debounced 300 ms).
* `GET /api/logs/rewrite?q=&op=add|remove|only|exclude&field=service&value=NavServer` → `{"q": "level:error service:(NavServer OR http)", "normalized": "..."}`.
  `add`: put the value into the field's positive clause of the top-level conjunction (creating `field:(a OR b)`), removing a
  matching exclusion. `exclude`: add `-field:value` and drop the value from the positive clause. `only`: replace the field's
  clauses with `field:value`. `remove`: delete the value from either clause. When the top level is not a conjunction the
  result is `(<old>) field:value`. One implementation (Python) for typing and clicking, so the two never disagree.
* `GET /api/logs/fields` → autocomplete:

```json
{
  "fields": [
    {"name": "level", "kind": "facet", "values": ["error", "warn", "info", "debug", "critical"]},
    {"name": "service", "kind": "facet"},
    {"name": "duration_ms", "kind": "number"},
    {"name": "@status", "kind": "attr", "type": "number", "seen": 16405},
    {"name": "@thread", "kind": "attr", "type": "string", "seen": 5265}
  ],
  "operators": ["AND", "OR", "NOT", "-"]
}
```

Attribute keys come from a sample kept by the rollup (every 50th line, keys of the last hour, at most 2,000, team-scoped
by the hosts that sent them).

### 3.11 Saved searches (per user)

Table (created by `LogSearch.__init__`):

```sql
CREATE TABLE IF NOT EXISTS log_searches (id {pk}, owner TEXT NOT NULL, name TEXT NOT NULL, query TEXT NOT NULL,
  range TEXT DEFAULT '', columns TEXT DEFAULT '[]', group_similar INTEGER DEFAULT 0, shared INTEGER DEFAULT 0,
  created_at TEXT, updated_at TEXT, UNIQUE (owner, name))
```

| Method | Path | Body / answer |
|---|---|---|
| GET | `/api/logs/saved` | `{"items": [{"id": 3, "name": "NAV SQL hataları", "query": "service:nav* \"sql server\"", "range": "last=60", "columns": ["time","level","service","host","message"], "group_similar": false, "shared": false, "owner": "Ayşe Y.", "mine": true, "updated_at": "..."}]}`: own searches and shared ones |
| POST | `/api/logs/saved` | `{"name", "query", "range": "last=60" or "start=…&end=…" or "", "columns": [...], "group_similar": false, "shared": false}` → `{"id": 3}`; 409 duplicate name; 400 invalid query |
| PUT | `/api/logs/saved/{id}` | any subset of the POST fields → `{"ok": true}`; 403 unless owner or administrator |
| DELETE | `/api/logs/saved/{id}` | `{"ok": true}`; 403 unless owner or administrator |

`owner` is stored as the account e-mail and shown as the display name. Sharing shares the query, never data: a shared search
runs under each viewer's own team scope.

### 3.12 Alert from a query

Notify gets a seventh condition, `query`. Migration (guarded by `db.columns("notify_rules")`):
`ALTER TABLE notify_rules ADD COLUMN query TEXT DEFAULT ''`, `window_min INTEGER DEFAULT 5`, `op TEXT DEFAULT '>='`,
`owner TEXT DEFAULT ''`; `_RULE_COLS` and `add_rule` take them. Condition: "matching lines in the last `window_min` minutes
`op` `threshold`", `op` in `>=` (burst) or `<` (absence: "no `backup completed` line in 60 minutes").

| Method | Path | Body / answer |
|---|---|---|
| POST | `/api/logs/alerts` (needs `act.logalert`) | `{"name", "query", "window_min": 5, "op": ">=", "threshold": 10, "severity": "high", "channels": "email,webhook", "targets": "ops, @dba", "cooldown_min": 30}` → `{"id": 17, "mode": "counters"}` (or `"stream"`); 400 invalid query or window outside 1..60; 409 `too_many_stream_rules` |
| GET | `/api/logs/alerts/preview?q=&window_min=5&op=>=&threshold=10&last=1440` | `{"would_fire": 3, "first": "...", "windows": [{"t": ..., "n": 14}], "exact": true}` from the histogram buckets |

Evaluation (`AlertEngine.evaluate`, every 60 s, through the existing `dispatch`, cooldown and alert log):

* `mode: counters` (facet-only query): count from the facet cube for the last `window_min` minutes under the owner's team
  scope; exact, about 2 ms.
* `mode: stream` (anything else): `LogWatch` holds the rule's compiled predicate and the owner's allowed hosts (refreshed every
  60 s); `services._on_ingest` feeds every accepted batch to `logwatch.add_events`, which increments a per-rule per-minute
  counter and keeps the last 3 matching seqs. Exact from the moment the rule exists. At most 20 stream rules; on creation the
  predicate is timed on 5,000 ring lines and refused above 5 µs per line (`lg_alert_heavy`). An absence rule (`<`) does not
  fire until the stream has run for `window_min` minutes (no false alarm after a restart).
* Alert key `query:<rule id>`; title `"{name}: {n} satır / {window} dk"`; body: the normalized query, count vs threshold,
  top 3 hosts, 3 sample lines (scrubbed, owner scope) and the link `/logs?q=…&start=…&end=…`.
* Owner removed or without `page.logs`: the rule is skipped and the alert log records `owner_missing` once per day.

### 3.13 Export (optional)

`GET /api/logs/export?q=&range&format=jsonl|csv` (needs `act.export`): up to 50,000 lines streamed newest first, scrubbed,
same scope rules; `X-Truncated: 1` when the cap is hit.

---

## 4. Storage

### 4.1 Line identity and arrival

* `LiveStore._accept` assigns `o.seq = self.seq + i + 1` to the accepted events and advances `self.seq` in the same locked
  block that extends the ring. Seqs are contiguous per batch and monotonic per process.
* `attach_db` restores `self.seq = MAX(seq_max)` from `live_batches`, so ids survive a restart. Without a database the ids
  are valid for the process lifetime only.
* A small deque of `(first seq, arrival epoch)` per accepted batch gives every line its arrival time (`received`, `skew_s`)
  and the ring's oldest arrival.
* `_ring_covers(s)` becomes "the ring's oldest arrival ≤ s - 60 s": the ring then holds every event that arrived since the
  range began, whatever its stamp (D2). Stale or future stamps no longer flip the source.

### 4.2 Stored line v2 (packed JSON object, one per line)

| Key | Content | Cap |
|---|---|---|
| `ts sev svc host env msg src agent kind org site aid ses` | as today (`_pack`) | `msg` 16 KB (`mt: 1` when cut) |
| `q` | seq | |
| `rt` | arrival epoch seconds, only when it differs from `ts` by more than 120 s | |
| `ln` | `line_no`, only when > 0 | |
| `raw` | the original line after the privacy transform, only when it differs from `msg` after whitespace normalisation | 2 KB, else dropped (`raw_state: absent`) |
| `at` | attributes after the privacy transform, without `agent`, `site`, `agent_id`, `_no_ts`, `_ses`, `discovery_*` | 32 keys, key 64 chars, string value 256 chars, 2 KB total; the rest counted in `atx` |
| `tr` | trace id: `traces.ID_KEYS` trace / traceparent / request attribute, else `TRACEPARENT` or `MSG_IDS` in the message | 64 chars |

### 4.3 Privacy transform (`logstore.pack`, follows `privacy.py`)

* Attribute keys whose `rollup.SES_ROLES` role is session (`s`, `S`), user (`u`, `R`) or client address (`c`, `x`, and `a`
  when the value is an address) are stored as their pseudonyms (`s_…`, `u_…`, `c_…`, same `privacy.pseudonym` key as the
  rollup, so the tokens match the sessions view). The user sensors' random session token (`r`) is kept, as the rollup does.
* Keys matching the `privacy.SECRET` names (password, passwd, pwd, parola, şifre, token, secret, api_key, authorization) and
  `cookie`, `set-cookie`, `jwt` keep the key with value `"***"`.
* Every other string value goes through `privacy.scrub`; numbers and booleans are kept as they are.
* `raw`: every original value that was pseudonymised in `at` (length ≥ 3) is replaced in the raw text by its pseudonym, then
  `privacy.scrub` runs. So the raw line never shows a client address or user name that the attributes hide (an access-log raw
  line carries the client IP; the message does not).
* `msg` is stored as today (other readers of the stored batches, such as the anomaly window and the learner, are not
  changed). The explorer displays `scrub(msg)`; the `pii_term` rule (2.7) keeps search from becoming an oracle for masked
  values.
* No new raw identifier is exposed: attributes and raw lines are new on screen, and both carry only pseudonyms or scrubbed text.

### 4.4 Rows

New columns on `live_batches` (added by `logstore.migrate`, existing rows keep `v = 1`):

```sql
ALTER TABLE live_batches ADD COLUMN v INTEGER DEFAULT 1;          -- 2: compressed v2 payload in pz
ALTER TABLE live_batches ADD COLUMN pz {db.blob};                 -- zlib level 1 of the JSONL (BYTEA on PostgreSQL)
ALTER TABLE live_batches ADD COLUMN seq_min INTEGER DEFAULT 0;
ALTER TABLE live_batches ADD COLUMN seq_max INTEGER DEFAULT 0;
ALTER TABLE live_batches ADD COLUMN sevmask INTEGER DEFAULT 31;   -- bit per severity present (DEBUG=1 .. CRITICAL=16)
ALTER TABLE live_batches ADD COLUMN host TEXT DEFAULT '';         -- the single host of the row, '' when mixed
ALTER TABLE live_batches ADD COLUMN src TEXT DEFAULT '';          -- the single source of the row, '' when mixed
CREATE INDEX IF NOT EXISTS live_batches_seq ON live_batches(seq_min);
CREATE INDEX IF NOT EXISTS live_batches_host ON live_batches(host, ts_max);
CREATE INDEX IF NOT EXISTS live_batches_agent ON live_batches(agent, ts_max);
```

* `payload` stays `''` for v2 rows; `n`, `ts_min`, `ts_max`, `received_at`, `agent`, `env` are filled as today, so
  `events_between`, `_window_from_db`, `prune` and `db_stats` keep working after they learn to read `pz` (one shared
  `logstore.iter_rows` decoder). `db_stats.bytes` adds `LENGTH(pz)`.
* Row splitting: the events of one POST are grouped by `floor(ts / 600 s)`; each group is one row. Invariant: a v2 row spans
  less than 10 minutes. The keyset scan can then bound its index range (`ts_max < T + 600 s`). Most POSTs stay one row; a
  POST holding a 23-day-old line becomes two rows (D2's EBS case).
* Write cost: zlib level 1 compresses about 128,000 lines/s on one core (measured on the load-test batches), about 1.6 % of
  a core at 2,000 lines/s.

### 4.5 Measured size and cost

From 3,000 stored batches of the load test (31,194 lines):

| Format | Bytes per line |
|---|---|
| v1 today, plain JSONL | 281 |
| v1 content, zlib level 1 per row | 41 (6.8x smaller) |
| v2 with raw + line number + 3 attributes, plain | 499 |
| v2 with raw + line number + 3 attributes, zlib level 1 | 54 |

The load-test corpus repeats more than real logs; with a conservative 4x ratio v2 costs about 125 B/line. At 125,000
lines/min and 48 h retention: today about 50 GB/day (the `db_growth` warning fires), v2 about 22 GB/day, i.e. smaller
than today with more detail. Read side: decompression runs at about 950,000 lines/s; JSON decoding (about 240,000 lines/s)
remains the main cost, which is why the scan prefilters on the decompressed text before decoding (5.3).

Settings (`settings.py` defaults, System page): `logs_raw` (true), `logs_attr_max_bytes` (2048), `logs_msg_max_bytes`
(16384). `live_retention_h` stays the retention for lines.

### 4.6 Persisted facet cube (phase 5, optional)

To make pure-facet counts exact for the whole 48 h (not only the last hour): every closed minute, `Rollup` writes its facet
cube minute as one row `log_cube(bucket TEXT PRIMARY KEY, width INTEGER, blob {db.blob})` (zlib JSON of key → count; 53 KB
per minute measured, about 150 MB for 48 h), and every closed hour a merged hourly row. A 48 h query then reads 48 hourly and
at most 120 minute blobs. Until this exists, facet counts beyond the last hour are scan- or sample-based and flagged.

---

## 5. Performance plan (100k+ lines/min)

### 5.1 Which source answers what

| Query | Range inside the last 60 min | 1 h - 48 h | Older than 48 h |
|---|---|---|---|
| Facet-only (level, service, host, env, pool, source; any AND/OR/NOT) | counts, histogram, facets: **exact from the cube**; lines: keyset scan with row pruning | counts / histogram / facets: 1/k batch sample (`sample: k`, `exact: false`) or the persisted cube when phase 5 exists; lines: keyset scan | totals and ERROR+ per host / env / pool from `slo_minute` (`source: history`); no lines |
| Anything with text, attributes, trace, session, numeric comparisons | bounded keyset scan; total estimated from the cube count of the query's facet part × the scanned match rate | bounded scan; `at_least` or estimate | not available (message) |
| Group similar | facet-only on level / service / host / env / pool: `Rollup.groups` (exact); else scan + `template_of` | sampled rollup (as today) or scan | not available |
| Live tail | ring, by seq | n/a | n/a |

### 5.2 Facet cube (exact counts for the last hour)

`Rollup.add` gets one more counter per minute: `fc[minute][key] += 1` with
`key = host_id << 40 | service_id << 20 | source_id << 3 | severity_rank` (new id tables for services and sources). Every cube
key fully determines level, service, source and host, and env and pool follow from the host, so any boolean combination of
facet leaves is evaluated per key (`compile_key`, memoised per request) and the counts are summed. Measured on the load: 6,022
distinct keys per minute; 61 minutes of packed counters take about 18 MB; one pass over 61 minutes with a memoised predicate
and per-facet grouping takes about 80 ms. Guard: a minute with more than 60,000 keys stops adding new keys and is marked
`fc_over`; answers that touch it are flagged `exact: false`. Hot-path cost: one dict increment per line.

### 5.3 Scan (`logstore.scan_desc`)

1. Rows are read newest first with the `ts_max` index:
   `SELECT … FROM live_batches WHERE ts_max >= :s AND ts_max < :T + 600 s AND ts_min <= :T ORDER BY ts_max DESC, id DESC LIMIT 200`,
   then the next window (keyset on `(ts_max, id)`, no OFFSET; D16). v1 rows (only during the first 48 h after the upgrade)
   come from a second query without the upper bound and are merged.
2. Row pruning before decompression: `sevmask & plan.sev_mask == 0` → skip; `plan.hosts` pins hosts and the row has a single
   `host` → skip unless it is in the set (with `host IN (…)` in SQL when the set is small, so a quiet host is found by its own
   rows: D3); same for `src`.
3. Text prefilter: `plan.prefilter` (regex or plain substring) runs on the decompressed row text before any `json.loads`;
   most rows of a selective search are rejected at C speed.
4. Lines that pass are decoded, adapted to `LineView`, tested by the compiled predicate and pushed into a heap.
5. Watermark: rows are read in `ts_max DESC`; a buffered line is final once its `ts` is at least 1 s (`ts_max` is stored to
   the second) above the next unread row's `ts_max`. Final lines leave the heap in `(ts, seq)` order until the page is full.
   No arrival-order assumption (D1).
6. Without a database the ring is scanned (sorted by `(ts, seq)` once per request, about 20 ms for 50,000 events).

Budgets per request: 1.5 s wall time or 600,000 decoded lines or 25,000 rows, whatever comes first; the answer then carries
`scan.complete: false`, `frontier`, `stopped_by`, and a cursor to continue. Concurrency: at most 2 scans in the process
(`threading.BoundedSemaphore`), a third waits up to 3 s and then gets `503 {"code": "busy"}`; a new request from the same
(user, tab) cancels its previous scan (checked between rows). Scans never hold `LiveStore.lock` while decoding: they copy
references under the lock and work outside it, so ingestion is not blocked; zlib releases the GIL while decompressing.

Caching: answers cached 5 s keyed by (normalized q, scope hash, range rounded to 5 s for live ranges, endpoint); the
histogram and facets of a live range are therefore computed at most once per 5 s however many viewers there are.

Expected timings at 125k lines/min: facet-only counts, histogram, facets for 15 min: under 150 ms. First page of a facet-only
search: under 300 ms. Selective text search over 15 min (1.9 M lines): about 2-3 s to scan the whole range (about 1.6 s
decompression, the rest prefilter and decode of candidates), so the first call returns within the 1.5 s budget with an
estimate, and one "scan further" completes it. Tail poll: under 10 ms.

### 5.4 What the user is told

| Flag | Shown as |
|---|---|
| `total.exact: true, source: counters` | "2.863 sonuç" with a small "Kesin" tooltip: "sayaçlardan, her satır sayıldı" |
| `total.estimated` | "≈ 12.400 sonuç" with tooltip "Tahmini: taranan 400.000 satırda 3.307 eşleşme, aralığın 1,5 M satırına oranlandı" |
| `total.at_least` | "en az 22 sonuç" |
| `scan.complete: false` | a bar above the table: "Taranan: 08:44:31 → 08:45:00 (41.032 satır) · Daha geriye tara" |
| `sample: k` | histogram caption "örneklem 1/k, sayılar ölçeklendi" (existing `lx_sample` wording) |
| `source: history` | "Bu aralık satır saklama süresinden (48 saat) eski; yalnız toplam ve hata sayıları var." |
| `fc_over` | "Bu dakikalarda çok fazla farklı servis/dosya vardı; sayılar tahmini." |
| hatched bars | "taranmadı" in the bar tooltip |

---

## 6. UI spec

### 6.1 Where it lives

* Route `/logs` (`web/src/pages/Logs.tsx`), nav item `nav_logs` "Loglar" / "Logs" with the lucide `ScrollText` icon, first
  item of the SİNYALLER group (`id: "signals"` in `Layout.tsx`), permission `page.logs`.
* URL state (shareable): `q`, `last` or `start` + `end`, `live=1`, `group=1`, `cols=time,level,service,host,message,@status`,
  `line=<id>` (opens that line's detail on load, even when it is not on the first page).
* The operations screen's log card stays as a compact view (top 10 groups or the newest 25 lines of the ops scope, no files
  tab, the files list stays in the ops menu "Log dosyaları") with a header link "Log gezgininde aç →" to
  `/logs?q=<scope as query>&<range>`. The scope becomes query text: `env:prod pool:prd-sap-app host:(a OR b)`, plus the
  card's severity (`level>=warn`) and text. `goLogs(...)` callers (attention items, fleet, host drawer, range detail)
  navigate to `/logs` with the same translation instead of scrolling to the card.

### 6.2 Layout (desktop ≥ 1100 px)

```
┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│ [⧩] level:(err OR crit) service:nav* "connection lost"                    │ Son 15 dk ▾ │ ● Canlı │ ☆ Kaydet │ 🔔 Uyarı │ ▤ ▾ │
│      ⚠ "levle" bilinen bir alan değil; öznitelik olarak arandı. "level" mi demek istediniz?               │
├──────────────────────┬───────────────────────────────────────────────────────────────────────────────────────┤
│ Filtreler        ⟲   │ 2.863 sonuç  · Kesin               Kırılım: Seviye ▾   ⊖ Uzaklaş   ⊕ Yakınlaş           │
│ ▾ Seviye             │ ▁▂▂▃▅▇▅▃▂▂▁▁▂▃▃▂▁ (stacked by level, drag to zoom)                                     │
│   ☐ ● Kritik  1,0 bin│ 08:30      08:33      08:36      08:39      08:42      08:45                         │
│   ☑ ● Hata   33,3 bin├───────────────────────────────────────────────────────────────────────────────────────┤
│   ☐ ● Uyarı   8,6 bin│   Zaman (yerel)        Seviye  Servis         Sunucu        Mesaj    [Benzerleri grupla ○] ⚙ │
│   ☐ ● Bilgi  32,6 bin│ › 15.10 08:44:59.995   HATA    NavServer      prd-ebs-12    SQL Server connection lost: A …   │
│   ☐ ● Debug        0 │ › 15.10 08:44:59.871   KRİTİK  Ax32Serv       prd-sap-db-07 Transaction (Process ID 61) wa…   │
│ ▾ Servis   [Ara…]    │ ⌄ 15.10 08:44:59.640   HATA    http           prd-web-05    GET /api/v1/orders/{id} 500       │
│   ☐ http     16,4 bin│   ┌ Ayrıntı │ Bağlam │ Ham │ JSON ───────────────────────────────────────────────────┐ │
│   ☐ NavServer 6,7 bin│   │ Zaman     15.10.2026 08:44:59.640 (UTC 05:44:59.640) · geldi 08:45:00.012     │ │
│   …  12 tane daha    │   │ Sunucu    prd-web-05  [+][−][⧉]   Pool prd-web   Ortam prod                    │ │
│ ▸ Sunucu  (300)      │   │ Dosya     access.log:120344      Ajan prd-web-05                               │ │
│ ▸ Ortam              │   │ İz        4bf92f35…  İzi aç →     Oturum s_c4a3…  Oturumu aç →                 │ │
│ ▸ Pool               │   │ @status   500 [+][−]   @method GET   @bytes 512                               │ │
│ ▸ Dosya              │   │ Mesaj     GET /api/v1/orders/{id} HTTP/1.1 500                       [Kopyala] │ │
│                      │   └──────────────────────────────────────────────────────────────────────────────┘ │
│                      │ › 15.10 08:44:59.512   UYARI   NavServer      prd-ik-06     Warning: Report 206 Sales …   │
│                      │                       ⌄ Daha eski satırlar                                            │
└──────────────────────┴───────────────────────────────────────────────────────────────────────────────────────┘
```

* Query bar (full width, sticky under the app header): filter icon (`Filter`), monospace input with a syntax-coloured mirror
  layer behind it (fields in `--accent2`, operators `--muted`, quoted text `--fg`, error span with a red wavy underline), the
  existing `TimeRange` component (ops/TimeRange.tsx) with a "Canlı" preset, then "Canlı" (live tail; green, `--green`, a
  pulsing dot while running), "Kaydet" (`Bookmark`), "Uyarı" (`BellPlus`, only with `act.logalert`), and a saved-searches menu
  (`FolderOpen`). Autocomplete under the input: field names after a space, values after `field:` (from `/logs/facets?search=`
  for facet fields, `/logs/fields` for attributes), operators. Parse errors and warnings show one line under the bar.
* Team scope chip at the right end of the results line when the viewer is narrowed: "Ekip kapsamı: Muhasebe · 42 sunucu".

### 6.3 Filter sidebar (`components/logs/Facets.tsx`)

* Groups in this order: Seviye, Servis, Sunucu, Ortam, Pool, Dosya. Each: header (name, count of selected values, chevron;
  open state remembered in `localStorage` `wo_logs_facets`), a search box when the field has more than 8 values ("Ara…",
  server-side `search=`), up to 12 rows, "{n} tane daha" to load 50 more, "Seçenek yok" when nothing matches.
* Row: checkbox, colour dot for levels (Kritik `--violet`, Hata `--red`, Uyarı `--amber`, Bilgi `--accent2`, Debug `--dim`),
  value (ellipsis, full value in `title`), count right-aligned (tabular numbers, the existing `fmtN` format: 33,3 bin / 1,2 mn).
* Checkbox click: `rewrite op=add` or `remove`. Click on the value text: `only`. Alt-click or the row's hover "−": `exclude`
  (excluded values stay listed with strikethrough and a red minus). "⟲" in the sidebar header removes every facet clause.
* Counts follow `mode: multi` (3.5): other values of a field keep their counts while one is selected.
* Below 1100 px the sidebar becomes a drawer opened by "Filtreler (3)" above the histogram.

### 6.4 Results line and histogram (`components/logs/Histogram.tsx`)

* "2.863 sonuç" / "≈ 12.400 sonuç" / "en az 22 sonuç" with the flag tooltips of 5.4; partial-scan bar with "Daha geriye tara".
* "Kırılım" select (Seviye / Servis / Sunucu / Dosya / Pool): this is the reference's "Create Chart"; the histogram stacks by
  the chosen field (top 5 + "diğer").
* Recharts `BarChart`, height 120 (90 on phones), stacked bars, local-time ticks (`HH:mm`, with `dd.MM` when the range spans
  days, `HH:mm:ss` for sub-minute steps). Tooltip: bar time span and the count per key.
* Drag across bars selects a range and zooms to it (absolute start/end); a click on a bar zooms to that bar. "Uzaklaş" doubles
  the range around its centre (clamped to now), "Yakınlaş" halves it. Both leave live tail.
* During live tail the histogram shows the last 15 minutes and refreshes every 10 s.

### 6.5 Table (`components/logs/LogTable.tsx`)

* Columns: Zaman (yerel) `dd.MM HH:mm:ss.SSS` (ms because thousands of lines share a second; UTC and arrival in the tooltip;
  a clock badge when `skew_s` is set), Seviye (coloured text: KRİTİK, HATA, UYARI, BİLGİ, DEBUG), Servis, Sunucu, Mesaj
  (monospace, one line, ellipsis), then chosen extra columns. The "Mesaj" header holds the "Benzerleri grupla" toggle at its
  right end; the gear (`Settings2`) at the far right opens the column chooser.
* Column chooser: checkboxes for Dosya, Ortam, Pool, Ajan, İz, Oturum and the attribute keys from `/logs/fields`
  (searchable), up/down buttons to order them; Zaman, Seviye and Mesaj cannot be removed. Saved in `localStorage`
  `wo_logs_cols`, mirrored in the URL `cols=` and stored with saved searches.
* Rows are keyed by `id` (seq), never by index (D7). A chevron at the start of each row; click (or Enter on the focused row)
  expands it inline. At most 1,000 rows are kept (5 pages); beyond that the oldest are dropped with a note.
* "Daha eski satırlar" at the bottom loads the next keyset page; a refresh never mixes old pages with a new first page: in
  search mode the list refreshes only on demand ("Yenile" button with "{n} yeni satır var" from the histogram's newest bucket);
  live updates are the job of the live tail (D6).
* Group similar on: the table switches to Seviye, Kalıp (template, monospace), Servis, Adet, Sunucu sayısı, Eğilim (`Spark`),
  Son görülme. Expanding a group shows the latest sample (scrubbed) and "Bu kalıptaki satırlar", which adds `group:<key>` and
  turns grouping off.

### 6.6 Expanded row (`components/logs/LineDetail.tsx`)

Tabs (the existing `Tabs` component):

* **Ayrıntı**: key/value grid: Zaman (local, UTC, arrival, skew note), Seviye, Servis, Sunucu (link opens the existing
  `HostDetail` drawer; "Operasyonda aç" goes to `host_link.ops`), Ortam, Pool, Dosya:satır, Ajan, Tür, Kaynak (origin),
  Ayrıştırıcı, İz ("İzi aç →" or "İz kaydı saklanmamış (örneklem dışında)"), Oturum / Kullanıcı / İstemci (pseudonyms, "Oturumu
  aç →"), then the attributes sorted by key. Every value shows on hover: "+" filter by it (`rewrite add` for facet fields,
  `field:"value"` appended otherwise), "−" exclude it, "⧉" copy. The full message in a `pre` (wrapping) with "Kopyala" and
  "Bu kalıptaki satırlar".
* **Bağlam**: `/logs/context/{id}`: 20 lines before and after on the same host and file, the anchor highlighted, lines that
  match the current query marked, "Daha fazla" at both ends; clicking a context line opens its own detail in place.
* **Ham**: the raw line (scrubbed) in a `pre` with "Kopyala"; "Ham satır mesajla aynı; ayrıca saklanmadı." or "Bu satırın ham
  hali saklanmıyor (eski kayıt)." per `raw_state`.
* **JSON**: the whole `/logs/line/{id}` answer pretty-printed, "Kopyala" and "İndir (.json)".

In live tail, expanding a row pauses following (so the row stays put) until the user resumes.

### 6.7 Live tail (`components/logs/TailStatus.tsx` + table in tail mode)

* "Canlı" switches the range to live, clears the keyset pages and polls `/logs/tail` every 2 s with the last `after`
  (react-query, `refetchIntervalInBackground: false`). Newest lines on top with a short fade-in; rows in arrival order; the
  table keeps at most 1,000 rows.
* Status line under the histogram: "Canlı · 1.742/sn geliyor · 38/sn eşleşiyor"; when capped, an amber note: "Ekranda saniyede
  en fazla 250 satır gösteriliyor; 612 satır atlandı. Filtreyi daraltın." When `skipped > 0`: "Akış 4.000 satır geride kaldı;
  en yeniye atlandı."
* Pause on scroll: when the table is scrolled away from the top (more than 40 px) or a row is expanded, following pauses and
  new lines are buffered (up to 2,000); a floating pill "▲ 1.240 yeni satır — başa dön" shows the count; clicking it (or
  scrolling to the top) flushes the buffer and resumes. Explicit "Duraklat" / "Sürdür" button next to the status.
* Changing the query in live mode restarts the tail (first call without `after`).

### 6.8 Dialogs

* **Kaydet** (`Modal`): Ad (required), "Zaman aralığını da kaydet", "Herkesle paylaş (sonuçlar herkesin kendi yetkisine göre)",
  "Kaydet". Saved-searches menu: own searches first, then shared ones with the owner name; open, rename, delete (own).
* **Uyarı** (`Modal`, needs `act.logalert`): Ad; condition row "Son [5] dakikada eşleşen satır [≥ ▾] [10]"; preview "Son 24
  saatte bu koşul 3 kez sağlanırdı." (`/logs/alerts/preview`, refreshed as the numbers change); Önem; Kanallar (e-posta, SMS,
  webhook checkboxes, as on the Alerts page); Alıcılar (recipients / groups, the same field as the Alerts page); Bekleme
  süresi (dk); "Kaydet" → toast "Uyarı kuralı oluşturuldu." with a link to `/alerts`. A heavy stream query shows
  `lg_alert_heavy` instead of saving.

### 6.9 Keyboard

`/` focuses the query (unless typing in another field); Enter in the query runs it; Esc closes, in this order, an open
dialog, the autocomplete, the expanded row, then blurs the query; ↑ / ↓ move the row focus; Enter or Space on a focused row
toggles it. A hint line in the column chooser footer: "Kısayollar: / sorgu · Enter çalıştır · Esc kapat · ↑↓ satır".

### 6.10 Phone width (≤ 640 px)

16 px gutters; query bar wraps under the title with the buttons as icons; facets in a drawer; histogram 90 px; table shows
Zaman (HH:mm:ss), a level dot and Mesaj; the expanded row shows all fields stacked. No horizontal page scroll.

### 6.11 Styling

New classes appended at the end of `styles.css`, prefix `lg-` (no change to the existing `lx-*`): `.lg-page` (grid: 248 px
sidebar + main), `.lg-qbar`, `.lg-q`, `.lg-q-mirror`, `.lg-q-err`, `.lg-hint`, `.lg-side`, `.lg-facet`, `.lg-fv`, `.lg-fv.excl`,
`.lg-hist`, `.lg-head`, `.lg-table` (sticky header), `.lg-row`, `.lg-row.open`, `.lg-row.fresh`, `.lg-exp`, `.lg-kv`,
`.lg-lvl.CRITICAL|ERROR|WARN|INFO|DEBUG`, `.btn.lg-live`, `.lg-pill`, `.lg-tail`, `.lg-ctx .anchor`. Only existing CSS
variables (`--fg --muted --dim --accent --accent2 --violet --amber --red --green --line --line2 --mono`), so dark and light
themes both work; `.card`, `.seg`, `.chip`, `.btn`, `.table` are reused.

### 6.12 i18n keys (add before the line starting `  tm_f_host:` in `web/src/i18n.ts`)

```
nav_logs: ["Loglar", "Logs"], lg_title: ["Log gezgini", "Log explorer"],
lg_query_ph: ["Sorgu: level:error service:nav* \"bağlantı koptu\" -host:test-*  ( / ile odaklan )", "Query: level:error service:nav* \"connection lost\" -host:test-*  ( / to focus )"],
lg_live: ["Canlı", "Live tail"], lg_save: ["Kaydet", "Save"], lg_alert: ["Uyarı", "Alert"], lg_saved: ["Kayıtlı aramalar", "Saved searches"],
lg_filters: ["Filtreler", "Filters"], lg_f_level: ["Seviye", "Level"], lg_f_service: ["Servis", "Service"], lg_f_host: ["Sunucu", "Host"],
lg_f_env: ["Ortam", "Environment"], lg_f_pool: ["Pool", "Pool"], lg_f_source: ["Dosya", "File"], lg_f_search: ["Ara…", "Search…"],
lg_f_none: ["Seçenek yok", "No options found"], lg_f_more: ["{n} tane daha", "{n} more"], lg_f_only: ["Yalnız bu", "Only this"],
lg_f_exclude: ["Hariç tut", "Exclude"], lg_f_reset: ["Filtreleri temizle", "Clear filters"],
lg_results: ["{n} sonuç", "{n} results"], lg_results_est: ["≈ {n} sonuç", "≈ {n} results"], lg_results_min: ["en az {n} sonuç", "at least {n} results"],
lg_exact_tip: ["Kesin: sayaçlardan, her satır sayıldı", "Exact: from the counters, every line counted"],
lg_est_tip: ["Tahmini: taranan {s} satırda {m} eşleşme, aralığın {r} satırına oranlandı", "Estimate: {m} matches in {s} scanned lines, scaled to the range's {r} lines"],
lg_scanned: ["Taranan: {from} → {to} ({n} satır)", "Scanned: {from} → {to} ({n} lines)"], lg_scan_more: ["Daha geriye tara", "Scan further back"],
lg_zoom_out: ["Uzaklaş", "Zoom out"], lg_zoom_in: ["Yakınlaş", "Zoom in"], lg_breakdown: ["Kırılım", "Breakdown"], lg_not_scanned: ["taranmadı", "not scanned"],
lg_c_time: ["Zaman (yerel)", "Timestamp (local)"], lg_c_level: ["Seviye", "Level"], lg_c_service: ["Servis", "Service"], lg_c_host: ["Sunucu", "Host"],
lg_c_message: ["Mesaj", "Message"], lg_c_source: ["Dosya", "File"], lg_c_env: ["Ortam", "Environment"], lg_c_pool: ["Pool", "Pool"], lg_c_agent: ["Ajan", "Agent"],
lg_c_trace: ["İz", "Trace"], lg_c_session: ["Oturum", "Session"], lg_group: ["Benzerleri grupla", "Group similar events"], lg_columns: ["Sütunlar", "Columns"],
lg_cols_attrs: ["Öznitelikler", "Attributes"], lg_c_template: ["Kalıp", "Pattern"], lg_c_count: ["Adet", "Count"], lg_c_hosts: ["Sunucu sayısı", "Hosts"],
lg_lv_CRITICAL: ["KRİTİK", "CRITICAL"], lg_lv_ERROR: ["HATA", "ERROR"], lg_lv_WARN: ["UYARI", "WARN"], lg_lv_INFO: ["BİLGİ", "INFO"], lg_lv_DEBUG: ["DEBUG", "DEBUG"],
lg_t_detail: ["Ayrıntı", "Detail"], lg_t_context: ["Bağlam", "Context"], lg_t_raw: ["Ham", "Raw"], lg_t_json: ["JSON", "JSON"],
lg_copy: ["Kopyala", "Copy"], lg_copied: ["Kopyalandı", "Copied"], lg_download: ["İndir (.json)", "Download (.json)"],
lg_filter_by: ["Buna göre filtrele", "Filter by this"], lg_exclude_val: ["Bunu hariç tut", "Exclude this"], lg_same_group: ["Bu kalıptaki satırlar", "Lines of this pattern"],
lg_raw_same: ["Ham satır mesajla aynı; ayrıca saklanmadı.", "The raw line equals the message; not stored separately."],
lg_raw_absent: ["Bu satırın ham hali saklanmıyor (eski kayıt).", "No raw line kept for this record (older format)."],
lg_ctx_more: ["Daha fazla", "More"], lg_open_trace: ["İzi aç", "Open trace"], lg_trace_missing: ["İz kaydı saklanmamış (örneklem dışında)", "Trace not stored (outside the sample)"],
lg_open_session: ["Oturumu aç", "Open session"], lg_open_ops: ["Operasyonda aç", "Open in operations"],
lg_skew: ["Saat farkı: satır zamanı geliş zamanından {d} {dir}", "Clock skew: the line time is {d} {dir} the arrival"], lg_ahead: ["ileri", "ahead of"], lg_behind: ["geri", "behind"],
lg_tail_rate: ["Canlı · {in}/sn geliyor · {m}/sn eşleşiyor", "Live · {in}/s arriving · {m}/s matching"],
lg_tail_cap: ["Ekranda saniyede en fazla {n} satır gösteriliyor; {d} satır atlandı. Filtreyi daraltın.", "At most {n} lines/s are shown; {d} skipped. Narrow the filter."],
lg_tail_new: ["{n} yeni satır — başa dön", "{n} new lines — back to top"], lg_tail_gap: ["Akış {n} satır geride kaldı; en yeniye atlandı.", "The stream fell {n} lines behind; jumped to the newest."],
lg_pause: ["Duraklat", "Pause"], lg_resume: ["Sürdür", "Resume"], lg_refresh_new: ["{n} yeni satır var · Yenile", "{n} new lines · Refresh"],
lg_older: ["Daha eski satırlar", "Older lines"], lg_empty: ["Bu aralık ve sorguda log yok.", "No logs for this range and query."],
lg_retention: ["Bu aralık satır saklama süresinden ({h} saat) eski; yalnız toplam ve hata sayıları var.", "This range is older than the {h} h line retention; only totals and error counts remain."],
lg_sample: ["örneklem 1/{k}, sayılar ölçeklendi", "sample 1/{k}, counts scaled"], lg_fc_over: ["Bu dakikalarda çok fazla farklı servis/dosya vardı; sayılar tahmini.", "Too many distinct services/files in these minutes; counts are estimates."],
lg_scope: ["Ekip kapsamı: {t} · {n} sunucu", "Team scope: {t} · {n} hosts"], lg_busy: ["Arama kuyruğu dolu, birkaç saniye sonra tekrar deneyin.", "Search queue is full, try again in a few seconds."],
lg_save_title: ["Aramayı kaydet", "Save search"], lg_save_name: ["Ad", "Name"], lg_save_range: ["Zaman aralığını da kaydet", "Also save the time range"],
lg_save_share: ["Herkesle paylaş (sonuçlar herkesin kendi yetkisine göre)", "Share with everyone (results follow each viewer's access)"],
lg_alert_title: ["Bu sorgudan uyarı", "Alert from this query"], lg_alert_cond: ["Son {m} dakikada eşleşen satır", "Matching lines in the last {m} minutes"],
lg_alert_preview: ["Son 24 saatte bu koşul {n} kez sağlanırdı.", "In the last 24 hours this would have fired {n} times."], lg_alert_saved: ["Uyarı kuralı oluşturuldu.", "Alert rule created."],
lg_alert_heavy: ["Bu sorgu canlı sayım için ağır; bir alan filtresi ekleyin (ör. service: ya da host:).", "This query is too heavy to count live; add a field filter (e.g. service: or host:)."],
lg_open_in_explorer: ["Log gezgininde aç", "Open in log explorer"], lg_keys: ["Kısayollar: / sorgu · Enter çalıştır · Esc kapat · ↑↓ satır", "Shortcuts: / query · Enter run · Esc close · ↑↓ rows"],
```

Parser error and warning texts come from the API in both languages (2.7), so they need no keys.

---

## 7. Delivery plan and tests

| Step | Content | Fixes |
|---|---|---|
| 1 | `logquery.py` + `tests/test_logquery.py` (every construct of 2.1, aliases, error codes with positions, normalize round trip, `facet_only`, PII refusal, rewrite ops) | D10-D12 groundwork |
| 2 | `Observation.seq`, seq in `_accept`, arrival markers, `_ring_covers` by arrival (live.py hunks); `logstore.py` v2 codec, privacy transform, row split, compression, migrate, v1 compatibility in every reader; `tests/test_logstore.py` | D2, D8, D16 |
| 3 | Facet cube in `Rollup` (+ brute-force equality test in `tests/test_rollup.py`), `logsearch.py` keyset scan with watermark, budgets, semaphore, cache; `tests/test_logsearch.py`: 2,000 lines sharing one second spread over out-of-order batches, every line exactly once newest first; a stale-stamp line at `buf[0]` does not change the answer; a quiet host is found through its rows; facet counts and histogram equal brute force; team scope hides other hosts in lines, facets, detail (404) and context | D1, D3, D4, D5, D14, D15, D16 |
| 4 | Router `logs.py`, permissions, services wiring; tail with cursor, caps, 429; `tests/test_logtail.py` | D6, D7, D17 |
| 5 | Web page `/logs`, nav item, components, i18n, CSS; ops card link and `goLogs` navigation; `npx tsc --noEmit -p . && npm run build` | D6, D7, D11, D12, D13 (files via `source:` facet + tail) |
| 6 | Saved searches; notify condition `query`, `LogWatch`, alert dialog; additions to `tests/test_notify.py` (counters and stream modes, absence warm-up, cooldown key, owner scope) | |
| 7 (optional) | Persisted facet cube (4.6); per-stream clock-offset inference for naive stamps (with the Go parse work, since parsing moves to `receiver/parse`): when a stream's median arrival − stamp is at least 15 min and within 30 s of a multiple of 15 min, apply that offset to its naive stamps and say so in the detail; export | D9 |

Verification under load (scratch server, `scripts/loadtest.py --rate 1700 --agents 300 --heavy 100`): rerun the section 1
probes; the paging run must return every line of the fixed minute exactly once (compare with the `live_batches` ground
truth); the same query 12 times must give 12 identical answers; `q=zzzz` must report `complete:false` with a cursor, never
"no logs"; a tail poll under 10 ms; facet-only answers under 150 ms; screenshots of `/logs` in dark, light and phone widths.
