# Go parse port, part 06: integration and architecture

Scope: how Go-side parse workers plug into the existing Go receiver -> spool -> Python consumer path, without changing
what the dashboard computes. Sources read: `receiver/main.go`, `receiver/main_test.go`, `src/watchover/spool_consumer.py`,
`src/watchover/live.py` (`LiveStore._ingest`, `_persist`, `_pack`, `metric_of`, `BAD_HOST`), `src/watchover/pipeline.py`
(`_parse_one`, `ingest`, `fill_missing_timestamps`), `src/watchover/profiles.py`, `src/watchover/api/services.py`
(consumer wiring), `scripts/parse_golden.py`, `scripts/loadtest.py`. Parser internals are in parts 01-05.

---

## 1. Today's path (as built)

### 1.1 Receiver (`receiver/main.go`)

* Config (env, trimmed, ints must be > 0 else default): `WO_LISTEN=:8600`, `WO_SPOOL=/data/spool`, `WO_AUTH=<spool>/auth.json`,
  `WO_UPSTREAM=http://dashboard:8601`, `WO_AGENT_DIR=/app/agent`, `WO_MAX_BODY_MB=16`, `WO_SEGMENT_MB=8`, `WO_MAX_BACKLOG_MB=2048`,
  `WO_SEGMENT_MS=1000`, `WO_FSYNC` (anything but `off` = fsync on rotate).
* Auth (`auth.check`): `auth.json` reloaded every 2 s on mtime change. `nil` state -> 503 + `Retry-After: 5`. Token from
  `X-API-Key`, else `Authorization: Bearer <tok>`. `sha256(tok)` in `agents` -> identity `token` with the agent record
  (`id,name,env,site`). Unknown token starting `wo_` -> 401 (never falls back to the shared key). No shared key: `open` -> identity
  `open`, else 401. Shared key compared in constant time on the hex digests -> identity `key`.
* `/ingest` order of checks: auth -> backlog (`backlog > maxBacklog` -> 503, `Retry-After: 10`) -> `Content-Length > maxBody` -> 413
  -> read at most `maxBody+1` -> 413 if over -> empty -> 400.
* Header fields (JSON object, `json.Marshal` => keys sorted alphabetically):
  `v`=1, `path` (URL path, `/ingest`), `file` = `filepath.Base(X-File-Name)` (`""`, `.`, `/` -> `agent.log`; truncated to 200
  **bytes**), `ip` (RemoteAddr host), `ts` (UTC RFC3339Nano), `len` (body bytes), `identity`, `agent`. With a token agent:
  `agent`=registered name, `agent_id`=int, `env`, `site` (may be `""`). Otherwise `agent` = `X-Agent` or client IP, truncated
  to 120 **bytes** (note: byte slicing can split a UTF-8 rune; Python then decodes the header with `json.loads` on bytes,
  which raises on invalid UTF-8 -> counted as torn -> rest of segment dropped. Pre-existing bug, keep in mind for new records).
* RUM/mobile header: `v,path(/rum|/mobile),app,app_key,origin,ua[:300 bytes],ts,len`.
* Record framing: `<header JSON>\n<body (len bytes)>\n`. Segment file `seg-%019d.tmp` (unixnano) while open, renamed to `.wo`
  on rotate (every `WO_SEGMENT_MS`, checked at `segEvery/4` ticks, or when size >= `WO_SEGMENT_MB`). One goroutine owns the file
  (`spool.run`); HTTP handlers send a `record` on a 4096-deep channel and wait for `done` (2 s send timeout -> 503 busy).
  Startup renames leftover `.tmp` to `.wo` (torn tail tolerated). Backlog = sum of `.wo` sizes, rescanned every 2 s.
