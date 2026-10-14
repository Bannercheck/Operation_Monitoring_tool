package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

func setup(t *testing.T, st *authState, mod func(*config)) (*server, *httptest.Server, func()) {
	t.Helper()
	dir := t.TempDir()
	cfg := config{spool: dir, authFile: filepath.Join(dir, "auth.json"), maxBody: 1 << 20, segBytes: 1 << 20, maxBacklog: 1 << 30, segEvery: 50 * time.Millisecond, agentDir: dir}
	if mod != nil {
		mod(&cfg)
	}
	if st != nil {
		b, _ := json.Marshal(st)
		_ = os.WriteFile(cfg.authFile, b, 0o600)
	}
	sp, err := newSpool(cfg)
	if err != nil {
		t.Fatal(err)
	}
	a := &auth{path: cfg.authFile}
	a.reload()
	up, _ := url.Parse("http://127.0.0.1:1")
	s := &server{cfg: cfg, auth: a, sp: sp, proxy: httputil.NewSingleHostReverseProxy(up), start: time.Now()}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { sp.run(ctx); close(done) }()
	ts := httptest.NewServer(s.routes())
	return s, ts, func() { ts.Close(); cancel(); <-done }
}

func post(t *testing.T, url, tok, body string) (*http.Response, map[string]any) {
	req, _ := http.NewRequest("POST", url, strings.NewReader(body))
	if tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	req.Header.Set("X-File-Name", "../../etc/app.log")
	r, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	_ = json.NewDecoder(r.Body).Decode(&m)
	r.Body.Close()
	return r, m
}

func records(t *testing.T, dir string) []map[string]any {
	var out []map[string]any
	files, _ := filepath.Glob(filepath.Join(dir, "seg-*.wo"))
	for _, f := range files {
		b, _ := os.ReadFile(f)
		rd := bufio.NewReader(bytes.NewReader(b))
		for {
			line, err := rd.ReadBytes('\n')
			if err != nil {
				break
			}
			var h map[string]any
			if json.Unmarshal(line, &h) != nil {
				t.Fatalf("bad header %q", line)
			}
			n := int(h["len"].(float64))
			body := make([]byte, n+1)
			if _, err := rd.Read(body); err != nil {
				t.Fatal(err)
			}
			h["body"] = string(body[:n])
			out = append(out, h)
		}
	}
	return out
}

func TestTokenAuthSpoolAndRotation(t *testing.T) {
	st := &authState{Agents: map[string]agentRec{sha("wo_good"): {ID: 7, Name: "erp-01", Env: "prod", Site: "ist"}}}
	s, ts, stop := setup(t, st, nil)
	r, m := post(t, ts.URL+"/ingest", "wo_good", "a\nb\nc\n")
	if r.StatusCode != 200 || m["accepted"].(float64) != 3 || m["agent"] != "erp-01" || m["queued"] != true {
		t.Fatalf("got %d %v", r.StatusCode, m)
	}
	if r, _ := post(t, ts.URL+"/ingest", "wo_revoked", "x\n"); r.StatusCode != 401 {
		t.Fatalf("revoked token: %d", r.StatusCode)
	}
	if r, _ := post(t, ts.URL+"/ingest", "", "x\n"); r.StatusCode != 401 { // agents enrolled, no shared key: closed
		t.Fatalf("anonymous: %d", r.StatusCode)
	}
	time.Sleep(200 * time.Millisecond) // segment closes after segEvery
	recs := records(t, s.cfg.spool)
	if len(recs) != 1 || recs[0]["agent"] != "erp-01" || recs[0]["agent_id"].(float64) != 7 || recs[0]["env"] != "prod" || recs[0]["file"] != "app.log" || recs[0]["body"] != "a\nb\nc\n" {
		t.Fatalf("records %v", recs)
	}
	stop()
}

