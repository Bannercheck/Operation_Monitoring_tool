package parse

import (
	"bufio"
	"compress/gzip"
	"crypto/sha256"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"sync"
	"testing"

	"watchover/receiver/parse/py"
)

// Parity harness (docs/go-parse/PLAN.md section 2). Inputs are processed sequentially in scripts/parse_golden.py's
// order with one Normalizer and one MemStore, whole then live per input, so caches and learned profiles evolve as in
// the generator. Every input is always processed (a -run filter only selects which comparisons are reported);
// -parity.input restricts processing and then masks the report "profile" field (learned-profile history differs).

var (
	flagParityInput = flag.String("parity.input", "", "glob on golden input names; restricts processing and masks the report profile")
	flagParityYear  = flag.Int("parity.year", 2026, "current year injected into the Normalizer (goldens: 2026)")
	flagParityOut   = flag.String("parity.out", "", "also write the parity summary to this file")
	// Stress replays: goldens regenerated with scripts/parse_golden.py --batch N --out DIR (never the checked-in set).
	flagParityGolden = flag.String("parity.golden", goldenDir, "directory of the *.jsonl.gz goldens TestGolden compares against")
	flagParityBatch  = flag.Int("parity.batch", 40, "live batch size in lines (must match the --batch the goldens were made with)")
)

const (
	corpusDir = "../../samples/corpus"
	inputsDir = "testdata/inputs"
	goldenDir = "testdata/golden"
)

type goldenInput struct{ name, path string }

// goldenInputs lists samples/corpus (sorted, no *.md, files only) then testdata/inputs (sorted).
func goldenInputs() ([]goldenInput, error) {
	var out []goldenInput
	for _, dir := range []string{corpusDir, inputsDir} {
		ents, err := os.ReadDir(dir)
		if err != nil {
			return nil, err
		}
		names := []string{}
		for _, e := range ents {
			if e.IsDir() || (dir == corpusDir && filepath.Ext(e.Name()) == ".md") {
				continue
			}
			names = append(names, e.Name())
		}
		sort.Strings(names)
		for _, n := range names {
			out = append(out, goldenInput{n, filepath.Join(dir, n)})
		}
	}
	return out, nil
}

// readGolden returns the lines of testdata/golden/<input>.<mode>.jsonl.gz (without the final newline).
func readGolden(input, mode string) ([]string, error) {
	f, err := os.Open(filepath.Join(*flagParityGolden, input+"."+mode+".jsonl.gz"))
	if err != nil {
		return nil, err
	}
	defer f.Close()
	zr, err := gzip.NewReader(f)
	if err != nil {
		return nil, err
	}
	data, err := io.ReadAll(zr)
	if err != nil {
		return nil, err
	}
	s := strings.TrimSuffix(string(data), "\n")
	if s == "" {
		return nil, nil
	}
	return strings.Split(s, "\n"), nil
}

type modeResult struct {
	status       string // PASS, FAIL, ERR
	goN, goldN   int    // observation lines (report line excluded)
	excerpt      string // first differences, or the error
	hash         [32]byte
	batchFormats []string // live only
}

type inputResult struct {
	name      string
	formatKey string // whole golden report[0] format[:family]
	whole     modeResult
	live      modeResult
}

var profileRE = regexp.MustCompile(`"profile": "[0-9a-f]*"`)

func maskProfile(s string) string { return profileRE.ReplaceAllString(s, `"profile": ""`) }

func trunc(s string, n int) string {
	if py.Len(s) > n {
		return py.Head(s, n) + "..."
	}
	return s
}

// fieldDiff lists the top-level keys whose canonical JSON differs between two lines.
func fieldDiff(got, want string) string {
	gv, gerr := py.Loads(got)
	wv, werr := py.Loads(want)
	if gerr != nil || werr != nil || gv.K != py.KMap || wv.K != py.KMap {
		return fmt.Sprintf("      go:     %s\n      golden: %s\n", trunc(got, 300), trunc(want, 300))
	}
	keys := map[string]bool{}
	for _, k := range gv.M.Keys() {
		keys[k] = true
	}
	for _, k := range wv.M.Keys() {
		keys[k] = true
	}
	var ks []string
	for k := range keys {
		ks = append(ks, k)
	}
	sort.Strings(ks)
	var b strings.Builder
	for _, k := range ks {
		g, gok := gv.M.Get(k)
		w, wok := wv.M.Get(k)
		gs, _ := py.Dumps(g, true)
		ws, _ := py.Dumps(w, true)
		if !gok {
			gs = "<absent>"
		}
		if !wok {
			ws = "<absent>"
		}
		if gs != ws {
			fmt.Fprintf(&b, "      %s: go=%s golden=%s\n", k, trunc(gs, 200), trunc(ws, 200))
		}
	}
	return b.String()
}