* `/health` JSON and `/metrics` (Prometheus text, only `int`/`int64` fields of health, prefix `watchover_receiver_`).
* Tests (`main_test.go`): `TestTokenAuthSpoolAndRotation`, `TestOpenModeKeyAndNotReady`, `TestBackpressureLimitsAndHealth`,
  `TestAgentFilesEnrollProxyAndCrashRecovery`, `TestRumAndMobileIntake`. All must stay green; the new path adds tests, it does
  not change these.

### 1.2 Consumer (`spool_consumer.py`)

* Wired in `api/services.py` only when `WATCHOVER_RECEIVER=go`: dir = `WATCHOVER_SPOOL` or `<WATCHOVER_HOME|data>/spool`;
  `store=self.live`, `registry=self.agents`, `handlers={"/rum": rum_h, "/mobile": rum_h}`, `apps_fn=self.rum.apps`. One daemon
  thread (`spool-consumer`), poll interval 0.2 s when idle. Exports `auth.json` every >3 s (atomic replace, only on change).
* `process_segment(seg)`: segments sorted by name (= creation time). Resume offset from `<seg>.pos` (int, bad -> 0). For each
  record: header line up to `\n`; `json.loads`, `int(h["len"])`; failure or short body -> `torn += 1` and **stop the segment**
  (the segment is still deleted). `_handle`: `path` default `/ingest`; `/ingest` ->
  `store.ingest(h.file or "agent.log", body, h.agent or "unknown", env=h.env or "", site=h.site or "", agent_id=h.agent_id)`
  then `registry.touch(int(agent_id), ip, n)` when `agent_id` present; other paths -> `handlers[path](h, body)`; unknown paths
  silently ignored. Exceptions -> `batches_failed += 1`, continue. Lag = now - header `ts`. `.pos` written at most once a
  second (so a crash replays <= ~1 s of records: at-least-once). After the loop: unlink segment and `.pos`.
* Single-threaded, strictly in segment+record order: per-stream order (agent, file) is exactly arrival order.

### 1.3 `LiveStore._ingest` (everything after parsing, must be reused verbatim)

1. Hint lookup (`self._detect[(agent, name)]`, `n += 1`, dropped when `n % 200 == 0`) -> `ingest_bytes(name, data, hint=hint)`.
2. Hint store: when report[0].format not in (None, "table") and (no hint or n%200==0): `fmt="jsonl"` if fmt=="json" and the
   stripped batch has no `\n` and starts with `{`; store `{format, confidence (default 0.9), family (default ""), n: 1}`;
   clear whole dict when > 5000 keys.
3. `now = datetime.now(UTC)`, `horizon = now + 1 day`. Per observation, in order:
   * `o.attributes.pop("_no_ts", False)` -> arrival time. **Effectively dead**: `pipeline.ingest` already ran
     `fill_missing_timestamps`, which pops `_no_ts` from every observation (rows without a time got the earliest real time of
     the batch, or `observations[0].timestamp` if none). Remaining triggers: `year < 2000` or (aware/UTC-assumed) ts > horizon
     -> `o.timestamp = now`.
   * `attributes["agent"] = agent` (overwrites a parsed `agent` attribute).
   * `BAD_HOST = r"^\[.*\]$|:$|\s|^-$"` searched on `o.host` -> `attributes.setdefault("component", host.strip("[]:"))`,
     `host=""`. RE2: fine as is (Python `$` also matches before a trailing `\n`; `\s` would already match that `\n`, so no
     difference). `\s` in Python str-regex is Unicode whitespace; RE2 `\s` is ASCII only -> irrelevant here because this step
     stays in Python.
   * empty host and agent not in ("", "unknown") -> `host = agent`.
   * `enricher(o)` (inventory: IP -> hostname, dc, criticality; mutates o), counts `matched`.
   * `env` non-empty -> `o.environment = env`; `site` -> `attributes["site"]`; `agent_id is not None` -> `attributes["agent_id"]`.
   * `"flow.dir" in attributes` -> flows list (not an event). Else `metric_of(o)` -> metric rows; else event.
