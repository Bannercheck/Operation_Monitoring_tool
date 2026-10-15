//go:build pydiff

package parse

// Differential tests of the WP2 ports against the Python source of truth (opt-in: go test -tags pydiff ./parse/
// -run PyDiff). Needs python3 with python-dateutil and the repository's src/ on PYTHONPATH (set here).

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"math/rand"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"watchover/receiver/parse/py"
)

const pydiffProbe = `
import sys, json
sys.setrecursionlimit(10000)
from watchover import normalize as N
def ts(v):
    N._ts_cache.clear(); N._format_cache.clear()
    try:
        d = N.parse_timestamp(v)
    except Exception as e:
        return "RAISE"
    if d is None:
        return "None"
    try:
        off = d.utcoffset()
    except Exception:
        return "BADOFF"
    if off is not None and off.microseconds:
        return "SUBSEC"
    try:
        return d.isoformat()
    except Exception:
        return "BADOFF"
def sev(v):
    N._sev_cache.clear()
    out = []
    for scale in (True, False):
        try: out.append(N.column_severity(v, scale))
        except Exception: out.append("RAISE")
    try: out.append(N.normalize_severity(v))
    except Exception: out.append("RAISE")
    return out
def env(v):
    N._env_cache.clear()
    out = []
    try: out.append(N.normalize_environment(v))
    except Exception: out.append("RAISE")
    try: out.append(N.infer_environment(str(v)))
    except Exception: out.append("RAISE")
    return out
for line in sys.stdin:
    q = json.loads(line)
    kind, v = q["k"], q["v"]
    if kind == "ts": r = ts(v)
    elif kind == "sev": r = sev(v)
    elif kind == "txt": r = N.severity_from_text(v)
    elif kind == "env": r = env(v)
    elif kind == "flat": r = N.flatten(v)
    elif kind == "auto":
        N._ts_cache.clear(); N._format_cache.clear()
        try: r = N.auto_map(v["keys"], v["sample"], v.get("mapping"))
        except Exception: r = "RAISE"
    sys.stdout.write(json.dumps(r, ensure_ascii=False) + "\n")
`

// runPy feeds one JSON query per line to the probe and returns one JSON answer per line.
func runPy(t *testing.T, queries []map[string]any) []string {
	t.Helper()
	dir := t.TempDir()
	script := filepath.Join(dir, "probe.py")
	if err := os.WriteFile(script, []byte(pydiffProbe), 0o644); err != nil {
		t.Fatal(err)
	}
	var in bytes.Buffer
	for _, q := range queries {
		b, err := json.Marshal(q)
		if err != nil {
			t.Fatal(err)
		}
		in.Write(b)
		in.WriteByte('\n')
	}
	src, _ := filepath.Abs("../../src")
	cmd := exec.Command("python3", script)
	cmd.Env = append(os.Environ(), "PYTHONPATH="+src, "TZ=UTC", "PYTHONIOENCODING=utf-8")
	cmd.Stdin = &in
	var out, errb bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errb
	if err := cmd.Run(); err != nil {
		t.Fatalf("python probe: %v\n%s", err, errb.String())
	}
	var res []string
	sc := bufio.NewScanner(&out)
	sc.Buffer(make([]byte, 1<<20), 1<<26)
	for sc.Scan() {
		res = append(res, sc.Text())
	}
	if len(res) != len(queries) {
		t.Fatalf("probe returned %d answers for %d queries", len(res), len(queries))
	}
	return res
}

// goTS renders Go's parse_timestamp result like the probe.
func goTS(v py.Value) string {
	n := NewNormalizer(2026)
	r, err := n.parseTS(v)
	if err != nil {
		return "RAISE"
	}
	if !r.ok {
		return "None"
	}
	switch r.flag {
	case tsBadOffset:
		return "BADOFF"
	case tsSubSecOffset:
		return "SUBSEC"
	}
	return r.dt.ISO()
}

// sameJSON compares two JSON texts by value.
func sameJSON(a, b string) bool {
	var x, y any
	if json.Unmarshal([]byte(a), &x) != nil || json.Unmarshal([]byte(b), &y) != nil {
		return a == b
	}
	return fmt.Sprintf("%#v", x) == fmt.Sprintf("%#v", y)
}

