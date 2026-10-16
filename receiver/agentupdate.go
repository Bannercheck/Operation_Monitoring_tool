package main

// Agent self-update and secure install on the agents' port (docs/KURULUM_DOCKER.md, "Ajan güncellemesi"):
//
//	GET /agent/update        an agent (its wo_ token) asks whether to update: keyd's signed release, the dashboard's switch,
//	                         the agent's pause, the staged rollout bucket, and a signed host-bound pin ("bu sürüme döndür")
//	GET /agent/manifest.json sha256 / size of what is served, plus the signed release (release_body / release_sig / release_key)
//	GET /agent/boot.sh       the verified bootstrap of the one-line install command (its sha256 is in the command)
//	GET /agent/ca.crt        the CA of the https edge (WO_AGENT_CA files, or WO_AGENT_CA_PEM), fetched by boot.sh and accepted
//	                         by its pinned sha256
//
// The receiver holds no key and decides nothing about trust: agent-update.json is written by the dashboard (signatures made by
// keyd), and the files keyd hashed are copied by the dashboard into <spool>/release/, which are served in preference to this
// image's own copies (a rebuilt receiver image can then never disagree with the signed manifest).

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

type signedDoc struct {
	Body    string `json:"body"`
	Sig     string `json:"sig"`
	Key     string `json:"key,omitempty"`
	Fp      string `json:"fp,omitempty"`
	Version string `json:"version,omitempty"`
}

// updExport is <spool>/agent-update.json (src/watchover/agent_update.py AgentUpdates.export).
type updExport struct {
	Release *signedDoc           `json:"release"`
	Enabled bool                 `json:"enabled"`
	Pct     int                  `json:"pct"`
	Paused  []int64              `json:"paused"`
	Pins    map[string]signedDoc `json:"pins"`
}

type fileSum struct {
	size  int64
	mtime time.Time
	sha   string
}

type releases struct {
	spool, agentDir string
	caPaths         []string
	caPEM           []byte // WO_AGENT_CA_PEM (OpenShift: the ingest Route's CA from the chart values)
	mu              sync.Mutex
	checked, mtime  time.Time
	doc             *updExport
	sums            map[string]fileSum
	caSHA           string
}

func newReleases(spool, agentDir, ca string) *releases {
	r := &releases{spool: spool, agentDir: agentDir, sums: map[string]fileSum{}}
	if pem := os.Getenv("WO_AGENT_CA_PEM"); strings.Contains(pem, "BEGIN CERTIFICATE") {
		r.caPEM = []byte(strings.TrimSpace(pem) + "\n")
	}
	for _, p := range strings.Split(ca, ",") {
		if p = strings.TrimSpace(p); p != "" {
			r.caPaths = append(r.caPaths, p)
		}
	}
	return r
}

// load re-reads agent-update.json when it changed (looked at most every 2 s).
func (r *releases) load() *updExport {
	r.mu.Lock()
	defer r.mu.Unlock()
	if time.Since(r.checked) < 2*time.Second && !r.checked.IsZero() {
		return r.doc
	}
	r.checked = time.Now()
	p := filepath.Join(r.spool, "agent-update.json")
	fi, err := os.Stat(p)
	if err != nil {
		r.doc, r.mtime = nil, time.Time{}
		return nil
	}
	if fi.ModTime().Equal(r.mtime) && r.doc != nil {
		return r.doc
	}
	b, err := os.ReadFile(p)
	var d updExport
	if err != nil || json.Unmarshal(b, &d) != nil {
		return r.doc // a torn read: keep the previous one
	}
	r.doc, r.mtime = &d, fi.ModTime()
	return r.doc
}

// path of a served agent file: the dashboard's copy of what keyd signed, else this image's own.
func (r *releases) path(name string) string {
	if p := filepath.Join(r.spool, "release", name); fileExists(p) {
		return p
	}
	return filepath.Join(r.agentDir, name)
}

func fileExists(p string) bool {
	fi, err := os.Stat(p)
	return err == nil && fi.Mode().IsRegular()
}