4. Under lock: ring `buf`, `metrics`, `metric_hosts[agent]`, `received += len(obs)` (all parsed incl. metrics/flows),
   `agents[agent] = time.time()`, `_rate`.
5. `rollup.add(events)`, `on_flows(flows)` (FlowStore), `_persist(events, agent, env)` (live_batches row, `_pack`),
   `on_ingest(events)` (`Services._on_ingest`: history, traces, bizflows, anomalies), JSONL spool append. Return `len(events)`
   (this is `n` passed to `registry.touch`).

### 1.4 Hidden state inside the parse step (relevant to Go)

* `scenario.MAPPING` (currently `{}`): used as the mapping when none passed. Go must read it from config or require it to be
  empty; if non-empty, Go must fall back to Python (simplest: Python exports it in `auth.json`-like `parse.json`, see section 5).
* `FAMILY_HINT` / `FAMILY_SEEN` contextvars: per-call, set/reset inside `_parse_one`; Go equivalent is a plain parameter/return
  value. No cross-call leakage.
* **Learned profiles** (`<WATCHOVER_HOME>/profiles.json`): for fmt in `json, jsonl, csv, kv, grok`, `_parse_one` calls
  `profiles.lookup(fmt, keys)` (signature `sha1(fmt + "|" + ",".join(sorted(keys)))[:16]`, keys = union of the first 50 records'
  keys in first-seen order) and, when a record with non-empty `mapping` exists, passes it to `auto_map` and the parser; then
  **always** calls `profiles.remember(...)`, which rewrites the whole file on every live batch of those formats. Profile:
  184 of 1540 batches wrote profiles.json, 1.17 s of 8.1 s profiled time (~15 %). Output effect: golden live reports already
  show `"profile": "5d7e874a6060f57b"` for `mut__logfmt.log` because `parse_golden.py` shares one temp WATCHOVER_HOME across
  all files (whole run first, then live) -> later batches are parsed *with* the auto-learned mapping. Goldens were therefore
  produced with profile state; see section 5 for what Go does.
* `pipeline.ingest`: `dedupe(observations, report)`, `fill_missing_timestamps`, stable sort by timestamp. The Go output must be
  post-dedupe, post-fill, post-sort (that is what the goldens contain).
* `PARALLEL_MIN_CPUS` process pool: never used on the live path for one file (`_workers` of one item = 1).

---

## 2. Measured baseline (this machine, Python 3.11)

Script: replay of every file in `receiver/parse/testdata/inputs` in 40-line batches through one `LiveStore(spool=None)` with
agent `lt-host`, env `prod`, site `ist`, agent_id 7, no DB and no hooks attached (so post-parse cost is a lower bound):

| measure | value |
|---|---|
| batches / lines / events kept | 1540 / 61 060 / 16 293 |
| total `LiveStore.ingest` | 4.32 s (14 127 lines/s, one core) |
| `ingest_bytes` share | 3.996 s = **92.5 %** |
| cProfile hot spots (cumulative, 8.1 s under profiler) | `records_to_observations` 3.4 s, `normalize.auto_map` 2.7 s (two calls per batch: head roles + `parse`), `text_parser.records`/`parse_head` 1.9 s / 1.4 s, `profiles.remember`->`save` 1.2 s, `re.search` 0.9 s, `re.match` 0.6 s |
| canonical JSON -> `Observation` (fast path, section 4) | 16 532 golden live observations, 10.4 MB, **0.151 s = 9.1 us/obs** |

Interpretation: in production, `_persist` (JSON pack + DB insert) and `on_ingest` (history/traces/bizflows) add a roughly
fixed per-event cost that does not shrink. Even so, replacing `ingest_bytes` (~260 us/line-batch-amortised; 65 us per kept
event) by the decode (9 us per event) removes ~85-90 % of the consumer thread's parse time. Expected end-to-end consumer
throughput gain on the ERP-heavy loadtest mix: 4-8x until `on_ingest`/DB becomes the bottleneck. Measure, do not assume
(section 9).

