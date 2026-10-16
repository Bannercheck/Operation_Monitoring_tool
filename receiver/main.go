// Watchover receiver: the :8600 front door for agents, written in Go so that accepting data never waits on parsing.
//
// Every POST /ingest is authenticated, framed and appended to a spool segment on disk, and answered as soon as the bytes are
// in the file (milliseconds, whatever the load). The dashboard (Python) reads closed segments in the background, parses them and
// feeds the live store, history, anomaly engine and traces. A slow scan or a parse spike delays processing by seconds; it never
// stalls an agent's request, and agents never time out or pile up retries.
//
//	POST /ingest              agent batches (X-API-Key / Bearer token, X-File-Name, X-Agent)       -> spooled, 200 {"queued": true}
//	POST /enroll              agent self-enrolment                                                 -> proxied to the dashboard
//	POST /rum, /mobile        browser / mobile sensor batches (?k=<app key>, CORS, origin check)   -> spooled, 202
//	GET  /rum.js              the browser sensor
//	GET  /agent.py, /agent/install.sh                                                             -> the collector itself
//	GET  /agent/update, /agent/boot.sh, /agent/ca.crt  self-update decision, verified bootstrap, edge CA (agentupdate.go)
//	GET  /health, /metrics    counters, backlog, auth state (JSON / Prometheus text)
//	GET/POST /agent/v1/…      action-agent job channel: actuator tokens (wa_, auth.json "actuators") only -> proxied to the dashboard
//	POST /v1/logs|traces|metrics  OpenTelemetry OTLP/HTTP (also :4318; OTLP/gRPC on :4317)          -> converted in Go, spooled (otlp_http.go)
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
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

type config struct {
	listen, spool, authFile, upstream, agentDir string
	agentCA                                     string // WO_AGENT_CA: the https edge's CA served at /agent/ca.crt (comma separated candidates)
	maxBody, segBytes, maxBacklog               int64
	segEvery                                    time.Duration
	fsync                                       bool
	// parse stage (parsestage.go): WATCHOVER_GO_PARSE, WO_PARSE_WORKERS, WO_PARSE_BATCH_TIMEOUT_MS, WATCHOVER_GO_PARSE_FORMATS,
	// WATCHOVER_GO_PARSE_DENY, WO_PROFILES, WO_SHADOW_MAX_MB. The zero value is off (tests build config literals).
	parse                parseMode
	parseWorkers         int
	parseTimeout         time.Duration
	parseOnly, parseDeny []string
	profiles             string
	shadowMax            int64
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
		upstream: env("WO_UPSTREAM", "http://dashboard:8601"), agentDir: env("WO_AGENT_DIR", "/app/agent"), agentCA: agentCAs(),
		maxBody: envInt("WO_MAX_BODY_MB", 16) << 20, segBytes: envInt("WO_SEGMENT_MB", 8) << 20, maxBacklog: envInt("WO_MAX_BACKLOG_MB", 2048) << 20,
		segEvery: time.Duration(envInt("WO_SEGMENT_MS", 1000)) * time.Millisecond, fsync: env("WO_FSYNC", "rotate") != "off",
		parse: parseModeOf(os.Getenv("WATCHOVER_GO_PARSE")), parseWorkers: int(envInt("WO_PARSE_WORKERS", 0)),
		parseTimeout: time.Duration(envInt("WO_PARSE_BATCH_TIMEOUT_MS", 2000)) * time.Millisecond,
		parseOnly:    csvList(os.Getenv("WATCHOVER_GO_PARSE_FORMATS")), parseDeny: csvList(os.Getenv("WATCHOVER_GO_PARSE_DENY")),
		profiles: env("WO_PROFILES", filepath.Join(spool, "profiles.json")), shadowMax: envInt("WO_SHADOW_MAX_MB", 256) << 20,
	}
}

