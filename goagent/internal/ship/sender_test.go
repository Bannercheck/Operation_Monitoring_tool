package ship

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestSpoolFormatCapsAndReopen(t *testing.T) {
	d := filepath.Join(t.TempDir(), "spool")
	s, err := OpenSpool(d, 3, 0)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 5; i++ {
		if err := s.Put([]byte(strings.Repeat("x", i+1)+"\n"), "/var/log/app.log"); err != nil {
			t.Fatal(err)
		}
	}
	if s.Len() != 3 || s.Evicted != 2 {
		t.Fatalf("len %d evicted %d", s.Len(), s.Evicted)
	}
	key, name, body, ok := s.Oldest()
	if !ok || name != "app.log" || string(body) != "xxx\n" || !strings.HasSuffix(key, "__app.log") {
		t.Fatalf("%s %s %q", key, name, body)
	}
	if fi, _ := os.Stat(d); fi.Mode().Perm() != 0o700 {
		t.Fatalf("spool dir mode %v", fi.Mode().Perm())
	}
	if fi, _ := os.Stat(filepath.Join(d, key)); fi.Mode().Perm() != 0o600 {
		t.Fatalf("batch mode %v", fi.Mode().Perm())
	}
	_ = os.WriteFile(filepath.Join(d, "1000000000000000000__left-by-agent.py.log"), []byte("py\n"), 0o600) // agent.py's own spool files drain too
	_ = os.WriteFile(filepath.Join(d, ".tmp-9__torn"), []byte("x"), 0o600)
	s2, err := OpenSpool(d, 2000, 10)
	if err != nil {
		t.Fatal(err)
	}
	if _, name, _, _ := s2.Oldest(); name != "left-by-agent.py.log" {
		t.Fatalf("oldest %s", name)
	}
	if _, err := os.Stat(filepath.Join(d, ".tmp-9__torn")); err == nil {
		t.Fatal("torn write kept")
	}
	_ = s2.Put([]byte("0123456789abc\n"), "big.log") // byte cap 10: older batches give way, the newest stays
	if s2.Len() != 1 {
		t.Fatalf("byte cap: %d left", s2.Len())
	}
}

type fakeReceiver struct {
	mu      sync.Mutex
	codes   []int // answers in order, then 200
	headers []http.Header
	bodies  []string
	names   []string
}

func (f *fakeReceiver) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	b, _ := io.ReadAll(r.Body)
	f.mu.Lock()
	code := 200
	if len(f.codes) > 0 {
		code, f.codes = f.codes[0], f.codes[1:]
	}
	if code == 200 {
		f.headers = append(f.headers, r.Header.Clone())
		f.bodies = append(f.bodies, string(b))
		f.names = append(f.names, r.Header.Get("X-File-Name"))
	}
	f.mu.Unlock()
	if code == 503 {
		w.Header().Set("Retry-After", "1")
	}
	w.WriteHeader(code)
	_, _ = w.Write([]byte(`{"accepted": 1, "agent": "web-1", "identity": "token", "queued": true}`))
}

func (f *fakeReceiver) set(codes ...int) {
	f.mu.Lock()
	f.codes = codes
	f.mu.Unlock()
}

func (f *fakeReceiver) got() ([]string, []string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.names...), append([]string(nil), f.bodies...)
}

func TestSenderHeadersSpoolRetryAfterAndDrainOrder(t *testing.T) {
	fr := &fakeReceiver{}
	srv := httptest.NewServer(fr)
	defer srv.Close()
	cl, _ := NewClient("", false, 5*time.Second)
	sp, _ := OpenSpool(filepath.Join(t.TempDir(), "spool"), 2000, 0)
	s := &Sender{URL: srv.URL + "/ingest", Key: "wo_tok", Agent: "web-1", Version: Version, Client: cl, Spool: sp}
	ctx := context.Background()
	if r := s.Send(ctx, []byte("a\n"), "app.log"); r != Delivered {
		t.Fatal(r)
	}
	h := fr.headers[0]
	if h.Get("Authorization") != "Bearer wo_tok" || h.Get("X-Agent") != "web-1" || h.Get("X-File-Name") != "app.log" ||
		h.Get("X-Agent-Version") != Version || h.Get("Content-Type") != "application/octet-stream" {
		t.Fatalf("headers %v", h)
	}
	fr.set(503) // backlog full: Retry-After 1
	if r := s.Send(ctx, []byte("b\n"), "app.log"); r != Spooled {
		t.Fatal(r)
	}
	if p, d := s.paused(); !p || d > time.Second {
		t.Fatalf("not paused for Retry-After: %v %v", p, d)
	}
	if r := s.Send(ctx, []byte("c\n"), "app.log"); r != Spooled { // while paused nothing is posted
		t.Fatal(r)
	}
	dctx, cancel := context.WithCancel(ctx)
	defer cancel()
	go s.Drain(dctx)
	deadline := time.Now().Add(5 * time.Second)
	for sp.Len() > 0 && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if r := s.Send(ctx, []byte("d\n"), "app.log"); r != Delivered {
		t.Fatal(r)
	}
	_, bodies := fr.got()
	if strings.Join(bodies, "") != "a\nb\nc\nd\n" {
		t.Fatalf("order %q", bodies)
	}
	// a bad token is never spooled; content the receiver refuses for good is dropped
	fr.set(401)
	if r := s.Send(ctx, []byte("e\n"), "app.log"); r != Dropped || sp.Len() != 0 {
		t.Fatal(r, sp.Len())
	}
	fr.set(422)
	if r := s.Send(ctx, []byte("f\n"), "app.log"); r != Dropped || sp.Len() != 0 {
		t.Fatal(r)
	}
	// too large: split at a line boundary instead of dropping
	fr.set(413)
	if r := s.Send(ctx, []byte("g1\ng2\ng3\ng4\n"), "app.log"); r != Delivered {
		t.Fatal(r)
	}
	_, bodies = fr.got()
	if strings.Join(bodies[len(bodies)-2:], "") != "g1\ng2\ng3\ng4\n" {
		t.Fatalf("split %q", bodies)
	}
}

func TestSenderWithoutSpoolAsksToRetry(t *testing.T) {
	cl, _ := NewClient("", false, time.Second)
	s := &Sender{URL: "http://127.0.0.1:1/ingest", Agent: "a", Version: Version, Client: cl}
	if r := s.Send(context.Background(), []byte("x\n"), "a.log"); r != Retry {
		t.Fatal(r)
	}
	if p, _ := s.paused(); !p {
		t.Fatal("network error must pause direct sends")
	}
	if !strings.Contains(s.Stats(), "failed=1") {
		t.Fatal(s.Stats())
	}
}
