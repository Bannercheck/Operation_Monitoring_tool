package ship

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"
)

type recorder struct {
	mu     sync.Mutex
	got    map[string][]string // X-File-Name -> lines in arrival order
	bodies int
	result Result
}

func (r *recorder) send(_ context.Context, body []byte, name string) Result {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.result == Retry {
		return Retry
	}
	if r.got == nil {
		r.got = map[string][]string{}
	}
	if len(body) == 0 || body[len(body)-1] != '\n' {
		panic("batch without final newline")
	}
	r.got[name] = append(r.got[name], strings.Split(strings.TrimSuffix(string(body), "\n"), "\n")...)
	r.bodies++
	return Delivered
}

func (r *recorder) lines(name string) []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.got[name]...)
}

func newTailer(state string, rec *recorder, patterns ...string) *Tailer {
	t := &Tailer{Patterns: patterns, State: state, Send: rec.send, Agent: "t", RotateCheck: time.Nanosecond, RotatedGrace: time.Nanosecond,
		FlushEvery: time.Nanosecond, ScanEvery: time.Hour}
	t.defaults()
	return t
}

// step is one turn of Run's loop: read, rescan when asked, flush.
func step(t *Tailer) {
	now := time.Now()
	t.poll(now, true)
	if t.wantScan {
		t.scan(now, false)
		t.poll(now, true)
	}
	t.flush(context.Background())
}

func start(t *Tailer) {
	now := time.Now()
	t.scan(now, true)
	t.recoverRotated(now)
	t.flush(context.Background())
	t.saveLog()
}

func stop(t *Tailer) {
	t.poll(time.Now(), false)
	t.flush(context.Background())
	t.saveLog()
	t.closeAll()
}

func appendFile(tb testing.TB, p, s string) {
	f, err := os.OpenFile(p, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		tb.Fatal(err)
	}
	if _, err := f.WriteString(s); err != nil {
		tb.Fatal(err)
	}
	_ = f.Close()
}

func eq(tb testing.TB, got []string, want ...string) {
	tb.Helper()
	if strings.Join(got, "|") != strings.Join(want, "|") {
		tb.Fatalf("got %q\nwant %q", got, want)
	}
}

func TestTailStartsAtEndAndRestartsWithoutLossOrDuplicates(t *testing.T) {
	d := t.TempDir()
	log, state := filepath.Join(d, "app.log"), filepath.Join(d, "st", "positions.json")
	appendFile(t, log, "old 1\nold 2\n")
	rec := &recorder{}
	tl := newTailer(state, rec, filepath.Join(d, "*.log"))
	start(tl)
	appendFile(t, log, "a\nb\npart")
	step(tl)
	eq(t, rec.lines("app.log"), "a", "b")
	stop(tl) // the fragment is not shipped: it is not a line yet
	appendFile(t, log, "ial\nwhile stopped\n")
	tl2 := newTailer(state, rec, filepath.Join(d, "*.log"))
	start(tl2)
	step(tl2)
	eq(t, rec.lines("app.log"), "a", "b", "partial", "while stopped")
	// a file created while the agent was stopped, under a pattern it already followed, is read from its start
	stop(tl2)
	appendFile(t, filepath.Join(d, "new.log"), "n1\nn2\n")
	tl3 := newTailer(state, rec, filepath.Join(d, "*.log"))
	start(tl3)
	step(tl3)
	eq(t, rec.lines("new.log"), "n1", "n2")
	// a newly configured pattern starts at the end of what is there (first sight)
	appendFile(t, filepath.Join(d, "other.txt"), "x\n")
	stop(tl3)
	tl4 := newTailer(state, rec, filepath.Join(d, "*.log"), filepath.Join(d, "*.txt"))
	start(tl4)
	appendFile(t, filepath.Join(d, "other.txt"), "y\n")
	step(tl4)
	eq(t, rec.lines("other.txt"), "y")
	stop(tl4)
}

func TestTailRenameRotation(t *testing.T) {
	d := t.TempDir()
	log := filepath.Join(d, "app.log")
	appendFile(t, log, "")
	rec := &recorder{}
	tl := newTailer(filepath.Join(d, "positions.json"), rec, log)
	start(tl)
	appendFile(t, log, "1\n2\n")
	step(tl)
	appendFile(t, log, "3\nlast-frag")
	if err := os.Rename(log, log+".1"); err != nil {
		t.Fatal(err)
	}
	appendFile(t, log+".1", "\n4 written late\n") // the writer has not reopened yet
	appendFile(t, log, "new 1\n")
	for i := 0; i < 4; i++ {
		step(tl)
	}
	appendFile(t, log, "new 2\n")
	step(tl)
	eq(t, rec.lines("app.log"), "1", "2", "3", "last-frag", "4 written late", "new 1", "new 2")
	stop(tl)
}

