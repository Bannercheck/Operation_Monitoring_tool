package parse

import (
	"errors"
	"math"
	"math/big"
	"os"
	"reflect"
	"strings"
	"testing"

	"watchover/receiver/parse/py"
)

// Literal escapes: "\U000000a0" etc. are spelled with \U so no tool rewrites them.

func TestNotLevel(t *testing.T) {
	cases := map[string]bool{
		"INFO x": false, "info": false, "ınfo x": false, "İNFO x": false, "INFOx": true, "INF": false, "INF:": false,
		"WARNING x": false, "WARNINGS": true, "WARN-1": false, "ERRx": true, "ERR": false, "ERROR:": false,
		"ſevere x": false, "DBG_x": true, "hostname": true, "notice\U000000a0x": false, "INFOé": true,
		"CRIT\U00000301": false, // combining mark is not \w
		"VERBOSE":        false, "verbosex": true, "": true,
	}
	for s, want := range cases {
		if got := notLevel(s); got != want {
			t.Errorf("notLevel(%q) = %v, want %v", s, got, want)
		}
	}
}

func TestDetectorRegexes(t *testing.T) {
	m := syslogMatch("<13>Jan  1 00:00:00 host1 prog[12]: hello")
	if m == nil {
		t.Fatal("syslog match")
	}
	ln := "<13>Jan  1 00:00:00 host1 prog[12]: hello"
	names := syslogRE.SubexpNames()
	got := map[string]string{}
	for i, n := range names {
		if n != "" && m[2*i] >= 0 {
			got[n] = ln[m[2*i]:m[2*i+1]]
		}
	}
	want := map[string]string{"pri": "13", "ts": "Jan  1 00:00:00", "host": "host1", "prog": "prog", "pid": "12", "msg": "hello"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("syslog captures = %v", got)
	}
	if syslogMatch("Jan  1 00:00:00 INFO prog: x") != nil || syslogNoProgMatch("Jan  1 00:00:00 ınfo x") != nil {
		t.Error("level word as host must not match")
	}
	if syslogMatch("2026-01-01T00:00:00Z host prog: x") == nil || syslogNoProgMatch("Oct 11 22:14:15 fw01 1,2,3") == nil {
		t.Error("ISO / noprog")
	}
	if syslogMatch("Jan  \U00000661 \U00000660\U00000660:\U00000660\U00000660:\U00000660\U00000660 h p: x") == nil {
		t.Error("Unicode digits are \\d")
	}
	if syslogMatch("Jan  1 00:00:00\U000000a0host prog: x") == nil {
		t.Error("U+00A0 is \\s")
	}
	yes := map[string][]string{
		"rfc5424": {"<34>1 2026-10-11T22:14:15.003Z host app 123 ID47 - msg", "<34>1 ts h a p m [x a=\"1\"][y] m"},
		"cef":     {"CEF:0|a|b|1|2|n|3|k=v", "Jan 1 host CEF:0|x", "LEEF:1.0|a|b|", "LEEF:2|x"},
		"clf":     {`1.2.3.4 - - [01/Jan/2026:00:00:00 +0000] "GET`},
		"alb":     {"https 2026-01-01T00:00:00.123Z app/x "},
		"envoy":   {`[2026-01-01T00:00:00.000Z] "GET /x HTTP/1.1" 200 - `},
		"winevt":  {"<Event xmlns='x'>", "  <Event  a='1' xmlns=", "\x1c<Event-x xmlns="},
		"w3c":     {"#Fields: date", "#Fields:\tx"},
		"cri":     {"2026-01-01T00:00:00.1Z stdout F x", "2026-01-01T00:00:00Z stderr P "},
	}
	no := map[string][]string{
		"rfc5424": {"<34>2 ts h a p m - x"},
		"cef":     {"xCEF:0|a", "CEF:x|"},
		"clf":     {`1.2.3.4 - - [1/Jan/2026:00:00:00 +0000] "GET`},
		"alb":     {"http 2026-01-01T00:00:00Z x "},
		"envoy":   {`[2026-01-01T00:00:00Z] "get / x" 200 - `},
		"winevt":  {"<Event\U000000e9 xmlns=", "<Event>xmlns=", "<Events xmlns=", "x<Event xmlns="},
		"w3c":     {"#Fields:x", " #Fields: x"},
		"cri":     {"2026-01-01T00:00:00 stdout F x"},
	}
	res := map[string]func(string) bool{"rfc5424": rfc5424RE.MatchString, "cef": cefRE.MatchString, "clf": clfRE.MatchString,
		"alb": albRE.MatchString, "envoy": envoyRE.MatchString, "winevt": winevtRE.MatchString, "w3c": w3cRE.MatchString,
		"cri": criLineRE.MatchString}
	for k, ls := range yes {
		for _, l := range ls {
			if !res[k](l) {
				t.Errorf("%s should match %q", k, l)
			}
		}
	}
	for k, ls := range no {
		for _, l := range ls {
			if res[k](l) {
				t.Errorf("%s should not match %q", k, l)
			}
		}
	}
	if n := len(kvRE.FindAllStringIndex(`a=1 b="x \"y\"" c.d-e@f=g`, -1)); n != 3 {
		t.Errorf("kv findall = %d", n)
	}
}

