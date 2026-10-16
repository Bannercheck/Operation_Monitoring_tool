package update

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeBin is a runnable stand-in for a wo-agent build: `version` prints the version, and the marker is in its bytes.
func fakeBin(v string) []byte {
	return []byte("#!/bin/sh\necho 'wo-agent " + v + "'\n# WO-AGENT-VERSION=" + v + ";\n")
}

func canon(v any) []byte { b, _ := json.Marshal(v); return b } // map keys sorted, compact: keyd's _canon for these shapes

func sign(priv ed25519.PrivateKey, domain string, body any) *Signed {
	raw := canon(body)
	return &Signed{Body: base64.StdEncoding.EncodeToString(raw), Sig: base64.StdEncoding.EncodeToString(ed25519.Sign(priv, append([]byte(domain), raw...)))}
}

func files(bins map[string][]byte) map[string]any {
	out := map[string]any{}
	for n, b := range bins {
		out[n] = map[string]any{"sha256": sha256Hex(b), "size": len(b)}
	}
	return out
}

type world struct {
	t        *testing.T
	priv     ed25519.PrivateKey
	pub      ed25519.PublicKey
	srv      *httptest.Server
	dir, bin string
	mu       sync.Mutex
	decision map[string]any
	serve    []byte // what /agent/wo-agent-linux-<arch> returns
	healthy  map[string]bool
	calls    []string
}

func newWorld(t *testing.T, running string) *world {
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	w := &world{t: t, priv: priv, pub: pub, dir: t.TempDir(), healthy: map[string]bool{}}
	w.bin = filepath.Join(t.TempDir(), "wo-agent")
	if err := os.WriteFile(w.bin, fakeBin(running), 0o755); err != nil {
		t.Fatal(err)
	}
	w.srv = httptest.NewServer(http.HandlerFunc(func(rw http.ResponseWriter, r *http.Request) {
		w.mu.Lock()
		defer w.mu.Unlock()
		switch r.URL.Path {
		case "/agent/update":
			if r.Header.Get("Authorization") != "Bearer wo_tok" {
				rw.WriteHeader(401)
				return
			}
			_ = json.NewEncoder(rw).Encode(w.decision)
		case "/agent/wo-agent-linux-" + runtime.GOARCH:
			_, _ = rw.Write(w.serve)
		default:
			rw.WriteHeader(404)
		}
	}))
	t.Cleanup(w.srv.Close)
	return w
}

// release publishes a signed release of version v (whose binary the receiver serves) and the receiver's decision.
func (w *world) release(v string, update bool, bin []byte) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.serve = bin
	rel := sign(w.priv, releaseDomain, map[string]any{"v": 1, "seq": 1, "version": v, "issued": "2026-10-16T10:00:00+00:00",
		"files": files(map[string][]byte{"wo-agent-linux-" + runtime.GOARCH: bin, "agent-install.sh": []byte("#!/bin/bash\n")})})
	w.decision = map[string]any{"release": rel, "update": update, "reason": map[bool]string{true: "", false: "paused"}[update]}
}

func (w *world) cfg(running string) *Config {
	return &Config{Base: w.srv.URL, Token: "wo_tok", Host: "app-07", Arch: runtime.GOARCH, Current: running, Key: w.pub, Binary: w.bin,
		Dir: w.dir, Units: []string{"watchover-agent", "watchover-actuator"}, Gate: 2 * time.Second, Client: w.srv.Client(),
		Systemctl: func(a ...string) error {
			w.mu.Lock()
			w.calls = append(w.calls, strings.Join(a, " "))
			w.mu.Unlock()
			if a[0] == "restart" { // "systemd" starts whatever is installed; a healthy build reaches the receiver
				b, _ := os.ReadFile(w.bin)
				v, _ := VersionOf(b)
				if w.healthy[v] {
					go func() { time.Sleep(50 * time.Millisecond); _ = WriteHello(w.dir, v) }()
				}
			}
			return nil
		}}
}

func (w *world) installed() string {
	b, _ := os.ReadFile(w.bin)
	v, _ := VersionOf(b)
	return v
}

func TestUpdateGoodRelease(t *testing.T) {
	w := newWorld(t, "0.8.0")
	w.healthy["0.9.0"] = true
	w.release("0.9.0", true, fakeBin("0.9.0"))
	st, err := Run(context.Background(), w.cfg("0.8.0"))
	if err != nil || st.Phase != PhaseUpdated || st.From != "0.8.0" || st.To != "0.9.0" {
		t.Fatalf("state %+v err %v", st, err)
	}
	if w.installed() != "0.9.0" {
		t.Fatalf("installed %s", w.installed())
	}
	if prev, _ := os.ReadFile(w.bin + ".prev"); string(prev) != string(fakeBin("0.8.0")) {
		t.Fatal("previous binary not kept")
	}
	if strings.Join(w.calls, ",") != "restart watchover-agent,try-restart watchover-actuator" {
		t.Fatalf("systemctl calls %v (the actuator is only try-restarted: a stopped one stays stopped)", w.calls)
	}
	if h := LoadState(w.dir).Header(); !strings.HasPrefix(h, "updated;0.9.0;0.8.0;") {
		t.Fatalf("header %q", h)
	}
}

