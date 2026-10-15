package parse

import (
	"bufio"
	"compress/gzip"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"testing"

	"watchover/receiver/parse/py"
)

// WP5 unit tests: json, jsonl, csv, winevt parsers (P04 sections 6-9, 12; P02 section 11.1).

var wp5Parsers = map[string]Parser{"json": JsonParser{}, "jsonl": JsonlParser{}, "csv": CsvParser{}, "winevt": WinEvtParser{}}

// wp5Run returns json.dumps([[line_no, rec], ...], ensure_ascii=False) of p.records(text), "!raise" for ErrPyRaise,
// "!forward: ..." for ErrForward.
func wp5Run(p Parser, text string) string {
	var out []py.Value
	err := p.Records(text, &FamilyCtx{}, func(r Record) bool {
		out = append(out, py.ListOf(py.Int(int64(r.LineNo)), py.MapOf(r.Rec)))
		return true
	})
	switch {
	case errors.Is(err, ErrPyRaise):
		return "!raise"
	case err != nil:
		return "!forward: " + err.Error()
	}
	s, err := py.Dumps(py.ListOf(out...), false)
	if err != nil {
		return "!dumps: " + err.Error()
	}
	return s
}

func wp5Same(got, want string) bool {
	if strings.HasPrefix(want, "!") {
		return got == "!raise"
	}
	return got == want
}

// TestWP5Cases: the generated Python-probed edge cases and seeded random texts (testdata/wp5gen/gen_cases.py).
func TestWP5Cases(t *testing.T) {
	fails := 0
	for i, c := range wp5Cases {
		got := wp5Run(wp5Parsers[c.parser], c.text)
		if !wp5Same(got, c.want) {
			fails++
			if fails <= 25 {
				t.Errorf("case %d %s %q:\n got  %.800s\n want %.800s", i, c.parser, c.text, got, c.want)
			}
		}
	}
	t.Logf("%d cases, %d mismatches", len(wp5Cases), fails)
}

// TestWP5Pipeline: pipeline.ingest_bytes on documents of these formats, golden style (winevt/json raw = json.dumps
// in insertion order, csv line_no = row index + 1 with the mismatched raw, SharePoint ULS fix-up, jsonl enrich).
func TestWP5Pipeline(t *testing.T) {
	norm := NewNormalizer(2026)
	store := NewMemStore()
	for _, c := range wp5PipelineCases {
		obs, reps, err := IngestBytes(c.name, []byte(c.text), Options{Norm: norm, Profiles: store})
		if err != nil {
			t.Errorf("%s: %v", c.name, err)
			continue
		}
		lines := []string{string(CanonReport(reps))}
		for _, o := range obs {
			b, err := CanonLine(o)
			if err != nil {
				t.Fatalf("%s: CanonLine: %v", c.name, err)
			}
			lines = append(lines, string(b))
		}
		want := strings.Split(c.want, "\n")
		if len(lines) != len(want) {
			t.Errorf("%s: %d lines, want %d", c.name, len(lines), len(want))
		}
		for i := 0; i < len(lines) && i < len(want); i++ {
			if lines[i] != want[i] {
				t.Errorf("%s line %d:\n got  %s\n want %s", c.name, i, lines[i], want[i])
			}
		}
	}
}

// TestWP5CsvFieldLimit: field_size_limit 131072 (code points); the error is raised lazily, row by row.
func TestWP5CsvFieldLimit(t *testing.T) {
	x := strings.Repeat("x", 131072)
	if got := wp5Run(CsvParser{}, "a,b\n"+x+"x,1\n"); got != "!raise" {
		t.Errorf("131073-char field: %.80s", got)
	}
	if got := wp5Run(CsvParser{}, "a,b\n\""+x+"\",1"); got != `[[2, {"a": "`+x+`", "b": "1"}]]` {
		t.Errorf("131072-char quoted field: %.80s", got)
	}
	if got := wp5Run(CsvParser{}, "a,b\n"+strings.Repeat("é", 131072)+",1"); !strings.HasPrefix(got, "[[2,") {
		t.Errorf("131072 code points (more bytes) must pass: %.80s", got)
	}
	// The probe stops after the first record: the error in the second row is never reached (Python generator).
	text := "a,b\n" + x + ",1\n" + x + "y"
	n := 0
	err := CsvParser{}.Records(text, &FamilyCtx{}, func(Record) bool { n++; return false })
	if err != nil || n != 1 {
		t.Errorf("stopped probe: n=%d err=%v", n, err)
	}
	if got := wp5Run(CsvParser{}, text); got != "!raise" {
		t.Errorf("full run must raise: %.80s", got)
	}
}