func TestCsvReader(t *testing.T) {
	recs, err := csvReadLines([]string{`a,"b,c`, `d,e`, `f`}, ',')
	if err != nil || !reflect.DeepEqual(recs, [][]string{{"a", "b,cd,ef"}}) {
		t.Errorf("quoted across lines = %q %v", recs, err)
	}
	recs, _ = csvReadLines([]string{`a,"b"x,c`, `"q""q",z`}, ',')
	if !reflect.DeepEqual(recs, [][]string{{"a", "bx", "c"}, {`q"q`, "z"}}) {
		t.Errorf("strict=False = %q", recs)
	}
	recs, _ = csvReadLines([]string{"", "a"}, ',')
	if !reflect.DeepEqual(recs, [][]string{{}, {"a"}}) {
		t.Errorf("empty line = %q", recs)
	}
	recs, _ = csvReadLines([]string{`a,""`, `"`}, ',')
	if !reflect.DeepEqual(recs, [][]string{{"a", ""}, {""}}) {
		t.Errorf("empty quoted = %q", recs)
	}
	ok := strings.Repeat("x", csvFieldLimit) + ",a"
	if d, _ := DetectDelimiter([]string{ok}); d != ',' {
		t.Errorf("131072-char field: delimiter %q", d)
	}
	if d, _ := DetectDelimiter([]string{"x" + ok}); d != 0 {
		t.Errorf("131073-char field: delimiter %q, want none (csv.Error)", d)
	}
	if d, _ := DetectDelimiter([]string{"a\tb", "c\td\te", "f\tg"}); d != '\t' {
		t.Errorf("tab = %q", d)
	}
	if d, _ := DetectDelimiter([]string{"a,b", "c,d,e", "f,g,h,i"}); d != 0 {
		t.Errorf("three distinct counts = %q", d)
	}
}