---

## 3. Design overview

```
agent POST /ingest ──► Go handler (auth, limits, unchanged) ──► spool writer ──► seg-*.wo  [raw records, v=1, as today]
                                                         │
                                       (WATCHOVER_GO_PARSE=1)
                                                         ▼
                      parse stage: reads closed raw segments ─► per-stream ordered worker pool ─► parsed records (v=2)
                                                                                                    in pseg-*.wo
                                                                                                         ▼
                                    Python SpoolConsumer: v=1 -> store.ingest(bytes) (today) ; v=2 -> store.ingest_parsed(obs)
```

Principle: the HTTP request path is untouched (answer as soon as bytes are on disk). Parsing happens in a separate Go stage
that consumes closed raw segments, so a parse bug or slowness can never cause agent 5xx.

---

## 4. (1) Where the parse workers run and the new record kind

### 4.1 Stage placement

* New goroutine `parseStage` in the receiver, enabled by `WATCHOVER_GO_PARSE`. It owns a second directory namespace inside the
  same spool dir:
  * raw segments `seg-<ns>.wo` (unchanged writer);
  * parsed output segments `pseg-<ns>.tmp` -> `pseg-<ns>.wo` (same rotation rules, written by one writer goroutine);
  * when the stage is on, the Python consumer must only read `pseg-*.wo` **and** raw `seg-*.wo` that the stage has handed over.
    Simplest correct rule: when on, the stage *consumes* every raw `seg-*.wo` (oldest first) and re-emits **every** record into
    `pseg` either as a parsed record (v=2) or as the original raw record (v=1, byte-identical header and body). Python then
    reads only `pseg-*.wo` (glob change is controlled by the same flag, exported by Go in `/health` and by env on the Python side).
    One reader per file -> no double processing, no ordering ambiguity.
* The raw segment is deleted only after the pseg containing all of its records is closed and fsynced (renamed `.wo`). A
  `seg-<ns>.gpos` file (byte offset of the next raw record not yet durably emitted) makes restarts resume inside a segment,
  exactly like `.pos` in Python; replay window = records after the last gpos write (at-least-once, duplicates possible after
  crash, same guarantee as today).
* Backlog for 503 backpressure = sum of `seg-*.wo` + `pseg-*.wo` sizes (rescan both globs). Keep `WO_MAX_BACKLOG_MB` semantics.

### 4.2 Parsed record (v=2) framing

Same framing as today: `<header JSON>\n<body len bytes>\n`, so `process_segment` torn-tail logic is reused.

Header (Go `json.Marshal` of a struct with these fields; all v=1 fields are copied unchanged so `_handle`, `touch`, lag work):

| field | type | meaning |
|---|---|---|
| `v` | int | `2` |
| `kind` | string | `"parsed"` |
| `path` | string | `/ingest` (only /ingest records are ever parsed; /rum, /mobile always pass through as v=1) |
| `file`,`agent`,`agent_id`,`env`,`site`,`ip`,`identity`,`ts` | as v=1 | copied from the raw header (`ts` stays the receive time, so lag is still arrival-based) |
| `len` | int | bytes of the NDJSON body |
| `n` | int | number of observations in the body |
| `raw_len` | int | length of the original body (metrics) |
| `format`,`family`,`confidence` | string,string,float | `report[0]` values, used to update the Python hint (section 6) |
| `report` | object | `report[0]` minus `sha`,`table` (same projection as the goldens); optional, for the Datasets/parse-report UI |
| `parser_version` | string | Go parse package version + golden set hash, for audit |

Body: one canonical observation per line, **exactly** the golden line format (`json.dumps(canon(o), sort_keys=True,
ensure_ascii=False)`): keys `attributes, environment, host, kind, line_no, message, origin, parser, parser_confidence, raw,
service, severity, source, ts`. Order = the order `ingest_bytes` returns (post dedupe, fill, stable timestamp sort). Empty batch
(n=0): still emit the v=2 record with empty body so hint counters and `touch` see the batch (Python returns 0 events today).

