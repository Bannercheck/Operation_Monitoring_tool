package parse

// WP2 unit tests (docs/go-parse/PLAN.md section 3, WP2). Expected values were produced by the Python source of truth
// (see wp2_tables_test.go); the opt-in differential tests in wp2_pydiff_test.go compare ~200k further inputs.

import (
	"errors"
	"fmt"
	"math/big"
	"strings"
	"testing"

	"watchover/receiver/parse/py"
)

func tsString(n *Normalizer, v py.Value) string {
	r, err := n.parseTS(v)
	switch {
	case err != nil && errors.Is(err, ErrPyRaise):
		return "RAISE"
	case err != nil:
		return "ERR " + err.Error()
	case !r.ok:
		return "None"
	case r.flag == tsBadOffset:
		return "BADOFF"
	case r.flag == tsSubSecOffset:
		return "SUBSEC"
	}
	return r.dt.ISO()
}

func TestParseTimestampTable(t *testing.T) {
	for _, c := range wp2TimestampTable {
		if got := tsString(NewNormalizer(2026), py.S(c.in)); got != c.want {
			t.Errorf("parse_timestamp(%q) = %s, want %s", c.in, got, c.want)
		}
	}
}

func TestFastFormatsAll(t *testing.T) {
	if len(wp2FastFormatTable) != len(fastFormats) {
		t.Fatalf("table has %d rows for %d formats", len(wp2FastFormatTable), len(fastFormats))
	}
	for i, c := range wp2FastFormatTable {
		n := NewNormalizer(2026)
		if got := tsString(n, py.S(c.in)); got != c.want {
			t.Errorf("format %d %q: parse_timestamp(%q) = %s, want %s", i+1, fastFormats[i], c.in, got, c.want)
		}
		// Python's _format_cache after the call (-1: fromisoformat or the digit branch took the string)
		wantCache := [...]int{-1, -1, -1, -1, -1, 5, 6, 7, 8, 9, 10, 11, -1, 13, 14, 15, 16, 17, 18, 19, -1, -1, -1, 23, 24, 25}[i]
		fi, ok := n.formatCache[shape(c.in)]
		if (wantCache < 0 && ok) || (wantCache >= 0 && (!ok || fi != wantCache)) {
			t.Errorf("format %d: cache holds %d (%v), want %d", i+1, fi, ok, wantCache)
		}
		if _, ok := strptime(c.in, i); !ok {
			t.Errorf("strptime(%q, %q) failed", c.in, fastFormats[i])
		}
	}
}

func TestEpochRounding(t *testing.T) {
	n := NewNormalizer(2026)
	cases := map[string]string{
		"1790068442":          "2026-09-22T09:14:02+00:00",        // 10 digits, exact int
		"9999999999999":       "2286-11-20T17:46:39.999001+00:00", // 13: correctly rounded quotient, then half-even micros
		"1790068442000000":    "2026-09-22T09:14:02+00:00",        // 16
		"1790068503500000000": "2026-09-22T09:15:03.500000+00:00", // 19
		"1790068503123456789": "2026-09-22T09:15:03.123457+00:00",
		"20260922091700":      "2026-09-22T09:17:00+00:00", // 14 -> %Y%m%d%H%M%S
		"1790068442.0034567":  "2026-09-22T09:14:02.003457+00:00",
	}
	for in, want := range cases {
		if got := tsString(n, py.S(in)); got != want {
			t.Errorf("parse_timestamp(%q) = %s, want %s", in, got, want)
		}
	}
	nums := []struct {
		v    py.Value
		want string
	}{
		{py.Int(0), "1970-01-01T00:00:00+00:00"}, {py.B(true), "1970-01-01T00:00:01+00:00"},
		{py.B(false), "1970-01-01T00:00:00+00:00"}, {py.Int(-5), "1969-12-31T23:59:55+00:00"},
		{py.F(100.0), "1970-01-01T00:01:40+00:00"}, {py.F(1790068442.5), "2026-09-22T09:14:02.500000+00:00"},
		{py.Int(1790068442000), "2026-09-22T09:14:02+00:00"}, {py.F(1790068442003.7), "2026-09-22T09:14:02.003700+00:00"},
		{py.Int(1000000000001), "2001-09-09T01:46:40.001000+00:00"}, {py.F(1790068503.1), "2026-09-22T09:15:03.100000+00:00"},
		{py.Int(1000000000000), "RAISE"}, {py.Int(1790068503500000000), "RAISE"},
		{py.BigInt(new(big.Int).Lsh(big.NewInt(1), 2000)), "RAISE"},
	}
	for _, c := range nums {
		if got := tsString(n, c.v); got != c.want {
			t.Errorf("parse_timestamp(%s) = %s, want %s", c.v.Repr(), got, c.want)
		}
	}
}