// sum is sha256 / size of a served file, cached by size and mtime (the manifest is asked for by every installer and update).
func (r *releases) sum(name string) (string, int64, bool) {
	p := r.path(name)
	fi, err := os.Stat(p)
	if err != nil || !fi.Mode().IsRegular() {
		return "", 0, false
	}
	r.mu.Lock()
	c, ok := r.sums[p]
	r.mu.Unlock()
	if ok && c.size == fi.Size() && c.mtime.Equal(fi.ModTime()) {
		return c.sha, c.size, true
	}
	b, err := os.ReadFile(p)
	if err != nil {
		return "", 0, false
	}
	h := sha256.Sum256(b)
	c = fileSum{size: int64(len(b)), mtime: fi.ModTime(), sha: hex.EncodeToString(h[:])}
	r.mu.Lock()
	r.sums[p] = c
	r.mu.Unlock()
	return c.sha, c.size, true
}

// bucket places an agent in the staged rollout of a version (0..99); the dashboard computes the same number.
func bucket(agentID int64, version string) int {
	h := sha256.Sum256([]byte(strconv.FormatInt(agentID, 10) + ":" + version))
	return int(binary.BigEndian.Uint32(h[:4]) % 100)
}

// newer: semver-ish numeric compare (a pre-release suffix sorts before the release), as goagent/internal/update.Compare.
func newer(a, b string) bool {
	pa, sa := vsplit(a)
	pb, sb := vsplit(b)
	for i := 0; i < 3; i++ {
		if pa[i] != pb[i] {
			return pa[i] > pb[i]
		}
	}
	if sa == sb {
		return false
	}
	return sa == "" || (sb != "" && sa > sb)
}

func vsplit(v string) ([3]int, string) {
	var n [3]int
	core, suf := strings.TrimSpace(v), ""
	if i := strings.IndexAny(core, "-+"); i >= 0 {
		core, suf = core[:i], core[i:]
	}
	for i, p := range strings.SplitN(core, ".", 3) {
		n[i], _ = strconv.Atoi(p)
	}
	return n, suf
}

type decision struct {
	Agent   string     `json:"agent,omitempty"`
	Release *signedDoc `json:"release"`
	Update  bool       `json:"update"`
	Reason  string     `json:"reason"`
	Version string     `json:"version,omitempty"`
	Pin     *signedDoc `json:"pin,omitempty"`
}

// decide applies the dashboard's policy for one agent. agent.py (any version) is offered the release: its update is the
// migration to wo-agent.
func (r *releases) decide(agentID int64, engine, version string) decision {
	d := r.load()
	if d == nil || d.Release == nil {
		return decision{Reason: "unsigned"}
	}
	out := decision{Release: d.Release, Version: d.Release.Version}
	if p, ok := d.Pins[strconv.FormatInt(agentID, 10)]; ok {
		pin := p
		out.Pin, out.Version, out.Reason = &pin, p.Version, "pinned"
		out.Update = engine == "agent.py" || version != p.Version
		return out
	}
	for _, id := range d.Paused {
		if id == agentID {
			out.Reason = "paused"
			return out
		}
	}
	switch {
	case !d.Enabled:
		out.Reason = "disabled"
	case bucket(agentID, d.Release.Version) >= max(0, min(d.Pct, 100)):
		out.Reason = "rollout"
	case engine == "agent.py":
		out.Update, out.Reason = true, "migrate"
	case newer(d.Release.Version, version):
		out.Update, out.Reason = true, "newer"
	default:
		out.Reason = "current"
	}
	return out
}

// ca is the edge CA (PEM) agents and the installer trust for https, the first readable WO_AGENT_CA candidate.
func (r *releases) ca() []byte {
	if r.caPEM != nil {
		return r.caPEM
	}
	for _, p := range r.caPaths {
		if b, err := os.ReadFile(p); err == nil && strings.Contains(string(b), "BEGIN CERTIFICATE") {
			return b
		}
	}
	return nil
}

