// Watchover receiver: the :8600 front door for agents, written in Go so that accepting data never waits on parsing.
//
// Every POST /ingest is authenticated, framed and appended to a spool segment on disk, and answered as soon as the bytes are
// in the file (milliseconds, whatever the load). The dashboard (Python) reads closed segments in the background, parses them and
// feeds the live store, history, anomaly engine and traces. A slow scan or a parse spike delays processing by seconds; it never
// stalls an agent's request, and agents never time out or pile up retries.
//
//	POST /ingest              agent batches (X-API-Key / Bearer token, X-File-Name, X-Agent)       -> spooled, 200 {"queued": true}
//	POST /enroll              agent self-enrolment                                                 -> proxied to the dashboard
//	GET  /agent.py, /agent/install.sh                                                             -> the collector itself
//	GET  /health, /metrics    counters, backlog, auth state (JSON / Prometheus text)
//
// Authentication mirrors the Python receiver: per-agent tokens (sha256 hashes exported by the dashboard to auth.json), the legacy
// shared key, open mode only while no agent is enrolled. Backpressure: when the unprocessed spool exceeds WO_MAX_BACKLOG_MB the
// receiver answers 503 with Retry-After, and agents keep the batch in their own disk spool until it clears.
//
// Spool record: one JSON header line {"v":1,"path","agent","agent_id","env","site","file","ip","ts","len"} + len body bytes + "\n".
// Segments: seg-<unixnano>.tmp while written, renamed to .wo when closed (every WO_SEGMENT_MS or WO_SEGMENT_MB).
package main

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

type config struct {
	listen, spool, authFile, upstream, agentDir string
	maxBody, segBytes, maxBacklog               int64
	segEvery                                    time.Duration
	fsync                                       bool
}

func env(k, d string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return d
}

func envInt(k string, d int64) int64 {
	if v, err := strconv.ParseInt(env(k, ""), 10, 64); err == nil && v > 0 {
		return v
	}
	return d
}

func loadConfig() config {
	spool := env("WO_SPOOL", "/data/spool")
	return config{
		listen: env("WO_LISTEN", ":8600"), spool: spool, authFile: env("WO_AUTH", filepath.Join(spool, "auth.json")),
		upstream: env("WO_UPSTREAM", "http://dashboard:8601"), agentDir: env("WO_AGENT_DIR", "/app/agent"),
		maxBody: envInt("WO_MAX_BODY_MB", 16) << 20, segBytes: envInt("WO_SEGMENT_MB", 8) << 20, maxBacklog: envInt("WO_MAX_BACKLOG_MB", 2048) << 20,
		segEvery: time.Duration(envInt("WO_SEGMENT_MS", 1000)) * time.Millisecond, fsync: env("WO_FSYNC", "rotate") != "off",
	}
}

// ---------------------------------------------------------------- authentication (auth.json written by the dashboard)

type agentRec struct {
	ID   int64  `json:"id"`
	Name string `json:"name"`
	Env  string `json:"env"`
	Site string `json:"site"`
}

type authState struct {
	Agents    map[string]agentRec `json:"agents"`     // sha256(token) -> agent
	KeySHA256 string              `json:"key_sha256"` // legacy shared key, "" when none
	Open      bool                `json:"open"`       // no key and no active agent: accept anyone (first setup only)
	TS        string              `json:"ts"`
}

type auth struct {
	path  string
	mu    sync.RWMutex
	st    *authState
	mtime time.Time
}

func (a *auth) reload() {
	fi, err := os.Stat(a.path)
	if err != nil {
		return
	}
	a.mu.RLock()
	same := fi.ModTime().Equal(a.mtime) && a.st != nil
	a.mu.RUnlock()
	if same {
		return
	}
	b, err := os.ReadFile(a.path)
	if err != nil {
		return
	}
	var st authState
	if json.Unmarshal(b, &st) != nil {
		return
	}
	if st.Agents == nil {
		st.Agents = map[string]agentRec{}
	}
	a.mu.Lock()
	a.st, a.mtime = &st, fi.ModTime()
	a.mu.Unlock()
}