func TestParseTimestampExceptions(t *testing.T) {
	// P02 section 3.10: these raise in Python (the whole ingest fails) -> ErrPyRaise, and are not cached
	for _, in := range []string{"20261322000000", "²²²²²²²²²²", "²²²²²²²²²²²²²", "20261399T999999"} {
		n := NewNormalizer(2026)
		_, _, err := n.ParseTimestamp(py.S(in))
		if !errors.Is(err, ErrPyRaise) {
			t.Errorf("ParseTimestamp(%q) err = %v, want ErrPyRaise", in, err)
		}
		if _, ok := n.tsCache[in]; ok {
			t.Errorf("%q cached after an exception", in)
		}
	}
	// a >= 24h dateutil offset: returned by parse_timestamp (auto_map counts it) but fatal downstream
	n := NewNormalizer(2026)
	if _, _, err := n.ParseTimestamp(py.S("2026-09-22T09:14:02+24:00")); !errors.Is(err, ErrPyRaise) {
		t.Errorf("+24:00: err = %v, want ErrPyRaise", err)
	}
	if _, _, err := n.ParseTimestamp(py.S("2026-09-22T09:14:02+03:00:30.5")); !errors.Is(err, ErrForward) {
		t.Errorf("sub-second offset: err = %v, want ErrForward", err)
	}
	if _, _, err := n.ParseTimestamp(py.F(nanF())); !errors.Is(err, ErrPyRaise) {
		t.Errorf("nan: err = %v, want ErrPyRaise", err)
	}
	if _, _, err := n.ParseTimestamp(py.S("10:5.12345678901234567890123456789 m")); err != nil {
		t.Errorf("29-digit fraction: %v", err)
	}
	if _, _, err := n.ParseTimestamp(py.S("12345678901234567890123456789.5 h")); !errors.Is(err, ErrPyRaise) {
		t.Errorf("Decimal DivisionImpossible: err = %v, want ErrPyRaise", err)
	}
}

func nanF() float64 { z := 0.0; return z / z }

func TestStrictHostTimeZone(t *testing.T) {
	n := NewNormalizer(2026)
	if dt, ok, err := n.ParseTimestamp(py.S("2026-09-22T09:14:02 EST")); err != nil || !ok || dt.ISO() != "2026-09-22T09:14:02+00:00" {
		t.Errorf("non-strict EST: %v %v %v", dt.ISO(), ok, err)
	}
	n.Strict = true
	if _, _, err := n.ParseTimestamp(py.S("2026-09-22T09:14:02 EST")); !errors.Is(err, ErrForward) {
		t.Errorf("strict EST: err = %v, want ErrForward", err)
	}
	if dt, _, err := n.ParseTimestamp(py.S("2026-09-22T09:14:02 UTC")); err != nil || dt.ISO() != "2026-09-22T09:14:02+00:00" {
		t.Errorf("strict UTC: %v %v", dt.ISO(), err)
	}
}

func TestFormatCacheOrderDependence(t *testing.T) {
	n := NewNormalizer(2026)
	if got := tsString(n, py.S("01/02/2026 10:00:00")); got != "2026-02-01T10:00:00+00:00" {
		t.Errorf("fresh d/m: %s", got)
	}
	n = NewNormalizer(2026)
	tsString(n, py.S("12/25/2026 10:00:00")) // only %m/%d/%Y fits -> cached for the shape
	if got := tsString(n, py.S("01/02/2026 10:00:00")); got != "2026-01-02T10:00:00+00:00" {
		t.Errorf("after m/d: %s", got)
	}
	n = NewNormalizer(2026)
	if got := tsString(n, py.S("09/22/2026 09:15:03 PM")); got != "2026-09-22T21:15:03+00:00" {
		t.Errorf("fresh %%I %%p: %s", got)
	}
	n = NewNormalizer(2026)
	tsString(n, py.S("09/22/2026 21:15:03 PM")) // %H...%p cached
	if got := tsString(n, py.S("09/22/2026 09:15:03 PM")); got != "2026-09-22T09:15:03+00:00" {
		t.Errorf("after %%H %%p: %s", got)
	}
	// a failing cached format is not rewritten until the loop finds another one
	n = NewNormalizer(2026)
	n.formatCache[shape("2026-09-22 09:14")] = 0
	if got := tsString(n, py.S("2026-9-22 09:14")); got != "2026-09-22T09:14:00+00:00" || n.formatCache[shape("2026-9-22 09:14")] != 5 {
		t.Errorf("loop after cached miss: %s, cache %d", got, n.formatCache[shape("2026-9-22 09:14")])
	}
}