func jsonStr(s string) string {
	b, _ := json.Marshal(s)
	return string(b)
}

// genTimestampish returns n pseudo-random timestamp-like strings (fixed seed).
func genTimestampish(n int) []string {
	rng := rand.New(rand.NewSource(20260922))
	atoms := []string{
		"2026", "26", "09", "9", "22", "31", "12", "13", "00", "24", "60", "99", "1", "0", "7", "123", "1234", "12345",
		"123456", "1234567", "20260922", "202609220917", "20260922091700", "1790068442", "1790068442000",
		"1790068442000000", "1790068442000000000", "003", "5", "500", "0000",
		"-", "/", ".", ",", ":", " ", "  ", "T", "t", "Z", "z", "+", "+03", "+0300", "+03:00", "-05", "+3", "+24:00",
		"+05:30:15", "+03:00:30.5", "(", ")", "'", ";", "_",
		"Sep", "sep", "SEP", "Sept", "September", "Feb", "May", "Mar", "Dec", "Jan", "Eylül", "Eyl", "Ekim",
		"Mon", "Tue", "Fri", "FRI", "Sunday", "AM", "PM", "am", "pm", "a", "p", "of", "at", "on", "th", "st", "h", "m", "s",
		"UTC", "GMT", "EST", "CET", "UTC+3", "GMT-2", "BST", "X", "ABCDEF", "inf", "nan", "noon", "N/A", "INFO", "ALM",
		"é", "ı", "İ", "ſ", "١٢", "²", " ", " ", "\x1c", "x", "foo", "v1.2", "order=231849",
	}
	shapes := []string{
		"2026-09-22T09:14:02", "2026-09-22 09:14:02", "2026-09-22T09:14:02.123Z", "22/09/2026 09:14:05",
		"09/22/2026 09:15:03 PM", "Sep 22 09:14:02", "22-Sep-2026 09:14:02.003", "Tue, 22 Sep 2026 09:16:44 +0300",
		"22/Sep/2026:09:14:02 +0300", "2026.09.22 09:14:02", "09:14:02.003", "09:14:05", "2026-09-22 09:14:31.001 +03",
	}
	out := make([]string, 0, n)
	for len(out) < n {
		var b strings.Builder
		if rng.Intn(3) == 0 { // structured date-ish token sequences (dateutil grammar)
			parts := []func() string{
				func() string { return fmt.Sprint(rng.Intn(32)) },
				func() string { return fmt.Sprintf("%02d", rng.Intn(13)) },
				func() string { return fmt.Sprint(1990 + rng.Intn(50)) },
				func() string { return fmt.Sprint(rng.Intn(100000)) },
				func() string {
					return []string{"Sep", "sept", "JANUARY", "feb", "Mar", "dec", "May", "Jun"}[rng.Intn(8)]
				},
				func() string { return []string{"Mon", "tuesday", "Sun", "FRI"}[rng.Intn(4)] },
				func() string { return fmt.Sprintf("%d:%02d", rng.Intn(25), rng.Intn(61)) },
				func() string { return fmt.Sprintf("%02d:%02d:%02d", rng.Intn(25), rng.Intn(61), rng.Intn(61)) },
				func() string {
					return fmt.Sprintf("%02d:%02d:%02d%s%d", rng.Intn(24), rng.Intn(60), rng.Intn(60), []string{".", ","}[rng.Intn(2)], rng.Intn(10000000))
				},
				func() string { return []string{"AM", "pm", "a", "P"}[rng.Intn(4)] },
				func() string {
					return []string{"UTC", "GMT", "Z", "z", "EST", "+0300", "-05:00", "+3", "+24", "UTC+2", "(UTC)"}[rng.Intn(11)]
				},
				func() string { return []string{"of", "at", "on", "th", "h", "m", "s", "hours", "minutes"}[rng.Intn(9)] },
				func() string { return fmt.Sprintf("%d.%d", rng.Intn(100), rng.Intn(100)) },
			}
			seps := []string{" ", "-", "/", ".", ", ", "T", "", ":", " - "}
			for k := 1 + rng.Intn(5); k > 0; k-- {
				b.WriteString(parts[rng.Intn(len(parts))]())
				if k > 1 {
					b.WriteString(seps[rng.Intn(len(seps))])
				}
			}
			out = append(out, b.String())
			continue
		}
		switch rng.Intn(4) {
		case 0: // mutate a known shape
			s := []rune(shapes[rng.Intn(len(shapes))])
			for k := rng.Intn(3); k >= 0; k-- {
				i := rng.Intn(len(s))
				switch rng.Intn(4) {
				case 0:
					s[i] = []rune("0123456789-:/. TZ+é")[rng.Intn(19)]
				case 1:
					s = append(s[:i], s[i+1:]...)
				case 2:
					s = append(s[:i], append([]rune(atoms[rng.Intn(len(atoms))]), s[i:]...)...)
				case 3:
					s = s[:i+1]
				}
				if len(s) == 0 {
					s = []rune("0")
				}
			}
			b.WriteString(string(s))
		default:
			for k := 1 + rng.Intn(7); k > 0; k-- {
				b.WriteString(atoms[rng.Intn(len(atoms))])
			}
		}
		out = append(out, b.String())
	}
	return out
}

