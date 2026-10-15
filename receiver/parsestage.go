// Parse stage: the Go side of WATCHOVER_GO_PARSE (docs/go-parse/PLAN.md section 4, 06-integration.md sections 3-13).
//
// The HTTP path is untouched: agents are answered as soon as their batch is in a raw segment. With the stage on, the
// spool writer closes raw segments as seg-<ns>.raw (the dashboard never reads those); the stage reads them oldest first,
// parses each /ingest record with receiver/parse (byte-for-byte port of pipeline.ingest_bytes) when the batch's
// format[:family] is allow-listed, and writes pseg-<ns>.wo with one output record per input record, in input order:
//
//	v=2 "parsed" record: header = the raw header's keys + v=2, kind, len, n, raw_len, format, family, confidence,
//	                     hint_format, report, parser_version; body = one observation per line in the golden format,
//	                     except that attributes keep their insertion order (parse.SpoolLine)
//	v=1 record:          the raw header and body bytes unchanged (not allow-listed, error, /rum, /mobile, ...)
//
// The dashboard's SpoolConsumer reads seg-*.wo and pseg-*.wo by time order; v=2 goes to LiveStore.ingest_parsed, v=1 to
// LiveStore.ingest exactly as today. pseg-<ns> carries the raw segment's <ns>, so arrival order is kept across the switch.
//
// Delivery: pseg-<ns>.tmp is written completely and fsynced, then the raw segment is removed, then the pseg is renamed
// to .wo. After a crash: pseg tmp + raw present -> the tmp is incomplete (removed, the segment is redone); pseg tmp
// without raw -> complete (renamed); pseg .wo + raw -> raw removed. No record is lost or emitted twice. If the output
// cannot be written (disk full, I/O error), the partial output is dropped and the raw segment is handed to the
// dashboard unparsed (renamed seg-<ns>.wo, same <ns>, so its place in the order is kept): it is never deleted unread.
//
// Shadow mode (WATCHOVER_GO_PARSE=shadow): the dashboard keeps parsing raw seg-*.wo; the writer hard-links every closed
// segment to seg-<ns>.shd, and the stage writes shadow-<ns>.wo (each /ingest raw record, followed by its v=2 record when
// Go parsed it) for scripts/parse_shadow_diff.py. Shadow output is capped at WO_SHADOW_MAX_MB (oldest dropped).
//
// Per-stream order and hint lockstep: records are sharded by fnv32a(agent + "\x00" + file), so one worker sees a stream
// in arrival order and keeps the per-stream hint (live.py _detect) exactly as Python would, including for batches it
// forwards raw. An emitter writes results strictly in input order (reorder buffer keyed by sequence number).
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"hash/fnv"
	"log"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
	_ "time/tzdata" // TZ=Europe/Istanbul in the alpine image has no zoneinfo: the syslog year (NowYear) follows local time like Python's

	"watchover/receiver/parse"
	"watchover/receiver/parse/py"
)

type parseMode int

const (
	parseOff parseMode = iota
	parseShadow
	parseOn
)

func (m parseMode) String() string {
	switch m {
	case parseOn:
		return "on"
	case parseShadow:
		return "shadow"
	}
	return "off"
}

// parseModeOf reads WATCHOVER_GO_PARSE: unset or "1" = on (the default whenever the Go receiver runs), "shadow",
// "0"/"off"/"false"/"no" = off (exactly the behaviour before the parse stage existed).
func parseModeOf(v string) parseMode {
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "0", "off", "false", "no":
		return parseOff
	case "shadow":
		return parseShadow
	}
	return parseOn
}

// csvList splits a comma separated env value into trimmed non-empty items.
func csvList(v string) []string {
	var out []string
	for _, s := range strings.Split(v, ",") {
		if s = strings.TrimSpace(s); s != "" {
			out = append(out, s)
		}
	}
	return out
}

// ---------------------------------------------------------------- spool file names

const (
	rawExt    = ".raw" // closed raw segment waiting for the stage (mode on)
	shadowExt = ".shd" // hard link of a closed raw segment for the shadow stage
)

