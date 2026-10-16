package act

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// The local ledger (§9.3.6): one hash-chained line per executed or rejected job in state/ledger.jsonl,
// h = sha256(prev ‖ canonical(line without h)). The highest seq also lives in state/last_seq and in every line, so a lost
// seen.json never re-opens a seq replay (R6). The dashboard compares (n, head) with what it can account for and asks for
// the lines (ledger_from) when they differ.

type Ledger struct {
	mu      sync.Mutex
	dir     string
	n       int
	head    string
	lastSeq int64
	lines   []map[string]any
	Broken  string // why the chain did not verify on load ("" = intact)
}

// OpenLedger loads and verifies state/ledger.jsonl.
func OpenLedger(stateDir string) (*Ledger, error) {
	l := &Ledger{dir: stateDir}
	f, err := os.Open(filepath.Join(stateDir, "ledger.jsonl"))
	if err == nil {
		sc := bufio.NewScanner(f)
		sc.Buffer(make([]byte, 1<<20), 1<<20)
		for sc.Scan() {
			if strings.TrimSpace(sc.Text()) == "" {
				continue
			}
			var m map[string]any
			if err := Decode(sc.Bytes(), &m); err != nil {
				l.Broken = fmt.Sprintf("line %d unreadable", l.n+1)
				break
			}
			h, _ := m["h"].(string)
			prev, _ := m["prev"].(string)
			if prev != l.head || lineHash(m) != h {
				l.Broken = fmt.Sprintf("chain broken at line %d", l.n+1)
			}
			l.n++
			l.head = h
			if s, ok := numOpt(m, "seq"); ok && s > l.lastSeq {
				l.lastSeq = s
			}
			l.lines = append(l.lines, m)
		}
		f.Close()
	} else if !os.IsNotExist(err) {
		return nil, err
	}
	if b, err := os.ReadFile(filepath.Join(stateDir, "last_seq")); err == nil {
		if s, err := strconv.ParseInt(strings.TrimSpace(string(b)), 10, 64); err == nil && s > l.lastSeq {
			l.lastSeq = s
		}
	}
	return l, nil
}

func lineHash(m map[string]any) string {
	cp := map[string]any{}
	for k, v := range m {
		if k != "h" {
			cp[k] = v
		}
	}
	prev, _ := m["prev"].(string)
	h := sha256.Sum256(append([]byte(prev), MustCanonical(cp)...))
	return hex.EncodeToString(h[:])
}

// State is (n, head, last_seq) as reported in hello and every result.
func (l *Ledger) State() map[string]any {
	l.mu.Lock()
	defer l.mu.Unlock()
	return map[string]any{"n": l.n, "head": l.head, "last_seq": l.lastSeq}
}

func (l *Ledger) LastSeq() int64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.lastSeq
}

// SetLastSeq records a delivered seq durably before the job runs.
func (l *Ledger) SetLastSeq(seq int64) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if seq <= l.lastSeq {
		return nil
	}
	l.lastSeq = seq
	return writeFileDurable(filepath.Join(l.dir, "last_seq"), []byte(strconv.FormatInt(seq, 10)+"\n"))
}

// Append writes one line (n, ts, prev, h filled in) and fsyncs it; the stored line.
func (l *Ledger) Append(line map[string]any) (map[string]any, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	line["n"] = l.n + 1
	if _, ok := line["ts"]; !ok {
		line["ts"] = time.Now().UTC().Format("2006-01-02T15:04:05Z")
	}
	line["prev"] = l.head
	norm, err := normalise(line)
	if err != nil {
		return nil, err
	}
	m := norm.(map[string]any)
	m["h"] = lineHash(m)
	b := MustCanonical(m)
	f, err := os.OpenFile(filepath.Join(l.dir, "ledger.jsonl"), os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return nil, err
	}
	if _, err := f.Write(append(b, '\n')); err != nil {
		f.Close()
		return nil, err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return nil, err
	}
	f.Close()
	l.n++
	l.head = m["h"].(string)
	l.lines = append(l.lines, m)
	return m, nil
}

// LinesFrom are the lines after n (for ledger_from), at most max.
func (l *Ledger) LinesFrom(n, max int) []map[string]any {
	l.mu.Lock()
	defer l.mu.Unlock()
	if n < 0 {
		n = 0
	}
	if n >= len(l.lines) {
		return nil
	}
	out := l.lines[n:]
	if len(out) > max {
		out = out[:max]
	}
	return append([]map[string]any(nil), out...)
}