// specTimestampVectors are the probed inputs of P02 sections 3.2-3.7.
var specTimestampVectors = []string{
	"2026-09-22T09:14:02", "2026-09-22 09:14:02", "2026-09-22T09:14:02Z", "2026-09-22T09:14:02z", "2026-09-22T09:14:02.1Z",
	"2026-09-22T09:14:02.12Z", "2026-09-22T09:14:02.1234567Z", "2026-09-22T09:14:02.0000000Z",
	"2026-09-22T09:14:02.123456789+03:00", "2026-09-22T09:14:02.000+03:00", "2026-09-22T09:14:02.5+03:00",
	"2026-09-22T09:14:02+0300", "2026-09-22T09:14:02+03", "2026-09-22T09:14:02-03", "2026-09-22T09:14:02-00:00",
	"2026-09-22T09:14:02+05:30:15", "2026-09-22T09:14:02+03:00:30.5", "2026-09-22T09:14:02.123-0330",
	"2026-09-22T09:14:02,123", "2026-09-22 09:14:02,123", "2026-09-22 09:14:02,1234", "2026-09-22x09:14:02",
	"2026-09-22T09:14:02 +03:00", "2026-09-22 09:14:31.001 +03", "2026-09-22 09:14:31 +03", "2026-09-22T09:14:31 -05",
	"2026-09-22 09:14:31.001+03", "2026-09-22 09:14:02.003 +03:00", "2026-09-22 09:14:02 +0300", "2026-09-22T09:14:02+3",
	"2026-09-22T24:00:00", "2026-09-22T09:14:60", "2026-02-30T09:14:02", "2026-09-22T09:14:02.123.456",
	"2026-09-22T09:14:02Zjunk", "2026-09-22T09:14:02 extra", "2026-09-22T09:14:02+03:00[Europe/Istanbul]",
	"2026-09-22  09:14:02", "2026-09-22T09:14", "2026-09-22 09:14", "2026-09-22T0914", "2026-09-22T091402",
	"2026-09-22T09", "2026-09-22", "2026-9-22 09:14:02", "0001-01-01T00:00:00", "2026-09-22T09:14:02+24:00",
	"2026-09-22é09:14:02", "2026-09-22T0914025", "2026-09-22T09:14:02:5",
	"22/09/2026 09:14:05", "1/2/2026 10:00:00", "01/02/2026 10:00:00", "12/25/2026 10:00:00", "22.09.2026 09:14:02",
	"2026/09/22 09:14:02", "22/Sep/2026:09:14:02 +0300", "22/sep/2026:09:14:02 +03:00", "Sep 22 09:14:02",
	"Sep  2 09:14:02", "SEP 22 09:14:02", "Feb 29 10:00:00", "20260922 091700", "Sep 22 2026 09:14:02",
	"22 Sep 2026 09:14:02.003", "22 Sep 2026 09:14:02", "09/22/2026 09:15:03 AM", "09/22/2026 09:15:03 PM",
	"09/22/2026 21:15:03 PM", "Tue, 22 Sep 2026 09:16:44 +0300", "22-SEP-2026 09:14:02", "22-09-2026 09:14:02",
	"2026.09.22 09:14:02", "202609220917", "20260922T091700Z", "20260922T091700.123Z", "20260922t091700z",
	"1790068442", "1790068442.5", "1790068442.0034567", "1790068442.", "179006844.5", "1790068442000",
	"9999999999999", "1790068503123456789", "1790068503500000000", "1790068442000000", "20261322000000",
	"20260922091700", "²²²²²²²²²²", "١٧٩٠٠٦٨٤٤٢",
	"09:14:02", "09:14:02,123", "09:14", "12:00 PM", "09:14:02.1234567", "22-Sep-2026 09:14:02,123",
	"Sep 22 09:14:02,123", "Sep 22", "September 22 09:14:02", "Sep 22, 2026 9:14:02 AM", "Mon Sep 22 09:14:02 2026",
	"Mon Sep 22 09:14:02 UTC 2026", "Tue, 22 Sep 2026 09:16:44 GMT", "2026-09-22T09:14:02 UTC", "2026-09-22T09:14:02 GMT",
	"2026-09-22T09:14:02 EST", "2026-09-22T09:14:02 CET", "01.02.2026 10:00", "10/09/2026", "10.09.2026", "09/22/2026",
	"20260922", "123456789", "1", "7", "12", "13", "31", "32", "60", "99", "100", "200", "2026", "1999", "1.2.3", "3.5",
	"0", "12345", "12345678", "0.5", "10.0.0.1", "192.168.1.10", "v1.2", "yesterday", "noon", "N/A", "-", "INFO",
	"ALM-00041", "22 Eylül 2026 09:14", "22 Eyl 2026", "22 Ekim 2026", "Sep 22. 2026 09:14:02,123",
	"ALM-00054", "user-01", "Clock drift 11ms on stage-02", "gateway timeout order=231849", "Disk usage 85% on /data (order-01)",
	"Monday", "monday 10:00", "10 h", "10h", "5 m", "m 5", "10:00 UTC+3", "10:00 GMT-2", "10:00 EST+5", "10:00 +05:00 (EST)",
	"10:00 z", "10:00 Z", "10:00 +3 (ABC)", "12 am", "12 pm", "13 pm", "0 am", "Sep of 26", "Sep 22 of 2026",
	"5.99999999999999999999999999999 h", "1.123456789012345678901234567890123 m", "10:5.99999999999999999999999999999",
	"10:0.999999999999999999999999999999999", "12345678901234567890123456789.5 h", "1234567890123456789012345678.5 m",
	"10:1234567890123456789012345678.25", "Sep 22 10:30.5", "22.9.26", "2026.9", "091402.5", "091402", "26-09-22", "09-22-26", "Sep-22-2026", "22-Sep", "Sep/22", "1 Sep 26",
}