func TestDetectFormat(t *testing.T) {
	cases := []struct {
		name, text string
		format     string
		conf       float64
	}{
		{"empty", "", "text", 0},
		{"ws", "  \n\t\n", "text", 0},
		{"jsonarr", `[{"a": 1}, {"b": 2}]`, "json", 0.95},
		{"jsonobj1", `{"a": 1}`, "json", 0.95},
		{"jsonobjmulti", "{\"a\":\n 1}", "json", 0.95},
		{"jsonl", "{\"a\": 1}\n{\"a\": NaN}\n{\"b\": \"x\"}\n", "jsonl", 1.0},
		{"jsonl_nbsp", "\U000000a0{\"a\": 1}\n\U000000a0{\"a\": 2}\n", "text", 0.5},
		{"syslog", "Jan  1 00:00:00 host1 prog[12]: hello\nJan  1 00:00:01 host1 prog: world\n", "syslog", 1.0},
		{"syslog12", "Jan  1 00:00:00 host1 prog: a\nJan  1 00:00:01 host1 prog: b\nJan  1 00:00:02 host1 prog: c\nzzz\nyyy\n", "syslog", 1.2},
		{"syslog_level_host", "Jan  1 00:00:00 INFO prog: a\nJan  1 00:00:01 \U00000131nfo prog: b\nJan  1 00:00:02 info2 prog: c\n", "syslog", 0.67},
		{"csv", "name,value\nfoo,1\nbar,2\n", "csv", 0.9},
		{"csv_numhdr", "1,2\n3,4\n5,6\n", "text", 0.5},
		{"kv", "a=1 b=2\nc=3 d=\"x y\"\n", "kv", 1.0},
		{"w3c", "#Software: IIS\n#Fields: date time\n2026-01-01 00:00:00\n", "w3c", 0.95},
		{"winevt", "<Event xmlns='http://x'><System/></Event>\n<Event xmlns='http://x'><System/></Event>\n", "winevt", 0.95},
		{"winevt_bad", "<Event\U000000e9 xmlns='x'>\n<Event>xmlns='x'\n", "text", 0.5},
		{"cef", "Jan 1 host CEF:0|a|b|1|2|n|3|k=v\nCEF:0|a|b|1|2|n|3|k=v\n", "cef", 1.0},
		{"cri", "2026-01-01T00:00:00.1Z stdout F hello\n2026-01-01T00:00:00Z stderr P x\n", "text", 0.9},
		{"clf", "1.2.3.4 - - [01/Jan/2026:00:00:00 +0000] \"GET / HTTP/1.1\" 200 1\n", "access", 1.0},
		{"prefix200", strings.Repeat("\n", 190) + strings.Repeat("Jan  1 00:00:00 host1 prog: a\n", 10) + strings.Repeat("a=1 b=2\n", 300), "syslog", 1.0},
		{"arabic_digits", "Jan  \U00000661 \U00000660\U00000660:\U00000660\U00000660:\U00000660\U00000660 host1 prog: a\n", "syslog", 1.0},
		{"plain", "hello world\nthis is text\n", "text", 0.5},
	}
	for _, c := range cases {
		f, conf, err := DetectFormat(c.text, nil)
		if err != nil || f != c.format || conf != c.conf {
			t.Errorf("%s: DetectFormat = %s %v %v, want %s %v", c.name, f, conf, err, c.format, c.conf)
		}
	}
	_, _, err := DetectFormat("{\"a\": "+strings.Repeat("1", 4301)+"}\n{\"a\": 1}", nil)
	if !errors.Is(err, ErrPyRaise) {
		t.Errorf("4301-digit int: err = %v, want ErrPyRaise (ValueError escapes)", err)
	}
	_, _, err = DetectFormat(strings.Repeat("[", 1000), nil)
	if !errors.Is(err, ErrForward) {
		t.Errorf("deep nesting: err = %v, want ErrForward", err)
	}
}

// TestDetectGolden: detect_format on every golden input (whole run) and on the first live batch reproduces the
// golden report's format and confidence. sap / grok inputs need WP6 / WP3 (sapScore, grokDetect) and are only logged
// while those are stubs.
func TestDetectGolden(t *testing.T) {
	ins, err := goldenInputs()
	if err != nil {
		t.Fatal(err)
	}
	pending := 0
	for _, in := range ins {
		data, err := os.ReadFile(in.path)
		if err != nil {
			t.Fatal(err)
		}
		for _, mode := range []string{"whole", "live"} {
			gl, err := readGolden(in.name, mode)
			if err != nil || len(gl) == 0 {
				t.Fatalf("%s %s: %v", in.name, mode, err)
			}
			v, _ := py.Loads(gl[0])
			rv, _ := v.M.Get("report")
			if len(rv.L) == 0 || rv.L[0].K != py.KMap {
				continue
			}
			wf, _ := rv.L[0].M.Get("format")
			wc, _ := rv.L[0].M.Get("confidence")
			body := data
			if mode == "live" {
				body = liveChunks(data, 40)[0]
			}
			text, err := Decode(in.name, body)
			if err != nil {
				t.Fatalf("%s: decode: %v", in.name, err)
			}
			f, c, err := DetectFormat(text, nil)
			if err != nil || f != wf.S || c != wc.F {
				if wf.S == "sap" || wf.S == "grok" || f == "sap" || f == "grok" {
					pending++
					t.Logf("%s/%s: go %s %v, golden %s %v (needs sapScore/grokDetect)", in.name, mode, f, c, wf.S, wc.F)
					continue
				}
				t.Errorf("%s/%s: DetectFormat = %s %v %v, golden %s %v", in.name, mode, f, c, err, wf.S, wc.F)
			}
		}
	}
	t.Logf("%d sap/grok detections pending WP3/WP6", pending)
}