func TestTailFragmentOfRotatedFileIsItsLastLine(t *testing.T) {
	d := t.TempDir()
	log := filepath.Join(d, "app.log")
	appendFile(t, log, "")
	rec := &recorder{}
	tl := newTailer("", rec, log)
	start(tl)
	appendFile(t, log, "x\nno newline")
	step(tl)
	_ = os.Rename(log, log+".1")
	for i := 0; i < 3; i++ {
		step(tl)
	}
	eq(t, rec.lines("app.log"), "x", "no newline")
}

func TestTailCopyTruncate(t *testing.T) {
	d := t.TempDir()
	log := filepath.Join(d, "app.log")
	appendFile(t, log, "")
	rec := &recorder{}
	tl := newTailer(filepath.Join(d, "positions.json"), rec, filepath.Join(d, "app.log*"))
	start(tl)
	long := strings.Repeat("x", 300) // long enough for a full fingerprint
	appendFile(t, log, long+"\nbefore 2\n")
	step(tl)
	// logrotate copytruncate: the copy matches the glob, the original is emptied and written again
	b, _ := os.ReadFile(log)
	_ = os.WriteFile(log+".1", b, 0o644)
	_ = os.Truncate(log, 0)
	appendFile(t, log, "after\n")
	tl.scan(time.Now(), false)
	for i := 0; i < 3; i++ {
		step(tl)
	}
	eq(t, rec.lines("app.log"), long, "before 2", "after")
	if got := rec.lines("app.log.1"); len(got) != 0 {
		t.Fatalf("the copy was shipped again: %q", got)
	}
	stop(tl)
}

func TestTailRenamedIntoTheGlobIsNotShippedTwice(t *testing.T) {
	d := t.TempDir()
	log := filepath.Join(d, "app.log")
	appendFile(t, log, "")
	state := filepath.Join(d, "positions.json")
	rec := &recorder{}
	pat := filepath.Join(d, "*.log")
	tl := newTailer(state, rec, pat)
	start(tl)
	appendFile(t, log, "one\ntwo\n")
	step(tl)
	_ = os.Rename(log, filepath.Join(d, "app-2026-10-16.log")) // logback-style rolling: the old file keeps a matching name
	appendFile(t, log, "three\n")
	for i := 0; i < 3; i++ {
		step(tl)
	}
	tl.scan(time.Now(), false) // later rescans meet the renamed file again
	step(tl)
	stop(tl)
	tl2 := newTailer(state, rec, pat) // and so does a restart
	start(tl2)
	step(tl2)
	eq(t, rec.lines("app.log"), "one", "two", "three")
	eq(t, rec.lines("app-2026-10-16.log"))
	stop(tl2)
}

func TestTailRotatedWhileStopped(t *testing.T) {
	d := t.TempDir()
	log := filepath.Join(d, "app.log")
	appendFile(t, log, "")
	state := filepath.Join(d, "positions.json")
	rec := &recorder{}
	tl := newTailer(state, rec, log)
	start(tl)
	appendFile(t, log, "a\n")
	step(tl)
	stop(tl)
	appendFile(t, log, "b written while stopped\n")
	_ = os.Rename(log, log+".1")
	appendFile(t, log, "c in the new file\n")
	tl2 := newTailer(state, rec, log)
	start(tl2)
	for i := 0; i < 3; i++ {
		step(tl2)
	}
	got := rec.lines("app.log")
	sort.Strings(got)
	eq(t, got, "a", "b written while stopped", "c in the new file")
	stop(tl2)
}

func TestTailRetryHoldsThePosition(t *testing.T) {
	d := t.TempDir()
	log := filepath.Join(d, "app.log")
	appendFile(t, log, "")
	state := filepath.Join(d, "positions.json")
	rec := &recorder{}
	tl := newTailer(state, rec, log)
	start(tl)
	rec.result = Retry // no spool and the receiver is away: the file is the buffer
	appendFile(t, log, "1\n2\n")
	step(tl)
	step(tl)
	stop(tl)
	tl2 := newTailer(state, rec, log)
	start(tl2)
	rec.mu.Lock()
	rec.result = Delivered
	rec.mu.Unlock()
	appendFile(t, log, "3\n")
	step(tl2)
	eq(t, rec.lines("app.log"), "1", "2", "3")
	stop(tl2)
}