func TestPyDiffParseTimestamp(t *testing.T) {
	inputs := append(append([]string(nil), specTimestampVectors...), genTimestampish(120000)...)
	var qs []map[string]any
	for _, s := range inputs {
		qs = append(qs, map[string]any{"k": "ts", "v": s})
	}
	nums := []any{0, 1, true, false, -5, 100.0, 1790068442, 1790068442.5, 1790068442000, 1790068442003.7,
		1000000000001, 1000000000000, 1e12, 253402300799, 253402300800, -62135596800, -62135596801, 1.5e300}
	for _, v := range nums {
		qs = append(qs, map[string]any{"k": "ts", "v": v})
	}
	res := runPy(t, qs)
	bad := 0
	cats := map[string]int{}
	for i, q := range qs {
		var want string
		if err := json.Unmarshal([]byte(res[i]), &want); err != nil {
			t.Fatal(err)
		}
		raw, _ := json.Marshal(q["v"])
		v, err := py.Loads(string(raw))
		if err != nil {
			t.Fatal(err)
		}
		got := goTS(v)
		switch {
		case strings.HasPrefix(want, "0") || strings.HasPrefix(want, "1") || strings.HasPrefix(want, "2") || strings.HasPrefix(want, "9"):
			cats["datetime"]++
		default:
			cats[want]++
		}
		if i < 12 {
			t.Logf("%s -> go %s, python %s", raw, got, want)
		}
		if got != want {
			bad++
			if bad <= 40 {
				t.Errorf("parse_timestamp(%s): go %s, python %s", raw, got, want)
			}
		}
	}
	if bad > 0 {
		t.Errorf("%d/%d mismatches", bad, len(qs))
	}
	t.Logf("%d parse_timestamp inputs compared: %v", len(qs), cats)
}