func sha(s string) string { h := sha256.Sum256([]byte(s)); return hex.EncodeToString(h[:]) }

// check -> (agent, identity, http status). identity: token | key | open.
func (a *auth) check(tok string) (*agentRec, string, int) {
	a.mu.RLock()
	st := a.st
	a.mu.RUnlock()
	if st == nil {
		return nil, "", http.StatusServiceUnavailable // the dashboard has not published credentials yet: agents retry
	}
	if tok != "" {
		if ag, ok := st.Agents[sha(tok)]; ok {
			return &ag, "token", 200
		}
		if strings.HasPrefix(tok, "wo_") { // an agent token that is unknown or revoked never falls back to the shared key
			return nil, "", http.StatusUnauthorized
		}
	}
	if st.KeySHA256 == "" {
		if st.Open {
			return nil, "open", 200
		}
		return nil, "", http.StatusUnauthorized
	}
	if subtle.ConstantTimeCompare([]byte(sha(tok)), []byte(st.KeySHA256)) == 1 {
		return nil, "key", 200
	}
	return nil, "", http.StatusUnauthorized
}

// ---------------------------------------------------------------- spool writer (one goroutine owns the file)

type record struct {
	header []byte
	body   []byte
	done   chan error
}

type spool struct {
	cfg      config
	in       chan record
	f        *os.File
	name     string
	size     int64
	opened   time.Time
	backlog  atomic.Int64 // bytes in closed, unprocessed segments (refreshed from the directory)
	segments atomic.Int64
	written  atomic.Int64
	records  atomic.Int64
}

func newSpool(cfg config) (*spool, error) {
	if err := os.MkdirAll(cfg.spool, 0o750); err != nil {
		return nil, err
	}
	matches, _ := filepath.Glob(filepath.Join(cfg.spool, "seg-*.tmp")) // a crash left an open segment: close it, the reader tolerates a torn tail
	for _, m := range matches {
		_ = os.Rename(m, strings.TrimSuffix(m, ".tmp")+".wo")
	}
	s := &spool{cfg: cfg, in: make(chan record, 4096)}
	s.scan()
	return s, nil
}

func (s *spool) scan() {
	var n, b int64
	matches, _ := filepath.Glob(filepath.Join(s.cfg.spool, "seg-*.wo"))
	for _, m := range matches {
		if fi, err := os.Stat(m); err == nil {
			n++
			b += fi.Size()
		}
	}
	s.segments.Store(n)
	s.backlog.Store(b)
}

func (s *spool) rotate() {
	if s.f == nil {
		return
	}
	if s.cfg.fsync {
		_ = s.f.Sync()
	}
	_ = s.f.Close()
	_ = os.Rename(s.name, strings.TrimSuffix(s.name, ".tmp")+".wo")
	s.backlog.Add(s.size)
	s.segments.Add(1)
	s.f, s.name, s.size = nil, "", 0
}

func (s *spool) run(ctx context.Context) {
	tick := time.NewTicker(s.cfg.segEvery / 4)
	scan := time.NewTicker(2 * time.Second)
	defer tick.Stop()
	defer scan.Stop()
	for {
		select {
		case <-ctx.Done():
			for { // drain what is already queued, then close the segment
				select {
				case r := <-s.in:
					r.done <- s.write(r)
				default:
					s.rotate()
					return
				}
			}
		case r := <-s.in:
			r.done <- s.write(r)
		case <-tick.C:
			if s.f != nil && time.Since(s.opened) >= s.cfg.segEvery {
				s.rotate()
			}
		case <-scan.C:
			s.scan()
		}
	}
}