func TestDecode(t *testing.T) {
	if s, err := Decode("a.log", []byte("\xef\xbb\xbfhi\xef\xbb\xbf")); err != nil || s != "hi\U0000feff" {
		t.Errorf("BOM strip = %q %v", s, err)
	}
	if _, err := Decode("a.log", []byte("a\x00b")); !errors.Is(err, errNoText) {
		t.Errorf("NUL guard err = %v", err)
	}
	if _, err := Decode("a.log", append([]byte(strings.Repeat("x", 4096)), 0)); err != nil {
		t.Errorf("NUL after 4096 bytes: %v", err)
	}
	if _, err := Decode("a.log", []byte("\xff\xfea\x00")); !errors.Is(err, ErrForward) {
		t.Errorf("UTF-16 BOM err = %v", err)
	}
	if _, err := Decode("a.log", []byte("caf\xe9 ok!")); !errors.Is(err, ErrForward) {
		t.Errorf("latin-1/utf-16 mojibake err = %v", err)
	}
	for _, n := range []string{"x.ZIP", "a.tar.gz", "b.tgz", "c.tar", "d.log.gz"} {
		if _, err := Decode(n, []byte("x")); !errors.Is(err, ErrForward) {
			t.Errorf("%s err = %v", n, err)
		}
	}
	for _, n := range []string{"x.PNG", "dir/a.parquet", "b.so"} {
		if _, err := Decode(n, []byte("x")); !errors.Is(err, errNoText) {
			t.Errorf("%s err = %v", n, err)
		}
	}
	for _, n := range []string{".pdf", "x.", "a.pdf/b", "a.log"} {
		if _, err := Decode(n, []byte("x")); err != nil {
			t.Errorf("%s err = %v", n, err)
		}
	}
	if obs, reps, err := IngestBytes("x.png", []byte("x"), Options{Norm: NewNormalizer(2026)}); obs != nil || reps != nil || err != nil {
		t.Error("binary extension: ingest returns ([], [])")
	}
	for name, want := range map[string]bool{"README.md": true, "x/service_DEPENDENCIES.csv": true, `a\inventory\b.log`: true,
		"VERI_SOZLUGU.md": true, "inventory/x.log": false, "app.log": false} {
		if isSideTable(name) != want {
			t.Errorf("isSideTable(%q) != %v", name, want)
		}
	}
}

func obsAt(ts py.DateTime, attrs map[string]py.Value, parser, source string) Observation {
	d := py.NewDict(0)
	for k, v := range attrs {
		d.Set(k, v)
	}
	return Observation{TS: ts, Attributes: d, Parser: parser, Source: source, Message: source}
}

func TestFillMissingTimestamps(t *testing.T) {
	a := py.NewDateTime(2026, 1, 1, 12, 0, 0, 0, 3*3600) // 09:00Z, +03:00
	b := py.NewDateTime(2026, 1, 1, 9, 0, 0, 0, 0)       // same instant, +00:00
	c := py.NewDateTime(2026, 1, 1, 10, 0, 0, 0, 0)
	miss := map[string]py.Value{"_no_ts": py.B(true)}
	obs := []Observation{obsAt(c, nil, "", "1"), obsAt(a, nil, "", "2"), obsAt(py.Epoch, miss, "", "3"), obsAt(b, nil, "", "4"),
		obsAt(c, map[string]py.Value{"_no_ts": py.Int(0)}, "", "5")}
	fillMissingTimestamps(obs)
	if obs[2].TS.ISO() != "2026-01-01T12:00:00+03:00" {
		t.Errorf("fallback = %s, want the first minimal instant with its offset", obs[2].TS.ISO())
	}
	if obs[2].Attributes.Has("_no_ts") || obs[4].Attributes.Has("_no_ts") || obs[4].TS != c {
		t.Error("_no_ts is always popped; a falsy one changes nothing")
	}
	// No real timestamp: everything gets the first observation's timestamp (the epoch placeholder).
	obs = []Observation{obsAt(py.Epoch, miss, "", "1"), obsAt(py.Epoch, miss, "", "2")}
	fillMissingTimestamps(obs)
	if obs[0].TS.ISO() != "1970-01-01T00:00:00+00:00" || obs[1].Attributes.Has("_no_ts") {
		t.Error("epoch fallback")
	}
	fillMissingTimestamps(nil)
}