func TestTSCacheClearAndYear(t *testing.T) {
	n := NewNormalizer(2026)
	for i := 0; i < tsCacheMax; i++ {
		n.tsCache[fmt.Sprint("k", i)] = tsResult{}
	}
	tsString(n, py.S("Sep 22 09:14:02"))
	if len(n.tsCache) != 1 {
		t.Errorf("cache not cleared at %d: len %d", tsCacheMax, len(n.tsCache))
	}
	// year injection: %b %d %H:%M:%S and dateutil defaults use Normalizer.Year
	n = NewNormalizer(2031)
	if got := tsString(n, py.S("Sep 22 09:14:02")); got != "2031-09-22T09:14:02+00:00" {
		t.Errorf("syslog year: %s", got)
	}
	if got := tsString(n, py.S("09:14:02.003")); got != "2031-01-01T09:14:02.003000+00:00" {
		t.Errorf("dateutil default year: %s", got)
	}
	if got := tsString(n, py.S("80")); got != "2080-01-01T00:00:00+00:00" { // 1980 when the year is 2026
		t.Errorf("two-digit year pivot around 2031: %s", got)
	}
	if got := tsString(NewNormalizer(2026), py.S("80")); got != "1980-01-01T00:00:00+00:00" {
		t.Errorf("two-digit year pivot around 2026: %s", got)
	}
	// Feb 29: strptime (year 1900) rejects it, dateutil in a non-leap year too; a leap year accepts it
	if got := tsString(NewNormalizer(2026), py.S("Feb 29 10:00:00")); got != "None" {
		t.Errorf("Feb 29 in 2026: %s", got)
	}
	if got := tsString(NewNormalizer(2028), py.S("Feb 29 10:00:00")); got != "2028-02-29T10:00:00+00:00" {
		t.Errorf("Feb 29 in 2028: %s", got)
	}
	// the memo freezes the first result even if the year changes later
	n = NewNormalizer(2026)
	tsString(n, py.S("Sep 22 09:14:02"))
	n.Year = 2027
	if got := tsString(n, py.S("Sep 22 09:14:02")); got != "2026-09-22T09:14:02+00:00" {
		t.Errorf("memo: %s", got)
	}
}

func TestDateutilFreeText(t *testing.T) {
	free := []string{"ALM-00054", "user-01", "Clock drift 11ms on stage-02", "gateway timeout order=231849",
		"Disk usage 85% on /data (order-01)", "yesterday", "noon", "N/A", "INFO", "10.0.0.1", "192.168.1.10", "v1.2",
		"22 Eylül 2026 09:14", "checkout-api", "GET /api/orders 200", "connection reset by peer"}
	for _, s := range free {
		if got := tsString(NewNormalizer(2026), py.S(s)); got != "None" {
			t.Errorf("parse_timestamp(%q) = %s, want None", s, got)
		}
	}
	shapes := map[string]string{
		"123456789": "1234-05-06T07:08:09+00:00", "1.2.3": "2003-01-02T00:00:00+00:00", "3.5": "2026-01-03T00:00:00+00:00",
		"09:14:02.003": "2026-01-01T09:14:02.003000+00:00", "09:14:05": "2026-01-01T09:14:05+00:00",
		"22-Sep-2026 09:14:02.003": "2026-09-22T09:14:02.003000+00:00", "Mon Sep 22 09:14:02 UTC 2026": "2026-09-22T09:14:02+00:00",
		"10/09/2026": "2026-09-10T00:00:00+00:00", "09/22/2026": "2026-09-22T00:00:00+00:00",
	}
	for in, want := range shapes {
		if got := tsString(NewNormalizer(2026), py.S(in)); got != want {
			t.Errorf("parse_timestamp(%q) = %s, want %s", in, got, want)
		}
	}
	if got := duSplit("Sep.20.2009 4:30:21.447 1,5 a.m."); strings.Join(got, "|") != "Sep|.|20|.|2009| |4|:|30|:|21.447| |1|,|5| |a|.|m|." {
		t.Errorf("_timelex.split = %q", got)
	}
}

