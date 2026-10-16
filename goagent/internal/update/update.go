package update

import (
	"context"
	"crypto/ed25519"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

// Phases recorded in state.json (and reported by the ship role as X-Agent-Update).
const (
	PhaseCurrent    = "current"     // nothing to do (or the receiver said no: switch off, paused, rollout, unsigned)
	PhasePending    = "pending"     // the new binary is in place and the services restart; the gate decides
	PhaseUpdated    = "updated"     // the new binary reported back (hello) within the gate
	PhaseFailed     = "failed"      // refused before anything changed (verification, download, sanity run)
	PhaseRolledBack = "rolled_back" // the new binary did not report back: the previous one is in place again
)

// State is <dir>/state.json.
type State struct {
	Phase  string         `json:"phase"`
	From   string         `json:"from,omitempty"`
	To     string         `json:"to,omitempty"`
	Reason string         `json:"reason,omitempty"`
	Error  string         `json:"error,omitempty"`
	At     string         `json:"at"`
	Tries  map[string]int `json:"tries,omitempty"` // failed gates per target sha256: a build that failed MaxTries times is skipped
}

// MaxTries is how often one build may fail its gate before the agent stops trying it (a newer build is tried again).
const MaxTries = 3

// Hello is <dir>/hello.json, written by the ship role once it reached the receiver (its first 2xx answer).
type Hello struct {
	Version string `json:"version"`
	TS      int64  `json:"ts"` // unix milliseconds
	PID     int    `json:"pid"`
}

// Config is one update run.
type Config struct {
	Base, Token, Host, Arch, Current string
	Key                              ed25519.PublicKey
	Binary, Dir                      string
	Units                            []string      // [0] must come back with a hello (watchover-agent); the rest only restart if running
	Gate                             time.Duration // how long the new binary has to reach the receiver
	Client                           *http.Client
	Systemctl                        func(args ...string) error
	Logf                             func(string, ...any)
	Now                              func() time.Time
}

// Decision is the receiver's answer to GET /agent/update (per agent: switch, pause, staged rollout, pin).
type Decision struct {
	Agent   string  `json:"agent"`
	Release *Signed `json:"release"`
	Update  bool    `json:"update"`
	Reason  string  `json:"reason"`
	Version string  `json:"version"`
	Pin     *Signed `json:"pin"`
}

func (c *Config) logf(f string, a ...any) {
	if c.Logf != nil {
		c.Logf(f, a...)
	}
}

func (c *Config) now() time.Time {
	if c.Now != nil {
		return c.Now()
	}
	return time.Now()
}

// LoadState reads <dir>/state.json (zero State when missing).
func LoadState(dir string) State {
	var s State
	b, err := os.ReadFile(filepath.Join(dir, "state.json"))
	if err == nil {
		_ = json.Unmarshal(b, &s)
	}
	return s
}

func (c *Config) save(s State) {
	s.At = c.now().UTC().Format(time.RFC3339)
	b, _ := json.Marshal(s)
	tmp := filepath.Join(c.Dir, "state.json.tmp")
	if os.WriteFile(tmp, b, 0o600) == nil {
		_ = os.Rename(tmp, filepath.Join(c.Dir, "state.json"))
	}
}

// Header is the compact X-Agent-Update value the ship role sends: phase;to;from;at;error (ASCII, at most 300 bytes).
func (s State) Header() string {
	if s.Phase == "" {
		return ""
	}
	clean := func(v string) string {
		v = strings.Map(func(r rune) rune {
			if r < 0x20 || r > 0x7e || r == ';' {
				return ' '
			}
			return r
		}, v)
		if len(v) > 200 {
			v = v[:200]
		}
		return v
	}
	return strings.Join([]string{clean(s.Phase), clean(s.To), clean(s.From), clean(s.At), clean(s.Error)}, ";")
}

// WriteHello records that this process (version) reached the receiver; the update gate waits for it.
func WriteHello(dir, version string) error {
	b, _ := json.Marshal(Hello{Version: version, TS: time.Now().UnixMilli(), PID: os.Getpid()})
	tmp := filepath.Join(dir, fmt.Sprintf("hello.json.%d", os.Getpid()))
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, filepath.Join(dir, "hello.json"))
}

