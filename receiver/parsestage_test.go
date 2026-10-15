package main

import (
	"bufio"
	"bytes"
	"compress/gzip"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"

	"watchover/receiver/parse"
	"watchover/receiver/parse/py"
)

// ---------------------------------------------------------------- helpers

type spRec struct {
	hdr  map[string]any
	body []byte
}

func ingestRec(agent, file, body string) spRec {
	return spRec{hdr: map[string]any{"v": 1, "path": "/ingest", "file": file, "ip": "10.0.0.9", "ts": time.Now().UTC().Format(time.RFC3339Nano),
		"identity": "token", "agent": agent, "agent_id": 7, "env": "prod", "site": "ist"}, body: []byte(body)}
}

func encodeRecs(recs []spRec) []byte {
	var b []byte
	for _, r := range recs {
		r.hdr["len"] = len(r.body)
		hb, _ := json.Marshal(r.hdr)
		b = frame(b, hb, r.body)
	}
	return b
}

func writeRaw(t *testing.T, dir, ns string, recs []spRec) []byte {
	t.Helper()
	b := encodeRecs(recs)
	if err := os.WriteFile(filepath.Join(dir, "seg-"+ns+rawExt), b, 0o640); err != nil {
		t.Fatal(err)
	}
	return b
}

type outRec struct {
	hdrRaw []byte
	hdr    map[string]any
	body   []byte
}

func readSpoolFile(t *testing.T, path string) []outRec {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var out []outRec
	for i := 0; i < len(data); {
		nl := bytes.IndexByte(data[i:], '\n')
		if nl < 0 {
			t.Fatalf("%s: torn header at %d", path, i)
		}
		var h map[string]any
		d := json.NewDecoder(bytes.NewReader(data[i : i+nl]))
		d.UseNumber()
		if err := d.Decode(&h); err != nil {
			t.Fatalf("%s: header: %v", path, err)
		}
		n, _ := h["len"].(json.Number).Int64()
		body := data[i+nl+1 : i+nl+1+int(n)]
		if data[i+nl+1+int(n)] != '\n' {
			t.Fatalf("%s: record not newline-terminated", path)
		}
		out = append(out, outRec{hdrRaw: data[i : i+nl], hdr: h, body: body})
		i += nl + 1 + int(n) + 1
	}
	return out
}

func testStage(t *testing.T, dir string, mode parseMode) *parseStage {
	t.Helper()
	cfg := config{spool: dir, parse: mode, parseWorkers: 4, parseTimeout: 5 * time.Second, fsync: true, shadowMax: 64 << 20}
	st := newParseStage(cfg, nil)
	n := parse.NewNormalizer(2026) // the goldens' year, non-strict like the goldens
	st.norm = n
	return st
}

func jsonl(n int, svc string) string {
	var b strings.Builder
	for i := 0; i < n; i++ {
		fmt.Fprintf(&b, `{"ts": "2026-10-15T05:58:%02d+00:00", "level": "error", "service": %q, "host": "h1", "msg": "boom %d"}`+"\n", i%60, svc, i)
	}
	return b.String()
}

// ---------------------------------------------------------------- tests