// agentCAs: WO_AGENT_CA, plus WO_AGENT_CA_INTERNAL (the install's own edge CA) only while the edge uses it (WATCHOVER_EDGE_TLS=internal):
// a company certificate must never be paired with a stale internal root the installer would then pin.
func agentCAs() string {
	ca := os.Getenv("WO_AGENT_CA")
	if in := os.Getenv("WO_AGENT_CA_INTERNAL"); in != "" && env("WATCHOVER_EDGE_TLS", "internal") == "internal" {
		ca += "," + in
	}
	return ca
}

// ---------------------------------------------------------------- authentication (auth.json written by the dashboard)

type agentRec struct {
	ID   int64  `json:"id"`
	Name string `json:"name"`
	Env  string `json:"env"`
	Site string `json:"site"`
}

type appRec struct {
	Name    string   `json:"name"`
	Origins []string `json:"origins"`
}

// actRec is an action-agent actuator (docs/design/action-agent.md §9.3.2): its wa_ token reaches /agent/v1/ and nothing else.
type actRec struct {
	AgentID int64  `json:"agent_id"`
	Host    string `json:"host"`
}

type authState struct {
	Apps      map[string]appRec   `json:"apps"`       // RUM / mobile app key -> app (keys are public; origins bind browser posts)
	Agents    map[string]agentRec `json:"agents"`     // sha256(token) -> agent
	Actuators map[string]actRec   `json:"actuators"`  // sha256(wa_ token) -> actuator (job channel only)
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
	if st.Apps == nil {
		st.Apps = map[string]appRec{}
	}
	if st.Actuators == nil {
		st.Actuators = map[string]actRec{}
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
		if strings.HasPrefix(tok, "wo_") || strings.HasPrefix(tok, "wa_") { // an agent token that is unknown or revoked never falls
			return nil, "", http.StatusUnauthorized // back to the shared key; an actuator token is for /agent/v1/ only
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

// checkActuator: the "actuators" map only; never open mode, the shared key or a collector token (C1, R4).
func (a *auth) checkActuator(tok string) (*actRec, int) {
	a.mu.RLock()
	st := a.st
	a.mu.RUnlock()
	if st == nil {
		return nil, http.StatusServiceUnavailable
	}
	if tok == "" || !strings.HasPrefix(tok, "wa_") {
		return nil, http.StatusUnauthorized
	}
	if ac, ok := st.Actuators[sha(tok)]; ok {
		return &ac, 200
	}
	return nil, http.StatusUnauthorized
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
	// parse stage: raw segments not parsed yet (seg-*.raw) and parsed segments not consumed yet (pseg-*.wo)
	rawBacklog, rawSegments, psegBacklog atomic.Int64
	onClose                              func() // wakes the parse stage
}

func newSpool(cfg config) (*spool, error) {
	if err := os.MkdirAll(cfg.spool, 0o750); err != nil {
		return nil, err
	}
	s := &spool{cfg: cfg, in: make(chan record, 4096)}
	matches, _ := filepath.Glob(filepath.Join(cfg.spool, "seg-*.tmp")) // a crash left an open segment: close it, the reader tolerates a torn tail
	for _, m := range matches {
		_ = os.Rename(m, strings.TrimSuffix(m, ".tmp")+s.closedExt())
	}
	recoverParseSpool(cfg.spool, cfg.parse)
	s.scan()
	return s, nil
}

// closedExt: a closed segment is .wo (the dashboard parses it) or, with the parse stage on, .raw (the stage parses it).
func (s *spool) closedExt() string {
	if s.cfg.parse == parseOn {
		return rawExt
	}
	return ".wo"
}

// scan refreshes the backlog: every closed segment not yet consumed, raw or parsed (the 503 threshold covers both).
func (s *spool) scan() {
	var n, b int64
	size := func(glob string) (int64, int64) {
		var c, t int64
		matches, _ := filepath.Glob(filepath.Join(s.cfg.spool, glob))
		for _, m := range matches {
			if fi, err := os.Stat(m); err == nil {
				c++
				t += fi.Size()
			}
		}
		return c, t
	}
	n, b = size("seg-*.wo")
	rc, rb := size("seg-*" + rawExt)
	pc, pb := size("pseg-*.wo")
	s.rawSegments.Store(rc)
	s.rawBacklog.Store(rb)
	s.psegBacklog.Store(pb)
	s.segments.Store(n + rc + pc)
	s.backlog.Store(b + rb + pb)
}

func (s *spool) rotate() {
	if s.f == nil {
		return
	}
	if s.cfg.fsync {
		_ = s.f.Sync()
	}
	_ = s.f.Close()
	base := strings.TrimSuffix(s.name, ".tmp")
	if s.cfg.parse == parseShadow { // the shadow stage gets its own name for the segment before the dashboard can delete it
		if os.Link(s.name, base+shadowExt) != nil {
			if b, err := os.ReadFile(s.name); err == nil {
				_ = os.WriteFile(base+shadowExt, b, 0o640)
			}
		}
	}
	_ = os.Rename(s.name, base+s.closedExt())
	s.backlog.Add(s.size)
	s.segments.Add(1)
	if s.cfg.parse == parseOn {
		s.rawBacklog.Add(s.size)
		s.rawSegments.Add(1)
	}
	s.f, s.name, s.size = nil, "", 0
	if s.onClose != nil {
		s.onClose()
	}
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
	cfg      config
	auth     *auth
	sp       *spool
	stage    *parseStage // nil when WATCHOVER_GO_PARSE is off
	proxy    *httputil.ReverseProxy
	jobProxy *httputil.ReverseProxy // /agent/v1/ (built from proxy's upstream when nil)
	start    time.Time
	stats    struct{ batches, bytes, rejected, busy, unauth, sensor atomic.Int64 }
	otlp     *otlpIn   // OpenTelemetry intake (otlp_http.go)
	rel      *releases // agent self-update: the dashboard's export, the served files (agentupdate.go)
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
	agentHeaders(h, r)
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
	s.hint(w, r, ag)
	s.json(w, 200, map[string]any{"accepted": lines, "agent": agentName, "identity": identity, "queued": true})
}

const rumMaxBody = 512 << 10

func originAllowed(app appRec, origin string) bool {
	if origin == "" || len(app.Origins) == 0 {
		return true // mobile SDKs send no Origin; an app without a list accepts any
	}
	o := strings.TrimRight(origin, "/")
	for _, a := range app.Origins {
		if a == "*" || a == o || (strings.HasPrefix(a, "*.") && strings.HasSuffix(o, a[1:])) {
			return true
		}
	}
	return false
}

func cors(w http.ResponseWriter, origin string) {
	if origin != "" {
		w.Header().Set("Access-Control-Allow-Origin", origin)
		w.Header().Add("Vary", "Origin")
	} else {
		w.Header().Set("Access-Control-Allow-Origin", "*")
	}
}

// rum: browser (/rum) and mobile (/mobile) sensor batches. The app key comes in ?k= (sendBeacon cannot set headers) or
// X-Watchover-Key; browser posts must come from one of the app's origins. Spooled like agent batches, parsed by the dashboard.
func (s *server) rum(w http.ResponseWriter, r *http.Request) {
	origin := r.Header.Get("Origin")
	cors(w, origin)
	if r.Method == http.MethodOptions {
		w.Header().Set("Access-Control-Allow-Methods", "POST, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, X-Watchover-Key")
		w.Header().Set("Access-Control-Max-Age", "600")
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if r.Method != http.MethodPost {
		s.json(w, http.StatusMethodNotAllowed, map[string]string{"error": "POST only"})
		return
	}
	key := r.Header.Get("X-Watchover-Key")
	if key == "" {
		key = r.URL.Query().Get("k")
	}
	s.auth.mu.RLock()
	st := s.auth.st
	s.auth.mu.RUnlock()
	if st == nil {
		w.Header().Set("Retry-After", "5")
		s.json(w, http.StatusServiceUnavailable, map[string]string{"error": "receiver starting: retry"})
		return
	}
	app, ok := st.Apps[key]
	if !ok {
		s.stats.unauth.Add(1)
		s.json(w, http.StatusUnauthorized, map[string]string{"error": "unknown or disabled app key"})
		return
	}
	if !originAllowed(app, origin) {
		s.stats.unauth.Add(1)
		s.json(w, http.StatusForbidden, map[string]string{"error": "origin not allowed for this app"})
		return
	}
	if s.sp.backlog.Load() > s.cfg.maxBacklog {
		s.stats.busy.Add(1)
		w.Header().Set("Retry-After", "10")
		s.json(w, http.StatusServiceUnavailable, map[string]string{"error": "backlog full"})
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, rumMaxBody+1))
	if err != nil || len(body) == 0 {
		s.json(w, http.StatusBadRequest, map[string]string{"error": "empty or unreadable body"})
		return
	}
	if len(body) > rumMaxBody {
		s.json(w, http.StatusRequestEntityTooLarge, map[string]string{"error": "sensor batch too large"})
		return
	}
	ua := r.Header.Get("User-Agent")
	if len(ua) > 300 {
		ua = ua[:300]
	}
	hb, _ := json.Marshal(map[string]any{"v": 1, "path": r.URL.Path, "app": app.Name, "app_key": key, "origin": origin, "ua": ua,
		"ts": time.Now().UTC().Format(time.RFC3339Nano), "len": len(body)})
	rec := record{header: hb, body: body, done: make(chan error, 1)}
	select {
	case s.sp.in <- rec:
	case <-time.After(2 * time.Second):
		s.stats.busy.Add(1)
		s.json(w, http.StatusServiceUnavailable, map[string]string{"error": "receiver busy: retry"})
		return
	}
	if err := <-rec.done; err != nil {
		s.stats.rejected.Add(1)
		s.json(w, http.StatusServiceUnavailable, map[string]string{"error": "spool write failed"})
		return
	}
	s.stats.sensor.Add(1)
	s.json(w, http.StatusAccepted, map[string]any{"ok": true, "queued": true})
}

func (s *server) health() map[string]any {
	s.auth.mu.RLock()
	st := s.auth.st
	s.auth.mu.RUnlock()
	agents, ready := 0, st != nil
	if st != nil {
		agents = len(st.Agents)
	}
	h := map[string]any{"status": "ok", "engine": "go", "uptime_s": int(time.Since(s.start).Seconds()), "auth_ready": ready, "agents": agents,
		"batches": s.stats.batches.Load(), "sensor_batches": s.stats.sensor.Load(), "bytes": s.stats.bytes.Load(), "unauthorized": s.stats.unauth.Load(), "busy": s.stats.busy.Load(),
		"write_errors": s.stats.rejected.Load(), "backlog_bytes": s.sp.backlog.Load(), "backlog_segments": s.sp.segments.Load(),
		"max_backlog_bytes": s.cfg.maxBacklog, "queue": len(s.sp.in)}
	if s.otlp != nil {
		s.otlp.health(h)
	}
	if s.stage != nil {
		s.stage.health(h)
	} else {
		h["parse_mode"], h["parse_enabled"] = parseOff.String(), 0
	}
	return h
}

func (s *server) routes() http.Handler {
	if s.rel == nil {
		s.rel = newReleases(s.cfg.spool, s.cfg.agentDir, s.cfg.agentCA)
	}
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
			case map[string]map[string]int64: // parse_by_format
				for _, f := range sortedKeys(x) {
					for _, res := range []string{"parsed", "forwarded"} {
						fmt.Fprintf(w, "watchover_receiver_parse_batches{format=%q,result=%q} %d\n", f, res, x[f][res])
					}
					fmt.Fprintf(w, "watchover_receiver_parse_observations_by_format{format=%q} %d\n", f, x[f]["obs"])
				}
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
	mux.HandleFunc("/enroll", func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(token(r), "wa_") { // an actuator token never enrols a collector
			s.stats.unauth.Add(1)
			s.json(w, http.StatusUnauthorized, map[string]string{"error": "enrolment key is wrong or disabled"})
			return
		}
		s.proxy.ServeHTTP(w, r)
	})
	jp := s.jobProxy
	if jp == nil && s.proxy != nil {
		jp = &httputil.ReverseProxy{Director: s.proxy.Director, Transport: &http.Transport{ResponseHeaderTimeout: 30 * time.Second}}
	}
	jobDown := func(w http.ResponseWriter, r *http.Request, err error) {
		w.Header().Set("Retry-After", "10")
		s.json(w, http.StatusBadGateway, map[string]string{"error": "job channel unavailable"})
	}
	if jp != nil {
		jp.ErrorHandler = jobDown
	}
	mux.HandleFunc("/agent/v1/", func(w http.ResponseWriter, r *http.Request) {
		act, code := s.auth.checkActuator(token(r))
		if code == http.StatusServiceUnavailable {
			w.Header().Set("Retry-After", "5")
			s.json(w, code, map[string]string{"error": "receiver starting: retry"})
			return
		}
		if act == nil {
			s.stats.unauth.Add(1)
			s.json(w, http.StatusUnauthorized, map[string]string{"error": "actuator token required"})
			return
		}
		if r.ContentLength > 1<<20 {
			s.json(w, http.StatusRequestEntityTooLarge, map[string]string{"error": "body too large"})
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
		if jp == nil {
			jobDown(w, r, nil)
			return
		}
		jp.ServeHTTP(w, r) // the dashboard checks the token again and every signature; this route only keeps others out
	})
	if s.otlp != nil {
		mux.HandleFunc("/v1/", s.otlp.serveHTTP) // OTLP/HTTP on the agents' port too
	}
	mux.HandleFunc("/rum", s.rum)
	mux.HandleFunc("/mobile", s.rum)
	mux.HandleFunc("/agent/update", s.agentUpdate)
	files := map[string]string{"/agent.py": "agent.py", "/agent/install.sh": "agent-install.sh", "/agent/boot.sh": "agent-boot.sh", "/rum.js": "rum.js"}
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet && r.URL.Path == "/agent/manifest.json" {
			s.json(w, http.StatusOK, s.agentManifest())
			return
		}
		if r.Method == http.MethodGet && r.URL.Path == "/agent/ca.crt" {
			if b := s.rel.ca(); b != nil {
				w.Header().Set("Content-Type", "application/x-pem-file")
				_, _ = w.Write(b)
				return
			}
		}
		if bin, ok := agentBins[r.URL.Path]; ok && r.Method == http.MethodGet && s.serveAgentFile(w, bin, "application/octet-stream") {
			return
		}
		if f, ok := files[r.URL.Path]; ok && r.Method == http.MethodGet && f != "rum.js" && s.serveAgentFile(w, f, "text/plain; charset=utf-8") {
			return
		}
		if f, ok := files[r.URL.Path]; ok && r.Method == http.MethodGet {
			b, err := os.ReadFile(filepath.Join(s.cfg.agentDir, f))
			if err == nil {
				if strings.HasSuffix(f, ".js") {
					w.Header().Set("Content-Type", "application/javascript; charset=utf-8")
					w.Header().Set("Access-Control-Allow-Origin", "*")
					w.Header().Set("Cache-Control", "public, max-age=3600")
					_, _ = w.Write(b)
					return
				}
				w.Header().Set("Content-Type", "text/plain; charset=utf-8")
				_, _ = w.Write(b)
				return
			}
		}
		s.json(w, http.StatusNotFound, map[string]string{"error": "not found"})
	})
	return mux
}

func sortedKeys[V any](m map[string]V) []string {
	ks := make([]string, 0, len(m))
	for k := range m {
		ks = append(ks, k)
	}
	sort.Strings(ks)
	return ks
}

// parseSpool is `wo-receiver parse-spool DIR`: parse every seg-*.raw in DIR into pseg-*.wo once and exit (with
// WATCHOVER_GO_PARSE=shadow: seg-*.shd into shadow-*.wo). Used by tests and for debugging a spool by hand.
func parseSpool(dir string) {
	cfg := loadConfig()
	cfg.spool = dir
	if cfg.parse != parseShadow { // shadow: seg-*.shd -> shadow-*.wo; otherwise seg-*.raw -> pseg-*.wo
		cfg.parse = parseOn
	}
	if os.Getenv("WO_PROFILES") == "" {
		cfg.profiles = filepath.Join(dir, "profiles.json")
	}
	recoverParseSpool(dir, cfg.parse)
	st := newParseStage(cfg, nil)
	n := st.drainOnce(context.Background())
	h := map[string]any{"records": n}
	st.health(h)
	b, _ := json.Marshal(h)
	fmt.Println(string(b))
}

func main() {
	if len(os.Args) == 3 && os.Args[1] == "parse-spool" {
		parseSpool(os.Args[2])
		return
	}
	applyContainerLimits("/sys/fs/cgroup")
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
	s.otlp = newOTLP(s)
	s.rel = newReleases(cfg.spool, cfg.agentDir, cfg.agentCA)
	s.rel.exportCA()
	if cfg.parse != parseOff {
		s.stage = newParseStage(cfg, sp)
		s.stage.needExport = true // parse only once the dashboard has exported profiles.json (see parseStage.needExport)
		sp.onClose = s.stage.wake
	}
	s.proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		w.Header().Set("Retry-After", "5")
		s.json(w, http.StatusBadGateway, map[string]string{"error": "dashboard not reachable for enrolment: retry"})
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go func() {
		t := time.NewTicker(2 * time.Second)
		defer t.Stop()
		for n := 1; ; n++ {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				a.reload()
				if n%30 == 0 {
					s.rel.exportCA() // a renewed edge CA reaches the dashboard's install command within a minute
				}
			}
		}
	}()
	wctx, wcancel := context.WithCancel(context.Background())
	wdone := make(chan struct{})
	go func() { sp.run(wctx); close(wdone) }()
	pdone := make(chan struct{})
	if s.stage != nil {
		go func() { s.stage.run(wctx); close(pdone) }()
	} else {
		close(pdone)
	}
	otlpSrv := s.otlp.listen(env("WO_OTLP_HTTP", ":4318"), env("WO_OTLP_GRPC", ":4317"))
	srv := &http.Server{Addr: cfg.listen, Handler: s.routes(), ReadHeaderTimeout: 10 * time.Second, ReadTimeout: 60 * time.Second,
		WriteTimeout: 30 * time.Second, IdleTimeout: 120 * time.Second, MaxHeaderBytes: 64 << 10}
	go func() {
		<-ctx.Done()
		sh, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(sh)
		for _, x := range otlpSrv {
			_ = x.Shutdown(sh)
		}
	}()
	log.Printf("watchover receiver (go) on %s, spool %s, upstream %s, go parse %s", cfg.listen, cfg.spool, cfg.upstream, cfg.parse)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("listen: %v", err)
	}
	wcancel()
	<-wdone
	<-pdone
}

// agentBins is the Go host agent (goagent/, wo-agent: log shipping and the action agent's actuator) built into this image for Linux.
var agentBins = map[string]string{"/agent/wo-agent-linux-amd64": "wo-agent-linux-amd64", "/agent/wo-agent-linux-arm64": "wo-agent-linux-arm64"}