func (c *Config) get(ctx context.Context, path string, limit int64) ([]byte, int, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(c.Base, "/")+path, nil)
	if err != nil {
		return nil, 0, err
	}
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	req.Header.Set("X-Agent-Version", c.Current)
	req.Header.Set("X-Agent-Engine", "wo-agent")
	resp, err := c.Client.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil {
		return nil, resp.StatusCode, err
	}
	if int64(len(b)) > limit {
		return nil, resp.StatusCode, fmt.Errorf("%s: answer larger than %d bytes", path, limit)
	}
	return b, resp.StatusCode, nil
}

// decision asks the receiver; an older receiver without /agent/update gets the public manifest (no per-agent policy).
func (c *Config) decision(ctx context.Context) (*Decision, error) {
	q := url.Values{"arch": {c.Arch}, "version": {c.Current}}
	b, code, err := c.get(ctx, "/agent/update?"+q.Encode(), 1<<20)
	if err != nil {
		return nil, err
	}
	if code == http.StatusOK {
		var d Decision
		if err := json.Unmarshal(b, &d); err != nil {
			return nil, fmt.Errorf("/agent/update: %v", err)
		}
		return &d, nil
	}
	if code != http.StatusNotFound {
		return nil, fmt.Errorf("/agent/update answered HTTP %d", code)
	}
	b, code, err = c.get(ctx, "/agent/manifest.json", 1<<20)
	if err != nil || code != http.StatusOK {
		return nil, fmt.Errorf("/agent/manifest.json: HTTP %d %v", code, err)
	}
	var m struct {
		Body string `json:"release_body"`
		Sig  string `json:"release_sig"`
		Key  string `json:"release_key"`
	}
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, err
	}
	d := &Decision{Update: true, Reason: "manifest"}
	if m.Body != "" {
		d.Release = &Signed{Body: m.Body, Sig: m.Sig, Key: m.Key}
	}
	return d, nil
}

func lock(dir string) (func(), error) {
	f, err := os.OpenFile(filepath.Join(dir, "lock"), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return nil, errors.New("another update run holds the lock")
	}
	return func() { _ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN); f.Close() }, nil
}