// TestParsedHeaderFields: a v=2 record keeps every v=1 header key with its value, adds the new keys, and its body is
// exactly the canonical observation lines of parse.IngestBytes.
func TestParsedHeaderFields(t *testing.T) {
	dir := t.TempDir()
	body := `{"ts": "2026-10-15T05:58:39.5+03:00", "level": "warn", "service": "api", "host": "h1", "msg": "one object"}`
	writeRaw(t, dir, "0000000000000000001", []spRec{ingestRec("srv-1", "app.json", body)})
	st := testStage(t, dir, parseOn)
	if n := st.drainOnce(context.Background()); n != 1 {
		t.Fatalf("records %d", n)
	}
	if _, err := os.Stat(filepath.Join(dir, "seg-0000000000000000001.raw")); !os.IsNotExist(err) {
		t.Fatalf("raw segment not removed: %v", err)
	}
	out := readSpoolFile(t, filepath.Join(dir, "pseg-0000000000000000001.wo"))
	if len(out) != 1 {
		t.Fatalf("records out %d", len(out))
	}
	h := out[0].hdr
	for k, want := range map[string]string{"v": "2", "kind": "parsed", "path": "/ingest", "file": "app.json", "agent": "srv-1", "agent_id": "7",
		"env": "prod", "site": "ist", "identity": "token", "ip": "10.0.0.9", "raw_len": fmt.Sprint(len(body)), "n": "1", "hint_format": "jsonl",
		"confidence": "0.95", "parser_version": parse.Version()} {
		if got := fmt.Sprint(h[k]); got != want {
			t.Errorf("header %s = %q, want %q", k, got, want)
		}
	}
	if h["ts"] == nil || h["len"].(json.Number).String() != fmt.Sprint(len(out[0].body)) {
		t.Errorf("ts/len: %v %v", h["ts"], h["len"])
	}
	rep, ok := h["report"].(map[string]any)
	if !ok || rep["format"] != h["format"] || fmt.Sprint(rep["rows"]) != "1" {
		t.Errorf("report: %v (format %v)", h["report"], h["format"])
	}
	obs, reps, err := parse.IngestBytes("app.json", []byte(body), parse.Options{Norm: parse.NewNormalizer(2026), Profiles: parse.NewMemStore()})
	if err != nil {
		t.Fatal(err)
	}
	var want []byte
	for _, o := range obs {
		ln, _ := parse.SpoolLine(o)
		want = append(append(want, ln...), '\n')
	}
	if !bytes.Equal(out[0].body, want) {
		t.Errorf("body\n got %s\nwant %s", out[0].body, want)
	}
	if reps[0].Format != h["format"] {
		t.Errorf("format %v vs %v", h["format"], reps[0].Format)
	}
	// The hint learned for the stream is the json -> jsonl single-object rule, as in live.py.
	if hint := st.hints.Get(parse.StreamKey{Agent: "srv-1", File: "app.json"}); hint == nil || hint.Format != "jsonl" {
		t.Errorf("hint %+v", hint)
	}
}

// TestParseStageOrderAndFallback: mixed records keep input order; parsed vs raw per batch, raw byte-identical; panic,
// timeout, deny, side table and /rum fall back to raw with their reason; an empty parse still yields a v=2 record.
func TestParseStageOrderAndFallback(t *testing.T) {
	dir := t.TempDir()
	syslog := "<34>Oct 11 22:14:15 mymachine su: 'su root' failed for lonvick on /dev/pts/8\n"
	recs := []spRec{
		ingestRec("a", "app.jsonl", jsonl(5, "api")),
		{hdr: map[string]any{"v": 1, "path": "/rum", "app": "shop", "app_key": "k", "origin": "", "ua": "x", "ts": "2026-10-15T00:00:00Z"}, body: []byte(`{"events":[]}`)},
		ingestRec("b", "panic.jsonl", jsonl(3, "p")),
		ingestRec("a", "app.jsonl", jsonl(7, "api")),
		ingestRec("c", "sys.log", syslog),
		ingestRec("d", "inventory.csv", "host,ip\nh1,10.0.0.1\n"),
		ingestRec("e", "slow.jsonl", jsonl(2, "s")),
		ingestRec("f", "blank.log", "   \n"),
		ingestRec("a", "app.jsonl", jsonl(1, "api")),
	}
	raw := writeRaw(t, dir, "0000000000000000005", recs)
	st := testStage(t, dir, parseOn)
	st.timeout = 100 * time.Millisecond
	st.deny = map[string]bool{"syslog": true}
	st.parseFn = func(name string, data []byte, o parse.Options) ([]parse.Observation, []parse.Report, error) {
		switch name {
		case "panic.jsonl":
			panic("boom")
		case "slow.jsonl":
			time.Sleep(400 * time.Millisecond)
		}
		return parse.IngestBytes(name, data, o)
	}
	if n := st.drainOnce(context.Background()); n != len(recs) {
		t.Fatalf("records %d", n)
	}
	out := readSpoolFile(t, filepath.Join(dir, "pseg-0000000000000000005.wo"))
	if len(out) != len(recs) {
		t.Fatalf("records out %d, want %d", len(out), len(recs))
	}
	// raw records of the input, by index
	in := readSpoolBytes(t, raw)
	wantV := []string{"2", "1", "1", "2", "1", "1", "1", "2", "2"}
	for i, o := range out {
		if got := fmt.Sprint(o.hdr["v"]); got != wantV[i] {
			t.Errorf("record %d (%v): v=%s want %s", i, o.hdr["file"], got, wantV[i])
		}
		if fmt.Sprint(o.hdr["file"]) != fmt.Sprint(recs[i].hdr["file"]) || fmt.Sprint(o.hdr["path"]) != fmt.Sprint(recs[i].hdr["path"]) {
			t.Errorf("record %d out of order: %v", i, o.hdr)
		}
		if wantV[i] == "1" && (!bytes.Equal(o.hdrRaw, in[i].hdrRaw) || !bytes.Equal(o.body, in[i].body)) {
			t.Errorf("record %d: forwarded record not byte-identical", i)
		}
	}
	if !bytes.Contains(out[0].hdrRaw, []byte(`"confidence":1.0,`)) {
		t.Errorf("confidence must stay a float for Python (1.0, not 1): %s", out[0].hdrRaw)
	}
	if fmt.Sprint(out[7].hdr["n"]) != "0" || len(out[7].body) != 0 {
		t.Errorf("blank batch: %v %q", out[7].hdr, out[7].body)
	}
	for r, want := range map[string]int64{"panic": 1, "timeout": 1, "format": 1, "table": 1, "path": 1} {
		if got := st.stats.reasons[r].Load(); got != want {
			t.Errorf("reason %s = %d, want %d", r, got, want)
		}
	}
	if st.stats.parsed.Load() != 4 || st.stats.forwarded.Load() != 4 {
		t.Errorf("parsed %d forwarded %d", st.stats.parsed.Load(), st.stats.forwarded.Load())
	}
	h := map[string]any{}
	st.health(h)
	by := h["parse_by_format"].(map[string]map[string]int64)
	if by["jsonl"]["parsed"] != 3 || by["jsonl"]["obs"] != 13 || by["syslog"]["forwarded"] != 1 {
		t.Errorf("by format %v", by)
	}
	// The slow stream still learned its hint (the late result is used for the hint, not emitted).
	if st.hints.Get(parse.StreamKey{Agent: "e", File: "slow.jsonl"}) == nil {
		t.Error("timed-out batch did not update the hint")
	}
}