func TestDedupe(t *testing.T) {
	ts := py.Epoch
	id := func(v py.Value) map[string]py.Value { return map[string]py.Value{"alarm_id": v} }
	obs := []Observation{
		obsAt(ts, id(py.S("A")), "csv", "a.csv"),   // kept first, replaced by the json one (keeps first position)
		obsAt(ts, nil, "csv", "a.csv"),             // passthrough (absent)
		obsAt(ts, id(py.S("")), "csv", "a.csv"),    // passthrough ("")
		obsAt(ts, id(py.Null), "csv", "a.csv"),     // passthrough (None)
		obsAt(ts, id(py.Int(0)), "csv", "a.csv"),   // key "0" (0 is not None/"")
		obsAt(ts, id(py.S("A")), "json", "a.json"), // replaces the csv row
		obsAt(ts, id(py.S("A")), "json", "b.json"), // dropped (cur is json)
		obsAt(ts, id(py.F(0)), "csv", "a.csv"),     // key "0.0": distinct from "0"
		obsAt(ts, id(py.Int(1)), "json", "a.json"),
		obsAt(ts, id(py.S("1")), "csv", "a.csv"), // same str() as int 1: dropped (not json)
	}
	rep := []Report{{File: "a.csv", Format: "csv"}, {File: "a.json", Format: "json"}, {File: "b.json", Format: "json"}}
	out := dedupe(obs, rep)
	var got []string
	for _, o := range out {
		v, _ := o.Attributes.Get("alarm_id")
		got = append(got, o.Parser+":"+v.Repr())
	}
	want := []string{"csv:None", "csv:''", "csv:None", "json:'A'", "csv:0", "csv:0.0", "json:1"}
	// passthrough rows first (absent alarm_id reads as None), then kept rows in first-seen key order
	if !reflect.DeepEqual(got, want) {
		t.Errorf("dedupe order = %v, want %v", got, want)
	}
	if rep[0].DedupDropped != 2 || rep[1].DedupDropped != 0 || rep[2].DedupDropped != 1 {
		t.Errorf("dedup_dropped = %d %d %d", rep[0].DedupDropped, rep[1].DedupDropped, rep[2].DedupDropped)
	}
	same := []Observation{obsAt(ts, id(py.S("x")), "csv", "a"), obsAt(ts, nil, "csv", "a")}
	if out := dedupe(same, nil); &out[0] != &same[0] {
		t.Error("no duplicates: the input slice is returned unchanged")
	}
}

func TestSortStable(t *testing.T) {
	a := py.NewDateTime(2026, 1, 1, 12, 0, 0, 0, 3*3600)
	b := py.NewDateTime(2026, 1, 1, 9, 0, 0, 0, 0)
	early := py.NewDateTime(2026, 1, 1, 8, 59, 59, 999999, 0)
	obs := []Observation{obsAt(a, nil, "", "1"), obsAt(b, nil, "", "2"), obsAt(early, nil, "", "3"), obsAt(a, nil, "", "4")}
	sortObservations(obs)
	var got []string
	for _, o := range obs {
		got = append(got, o.Source)
	}
	if strings.Join(got, "") != "3124" {
		t.Errorf("stable sort = %v", got)
	}
}