func (s *spool) write(r record) error {
	if s.f == nil {
		s.name = filepath.Join(s.cfg.spool, fmt.Sprintf("seg-%019d.tmp", time.Now().UnixNano()))
		f, err := os.OpenFile(s.name, os.O_CREATE|os.O_WRONLY|os.O_EXCL, 0o640)
		if err != nil {
			return err
		}
		s.f, s.opened, s.size = f, time.Now(), 0
	}
	buf := make([]byte, 0, len(r.header)+len(r.body)+2)
	buf = append(append(append(append(buf, r.header...), '\n'), r.body...), '\n')
	n, err := s.f.Write(buf)
	s.size += int64(n)
	s.written.Add(int64(n))
	s.records.Add(1)
	if err != nil {
		return err
	}
	if s.size >= s.cfg.segBytes {
		s.rotate()
	}
	return nil
}

// ---------------------------------------------------------------- HTTP

type server struct {
	cfg   config
	auth  *auth
	sp    *spool
	proxy *httputil.ReverseProxy
	start time.Time
	stats struct{ batches, bytes, rejected, busy, unauth atomic.Int64 }
}

func (s *server) json(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func token(r *http.Request) string {
	if k := r.Header.Get("X-API-Key"); k != "" {
		return k
	}
	if a := r.Header.Get("Authorization"); strings.HasPrefix(a, "Bearer ") {
		return a[7:]
	}
	return ""
}

func clientIP(r *http.Request) string {
	h, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return h
}

func (s *server) ingest(w http.ResponseWriter, r *http.Request) {
	ag, identity, code := s.auth.check(token(r))
	if code != 200 {
		if code == http.StatusServiceUnavailable {
			w.Header().Set("Retry-After", "5")
			s.json(w, code, map[string]string{"error": "receiver starting: retry"})
			return
		}
		s.stats.unauth.Add(1)
		s.json(w, code, map[string]string{"error": "invalid or revoked token"})
		return
	}
	if s.sp.backlog.Load() > s.cfg.maxBacklog {
		s.stats.busy.Add(1)
		w.Header().Set("Retry-After", "10")
		s.json(w, http.StatusServiceUnavailable, map[string]string{"error": "backlog full: keep the batch and retry"})
		return
	}
	if r.ContentLength > s.cfg.maxBody {
		s.json(w, http.StatusRequestEntityTooLarge, map[string]string{"error": fmt.Sprintf("body larger than %d MB: send smaller batches", s.cfg.maxBody>>20)})
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, s.cfg.maxBody+1))
	if err != nil {
		s.json(w, http.StatusBadRequest, map[string]string{"error": "read failed"})
		return
	}
	if int64(len(body)) > s.cfg.maxBody {
		s.json(w, http.StatusRequestEntityTooLarge, map[string]string{"error": fmt.Sprintf("body larger than %d MB: send smaller batches", s.cfg.maxBody>>20)})
		return
	}
	if len(body) == 0 {
		s.json(w, http.StatusBadRequest, map[string]string{"error": "empty body"})
		return
	}
	name := filepath.Base(r.Header.Get("X-File-Name"))
	if name == "" || name == "." || name == "/" {
		name = "agent.log"
	}
	if len(name) > 200 {
		name = name[:200]
	}
	h := map[string]any{"v": 1, "path": r.URL.Path, "file": name, "ip": clientIP(r), "ts": time.Now().UTC().Format(time.RFC3339Nano), "len": len(body), "identity": identity}
	agentName := r.Header.Get("X-Agent")
	if agentName == "" {
		agentName = clientIP(r)
	}
	if ag != nil {
		h["agent"], h["agent_id"], h["env"], h["site"] = ag.Name, ag.ID, ag.Env, ag.Site
		agentName = ag.Name
	} else {
		if len(agentName) > 120 {
			agentName = agentName[:120]
		}
		h["agent"] = agentName
	}
	hb, _ := json.Marshal(h)
	rec := record{header: hb, body: body, done: make(chan error, 1)}
	select {
	case s.sp.in <- rec:
	case <-time.After(2 * time.Second):
		s.stats.busy.Add(1)
		w.Header().Set("Retry-After", "2")
		s.json(w, http.StatusServiceUnavailable, map[string]string{"error": "receiver busy: retry"})
		return
	}
	if err := <-rec.done; err != nil {
		s.stats.rejected.Add(1)
		w.Header().Set("Retry-After", "10")
		s.json(w, http.StatusServiceUnavailable, map[string]string{"error": "spool write failed: " + err.Error()})
		return
	}
	s.stats.batches.Add(1)
	s.stats.bytes.Add(int64(len(body)))
	lines := strings.Count(string(body), "\n")
	if lines == 0 || body[len(body)-1] != '\n' {
		lines++
	}
	s.json(w, 200, map[string]any{"accepted": lines, "agent": agentName, "identity": identity, "queued": true})
}

