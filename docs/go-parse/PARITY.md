# Go parse parity status

Status after the parity close-out (2026-10-15): **120/120 golden inputs are byte-identical to Python in whole mode and
120/120 in live mode** (`go test ./parse/ -run TestGolden -v`). Every input is listed in `receiver/parse/testdata/allow.txt`
with its format column, so every format and text/grok family below is enabled for Go parsing (the runtime env
`WATCHOVER_GO_PARSE_FORMATS` / `_DENY` can still narrow it).

## Remaining failures

None. No format or family has to stay in Python because of a known parity gap. Inputs Go cannot reproduce by design are
still forwarded raw to Python at runtime (`ErrForward`): archives, non-UTF-8 input, side tables, JSON nested deeper
than 900 levels, OTLP attributes with non-string keys, timestamps with a UTC offset carrying microseconds, and (with
`Strict`) dateutil time-zone names other than UTC. Inputs where Python raises are reported as `ErrPyRaise`.

## Per format (whole / live)

| format[:family] | whole | live |
|---|---|---|
| access | 6/6 | 6/6 |
| cef | 4/4 | 4/4 |
| csv | 4/4 | 4/4 |
| grok: | 12/12 | 12/12 |
| json | 5/5 | 5/5 |
| jsonl | 14/14 | 14/14 |
| kv | 4/4 | 4/4 |
| sap | 11/11 | 11/11 |
| syslog | 19/19 | 19/19 |
| text: (no family) | 10/10 | 10/10 |
| text:dotnet, dynamics-ax, dynamics-nav, golang, java, kafka, mysql, nginx, oracle, oracle-ebs, postgres, python, redis, sqlserver | 25/25 | 25/25 |
| w3c | 1/1 | 1/1 |
| winevt | 2/2 | 2/2 |
| **total** | **120/120** | **120/120** |

## Extra checks beyond the checked-in goldens

- Live replays at other batch sizes: goldens regenerated with `scripts/parse_golden.py --batch N --out DIR` for
  N = 1, 7, 13 and 100 (scratch directories, not checked in) and compared with
  `go test ./parse/ -run 'TestGolden$' -parity.golden=DIR -parity.batch=N`: 120/120 whole and live for every N. The
  regenerated whole goldens were identical to the checked-in ones (the Python parsers have not drifted).
- `TestWP3PythonDiff` re-run after the family prefilter change: 0 differences over 19,693 lines, 1,338 record calls and
  127 grok detections.
- `go test -race ./parse/`: passes.

## Families without a golden

`apache`, `zookeeper`, `elasticsearch`, `haproxy`, `postfix`, `sshd`, `iis`, `windows`, `sap` and `dynamics-365` as
*text* families have no golden input of their own (their lines are routed to syslog, w3c, winevt or sap in the corpus),
so `text:<family>` keys for them are not in `allow.txt` and such batches are forwarded to Python. They are covered at the
pattern level by `TestWP3PythonDiff` and `TestLitFilterExact`.

## Review (2026-10-15)

- Re-checked: `TestGolden` 120/120 whole and live; goldens regenerated from Python into a scratch directory are
  identical to the checked-in set; 8 inputs hand-diffed against `zcat` of their goldens. A differential harness that
  emulates the stage decision per batch (hint cache, allow list, sticky) against Python's live loop found no allowed
  batch that differs, over all inputs, 40 random multi-file streams (batch sizes 1 to 120, format switches mid-stream),
  untested families (apache, zookeeper, elasticsearch, haproxy, postfix, sshd, iis, windows, sap, dynamics-365, also as
  a minority of lines or a single line), non-UTF-8 / UTF-16 / BOM / CR / U+2028 / NUL input, zip and gzip payloads, and
  seeded auto and user profiles.
- Found and fixed: user grok patterns (`<home>/grok.json`) were not handled although they change Python's format
  detection (a text batch Python parses with the user pattern was emitted by Go as `text:`); edited parse-path
  scenario constants were not detected; the stage parsed without the dashboard's export; an output write error deleted
  the raw segment; the syslog year was frozen at receiver start. Batches are now forwarded raw for the first three
  (`usergrok`, `scenario`, `noexport`), the raw segment is handed back for the fourth, and the year follows the clock.
- Known residual differences (not fixed): the process-global timestamp format cache (`_format_cache`, ambiguous
  dd/mm vs mm/dd shapes) and learned profiles evolve per process and, in Go, across parallel workers; after a receiver
  restart Go re-detects streams Python still has hints for (the same as a dashboard restart today); an auto profile
  re-learned from an upload in the dashboard is seen by Go only after its own learned copy is evicted.