// compareLines fills status / counts / excerpt.
func compareLines(goLines, golden []string, mask bool) modeResult {
	r := modeResult{goN: len(goLines) - 1, goldN: len(golden) - 1}
	h := sha256.New()
	for _, l := range goLines {
		h.Write([]byte(l))
		h.Write([]byte{'\n'})
	}
	copy(r.hash[:], h.Sum(nil))
	if mask && len(goLines) > 0 && len(golden) > 0 {
		goLines = append([]string{maskProfile(goLines[0])}, goLines[1:]...)
		golden = append([]string{maskProfile(golden[0])}, golden[1:]...)
	}
	var diffs []int
	n := len(goLines)
	if len(golden) > n {
		n = len(golden)
	}
	for i := 0; i < n && len(diffs) < 5; i++ {
		if i >= len(goLines) || i >= len(golden) || goLines[i] != golden[i] {
			diffs = append(diffs, i)
		}
	}
	if len(diffs) == 0 {
		r.status = "PASS"
		return r
	}
	r.status = "FAIL"
	var b strings.Builder
	fmt.Fprintf(&b, "    go %d obs, golden %d obs; first differing line indexes (0 = report): %v\n", r.goN, r.goldN, diffs)
	i := diffs[0]
	switch {
	case i >= len(goLines):
		fmt.Fprintf(&b, "    line %d missing in go output; golden: %s\n", i, trunc(golden[i], 300))
	case i >= len(golden):
		fmt.Fprintf(&b, "    line %d extra in go output: %s\n", i, trunc(goLines[i], 300))
	default:
		fmt.Fprintf(&b, "    line %d:\n%s", i, fieldDiff(goLines[i], golden[i]))
	}
	r.excerpt = b.String()
	return r
}

func errResult(err error) modeResult {
	return modeResult{status: "ERR", excerpt: "    error: " + err.Error() + "\n"}
}

// safe runs f, turning a panic into an error.
func safe(f func() error) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("panic: %v", r)
		}
	}()
	return f()
}

func goLinesOf(reps []Report, obs []Observation) []string {
	lines := make([]string, 0, len(obs)+1)
	lines = append(lines, string(CanonReport(reps)))
	for _, o := range obs {
		lines = append(lines, string(canonLineNaN(o)))
	}
	return lines
}

// computeParity runs every (filtered) input whole then live and compares with the goldens.
func computeParity(filter string, year int) ([]inputResult, error) {
	ins, err := goldenInputs()
	if err != nil {
		return nil, err
	}
	norm := NewNormalizer(year)
	store := NewMemStore()
	mask := filter != ""
	var out []inputResult
	for _, in := range ins {
		if filter != "" {
			if ok, _ := filepath.Match(filter, in.name); !ok {
				continue
			}
		}
		res := inputResult{name: in.name, formatKey: "?"}
		if wl, err := readGolden(in.name, "whole"); err == nil && len(wl) > 0 {
			if v, err := py.Loads(wl[0]); err == nil {
				if rv, _ := v.M.Get("report"); len(rv.L) > 0 && rv.L[0].K == py.KMap {
					f, _ := rv.L[0].M.Get("format")
					fam, _ := rv.L[0].M.Get("family")
					res.formatKey = FormatKey(f.S, fam.S)
				} else {
					res.formatKey = "-"
				}
			}
		}
		data, err := os.ReadFile(in.path)
		if err != nil {
			return nil, err
		}
		opts := Options{Profiles: store, Norm: norm}
		// whole
		var obs []Observation
		var reps []Report
		if err := safe(func() (e error) { obs, reps, e = IngestBytes(in.name, data, opts); return }); err != nil {
			res.whole = errResult(err)
		} else if golden, err := readGolden(in.name, "whole"); err != nil {
			res.whole = errResult(err)
		} else {
			res.whole = compareLines(goLinesOf(reps, obs), golden, mask)
		}
		// live
		var bf []string
		if err := safe(func() (e error) { obs, reps, bf, e = GoldenLive(in.name, data, *flagParityBatch, opts); return }); err != nil {
			res.live = errResult(err)
		} else if golden, err := readGolden(in.name, "live"); err != nil {
			res.live = errResult(err)
		} else {
			if len(reps) > 3 {
				reps = reps[:3]
			}
			res.live = compareLines(goLinesOf(reps, obs), golden, mask)
		}
		res.live.batchFormats = bf
		out = append(out, res)
	}
	return out, nil
}

var (
	parityOnce sync.Once
	parityRes  []inputResult
	parityErr  error
)