func TestOpenModeKeyAndNotReady(t *testing.T) {
	_, ts, stop := setup(t, nil, nil)
	if r, _ := post(t, ts.URL+"/ingest", "", "x\n"); r.StatusCode != 503 || r.Header.Get("Retry-After") == "" {
		t.Fatalf("not ready should be 503: %d", r.StatusCode)
	}
	stop()
	_, ts, stop = setup(t, &authState{Open: true}, nil)
	if r, m := post(t, ts.URL+"/ingest", "", "x"); r.StatusCode != 200 || m["identity"] != "open" || m["accepted"].(float64) != 1 {
		t.Fatalf("open: %d %v", r.StatusCode, m)
	}
	stop()
	_, ts, stop = setup(t, &authState{KeySHA256: sha("k3y")}, nil)
	if r, m := post(t, ts.URL+"/ingest", "k3y", "x\n"); r.StatusCode != 200 || m["identity"] != "key" {
		t.Fatalf("key: %d %v", r.StatusCode, m)
	}
	if r, _ := post(t, ts.URL+"/ingest", "nope", "x\n"); r.StatusCode != 401 {
		t.Fatalf("wrong key: %d", r.StatusCode)
	}
	stop()
}

func TestBackpressureLimitsAndHealth(t *testing.T) {
	s, ts, stop := setup(t, &authState{Open: true}, func(c *config) { c.maxBacklog = 10; c.maxBody = 64 })
	if r, _ := post(t, ts.URL+"/ingest", "", strings.Repeat("y", 100)); r.StatusCode != 413 {
		t.Fatalf("too large: %d", r.StatusCode)
	}
	if r, _ := post(t, ts.URL+"/ingest", "", ""); r.StatusCode != 400 {
		t.Fatalf("empty: %d", r.StatusCode)
	}
	post(t, ts.URL+"/ingest", "", strings.Repeat("z", 40))
	time.Sleep(200 * time.Millisecond)
	if r, _ := post(t, ts.URL+"/ingest", "", "more\n"); r.StatusCode != 503 || r.Header.Get("Retry-After") != "10" {
		t.Fatalf("backlog full should be 503: %d", r.StatusCode)
	}
	hr, _ := http.Get(ts.URL + "/health")
	var h map[string]any
	_ = json.NewDecoder(hr.Body).Decode(&h)
	if h["engine"] != "go" || h["busy"].(float64) < 1 || h["backlog_segments"].(float64) < 1 {
		t.Fatalf("health %v", h)
	}
	mr, _ := http.Get(ts.URL + "/metrics")
	buf := new(bytes.Buffer)
	_, _ = buf.ReadFrom(mr.Body)
	if !strings.Contains(buf.String(), "watchover_receiver_batches 1") {
		t.Fatalf("metrics %s", buf.String())
	}
	_ = s
	stop()
}

func TestAgentFilesEnrollProxyAndCrashRecovery(t *testing.T) {
	s, ts, stop := setup(t, &authState{Open: true}, nil)
	_ = os.WriteFile(filepath.Join(s.cfg.agentDir, "agent.py"), []byte("print('agent')\n"), 0o644)
	r, _ := http.Get(ts.URL + "/agent.py")
	if r.StatusCode != 200 {
		t.Fatalf("agent.py %d", r.StatusCode)
	}
	if r, _ := post(t, ts.URL+"/enroll", "k", "{}"); r.StatusCode != 502 { // dashboard down: a clear retryable error
		t.Fatalf("enroll proxy %d", r.StatusCode)
	}
	stop()
	dir := t.TempDir() // a torn .tmp segment from a crash is closed on start and counted as backlog
	_ = os.WriteFile(filepath.Join(dir, "seg-0000000000000000001.tmp"), []byte(`{"v":1,"len":3}`+"\nabc\n"+`{"v":1,"len":99}`+"\nxy"), 0o640)
	sp, _ := newSpool(config{spool: dir})
	if sp.segments.Load() != 1 {
		t.Fatalf("recovered segments %d", sp.segments.Load())
	}
	if _, err := os.Stat(filepath.Join(dir, "seg-0000000000000000001.wo")); err != nil {
		t.Fatal(err)
	}
	_ = strconv.Itoa(0)
}