// Run is one check: ask, verify, install, restart, gate, roll back. The returned State is also in state.json.
func Run(ctx context.Context, c *Config) (State, error) {
	if err := os.MkdirAll(c.Dir, 0o700); err != nil {
		return State{}, err
	}
	unlock, err := lock(c.Dir)
	if err != nil {
		return State{}, err
	}
	defer unlock()
	st := LoadState(c.Dir)
	if st.Tries == nil {
		st.Tries = map[string]int{}
	}
	if st.Phase == PhasePending { // the previous run died during its gate (reboot, kill): finish it now
		if st = c.recover(ctx, st); st.Phase == PhaseRolledBack {
			return st, nil // this process is the build that was rolled back: the next run starts from the restored one
		}
	}
	d, err := c.decision(ctx)
	if err != nil {
		return st, err
	}
	if d.Release == nil {
		st.Phase, st.Reason, st.Error = PhaseCurrent, "no signed release on the dashboard (keyd)", ""
		c.save(st)
		return st, nil
	}
	rel, err := VerifyRelease(*d.Release, c.Key)
	if err != nil {
		return c.fail(st, "", err)
	}
	target, sums := rel.Version, rel.Files
	pinned := false
	if d.Pin != nil {
		p, err := VerifyPin(*d.Pin, c.Key, c.Host, c.now())
		if err != nil {
			return c.fail(st, "", fmt.Errorf("pin refused: %v", err))
		}
		target, sums, pinned = p.Version, p.Files, true
	}
	switch {
	case target == c.Current:
		st.Phase, st.Reason, st.Error = PhaseCurrent, map[bool]string{true: "pinned", false: "current"}[pinned], ""
		c.save(st)
		return st, nil
	case !pinned && !d.Update:
		st.Phase, st.Reason, st.Error = PhaseCurrent, d.Reason, ""
		c.save(st)
		return st, nil
	case !pinned && Compare(target, c.Current) < 0: // never a downgrade without a pin signed for this host
		st.Phase, st.Reason, st.Error = PhaseCurrent, "release "+target+" is older than the running "+c.Current+": not installed", ""
		c.save(st)
		return st, nil
	}
	name := "wo-agent-linux-" + c.Arch
	want, ok := sums[name]
	if !ok {
		return c.fail(st, target, fmt.Errorf("release %s has no build for linux/%s", target, c.Arch))
	}
	if !pinned && st.Tries[want.SHA256] >= MaxTries {
		st.Phase, st.Reason = PhaseCurrent, fmt.Sprintf("release %s failed its start check %d times: skipped until a newer one", target, MaxTries)
		c.save(st)
		return st, nil
	}
	bin, err := c.obtain(ctx, name, want)
	if err != nil {
		return c.fail(st, target, err)
	}
	if v, err := VersionOf(bin); err != nil || v != target {
		return c.fail(st, target, fmt.Errorf("binary version %q does not match the signed %s (%v)", v, target, err))
	}
	if err := c.install(ctx, bin, target); err != nil {
		return c.fail(st, target, err)
	}
	st.Phase, st.From, st.To, st.Reason, st.Error = PhasePending, c.Current, target, map[bool]string{true: "pinned", false: "release"}[pinned], ""
	c.save(st)
	c.logf("update: %s → %s installed, restarting %s", c.Current, target, strings.Join(c.Units, ", "))
	return c.gate(ctx, st, want.SHA256), nil
}

func (c *Config) fail(st State, target string, err error) (State, error) {
	st.Phase, st.To, st.Error = PhaseFailed, target, err.Error()
	st.From = c.Current
	c.save(st)
	return st, err
}

// obtain returns the verified bytes: the kept previous binary when it is the one asked for (a rollback needs no download),
// else the receiver's copy; either way sha256 and size must be the signed ones.
func (c *Config) obtain(ctx context.Context, name string, want FileSum) ([]byte, error) {
	if b, err := os.ReadFile(c.Binary + ".prev"); err == nil && int64(len(b)) == want.Size && sha256Hex(b) == want.SHA256 {
		return b, nil
	}
	if want.Size > 256<<20 {
		return nil, fmt.Errorf("signed size %d is implausible", want.Size)
	}
	b, code, err := c.get(ctx, "/agent/"+name, want.Size)
	if err != nil {
		return nil, fmt.Errorf("download %s: %v", name, err)
	}
	if code != http.StatusOK {
		return nil, fmt.Errorf("download %s: HTTP %d", name, code)
	}
	if int64(len(b)) != want.Size || sha256Hex(b) != want.SHA256 {
		return nil, fmt.Errorf("download %s: sha256 %s does not match the signed manifest", name, sha256Hex(b))
	}
	return b, nil
}

