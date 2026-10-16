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

func TestRumAndMobileIntake(t *testing.T) {
	st := &authState{Apps: map[string]appRec{"pk_web": {Name: "ik-portal", Origins: []string{"https://ik.example.com", "*.turkcell.com.tr"}}, "pk_mob": {Name: "ik-mobil"}}}
	s, ts, stop := setup(t, st, nil)
	defer stop()
	send := func(path, origin, body string) *http.Response {
		req, _ := http.NewRequest("POST", ts.URL+path, strings.NewReader(body))
		if origin != "" {
			req.Header.Set("Origin", origin)
		}
		r, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		r.Body.Close()
		return r
	}
	if r := send("/rum?k=pk_web", "https://ik.example.com", `{"session":"s1","events":[]}`); r.StatusCode != 202 || r.Header.Get("Access-Control-Allow-Origin") != "https://ik.example.com" {
		t.Fatalf("web %d %v", r.StatusCode, r.Header)
	}
	if r := send("/rum?k=pk_web", "https://hr.turkcell.com.tr", `{}`); r.StatusCode != 202 {
		t.Fatalf("wildcard origin %d", r.StatusCode)
	}
	if r := send("/rum?k=pk_web", "https://evil.example", `{}`); r.StatusCode != 403 {
		t.Fatalf("foreign origin %d", r.StatusCode)
	}
	if r := send("/mobile?k=pk_mob", "", `{"platform":"android"}`); r.StatusCode != 202 {
		t.Fatalf("mobile %d", r.StatusCode)
	}
	if r := send("/mobile?k=nope", "", `{}`); r.StatusCode != 401 {
		t.Fatalf("bad key %d", r.StatusCode)
	}
	if r := send("/rum?k=pk_web", "", strings.Repeat("x", rumMaxBody+10)); r.StatusCode != 413 {
		t.Fatalf("too large %d", r.StatusCode)
	}
	req, _ := http.NewRequest("OPTIONS", ts.URL+"/rum?k=pk_web", nil)
	req.Header.Set("Origin", "https://ik.example.com")
	r, _ := http.DefaultClient.Do(req)
	if r.StatusCode != 204 || !strings.Contains(r.Header.Get("Access-Control-Allow-Headers"), "Content-Type") {
		t.Fatalf("preflight %d", r.StatusCode)
	}
	time.Sleep(200 * time.Millisecond)
	recs := records(t, s.cfg.spool)
	if len(recs) != 3 || recs[0]["path"] != "/rum" || recs[0]["app"] != "ik-portal" || recs[2]["path"] != "/mobile" || recs[0]["origin"] != "https://ik.example.com" {
		t.Fatalf("spooled %v", recs)
	}
}

// ---------------------------------------------------------------- action agent job channel (docs/design/action-agent.md §9.3.2, WP11a)
func jobSetup(t *testing.T, st *authState, upstream string) (*httptest.Server, func()) {
	t.Helper()
	dir := t.TempDir()
	cfg := config{spool: dir, authFile: filepath.Join(dir, "auth.json"), maxBody: 1 << 20, segBytes: 1 << 20, maxBacklog: 1 << 30, segEvery: 50 * time.Millisecond, agentDir: dir}
	b, _ := json.Marshal(st)
	_ = os.WriteFile(cfg.authFile, b, 0o600)
	sp, err := newSpool(cfg)
	if err != nil {
		t.Fatal(err)
	}
	a := &auth{path: cfg.authFile}
	a.reload()
	up, _ := url.Parse(upstream)
	s := &server{cfg: cfg, auth: a, sp: sp, proxy: httputil.NewSingleHostReverseProxy(up), start: time.Now()}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { sp.run(ctx); close(done) }()
	ts := httptest.NewServer(s.routes())
	return ts, func() { ts.Close(); cancel(); <-done }
}