// Started is when a job began (its ledger line's "started"), for the manifest-free rollback.
func (l *Ledger) Started(jobID string) (time.Time, bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	for i := len(l.lines) - 1; i >= 0; i-- {
		if l.lines[i]["job_id"] == jobID {
			s, _ := l.lines[i]["started"].(string)
			t, err := time.Parse(time.RFC3339Nano, s)
			return t, err == nil
		}
	}
	return time.Time{}, false
}

// CountSince counts executed (not rejected) lines in the last window matching the filter.
func (l *Ledger) CountSince(since time.Time, match func(map[string]any) bool) int {
	l.mu.Lock()
	defer l.mu.Unlock()
	n := 0
	for i := len(l.lines) - 1; i >= 0; i-- {
		m := l.lines[i]
		ts, _ := m["ts"].(string)
		t, err := time.Parse("2006-01-02T15:04:05Z", ts)
		if err == nil && t.Before(since) {
			break
		}
		if m["status"] != "rejected" && match(m) {
			n++
		}
	}
	return n
}

// ---------------------------------------------------------------- seen.json (7 days of job ids and nonces)

type Seen struct {
	mu   sync.Mutex
	path string
	Jobs map[string]seenEntry `json:"jobs"`
}

type seenEntry struct {
	Nonce string `json:"nonce"`
	TS    int64  `json:"ts"`
}

func OpenSeen(stateDir string) *Seen {
	s := &Seen{path: filepath.Join(stateDir, "seen.json"), Jobs: map[string]seenEntry{}}
	if b, err := os.ReadFile(s.path); err == nil {
		_ = json.Unmarshal(b, s)
		if s.Jobs == nil {
			s.Jobs = map[string]seenEntry{}
		}
	}
	return s
}

// Has reports a job id or nonce received in the last 7 days.
func (s *Seen) Has(jobID, nonce string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.Jobs[jobID]; ok {
		return true
	}
	for _, e := range s.Jobs {
		if nonce != "" && e.Nonce == nonce {
			return true
		}
	}
	return false
}

// Add records a job and prunes entries older than 7 days.
func (s *Seen) Add(jobID, nonce string, now time.Time) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.Jobs[jobID] = seenEntry{Nonce: nonce, TS: now.Unix()}
	cut := now.Add(-7 * 24 * time.Hour).Unix()
	for k, e := range s.Jobs {
		if e.TS < cut {
			delete(s.Jobs, k)
		}
	}
	b, _ := json.Marshal(s)
	return writeFileDurable(s.path, b)
}

// ---------------------------------------------------------------- result spool (re-sent until acknowledged)

type Spool struct{ dir string }

func NewSpool(stateDir string) *Spool {
	d := filepath.Join(stateDir, "results")
	_ = os.MkdirAll(d, 0o700)
	return &Spool{dir: d}
}

func (s *Spool) path(jobID string) string { return filepath.Join(s.dir, jobID+".json") }

// Put stores a signed result envelope (pending until Ack).
func (s *Spool) Put(jobID string, env map[string]any) error {
	b, err := json.Marshal(map[string]any{"env": env, "acked": false})
	if err != nil {
		return err
	}
	return writeFileDurable(s.path(jobID), b)
}

// Ack marks a result acknowledged (kept 7 days so a duplicate job gets the stored result).
func (s *Spool) Ack(jobID string) {
	if env, _, ok := s.Get(jobID); ok {
		b, _ := json.Marshal(map[string]any{"env": env, "acked": true})
		_ = writeFileDurable(s.path(jobID), b)
	}
}

// Get returns a stored envelope and whether it was acknowledged.
func (s *Spool) Get(jobID string) (map[string]any, bool, bool) {
	b, err := os.ReadFile(s.path(jobID))
	if err != nil {
		return nil, false, false
	}
	var m struct {
		Env   map[string]any `json:"env"`
		Acked bool           `json:"acked"`
	}
	if Decode(b, &m) != nil || m.Env == nil {
		return nil, false, false
	}
	return m.Env, m.Acked, true
}

// Pending lists unacknowledged job ids; acknowledged files older than 7 days are removed.
func (s *Spool) Pending(now time.Time) []string {
	ents, _ := os.ReadDir(s.dir)
	var out []string
	for _, e := range ents {
		id, ok := strings.CutSuffix(e.Name(), ".json")
		if !ok {
			continue
		}
		_, acked, found := s.Get(id)
		if !found {
			continue
		}
		if !acked {
			out = append(out, id)
		} else if fi, err := e.Info(); err == nil && now.Sub(fi.ModTime()) > 7*24*time.Hour {
			os.Remove(s.path(id))
		}
	}
	return out
}
