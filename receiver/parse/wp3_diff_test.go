package parse

import (
	"bufio"
	"compress/gzip"
	"encoding/json"
	"os"
	"testing"

	"watchover/receiver/parse/py"
)

// TestWP3PythonDiff is an opt-in differential test against Python (P03 verification method). The fixture is produced
// by a Python dumper that wraps TextParser.records / GrokParser.records / grok.detect while replaying
// scripts/parse_golden.py (whole + 40-line live runs of every golden input) and evaluates parse_head, CONT_RE, every
// family pattern and every grok LINES pattern (groupdict) on every distinct input line plus Unicode edge cases:
//
//	PYTHONPATH=src python3 receiver/parse/testdata/wp3gen/wp3_dump.py /tmp/wp3.jsonl.gz receiver/parse/testdata/wp3gen/edge.txt
//	WP3_DIFF=/tmp/wp3.jsonl.gz go test ./parse/ -run TestWP3PythonDiff
//
// Each fixture line is JSON: {"t":"rec",parser,text,hint,seen,out} | {"t":"gdet",lines,name,share} |
// {"t":"line",ln,head,cont,fams,grok}. Records are compared as json.dumps text (key order, int/bool/str kinds).
func TestWP3PythonDiff(t *testing.T) {
	path := os.Getenv("WP3_DIFF")
	if path == "" {
		t.Skip("set WP3_DIFF to a fixture produced by the WP3 Python dumper")
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
	type fixture struct {
		T      string                           `json:"t"`
		Parser string                           `json:"parser"`
		Text   string                           `json:"text"`
		Hint   *string                          `json:"hint"`
		Seen   *string                          `json:"seen"`
		Out    string                           `json:"out"`
		Lines  []string                         `json:"lines"`
		Name   *string                          `json:"name"`
		Share  float64                          `json:"share"`
		Ln     string                           `json:"ln"`
		Head   string                           `json:"head"`
		Cont   bool                             `json:"cont"`
		Fams   []string                         `json:"fams"`
		Grok   map[string][][2]*json.RawMessage `json:"grok"`
	}
	counts := map[string]int{}
	fails := 0
	fail := func(format string, args ...any) {
		fails++
		if fails <= 40 {
			t.Errorf(format, args...)
		}
	}
	for sc.Scan() {
		var fx fixture
		if err := json.Unmarshal(sc.Bytes(), &fx); err != nil {
			t.Fatal(err)
		}
		counts[fx.T]++
		switch fx.T {
		case "rec":
			var p Parser = TextParser{}
			if fx.Parser == "grok" {
				p = GrokParser{}
			}
			fc := &FamilyCtx{Hint: fx.Hint}
			var items []py.Value
			if err := p.Records(fx.Text, fc, func(r Record) bool {
				items = append(items, py.ListOf(py.Int(int64(r.LineNo)), py.MapOf(r.Rec)))
				return true
			}); err != nil {
				fail("%s records error: %v", fx.Parser, err)
				continue
			}
			got, _ := py.Dumps(py.ListOf(items...), false)
			if items == nil {
				got = "[]"
			}
			if got != fx.Out {
				fail("%s records differ (hint %v) for text starting %q:\n go %.600s\n py %.600s", fx.Parser, fx.Hint, py.Head(fx.Text, 80), got, fx.Out)
			}
			if fx.Parser == "text" {
				gs := ""
				if fc.Seen != nil {
					gs = *fc.Seen
				}
				if fx.Seen == nil || gs != *fx.Seen {
					fail("family seen: go %q py %v (text %q)", gs, fx.Seen, py.Head(fx.Text, 80))
				}
			}
		case "gdet":
			lp, share := grokDetect(fx.Lines)
			gn := ""
			if lp != nil {
				gn = lp.Name
			}
			pn := ""
			if fx.Name != nil {
				pn = *fx.Name
			}
			if gn != pn || share != fx.Share {
				fail("grokDetect: go %q %v py %q %v (first line %q)", gn, share, pn, fx.Share, fx.Lines[0])
			}
		case "line":
			rec, rest := parseHead(fx.Ln)
			got, _ := py.Dumps(py.ListOf(py.MapOf(rec), py.S(rest)), false)
			if got != fx.Head {
				fail("parseHead(%q):\n go %s\n py %s", fx.Ln, got, fx.Head)
			}
			if contMatch(fx.Ln) != fx.Cont {
				fail("contMatch(%q) = %v, py %v", fx.Ln, !fx.Cont, fx.Cont)
			}
			var fams []string
			for _, fd := range families {
				for _, p := range fd.pats {
					if p.MatchString(fx.Ln) {
						fams = append(fams, fd.name)
						break
					}
				}
			}
			if !equalStrings(fams, fx.Fams) {
				fail("families(%q): go %v py %v", fx.Ln, fams, fx.Fams)
			}
			if pg := fx.Grok["DHCPD"]; pg != nil {
				fail("DHCPD matched in Python: %q", fx.Ln)
			}
			for _, lp := range grokLines {
				want := fx.Grok[lp.Name]
				m := lp.rx.FindStringSubmatchIndex(fx.Ln)
				if (m == nil) != (want == nil) {
					fail("grok %s match(%q): go %v py %v", lp.Name, fx.Ln, m != nil, want != nil)
					continue
				}
				if m == nil {
					continue
				}
				var got [][2]string
				for g, name := range lp.names {
					if g == 0 || name == "" {
						continue
					}
					v := "null"
					if m[2*g] >= 0 {
						b, _ := json.Marshal(fx.Ln[m[2*g]:m[2*g+1]])
						v = string(b)
					}
					got = append(got, [2]string{name, v})
				}
				ok := len(got) == len(want)
				for i := 0; ok && i < len(got); i++ {
					var wn string
					_ = json.Unmarshal(*want[i][0], &wn)
					wv := "null"
					if want[i][1] != nil {
						var s string
						_ = json.Unmarshal(*want[i][1], &s)
						b, _ := json.Marshal(s)
						wv = string(b)
					}
					ok = got[i][0] == wn && got[i][1] == wv
				}
				if !ok {
					fail("grok %s groups(%q): go %v", lp.Name, fx.Ln, got)
				}
			}
		}
	}
	if err := sc.Err(); err != nil {
		t.Fatal(err)
	}
	t.Logf("checked %v; %d differences", counts, fails)
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
