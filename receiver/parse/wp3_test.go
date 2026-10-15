package parse

import (
	"regexp"
	"strings"
	"testing"

	"watchover/receiver/parse/py"
)

// WP3 unit tests (PLAN.md section 3, WP3). Expected values live in wp3_cases_test.go, generated from Python 3.11.

func strp(s string) *string { return &s }

func dumpRecords(t *testing.T, p Parser, text string, fc *FamilyCtx) string {
	t.Helper()
	items := []py.Value{}
	if err := p.Records(text, fc, func(r Record) bool {
		items = append(items, py.ListOf(py.Int(int64(r.LineNo)), py.MapOf(r.Rec)))
		return true
	}); err != nil {
		t.Fatalf("%s.Records: %v", p.Name(), err)
	}
	s, err := py.Dumps(py.ListOf(items...), false)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

// TestParseHeadTable: P03 section 2.1 examples plus the Unicode / uncovered-branch edge lines (key order + rest).
func TestParseHeadTable(t *testing.T) {
	for _, c := range wp3HeadCases {
		rec, rest := parseHead(c.ln)
		got, _ := py.Dumps(py.ListOf(py.MapOf(rec), py.S(rest)), false)
		if got != c.want {
			t.Errorf("parseHead(%q)\n go %s\n py %s", c.ln, got, c.want)
		}
	}
}

func TestContMatchTable(t *testing.T) {
	for _, c := range wp3ContCases {
		if got := contMatch(c.ln); got != c.want {
			t.Errorf("contMatch(%q) = %v, want %v", c.ln, got, c.want)
		}
	}
}

// TestTsMatchEpoch: the epoch alternatives need trailing whitespace (Python lookahead), at most 6 fraction digits.
func TestTsMatchEpoch(t *testing.T) {
	cases := []struct {
		in, ts string
		end    int
		ok     bool
	}{
		{"1790068560 x", "1790068560", 11, true},
		{"1790068560\t\t x", "1790068560", 13, true},
		{"1790068560.123456 x", "1790068560.123456", 18, true},
		{"1790068560.1234567 x", "", 0, false},
		{"1790068560", "", 0, false},
		{"1790068560]: x", "", 0, false},
		{"1790068560123 x", "1790068560123", 14, true},
		{"[2026-09-22 09:14:02,003] INFO", "2026-09-22 09:14:02,003", 26, true},
		{"2026-09-22 09:14:02] x", "2026-09-22 09:14:02", 21, true},
	}
	for _, c := range cases {
		ts, end, ok := tsMatch(c.in)
		if ts != c.ts || end != c.end || ok != c.ok {
			t.Errorf("tsMatch(%q) = %q %d %v, want %q %d %v", c.in, ts, end, ok, c.ts, c.end, c.ok)
		}
	}
}

// TestLevelLoggerShared: the regexes WP4 / WP5 use directly (group 1 keeps the original spelling; re.I folds ı/İ).
func TestLevelLoggerShared(t *testing.T) {
	for in, want := range map[string]string{"Warning x": "Warning", "ınfo x": "ınfo", "İNFO: x": "İNFO", "[ERR] x": "ERR", "INFORMATION: x": "INFORMATION", "ſevere x": "ſevere"} {
		m := levelRE.FindStringSubmatch(in)
		if m == nil || m[1] != want {
			t.Errorf("levelRE(%q) = %v, want %q", in, m, want)
		}
	}
	for _, in := range []string{"ERROR", "WARNx y", "INFOé x"} {
		if levelRE.MatchString(in) {
			t.Errorf("levelRE(%q) matched", in)
		}
	}
	if m := loggerRE.FindStringSubmatch("payment_api.charge: x"); m == nil || m[1] != "payment_api.charge" {
		t.Errorf("loggerRE: %v", m)
	}
	if loggerRE.MatchString("main.go:42: x") {
		t.Error("loggerRE matched main.go:42:")
	}
}

func TestTextRecordsTable(t *testing.T) {
	for _, c := range wp3TextRecordCases {
		fc := &FamilyCtx{Hint: c.hint}
		if got := dumpRecords(t, TextParser{}, c.text, fc); got != c.want {
			t.Errorf("%s (hint %v)\n go %s\n py %s", c.name, c.hint, got, c.want)
		}
	}
}

// TestTextRecordsProbeStop: yield returning false stops the generator (the 50-record probe).
func TestTextRecordsProbeStop(t *testing.T) {
	var b strings.Builder
	for i := 0; i < 80; i++ {
		b.WriteString("2026-09-22 09:14:02 INFO line\n")
	}
	n := 0
	_ = TextParser{}.Records(b.String(), &FamilyCtx{}, func(Record) bool { n++; return n < 50 })
	if n != 50 {
		t.Fatalf("probe got %d records", n)
	}
}

func TestFamilyTable(t *testing.T) {
	for _, c := range wp3FamilyCases {
		if got := detectFamilyLines(c.lines); got != c.want {
			t.Errorf("%s: detectFamily = %q, want %q", c.name, got, c.want)
		}
		fc := &FamilyCtx{}
		if got := detectFamily(c.lines, fc); got != c.want || fc.Seen == nil || *fc.Seen != c.want {
			t.Errorf("%s: detectFamily with ctx = %q seen %v", c.name, got, fc.Seen)
		}
	}
}

// TestFamilyHint: a non-nil hint (even "") is returned verbatim, recorded in Seen, and disables detection.
func TestFamilyHint(t *testing.T) {
	lines := []string{"Traceback (most recent call last):"}
	for _, h := range []string{"", "redis"} {
		fc := &FamilyCtx{Hint: strp(h)}
		if got := detectFamily(lines, fc); got != h || fc.Seen == nil || *fc.Seen != h {
			t.Errorf("hint %q: got %q seen %v", h, got, fc.Seen)
		}
		if *fc.Hint != h {
			t.Error("hint modified")
		}
	}
	fc := &FamilyCtx{Hint: strp("")}
	got := dumpRecords(t, TextParser{}, "2026-09-22 09:14:02 x\nTraceback (most recent call last):\n", fc)
	if strings.Contains(got, `"service"`) {
		t.Errorf("empty hint still applied a family: %s", got)
	}
}

func TestGrokLinesTable(t *testing.T) {
	for _, c := range wp3GrokLineCases {
		var got []string
		for _, lp := range grokLines {
			if lp.rx.MatchString(c.ln) {
				got = append(got, lp.Name)
			}
		}
		if !equalStrings(got, c.want) {
			t.Errorf("grok LINES on %q: go %v py %v", c.ln, got, c.want)
		}
	}
}

func TestGrokRecordsTable(t *testing.T) {
	for _, c := range wp3GrokRecordCases {
		if got := dumpRecords(t, GrokParser{}, c.text, &FamilyCtx{}); got != c.want {
			t.Errorf("%s\n go %s\n py %s", c.name, got, c.want)
		}
	}
}

// TestGrokDetectSampling: detect_format samples the first 120 non-blank lines, GrokParser the non-blank lines among the
// first 120 physical lines; with 120 leading blank lines the format is grok but the parser finds no pattern (0 rows).
func TestGrokDetectSampling(t *testing.T) {
	var text string
	for _, c := range wp3GrokRecordCases {
		if c.name == "parser_sample_differs" {
			text = c.text
		}
	}
	var nonBlank []string
	for _, ln := range py.SplitLines(text) {
		if !isBlank(ln) {
			nonBlank = append(nonBlank, ln)
		}
	}
	lp, share := grokDetect(nonBlank)
	if lp == nil || lp.Name != wp3DetectSampling.fmtName || share != wp3DetectSampling.fmtShare {
		t.Errorf("format-side detect: %v %v", lp, share)
	}
	if lp, _ := grokDetect(py.SplitLines(text)); lp != nil || wp3DetectSampling.parName != "" {
		t.Errorf("parser-side detect: %v (python %q)", lp, wp3DetectSampling.parName)
	}
}

// TestGrokTieAndThreshold: RABBITMQ beats NODE_WINSTON on a tie (LINES order); 0.6 is tested on the unrounded share.
func TestGrokTieAndThreshold(t *testing.T) {
	ln := "2026-09-22 09:14:02.003 [info] <0.812.0> x"
	lp, share := grokDetect([]string{ln, ln})
	if lp == nil || lp.Name != "RABBITMQ" || share != 1 {
		t.Fatalf("tie: %v %v", lp, share)
	}
	// 3 of 5 = 0.6 passes; 0.5999 would not.
	lines := []string{ln, ln, ln, "a", "b"}
	if lp, share := grokDetect(lines); lp == nil || share != 0.6 {
		t.Errorf("3/5: %v %v", lp, share)
	}
	if lp, share := grokDetect([]string{ln, "a"}); lp != nil || share != 0 {
		t.Errorf("1/2: %v %v", lp, share)
	}
	// rounding: 2/3 -> 0.67
	if _, share := grokDetect([]string{ln, ln, "a"}); share != 0.67 {
		t.Errorf("2/3 share %v", share)
	}
}

// TestGrokDHCPDRemoved: DHCPD can never match in Python, so it is not in the Go list (and with a naive Go WORD it would).
func TestGrokDHCPDRemoved(t *testing.T) {
	for _, lp := range grokLines {
		if lp.Name == "DHCPD" {
			t.Fatal("DHCPD present")
		}
	}
	if len(grokLines) != len(grokLineSrc)-1 {
		t.Fatalf("grokLines %d, LINES %d", len(grokLines), len(grokLineSrc))
	}
	ln := "Sep 22 09:14:02 host dhcpd[1]: DHCPACK on 10.0.0.5 to aa:bb:cc:dd:ee:ff (h) via eth0"
	if !naiveGrok(t, "DHCPD", nil).MatchString(ln) {
		t.Error("naive DHCPD should match in Go (that is why it is removed)")
	}
}

// naiveGrok compiles a LINES pattern with the Go BASE overrides except the ones named in drop (restored to a plain
// ASCII-\b form), and without the SSHD line fix when "SSHD_AUTH" is in drop.
func naiveGrok(t *testing.T, name string, drop []string) *regexp.Regexp {
	t.Helper()
	lib := map[string]string{}
	for k, v := range grokBase {
		lib[k] = v
	}
	for k, v := range grokGoBase {
		lib[k] = v
	}
	naive := map[string]string{"SYSLOGFACILITY": `<%{NONNEGINT:facility}.%{NONNEGINT:priority}>`}
	for _, d := range drop {
		if v, ok := naive[d]; ok {
			lib[d] = v
		}
	}
	for _, d := range grokLineSrc {
		if d.Name != name {
			continue
		}
		pat := d.Pattern
		if f, ok := grokLineFix[name]; ok && !containsStr(drop, name) {
			pat = strings.Replace(pat, f[0], f[1], 1)
		}
		return regexp.MustCompile(py.TranslateKeepB(grokExpand(pat, lib, 0)))
	}
	t.Fatalf("no LINES pattern %s", name)
	return nil
}

func containsStr(xs []string, s string) bool {
	for _, x := range xs {
		if x == s {
			return true
		}
	}
	return false
}

// TestGrokOverridesNecessary: without the SYSLOGFACILITY / SSHD rewrites, Go would match lines Python rejects.
func TestGrokOverridesNecessary(t *testing.T) {
	fac := "Sep 22 09:14:02 <1é2> host prog: m"
	port := "Sep 22 09:14:02 host sshd[42]: Accepted password for ali from 10.0.0.1 port 22é"
	pyMatches := map[string][]string{}
	for _, c := range wp3GrokLineCases {
		pyMatches[c.ln] = c.want
	}
	if containsStr(pyMatches[fac], "SYSLOGLINE") || containsStr(pyMatches[port], "SSHD_AUTH") {
		t.Fatal("case table disagrees with the premise")
	}
	if !naiveGrok(t, "SYSLOGLINE", []string{"SYSLOGFACILITY"}).MatchString(fac) {
		t.Error("naive SYSLOGFACILITY should accept <1é2> in Go")
	}
	if !naiveGrok(t, "SSHD_AUTH", []string{"SSHD_AUTH"}).MatchString(port) {
		t.Error("naive SSHD port should accept 22é in Go")
	}
	for _, lp := range grokLines {
		if (lp.Name == "SYSLOGLINE" && lp.rx.MatchString(fac)) || (lp.Name == "SSHD_AUTH" && lp.rx.MatchString(port)) {
			t.Errorf("%s still matches the edge line", lp.Name)
		}
	}
}

// TestGrokSourcesSix: the Go-expanded sources of the six golden-detected patterns have the P03 section 5.7.5 sizes
// (the spec counts code points: MONTH holds a literal ä).
func TestGrokSourcesSix(t *testing.T) {
	want := map[string]int{"BIND9_QUERY": 1740, "DOCKER_DAEMON": 604, "NAGIOS_LOG": 139, "RABBITMQ": 585, "SQUID_ACCESS": 2474, "VMWARE_ESXI": 838}
	for _, d := range grokLineSrc {
		if n, ok := want[d.Name]; ok {
			if got := py.Len(grokGoSource(d.Name, d.Pattern)); got != n {
				t.Errorf("%s: %d code points, spec %d", d.Name, got, n)
			}
		}
	}
}