// TestWP5CsvReader: the _csv state machine on its own (records as Python's csv.reader(io.StringIO(text)) returns them).
func TestWP5CsvReader(t *testing.T) {
	for _, c := range []struct {
		text string
		want string // records joined: fields by "|", records by "/"; "[]" for an empty record
		err  bool
	}{
		{"a,b\n1,2", "a|b/1|2", false},
		{"\n\na", "[]/[]/a", false},
		{"\"q\"\"r\"s,2", "q\"rs|2", false},
		{"\"a\nb\",c\nd", "a\nb|c/d", false},
		{"\"open", "open", false},
		{"\"", "", false},
		{"a\r\nb\rc", "", true},
		{"a\r\nb\r", "a/b", false},
		{"x\"y\"z,1", "x\"y\"z|1", false},
		{",\n", "|", false},
		{" \n", " ", false},
		{"a\x00b", "a\x00b", false},
		{"a\u2028b,c", "a\u2028b|c", false},
	} {
		rd := newCSVReader(c.text, ',')
		var recs []string
		var err error
		for {
			var row []string
			var ok bool
			row, ok, err = rd.next()
			if err != nil || !ok {
				break
			}
			if len(row) == 0 {
				recs = append(recs, "[]")
			} else {
				recs = append(recs, strings.Join(row, "|"))
			}
		}
		if c.err {
			if !errors.Is(err, ErrPyRaise) {
				t.Errorf("%q: want csv.Error, got %v (%q)", c.text, err, recs)
			}
			continue
		}
		if err != nil || strings.Join(recs, "/") != c.want {
			t.Errorf("%q: got %q err %v, want %q", c.text, strings.Join(recs, "/"), err, c.want)
		}
	}
}

// TestWP5JsonForward: inputs Python handles but this port does not reproduce -> ErrForward (never a wrong answer).
func TestWP5JsonForward(t *testing.T) {
	deep := strings.Repeat("[", 1000) + strings.Repeat("]", 1000)
	for _, c := range []struct {
		p    Parser
		text string
	}{
		{JsonParser{}, `{"resourceLogs": [{"resource": {"attributes": [{"key": 1, "value": {"intValue": "1"}}]}}]}`},
		{JsonParser{}, `{"resourceLogs": [{"scopeLogs": [{"logRecords": [{"attributes": [{"key": null}]}]}]}]}`},
		{JsonParser{}, deep},
		{JsonlParser{}, "{\"a\":1}\n" + `{"a":` + deep + `}`},
	} {
		err := c.p.Records(c.text, &FamilyCtx{}, func(Record) bool { return true })
		if !errors.Is(err, ErrForward) || errors.Is(err, ErrPyRaise) {
			t.Errorf("%s %.60q: want ErrForward, got %v", c.p.Name(), c.text, err)
		}
	}
}

// TestWP5FindRecords: depth limit (a list 4 dict levels down is found, 5 is not), only the first element is checked,
// and the json parser counts non-dict rows in line_no.
func TestWP5FindRecords(t *testing.T) {
	for _, c := range []struct {
		doc   string
		found bool
	}{
		{`[{"a":1}]`, true},
		{`[1, {"a":1}]`, false},
		{`{"a":{"b":{"c":{"d":[{"x":1}]}}}}`, true},
		{`{"a":{"b":{"c":{"d":{"e":[{"x":1}]}}}}}`, false},
		{`{"a":[[{"x":1}]]}`, false},
		{`{"a":[], "b":[{"x":1}]}`, true},
	} {
		v, err := py.Loads(c.doc)
		if err != nil {
			t.Fatal(err)
		}
		if got := findRecords(v, 0) != nil; got != c.found {
			t.Errorf("%s: found=%v want %v", c.doc, got, c.found)
		}
	}
	if got := wp5Run(JsonParser{}, `[{"a":1}, "x", null, {"b":2}]`); got != `[[1, {"a": 1}], [4, {"b": 2}]]` {
		t.Errorf("line_no over non-dict rows: %s", got)
	}
}

// TestWP5EventChunks: EVENT_RE.findall with the Unicode \b emulation.
func TestWP5EventChunks(t *testing.T) {
	for _, c := range []struct {
		text string
		want []string
	}{
		{"<Event></Event>", []string{"<Event></Event>"}},
		{"<Event</Event>", []string{"<Event</Event>"}},
		{"<Events><Event>x</Event></Events>", []string{"<Event>x</Event>"}},
		{"<Eventé></Event><Event/></Event>", []string{"<Event/></Event>"}},
		{"<Event_x></Event>", nil},
		{"<Event-x></Event>", []string{"<Event-x></Event>"}},
		{"<Event>a\n</Event>b<Event>c</Event>", []string{"<Event>a\n</Event>", "<Event>c</Event>"}},
		{"<Event>no close <Event>", nil},
		{"<Event", nil},
	} {
		got := eventChunks(c.text)
		if strings.Join(got, "\x00") != strings.Join(c.want, "\x00") || len(got) != len(c.want) {
			t.Errorf("%q: got %q want %q", c.text, got, c.want)
		}
	}
}