### 4.3 Ordering and delivery semantics

* Within one raw segment, records are emitted to pseg in **input record order** regardless of which worker parsed them
  (sequence numbers + a reorder buffer in the emitter). Since Python handles segments in name order and records in file order,
  per-stream (agent, file) order is identical to today, and cross-stream interleaving is also identical.
* At-least-once end to end: Go `.gpos` + Python `.pos`. Duplicate delivery after a crash is the same as today.
* Fallback per record: if the Go parse panics (recover in worker), times out (per-batch budget, e.g. 2 s), or the format is not
  allow-listed, emit the raw v=1 record. The decision is per batch, never per line (a batch is never split between Go and Python).

### 4.4 Python consumer changes (`spool_consumer.py`)

* `pending()` globs `pseg-*.wo` when `WATCHOVER_GO_PARSE` is on, else `seg-*.wo` (stats/oldest parse `[5:24]` for pseg names).
* `_handle`: `if h.get("v") == 2 and h.get("kind") == "parsed": n = store.ingest_parsed(body, h, ...)` else today's path.
* Mixed segments are expected (v=1 and v=2 interleaved).

---

## 5. (2) Python fast path: `LiveStore.ingest_parsed`

Refactor `_ingest` into `_parse(name, data, agent) -> obs` (steps 1-2) and `_accept(obs, agent, env, site, agent_id) -> int`
(steps 3-5, unchanged code). Then:

```python
def ingest_parsed(self, body: bytes, h: dict) -> int:
    t0 = time.perf_counter()
    try:
        agent = h.get("agent") or "unknown"; name = h.get("file") or "agent.log"
        self._note_hint(agent, name, h)                  # keep _detect coherent if the flag is switched off later
        fi = datetime.fromisoformat
        obs = []
        for ln in body.splitlines():
            if not ln: continue
            d = json.loads(ln)
            obs.append(Observation(timestamp=fi(d["ts"]), message=d["message"], kind=d["kind"], severity=d["severity"],
                service=d["service"], host=d["host"], environment=d["environment"], origin=d["origin"], attributes=d["attributes"],
                source=d["source"], line_no=d["line_no"], parser=d["parser"], parser_confidence=d["parser_confidence"], raw=d["raw"]))
        return self._accept(obs, agent, h.get("env") or "", h.get("site") or "", h.get("agent_id"))
    finally:
        self._ingest_ms.append((time.perf_counter() - t0) * 1000)
```

Notes for exact equivalence:
* `datetime.fromisoformat` (3.11) round-trips `isoformat()` output, including naive stamps (no offset) and microseconds. Go must
  write `ts` exactly as Python `isoformat()`: `YYYY-MM-DDTHH:MM:SS[.ffffff][+HH:MM]` (microseconds only when non-zero, 6 digits;
  offset `+00:00` not `Z`; naive -> no offset). All 16 532 golden live observations are aware; naive must still be supported.
* `parser_confidence` is rounded to 4 decimals in the canonical line; the Python object today carries the unrounded value. It is
  not read after `_accept` (not in `_pack`, spool or rollups); accept the difference, document it.
* Attribute values: JSON only yields `str, int, float, bool, None, list, dict`. Python-side parsers may put tuples or
  `datetime` objects in attributes (goldens used `default=str`): such values would arrive as list/str. Grep showed no consumer
  after `_accept` depending on non-JSON types, but the parity test must assert the golden file has no value that differs between
  `json.loads(json.dumps(x, default=str))` and `x` for allow-listed formats.
* Floats: Go must emit Python `repr` style (`1.0`, `0.1`, `1e-05`, `1e+16`); `NaN`/`Infinity` cannot be produced by
  `encoding/json` -> if an attribute is NaN/Inf, the Go worker must fall back to raw for that batch.