func readSpoolBytes(t *testing.T, b []byte) []outRec {
	t.Helper()
	p := filepath.Join(t.TempDir(), "x")
	_ = os.WriteFile(p, b, 0o640)
	return readSpoolFile(t, p)
}

// TestParseStageSticky: an unhinted batch that Go cannot parse (Python can) hands the stream to Python until the next
// re-detection (live.py: every 200th batch), then Go parses it again with the hint in lockstep.
func TestParseStageSticky(t *testing.T) {
	dir := t.TempDir()
	var recs []spRec
	for i := 0; i < 205; i++ {
		recs = append(recs, ingestRec("a", "app.jsonl", jsonl(1, fmt.Sprint(i))))
	}
	writeRaw(t, dir, "0000000000000000009", recs)
	st := testStage(t, dir, parseOn)
	calls := 0
	st.parseFn = func(name string, data []byte, o parse.Options) ([]parse.Observation, []parse.Report, error) {
		calls++
		if calls == 1 {
			return nil, nil, fmt.Errorf("%w: unverified dateutil shape", parse.ErrForward)
		}
		return parse.IngestBytes(name, data, o)
	}
	st.drainOnce(context.Background())
	out := readSpoolFile(t, filepath.Join(dir, "pseg-0000000000000000009.wo"))
	var vs []string
	for _, o := range out {
		vs = append(vs, fmt.Sprint(o.hdr["v"]))
	}
	// batch 1 error, 2..199 sticky (Python's n = 2..199), batch 200 re-detect (parsed, learns n=1), 201.. hinted.
	for i, v := range vs {
		want := "1"
		if i >= 199 {
			want = "2"
		}
		if v != want {
			t.Fatalf("batch %d: v=%s want %s (all: %v)", i+1, v, want, vs)
		}
	}
	if got := st.stats.reasons["sticky"].Load(); got != 198 {
		t.Errorf("sticky %d", got)
	}
	if calls != 1+6 {
		t.Errorf("parse calls %d (sticky batches must not be parsed)", calls)
	}
	if h := st.hints.Get(parse.StreamKey{Agent: "a", File: "app.jsonl"}); h == nil || h.N != 6 {
		t.Errorf("hint after re-detect %+v", h)
	}
}