func TestHintCache(t *testing.T) {
	c := NewHintCache()
	k := StreamKey{"agent", "app.log"}
	rep := []Report{{Format: "text", Confidence: 0.5, Family: ""}}
	var redetect []int
	for batch := 1; batch <= 700; batch++ {
		h := c.Next(k)
		if h == nil {
			redetect = append(redetect, batch)
		} else if h.Format != "text" || !h.HasFamily || h.Family != "" || h.Confidence != 0.5 {
			t.Fatalf("batch %d: hint %+v (family \"\" must be applied)", batch, h)
		}
		c.Learn(k, h, rep, []byte("x"))
	}
	if !reflect.DeepEqual(redetect, []int{1, 200, 399, 598}) {
		t.Errorf("re-detections at %v", redetect)
	}
	// A re-detection batch without report (or a table report) keeps the stale entry, which is used again.
	c = NewHintCache()
	c.Learn(k, nil, rep, []byte("x"))
	for i := 2; i < 200; i++ {
		c.Next(k)
	}
	if h := c.Next(k); h != nil {
		t.Fatal("batch 200 must re-detect")
	}
	c.Learn(k, nil, nil, []byte("x"))
	c.Learn(k, nil, []Report{{Format: "table"}}, []byte("x"))
	if h := c.Next(k); h == nil || h.N != 201 || h.Format != "text" {
		t.Errorf("stale hint after empty re-detection = %+v", h)
	}
	// json -> jsonl single-object rule on the raw body (ASCII bytes.strip set).
	jr := []Report{{Format: "json", Confidence: 0.95}}
	for body, want := range map[string]string{
		`{"a": 1}`: "jsonl", " \t\x0b{\"a\": 1}\r\n": "jsonl", "{\"a\":\n1}": "json", `[{"a": 1}]`: "json",
		"\xef\xbb\xbf{\"a\": 1}": "json", "\U000000a0{}": "json",
	} {
		c := NewHintCache()
		c.Learn(k, nil, jr, []byte(body))
		if h := c.Get(k); h == nil || h.Format != want || h.Confidence != 0.95 || h.N != 1 {
			t.Errorf("json rule on %q = %+v, want %s", body, h, want)
		}
	}
	// A used hint never updates the entry.
	c = NewHintCache()
	c.Learn(k, &Hint{Format: "kv"}, rep, []byte("x"))
	if c.Len() != 0 {
		t.Error("Learn with a used hint must not store")
	}
	// More than 5000 streams: the whole cache is cleared after inserting.
	c = NewHintCache()
	for i := 0; i < 5000; i++ {
		c.Learn(StreamKey{"a", string(rune(0x4e00 + i))}, nil, rep, nil)
	}
	if c.Len() != 5000 {
		t.Fatalf("len = %d", c.Len())
	}
	c.Learn(StreamKey{"b", "x"}, nil, rep, nil)
	if c.Len() != 0 {
		t.Errorf("len after 5001st = %d, want 0", c.Len())
	}
	// Whitespace-only body: Python caches the text/0.0 report.
	c = NewHintCache()
	c.Learn(k, nil, []Report{{Format: "text", Confidence: 0}}, []byte("  \n"))
	if h := c.Get(k); h == nil || h.Format != "text" || h.Confidence != 0 {
		t.Errorf("whitespace-only = %+v", h)
	}
}

func TestLiveChunks(t *testing.T) {
	data := []byte("a\nb\n \n\t\n\nc\r\nd\n")
	var got []string
	for _, c := range liveChunks(data, 2) {
		got = append(got, string(c))
	}
	// split -> [a b " " "\t" "" "c\r" d ""]; pairs: "a\nb", " \n\t" (skipped), "\nc\r", "d\n"
	want := []string{"a\nb", "\nc\r", "d\n"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("liveChunks = %q, want %q", got, want)
	}
	if len(liveChunks([]byte(""), 40)) != 0 || len(liveChunks([]byte("\n\n"), 1)) != 0 {
		t.Error("empty input")
	}
}

