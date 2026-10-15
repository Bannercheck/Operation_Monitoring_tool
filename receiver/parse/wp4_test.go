package parse

import (
	"bufio"
	"compress/gzip"
	"encoding/json"
	"errors"
	"os"
	"testing"

	"watchover/receiver/parse/py"
)

// WP4 unit tests: syslog, access, w3c, kv, cef/leef parsers (P04 sections 1-5, 12).

var wp4Parsers = map[string]Parser{
	"syslog": SyslogParser{}, "access": AccessParser{}, "w3c": W3cParser{}, "kv": KvParser{}, "cef": CefParser{},
}

// wp4Run returns json.dumps([[line_no, rec], ...], ensure_ascii=False) of p.records(text), or "!err" / "!forward".
func wp4Run(p Parser, text string) string {
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

// wp4Same compares a Go result with a Python expectation ("!<Exception>" means any ErrPyRaise).
func wp4Same(got, want string) bool {
	if len(want) > 0 && want[0] == '!' {
		return got == "!raise"
	}
	return got == want
}

// TestWP4Cases: the generated Python-probed edge cases (testdata/wp4gen/gen_cases.py -> wp4_cases_test.go).
func TestWP4Cases(t *testing.T) {
	for i, c := range wp4Cases {
		got := wp4Run(wp4Parsers[c.parser], c.text)
		if !wp4Same(got, c.want) {
			t.Errorf("case %d %s %q:\n got  %s\n want %s", i, c.parser, c.text, got, c.want)
		}
	}
}

func TestWP4NotLevelHost(t *testing.T) {
	for _, c := range []struct {
		ln   string
		want bool
	}{
		{"Sep 22 09:14:02 INFO sshd: x", false},
		{"Sep 22 09:14:02 info-svc sshd: x", false},
		{"Sep 22 09:14:02 \U00000130NFO sshd: x", false},
		{"Sep 22 09:14:02 \U00000131nfo sshd: x", false},
		{"Sep 22 09:14:02 ERRORS sshd: x", true},
		{"Sep 22 09:14:02 err_1 sshd: x", true},
		{"Sep 22 09:14:02 Verbose sshd: x", false},
	} {
		if got := syslogMatch(c.ln) != nil; got != c.want {
			t.Errorf("syslogMatch(%q) = %v, want %v", c.ln, got, c.want)
		}
	}
}

func TestWP4CefKVScanner(t *testing.T) {
	for _, c := range []struct {
		ext  string
		want [][2]string
	}{
		{"xk=v", [][2]string{{"xk", "v"}}},
		{"abc=foo=bar baz=1", [][2]string{{"foo", "bar"}, {"baz", "1"}}},
		{`a=1 baz=1 tail\`, [][2]string{{"a", "1"}}},
		{"cs1=Web Application Attack end=", [][2]string{{"cs1", "Web Application Attack"}, {"end", ""}}},
		{`k=a\=b\\ c=d`, [][2]string{{"k", `a\=b\\`}, {"c", "d"}}},
		{"=x a=", [][2]string{{"a", ""}}},
		{"", nil},
	} {
		got := cefKV(c.ext)
		if len(got) != len(c.want) {
			t.Errorf("cefKV(%q) = %q, want %q", c.ext, got, c.want)
			continue
		}
		for i := range got {
			if got[i] != c.want[i] {
				t.Errorf("cefKV(%q) = %q, want %q", c.ext, got, c.want)
				break
			}
		}
	}
}

func TestWP4SplitHeader(t *testing.T) {
	for _, c := range []struct {
		s    string
		n    int
		want []string
	}{
		{`a|b|c|rest|x`, 3, []string{"a", "b", "c", "rest|x"}},
		{`a\|b|c\\|d|e\|f`, 3, []string{"a|b", `c\`, "d", `e\|f`}},
		{`a|b`, 3, []string{"a", "b"}},
		{`\`, 1, []string{`\`}},
		{"\U000000e9\\\U000000e9|x", 1, []string{"\U000000e9\U000000e9", "x"}},
	} {
		got := splitHeader(c.s, c.n)
		if len(got) != len(c.want) {
			t.Errorf("splitHeader(%q, %d) = %q, want %q", c.s, c.n, got, c.want)
			continue
		}
		for i := range got {
			if got[i] != c.want[i] {
				t.Errorf("splitHeader(%q, %d) = %q, want %q", c.s, c.n, got, c.want)
				break
			}
		}
	}
}

// TestWP4ProbeStop: Records stops when yield returns false (the 50-record probe).
func TestWP4ProbeStop(t *testing.T) {
	text := "a=1 b=2\na=3 b=4\na=5 b=6\n"
	n := 0
	err := KvParser{}.Records(text, &FamilyCtx{}, func(Record) bool { n++; return n < 2 })
	if err != nil || n != 2 {
		t.Fatalf("got n=%d err=%v", n, err)
	}
}

// TestWP4PythonDiff is an opt-in differential test: every golden input (whole decoded text and every 40-line live
// chunk) through all five WP4 parsers, against the Python parsers. Fixture:
//
//	PYTHONPATH=src python3 receiver/parse/testdata/wp4gen/wp4_dump.py /tmp/wp4.jsonl.gz
//	WP4_DIFF=/tmp/wp4.jsonl.gz go test ./parse/ -run TestWP4PythonDiff -count=1
func TestWP4PythonDiff(t *testing.T) {
	path := os.Getenv("WP4_DIFF")
	if path == "" {
		t.Skip("set WP4_DIFF to a fixture produced by testdata/wp4gen/wp4_dump.py")
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
	texts, runs, fails := 0, 0, 0
	for sc.Scan() {
		var fx struct {
			Text string            `json:"text"`
			Out  map[string]string `json:"out"`
		}
		if err := json.Unmarshal(sc.Bytes(), &fx); err != nil {
			t.Fatal(err)
		}
		texts++
		for name, want := range fx.Out {
			runs++
			got := wp4Run(wp4Parsers[name], fx.Text)
			if !wp4Same(got, want) {
				fails++
				if fails <= 20 {
					t.Errorf("%s on text %d (%q...):\n got  %.600s\n want %.600s", name, texts, py.Head(fx.Text, 80), got, want)
				}
			}
		}
	}
	if err := sc.Err(); err != nil {
		t.Fatal(err)
	}
	t.Logf("%d texts, %d parser runs, %d mismatches", texts, runs, fails)
}