// segNS returns the <ns> part of seg-<ns>.<ext> / pseg-<ns>.<ext> / shadow-<ns>.<ext>.
func segNS(path string) string {
	b := filepath.Base(path)
	if i := strings.IndexByte(b, '-'); i >= 0 {
		b = b[i+1:]
	}
	if i := strings.IndexByte(b, '.'); i >= 0 {
		b = b[:i]
	}
	return b
}

func exists(p string) bool { _, err := os.Stat(p); return err == nil }

// recoverParseSpool settles what a previous run (in any mode) left behind, before the writer starts. Called by newSpool.
func recoverParseSpool(dir string, mode parseMode) {
	glob := func(p string) []string { m, _ := filepath.Glob(filepath.Join(dir, p)); sort.Strings(m); return m }
	seg := func(ns, ext string) string { return filepath.Join(dir, "seg-"+ns+ext) }
	pseg := func(ns, ext string) string { return filepath.Join(dir, "pseg-"+ns+ext) }
	// A complete pseg tmp has no raw segment any more (the raw is removed only after the tmp is fsynced): publish it.
	for _, t := range glob("pseg-*.tmp") {
		ns := segNS(t)
		if exists(seg(ns, rawExt)) {
			_ = os.Remove(t) // incomplete: the raw segment is parsed again (or handed to Python below)
		} else {
			_ = os.Rename(t, pseg(ns, ".wo"))
		}
	}
	for _, r := range glob("seg-*" + rawExt) {
		ns := segNS(r)
		if exists(pseg(ns, ".wo")) {
			_ = os.Remove(r) // already emitted, the crash came before the raw segment was removed
			continue
		}
		if mode != parseOn {
			_ = os.Rename(r, seg(ns, ".wo")) // the stage was switched off: Python parses what is left, as before
		}
	}
	for _, t := range glob("shadow-*.tmp") {
		_ = os.Remove(t)
	}
	if mode != parseShadow {
		for _, s := range glob("seg-*" + shadowExt) {
			_ = os.Remove(s)
		}
	}
}

// ---------------------------------------------------------------- profiles: Python's snapshot + what Go learned

// profileStore is the parse-time profile view of the stage. User profiles (made in the dashboard) come from Python's
// export (<spool>/profiles.json) and always win; auto profiles that Go itself learned while parsing (profiles.remember
// semantics, MemStore) come next, then the exported auto profiles (seed after a restart). Go never writes the file:
// Python remembers the shapes of v=2 batches from their report.
type profileStore struct {
	snap *parse.FileSnapshot
	mem  atomic.Pointer[parse.MemStore]
	mu   sync.Mutex
	sigs map[string]bool // shapes in the overlay (bounded: Python's profiles.json grows the same way, without a bound)
}

func newProfileStore(path string) *profileStore {
	s := &profileStore{snap: parse.NewFileSnapshot(path), sigs: map[string]bool{}}
	s.mem.Store(parse.NewMemStore())
	return s
}

func (s *profileStore) Lookup(format string, keys []string) *parse.Profile {
	sp := s.snap.Lookup(format, keys)
	if sp != nil && sp.Source == "user" {
		return sp
	}
	if mp := s.mem.Load().Lookup(format, keys); mp != nil {
		return mp
	}
	return sp
}

func (s *profileStore) Remember(format string, keys []string, roles parse.Roles, name, source string) {
	if sp := s.snap.Lookup(format, keys); sp != nil && sp.Source == "user" && source != "user" {
		return // profiles.remember only counts a hit on a user profile
	}
	if len(keys) == 0 {
		return
	}
	sig := parse.Signature(format, keys)
	s.mu.Lock()
	if !s.sigs[sig] {
		if len(s.sigs) >= 50_000 { // bounded memory: start over (Python's exported file still seeds the lookups)
			s.mem.Store(parse.NewMemStore())
			s.sigs = map[string]bool{}
		}
		s.sigs[sig] = true
	}
	s.mu.Unlock()
	s.mem.Load().Remember(format, keys, roles, name, source)
}

func (s *profileStore) HasUser(format string, keys []string) bool {
	return s.snap.HasUser(format, keys)
}

// scenario: a non-empty scenario.MAPPING, or parse-path scenario constants that differ from the compiled ones.
func (s *profileStore) scenario() bool {
	m := s.snap.ScenarioMapping()
	return (m != nil && m.Len() > 0) || s.snap.ScenarioDiffers()
}