// TestWP5XmlNames: expat's name tables (winevt_names.go): non-ASCII letters are name characters, combining marks only
// after the first character, symbols and non-BMP characters never.
func TestWP5XmlNames(t *testing.T) {
	for _, c := range []struct {
		s  string
		ok bool
	}{
		{"<Event><Ev\u00e9nt/></Event>", true},
		{"<Event><\u00e9/></Event>", true},
		{"<Event><a\u0300/></Event>", true},
		{"<Event><\u0300/></Event>", false},
		{"<Event><a\u00d7/></Event>", false},
		{"<Event><a\U0001F600/></Event>", false},
		{"<Event a\u00e9='1'></Event>", true},
		{"<Event xmlns:\u00e9='u'><\u00e9:x/></Event>", true},
		{"<Event><x:\u00e9/></Event>", false},
	} {
		_, err := xmlParse(c.s)
		if err != nil && !errors.Is(err, errXML) {
			t.Fatalf("%q: %v", c.s, err)
		}
		if (err == nil) != c.ok {
			t.Errorf("%q: ok=%v want %v", c.s, err == nil, c.ok)
		}
	}
}

// TestWP5Enrich: enrich only fires when no severity-like key exists and the first str text field has a level head.
func TestWP5Enrich(t *testing.T) {
	mk := func(kv ...string) *py.Dict {
		d := py.NewDict(0)
		for i := 0; i < len(kv); i += 2 {
			d.Set(kv[i], py.S(kv[i+1]))
		}
		return d
	}
	dump := func(d *py.Dict) string { s, _ := py.Dumps(py.MapOf(d), false); return s }
	for _, c := range []struct {
		in   *py.Dict
		want string
	}{
		{mk("log", "2026-01-01T00:00:00Z ERROR [app] boom\n"), `{"log": "2026-01-01T00:00:00Z ERROR [app] boom\n", "severity": "ERROR", "service": "app", "message": "boom"}`},
		{mk("log", "x ERROR y", "s", "1"), `{"log": "x ERROR y", "s": "1"}`},
		{mk("message", "", "msg", "WARN y"), `{"message": "", "msg": "WARN y"}`},
		{mk("msg", "plain text"), `{"msg": "plain text"}`},
		{mk("app", "a", "msg", "ERROR [svc] z"), `{"app": "a", "msg": "ERROR [svc] z", "severity": "ERROR", "message": "z"}`},
	} {
		if got := dump(enrich(c.in)); got != c.want {
			t.Errorf("got  %s\nwant %s", got, c.want)
		}
	}
}

// TestWP5PythonDiff is an opt-in differential test: golden inputs (whole and 40-line chunks) and seeded random texts
// through the four WP5 parsers, against the Python parsers. Fixture:
//
//	PYTHONPATH=src python3 receiver/parse/testdata/wp5gen/wp5_dump.py /tmp/wp5.jsonl.gz 20000
//	WP5_DIFF=/tmp/wp5.jsonl.gz go test ./parse/ -run TestWP5PythonDiff -count=1
func TestWP5PythonDiff(t *testing.T) {
	path := os.Getenv("WP5_DIFF")
	if path == "" {
		t.Skip("set WP5_DIFF to a fixture produced by testdata/wp5gen/wp5_dump.py")
	}
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	zr, err := gzip.NewReader(f)
	if err != nil {
		t.Fatal(err)
	}
	sc := bufio.NewScanner(zr)
	sc.Buffer(make([]byte, 1<<20), 1<<28)
	runs, fails, forwards := 0, 0, 0
	for sc.Scan() {
		var fx struct{ Parser, Text, Out string }
		if err := json.Unmarshal(sc.Bytes(), &fx); err != nil {
			t.Fatal(err)
		}
		runs++
		got := wp5Run(wp5Parsers[fx.Parser], fx.Text)
		if strings.HasPrefix(got, "!forward") {
			forwards++
			continue
		}
		if !wp5Same(got, fx.Out) {
			fails++
			if fails <= 20 {
				t.Errorf("%s on %q:\n got  %.600s\n want %.600s", fx.Parser, py.Head(fx.Text, 200), got, fx.Out)
			}
		}
	}
	if err := sc.Err(); err != nil {
		t.Fatal(err)
	}
	t.Logf("%d runs, %d mismatches, %d forwarded", runs, fails, forwards)
}