func TestSeverity(t *testing.T) {
	n := NewNormalizer(2026)
	col := []struct {
		v            py.Value
		scale, plain string
	}{
		{py.S("5"), "CRITICAL", "INFO"}, {py.S("4"), "ERROR", "WARN"}, {py.S("3"), "WARN", "ERROR"}, {py.S("2"), "INFO", "CRITICAL"},
		{py.S("1"), "DEBUG", "CRITICAL"}, {py.S("0"), "CRITICAL", "CRITICAL"}, {py.S("6"), "INFO", "INFO"},
		{py.Int(3), "WARN", "ERROR"}, {py.S(" Warning "), "WARN", "WARN"}, {py.S("E"), "ERROR", "ERROR"},
		{py.S("p1"), "CRITICAL", "CRITICAL"}, {py.S("AUDIT_FAILURE"), "WARN", "WARN"}, {py.S("Firing"), "ERROR", "ERROR"},
		{py.S("notice"), "INFO", "INFO"}, {py.S("Note"), "INFO", "INFO"}, {py.S("System"), "INFO", "INFO"},
		{py.S("crıtıcal"), "CRITICAL", "CRITICAL"}, {py.S("CRİTİCAL"), "INFO", "INFO"}, {py.S("-1"), "INFO", "INFO"},
	}
	for _, c := range col {
		a, _ := n.ColumnSeverity(c.v, true)
		b, _ := n.ColumnSeverity(c.v, false)
		if a != c.scale || b != c.plain {
			t.Errorf("column_severity(%s) = %s/%s, want %s/%s", c.v.Repr(), a, b, c.scale, c.plain)
		}
	}
	// 1, 1.0 and True share a memo key (Python hash equality)
	n = NewNormalizer(2026)
	if s, _ := n.ColumnSeverity(py.B(true), true); s != "INFO" {
		t.Errorf("fresh True = %s", s)
	}
	n = NewNormalizer(2026)
	n.ColumnSeverity(py.Int(1), true)
	for _, v := range []py.Value{py.B(true), py.F(1.0)} {
		if s, _ := n.ColumnSeverity(v, true); s != "DEBUG" {
			t.Errorf("%s after 1 = %s, want DEBUG", v.Repr(), s)
		}
	}
	if s, _ := NewNormalizer(2026).ColumnSeverity(py.F(3.0), true); s != "INFO" {
		t.Errorf("fresh 3.0 = %s", s)
	}
	if _, err := NormalizeSeverity(py.S("²")); !errors.Is(err, ErrPyRaise) {
		t.Errorf("isdigit but not int(): err = %v", err)
	}
	text := map[string]string{
		"5Error: x": "ERROR", "foo_ERROR bar": "ERROR", "INFO_x": "INFO", "çError: x": "ERROR", "ERRORS": "INFO",
		"xINFO": "INFO", "ınf ready": "INFO", "crıtıcal disk": "CRITICAL", "WARNING: low": "WARN", "WARNINGS": "INFO",
		"java.lang.NullPointerException: x": "ERROR", "my.Error: x": "ERROR", "çtimeout": "INFO", "timeout": "WARN",
		"retrying now": "WARN", "kernel panicked": "ERROR", "ORA-01555 snapshot": "ERROR", "ora-١٢٣٤٥": "ERROR",
		"tımeout": "WARN", "connection refused": "WARN", "all good": "INFO", "ok": "INFO", "ab_Exception: x": "ERROR",
		"éError: x": "ERROR", "1Exception: x": "INFO", "x.yException: z": "ERROR",
	}
	for in, want := range text {
		if got := SeverityFromText(in); got != want {
			t.Errorf("severity_from_text(%q) = %s, want %s", in, got, want)
		}
	}
}