// TestParseStageResume: what a crash leaves behind is settled without loss or duplicates.
func TestParseStageResume(t *testing.T) {
	dir := t.TempDir()
	one := []spRec{ingestRec("a", "app.jsonl", jsonl(2, "x")), ingestRec("a", "app.jsonl", jsonl(3, "x"))}
	// (1) incomplete pseg tmp next to its raw segment: dropped and redone
	writeRaw(t, dir, "0000000000000000001", one)
	_ = os.WriteFile(filepath.Join(dir, "pseg-0000000000000000001.tmp"), []byte("partial"), 0o640)
	// (2) complete pseg tmp whose raw segment is gone: published
	_ = os.WriteFile(filepath.Join(dir, "pseg-0000000000000000002.tmp"), encodeRecs([]spRec{ingestRec("b", "x.log", "hello")}), 0o640)
	// (3) published pseg whose raw segment was not removed yet: the raw one is dropped, not parsed twice
	writeRaw(t, dir, "0000000000000000003", one)
	_ = os.WriteFile(filepath.Join(dir, "pseg-0000000000000000003.wo"), encodeRecs([]spRec{ingestRec("c", "y.log", "done")}), 0o640)
	recoverParseSpool(dir, parseOn)
	st := testStage(t, dir, parseOn)
	if n := st.drainOnce(context.Background()); n != 2 {
		t.Fatalf("records parsed %d, want 2", n)
	}
	left, _ := filepath.Glob(filepath.Join(dir, "*"))
	var names []string
	for _, l := range left {
		names = append(names, filepath.Base(l))
	}
	sort.Strings(names)
	want := []string{"pseg-0000000000000000001.wo", "pseg-0000000000000000002.wo", "pseg-0000000000000000003.wo"}
	if !reflect.DeepEqual(names, want) {
		t.Fatalf("files %v", names)
	}
	if out := readSpoolFile(t, filepath.Join(dir, "pseg-0000000000000000001.wo")); len(out) != 2 || fmt.Sprint(out[1].hdr["n"]) != "3" {
		t.Errorf("redone segment: %d records", len(out))
	}
	if out := readSpoolFile(t, filepath.Join(dir, "pseg-0000000000000000003.wo")); len(out) != 1 || out[0].hdr["file"] != "y.log" {
		t.Errorf("published pseg was rewritten")
	}
	// (4) switched off: unparsed raw segments go back to Python as seg-*.wo
	dir2 := t.TempDir()
	writeRaw(t, dir2, "0000000000000000004", one)
	recoverParseSpool(dir2, parseOff)
	if _, err := os.Stat(filepath.Join(dir2, "seg-0000000000000000004.wo")); err != nil {
		t.Errorf("raw segment not handed back: %v", err)
	}
}

// TestParseStageWithSpool: the writer closes .raw segments when the stage is on, the running stage turns them into
// pseg-*.wo, and /health + /metrics show the stage.
func TestParseStageWithSpool(t *testing.T) {
	dir := t.TempDir()
	cfg := config{spool: dir, authFile: filepath.Join(dir, "auth.json"), maxBody: 1 << 20, segBytes: 1 << 20, maxBacklog: 1 << 30,
		segEvery: 40 * time.Millisecond, agentDir: dir, parse: parseOn, parseWorkers: 2, parseTimeout: time.Second}
	_ = os.WriteFile(cfg.authFile, []byte(`{"open": true}`), 0o640)
	sp, err := newSpool(cfg)
	if err != nil {
		t.Fatal(err)
	}
	a := &auth{path: cfg.authFile}
	a.reload()
	s := &server{cfg: cfg, auth: a, sp: sp, start: time.Now()}
	s.stage = newParseStage(cfg, sp)
	sp.onClose = s.stage.wake
	ctx, cancel := context.WithCancel(context.Background())
	wd, pd := make(chan struct{}), make(chan struct{})
	go func() { sp.run(ctx); close(wd) }()
	go func() { s.stage.run(ctx); close(pd) }()
	ts := httptest.NewServer(s.routes())
	defer ts.Close()
	for i := 0; i < 3; i++ {
		req, _ := http.NewRequest("POST", ts.URL+"/ingest", strings.NewReader(jsonl(4, "api")))
		req.Header.Set("X-File-Name", "app.jsonl")
		req.Header.Set("X-Agent", "srv-9")
		resp, err := http.DefaultClient.Do(req)
		if err != nil || resp.StatusCode != 200 {
			t.Fatalf("post: %v %v", err, resp)
		}
		resp.Body.Close()
	}
	deadline := time.Now().Add(5 * time.Second)
	for s.stage.stats.parsed.Load() < 3 && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if s.stage.stats.parsed.Load() != 3 {
		t.Fatalf("parsed %d", s.stage.stats.parsed.Load())
	}
	time.Sleep(50 * time.Millisecond)
	if m, _ := filepath.Glob(filepath.Join(dir, "seg-*.wo")); len(m) != 0 {
		t.Errorf("raw segments visible to the dashboard: %v", m)
	}
	ps, _ := filepath.Glob(filepath.Join(dir, "pseg-*.wo"))
	n := 0
	for _, p := range ps {
		n += len(readSpoolFile(t, p))
	}
	if n != 3 {
		t.Errorf("pseg records %d", n)
	}
	resp, _ := http.Get(ts.URL + "/metrics")
	mb, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	for _, want := range []string{`watchover_receiver_parse_batches{format="jsonl",result="parsed"} 3`, "watchover_receiver_parse_enabled 1",
		"watchover_receiver_parse_observations 12"} {
		if !strings.Contains(string(mb), want) {
			t.Errorf("/metrics lacks %q:\n%s", want, mb)
		}
	}
	var hj map[string]any
	resp, _ = http.Get(ts.URL + "/health")
	_ = json.NewDecoder(resp.Body).Decode(&hj)
	resp.Body.Close()
	if hj["parse_mode"] != "on" || hj["parse_by_format"] == nil {
		t.Errorf("/health: %v", hj)
	}
	cancel()
	<-wd
	<-pd
}