func TestCanonLine(t *testing.T) {
	d := py.NewDict(0)
	d.Set("b", py.Int(1))
	d.Set("a", py.S("x<\U00002028\x01"))
	d.Set("f", py.F(2))
	d.Set("t", py.B(true))
	d.Set("n", py.Null)
	big, _ := new(big.Int).SetString("1000000000000000000000000000000", 10)
	d.Set("big", py.BigInt(big))
	d.Set("\U000000e9", py.F(0.1))
	o := Observation{TS: py.NewDateTime(2026, 10, 14, 9, 30, 0, 32000, 3*3600), Message: `m "q" <&>`, Kind: "log", Severity: "INFO",
		Service: "svc", Host: "h", Environment: "prod", Attributes: d, Source: "a.log", LineNo: 3, Parser: "kv",
		ParserConfidence: 0.6066, Raw: "r\tx"}
	want := `{"attributes": {"a": "x<` + "\U00002028" + `\u0001", "b": 1, "big": 1000000000000000000000000000000, "f": 2.0, "n": null, "t": true, "` +
		"\U000000e9" + `": 0.1}, "environment": "prod", "host": "h", "kind": "log", "line_no": 3, "message": "m \"q\" <&>", "origin": "", ` +
		`"parser": "kv", "parser_confidence": 0.6066, "raw": "r\tx", "service": "svc", "severity": "INFO", "source": "a.log", ` +
		`"ts": "2026-10-14T09:30:00.032000+03:00"}`
	got, err := CanonLine(o)
	if err != nil || string(got) != want {
		t.Errorf("CanonLine =\n%s\nwant\n%s", got, want)
	}
	for conf, s := range map[float64]string{1: "1.0", 0.95: "0.95", 0.61: "0.61", 0.57: "0.57", 1.2: "1.2", 1.23: "1.23", 0.61234567: "0.6123", 2.0 / 3: "0.6667"} {
		o.ParserConfidence = conf
		b, _ := CanonLine(o)
		if !strings.Contains(string(b), `"parser_confidence": `+s+",") {
			t.Errorf("parser_confidence %v -> want %s in %s", conf, s, b)
		}
	}
	d.Set("x", py.F(math.NaN()))
	if _, err := CanonLine(o); !errors.Is(err, ErrForward) {
		t.Errorf("NaN attribute err = %v", err)
	}
	if !strings.Contains(string(canonLineNaN(o)), `"x": NaN`) {
		t.Error("canonLineNaN writes NaN like Python")
	}
}

func TestCanonReport(t *testing.T) {
	var roles Roles
	roles.Set(RoleTimestamp, "ts")
	roles.Set(RoleMessage, "")
	r := Report{File: "a.log", Format: "kv", Confidence: 1, Rows: 2, Kind: "log", Keys: []string{"ts", "msg"}, Roles: roles,
		Profile: "5d7e874a6060f57b", DedupDropped: 1}
	want := `{"report": [{"confidence": 1.0, "dedup_dropped": 1, "family": "", "file": "a.log", "format": "kv", "keys": ["ts", "msg"], ` +
		`"kind": "log", "profile": "5d7e874a6060f57b", "roles": {"environment": null, "host": null, "message": "", "origin": null, ` +
		`"service": null, "severity": null, "timestamp": "ts"}, "rows": 2}, {}]}`
	if got := string(CanonReport([]Report{r, {}})); got != want {
		t.Errorf("CanonReport =\n%s\nwant\n%s", got, want)
	}
	if got := string(CanonReport(nil)); got != `{"report": []}` {
		t.Errorf("empty report = %s", got)
	}
}

func TestSharepointULS(t *testing.T) {
	attrs := py.NewDict(0)
	attrs.Set("Area", py.S(" SharePoint Foundation "))
	attrs.Set("Process", py.S(" w3wp.exe "))
	attrs.Set("n", py.Int(5))
	o := Observation{Raw: "2026\tw3wp.exe \t SharePoint Foundation\tMonitoring\tb4ly\t High \tLeaving Monitored Scope (Request (GET:http://x/a.aspx?b=1)). Execution Time=\U00000661245.12\tcorr",
		Message: "Leaving Monitored Scope (Request (GET:http://x/a.aspx?b=1)). Execution Time=\U00000661245.12", Attributes: attrs, Severity: "WARN"}
	obs := []Observation{o}
	sharepointULS(obs)
	got := obs[0]
	if got.Severity != "INFO" || got.Service != "sharepoint:Monitoring" {
		t.Errorf("severity/service = %s %s", got.Severity, got.Service)
	}
	if v, _ := got.Attributes.Get("Process"); v.S != "w3wp.exe" {
		t.Errorf("strip = %q", v.S)
	}
	if v, _ := got.Attributes.Get("duration_ms"); v.K != py.KFloat || v.F != 1245.12 {
		t.Errorf("duration_ms = %+v", v)
	}
	if v, _ := got.Attributes.Get("http.method"); v.S != "GET" {
		t.Errorf("http.method = %q", v.S)
	}
	if v, _ := got.Attributes.Get("url"); v.S != "http://x/a.aspx?b=1" {
		t.Errorf("url = %q", v.S)
	}
}