func genText(n int) []string {
	rng := rand.New(rand.NewSource(7))
	atoms := []string{"error", "ERROR", "Error: ", "Exception: ", "java.lang.NullPointerException: x", "5Error: ", "çError: ",
		"my.Error: ", "_Error: ", "$Error: ", "ExceptİON: ", "warn", "WARNING", "warnings", "INFO", "info", "ınfo", "İNFO",
		"crıtıcal", "CRİTİCAL", "ERRORS", "xINFO", "INFO_x", "foo_ERROR bar", "Severity", "fatal", "panic:", "kernel panicked",
		"segfault", "segfaults", "ORA-01555", "ora-01555", "ORA-١٢٣٤٥", "APP-FND-01234", "TNS-12345", "FRM-1234",
		"Traceback (most recent call last)", "deadlock detected", "Out of memory", "Stack overflow", "BACKUP failed",
		"Login failed", "Assertion failure", "circuit breaker open", "timed out", "timeout", "timeouts", "çtimeout",
		"retry", "retrying", "deferred", "denied", "dенied", "refused", "slow query", "unreachable", "degraded",
		"deprecated", "TİMEOUT", "tımeout", "prod", "production", "prd", "PROD", "prod1", "ıprod", "productıon", "ſtage",
		"stage", "staging", "preprod", "pre-prod", "canary", "sandbox", "live", "lıve", "dev", "development", "qa", "uat",
		"test", "tst", "stg", "DR", "dr", "disaster", "Canli", "canlı", "eu-west-1", "İstanbul-prod", "x-live-y",
		" ", "-", "_", ".", ":", "/", "1", "2", "3", "4", "5", "6", "0", "10", "²", " ", "K", "k", "ok", "e", "w",
		"p1", "Firing", "AUDIT_FAILURE", "notice", "System", "Note", "3.0", "-1", "True", "None",
	}
	out := make([]string, 0, n)
	for len(out) < n {
		var b strings.Builder
		for k := 1 + rng.Intn(5); k > 0; k-- {
			b.WriteString(atoms[rng.Intn(len(atoms))])
			if rng.Intn(3) > 0 {
				b.WriteString([]string{" ", "-", "", "_", ".", "é", "1"}[rng.Intn(7)])
			}
		}
		out = append(out, b.String())
	}
	return out
}

func TestPyDiffSeverityEnv(t *testing.T) {
	texts := genText(30000)
	var qs []map[string]any
	for _, s := range texts {
		qs = append(qs, map[string]any{"k": "txt", "v": s}, map[string]any{"k": "env", "v": s}, map[string]any{"k": "sev", "v": s})
	}
	for _, v := range []any{3, 3.0, true, false, 1, 1.0, 0, 6, 100.0, nil, []any{"a", 1}} {
		qs = append(qs, map[string]any{"k": "sev", "v": v}, map[string]any{"k": "env", "v": v})
	}
	res := runPy(t, qs)
	bad := 0
	for i, q := range qs {
		raw, _ := json.Marshal(q["v"])
		v, err := py.Loads(string(raw))
		if err != nil {
			t.Fatal(err)
		}
		var got string
		switch q["k"] {
		case "txt":
			got = jsonStr(SeverityFromText(v.S))
		case "sev":
			n := NewNormalizer(2026)
			var parts []string
			for _, scale := range []bool{true, false} {
				s, err := n.ColumnSeverity(v, scale)
				if err != nil {
					s = "RAISE"
				}
				parts = append(parts, jsonStr(s))
			}
			s, err := NormalizeSeverity(v)
			if err != nil {
				s = "RAISE"
			}
			parts = append(parts, jsonStr(s))
			got = "[" + strings.Join(parts, ", ") + "]"
		case "env":
			n := NewNormalizer(2026)
			a, err := NormalizeEnvironment(v)
			if err != nil {
				a = "RAISE"
			}
			b, err := n.InferEnvironment(v.Str())
			if err != nil {
				b = "RAISE"
			}
			got = "[" + jsonStr(a) + ", " + jsonStr(b) + "]"
		}
		if !sameJSON(got, res[i]) {
			bad++
			if bad <= 40 {
				t.Errorf("%s(%s): go %s, python %s", q["k"], raw, got, res[i])
			}
		}
	}
	if bad > 0 {
		t.Errorf("%d/%d mismatches", bad, len(qs))
	}
	t.Logf("%d severity/environment queries compared", len(qs))
}