func TestEnvironment(t *testing.T) {
	norm := map[string]string{
		"Production": "prod", "PRD": "prod", "  Staging ": "staging", "prod-eu": "prod", "prod_eu": "prod",
		"x-live-y": "prod", "İstanbul-prod": "prod", "preprod": "staging", "pre-prod": "staging", "prod1": "prod1",
		"eu-west-1": "eu-west-1", "DR-site": "dr-site", "disaster": "dr", "dr": "dr", "Canli": "prod", "canlı": "canlı",
		strings.Repeat("a", 30): strings.Repeat("a", 20), "0": "0", "": "",
	}
	for in, want := range norm {
		if got, err := NormalizeEnvironment(py.S(in)); err != nil || got != want {
			t.Errorf("normalize_environment(%q) = %q, %v; want %q", in, got, err, want)
		}
	}
	for v, want := range map[string]py.Value{"1.5": py.F(1.5), "true": py.B(true)} {
		if got, _ := NormalizeEnvironment(want); got != v {
			t.Errorf("normalize_environment(%s) = %q", want.Repr(), got)
		}
	}
	for _, in := range []string{"productıon", "ſtage", "lıve-1"} {
		if _, err := NormalizeEnvironment(py.S(in)); !errors.Is(err, ErrPyRaise) {
			t.Errorf("normalize_environment(%q): err = %v, want KeyError", in, err)
		}
	}
	n := NewNormalizer(2026)
	infer := []struct {
		in   []string
		want string
	}{
		{[]string{"prd-api-01", "", ""}, "prod"}, {[]string{"web01", "checkout", "app-prod.log"}, "prod"},
		{[]string{"PROD_DB", "", ""}, "prod"}, {[]string{"db.prod.local", "", ""}, "prod"}, {[]string{"ıprod", "", ""}, ""},
		{[]string{"Kprod", "", ""}, ""}, {[]string{"prod1", "", ""}, ""}, {[]string{"deploy to staging done"}, "staging"},
		{[]string{"a", "b", "c", "dev box"}, "dev"},
	}
	for _, c := range infer {
		if got, err := n.InferEnvironment(c.in...); err != nil || got != c.want {
			t.Errorf("infer_environment(%q) = %q, %v; want %q", c.in, got, err, c.want)
		}
	}
	if _, err := n.InferEnvironment("lıve-1", "", ""); !errors.Is(err, ErrPyRaise) {
		t.Errorf("infer_environment KeyError: %v", err)
	}
}

func dictOf(kv ...any) *py.Dict {
	d := py.NewDict(len(kv) / 2)
	for i := 0; i < len(kv); i += 2 {
		var v py.Value
		switch x := kv[i+1].(type) {
		case nil:
			v = py.Null
		case string:
			v = py.S(x)
		case int:
			v = py.Int(int64(x))
		case py.Value:
			v = x
		}
		d.Set(kv[i].(string), v)
	}
	return d
}

func rolesString(r Roles) string {
	var b strings.Builder
	for i := 0; i < NumRoles; i++ {
		if k, ok := r.Get(i); ok {
			fmt.Fprintf(&b, "%s=%q ", RoleNames[i], k)
		}
	}
	return strings.TrimSpace(b.String())
}

