package ship

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// Spool keeps undeliverable batches on disk in agent.py's format ("<unix ns>__<file name>", 0600 files in a 0700 directory),
// so a migrated host drains what the Python agent left behind and agent.py could drain ours. It is bounded by a batch count
// (agent.py's 2.000) and a byte cap; the oldest batches give way first. The index lives in memory: no directory listing per
// write.
type Spool struct {
	dir      string
	maxFiles int
	maxBytes int64

	mu    sync.Mutex
	items []spoolItem // oldest first
	bytes int64
	last  int64
	// Evicted counts batches removed by the caps (the caller adds them to its dropped counter).
	Evicted int64
}

type spoolItem struct {
	name string
	size int64
}

// OpenSpool creates (0700) or opens dir and indexes the batches already there.
func OpenSpool(dir string, maxFiles int, maxBytes int64) (*Spool, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	_ = os.Chmod(dir, 0o700) // spooled batches may hold privileged log lines
	s := &Spool{dir: dir, maxFiles: maxFiles, maxBytes: maxBytes}
	ents, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	for _, e := range ents {
		n := e.Name()
		if strings.HasPrefix(n, ".tmp-") { // a write torn by a crash: never complete, never sent
			_ = os.Remove(filepath.Join(dir, n))
			continue
		}
		if strings.HasPrefix(n, ".") || !strings.Contains(n, "__") || e.IsDir() {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		s.items = append(s.items, spoolItem{n, info.Size()})
		s.bytes += info.Size()
	}
	sort.Slice(s.items, func(i, j int) bool { return s.items[i].name < s.items[j].name })
	return s, nil
}

// Len is the number of batches waiting.
func (s *Spool) Len() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.items)
}

// Bytes is the size of the batches waiting.
func (s *Spool) Bytes() int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.bytes
}

// Put stores one batch (atomically: a temporary file renamed into place) and applies the caps.
func (s *Spool) Put(body []byte, name string) error {
	s.mu.Lock()
	ns := time.Now().UnixNano()
	if ns <= s.last {
		ns = s.last + 1
	}
	s.last = ns
	s.mu.Unlock()
	final := fmt.Sprintf("%d__%s", ns, filepath.Base(name))
	tmp := filepath.Join(s.dir, ".tmp-"+final)
	if err := os.WriteFile(tmp, body, 0o600); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, filepath.Join(s.dir, final)); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.items = append(s.items, spoolItem{final, int64(len(body))})
	s.bytes += int64(len(body))
	for len(s.items) > 1 && (len(s.items) > s.maxFiles || (s.maxBytes > 0 && s.bytes > s.maxBytes)) {
		old := s.items[0]
		s.items = s.items[1:]
		s.bytes -= old.size
		_ = os.Remove(filepath.Join(s.dir, old.name))
		s.Evicted++
	}
	return nil
}

// Oldest returns the oldest batch (its spool name, the X-File-Name to send it under, and its bytes).
func (s *Spool) Oldest() (key, name string, body []byte, ok bool) {
	for {
		s.mu.Lock()
		if len(s.items) == 0 {
			s.mu.Unlock()
			return "", "", nil, false
		}
		it := s.items[0]
		s.mu.Unlock()
		b, err := os.ReadFile(filepath.Join(s.dir, it.name))
		if err != nil { // removed behind our back: forget it
			s.Remove(it.name)
			continue
		}
		return it.name, strings.SplitN(it.name, "__", 2)[1], b, true
	}
}

// Remove deletes a batch (delivered, or refused for good).
func (s *Spool) Remove(key string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for i, it := range s.items {
		if it.name == key {
			s.items = append(s.items[:i], s.items[i+1:]...)
			s.bytes -= it.size
			break
		}
	}
	_ = os.Remove(filepath.Join(s.dir, key))
}
