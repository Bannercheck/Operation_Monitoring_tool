package parse

import (
	"errors"
	"strconv"
	"testing"
)

// WP6 unit tests: SAP parser (P05). The Python-probed tables live in wp6_cases_test.go (testdata/wp6gen/gen_cases.py);
// wp4Run / wp4Same (wp4_test.go) run a parser and compare with the Python json.dumps output.

// TestWP6Cases covers the branches no golden reaches (P05 section 9): LOGHEADER meta, NW Java blank-line termination /
// stack-trace continuation / single-line records / msg id / application / "/"-origin service / no tz, availability
// (ties-to-even minutes, ValueError -> ErrPyRaise), tp / R3trans, HANA d, GC level tags and thresholds, classic GC
// rounding, JUL PM / 12 AM / unpaired headers, dev trace META swallowing, SM21 without date, ALOG WRN/START,
// end-of-text records with line_no 1, the stable class-list sort and the Unicode rewrites of section 2.
func TestWP6Cases(t *testing.T) {
	for i, c := range wp6Cases {
		got := wp4Run(SapParser{}, c.text)
		if !wp4Same(got, c.want) {
			t.Errorf("case %d %q:\n got  %s\n want %s", i, c.text, got, c.want)
		}
	}
}

func TestWP6Score(t *testing.T) {
	for i, c := range wp6Scores {
		want, err := strconv.ParseFloat(c.want, 64)
		if err != nil {
			t.Fatal(err)
		}
		if got := sapScore(c.lines); got != want {
			t.Errorf("case %d sapScore(%q) = %v, want %v", i, c.lines, got, want)
		}
	}
}

func TestWP6SevFromWords(t *testing.T) {
	for _, c := range wp6Words {
		if got := sevFromWords(c[0], "D"); got != c[1] {
			t.Errorf("sevFromWords(%q) = %q, want %q", c[0], got, c[1])
		}
	}
}

func TestWP6SvcFromLogger(t *testing.T) {
	for _, c := range wp6Loggers {
		if got := svcFromLogger(c[0]); got != c[1] {
			t.Errorf("svcFromLogger(%q) = %q, want %q", c[0], got, c[1])
		}
	}
}

// TestWP6ProbeStop: the 50-record probe stops the generator; no end-of-text record or later error may follow.
func TestWP6ProbeStop(t *testing.T) {
	text := "4 ETW000 a\n4 ETW000 b\nAvailable 31.09.2021 18:51:33 - 24.10.2021 18:50:03\nx/Y.class"
	n := 0
	err := SapParser{}.Records(text, &FamilyCtx{}, func(Record) bool { n++; return n < 2 })
	if err != nil || n != 2 {
		t.Fatalf("stop after 2: n=%d err=%v", n, err)
	}
	err = SapParser{}.Records(text, &FamilyCtx{}, func(Record) bool { return true })
	if !errors.Is(err, ErrPyRaise) {
		t.Fatalf("full run: want ErrPyRaise, got %v", err)
	}
}

func TestWP6Iso(t *testing.T) {
	for _, c := range []struct {
		got, want string
	}{
		{iso(2026, 9, 16, 2, 14, 7, "000"), "2026-09-16T02:14:07.000"},
		{iso(2026, 9, 16, 2, 14, 7, ""), "2026-09-16T02:14:07"},
		{iso(26, 13, 31, 25, 61, 61, "\U00000665"), "0026-13-31T25:61:61.005"},
		{iso(sapInt("\U00000662\U00000660\U00000662\U00000666"), 1, 2, 3, 4, 5, ""), "2026-01-02T03:04:05"},
	} {
		if c.got != c.want {
			t.Errorf("iso = %q, want %q", c.got, c.want)
		}
	}
}

// TestWP6StaleNwjProbe: the KeyError of a stale NW Java record is raised only when the generator is resumed; a probe
// that stops right after the single-line record sees no error.
func TestWP6StaleNwjProbe(t *testing.T) {
	text := "#2.0 #2026 09 16 02:14:15#+0300#Info#a#\n#2.0 #2026 09 16 02:14:16#+0300#Info#b#c#d#e#\nmore"
	n := 0
	if err := (SapParser{}).Records(text, &FamilyCtx{}, func(Record) bool { n++; return n < 2 }); err != nil || n != 2 {
		t.Fatalf("probe: n=%d err=%v", n, err)
	}
	if err := (SapParser{}).Records(text, &FamilyCtx{}, func(Record) bool { return true }); !errors.Is(err, ErrPyRaise) {
		t.Fatalf("full run: want ErrPyRaise, got %v", err)
	}
}