// TestParseStageShadow: shadow mode leaves the raw segment to the dashboard and writes raw + parsed pairs.
func TestParseStageShadow(t *testing.T) {
	dir := t.TempDir()
	recs := []spRec{ingestRec("a", "app.jsonl", jsonl(2, "x")), ingestRec("c", "sys.log", "<34>Oct 11 22:14:15 m su: hi\n")}
	b := encodeRecs(recs)
	_ = os.WriteFile(filepath.Join(dir, "seg-0000000000000000007.wo"), b, 0o640)
	_ = os.Link(filepath.Join(dir, "seg-0000000000000000007.wo"), filepath.Join(dir, "seg-0000000000000000007"+shadowExt))
	st := testStage(t, dir, parseShadow)
	st.deny = map[string]bool{"syslog": true}
	st.drainOnce(context.Background())
	if _, err := os.Stat(filepath.Join(dir, "seg-0000000000000000007.wo")); err != nil {
		t.Fatal("shadow mode must not touch the dashboard's segment")
	}
	out := readSpoolFile(t, filepath.Join(dir, "shadow-0000000000000000007.wo"))
	var vs []string
	for _, o := range out {
		vs = append(vs, fmt.Sprint(o.hdr["v"], o.hdr["shadow"]))
	}
	if !reflect.DeepEqual(vs, []string{"1<nil>", "2true", "1<nil>"}) {
		t.Errorf("shadow records %v", vs)
	}
}