// exportCA copies the served CA into the spool (agent-ca.pem) for the dashboard, which shows its sha256 in the install
// command; removed when there is none.
func (r *releases) exportCA() {
	b := r.ca()
	out := filepath.Join(r.spool, "agent-ca.pem")
	h := sha256.Sum256(b)
	sha := hex.EncodeToString(h[:])
	if b == nil {
		sha = ""
	}
	r.mu.Lock()
	same := sha == r.caSHA
	r.caSHA = sha
	r.mu.Unlock()
	if same && (b == nil) != fileExists(out) {
		return
	}
	if b == nil {
		_ = os.Remove(out)
		return
	}
	tmp := out + ".tmp"
	if os.WriteFile(tmp, b, 0o644) == nil {
		_ = os.Rename(tmp, out)
	}
}

// agentManifest lists sha256 and size of what the installer downloads, plus the signed release: the installer and boot.sh
// verify release_sig with the pinned release key and take the sha256 of each file from release_body.
func (s *server) agentManifest() map[string]any {
	files := map[string]any{}
	for _, name := range []string{"agent.py", "agent-install.sh", "agent-boot.sh", "wo-agent-linux-amd64", "wo-agent-linux-arm64"} {
		if sha, size, ok := s.rel.sum(name); ok {
			files[name] = map[string]any{"sha256": sha, "size": size}
		}
	}
	out := map[string]any{"files": files}
	if d := s.rel.load(); d != nil && d.Release != nil {
		out["release_body"], out["release_sig"], out["release_key"] = d.Release.Body, d.Release.Sig, d.Release.Key
		out["release_fp"], out["release_version"] = d.Release.Fp, d.Release.Version
	}
	return out
}

func (s *server) agentUpdate(w http.ResponseWriter, r *http.Request) {
	ag, _, code := s.auth.check(token(r))
	if code == http.StatusServiceUnavailable {
		w.Header().Set("Retry-After", "5")
		s.json(w, code, map[string]string{"error": "receiver starting: retry"})
		return
	}
	if ag == nil {
		s.stats.unauth.Add(1)
		s.json(w, http.StatusUnauthorized, map[string]string{"error": "an agent token is required"})
		return
	}
	d := s.rel.decide(ag.ID, engineOf(r), r.Header.Get("X-Agent-Version"))
	d.Agent = ag.Name
	s.json(w, http.StatusOK, d)
}

// engineOf is "wo-agent" or "agent.py" (X-Agent-Engine; older builds: their User-Agent).
func engineOf(r *http.Request) string {
	if e := r.Header.Get("X-Agent-Engine"); e != "" {
		return e
	}
	if strings.HasPrefix(r.Header.Get("User-Agent"), "wo-agent/") {
		return "wo-agent"
	}
	return "agent.py"
}

// hint sets X-Watchover-Update on an answer to an agent that should update now (it starts its update unit at once).
func (s *server) hint(w http.ResponseWriter, r *http.Request, ag *agentRec) {
	if ag == nil || r.Header.Get("X-Agent-Version") == "" {
		return
	}
	if d := s.rel.decide(ag.ID, engineOf(r), r.Header.Get("X-Agent-Version")); d.Update {
		w.Header().Set("X-Watchover-Update", d.Version)
	}
}

func (s *server) serveAgentFile(w http.ResponseWriter, name, ctype string) bool {
	b, err := os.ReadFile(s.rel.path(name))
	if err != nil {
		return false
	}
	w.Header().Set("Content-Type", ctype)
	w.Header().Set("Content-Length", fmt.Sprint(len(b)))
	_, _ = w.Write(b)
	return true
}

// agentHeaders are the spool record's view of the agent build: version, engine and self-update state (X-Agent-*).
func agentHeaders(h map[string]any, r *http.Request) {
	clip := func(v string, n int) string {
		if len(v) > n {
			return v[:n]
		}
		return v
	}
	if v := r.Header.Get("X-Agent-Version"); v != "" {
		h["ver"] = clip(v, 40)
		h["eng"] = clip(engineOf(r), 20)
	}
	if v := r.Header.Get("X-Agent-Update"); v != "" {
		h["upd"] = clip(v, 300)
	}
}
