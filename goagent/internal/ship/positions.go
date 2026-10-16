package ship

import (
	"encoding/hex"
	"encoding/json"
	"hash/fnv"
	"io"
	"os"
	"path/filepath"
	"sort"
	"syscall"
	"time"
)

// Positions: what has been handed to the receiver (delivered or spooled), per file identity (device + inode), so a
// restart neither re-sends nor skips lines. A file is recognised by its inode, checked against a fingerprint of its first
// bytes (inode numbers are reused after a delete); the fingerprint also recognises a copy of a file already shipped
// (copytruncate into a name the globs match).

const fpMax = 256 // bytes hashed for a file's fingerprint

type fileID struct{ Dev, Ino uint64 }

func idOf(fi os.FileInfo) (fileID, bool) {
	st, ok := fi.Sys().(*syscall.Stat_t)
	if !ok {
		return fileID{}, false
	}
	return fileID{uint64(st.Dev), uint64(st.Ino)}, true //nolint:unconvert // Dev is int32 / uint64 depending on the platform
}

type posEntry struct {
	Path   string `json:"path"`
	Dev    uint64 `json:"dev"`
	Ino    uint64 `json:"ino"`
	Off    int64  `json:"off"`
	FP     string `json:"fp,omitempty"`
	FPN    int    `json:"fpn,omitempty"`
	Active bool   `json:"active,omitempty"` // the current file of its path when saved (a restart looks for it if rotated meanwhile)
	Seen   int64  `json:"seen"`
}

func (e *posEntry) id() fileID { return fileID{e.Dev, e.Ino} }

type posDoc struct {
	V        int        `json:"v"`
	Agent    string     `json:"agent"`
	Patterns []string   `json:"patterns"`
	Files    []posEntry `json:"files"`
	Ghosts   []ghost    `json:"ghosts,omitempty"`
	Saved    string     `json:"saved"`
}

// ghost: a content truncated away, shipped up to Off; Exp is when it is forgotten (unix s).
type ghost struct {
	FP  string `json:"fp"`
	Off int64  `json:"off"`
	Exp int64  `json:"exp"`
}

type registry struct {
	path     string
	entries  map[fileID]*posEntry
	ghosts   map[string]ghost // content truncated away (copytruncate), by fingerprint: a copy of it continues where it was shipped
	prevPats map[string]bool  // patterns of the previous run
	hadState bool
	dirty    bool
}

func loadRegistry(path string) *registry {
	r := &registry{path: path, entries: map[fileID]*posEntry{}, ghosts: map[string]ghost{}, prevPats: map[string]bool{}}
	if path == "" {
		return r
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return r
	}
	var d posDoc
	if json.Unmarshal(b, &d) != nil || d.V != 1 {
		return r
	}
	r.hadState = true
	for _, p := range d.Patterns {
		r.prevPats[p] = true
	}
	for i := range d.Files {
		e := d.Files[i]
		r.entries[e.id()] = &e
	}
	for _, g := range d.Ghosts {
		r.ghosts[g.FP] = g
	}
	return r
}

// save writes the registry atomically (temporary file + rename, 0600).
func (r *registry) save(patterns []string, agent string) error {
	if r.path == "" {
		r.dirty = false
		return nil
	}
	d := posDoc{V: 1, Agent: agent, Patterns: patterns, Files: make([]posEntry, 0, len(r.entries)), Saved: time.Now().UTC().Format(time.RFC3339)}
	for _, e := range r.entries {
		d.Files = append(d.Files, *e)
	}
	sort.Slice(d.Files, func(i, j int) bool {
		if d.Files[i].Path != d.Files[j].Path {
			return d.Files[i].Path < d.Files[j].Path
		}
		return d.Files[i].Ino < d.Files[j].Ino
	})
	now := time.Now().Unix()
	for _, g := range r.ghosts {
		if g.Exp > now {
			d.Ghosts = append(d.Ghosts, g)
		}
	}
	sort.Slice(d.Ghosts, func(i, j int) bool { return d.Ghosts[i].FP < d.Ghosts[j].FP })
	b, err := json.Marshal(d)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(r.path), 0o700); err != nil {
		return err
	}
	tmp := r.path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	if err := os.Rename(tmp, r.path); err != nil {
		return err
	}
	r.dirty = false
	return nil
}

// expire forgets files not seen for an hour that are not open, and ghosts past their time.
func (r *registry) expire(open map[fileID]*harvester) {
	cut := time.Now().Add(-time.Hour).Unix()
	for id, e := range r.entries {
		if open[id] == nil && e.Seen < cut {
			delete(r.entries, id)
			r.dirty = true
		}
	}
	now := time.Now().Unix()
	for fp, g := range r.ghosts {
		if g.Exp <= now {
			delete(r.ghosts, fp)
		}
	}
}

// fingerprint hashes the first n bytes of f (n <= fpMax); "" when fewer are readable.
func fingerprint(f io.ReaderAt, n int) string {
	if n <= 0 {
		return ""
	}
	if n > fpMax {
		n = fpMax
	}
	buf := make([]byte, n)
	if k, err := f.ReadAt(buf, 0); k < n && err != nil {
		return ""
	}
	h := fnv.New64a()
	_, _ = h.Write(buf)
	return hex.EncodeToString(h.Sum(nil))
}

// copyStart: is a new file (full-length fingerprint fp, size bytes) a copy of content already followed? Then it starts where
// that content was shipped: a copy of a truncated file (copytruncate into a name the globs match) ships only the lines the
// original lost; a copy of a file still followed ships nothing (the original covers it). A file that only shares its first
// bytes with a longer one (the same banner at the top of every new log) is not a copy.
func (r *registry) copyStart(fp string, size int64, open map[fileID]*harvester) (int64, bool) {
	if fp == "" {
		return 0, false
	}
	if g, ok := r.ghosts[fp]; ok {
		return min(g.Off, size), true
	}
	for id, e := range r.entries {
		if e.FPN != fpMax || e.FP != fp {
			continue
		}
		if h := open[id]; h != nil && !h.rotated {
			return size, true
		}
		if size >= e.Off {
			return e.Off, true
		}
	}
	return 0, false
}