func TestAutoMap(t *testing.T) {
	n := NewNormalizer(2026)
	cases := []struct {
		keys   []string
		sample []*py.Dict
		mp     *Mapping
		want   string
	}{
		{[]string{"severity", "level", "msg"}, []*py.Dict{dictOf("severity", "", "level", "ERROR"), dictOf("severity", "WARN", "level", "ERROR")},
			nil, `severity="level" message="msg"`},
		{[]string{"app_time", "x"}, nil, nil, `timestamp="app_time"`},
		{[]string{"a", "b", "c", "d"}, repeat(dictOf("a", "2026-09-22T09:14:02Z", "b", "warn", "c", "Prod", "d", "hello world"), 5),
			nil, `timestamp="a" severity="b" service="c" message="d"`},
		{[]string{"n", "m"}, []*py.Dict{dictOf("n", "1_000", "m", "inf")}, nil, `severity="m"`},
		{[]string{"a", "b"}, []*py.Dict{dictOf("a", "12", "b", "x"), dictOf("a", "13", "b", nil)}, nil, `message="b"`},
		{[]string{"Level", "level", "msg"}, nil, nil, `severity="level" message="msg"`},                                // duplicate normalised keys: last wins
		{[]string{" LEVEL ", "Oluşturma Zamanı", "Önem"}, nil, nil, `timestamp="Oluşturma Zamanı" severity=" LEVEL "`}, // padded / Turkish keys
		{[]string{"ts", "msg"}, nil, NewMapping(dictOf("timestamp", "msg", "message", "nope")), `timestamp="msg" message="msg"`},
		{[]string{"x", "y"}, []*py.Dict{dictOf("x", "", "y", "")}, nil, `message="x"`},              // all-empty columns are candidates (length 0)
		{[]string{"", "z"}, []*py.Dict{dictOf("", "Sep 22 09:14:02", "z", "1")}, nil, `message=""`}, // "" is falsy: not a timestamp, but message has no truthiness test
	}
	for _, c := range cases {
		got, err := n.AutoMap(c.keys, c.sample, c.mp)
		if err != nil {
			t.Fatal(err)
		}
		if s := rolesString(got); s != c.want {
			t.Errorf("auto_map(%q) = %s, want %s", c.keys, s, c.want)
		}
	}
	// the value phase propagates parse_timestamp exceptions
	if _, err := NewNormalizer(2026).AutoMap([]string{"x"}, []*py.Dict{dictOf("x", 1790068503500000000)}, nil); !errors.Is(err, ErrPyRaise) {
		t.Errorf("auto_map with an out-of-range epoch int: err = %v, want ErrPyRaise", err)
	}
	if normKey("İŞLEM") != "islem" || normKey("Oluşturma Zamanı") != "olusturma zamani" || normKey(" LEVEL ") != "level" {
		t.Errorf("normKey: %q %q %q", normKey("İŞLEM"), normKey("Oluşturma Zamanı"), normKey(" LEVEL "))
	}
}

func repeat(d *py.Dict, k int) []*py.Dict {
	out := make([]*py.Dict, k)
	for i := range out {
		out[i] = d
	}
	return out
}

func TestFlatten(t *testing.T) {
	v, err := py.Loads(`{"a": {"b": {"c": 1}, "d": [{"x": 1}]}, "a.b": {"c": 2}, "e": {}, "f": null}`)
	if err != nil {
		t.Fatal(err)
	}
	got, _ := py.Dumps(py.MapOf(Flatten(v.M, "")), false)
	if want := `{"a.b.c": 2, "a.d": [{"x": 1}], "f": null}`; got != want {
		t.Errorf("flatten = %s, want %s", got, want)
	}
	got, _ = py.Dumps(py.MapOf(Flatten(v.M, "p.")), false)
	if want := `{"p.a.b.c": 2, "p.a.d": [{"x": 1}], "p.f": null}`; got != want {
		t.Errorf("flatten prefix = %s", got)
	}
}

func TestProfiles(t *testing.T) {
	if got := Signature("kv", []string{"ts", "level", "msg", "service"}); got != wp2SigKV {
		t.Errorf("Signature = %s, want %s", got, wp2SigKV)
	}
	s := NewMemStore()
	keys := []string{"ts", "level", "msg", "service"}
	var roles Roles
	if s.Remember("kv", keys, roles, "a.log", "auto"); s.Lookup("kv", keys) != nil {
		t.Error("empty mapping remembered")
	}
	roles.Set(RoleTimestamp, "ts")
	roles.Set(RoleSeverity, "level")
	roles.Set(RoleHost, "") // falsy roles are dropped
	s.Remember("kv", nil, roles, "a.log", "auto")
	if s.Lookup("kv", nil) != nil {
		t.Error("no keys must not be remembered")
	}
	s.Remember("kv", keys, roles, "a.log", "auto")
	p := s.Lookup("kv", []string{"service", "msg", "level", "ts"})
	if p == nil || p.ID != wp2SigKV || p.Source != "auto" {
		t.Fatalf("lookup = %+v", p)
	}
	if d, _ := py.Dumps(py.MapOf(p.Mapping.Dict()), false); d != `{"timestamp": "ts", "severity": "level"}` {
		t.Errorf("mapping = %s", d)
	}
	// a user profile is never overwritten by an auto one
	var user Roles
	user.Set(RoleMessage, "msg")
	s.Remember("kv", keys, user, "b.log", "user")
	s.Remember("kv", keys, roles, "c.log", "auto")
	if d, _ := py.Dumps(py.MapOf(s.Lookup("kv", keys).Mapping.Dict()), false); d != `{"message": "msg"}` {
		t.Errorf("user mapping overwritten: %s", d)
	}
}