func runParity(t *testing.T) []inputResult {
	t.Helper()
	parityOnce.Do(func() { parityRes, parityErr = computeParity(*flagParityInput, *flagParityYear) })
	if parityErr != nil {
		t.Fatalf("parity run: %v", parityErr)
	}
	return parityRes
}

func distinct(xs []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, x := range xs {
		if !seen[x] {
			seen[x] = true
			out = append(out, x)
		}
	}
	return out
}

// summary renders the per-input table, the per-format totals and the live batch-format inventory.
func summary(res []inputResult) string {
	var b strings.Builder
	type tot struct{ whole, live, n int }
	byFmt := map[string]*tot{}
	pass := [2]int{}
	fmt.Fprintf(&b, "%-38s %-18s %-22s %-22s %s\n", "input", "format", "whole", "live", "live batch formats")
	cell := func(m modeResult) string {
		if m.status == "PASS" {
			return "PASS"
		}
		if m.status == "ERR" {
			return "ERR"
		}
		return fmt.Sprintf("FAIL(%d/%d)", m.goN, m.goldN)
	}
	for _, r := range res {
		bf := distinct(r.live.batchFormats)
		inv := strings.Join(bf, ",")
		if len(bf) == 1 && bf[0] == r.formatKey {
			inv = "="
		}
		lw, ll := "", ""
		if allowedInput(r.name, "whole") {
			lw = "*"
		}
		if allowedInput(r.name, "live") {
			ll = "*"
		}
		fmt.Fprintf(&b, "%-38s %-18s %-22s %-22s %s\n", r.name, r.formatKey, cell(r.whole)+lw, cell(r.live)+ll, inv)
		t := byFmt[r.formatKey]
		if t == nil {
			t = &tot{}
			byFmt[r.formatKey] = t
		}
		t.n++
		if r.whole.status == "PASS" {
			t.whole++
			pass[0]++
		}
		if r.live.status == "PASS" {
			t.live++
			pass[1]++
		}
	}
	fmt.Fprintf(&b, "\n%-18s %-12s %-12s\n", "format", "whole", "live")
	var fks []string
	for k := range byFmt {
		fks = append(fks, k)
	}
	sort.Strings(fks)
	for _, k := range fks {
		t := byFmt[k]
		fmt.Fprintf(&b, "%-18s %-12s %-12s\n", k, fmt.Sprintf("%d/%d", t.whole, t.n), fmt.Sprintf("%d/%d", t.live, t.n))
	}
	fmt.Fprintf(&b, "%-18s %-12s %-12s   (* = listed in allow.txt)\n", "TOTAL", fmt.Sprintf("%d/%d", pass[0], len(res)),
		fmt.Sprintf("%d/%d", pass[1], len(res)))
	// first difference per failing input
	b.WriteString("\nfirst differences:\n")
	for _, r := range res {
		for _, m := range []struct {
			mode string
			r    modeResult
		}{{"whole", r.whole}, {"live", r.live}} {
			if m.r.status != "PASS" {
				fmt.Fprintf(&b, "  %s/%s %s\n%s", r.name, m.mode, m.r.status, firstLines(m.r.excerpt, 6))
			}
		}
	}
	return b.String()
}

func firstLines(s string, n int) string {
	sc := bufio.NewScanner(strings.NewReader(s))
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	var b strings.Builder
	for i := 0; i < n && sc.Scan(); i++ {
		b.WriteString(sc.Text())
		b.WriteByte('\n')
	}
	return b.String()
}

func TestGolden(t *testing.T) {
	res := runParity(t)
	for _, r := range res {
		r := r
		for _, m := range []struct {
			mode string
			r    modeResult
		}{{"whole", r.whole}, {"live", r.live}} {
			m := m
			t.Run(r.name+"/"+m.mode, func(t *testing.T) {
				if m.r.status == "PASS" {
					return
				}
				msg := fmt.Sprintf("%s %s/%s:\n%s", m.r.status, r.name, m.mode, m.r.excerpt)
				if allowedInput(r.name, m.mode) {
					t.Error(msg)
					return
				}
				t.Log(msg)
				t.Skip("not in allow.txt")
			})
		}
	}
	s := summary(res)
	if testing.Verbose() {
		fmt.Print("\n" + s)
	}
	if *flagParityOut != "" {
		if err := os.WriteFile(*flagParityOut, []byte(s), 0o644); err != nil {
			t.Errorf("write %s: %v", *flagParityOut, err)
		}
	}
}