func TestUpdateRefusesBadSignature(t *testing.T) {
	w := newWorld(t, "0.8.0")
	w.healthy["0.9.0"] = true
	w.release("0.9.0", true, fakeBin("0.9.0"))
	_, other, _ := ed25519.GenerateKey(rand.Reader)
	w.priv = other // a release signed by any other key (a MITM, another dashboard)
	w.release("0.9.0", true, fakeBin("0.9.0"))
	st, err := Run(context.Background(), w.cfg("0.8.0"))
	if err == nil || st.Phase != PhaseFailed || !strings.Contains(err.Error(), "signature") || w.installed() != "0.8.0" || len(w.calls) != 0 {
		t.Fatalf("state %+v err %v calls %v", st, err, w.calls)
	}
}

func TestUpdateRefusesTamperedBody(t *testing.T) {
	w := newWorld(t, "0.8.0")
	w.release("0.9.0", true, fakeBin("0.9.0"))
	rel := w.decision["release"].(*Signed)
	raw, _ := base64.StdEncoding.DecodeString(rel.Body)
	rel.Body = base64.StdEncoding.EncodeToString([]byte(strings.Replace(string(raw), "0.9.0", "9.9.9", 1)))
	if _, err := Run(context.Background(), w.cfg("0.8.0")); err == nil || w.installed() != "0.8.0" {
		t.Fatalf("tampered manifest accepted: %v", err)
	}
}

func TestUpdateRefusesBadSHA(t *testing.T) {
	w := newWorld(t, "0.8.0")
	w.healthy["0.9.0"] = true
	w.release("0.9.0", true, fakeBin("0.9.0"))
	w.serve = append(fakeBin("0.9.0"), []byte("# evil\n")...) // the receiver (or the wire) serves other bytes
	st, err := Run(context.Background(), w.cfg("0.8.0"))
	if err == nil || st.Phase != PhaseFailed || w.installed() != "0.8.0" || len(w.calls) != 0 {
		t.Fatalf("state %+v err %v", st, err)
	}
	if _, e := os.Stat(w.bin + ".new"); e == nil {
		t.Fatal("left a .new file behind")
	}
}

func TestUpdateRefusesWrongVersionMarker(t *testing.T) {
	w := newWorld(t, "0.8.0")
	w.release("0.9.0", true, fakeBin("0.7.0")) // signed as 0.9.0 but the bytes say 0.7.0
	if st, err := Run(context.Background(), w.cfg("0.8.0")); err == nil || w.installed() != "0.8.0" {
		t.Fatalf("state %+v err %v", st, err)
	}
}

func TestDowngradeRefusedWithoutPin(t *testing.T) {
	w := newWorld(t, "0.9.0")
	w.healthy["0.8.0"] = true
	w.release("0.8.0", true, fakeBin("0.8.0")) // a correctly signed but older release (e.g. the dashboard went back)
	st, err := Run(context.Background(), w.cfg("0.9.0"))
	if err != nil || st.Phase != PhaseCurrent || !strings.Contains(st.Reason, "older") || w.installed() != "0.9.0" {
		t.Fatalf("state %+v err %v", st, err)
	}
}

func TestPinnedRollbackUsesKeptBinary(t *testing.T) {
	w := newWorld(t, "0.8.0")
	w.healthy["0.8.0"], w.healthy["0.9.0"] = true, true
	w.release("0.9.0", true, fakeBin("0.9.0"))
	if st, _ := Run(context.Background(), w.cfg("0.8.0")); st.Phase != PhaseUpdated {
		t.Fatal(st)
	}
	old := fakeBin("0.8.0")
	pinBody := func(host, exp string) *Signed {
		return sign(w.priv, pinDomain, map[string]any{"v": 1, "host": host, "version": "0.8.0", "seq": 1, "issued": "x", "expires_at": exp,
			"files": files(map[string][]byte{"wo-agent-linux-" + runtime.GOARCH: old})})
	}
	future := time.Now().Add(time.Hour).UTC().Format(time.RFC3339)
	w.mu.Lock()
	w.serve = nil                                     // nothing to download: the kept .prev is the requested build
	w.decision["pin"] = pinBody("other-host", future) // bound to another host: refused
	w.mu.Unlock()
	if _, err := Run(context.Background(), w.cfg("0.9.0")); err == nil || w.installed() != "0.9.0" {
		t.Fatalf("a pin for another host was accepted: %v", err)
	}
	w.mu.Lock()
	w.decision["pin"] = pinBody("app-07", time.Now().Add(-time.Minute).UTC().Format(time.RFC3339)) // expired
	w.mu.Unlock()
	if _, err := Run(context.Background(), w.cfg("0.9.0")); err == nil || w.installed() != "0.9.0" {
		t.Fatalf("an expired pin was accepted: %v", err)
	}
	w.mu.Lock()
	w.decision["pin"] = pinBody("APP-07", future)
	w.mu.Unlock()
	st, err := Run(context.Background(), w.cfg("0.9.0"))
	if err != nil || st.Phase != PhaseUpdated || st.To != "0.8.0" || w.installed() != "0.8.0" {
		t.Fatalf("pinned rollback: state %+v err %v", st, err)
	}
	// still pinned: the newer release is not installed again
	if st, _ := Run(context.Background(), w.cfg("0.8.0")); st.Phase != PhaseCurrent || st.Reason != "pinned" || w.installed() != "0.8.0" {
		t.Fatalf("pinned host moved on: %+v", st)
	}
}