// ---------------------------------------------------------------- statistics

var forwardReasons = []string{"format", "profile", "panic", "timeout", "nan", "decode", "pyraise", "table", "scenario", "sticky", "path", "error", "noexport", "usergrok"}

type fmtCount struct{ parsed, forwarded, obs int64 }

type parseStats struct {
	parsed, forwarded, observations, linesIn, bytesIn, nsTotal, lagMs atomic.Int64
	segments, torn, shadowDropped, records, writeErrors               atomic.Int64
	reasons                                                           map[string]*atomic.Int64
	mu                                                                sync.Mutex
	byFormat                                                          map[string]*fmtCount
}

func newParseStats() *parseStats {
	s := &parseStats{reasons: map[string]*atomic.Int64{}, byFormat: map[string]*fmtCount{}}
	for _, r := range forwardReasons {
		s.reasons[r] = new(atomic.Int64)
	}
	return s
}

func (s *parseStats) count(format string, parsed bool, obs int) {
	s.mu.Lock()
	c := s.byFormat[format]
	if c == nil {
		c = &fmtCount{}
		s.byFormat[format] = c
	}
	if parsed {
		c.parsed++
		c.obs += int64(obs)
	} else {
		c.forwarded++
	}
	s.mu.Unlock()
}

// ---------------------------------------------------------------- the stage

type parseStage struct {
	cfg      config
	mode     parseMode
	sp       *spool // backlog figures for /health (nil in tests)
	hints    *parse.HintCache
	norm     *parse.Normalizer
	profiles *profileStore
	workers  int
	timeout  time.Duration
	hardCap  time.Duration // a parse still running after this is abandoned (stream goes sticky)
	maxBytes int64         // in-flight cap across the pipeline
	only     map[string]bool
	deny     map[string]bool
	version  string
	notify   chan struct{}
	stats    *parseStats

	stickyMu sync.Mutex
	sticky   map[parse.StreamKey]int // stream -> live.py hint counter while Go cannot know Python's hint

	// needExport: parse only while the dashboard's profiles.json export is present in the spool (the live receiver;
	// false for `parse-spool` and tests). Without it Go cannot see user profiles or scenario.MAPPING (e.g. the
	// dashboard runs with WATCHOVER_GO_PARSE=0 and removes the file), so every batch is forwarded ("noexport").
	needExport bool

	// test hooks
	parseFn func(name string, data []byte, o parse.Options) ([]parse.Observation, []parse.Report, error)
}

func newParseStage(cfg config, sp *spool) *parseStage {
	w := cfg.parseWorkers
	if w <= 0 {
		w = runtime.GOMAXPROCS(0)
	}
	to := cfg.parseTimeout
	if to <= 0 {
		to = 2 * time.Second
	}
	prof := cfg.profiles
	if prof == "" {
		prof = filepath.Join(cfg.spool, "profiles.json")
	}
	norm := parse.NewNormalizer(time.Now().Year())         // normalize._DEFAULT: the year at start (dateutil default)
	norm.NowYear = func() int { return time.Now().Year() } // "%b %d %H:%M:%S" takes datetime.now().year per call
	norm.Strict = true
	p := &parseStage{cfg: cfg, mode: cfg.parse, sp: sp, hints: parse.NewHintCache(), norm: norm, profiles: newProfileStore(prof),
		workers: w, timeout: to, hardCap: 60 * time.Second, maxBytes: 256 << 20, version: parse.Version(),
		notify: make(chan struct{}, 1), stats: newParseStats(), sticky: map[parse.StreamKey]int{}, parseFn: parse.IngestBytes}
	if len(cfg.parseOnly) > 0 {
		p.only = map[string]bool{}
		for _, f := range cfg.parseOnly {
			p.only[f] = true
		}
	}
	p.deny = map[string]bool{}
	for _, f := range cfg.parseDeny {
		p.deny[f] = true
	}
	return p
}

// allowed: compiled allow-list (testdata/allow.txt), narrowed by WATCHOVER_GO_PARSE_FORMATS / _DENY. An entry names a
// format[:family] key ("text:java", "text:" for text without family) or a bare format ("text" = every allowed family).
func (p *parseStage) allowed(format, family string) bool {
	if !parse.Allowed(format, family) {
		return false
	}
	key := parse.FormatKey(format, family)
	if p.deny[key] || p.deny[format] {
		return false
	}
	return p.only == nil || p.only[key] || p.only[format]
}

