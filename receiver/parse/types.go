// Package parse is the Go port of the Python parse step (src/watchover: loader.decode, pipeline.ingest_bytes,
// format_detector, parsers/*, normalize, profiles) with byte-for-byte parity on the goldens in testdata/golden.
// Design and work packages: docs/go-parse/PLAN.md; semantics: docs/go-parse/01..06-*.md.
package parse

import (
	"errors"
	"fmt"

	"watchover/receiver/parse/py"
)

// Observation is models.Observation as produced by parsing (P01 section 13). Encoding is done by canon.go only.
type Observation struct {
	TS               py.DateTime `json:"ts"` // written with DateTime.ISO()
	Message          string      `json:"message"`
	Kind             string      `json:"kind"`     // log|event|alert|incident
	Severity         string      `json:"severity"` // SEV_RANK names
	Service          string      `json:"service"`
	Host             string      `json:"host"`
	Environment      string      `json:"environment"`
	Origin           string      `json:"origin"`
	Attributes       *py.Dict    `json:"attributes"` // values: str, int (int64/big), float, bool, None
	Source           string      `json:"source"`
	LineNo           int         `json:"line_no"`
	Parser           string      `json:"parser"`
	ParserConfidence float64     `json:"parser_confidence"` // unrounded; canonical writes round(x, 4)
	Raw              string      `json:"raw"`
}

// Role indexes in auto_map / ROLE_HINTS order (P02 section 10).
const (
	RoleTimestamp = iota
	RoleSeverity
	RoleService
	RoleEnvironment
	RoleOrigin
	RoleHost
	RoleMessage
	NumRoles
)

// RoleNames are the seven role names in ROLE_HINTS order.
var RoleNames = [NumRoles]string{"timestamp", "severity", "service", "environment", "origin", "host", "message"}

// Roles is the auto_map result: role -> key, Has[i] false = None. A role can map to the key "" (an empty CSV header).
type Roles struct {
	Timestamp, Severity, Service, Environment, Origin, Host, Message string
	Has                                                              [NumRoles]bool
}

// Get returns role i (Role* constant) and whether it is set (not None).
func (r *Roles) Get(i int) (string, bool) {
	if !r.Has[i] {
		return "", false
	}
	return *r.ptr(i), true
}

// Set assigns role i.
func (r *Roles) Set(i int, key string) {
	*r.ptr(i) = key
	r.Has[i] = true
}

// Clear sets role i to None.
func (r *Roles) Clear(i int) {
	*r.ptr(i) = ""
	r.Has[i] = false
}

// Key returns role i when it is truthy (set and non-empty): the Python `if roles[x]` test.
func (r *Roles) Key(i int) (string, bool) {
	k, ok := r.Get(i)
	return k, ok && k != ""
}

func (r *Roles) ptr(i int) *string {
	switch i {
	case RoleTimestamp:
		return &r.Timestamp
	case RoleSeverity:
		return &r.Severity
	case RoleService:
		return &r.Service
	case RoleEnvironment:
		return &r.Environment
	case RoleOrigin:
		return &r.Origin
	case RoleHost:
		return &r.Host
	case RoleMessage:
		return &r.Message
	}
	panic(fmt.Sprintf("parse: bad role index %d", i))
}

// Report is one pipeline report entry (P01 section 2.5) without sha/table.
type Report struct {
	File         string
	Format       string // "" only for the empty live-batch report {} (GoldenLive)
	Confidence   float64
	Rows         int
	Family       string   // "" when none
	Kind         string   // "-" when no rows
	Keys         []string // first-seen order over the first 50 probe records
	Roles        Roles
	Profile      string // "" or 16-hex id
	DedupDropped int    // written only when > 0
}

// Hint is the live per-stream hint (live.py _detect value).
type Hint struct {
	Format     string  // must be a key of Registry to be honoured
	Confidence float64 // default 0.9
	Family     string
	HasFamily  bool // Python: "family" in hint (true for every cached hint, even when Family == "")
	N          int
}

// Record is one (line_no, record dict) pair yielded by Parser.Records.
type Record struct {
	LineNo int
	Rec    *py.Dict
}

// FamilyCtx replaces the FAMILY_HINT / FAMILY_SEEN contextvars (P01 section 6 steps 4/12, P03 section 4.4).
type FamilyCtx struct {
	Hint *string // nil = unset; non-nil "" = hint present and empty (disables detection)
	Seen *string // set by detectFamily; nil -> report family ""
}

// Parser is one entry of the PARSERS registry.
type Parser interface {
	Name() string
	Kind() string // default kind
	LineOriented() bool
	// Records mirrors the Python generator: yield returns false to stop (the 50-record probe).
	// An error is a Python exception that escaped records() (wrap ErrPyRaise), or ErrForward.
	Records(text string, fc *FamilyCtx, yield func(Record) bool) error
}

var (
	// ErrPyRaise: Python raises here, so the whole ingest fails in Python -> Go forwards the batch raw.
	ErrPyRaise = errors.New("python would raise")
	// ErrForward: not handled in Go (archive, side table, non-UTF-8, unverified dateutil shape, NaN, not implemented).
	ErrForward = errors.New("not handled in go")
)

// ErrNotImplemented is returned by the stubs of work packages that have not landed yet (wraps ErrForward).
var ErrNotImplemented = fmt.Errorf("%w: not implemented yet", ErrForward)

// pyRaise wraps ErrPyRaise with the Python exception it stands for.
func pyRaise(format string, args ...any) error {
	return fmt.Errorf("%w: "+format, append([]any{ErrPyRaise}, args...)...)
}

// forward wraps ErrForward with a reason.
func forward(format string, args ...any) error {
	return fmt.Errorf("%w: "+format, append([]any{ErrForward}, args...)...)
}
