package parse

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"watchover/receiver/parse/py"
)

// TestLitFilterExact: the literal prefilter of every family pattern never changes a match result, on every line of
// every golden input and the WP3 edge lines, plus hand-picked lines that carry the literals in odd places.
func TestLitFilterExact(t *testing.T) {
	var lines []string
	ins, err := goldenInputs()
	if err != nil {
		t.Fatal(err)
	}
	for _, in := range ins {
		b, err := os.ReadFile(in.path)
		if err != nil {
			t.Fatal(err)
		}
		lines = append(lines, py.SplitLines(string(b))...)
	}
	if b, err := os.ReadFile(filepath.Join("testdata", "wp3gen", "edge.txt")); err == nil {
		lines = append(lines, py.SplitLines(string(b))...)
	}
	lines = append(lines, "x mysqld", "mysqld_safe", "[MY-012345]", "ORA-00600", "xORA-00600", "ARC3 ", "ARC",
		"  at a.B(C.java:1)", "a.bException: ", "goroutine 7 [", "2024/01/01 00:00:00 main.go:3:", "1:M 01 Jan 2024",
		"haproxy[1]", "é sshd[2]", "DMFx", "SysOperationFoo", "BC12 ", "AOS ... session", "advanced to log sequence")
	matched, filtered := 0, 0
	for _, f := range families {
		for _, p := range f.pats {
			if p.lits == nil {
				continue
			}
			for _, ln := range lines {
				got, want := p.MatchString(ln), p.re.MatchString(ln)
				if got != want {
					t.Fatalf("%s %q: filtered %v, regexp %v (lits %q)", f.name, ln, got, want, p.lits)
				}
				if want {
					matched++
				}
				filtered++
			}
		}
	}
	if matched == 0 || filtered == 0 {
		t.Fatal("no coverage")
	}
}

// TestRequiredLits pins the derived literal sets of a few patterns (an empty set must never be derived).
func TestRequiredLits(t *testing.T) {
	cases := map[string]string{
		`\[MY-\d{6}\]`:                   "[MY-",
		famLB + `mysqld` + famRB:         "mysqld",
		`a+b|cd`:                         "a|cd",
		`x?y`:                            "y",
		`(?i:abc)`:                       "<nil>",
		`^\s+at [\w$.]+\([\w.]*:\d+\)|q`: "at |q",
		`a*|b`:                           "<nil>",
	}
	for pat, want := range cases {
		f := newLitFilter(py.MustCompile(pat))
		got := "<nil>"
		if f.lits != nil {
			got = strings.Join(f.lits, "|")
		}
		if got != want {
			t.Errorf("requiredLits(%q) = %q, want %q", pat, got, want)
		}
	}
	for _, f := range families {
		for _, p := range f.pats {
			if p.lits == nil {
				t.Logf("%s: no prefilter", f.name)
			}
		}
	}
}