// wake tells the stage a raw segment was closed (non-blocking; the stage also polls).
func (p *parseStage) wake() {
	select {
	case p.notify <- struct{}{}:
	default:
	}
}

// rawHeader is what the stage needs from a v=1 header; the full header is kept as bytes.
type rawHeader struct {
	Path  string `json:"path"`
	File  string `json:"file"`
	Agent string `json:"agent"`
	TS    string `json:"ts"`
	Len   *int   `json:"len"`
}

type segJob struct {
	name, ns string
	out      *os.File
	outName  string
	err      error // first output error: the segment then goes to the dashboard unparsed (finalize)
	records  int
	lastTS   string
	wg       *sync.WaitGroup
}

type job struct {
	seq    int64
	seg    *segJob
	marker bool // end of segment
	hdr    []byte
	body   []byte
	h      rawHeader
	size   int64
}

type result struct {
	seq    int64
	seg    *segJob
	marker bool
	out    []byte // framed record(s) to append
	size   int64
	ts     string
}

// byteSem bounds the bytes held between the reader and the emitter.
type byteSem struct {
	mu   sync.Mutex
	cond *sync.Cond
	used int64
	max  int64
}

func newByteSem(max int64) *byteSem { s := &byteSem{max: max}; s.cond = sync.NewCond(&s.mu); return s }

func (s *byteSem) acquire(n int64) {
	s.mu.Lock()
	for s.used > 0 && s.used+n > s.max {
		s.cond.Wait()
	}
	s.used += n
	s.mu.Unlock()
}

func (s *byteSem) release(n int64) {
	s.mu.Lock()
	s.used -= n
	s.mu.Unlock()
	s.cond.Broadcast()
}

// frame is the spool record framing: header line, body, newline.
func frame(dst, hdr, body []byte) []byte {
	dst = append(dst, hdr...)
	dst = append(dst, '\n')
	dst = append(dst, body...)
	return append(dst, '\n')
}

// run processes closed raw segments until ctx is cancelled.
func (p *parseStage) run(ctx context.Context) {
	p.loop(ctx, false)
}

// drainOnce processes the raw segments present now (and any closed meanwhile) and returns: `wo-receiver parse-spool`.
func (p *parseStage) drainOnce(ctx context.Context) int {
	return p.loop(ctx, true)
}

func (p *parseStage) inputs() []string {
	ext := rawExt
	if p.mode == parseShadow {
		ext = shadowExt
	}
	m, _ := filepath.Glob(filepath.Join(p.cfg.spool, "seg-*"+ext))
	sort.Strings(m)
	return m
}

func (p *parseStage) loop(ctx context.Context, once bool) int {
	shards := make([]chan job, p.workers)
	results := make(chan result, 1024)
	sem := newByteSem(p.maxBytes)
	var wwg sync.WaitGroup
	for i := range shards {
		shards[i] = make(chan job, 256)
		wwg.Add(1)
		go func(in chan job) {
			defer wwg.Done()
			for j := range in {
				p.work(j, results)
			}
		}(shards[i])
	}
	edone := make(chan struct{})
	go func() { p.emit(results, sem); close(edone) }()
	defer func() {
		for _, c := range shards {
			close(c)
		}
		wwg.Wait()
		close(results)
		<-edone
	}()
	var seq int64
	total := 0
	poll := time.NewTicker(250 * time.Millisecond)
	defer poll.Stop()
	for {
		segs := p.inputs()
		if len(segs) == 0 {
			if once {
				return total
			}
			select {
			case <-ctx.Done():
				return total
			case <-p.notify:
			case <-poll.C:
			}
			continue
		}
		var wg sync.WaitGroup
		for _, name := range segs {
			if ctx.Err() != nil {
				break
			}
			total += p.dispatch(name, &seq, shards, results, sem, &wg)
		}
		wg.Wait() // every listed segment is finalized before the directory is listed again
		if ctx.Err() != nil {
			return total
		}
	}
}