* `_no_ts`: never present in canonical output (popped by fill). Nothing to do.
* `self._detect` hint: Python's hint dict must still be updated as if Python had parsed, so that switching the flag off mid-stream
  behaves as today: on v=2, apply step 2 with `rep[0]` = `{format, confidence, family}` from the header.

Cost: 9.1 us/observation measured, vs ~245 us/observation (3.996 s / 16 293) for `ingest_bytes` today.

---

## 6. (3) Per-format allow-list

* Go keeps `allow = map[format]set[family]` compiled in, guarded by tests: a format/family is added only when its parity test
  (golden `whole` and `live` for every input whose report[0] has that format/family) is byte-identical. Example table to fill
  from parts 02-05 as ports land: `jsonl`, `json` (arrays), `kv` (logfmt, fortigate), `syslog` / `grok` families (nginx, apache,
  haproxy, postfix...), `csv`, `sap_*`, `oracle_*`, `dynamics_*`.
* Runtime override `WATCHOVER_GO_PARSE_FORMATS=jsonl,kv,...` (intersection with the compiled allow-list, never a superset) and
  `WATCHOVER_GO_PARSE_DENY=...`.
* Decision per batch: Go runs detection (or uses the stream hint), and if the chosen `format` (+`family` for text/grok) is not
  allowed, the record is forwarded raw (v=1) **and** Go does not update its hint cache for that stream (Python will). Side
  tables (`tables.is_side_table(fname)`) and `format=table` are always forwarded raw.
* `/rum`, `/mobile`, any non-`/ingest` path: always raw.

---

## 7. (4) Hint cache and learned profiles

### 7.1 Hint cache in Go

Replicate `_detect` exactly, keyed by `(agent, file)` from the header (agent = header `agent`, i.e. registered name or X-Agent),
owned by the stream's worker (no lock needed with stream sharding, section 8):

1. `h := cache[key]`; if present `h.n++`; if `h.n % 200 == 0` treat as absent for this batch.
2. Parse with hint if present and `h.format` is a known parser (else detect).
3. If `report.format` not in {"", "table"} and (no hint or `h.n % 200 == 0`): `fmt = report.format`; if `fmt=="json"` and
   `bytes.Count(bytes.Trim(body, " \t\n\r\v\f"), "\n")==0` and the left-trimmed body starts with `{` -> `"jsonl"`. Store
   `{format, confidence(default 0.9), family(default ""), n:1}`; if `len(cache) > 5000` clear it.
   Python `bytes.strip()`/`lstrip()` strip exactly the ASCII set `b" \t\n\r\x0b\x0c"`; Go must use that set
   (`bytes.Trim(b, " \t\n\r\v\f")`), not `unicode.IsSpace`.
4. Live parity: the goldens' `*.live.jsonl.gz` were generated with exactly this algorithm (`parse_golden.py` main loop).
   Caveat: Go always runs steps 1-3 (detection included) for every /ingest batch, even when the resulting format is not
   allow-listed and the batch is forwarded raw. Python runs the same steps for raw batches and `_note_hint` for v=2 batches.
   Both caches therefore see the same batch sequence and stay in lockstep. Rule: every batch, parsed or forwarded, increments
   the Go counter.
5. Restart: both caches are in-memory; a Go restart re-detects (same as a Python restart today). Divergence window: Go restarts
   while Python keeps its cache -> Python's hint would have been used for the next batch, Go detects instead. Detection and
   hinted parse can differ for ambiguous streams. Mitigation: on startup Go reads `hints.json` exported by the consumer every
   few seconds (same mechanism as `auth.json`), or accepts this rare first-batch difference (documented).

### 7.2 Learned profiles

`profiles.json` changes parse output (mapping fed to `auto_map` and the parser) for `json, jsonl, csv, kv, grok`, and Python
writes it on nearly every batch of those formats. Options:

* **Phase 1 (recommended)**: Go reads `<WATCHOVER_HOME>/profiles.json` read-only (mtime-reloaded, like `auth.json`; dashboard
  exports a copy into the spool dir as `profiles.json` so the receiver container needs no access to WATCHOVER_HOME). Go
  computes `signature(fmt, keys)` identically (`sha1` hex[:16] of `fmt|` + sorted keys joined by `,`, sort = Python codepoint
  order = Go byte order for valid UTF-8). If a profile with `source=="user"` exists for the shape -> **forward raw** (user
  mappings are rare and must be honoured exactly). If an `auto` profile exists, Go must apply its mapping exactly as
  `_parse_one` does (mapping = dict(learned["mapping"]) passed to `auto_map` and `parser.parse`); parity tests must cover it
  (the logfmt live golden already has `profile` set, so it does).
* `remember()` side effect: Go never writes profiles. On v=2 records, Python calls `profiles.remember(format, keys, roles, name)`
  only when `report.keys` is present and the shape is new or the hit counter matters — recommend debouncing in Python
  (once per minute per signature) since the 15 % cost is pure overhead; this changes only `hits/last` bookkeeping, not output.
* `scenario.MAPPING`: Python exports it in the same file; non-empty -> Go forwards everything raw.

---

## 8. (5) Concurrency

* `N = GOMAXPROCS` (override `WO_PARSE_WORKERS`) workers; each owns a shard `fnv32(agent + "\x00" + file) % N` so every stream is
  processed by one goroutine in order and owns its hint-cache slice (hint cache cap 5000 is global in Python; emulate with a
  global atomic count and clear-all, or accept per-shard cap 5000/N — must be global for exact parity, use a mutex-guarded map).
* Reader goroutine walks raw segments oldest first, assigns `seq` per record, dispatches to the shard channel (bounded,
  e.g. 256). Emitter goroutine writes results strictly in `seq` order (reorder buffer keyed by seq); a slow batch therefore
  delays emission of later records, bounded by the per-batch timeout -> fallback raw.
* Memory bound: max in-flight bytes (e.g. 256 MB) across the reorder buffer; the reader blocks when exceeded.
* Parsers must be pure: no package-level mutable state; compiled regexps are shared read-only (Go `regexp` is goroutine safe).

---

## 9. (6) Metrics

Add to `health()` (and therefore `/metrics`, ints only):

| name | meaning |
|---|---|
| `parse_enabled` | 0/1 |
| `parse_batches_parsed`, `parse_batches_forwarded` | v=2 vs v=1 emitted for /ingest |
| `parse_forward_reason_{format,profile,panic,timeout,nan,table,scenario}` | why raw |
| `parse_observations` | observations emitted |
| `parse_lines_in`, `parse_bytes_in` | input volume |
| `parse_ns_total` | total worker time (rate -> CPU) |
| `parse_backlog_bytes`, `parse_backlog_segments` | raw segments not yet parsed |
| `parse_pseg_backlog_bytes` | pseg not yet consumed by Python |
| `parse_lag_ms` | now - header ts of last emitted record |

Per format: a JSON map `parse_by_format: {"kv": {"parsed": n, "forwarded": n, "obs": n}}` in `/health`, and Prometheus lines
`watchover_receiver_parse_batches{format="kv",result="parsed"} n` (requires extending the `/metrics` writer beyond the int
switch). Python `SpoolConsumer.stats()` adds `parsed_records`, `raw_records`, `ingest_parsed_ms_avg`.

---

## 10. (7) Feature flag and rollout

* `WATCHOVER_GO_PARSE` read by **both** processes (docker-compose sets it once): `0`/unset = today; `shadow`; `1` = live.
* `shadow` mode: Go parses but writes results to `pseg-shadow-*.wo` only, Python keeps consuming raw. A Python checker
  (`scripts/parse_shadow_diff.py`, new) re-parses the matching raw record with `ingest_bytes` (hint from its own cache) and
  compares canonical lines; mismatches counted per format, first 100 stored. A format graduates to the runtime allow-list
  after 0 mismatches over >= 24 h of real traffic.