func TestTailBatchesAreCappedAndLongLinesSplit(t *testing.T) {
	d := t.TempDir()
	log := filepath.Join(d, "big.log")
	appendFile(t, log, "")
	rec := &recorder{}
	tl := newTailer("", rec, log)
	tl.MaxBatch, tl.MaxLine = 1000, 500
	start(tl)
	var want []string
	var sb strings.Builder
	for i := 0; i < 300; i++ {
		l := fmt.Sprintf("line %03d", i)
		want = append(want, l)
		sb.WriteString(l + "\n")
	}
	appendFile(t, log, sb.String())
	for i := 0; i < 10; i++ {
		step(tl)
	}
	eq(t, rec.lines("big.log"), want...)
	if rec.bodies < 2 { // 2.700 bytes with a 1.000 byte cap (a read is at least 512 bytes)
		t.Fatalf("batches not capped: %d bodies", rec.bodies)
	}
	appendFile(t, log, strings.Repeat("y", 1200)+"\n")
	for i := 0; i < 3; i++ {
		step(tl)
	}
	got := rec.lines("big.log")[300:]
	if strings.Join(got, "") != strings.Repeat("y", 1200) || len(got) < 2 {
		t.Fatalf("long line: %d pieces", len(got))
	}
}

func TestSeedThenRun(t *testing.T) {
	d := t.TempDir()
	log, state := filepath.Join(d, "app.log"), filepath.Join(d, "positions.json")
	appendFile(t, log, "shipped by agent.py\n")
	n, err := (&Tailer{Patterns: []string{log}, State: state}).Seed()
	if err != nil || n != 1 {
		t.Fatal(n, err)
	}
	if n, _ := (&Tailer{Patterns: []string{log}, State: state}).Seed(); n != -1 {
		t.Fatal("an existing positions file must be kept")
	}
	appendFile(t, log, "after the hand-over\n")
	rec := &recorder{}
	tl := newTailer(state, rec, log)
	start(tl)
	step(tl)
	eq(t, rec.lines("app.log"), "after the hand-over")
	stop(tl)
}

func TestTailRunShutdownFlushes(t *testing.T) {
	d := t.TempDir()
	log := filepath.Join(d, "app.log")
	appendFile(t, log, "")
	rec := &recorder{}
	tl := &Tailer{Patterns: []string{log}, State: filepath.Join(d, "p.json"), Send: rec.send, Poll: 20 * time.Millisecond, FlushEvery: time.Hour}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { tl.Run(ctx); close(done) }()
	time.Sleep(100 * time.Millisecond)
	appendFile(t, log, "x\ny\n")
	time.Sleep(100 * time.Millisecond)
	cancel()
	<-done
	eq(t, rec.lines("app.log"), "x", "y")
}

func TestTailCopyOfTruncatedFileShipsOnlyWhatTheOriginalLost(t *testing.T) {
	d := t.TempDir()
	log := filepath.Join(d, "app.log")
	appendFile(t, log, "")
	rec := &recorder{}
	tl := newTailer(filepath.Join(d, "positions.json"), rec, filepath.Join(d, "app.log*"))
	start(tl)
	long := strings.Repeat("h", 300)
	appendFile(t, log, long+"\nread 1\n")
	step(tl)
	appendFile(t, log, "missed by the original\n") // written, then copied and truncated before the next poll
	b, _ := os.ReadFile(log)
	_ = os.Truncate(log, 0)
	appendFile(t, log, "after\n")
	step(tl) // the truncation is noticed first
	_ = os.WriteFile(log+".1", b, 0o644)
	tl.scan(time.Now(), false)
	step(tl)
	eq(t, rec.lines("app.log"), long, "read 1", "after")
	eq(t, rec.lines("app.log.1"), "missed by the original")
	stop(tl)
}

func TestTailNewFileWithTheSameBannerIsNotACopy(t *testing.T) {
	d := t.TempDir()
	log := filepath.Join(d, "SystemOut.log")
	banner := strings.Repeat("*", 300) + "\n" // every new file starts with the same header (WebSphere and friends)
	appendFile(t, log, banner+"old a\nold b\n")
	rec := &recorder{}
	tl := newTailer(filepath.Join(d, "positions.json"), rec, filepath.Join(d, "*.log*"))
	start(tl)
	appendFile(t, log, "old c\n")
	step(tl)
	_ = os.Rename(log, log+".1")
	appendFile(t, log, banner+"new 1\n")
	for i := 0; i < 3; i++ {
		step(tl)
	}
	eq(t, rec.lines("SystemOut.log"), "old c", strings.TrimSuffix(banner, "\n"), "new 1")
	stop(tl)
}