// dispatch reads one raw segment and hands its records to the shards (ingest) or straight to the emitter.
func (p *parseStage) dispatch(name string, seq *int64, shards []chan job, results chan result, sem *byteSem, wg *sync.WaitGroup) int {
	data, err := os.ReadFile(name)
	if err != nil {
		return 0
	}
	sj := &segJob{name: name, ns: segNS(name), wg: wg}
	wg.Add(1)
	i, n := 0, 0
	for i < len(data) {
		nl := bytes.IndexByte(data[i:], '\n')
		if nl < 0 {
			p.stats.torn.Add(1)
			break
		}
		hdr := data[i : i+nl]
		var h rawHeader
		if json.Unmarshal(hdr, &h) != nil || h.Len == nil || *h.Len < 0 {
			p.stats.torn.Add(1)
			break
		}
		start := i + nl + 1
		end := start + *h.Len
		if end > len(data) {
			p.stats.torn.Add(1)
			break
		}
		body := data[start:end]
		i = end + 1
		n++
		j := job{seq: *seq, seg: sj, hdr: hdr, body: body, h: h, size: int64(len(hdr) + len(body))}
		*seq++
		sem.acquire(j.size)
		if h.Path == "" || h.Path == "/ingest" {
			shards[p.shard(h)] <- j
		} else { // /rum, /mobile, ...: always the dashboard's (shadow output only carries /ingest)
			p.stats.reasons["path"].Add(1)
			var out []byte
			if p.mode != parseShadow {
				out = p.rawOut(j)
			}
			results <- result{seq: j.seq, seg: sj, out: out, size: j.size, ts: h.TS}
		}
	}
	sj.records = n
	results <- result{seq: *seq, seg: sj, marker: true}
	*seq++
	return n
}

func (p *parseStage) shard(h rawHeader) int {
	f := fnv.New32a()
	_, _ = f.Write([]byte(agentOf(h)))
	_, _ = f.Write([]byte{0})
	_, _ = f.Write([]byte(fileOf(h)))
	return int(f.Sum32() % uint32(p.workers))
}

func agentOf(h rawHeader) string {
	if h.Agent == "" {
		return "unknown"
	}
	return h.Agent
}

func fileOf(h rawHeader) string {
	if h.File == "" {
		return "agent.log"
	}
	return h.File
}

// rawOut is the record as the dashboard would have read it from the raw segment (shadow: nothing for non-ingest).
func (p *parseStage) rawOut(j job) []byte {
	return frame(make([]byte, 0, len(j.hdr)+len(j.body)+2), j.hdr, j.body)
}

// outcome of one /ingest batch.
type outcome struct {
	parsed bool
	out    []byte // v=2 framed record (parsed) or nil
	reason string // forward reason when !parsed
	format string // format[:family] or "-"
	obs    int
}

// work runs one /ingest batch on its shard: hint step, parse (bounded by the batch timeout), hint learn, decision.
func (p *parseStage) work(j job, results chan<- result) {
	t0 := time.Now()
	k := parse.StreamKey{Agent: agentOf(j.h), File: fileOf(j.h)}
	send := func(o outcome) {
		res := result{seq: j.seq, seg: j.seg, size: j.size, ts: j.h.TS}
		switch {
		case p.mode == parseShadow:
			res.out = p.rawOut(j)
			if o.parsed {
				res.out = append(res.out, o.out...)
			}
		case o.parsed:
			res.out = o.out
		default:
			res.out = p.rawOut(j)
		}
		results <- res
	}
	o := p.decide(j, k, t0, send)
	if o.parsed {
		p.stats.parsed.Add(1)
		p.stats.observations.Add(int64(o.obs))
	} else {
		p.stats.forwarded.Add(1)
		if c := p.stats.reasons[o.reason]; c != nil {
			c.Add(1)
		}
	}
	p.stats.count(o.format, o.parsed, o.obs)
	p.stats.bytesIn.Add(int64(len(j.body)))
	lines := bytes.Count(j.body, []byte("\n"))
	if len(j.body) > 0 && j.body[len(j.body)-1] != '\n' {
		lines++
	}
	p.stats.linesIn.Add(int64(lines))
	p.stats.nsTotal.Add(int64(time.Since(t0)))
}

type parsed struct {
	obs  []parse.Observation
	reps []parse.Report
	err  error
}