// install writes <bin>.new, proves it runs on this host, keeps the running binary as <bin>.prev and renames the new one over
// <bin> (one rename: a crash leaves either the old or the new binary, never half of one).
func (c *Config) install(ctx context.Context, bin []byte, version string) error {
	tmp := c.Binary + ".new"
	f, err := os.OpenFile(tmp, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
	if err != nil {
		return err
	}
	if _, err := f.Write(bin); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	f.Close()
	_ = os.Chmod(tmp, 0o755)
	cctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	out, err := exec.CommandContext(cctx, tmp, "version").CombinedOutput()
	cancel()
	if err != nil || !strings.Contains(string(out), version) {
		_ = os.Remove(tmp)
		return fmt.Errorf("the new binary does not run here (%v): %s", err, trimmed(out))
	}
	prevTmp := c.Binary + ".prev.tmp"
	_ = os.Remove(prevTmp)
	if err := os.Link(c.Binary, prevTmp); err != nil {
		b, rerr := os.ReadFile(c.Binary)
		if rerr != nil {
			_ = os.Remove(tmp)
			return fmt.Errorf("keep the previous binary: %v", err)
		}
		if err := os.WriteFile(prevTmp, b, 0o755); err != nil {
			_ = os.Remove(tmp)
			return err
		}
	}
	if err := os.Rename(prevTmp, c.Binary+".prev"); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, c.Binary); err != nil {
		return err
	}
	syncDir(filepath.Dir(c.Binary))
	return nil
}

func syncDir(d string) {
	if f, err := os.Open(d); err == nil {
		_ = f.Sync()
		f.Close()
	}
}

func (c *Config) restart() time.Time {
	at := c.now()
	for i, u := range c.Units {
		verb := "restart"
		if i > 0 {
			verb = "try-restart" // the actuator: restarted only when it runs; a dormant / stopped one stays as it was
		}
		if err := c.Systemctl(verb, u); err != nil {
			c.logf("update: systemctl %s %s: %v", verb, u, err)
		}
	}
	return at
}

// waitHello waits until the ship role of `version` wrote hello.json after `since`.
func (c *Config) waitHello(ctx context.Context, version string, since time.Time) bool {
	end := time.Now().Add(c.Gate)
	for {
		b, err := os.ReadFile(filepath.Join(c.Dir, "hello.json"))
		if err == nil {
			var h Hello
			if json.Unmarshal(b, &h) == nil && h.Version == version && h.TS >= since.UnixMilli() {
				return true
			}
		}
		if time.Now().After(end) {
			return false
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(300 * time.Millisecond):
		}
	}
}

// gate restarts the services on the new binary and waits for its hello; without one it puts the previous binary back.
func (c *Config) gate(ctx context.Context, st State, sha string) State {
	since := c.restart()
	if c.waitHello(ctx, st.To, since) {
		st.Phase, st.Error = PhaseUpdated, ""
		delete(st.Tries, sha)
		c.save(st)
		c.logf("update: %s reached the receiver: done", st.To)
		return st
	}
	st.Tries[sha]++
	return c.rollback(ctx, st, fmt.Sprintf("%s did not reach the receiver within %s", st.To, c.Gate))
}

func (c *Config) rollback(ctx context.Context, st State, why string) State {
	st.Phase, st.Error = PhaseRolledBack, why
	if err := os.Rename(c.Binary+".prev", c.Binary); err != nil {
		st.Error = why + "; rollback failed: " + err.Error()
		c.save(st)
		return st
	}
	syncDir(filepath.Dir(c.Binary))
	c.save(st)
	c.logf("update: %s; previous binary %s restored", why, st.From)
	since := c.restart()
	if !c.waitHello(ctx, st.From, since) {
		st.Error = why + "; the restored " + st.From + " has not reported back either"
		c.save(st)
	}
	return st
}

// recover finishes a run that died while pending: the new version reported back → updated, else roll back.
func (c *Config) recover(ctx context.Context, st State) State {
	b, err := os.ReadFile(filepath.Join(c.Dir, "hello.json"))
	var h Hello
	if err == nil && json.Unmarshal(b, &h) == nil && h.Version == st.To {
		if at, err := time.Parse(time.RFC3339, st.At); err == nil && h.TS >= at.UnixMilli() {
			st.Phase, st.Error = PhaseUpdated, ""
			c.save(st)
			return st
		}
	}
	if c.Current == st.To && st.From != "" {
		if _, err := os.Stat(c.Binary + ".prev"); err == nil {
			return c.rollback(ctx, st, "the update run stopped before "+st.To+" reported back")
		}
	}
	st.Phase = PhaseCurrent
	c.save(st)
	return st
}