// TestHintCacheParity replays every golden input through the stage (40-line batches of one stream, after the input's
// whole run, in the generator's order with one profile store and one Normalizer) and compares the v=2 bodies with
// testdata/golden/<input>.live.jsonl.gz and the first three batch reports with the golden report line.
func TestHintCacheParity(t *testing.T) {
	if testing.Short() {
		t.Skip("short")
	}
	inputs := goldenInputList(t)
	dir := t.TempDir()
	st := testStage(t, dir, parseOn)
	pass, forwarded := 0, map[string]int{}
	for i, in := range inputs {
		data, err := os.ReadFile(in)
		if err != nil {
			t.Fatal(err)
		}
		name := filepath.Base(in)
		// the whole run first (it teaches the shared profile store, as in scripts/parse_golden.py)
		_, _, _ = parse.IngestBytes(name, data, parse.Options{Profiles: st.profiles, Norm: st.norm})
		var recs []spRec
		for _, c := range liveChunks(data, 40) {
			recs = append(recs, ingestRec("lt-host", name, string(c)))
		}
		ns := fmt.Sprintf("%019d", i+1)
		writeRaw(t, dir, ns, recs)
		st.drainOnce(context.Background())
		var got []string
		var reports []any
		ok := true
		out := readSpoolFile(t, filepath.Join(dir, "pseg-"+ns+".wo"))
		for bi, o := range out {
			if fmt.Sprint(o.hdr["v"]) != "2" {
				forwarded[name]++
				ok = false
				continue
			}
			if bi < 3 {
				reports = append(reports, o.hdr["report"])
			}
			for _, ln := range strings.Split(strings.TrimSuffix(string(o.body), "\n"), "\n") {
				if ln != "" { // the golden sorts attribute keys; the spool keeps their order
					v, err := py.Loads(ln)
					if err != nil {
						t.Fatalf("%s: %v", name, err)
					}
					d, _ := py.Dumps(v, true)
					got = append(got, d)
				}
			}
		}
		want := readGoldenLines(t, name+".live.jsonl.gz")
		if !ok {
			t.Errorf("%s: %d batch(es) forwarded raw", name, forwarded[name])
			continue
		}
		var grep map[string]any
		d := json.NewDecoder(strings.NewReader(want[0]))
		d.UseNumber()
		_ = d.Decode(&grep)
		if !reflect.DeepEqual(grep["report"], reports) && !(len(reports) == 0 && len(grep["report"].([]any)) == 0) {
			t.Errorf("%s: batch reports differ\n got %v\nwant %v", name, reports, grep["report"])
		}
		want = want[1:]
		if len(got) != len(want) {
			t.Errorf("%s: %d observations, golden %d", name, len(got), len(want))
			continue
		}
		bad := 0
		for k := range want {
			if got[k] != want[k] {
				if bad == 0 {
					t.Errorf("%s line %d\n got %s\nwant %s", name, k, got[k], want[k])
				}
				bad++
			}
		}
		if bad == 0 {
			pass++
		}
	}
	t.Logf("live parity through the stage: %d/%d inputs", pass, len(inputs))
	if pass != len(inputs) {
		t.Errorf("forwarded: %v", forwarded)
	}
}

// liveChunks: scripts/parse_golden.py batching (split on \n, 40 elements joined, whitespace-only chunks skipped).
func liveChunks(data []byte, batch int) [][]byte {
	lines := bytes.Split(data, []byte("\n"))
	var out [][]byte
	for i := 0; i < len(lines); i += batch {
		j := min(i+batch, len(lines))
		c := bytes.Join(lines[i:j], []byte("\n"))
		if len(bytes.Trim(c, " \t\n\r\x0b\x0c")) == 0 {
			continue
		}
		out = append(out, c)
	}
	return out
}

func goldenInputList(t *testing.T) []string {
	t.Helper()
	var out []string
	for _, dir := range []string{"../samples/corpus", "parse/testdata/inputs"} {
		ents, err := os.ReadDir(dir)
		if err != nil {
			t.Fatal(err)
		}
		var names []string
		for _, e := range ents {
			if e.IsDir() || (strings.Contains(dir, "corpus") && filepath.Ext(e.Name()) == ".md") {
				continue
			}
			names = append(names, e.Name())
		}
		sort.Strings(names)
		for _, n := range names {
			out = append(out, filepath.Join(dir, n))
		}
	}
	return out
}

func readGoldenLines(t *testing.T, name string) []string {
	t.Helper()
	f, err := os.Open(filepath.Join("parse/testdata/golden", name))
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	zr, err := gzip.NewReader(f)
	if err != nil {
		t.Fatal(err)
	}
	var out []string
	sc := bufio.NewScanner(zr)
	sc.Buffer(make([]byte, 1<<20), 64<<20)
	for sc.Scan() {
		out = append(out, sc.Text())
	}
	if err := sc.Err(); err != nil && !errors.Is(err, io.EOF) {
		t.Fatal(err)
	}
	return out
}