// decide runs the per-batch decision of PLAN section 4.2 and calls send exactly once.
func (p *parseStage) decide(j job, k parse.StreamKey, t0 time.Time, send func(outcome)) (o outcome) {
	o.format = "-"
	sent := false
	finish := func(x outcome) outcome {
		if !sent {
			sent = true
			send(x)
		}
		return x
	}
	// A stream whose hint Go cannot know (an earlier unhinted batch failed in Go but not in Python) stays with Python
	// until both re-detect at the same batch (live.py: every 200th).
	p.stickyMu.Lock()
	n, isSticky := p.sticky[k]
	if isSticky {
		n++
		if n%200 != 0 {
			p.sticky[k] = n
			p.stickyMu.Unlock()
			o.reason = "sticky"
			return finish(o)
		}
		delete(p.sticky, k) // re-detect batch: Python ignores its hint now, so Go's fresh detection is in lockstep again
	}
	p.stickyMu.Unlock()
	var hint *parse.Hint
	if !isSticky {
		hint = p.hints.Next(k)
	}
	scenario := p.profiles.scenario()
	userGrok := p.profiles.snap.UserGrok()
	opts := parse.Options{Hint: hint, Profiles: p.profiles, Norm: p.norm}
	if scenario {
		opts.Profiles = nil
	}
	ch := make(chan parsed, 1)
	go func() {
		var r parsed
		defer func() {
			if x := recover(); x != nil {
				r = parsed{err: fmt.Errorf("%w: panic: %v", errPanic, x)}
			}
			ch <- r
		}()
		r.obs, r.reps, r.err = p.parseFn(fileOf(j.h), j.body, opts)
	}()
	timer := time.NewTimer(p.timeout)
	var r parsed
	select {
	case r = <-ch:
		timer.Stop()
	case <-timer.C:
		finish(outcome{reason: "timeout", format: "-"}) // the batch goes to Python now; the hint still needs Go's report
		hard := time.NewTimer(p.hardCap)
		select {
		case r = <-ch:
			hard.Stop()
		case <-hard.C:
			r = parsed{err: fmt.Errorf("%w: abandoned", errPanic)}
		}
	}
	if len(r.reps) > 0 {
		o.format = parse.FormatKey(r.reps[0].Format, r.reps[0].Family)
	}
	if r.err != nil {
		switch {
		case errors.Is(r.err, parse.ErrPyRaise) && !userGrok:
			o.reason = "pyraise" // Python raises too: its hint is not updated either (user grok patterns: unknown)
		default:
			if hint == nil && !parse.IsSideTable(fileOf(j.h)) {
				// Python parses this unhinted batch and learns a hint Go does not know: hand the stream to Python.
				p.hints.Invalidate(k)
				p.stickyMu.Lock()
				p.sticky[k] = 1
				p.stickyMu.Unlock()
			}
			switch {
			case errors.Is(r.err, errPanic):
				o.reason = "panic"
			case parse.IsSideTable(fileOf(j.h)):
				o.reason = "table"
			case strings.Contains(r.err.Error(), "decode") || strings.Contains(r.err.Error(), "UTF") || strings.Contains(r.err.Error(), "archive"):
				o.reason = "decode"
			default:
				o.reason = "error"
			}
		}
		if sent {
			o.reason = "timeout"
		}
		return finish(o)
	}
	p.hints.Learn(k, hint, r.reps, j.body)
	if userGrok && hint == nil {
		// User grok patterns change Python's detection, so the hint Go just learned may not be Python's: like a
		// failed unhinted batch, the stream stays with Python until both re-detect (sticky), also after the
		// patterns are removed. A hint learned before they appeared is the same on both sides.
		p.hints.Invalidate(k)
		p.stickyMu.Lock()
		p.sticky[k] = 1
		p.stickyMu.Unlock()
	}
	if sent {
		o.reason = "timeout"
		return o
	}
	switch {
	case userGrok:
		o.reason = "usergrok"
		return finish(o)
	case scenario:
		o.reason = "scenario"
		return finish(o)
	case p.needExport && !p.profiles.snap.Present():
		o.reason = "noexport"
		return finish(o)
	case len(r.reps) == 0 || !p.allowed(r.reps[0].Format, r.reps[0].Family):
		o.reason = "format"
		return finish(o)
	case learnFormat(r.reps[0].Format) && p.profiles.HasUser(r.reps[0].Format, r.reps[0].Keys):
		o.reason = "profile"
		return finish(o)
	}
	out, err := p.encode(j, r.obs, r.reps[0])
	if err != nil {
		o.reason = "nan"
		return finish(o)
	}
	o.parsed, o.out, o.obs = true, out, len(r.obs)
	return finish(o)
}