func TestPyDiffAutoMap(t *testing.T) {
	rng := rand.New(rand.NewSource(11))
	keyPool := []string{"ts", "time", "Time", "TIME ", "level", "Level", "severity", "sev", "msg", "message", "host", "app",
		"x_time", "app_time", "a", "b", "c", "d", "n", "m", "Oluşturma Zamanı", "Önem", "İŞLEM", "env", "tags.ortam",
		"status", "source", "s", "t", "name", "", "event.source", "my-host", "x.level"}
	valPool := []any{"2026-09-22T09:14:02Z", "warn", "Prod", "hello world", "1_000", "inf", "12", "13", nil, "", 0, false,
		true, 1.5, "ERROR", "e", "dev", "  staging ", "Sep 22 09:14:02", "1790068442", 1790068442, "x", "user-01",
		"gateway timeout order=231849", "ALM-00054", []any{"a"}, "ok", " warn"}
	var qs []map[string]any
	for c := 0; c < 3000; c++ {
		nk := 1 + rng.Intn(5)
		var keys []string
		seen := map[string]bool{}
		for len(keys) < nk {
			k := keyPool[rng.Intn(len(keyPool))]
			if !seen[k] {
				seen[k] = true
				keys = append(keys, k)
			}
		}
		var sample []any
		for r := rng.Intn(6); r > 0; r-- {
			row := map[string]any{}
			for _, k := range keys {
				if rng.Intn(5) > 0 {
					row[k] = valPool[rng.Intn(len(valPool))]
				}
			}
			sample = append(sample, row)
		}
		q := map[string]any{"keys": keys, "sample": sample}
		if rng.Intn(6) == 0 {
			q["mapping"] = map[string]any{"timestamp": keys[rng.Intn(len(keys))], "message": "zz"}
		}
		qs = append(qs, map[string]any{"k": "auto", "v": q})
	}
	res := runPy(t, qs)
	bad := 0
	for i, q := range qs {
		raw, _ := json.Marshal(q["v"])
		v, err := py.Loads(string(raw))
		if err != nil {
			t.Fatal(err)
		}
		var keys []string
		kv, _ := v.M.Get("keys")
		for _, k := range kv.L {
			keys = append(keys, k.S)
		}
		var sample []*py.Dict
		sv, _ := v.M.Get("sample")
		for _, r := range sv.L {
			sample = append(sample, r.M)
		}
		var mp *Mapping
		if m, ok := v.M.Get("mapping"); ok {
			mp = NewMapping(m.M)
		}
		roles, err := NewNormalizer(2026).AutoMap(keys, sample, mp)
		got := "RAISE"
		if err == nil {
			var parts []string
			for r := 0; r < NumRoles; r++ {
				val := "null"
				if k, ok := roles.Get(r); ok {
					val = jsonStr(k)
				}
				parts = append(parts, jsonStr(RoleNames[r])+": "+val)
			}
			got = "{" + strings.Join(parts, ", ") + "}"
		} else {
			got = jsonStr("RAISE")
		}
		if !sameJSON(got, res[i]) {
			bad++
			if bad <= 20 {
				t.Errorf("auto_map(%s):\n go %s\n py %s", raw, got, res[i])
			}
		}
	}
	if bad > 0 {
		t.Errorf("%d/%d mismatches", bad, len(qs))
	}
	_ = fmt.Sprint
}