// TestParseStageProfiles: a user profile exported by the dashboard for the batch's shape, or a non-empty
// scenario.MAPPING, sends the batch to Python.
func TestParseStageProfiles(t *testing.T) {
	dir := t.TempDir()
	keys := []string{"ts", "level", "service", "host", "msg"}
	sig := parse.Signature("jsonl", keys)
	prof := fmt.Sprintf(`{%q: {"id": %q, "format": "jsonl", "keys": ["host","level","msg","service","ts"], "mapping": {"message": "service"}, "source": "user"}, "_scenario_mapping": {}}`, sig, sig)
	_ = os.WriteFile(filepath.Join(dir, "profiles.json"), []byte(prof), 0o640)
	writeRaw(t, dir, "0000000000000000001", []spRec{ingestRec("a", "app.jsonl", jsonl(3, "x")), ingestRec("b", "other.jsonl",
		`{"ts": "2026-10-15T05:58:00+00:00", "msg": "different shape"}`+"\n")})
	st := testStage(t, dir, parseOn)
	st.drainOnce(context.Background())
	out := readSpoolFile(t, filepath.Join(dir, "pseg-0000000000000000001.wo"))
	if fmt.Sprint(out[0].hdr["v"]) != "1" || fmt.Sprint(out[1].hdr["v"]) != "2" || st.stats.reasons["profile"].Load() != 1 {
		t.Fatalf("user profile: v=%v,%v reasons %d", out[0].hdr["v"], out[1].hdr["v"], st.stats.reasons["profile"].Load())
	}
	_ = os.WriteFile(filepath.Join(dir, "profiles.json"), []byte(`{"_scenario_mapping": {"message": "msg"}}`), 0o640)
	st = testStage(t, dir, parseOn)
	writeRaw(t, dir, "0000000000000000002", []spRec{ingestRec("b", "other.jsonl", `{"ts": "2026-10-15T05:58:00+00:00", "msg": "x"}`+"\n")})
	st.drainOnce(context.Background())
	out = readSpoolFile(t, filepath.Join(dir, "pseg-0000000000000000002.wo"))
	if fmt.Sprint(out[0].hdr["v"]) != "1" || st.stats.reasons["scenario"].Load() != 1 {
		t.Fatalf("scenario mapping: v=%v", out[0].hdr["v"])
	}
	if st.hints.Get(parse.StreamKey{Agent: "b", File: "other.jsonl"}) == nil {
		t.Error("a forwarded batch must still advance the hint (Python parses it and learns)")
	}
}