var errPanic = errors.New("parse panic")

func learnFormat(f string) bool {
	switch f {
	case "json", "jsonl", "csv", "kv", "grok":
		return true
	}
	return false
}

// encode builds the framed v=2 record.
func (p *parseStage) encode(j job, obs []parse.Observation, rep parse.Report) ([]byte, error) {
	body := make([]byte, 0, len(j.body)*3+256)
	for _, o := range obs {
		ln, err := parse.SpoolLine(o)
		if err != nil {
			return nil, err
		}
		body = append(append(body, ln...), '\n')
	}
	var hm map[string]json.RawMessage
	if err := json.Unmarshal(j.hdr, &hm); err != nil {
		return nil, err
	}
	str := func(s string) json.RawMessage { b, _ := json.Marshal(s); return b }
	num := func(n int) json.RawMessage { return json.RawMessage(strconv.Itoa(n)) }
	hintFmt := parse.HintFormat(rep, j.body) // the json -> jsonl single-object rule needs the raw body: Python gets it here
	hm["v"] = num(2)
	hm["kind"] = str("parsed")
	hm["len"] = num(len(body))
	hm["n"] = num(len(obs))
	hm["raw_len"] = num(len(j.body))
	hm["format"] = str(rep.Format)
	hm["family"] = str(rep.Family)
	hm["hint_format"] = str(hintFmt)
	hm["confidence"] = json.RawMessage(py.FloatRepr(rep.Confidence)) // a Python float on the other side (1.0, not 1)
	hm["report"] = parse.ReportJSON(rep)
	hm["parser_version"] = str(p.version)
	if p.mode == parseShadow {
		hm["shadow"] = json.RawMessage("true")
	}
	hb, err := json.Marshal(hm)
	if err != nil {
		return nil, err
	}
	return frame(make([]byte, 0, len(hb)+len(body)+2), hb, body), nil
}

// emit writes results in sequence order into pseg-<ns>.tmp (shadow-<ns>.tmp) and finalizes each segment.
func (p *parseStage) emit(results <-chan result, sem *byteSem) {
	pending := map[int64]result{}
	var next int64
	prefix := "pseg-"
	if p.mode == parseShadow {
		prefix = "shadow-"
	}
	for r := range results {
		pending[r.seq] = r
		for {
			x, ok := pending[next]
			if !ok {
				break
			}
			delete(pending, next)
			next++
			sj := x.seg
			if x.marker {
				p.finalize(sj, prefix)
				continue
			}
			if x.ts != "" {
				sj.lastTS = x.ts
			}
			if len(x.out) > 0 && sj.err == nil {
				if sj.out == nil {
					sj.outName = filepath.Join(p.cfg.spool, prefix+sj.ns+".tmp")
					f, err := os.OpenFile(sj.outName, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o640)
					if err != nil {
						sj.err = err
					} else {
						sj.out = f
					}
				}
				if sj.out != nil {
					if _, err := sj.out.Write(x.out); err != nil {
						sj.err = err
					}
				}
			}
			p.stats.records.Add(1)
			sem.release(x.size)
		}
	}
}

// finalize: fsync the output, remove the input, publish the output (see the delivery note at the top).
func (p *parseStage) finalize(sj *segJob, prefix string) {
	defer sj.wg.Done()
	if sj.out != nil {
		if p.cfg.fsync {
			if err := sj.out.Sync(); err != nil && sj.err == nil {
				sj.err = err
			}
		}
		if err := sj.out.Close(); err != nil && sj.err == nil {
			sj.err = err
		}
	}
	if sj.err != nil {
		p.handBack(sj)
	} else {
		_ = os.Remove(sj.name)
		if sj.out != nil {
			_ = os.Rename(sj.outName, filepath.Join(p.cfg.spool, prefix+sj.ns+".wo"))
		}
	}
	p.stats.segments.Add(1)
	if t, err := time.Parse(time.RFC3339Nano, sj.lastTS); err == nil {
		p.stats.lagMs.Store(max(0, time.Since(t).Milliseconds()))
	}
	if p.mode == parseShadow {
		p.capShadow()
	}
}