func call(t *testing.T, method, u, tok, body string) (*http.Response, map[string]any) {
	req, _ := http.NewRequest(method, u, strings.NewReader(body))
	if tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	r, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	_ = json.NewDecoder(r.Body).Decode(&m)
	r.Body.Close()
	return r, m
}

func TestJobChannelOnlyActuatorTokens(t *testing.T) {
	var seen []string
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.Method+" "+r.URL.RequestURI())
		w.WriteHeader(204)
	}))
	defer up.Close()
	st := &authState{Agents: map[string]agentRec{sha("wo_collector"): {ID: 1, Name: "web-01"}}, KeySHA256: sha("shared"),
		Actuators: map[string]actRec{sha("wa_actuator"): {AgentID: 1, Host: "web-01"}}}
	ts, stop := jobSetup(t, st, up.URL)
	defer stop()
	for _, tok := range []string{"", "wo_collector", "shared", "wa_unknown"} {
		r, m := call(t, "GET", ts.URL+"/agent/v1/jobs?after=0", tok, "")
		if r.StatusCode != 401 || m["error"] != "actuator token required" {
			t.Fatalf("token %q: %d %v", tok, r.StatusCode, m)
		}
	}
	r, _ := call(t, "GET", ts.URL+"/agent/v1/jobs?after=3", "wa_actuator", "")
	if r.StatusCode != 204 || len(seen) != 1 || seen[0] != "GET /agent/v1/jobs?after=3" {
		t.Fatalf("proxied: %d %v", r.StatusCode, seen)
	}
	// the actuator token reaches nothing else: not /ingest, not /enroll
	r, _ = post(t, ts.URL+"/ingest", "wa_actuator", "line\n")
	if r.StatusCode != 401 {
		t.Fatalf("wa_ on /ingest: %d", r.StatusCode)
	}
	r, _ = post(t, ts.URL+"/enroll", "wa_actuator", "{}")
	if r.StatusCode != 401 {
		t.Fatalf("wa_ on /enroll: %d", r.StatusCode)
	}
	// body limit
	r, m := call(t, "POST", ts.URL+"/agent/v1/hello", "wa_actuator", strings.Repeat("x", (1<<20)+10))
	if r.StatusCode != 413 || m["error"] != "body too large" {
		t.Fatalf("body limit: %d %v", r.StatusCode, m)
	}
}

func TestJobChannelOpenModeIsNotEnough(t *testing.T) {
	st := &authState{Open: true, Actuators: map[string]actRec{}}
	ts, stop := jobSetup(t, st, "http://127.0.0.1:1")
	defer stop()
	r, _ := call(t, "POST", ts.URL+"/agent/v1/hello", "", "{}")
	if r.StatusCode != 401 {
		t.Fatalf("open mode on /agent/v1/: %d", r.StatusCode)
	}
	r, _ = post(t, ts.URL+"/ingest", "wa_anything", "line\n") // open mode accepts anyone on /ingest, but never a wa_ token
	if r.StatusCode != 401 {
		t.Fatalf("wa_ on /ingest in open mode: %d", r.StatusCode)
	}
	r, _ = post(t, ts.URL+"/ingest", "", "line\n")
	if r.StatusCode != 200 {
		t.Fatalf("open mode ingest: %d", r.StatusCode)
	}
}

func TestJobChannelOwnErrorText(t *testing.T) {
	st := &authState{Actuators: map[string]actRec{sha("wa_a"): {AgentID: 2, Host: "h"}}}
	ts, stop := jobSetup(t, st, "http://127.0.0.1:1") // the dashboard is down
	defer stop()
	r, m := call(t, "GET", ts.URL+"/agent/v1/jobs", "wa_a", "")
	if r.StatusCode != 502 || m["error"] != "job channel unavailable" {
		t.Fatalf("proxy error: %d %v", r.StatusCode, m)
	}
	r, m = post(t, ts.URL+"/enroll", "", "{}")
	if r.StatusCode != 502 || m["error"] == "job channel unavailable" {
		t.Fatalf("enroll keeps its own text: %d %v", r.StatusCode, m)
	}
}