// TestParseStageOutputErrorHandsBack: when the parsed output cannot be written (here: pseg-<ns>.tmp cannot be
// created), the raw segment must not be deleted: it goes to the dashboard unparsed as seg-<ns>.wo, byte for byte.
// Before the fix the stage logged the error and removed the raw segment (acknowledged batches lost).
func TestParseStageOutputErrorHandsBack(t *testing.T) {
	dir := t.TempDir()
	raw := writeRaw(t, dir, "0000000000000000001", []spRec{ingestRec("a", "app.jsonl", jsonl(3, "x")), ingestRec("b", "x.log", "hello\n")})
	if err := os.Mkdir(filepath.Join(dir, "pseg-0000000000000000001.tmp"), 0o750); err != nil { // OpenFile fails: EISDIR
		t.Fatal(err)
	}
	writeRaw(t, dir, "0000000000000000002", []spRec{ingestRec("a", "app.jsonl", jsonl(2, "x"))})
	st := testStage(t, dir, parseOn)
	st.drainOnce(context.Background())
	got, err := os.ReadFile(filepath.Join(dir, "seg-0000000000000000001.wo"))
	if err != nil || !bytes.Equal(got, raw) {
		t.Fatalf("raw segment not handed back intact: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "pseg-0000000000000000001.wo")); err == nil {
		t.Error("a pseg was published for the failed segment")
	}
	if _, err := os.Stat(filepath.Join(dir, "pseg-0000000000000000002.wo")); err != nil {
		t.Errorf("the next segment must still be parsed: %v", err)
	}
	if st.stats.writeErrors.Load() != 1 {
		t.Errorf("write errors %d", st.stats.writeErrors.Load())
	}
	// recovery after a restart does not touch the handed-back segment
	recoverParseSpool(dir, parseOn)
	if _, err := os.Stat(filepath.Join(dir, "seg-0000000000000000001.wo")); err != nil {
		t.Errorf("handed-back segment lost by recovery: %v", err)
	}
}

// TestParseStageNeedsExport: the live stage parses only while the dashboard's profiles.json export is present;
// without it (dashboard on WATCHOVER_GO_PARSE=0, or not started yet) user profiles and scenario.MAPPING are unknown,
// so every batch is forwarded, while the hint still advances in lockstep. The export appearing turns parsing on.
func TestParseStageNeedsExport(t *testing.T) {
	dir := t.TempDir()
	writeRaw(t, dir, "0000000000000000001", []spRec{ingestRec("a", "app.jsonl", jsonl(3, "x"))})
	st := testStage(t, dir, parseOn)
	st.needExport = true
	st.profiles.snap.Interval = time.Nanosecond
	st.drainOnce(context.Background())
	out := readSpoolFile(t, filepath.Join(dir, "pseg-0000000000000000001.wo"))
	if fmt.Sprint(out[0].hdr["v"]) != "1" || st.stats.reasons["noexport"].Load() != 1 {
		t.Fatalf("without export: v=%v noexport=%d", out[0].hdr["v"], st.stats.reasons["noexport"].Load())
	}
	if h := st.hints.Get(parse.StreamKey{Agent: "a", File: "app.jsonl"}); h == nil || h.Format != "jsonl" {
		t.Errorf("forwarded batch must still teach the hint: %+v", h)
	}
	_ = os.WriteFile(filepath.Join(dir, "profiles.json"), []byte(`{"_scenario_mapping": {}}`), 0o640)
	writeRaw(t, dir, "0000000000000000002", []spRec{ingestRec("a", "app.jsonl", jsonl(3, "x"))})
	st.drainOnce(context.Background())
	out = readSpoolFile(t, filepath.Join(dir, "pseg-0000000000000000002.wo"))
	if fmt.Sprint(out[0].hdr["v"]) != "2" {
		t.Fatalf("with export: v=%v", out[0].hdr["v"])
	}
	// the receiver's own stage: export required, syslog year from the clock
	cfg := config{spool: dir, parse: parseOn}
	if live := newParseStage(cfg, nil); live.norm.NowYear == nil || live.norm.NowYear() != time.Now().Year() {
		t.Error("the receiver's Normalizer must take the syslog year from the clock")
	}
}

// TestParseStageUserGrokAndScenarioConstants: enabled user grok patterns in the dashboard's export ("_grok_user")
// send every batch to Python and hand unhinted streams to Python until both re-detect (Go's hint may not be
// Python's), also after the patterns are removed; parse-path scenario constants other than the compiled ones
// ("_scenario_parse") forward everything, the exact compiled values do not.
func TestParseStageUserGrokAndScenarioConstants(t *testing.T) {
	dir := t.TempDir()
	prof := filepath.Join(dir, "profiles.json")
	_ = os.WriteFile(prof, []byte(`{"_scenario_mapping": {}, "_grok_user": ["MY_APP"]}`), 0o640)
	st := testStage(t, dir, parseOn)
	st.profiles.snap.Interval = time.Nanosecond
	one := func(ns string) string {
		writeRaw(t, dir, ns, []spRec{ingestRec("a", "app.jsonl", jsonl(2, "x"))})
		st.drainOnce(context.Background())
		return fmt.Sprint(readSpoolFile(t, filepath.Join(dir, "pseg-"+ns+".wo"))[0].hdr["v"])
	}
	if v := one("0000000000000000001"); v != "1" || st.stats.reasons["usergrok"].Load() != 1 {
		t.Fatalf("user grok: v=%s", v)
	}
	_ = os.WriteFile(prof, []byte(`{"_scenario_mapping": {}, "_grok_user": []}`), 0o640)
	for i := 2; i < 200; i++ { // live.py: Python's hint from batch 1 is used until its 200th batch
		if v := one(fmt.Sprintf("%019d", i)); v != "1" {
			t.Fatalf("batch %d parsed by Go while Python still uses the hint it learned under user patterns", i)
		}
	}
	if v := one(fmt.Sprintf("%019d", 200)); v != "2" {
		t.Fatalf("batch 200 (both re-detect): v=%s", v)
	}
	if st.stats.reasons["sticky"].Load() != 198 {
		t.Errorf("sticky forwards %d", st.stats.reasons["sticky"].Load())
	}
	// scenario constants
	_ = os.WriteFile(prof, []byte(`{"_scenario_parse": {"DEDUP_KEY": "event_id", "DEDUP_PREFER": "json", "SEVERITY_MAP": {}, "SIDE_TABLES": []}}`), 0o640)
	if v := one(fmt.Sprintf("%019d", 201)); v != "1" || st.stats.reasons["scenario"].Load() != 1 {
		t.Fatalf("edited scenario constants: v=%s", v)
	}
	same := `{"_scenario_parse": {"DEDUP_KEY": "alarm_id", "DEDUP_PREFER": "json", "SEVERITY_MAP": {"5": "CRITICAL", "4": "ERROR", "3": "WARN", "2": "INFO", "1": "DEBUG"}, "SIDE_TABLES": ["depend", "bagimlilik", "inventory", "envanter", "sozlugu", "brifing", "readme"]}}`
	_ = os.WriteFile(prof, []byte(same), 0o640)
	if v := one(fmt.Sprintf("%019d", 202)); v != "2" {
		t.Fatalf("the compiled scenario constants: v=%s", v)
	}
}