// handBack is finalize after an output error: the partial output is removed and the raw segment goes to the dashboard
// as it is (seg-<ns>.wo: same <ns>, so the order is kept; Python parses it and advances its hints exactly as Go did).
// In shadow mode the dashboard already has its own copy, so only the shadow input is dropped. The log line carries
// file names and the OS error only.
func (p *parseStage) handBack(sj *segJob) {
	p.stats.writeErrors.Add(1)
	if sj.out != nil {
		_ = os.Remove(sj.outName)
	}
	if p.mode == parseShadow {
		_ = os.Remove(sj.name)
		log.Printf("parse stage: shadow output for %s not written: %v", filepath.Base(sj.name), sj.err)
		return
	}
	if err := os.Rename(sj.name, filepath.Join(p.cfg.spool, "seg-"+sj.ns+".wo")); err != nil {
		log.Printf("parse stage: %s: output failed (%v) and hand-back failed (%v); retried on the next pass", filepath.Base(sj.name), sj.err, err)
		return
	}
	log.Printf("parse stage: %s handed to the dashboard unparsed: %v", filepath.Base(sj.name), sj.err)
}

// capShadow keeps shadow-*.wo under WO_SHADOW_MAX_MB, dropping the oldest.
func (p *parseStage) capShadow() {
	m, _ := filepath.Glob(filepath.Join(p.cfg.spool, "shadow-*.wo"))
	sort.Strings(m)
	var total int64
	sizes := make([]int64, len(m))
	for i, f := range m {
		if fi, err := os.Stat(f); err == nil {
			sizes[i] = fi.Size()
			total += sizes[i]
		}
	}
	for i := 0; i < len(m) && total > p.cfg.shadowMax && p.cfg.shadowMax > 0; i++ {
		if os.Remove(m[i]) == nil {
			total -= sizes[i]
			p.stats.shadowDropped.Add(1)
		}
	}
}

// health adds the stage's fields to the receiver's /health (ints also become /metrics gauges).
func (p *parseStage) health(h map[string]any) {
	h["parse_mode"] = p.mode.String()
	h["parse_enabled"] = 1
	h["parse_workers"] = p.workers
	h["parse_batches_parsed"] = p.stats.parsed.Load()
	h["parse_batches_forwarded"] = p.stats.forwarded.Load()
	for _, r := range forwardReasons {
		h["parse_forward_reason_"+r] = p.stats.reasons[r].Load()
	}
	h["parse_observations"] = p.stats.observations.Load()
	h["parse_lines_in"] = p.stats.linesIn.Load()
	h["parse_bytes_in"] = p.stats.bytesIn.Load()
	h["parse_ns_total"] = p.stats.nsTotal.Load()
	h["parse_lag_ms"] = p.stats.lagMs.Load()
	h["parse_segments"] = p.stats.segments.Load()
	h["parse_records"] = p.stats.records.Load()
	h["parse_torn"] = p.stats.torn.Load()
	h["parse_write_errors"] = p.stats.writeErrors.Load()
	h["parse_hint_streams"] = p.hints.Len()
	p.stickyMu.Lock()
	h["parse_sticky_streams"] = len(p.sticky)
	p.stickyMu.Unlock()
	if p.mode == parseShadow {
		h["parse_shadow_dropped"] = p.stats.shadowDropped.Load()
	}
	if p.sp != nil {
		h["parse_backlog_bytes"] = p.sp.rawBacklog.Load()
		h["parse_backlog_segments"] = p.sp.rawSegments.Load()
		h["parse_pseg_backlog_bytes"] = p.sp.psegBacklog.Load()
	}
	by := map[string]map[string]int64{}
	p.stats.mu.Lock()
	for f, c := range p.stats.byFormat {
		by[f] = map[string]int64{"parsed": c.parsed, "forwarded": c.forwarded, "obs": c.obs}
	}
	p.stats.mu.Unlock()
	h["parse_by_format"] = by
	h["parser_version"] = p.version
}