func TestLevelWordSearch(t *testing.T) {
	for s, want := range map[string]bool{"an ERROR here": true, "xERROR": false, "ERRORS": false, "error:": true, "\U00000131nfo": true,
		"\U00000130NFO x": true, "INFOx": false, "1INFO2": true, "_warn_": true, "warning!": true, "\U0000017fevere": true,
		"vrb": true, "inf": true, "infinity": false, "": false, "crit\U00000131cal": true} {
		if got := levelWordSearch.MatchString(s); got != want {
			t.Errorf("levelWordSearch(%q) = %v, want %v", s, got, want)
		}
	}
}

// TestAttributesEmptyRule: P01 section 9 step 6 — a record with no more keys than distinct used role keys gets no
// attributes even when it has unused keys. Needs WP2 (AutoMap, ParseTimestamp, ...).
func TestAttributesEmptyRule(t *testing.T) {
	n := NewNormalizer(2026)
	mk := func(kv ...string) *py.Dict {
		d := py.NewDict(0)
		for i := 0; i < len(kv); i += 2 {
			d.Set(kv[i], py.S(kv[i+1]))
		}
		return d
	}
	recs := []Record{
		{1, mk("timestamp", "2026-01-01T00:00:00Z", "level", "ERROR", "message", "a", "host", "h1", "extra", "x")},
		{2, mk("timestamp", "2026-01-01T00:00:01Z", "message", "b", "extra", "y")}, // 3 keys <= 4 used keys -> {}
	}
	obs, err := recordsToObservations(n, recs, "app.log", "jsonl", "event", nil, 1, nil)
	if errors.Is(err, ErrNotImplemented) {
		t.Skip("needs WP2 (normalize)")
	}
	if err != nil {
		t.Fatal(err)
	}
	if v, ok := obs[0].Attributes.Get("extra"); !ok || v.S != "x" {
		t.Errorf("record 1 attributes = %v", obs[0].Attributes.Keys())
	}
	if obs[1].Attributes.Has("extra") {
		t.Errorf("record 2 must have no attributes besides _sev_src, got %v", obs[1].Attributes.Keys())
	}
}

func TestStubsAndRegistry(t *testing.T) {
	if len(Registry) != len(RegistryOrder) {
		t.Fatal("Registry / RegistryOrder size")
	}
	lineOriented := map[string]bool{"json": false, "winevt": false}
	kinds := map[string]string{"json": "event", "jsonl": "event", "csv": "event", "cef": "event", "winevt": "event"}
	for _, name := range RegistryOrder {
		p := Registry[name]
		if p == nil || p.Name() != name {
			t.Errorf("Registry[%s]", name)
			continue
		}
		wantLO, ok := lineOriented[name]
		if !ok {
			wantLO = true
		}
		wantKind := kinds[name]
		if wantKind == "" {
			wantKind = "log"
		}
		if p.LineOriented() != wantLO || p.Kind() != wantKind {
			t.Errorf("%s: line_oriented %v kind %s", name, p.LineOriented(), p.Kind())
		}
	}
	if FormatKey("text", "") != "text:" || FormatKey("text", "java") != "text:java" || FormatKey("kv", "") != "kv" {
		t.Error("FormatKey")
	}
	a := parseAllow("# c\nkv fortigate_kv.log whole,live\nlogfmt.log live\nx.log\ntext:java java.log whole|live\n")
	if !a.formats["kv"] || !a.formats["text:java"] || !a.inputs["logfmt.log"]["live"] || a.inputs["logfmt.log"]["whole"] ||
		!a.inputs["x.log"]["whole"] || !a.inputs["java.log"]["live"] {
		t.Errorf("parseAllow = %+v", a)
	}
	if Signature("kv", []string{"b", "a"}) != Signature("kv", []string{"a", "b"}) || len(Signature("kv", nil)) != 16 {
		t.Error("Signature")
	}
}