// TestAllowConsistency: every format key of allow.txt has all of its golden inputs listed in both modes and passing,
// and no failing live replay has a batch of that format.
func TestAllowConsistency(t *testing.T) {
	for input := range allow.inputs {
		if _, err := os.Stat(filepath.Join(goldenDir, input+".whole.jsonl.gz")); err != nil {
			t.Errorf("allow.txt lists %q, which has no golden", input)
		}
	}
	if len(allow.formats) == 0 {
		return
	}
	res := runParity(t)
	for f := range allow.formats {
		for _, r := range res {
			if r.formatKey == f {
				for _, m := range []struct {
					mode string
					r    modeResult
				}{{"whole", r.whole}, {"live", r.live}} {
					if !allowedInput(r.name, m.mode) {
						t.Errorf("format %s is allowed but %s/%s is not listed", f, r.name, m.mode)
					} else if m.r.status != "PASS" {
						t.Errorf("format %s is allowed but %s/%s is %s", f, r.name, m.mode, m.r.status)
					}
				}
			}
			if r.live.status != "PASS" {
				for _, bf := range r.live.batchFormats {
					if bf == f {
						t.Errorf("format %s is allowed but occurs as a batch format in the failing live replay of %s", f, r.name)
						break
					}
				}
			}
		}
	}
}

// TestCanonRoundTrip: every golden line survives py.Loads + py.Dumps(sort_keys) byte for byte (validates the JSON
// decoder and the canonical encoder independently of the parsers).
func TestCanonRoundTrip(t *testing.T) {
	files, err := filepath.Glob(filepath.Join(goldenDir, "*.jsonl.gz"))
	if err != nil {
		t.Fatal(err)
	}
	if len(files) != 240 {
		t.Errorf("found %d golden files, want 240", len(files))
	}
	lines := 0
	for _, f := range files {
		base := strings.TrimSuffix(filepath.Base(f), ".jsonl.gz")
		mode := base[strings.LastIndexByte(base, '.')+1:]
		input := strings.TrimSuffix(base, "."+mode)
		gl, err := readGolden(input, mode)
		if err != nil {
			t.Errorf("%s: %v", f, err)
			continue
		}
		bad := 0
		for i, l := range gl {
			lines++
			v, err := py.Loads(l)
			if err != nil {
				t.Errorf("%s line %d: Loads: %v", base, i, err)
				bad++
			} else if s, _ := py.Dumps(v, true); s != l {
				t.Errorf("%s line %d: round trip differs:\n  got  %s\n  want %s", base, i, trunc(s, 300), trunc(l, 300))
				bad++
			}
			if bad >= 3 {
				break
			}
		}
	}
	t.Logf("%d golden lines round-tripped", lines)
}

// TestDeterminism: a second full run in the same process (fresh Normalizer and store, GOMAXPROCS=1) produces the same
// bytes as the first.
func TestDeterminism(t *testing.T) {
	if testing.Short() {
		t.Skip("short")
	}
	first := runParity(t)
	prev := runtime.GOMAXPROCS(1)
	defer runtime.GOMAXPROCS(prev)
	second, err := computeParity(*flagParityInput, *flagParityYear)
	if err != nil {
		t.Fatal(err)
	}
	if len(first) != len(second) {
		t.Fatalf("input count %d vs %d", len(first), len(second))
	}
	for i := range first {
		a, b := first[i], second[i]
		if a.whole.status != b.whole.status || a.live.status != b.live.status || a.whole.hash != b.whole.hash || a.live.hash != b.live.hash {
			if a.whole.status == "ERR" && b.whole.status == "ERR" && a.live.status == "ERR" && b.live.status == "ERR" {
				continue
			}
			t.Errorf("%s: run 1 %s/%s vs run 2 %s/%s (output hashes differ: %v)", a.name, a.whole.status, a.live.status,
				b.whole.status, b.live.status, a.whole.hash != b.whole.hash || a.live.hash != b.live.hash)
		}
	}
}

// FuzzIngestNoPanic: IngestBytes / GoldenLive never panic (errors are fine).
func FuzzIngestNoPanic(f *testing.F) {
	ins, _ := goldenInputs()
	for i, in := range ins {
		if i%10 != 0 {
			continue
		}
		data, err := os.ReadFile(in.path)
		if err != nil {
			continue
		}
		if len(data) > 4096 {
			data = data[:4096]
		}
		f.Add(in.name, data)
	}
	f.Add("x.log", []byte("{\"a\": [1, 2"))
	f.Add("x.csv", []byte("a,\"b\nc,d"))
	f.Fuzz(func(t *testing.T, name string, data []byte) {
		norm := NewNormalizer(2026)
		_, _, err := IngestBytes(name, data, Options{Norm: norm, Profiles: NewMemStore()})
		if err != nil && !errors.Is(err, ErrForward) && !errors.Is(err, ErrPyRaise) {
			t.Fatalf("unexpected error class: %v", err)
		}
		_, _, _, _ = GoldenLive(name, data, 3, Options{Norm: norm})
	})
}