* `1`: as section 4. Switching back to `0`: Go stops parsing, finishes (or forwards raw) what is in flight, and Python's glob
  falls back to `seg-*.wo` after the `pseg` backlog is empty (consumer drains both globs, pseg first, during the transition).
* CI gate: `go test ./receiver/parse/...` runs every golden (`*.whole` and `*.live`) for allow-listed formats; goldens
  regenerated with `scripts/parse_golden.py` (note: year-less timestamps take the current year -> regenerate after New Year).

---

## 11. (8) Measuring the gain with `scripts/loadtest.py`

1. Baseline: `WATCHOVER_RECEIVER=go WATCHOVER_GO_PARSE=0`, run
   `python scripts/loadtest.py --url http://localhost:8600 --api http://localhost:8501 --rate 2000 --agents 300 --heavy 100 --heavy-share 0.85 --duration 120 --probe --out lt-raw.json`.
2. Same with `WATCHOVER_GO_PARSE=1`, `--out lt-go.json`.
3. Raise `--rate` stepwise (2k, 5k, 10k, 20k ev/s) until the receiver's `backlog_bytes` grows monotonically (sampled by the
   script from `/health`) — the knee is the sustainable rate. Compare: knee rate, `capacity.ingest.avg_ms`/`p95_ms` (Python
   per-batch time, now `ingest_parsed`), consumer `last_lag_s`, probe latencies of the operations screen, receiver CPU
   (`parse_ns_total` rate) and dashboard CPU.
4. Correctness during the run: event counts per host/env in the operations screen must match between the two runs for the
   same seed; shadow-diff mismatch count = 0.

---

## 12. Golden coverage of this slice

* The integration path itself is not covered by goldens; goldens cover only `ingest_bytes` output (whole) and the hint loop
  (live). `parse_golden.py`'s live loop == `LiveStore._ingest` steps 1-2, so `*.live.jsonl.gz` is the reference for the Go
  stream worker. Post-parse steps (host fallback, BAD_HOST, enricher, env/site/agent_id, flow/metric diversion) stay in Python
  and need no golden.
* Inputs exercising post-parse branches when run through `ingest_parsed` (useful for an end-to-end test comparing
  `ingest` vs `ingest_parsed` on the same LiveStore state): `sim__simulate_flows.jsonl` (`flow.dir` diversion),
  `sim__simulate_batch.jsonl` (metric records `metric_of`, hosts), `mut__dynamics_ax_aos.log` / `mut__dynamics_nav_bc.log`
  (component-like hosts such as `Ax32Serv:`/`[NavServer]` -> BAD_HOST), `demo__*` (env/site override).
* NOT covered: learned **user** profiles (goldens only have auto profiles), non-empty `scenario.MAPPING`, NaN/Infinity attribute
  values, naive timestamps in canonical output (0 of 16 532), hint-cache overflow (>5000 streams), re-detection producing a
  different format at n%200 (inputs are 241 lines = 7 batches, so the n%200 reset never fires in the goldens), Go/Python
  restart divergence, torn v=2 records.

## 13. Required new tests

* Go: `TestParseStageOrderAndFallback` (mixed allowed/forwarded records keep order; panic -> raw), `TestParseStageResume`
  (`.gpos`), `TestParsedHeaderFields`, `TestHintCacheParity` (replay the live loop on every input, compare with
  `*.live.jsonl.gz`).
* Python: `test_ingest_parsed_equivalence` — for each golden input, feed 40-line batches into store A via `ingest` and the golden
  canonical lines into store B via `ingest_parsed` (same agent/env/site/agent_id, frozen `now`), assert identical ring buffer
  `_pack` output, metrics, flows and rollup counters.