func (s *server) health() map[string]any {
	s.auth.mu.RLock()
	st := s.auth.st
	s.auth.mu.RUnlock()
	agents, ready := 0, st != nil
	if st != nil {
		agents = len(st.Agents)
	}
	return map[string]any{"status": "ok", "engine": "go", "uptime_s": int(time.Since(s.start).Seconds()), "auth_ready": ready, "agents": agents,
		"batches": s.stats.batches.Load(), "bytes": s.stats.bytes.Load(), "unauthorized": s.stats.unauth.Load(), "busy": s.stats.busy.Load(),
		"write_errors": s.stats.rejected.Load(), "backlog_bytes": s.sp.backlog.Load(), "backlog_segments": s.sp.segments.Load(),
		"max_backlog_bytes": s.cfg.maxBacklog, "queue": len(s.sp.in)}
}

func (s *server) routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) { s.json(w, 200, s.health()) })
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		for k, v := range s.health() {
			switch x := v.(type) {
			case int64:
				fmt.Fprintf(w, "watchover_receiver_%s %d\n", k, x)
			case int:
				fmt.Fprintf(w, "watchover_receiver_%s %d\n", k, x)
			}
		}
	})
	mux.HandleFunc("/ingest", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			s.json(w, http.StatusMethodNotAllowed, map[string]string{"error": "POST only"})
			return
		}
		s.ingest(w, r)
	})
	mux.HandleFunc("/enroll", func(w http.ResponseWriter, r *http.Request) { s.proxy.ServeHTTP(w, r) })
	files := map[string]string{"/agent.py": "agent.py", "/agent/install.sh": "agent-install.sh"}
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if f, ok := files[r.URL.Path]; ok && r.Method == http.MethodGet {
			b, err := os.ReadFile(filepath.Join(s.cfg.agentDir, f))
			if err == nil {
				w.Header().Set("Content-Type", "text/plain; charset=utf-8")
				_, _ = w.Write(b)
				return
			}
		}
		s.json(w, http.StatusNotFound, map[string]string{"error": "not found"})
	})
	return mux
}

func main() {
	cfg := loadConfig()
	sp, err := newSpool(cfg)
	if err != nil {
		log.Fatalf("spool: %v", err)
	}
	up, err := url.Parse(cfg.upstream)
	if err != nil {
		log.Fatalf("upstream: %v", err)
	}
	a := &auth{path: cfg.authFile}
	a.reload()
	s := &server{cfg: cfg, auth: a, sp: sp, proxy: httputil.NewSingleHostReverseProxy(up), start: time.Now()}
	s.proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		w.Header().Set("Retry-After", "5")
		s.json(w, http.StatusBadGateway, map[string]string{"error": "dashboard not reachable for enrolment: retry"})
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go func() {
		t := time.NewTicker(2 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				a.reload()
			}
		}
	}()
	wctx, wcancel := context.WithCancel(context.Background())
	wdone := make(chan struct{})
	go func() { sp.run(wctx); close(wdone) }()
	srv := &http.Server{Addr: cfg.listen, Handler: s.routes(), ReadHeaderTimeout: 10 * time.Second, ReadTimeout: 60 * time.Second,
		WriteTimeout: 30 * time.Second, IdleTimeout: 120 * time.Second, MaxHeaderBytes: 64 << 10}
	go func() {
		<-ctx.Done()
		sh, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(sh)
	}()
	log.Printf("watchover receiver (go) on %s, spool %s, upstream %s", cfg.listen, cfg.spool, cfg.upstream)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("listen: %v", err)
	}
	wcancel()
	<-wdone
}