func TestRollbackWhenNewBinaryDoesNotReportBack(t *testing.T) {
	w := newWorld(t, "0.8.0")
	w.healthy["0.8.0"] = true // 0.9.0 starts but never reaches the receiver
	w.release("0.9.0", true, fakeBin("0.9.0"))
	st, err := Run(context.Background(), w.cfg("0.8.0"))
	if err != nil || st.Phase != PhaseRolledBack || !strings.Contains(st.Error, "did not reach the receiver") || w.installed() != "0.8.0" {
		t.Fatalf("state %+v err %v installed %s", st, err, w.installed())
	}
	if !strings.HasPrefix(LoadState(w.dir).Header(), "rolled_back;0.9.0;0.8.0;") {
		t.Fatal(LoadState(w.dir).Header())
	}
	// the same build is retried until MaxTries failures, then skipped until a newer one appears
	for i := 1; i < MaxTries; i++ {
		if st, _ := Run(context.Background(), w.cfg("0.8.0")); st.Phase != PhaseRolledBack {
			t.Fatalf("try %d: %+v", i, st)
		}
	}
	st, _ = Run(context.Background(), w.cfg("0.8.0"))
	if st.Phase != PhaseCurrent || !strings.Contains(st.Reason, "skipped") || w.installed() != "0.8.0" {
		t.Fatalf("not skipped: %+v", st)
	}
}

func TestRollbackWhenNewBinaryDoesNotRun(t *testing.T) {
	w := newWorld(t, "0.8.0")
	broken := []byte("#!/bin/sh\nexit 1\n# WO-AGENT-VERSION=0.9.0;\n")
	w.release("0.9.0", true, broken)
	st, err := Run(context.Background(), w.cfg("0.8.0"))
	if err == nil || st.Phase != PhaseFailed || !strings.Contains(st.Error, "does not run") || w.installed() != "0.8.0" || len(w.calls) != 0 {
		t.Fatalf("state %+v err %v", st, err)
	}
}

func TestReceiverSaysNo(t *testing.T) {
	w := newWorld(t, "0.8.0")
	w.release("0.9.0", false, fakeBin("0.9.0")) // paused (or switch off, or outside the rollout)
	st, err := Run(context.Background(), w.cfg("0.8.0"))
	if err != nil || st.Phase != PhaseCurrent || st.Reason != "paused" || w.installed() != "0.8.0" {
		t.Fatalf("state %+v err %v", st, err)
	}
}

func TestRecoverPendingAfterCrash(t *testing.T) {
	w := newWorld(t, "0.8.0")
	w.release("0.9.0", false, fakeBin("0.9.0"))
	cfg := w.cfg("0.9.0")
	// the run that installed 0.9.0 died during its gate; 0.9.0 never reported; the kept 0.8.0 comes back
	_ = os.WriteFile(w.bin+".prev", fakeBin("0.8.0"), 0o755)
	_ = os.WriteFile(w.bin, fakeBin("0.9.0"), 0o755)
	cfg.save(State{Phase: PhasePending, From: "0.8.0", To: "0.9.0"})
	w.healthy["0.8.0"] = true
	st, _ := Run(context.Background(), cfg)
	if st.Phase != PhaseRolledBack || w.installed() != "0.8.0" {
		t.Fatalf("state %+v installed %s", st, w.installed())
	}
}

func TestVersionsAndKeys(t *testing.T) {
	for _, c := range []struct {
		a, b string
		want int
	}{{"0.8.0", "0.9.0", -1}, {"0.10.0", "0.9.9", 1}, {"1.0.0", "1.0.0", 0}, {"1.0.0-rc1", "1.0.0", -1}, {"1.2.3", "1.2.3-rc1", 1}} {
		if got := Compare(c.a, c.b); got != c.want {
			t.Errorf("Compare(%s,%s)=%d", c.a, c.b, got)
		}
	}
	if _, err := VersionOf([]byte("WO-AGENT-VERSION=1.0.0; WO-AGENT-VERSION=1.1.0;")); err == nil {
		t.Error("two markers accepted")
	}
	if _, err := ParseKey("ed25519:AAAA"); err == nil {
		t.Error("short key accepted")
	}
	pub := ed25519.PublicKey(make([]byte, 32))
	if fp := Fingerprint(pub); !strings.HasPrefix(fp, "SHA256:") || strings.HasSuffix(fp, "=") || len(fp) != 7+43 {
		t.Errorf("fingerprint %q", fp)
	}
}
