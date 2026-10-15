package parse

import (
	"crypto/sha1"
	"encoding/hex"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"watchover/receiver/parse/py"
)

// Owner: WP2 (P01 "Profiles", P02 section 12, P06 section 7.2).

// Profile is one learned (or user) column mapping of profiles.json.
type Profile struct {
	ID, Format, Source string
	Keys               []string
	Mapping            *Mapping
}

// ProfileStore is the parse-time view of profiles.py.
type ProfileStore interface {
	// Lookup is profiles.lookup: the stored profile for the shape if its mapping is non-empty, else nil.
	Lookup(format string, keys []string) *Profile
	// Remember is profiles.remember(fmt, keys, {role: key for truthy roles}, name, source).
	Remember(format string, keys []string, roles Roles, name, source string)
}

// Signature is profiles.signature: sha1(fmt + "|" + ",".join(sorted(keys))).hexdigest()[:16].
func Signature(format string, keys []string) string {
	ks := append([]string(nil), keys...)
	sort.Strings(ks)
	h := sha1.Sum([]byte(format + "|" + strings.Join(ks, ",")))
	return hex.EncodeToString(h[:])[:16]
}

// profileRoles is profiles.ROLES (the order of a cleaned mapping follows the caller's dict, i.e. auto_map role order).
var profileRoles = map[string]bool{"timestamp": true, "severity": true, "service": true, "host": true, "message": true,
	"environment": true, "origin": true}

// rolesMapping is {k: v for k, v in roles.items() if v} restricted to ROLES, in auto_map role order.
func rolesMapping(roles Roles) *py.Dict {
	d := py.NewDict(NumRoles)
	for i := 0; i < NumRoles; i++ {
		if k, ok := roles.Key(i); ok {
			d.Set(RoleNames[i], py.S(k))
		}
	}
	return d
}

type memProfile struct {
	id, format, source string
	keys               []string
	mapping            *py.Dict
	hits               int
}

// MemStore is an in-memory profile store (golden tests: one per run, starts empty) with profiles.remember semantics.
type MemStore struct {
	mu sync.Mutex
	m  map[string]*memProfile
}

// NewMemStore returns an empty store.
func NewMemStore() *MemStore { return &MemStore{m: map[string]*memProfile{}} }

// Lookup implements ProfileStore.
func (s *MemStore) Lookup(format string, keys []string) *Profile {
	s.mu.Lock()
	defer s.mu.Unlock()
	p := s.m[Signature(format, keys)]
	if p == nil || p.mapping.Len() == 0 {
		return nil
	}
	return &Profile{ID: p.id, Format: p.format, Source: p.source, Keys: append([]string(nil), p.keys...),
		Mapping: NewMapping(p.mapping.Clone())}
}

// Remember implements ProfileStore (profiles.remember: a user profile is never overwritten by an auto one).
func (s *MemStore) Remember(format string, keys []string, roles Roles, name, source string) {
	if len(keys) == 0 {
		return
	}
	clean := rolesMapping(roles)
	if clean.Len() == 0 {
		return
	}
	sig := Signature(format, keys)
	s.mu.Lock()
	defer s.mu.Unlock()
	cur := s.m[sig]
	if cur != nil && cur.source == "user" && source != "user" {
		cur.hits++
		return
	}
	sorted := append([]string(nil), keys...)
	sort.Strings(sorted)
	if len(sorted) > 60 {
		sorted = sorted[:60]
	}
	hits := 1
	if cur != nil {
		hits = cur.hits + 1
	}
	s.m[sig] = &memProfile{id: sig, format: format, source: source, keys: sorted, mapping: clean, hits: hits}
}

// FileSnapshot is the receiver's read-only view of a profiles.json written by Python (reloaded when its mtime or
// size changes, checked at most every Interval); Remember is a no-op (Python owns the file).
type FileSnapshot struct {
	Path     string
	Interval time.Duration // default 2s

	mu       sync.Mutex
	checked  time.Time
	mtime    time.Time
	size     int64
	profiles *py.Dict // signature -> record (a dict); nil = empty
	scenario *py.Dict // top-level "_scenario_mapping" (scenario.MAPPING export)
}

// NewFileSnapshot returns a snapshot of path (missing file = no profiles).
func NewFileSnapshot(path string) *FileSnapshot {
	return &FileSnapshot{Path: path, Interval: 2 * time.Second}
}

func (f *FileSnapshot) refresh() {
	now := time.Now()
	iv := f.Interval
	if iv <= 0 {
		iv = 2 * time.Second
	}
	if !f.checked.IsZero() && now.Sub(f.checked) < iv {
		return
	}
	f.checked = now
	st, err := os.Stat(f.Path)
	if err != nil {
		f.profiles, f.scenario, f.mtime, f.size = nil, nil, time.Time{}, 0
		return
	}
	if st.ModTime().Equal(f.mtime) && st.Size() == f.size && f.profiles != nil {
		return
	}
	f.mtime, f.size = st.ModTime(), st.Size()
	f.profiles, f.scenario = nil, nil
	data, err := os.ReadFile(f.Path)
	if err != nil {
		return
	}
	v, err := py.Loads(string(data)) // read_text(encoding="utf-8"): a BOM makes json.loads fail -> {}
	if err != nil || v.K != py.KMap {
		return // profiles.load: invalid file -> {}
	}
	f.profiles = v.M
	if sm, ok := v.M.Get("_scenario_mapping"); ok && sm.K == py.KMap {
		f.scenario = sm.M
	}
}

// record returns the stored record for the shape (nil when absent or not a dict).
func (f *FileSnapshot) record(format string, keys []string) *py.Dict {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.refresh()
	if f.profiles == nil {
		return nil
	}
	v, ok := f.profiles.Get(Signature(format, keys))
	if !ok || v.K != py.KMap {
		return nil
	}
	return v.M
}

// Lookup implements ProfileStore (profiles.lookup).
func (f *FileSnapshot) Lookup(format string, keys []string) *Profile {
	rec := f.record(format, keys)
	if rec == nil {
		return nil
	}
	mv, ok := rec.Get("mapping")
	if !ok || mv.K != py.KMap || mv.M.Len() == 0 {
		return nil
	}
	p := &Profile{Format: format, Mapping: NewMapping(mv.M.Clone())}
	if id, ok := rec.Get("id"); ok && id.K == py.KStr {
		p.ID = id.S
	}
	if src, ok := rec.Get("source"); ok && src.K == py.KStr {
		p.Source = src.S
	}
	if ks, ok := rec.Get("keys"); ok && ks.K == py.KList {
		for _, k := range ks.L {
			p.Keys = append(p.Keys, k.Str())
		}
	}
	return p
}

// Remember implements ProfileStore (no-op: Python owns profiles.json).
func (f *FileSnapshot) Remember(format string, keys []string, roles Roles, name, source string) {}

// HasUser reports a user-made profile for the shape that lookup would apply (the batch is then forwarded).
func (f *FileSnapshot) HasUser(format string, keys []string) bool {
	p := f.Lookup(format, keys)
	return p != nil && p.Source == "user"
}

// ScenarioMapping is the exported scenario.MAPPING (nil or empty = none; non-empty -> batches are forwarded).
func (f *FileSnapshot) ScenarioMapping() *py.Dict {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.refresh()
	return f.scenario
}
